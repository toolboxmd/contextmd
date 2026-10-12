"""Read-only access to Chromeria V2's thread records on this machine.

Chromeria keeps each thread's user messages, assistant text and tool items in
``chromeria-v2.sqlite``. Capture renders transcripts from these rows and never
writes the database.
"""

from __future__ import annotations

import json
import sqlite3
from dataclasses import dataclass, field
from pathlib import Path
from urllib.parse import unquote

THREADS = "orchestration_v2_projection_threads"
ITEMS = "orchestration_v2_projection_turn_items"
RUNS = "orchestration_v2_projection_runs"


def connect(path: str) -> sqlite3.Connection:
    con = sqlite3.connect(Path(path).absolute().as_uri() + "?mode=ro", uri=True)
    con.row_factory = sqlite3.Row
    return con


@dataclass
class Thread:
    thread_id: str
    title: str
    created_at: str
    created_by: str | None
    creation_source: str | None
    parent_id: str | None
    provider: str | None
    model: str | None
    payload: dict = field(repr=False, default_factory=dict)

    @property
    def task_name(self) -> str | None:
        """The delegate_task clientRequestId a delegated thread was created with."""
        marker = "delegate-task%3A"
        if marker not in self.thread_id:
            return None
        return unquote(self.thread_id.rsplit(marker, 1)[1])


@dataclass
class Item:
    """One Chromeria turn item, the unit a transcript line comes from."""

    item_id: str
    thread_id: str
    ordinal: int
    ts: str
    type: str
    payload: dict = field(repr=False, default_factory=dict)

    @property
    def native_id(self) -> str | None:
        ref = self.payload.get("nativeItemRef") or {}
        return ref.get("nativeId") if isinstance(ref, dict) else None


def _thread(row) -> Thread:
    p = json.loads(row["payload_json"])
    lineage = p.get("lineage") or {}
    ms = p.get("modelSelection") or {}
    return Thread(
        thread_id=row["thread_id"], title=p.get("title") or "",
        created_at=p.get("createdAt") or "", created_by=p.get("createdBy"),
        creation_source=p.get("creationSource"),
        parent_id=lineage.get("parentThreadId"),
        provider=ms.get("instanceId") or p.get("providerInstanceId"),
        model=ms.get("model"), payload=p)


def threads(con: sqlite3.Connection, ids=None) -> dict[str, Thread]:
    sql = f"SELECT thread_id, payload_json FROM {THREADS}"
    rows = con.execute(sql).fetchall()
    out = {r["thread_id"]: _thread(r) for r in rows}
    if ids is not None:
        out = {k: v for k, v in out.items() if k in set(ids)}
    return out


def _item(row) -> Item:
    p = json.loads(row["payload_json"])
    ts = p.get("startedAt") or p.get("updatedAt") or row["updated_at"] or ""
    return Item(item_id=row["turn_item_id"], thread_id=row["thread_id"],
                ordinal=row["ordinal"], ts=ts, type=row["type"], payload=p)


def active_thread_ids(con: sqlite3.Connection, since: str, until: str) -> list[str]:
    """Threads with at least one turn item that started in [since, until)."""
    rows = con.execute(
        f"SELECT DISTINCT thread_id FROM {ITEMS}"
        " WHERE coalesce(json_extract(payload_json, '$.startedAt'), updated_at) >= ?"
        " AND coalesce(json_extract(payload_json, '$.startedAt'), updated_at) < ?",
        (since, until)).fetchall()
    return sorted(r[0] for r in rows)


def items(con: sqlite3.Connection, thread_id: str) -> list[Item]:
    rows = con.execute(
        f"SELECT turn_item_id, thread_id, ordinal, type, updated_at, payload_json"
        f" FROM {ITEMS} WHERE thread_id=? ORDER BY ordinal, turn_item_id",
        (thread_id,)).fetchall()
    return [_item(r) for r in rows]


def run_window(con: sqlite3.Connection, thread_id: str) -> tuple[str | None, str | None]:
    """First start and last completion of a thread's runs (a delegated job's wall time)."""
    rows = con.execute(f"SELECT payload_json FROM {RUNS} WHERE thread_id=? ORDER BY ordinal",
                       (thread_id,)).fetchall()
    if not rows:
        return None, None
    first, last = json.loads(rows[0][0]), json.loads(rows[-1][0])
    return first.get("startedAt"), last.get("completedAt")


def run_finished(con: sqlite3.Connection, thread_id: str) -> bool | None:
    """Whether a thread's latest run has completed; None when it has no runs.

    A model may write its output file in several steps, so a delegated job's
    output counts only once its run is over.
    """
    row = con.execute(f"SELECT payload_json FROM {RUNS} WHERE thread_id=? ORDER BY ordinal DESC LIMIT 1",
                      (thread_id,)).fetchone()
    if row is None:
        return None
    return bool(json.loads(row[0]).get("completedAt"))
