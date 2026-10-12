"""The serving owner: one machine's serving state and per-moment log.

Every host moment goes through ``Serving.handle``. State lives in one SQLite
database. A moment first reserves in one write transaction, so concurrent hooks
(parallel tool calls, subagents) take the turn budget and deny-once records
atomically; its serves are logged ``pending``. The hook then writes the reply,
and a second transaction settles the reservation: ``injected`` once the output
is written, ``delivery_failed`` with the budget, dedupe and deny-once released
when writing fails. A reservation whose process died before settling is
released as ``delivery_unverified`` by the next moment: at once when its
process no longer exists, else after ``STALE_SECONDS``. Live writers keep
their reservations, so concurrent hooks still share one atomic budget.
Dedupe and deny-once count only settled serves, so an unsettled reservation
never suppresses a later deny: concurrent calls may each deny, which fails
toward the rule being read.

Rules (Spec #3, Issue #11):

- A user message starts a new turn with a fresh budget of ``turn_budget``
  characters. Within a turn an entry id and version is served at most once;
  across turns it may be served again.
- At ``pre_tool`` a matching must-know-before entry denies the call once per
  entry version per session, with the entry as the reason; the retry passes.
  Other matches wait for the tool result, the next seam the model reads.
- At ``tool_result`` and ``tool_failure`` matches are served as context, first
  fit within the turn budget and the host's per-moment limit; the rest are cut.
- ``session_start`` serves the session-start files outside the turn budget.
  Compaction and ``/clear`` start a new context epoch, so the pack is served
  again.
- Every moment writes one log record: ids, versions, outcomes and owner
  commits, never prompt, command or result text.
"""

from __future__ import annotations

import hashlib
import json
import os
import sqlite3
import time
import uuid
from dataclasses import dataclass, field
from pathlib import Path

from . import keys as keys_mod
from . import store as store_mod
from .config import Config

KINDS = ("session_start", "user_message", "pre_tool", "tool_result", "tool_failure", "before_done")
NEW_EPOCH_SOURCES = ("compact", "clear")

# UTF-8 bytes of served text one hook output may carry before the host spills
# or clips it. Claude Code: a 2 KB preview of an 11.7 KB payload (canary, #5);
# session-start context of about 8,000 characters arrives inline (AgentsMD's
# own hook). The canary measured ASCII only, so bytes are the safe unit.
HOST_LIMITS = {"claude-code": {"moment": 2000, "session_start": 9500}}

# Host hooks time out after 5 s; a reservation older than this was never settled.
STALE_SECONDS = 10

DENY_HEAD = "contextmd: read this before the call, then run the same call again."
CONTEXT_HEAD = "contextmd:"

SCHEMA = """
CREATE TABLE IF NOT EXISTS sessions (host TEXT, session TEXT, epoch INTEGER, turn INTEGER,
    PRIMARY KEY (host, session));
CREATE TABLE IF NOT EXISTS turns (host TEXT, session TEXT, turn INTEGER, used INTEGER,
    PRIMARY KEY (host, session, turn));
CREATE TABLE IF NOT EXISTS served (host TEXT, session TEXT, turn INTEGER, entry TEXT, version TEXT,
    seq INTEGER, settled INTEGER DEFAULT 0, PRIMARY KEY (host, session, turn, entry, version, seq));
CREATE TABLE IF NOT EXISTS denied (host TEXT, session TEXT, entry TEXT, version TEXT, seq INTEGER,
    settled INTEGER DEFAULT 0, PRIMARY KEY (host, session, entry, version, seq));
CREATE TABLE IF NOT EXISTS moments (seq INTEGER PRIMARY KEY, record TEXT);
CREATE TABLE IF NOT EXISTS pending (seq INTEGER PRIMARY KEY, host TEXT, session TEXT, turn INTEGER,
    chars INTEGER, created REAL, pid INTEGER);
"""


@dataclass
class Moment:
    host: str
    session: str
    kind: str
    moment_id: str = field(default_factory=lambda: uuid.uuid4().hex)
    cwd: str | None = None
    tool: str | None = None
    tool_input: object = None
    error: str | None = None
    source: str | None = None  # session_start: startup, resume, clear or compact

    def __post_init__(self):
        if self.kind not in KINDS:
            raise ValueError(f"unknown moment kind {self.kind!r}")


@dataclass
class Nothing:
    pass


@dataclass
class Context:
    text: str


@dataclass
class Deny:
    reason: str


Reply = Nothing | Context | Deny


