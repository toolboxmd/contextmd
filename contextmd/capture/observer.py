"""Agent Observer: the sync gate, moment anchors and model evidence.

Capture reads Observer's ledger read-only after a successful
``agent-observer sync``. Observer maps each Chromeria thread to its native
session; every inbox item is anchored to an Observer event or submission in
that session, and every delegated job's actual model and token use come from
the job's own session.
"""

from __future__ import annotations

import hashlib
import json
import sqlite3
import subprocess
from pathlib import Path


class SyncFailed(RuntimeError):
    pass


def sync(observer_bin: str) -> dict:
    """Run ``agent-observer sync``; any failure fails the capture pass."""
    try:
        proc = subprocess.run([observer_bin, "sync", "--json"], capture_output=True,
                              text=True, timeout=1800)
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise SyncFailed(f"agent-observer sync did not run: {exc}") from exc
    if proc.returncode != 0:
        raise SyncFailed(f"agent-observer sync exited {proc.returncode}: {proc.stderr.strip()[-500:]}")
    try:
        report = json.loads(proc.stdout)
    except ValueError as exc:
        raise SyncFailed("agent-observer sync printed no JSON report") from exc
    failed = [h for h in report.get("harnesses", report.get("results", []))
              if isinstance(h, dict) and h.get("failed")]
    if failed:
        names = ", ".join(str(h.get("harness")) for h in failed)
        raise SyncFailed(f"agent-observer sync reported failed sources for: {names}")
    return report


def connect(path: str) -> sqlite3.Connection:
    con = sqlite3.connect(Path(path).absolute().as_uri() + "?mode=ro", uri=True)
    con.row_factory = sqlite3.Row
    return con


def text_hash(text: str) -> str:
    """Observer's submission text hash (agent_observer.ingest.text_hash)."""
    return hashlib.sha256(text.encode("utf-8", "replace")).hexdigest()[:16]


def session_of(con: sqlite3.Connection, thread_id: str) -> str | None:
    row = con.execute(
        "SELECT s.session_key FROM t3_threads t JOIN sessions s ON s.native_id = t.native_session"
        " WHERE t.thread_id=? ORDER BY s.updated_at DESC LIMIT 1", (thread_id,)).fetchone()
    return row["session_key"] if row else None


def project_of(con: sqlite3.Connection, session_key: str) -> str | None:
    row = con.execute("SELECT project_dir FROM sessions WHERE session_key=?",
                      (session_key,)).fetchone()
    return row["project_dir"] if row else None


def _epoch(iso: str) -> float | None:
    from datetime import datetime
    try:
        return datetime.fromisoformat(iso.replace("Z", "+00:00")).timestamp()
    except (ValueError, AttributeError):
        return None


def anchor(con: sqlite3.Connection, session_key: str, *, native_id: str | None,
           kind: str, text: str | None, ts: str) -> dict | None:
    """The Observer record a transcript line's moment points at.

    Exact when Observer holds the same native item (tool call, assistant
    block) or the same submitted text; otherwise the first Observer event of
    the session at or after the line's time (``next``), else the last one
    before it (``prev``). None only when the session has no events.
    """
    if native_id:
        prefix = native_id + ":"
        row = con.execute(
            "SELECT id FROM events WHERE session_key=? AND (native_id=? OR substr(native_id, 1, ?)=?)"
            " ORDER BY id LIMIT 1", (session_key, native_id, len(prefix), prefix)).fetchone()
        if row:
            return {"session_key": session_key, "ref": "event", "id": row["id"], "anchor": "exact"}
    if kind in ("user", "brief") and text:
        row = con.execute(
            "SELECT native_id FROM submissions WHERE session_key=? AND text_hash=? LIMIT 1",
            (session_key, text_hash(text))).fetchone()
        if row:
            return {"session_key": session_key, "ref": "submission", "id": row["native_id"],
                    "anchor": "exact"}
    t = _epoch(ts)
    if t is not None:
        row = con.execute("SELECT id, ts FROM events WHERE session_key=? AND ts>=? ORDER BY ts, id LIMIT 1",
                          (session_key, t)).fetchone()
        if row:
            return {"session_key": session_key, "ref": "event", "id": row["id"], "anchor": "next",
                    "distance_s": round(row["ts"] - t, 1)}
        row = con.execute("SELECT id, ts FROM events WHERE session_key=? AND ts<? ORDER BY ts DESC, id DESC LIMIT 1",
                          (session_key, t)).fetchone()
        if row:
            return {"session_key": session_key, "ref": "event", "id": row["id"], "anchor": "prev",
                    "distance_s": round(t - row["ts"], 1)}
    return None


def exists(con: sqlite3.Connection, ref: dict) -> bool:
    if ref.get("ref") == "event":
        q = "SELECT 1 FROM events WHERE id=? AND session_key=?"
    else:
        q = "SELECT 1 FROM submissions WHERE native_id=? AND session_key=?"
    return con.execute(q, (ref["id"], ref["session_key"])).fetchone() is not None


def job_usage(con: sqlite3.Connection, child_thread_id: str) -> dict | None:
    """Models that actually answered and tokens used in a delegated job's session."""
    session = session_of(con, child_thread_id)
    if not session:
        return None
    rows = con.execute(
        "SELECT model, coalesce(input_tokens,0) i, coalesce(cached_input_tokens,0) c,"
        " coalesce(output_tokens,0) o, coalesce(reasoning_output_tokens,0) r,"
        " coalesce(total_tokens,0) t FROM responses WHERE session_key=? AND is_overlap=0",
        (session,)).fetchall()
    models = sorted({r["model"] for r in rows if r["model"]})
    return {"session_key": session, "models": models, "responses": len(rows),
            "input_tokens": sum(r["i"] for r in rows),
            "cached_input_tokens": sum(r["c"] for r in rows),
            "output_tokens": sum(r["o"] for r in rows),
            "reasoning_tokens": sum(r["r"] for r in rows)}
