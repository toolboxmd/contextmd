"""The serving owner: one machine's serving state and per-moment log.

Every host moment goes through ``Serving.handle``. State lives in one SQLite
database, and each moment runs in one write transaction, so concurrent hooks
(parallel tool calls, subagents) reserve the turn budget and deny-once records
atomically.

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

# Characters one hook output may carry before the host spills or clips it.
# Claude Code: a 2 KB preview of an 11.7 KB payload (canary, #5); session-start
# context of about 8,000 characters arrives inline (AgentsMD's own hook).
HOST_LIMITS = {"claude-code": {"moment": 2000, "session_start": 9500}}

DENY_HEAD = "contextmd: read this before the call, then run the same call again."
CONTEXT_HEAD = "contextmd:"

SCHEMA = """
CREATE TABLE IF NOT EXISTS sessions (host TEXT, session TEXT, epoch INTEGER, turn INTEGER,
    PRIMARY KEY (host, session));
CREATE TABLE IF NOT EXISTS turns (host TEXT, session TEXT, turn INTEGER, used INTEGER,
    PRIMARY KEY (host, session, turn));
CREATE TABLE IF NOT EXISTS served (host TEXT, session TEXT, turn INTEGER, entry TEXT, version TEXT,
    PRIMARY KEY (host, session, turn, entry, version));
CREATE TABLE IF NOT EXISTS denied (host TEXT, session TEXT, entry TEXT, version TEXT,
    PRIMARY KEY (host, session, entry, version));
CREATE TABLE IF NOT EXISTS moments (seq INTEGER PRIMARY KEY, record TEXT);
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
        self.db = sqlite3.connect(config.state / "serving.db", timeout=5, isolation_level=None)
        self.db.executescript(SCHEMA)

    def handle(self, moment: Moment) -> Reply:
        db = self.db
        db.execute("BEGIN IMMEDIATE")
        try:
            reply, record = self._handle(moment)
            db.execute("INSERT INTO moments (record) VALUES (?)", (json.dumps(record),))
            db.execute("COMMIT")
        except BaseException:
            db.execute("ROLLBACK")
            raise
        return reply

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
        self.db.execute(
            "INSERT INTO turns VALUES (?, ?, ?, ?) ON CONFLICT DO UPDATE SET used = used + excluded.used",
            (m.host, m.session, turn, chars))

    def _served(self, m: Moment, turn: int, e) -> bool:
        return self.db.execute(
            "SELECT 1 FROM served WHERE host=? AND session=? AND turn=? AND entry=? AND version=?",
            (m.host, m.session, turn, e.id, e.version)).fetchone() is not None

    def _mark_served(self, m: Moment, turn: int, e) -> None:
        self.db.execute("INSERT OR IGNORE INTO served VALUES (?, ?, ?, ?, ?)",
                        (m.host, m.session, turn, e.id, e.version))

    def _denied(self, m: Moment, e) -> bool:
        return self.db.execute(
            "SELECT 1 FROM denied WHERE host=? AND session=? AND entry=? AND version=?",
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
            if len(text) > f.max_chars or used + len(text) > limit:
                serve["outcome"] = "cut_budget"
                continue
            serve["outcome"] = "injected"
            parts.append(text)
            used += len(text)
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
        """First fit within the turn budget and the host's per-moment limit."""
        budget = self.config.turn_budget - self._used(m, turn)
        limit = HOST_LIMITS.get(m.host, {}).get("moment", 0)
        picked: list = []

        def fits(e, record, key) -> bool:
            nonlocal budget, limit
            cost = len(e.text) + 2 + (0 if picked else len(head))
            if cost > budget or cost > limit:
                self._serve(record, e, key, "cut_budget", seam=seam,
                            cut="turn" if cost > budget else "moment")
                return False
            budget, limit = budget - cost, limit - cost
            picked.append(e)
            self._mark_served(m, turn, e)
            self._serve(record, e, key, "injected", seam=seam)
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
                self.db.execute("INSERT INTO denied VALUES (?, ?, ?, ?)",
                                (m.host, m.session, e.id, e.version))
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


def log_records(config: Config):
    db = sqlite3.connect(config.state / "serving.db")
    for (rec,) in db.execute("SELECT record FROM moments ORDER BY seq"):
        yield json.loads(rec)