class Serving:
    def __init__(self, config: Config, clock=time.time):
        self.config = config
        self.clock = clock
        config.state.mkdir(parents=True, exist_ok=True)
        # Under the host's 5 s hook timeout, leaving time to write and settle.
        self.db = sqlite3.connect(config.state / "serving.db", timeout=2, isolation_level=None)
        self.db.executescript(SCHEMA)

    def handle(self, moment: Moment, deliver) -> Reply:
        """Reserve, call ``deliver(reply)`` to write it to the host, then settle."""
        reply, seq = self.reserve(moment)
        try:
            deliver(reply)
        except BaseException:
            self.settle(seq, "delivery_failed")
            raise
        self.settle(seq, "injected")
        return reply

    def reserve(self, moment: Moment) -> tuple[Reply, int]:
        """Decide the reply and reserve its state; serves are logged ``pending``."""
        with self._transaction():
            self._release_stale()
            seq = self.db.execute("INSERT INTO moments (record) VALUES ('{}')").lastrowid
            self.seq, self.chars = seq, 0
            reply, record = self._handle(moment)
            self.db.execute("UPDATE moments SET record=? WHERE seq=?", (json.dumps(record), seq))
            if any(s.get("outcome") == "pending" for s in record["serves"]):
                self.db.execute("INSERT INTO pending VALUES (?, ?, ?, ?, ?, ?, ?)",
                                (seq, moment.host, moment.session, record["turn"], self.chars, self.clock(),
                                 os.getpid()))
        return reply, seq

    def settle(self, seq: int, outcome: str) -> None:
        with self._transaction():
            self._settle(seq, outcome)

    def _transaction(self):
        db = self.db

        class Tx:
            def __enter__(self):
                db.execute("BEGIN IMMEDIATE")

            def __exit__(self, exc_type, *_):
                db.execute("ROLLBACK" if exc_type else "COMMIT")

        return Tx()

    def _settle(self, seq: int, outcome: str) -> None:
        row = self.db.execute("SELECT host, session, turn, chars FROM pending WHERE seq=?", (seq,)).fetchone()
        if row is None:
            return  # nothing pending, or already released as stale
        if outcome == "injected":
            self.db.execute("UPDATE served SET settled=1 WHERE seq=?", (seq,))
            self.db.execute("UPDATE denied SET settled=1 WHERE seq=?", (seq,))
        else:
            host, session, turn, chars = row
            self.db.execute("DELETE FROM served WHERE seq=?", (seq,))
            self.db.execute("DELETE FROM denied WHERE seq=?", (seq,))
            self.db.execute("UPDATE turns SET used = used - ? WHERE host=? AND session=? AND turn=?",
                            (chars, host, session, turn))
        self.db.execute("DELETE FROM pending WHERE seq=?", (seq,))
        (raw,) = self.db.execute("SELECT record FROM moments WHERE seq=?", (seq,)).fetchone()
        record = json.loads(raw)
        for serve in record["serves"]:
            if serve.get("outcome") == "pending":
                serve["outcome"] = outcome
        self.db.execute("UPDATE moments SET record=? WHERE seq=?", (json.dumps(record), seq))

    def _release_stale(self) -> None:
        """Release reservations whose hook is gone (killed by the host) or older than ``STALE_SECONDS``."""
        cutoff = self.clock() - STALE_SECONDS
        for seq, created, pid in self.db.execute("SELECT seq, created, pid FROM pending").fetchall():
            if created < cutoff or not _alive(pid):
                self._settle(seq, "delivery_unverified")

    # -- state ------------------------------------------------------------

    def _session(self, m: Moment) -> tuple[int, int, bool]:
        row = self.db.execute("SELECT epoch, turn FROM sessions WHERE host=? AND session=?",
                              (m.host, m.session)).fetchone()
        if row is None:
            self.db.execute("INSERT INTO sessions VALUES (?, ?, 0, 0)", (m.host, m.session))
            return 0, 0, True
        return row[0], row[1], False

    def _set_session(self, m: Moment, epoch: int, turn: int) -> None:
        self.db.execute("UPDATE sessions SET epoch=?, turn=? WHERE host=? AND session=?",
                        (epoch, turn, m.host, m.session))

    def _used(self, m: Moment, turn: int) -> int:
        row = self.db.execute("SELECT used FROM turns WHERE host=? AND session=? AND turn=?",
                              (m.host, m.session, turn)).fetchone()
        return row[0] if row else 0

    def _spend(self, m: Moment, turn: int, chars: int) -> None:
        self.chars += chars
        self.db.execute(
            "INSERT INTO turns VALUES (?, ?, ?, ?) ON CONFLICT DO UPDATE SET used = used + excluded.used",
            (m.host, m.session, turn, chars))

    def _served(self, m: Moment, turn: int, e) -> bool:
        return self.db.execute(
            "SELECT 1 FROM served WHERE host=? AND session=? AND turn=? AND entry=? AND version=? AND settled=1",
            (m.host, m.session, turn, e.id, e.version)).fetchone() is not None

    def _mark_served(self, m: Moment, turn: int, e) -> None:
        self.db.execute("INSERT INTO served (host, session, turn, entry, version, seq) VALUES (?, ?, ?, ?, ?, ?)",
                        (m.host, m.session, turn, e.id, e.version, self.seq))

    def _denied(self, m: Moment, e) -> bool:
        return self.db.execute(
            "SELECT 1 FROM denied WHERE host=? AND session=? AND entry=? AND version=? AND settled=1",
            (m.host, m.session, e.id, e.version)).fetchone() is not None

    # -- moments ----------------------------------------------------------

    def _handle(self, m: Moment) -> tuple[Reply, dict]:
        epoch, turn, new = self._session(m)
        if m.kind == "session_start" and not new and m.source in NEW_EPOCH_SOURCES:
            epoch += 1
        if m.kind == "user_message":
            turn += 1
        self._set_session(m, epoch, turn)
        record = {"moment": m.moment_id, "time": self.clock(), "host": m.host, "session": m.session,
                  "kind": m.kind, "epoch": epoch, "turn": turn, "tool": m.tool, "owners": {},
                  "serves": []}
        if m.kind == "session_start":
            record["source"] = m.source
            return self._session_start(m, record), record
        if m.kind in ("pre_tool", "tool_result", "tool_failure"):
            return self._exact_keys(m, turn, record), record
        return Nothing(), record

    def _session_start(self, m: Moment, record: dict) -> Reply:
        limit = HOST_LIMITS.get(m.host, {}).get("session_start", 0)
        parts, used = [], 0
        for f in self.config.session_start:
            p = Path(os.path.expanduser(f.path))
            serve = {"lane": "session_start", "entry": str(p), "remote": "not_called"}
            record["serves"].append(serve)
            record["owners"][str(p.parent)] = store_mod.head_commit(p.parent)
            try:
                text = p.read_text().strip()
            except OSError:
                serve.update(version=None, outcome="not_on_machine")
                continue
            serve["version"] = hashlib.sha256(text.encode()).hexdigest()[:12]
            size = len(text.encode())
            if len(text) > f.max_chars or used + size > limit:
                serve["outcome"] = "cut_budget"
                continue
            serve["outcome"] = "pending"
            parts.append(text)
            used += size + 2
        return Context("\n\n".join(parts)) if parts else Nothing()

    def _exact_keys(self, m: Moment, turn: int, record: dict) -> Reply:
        try:
            k = keys_mod.extract(m.tool or "", m.tool_input, m.cwd,
                                 m.error if m.kind == "tool_failure" else None)
        except keys_mod.Unsupported as exc:
            record["serves"].append({"lane": "exact_key", "outcome": "unsupported_key",
                                     "reason": str(exc), "remote": "not_called"})
            return Nothing()
        st = store_mod.load(self.config.exact_keys)
        record["owners"].update(st.owners)
        if st.errors:
            record["store_errors"] = len(st.errors)
        matches = [(e, key) for e in st.entries if (key := e.matches(k))]
        if m.kind == "pre_tool":
            return self._pre_tool(m, turn, matches, record)
        return self._serve_context(m, turn, matches, record)

    def _serve(self, record, e, key, outcome, **extra):
        record["serves"].append({"lane": "exact_key", "entry": e.id, "version": e.version, "key": key,
                                 "outcome": outcome, "remote": "not_called", **extra})

    def _fit(self, m, turn, head: str, seam: str):
        """First fit within the turn budget (characters) and the host's per-output limit (bytes)."""
        budget = self.config.turn_budget - self._used(m, turn)
        limit = HOST_LIMITS.get(m.host, {}).get("moment", 0)
        picked: list = []

        def fits(e, record, key) -> bool:
            nonlocal budget, limit
            chars = len(e.text) + 2 + (0 if picked else len(head))
            size = len(e.text.encode()) + 2 + (0 if picked else len(head.encode()))
            if chars > budget or size > limit:
                self._serve(record, e, key, "cut_budget", seam=seam,
                            cut="turn" if chars > budget else "moment")
                return False
            budget, limit = budget - chars, limit - size
            picked.append(e)
            self._mark_served(m, turn, e)
            self._serve(record, e, key, "pending", seam=seam)
            return True

        def text() -> str | None:
            if not picked:
                return None
            out = head + "".join("\n\n" + e.text for e in picked)
            self._spend(m, turn, len(out))
            return out

        return fits, text

    def _pre_tool(self, m, turn, matches, record) -> Reply:
        fits, text = self._fit(m, turn, DENY_HEAD, "deny")
        for e, key in matches:
            if not e.must_know_before:
                self._serve(record, e, key, "selected", seam="tool_result")
            elif self._denied(m, e) or self._served(m, turn, e):
                self._serve(record, e, key, "deduped")
            elif fits(e, record, key):
                self.db.execute("INSERT INTO denied (host, session, entry, version, seq) VALUES (?, ?, ?, ?, ?)",
                                (m.host, m.session, e.id, e.version, self.seq))
        out = text()
        return Deny(out) if out else Nothing()

    def _serve_context(self, m, turn, matches, record) -> Reply:
        fits, text = self._fit(m, turn, CONTEXT_HEAD, "context")
        for e, key in matches:
            if self._served(m, turn, e):
                self._serve(record, e, key, "deduped")
            else:
                fits(e, record, key)
        out = text()
        return Context(out) if out else Nothing()


def _alive(pid: int) -> bool:
    if pid == os.getpid():
        return True
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True


def log_records(config: Config):
    db = sqlite3.connect(config.state / "serving.db")
    for (rec,) in db.execute("SELECT record FROM moments ORDER BY seq"):
        yield json.loads(rec)
