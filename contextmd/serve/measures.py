"""Cue speed and lost tool calls for the daily health check (#57), read-only.

From the serving log, per harness and moment kind:

- **Cue:** moments of kind ``user_message``, ``tool_result`` and ``tool_failure`` with a ``cue_status``
  other than ``started`` (a pre-tool moment only starts a request). The buckets are exclusive:
  *unavailable* (``not_called``, ``connect_failed``: the server could not reach Cue) is rated over all
  scored moments; *timeout*, *lost*, *ok* and *other* over the available ones. Round trip is the
  nearest-rank p50/p90 of ``cue_ms`` over ``ok`` rows only, so it understates the slow tail.
- **Server duration:** nearest-rank p50/p90/max of the server's own time, stored in seconds
  (``server_ms``, or ``server_seconds`` once chromeria#250 renames it).
- **Budget cuts:** ``over_budget`` moments (chromeria#251), and served (200) replies the hook never
  settled.

From Agent Observer's ledger, read ``mode=ro``, over the health window two hours back: tool calls
(Codex: completed shell execs, the ``exec-`` tool results its hook names) inside their session's
first-to-last logged moment, in sessions whose every Observer source was imported after that
session's last moment in the window with no unread tail. A call whose pre-tool row is
``over_budget`` is a server cut; a call with no pre-tool row of any status is *no pre-tool row*,
cause unknown. Everything not judged is counted and printed, never counted as lost.

A rate files only on a regression, per harness and kind, never pooled: at least ``FLOOR`` events,
over ``BAR``, and the previous window of equal length at or under ``BAR`` or ``RISE`` lower.
"""

from __future__ import annotations

import math
import sqlite3
from collections import Counter, defaultdict
from dataclasses import dataclass, field
from pathlib import Path

KINDS = {"user_message": "user message", "tool_result": "tool result", "tool_failure": "tool failure"}
SERVER_KINDS = {"pre_tool": "pre-tool", "tool_result": "tool result", "user_message": "user message"}
UNAVAILABLE = ("not_called", "connect_failed")
BAR, FLOOR, RISE = 0.02, 10, 0.05
OBSERVER_LAG = 2 * 3600  # Observer's window trails the health window, so the ledger has caught up
PRE_TOOL_LOOKBACK = 24 * 3600  # a Codex exec's result can come long after its pre-tool moment
MAX_IDS = 10


class ObserverError(Exception):
    """Agent Observer's ledger did not open read-only, or its schema is not one this reads."""


@dataclass
class Measures:
    start: float
    end: float
    has_cue: bool = False
    cue: dict = field(default_factory=dict)  # (harness, kind) -> counts, ids and round trip
    server: dict = field(default_factory=dict)  # (harness, kind) -> (p50, p90, max) ms
    over_budget: Counter = field(default_factory=Counter)
    replies: dict = field(default_factory=dict)  # harness -> (unconfirmed, served)
    observer: dict = field(default_factory=dict)  # harness -> judged, cut, gap, ids
    excluded: Counter = field(default_factory=Counter)
    idless: int = 0


def nearest_rank(values: list[float], q: float) -> float | None:
    values = sorted(values)
    return values[max(0, math.ceil(q * len(values)) - 1)] if values else None


def measure(db_path: Path, ledger: Path | None, start: float, end: float) -> Measures:
    from .report import Observer
    observer = Observer(ledger)
    con = observer.connect()
    if con is None:
        raise ObserverError(observer.error)
    db = sqlite3.connect(f"{db_path.absolute().as_uri()}?mode=ro", uri=True)
    try:
        m = Measures(start, end)
        _serving(db, m)
        _observed(db, con, m)
        return m
    finally:
        db.close()


def _columns(db: sqlite3.Connection, table: str) -> set[str]:
    return {r[1] for r in db.execute(f"PRAGMA table_info({table})")}


def _serving(db: sqlite3.Connection, m: Measures) -> None:
    cols = _columns(db, "moments")
    window = (m.start, m.end)
    if "cue_status" in cols:
        rows = db.execute(f"""SELECT id, harness, kind, cue_status, cue_ms FROM moments WHERE received_at >= ?
                              AND received_at < ? AND cue_status IS NOT NULL AND cue_status != 'started'
                              AND kind IN ({",".join("?" * len(KINDS))})""", (*window, *KINDS)).fetchall()
        m.has_cue = bool(rows)
        for mid, harness, kind, status, ms in rows:
            c = m.cue.setdefault((harness or "unknown", kind), {"scored": 0, "unavailable": 0, "timeout": 0, "lost": 0,
                                                                 "ok": 0, "other": 0, "ms": [], "ids": defaultdict(list)})
            bucket = ("unavailable" if status in UNAVAILABLE else status if status in ("timeout", "lost", "ok")
                      else "other")
            c["scored"] += 1
            c[bucket] += 1
            c["ids"][bucket].append(mid)
            if status == "ok" and ms is not None:
                c["ms"].append(ms)
    seconds = "server_seconds" if "server_seconds" in cols else "server_ms"
    durations: dict = defaultdict(list)
    for harness, kind, s in db.execute(f"""SELECT harness, kind, {seconds} FROM moments WHERE received_at >= ?
                                           AND received_at < ? AND {seconds} IS NOT NULL""", window):
        if kind in SERVER_KINDS:
            durations[(harness or "unknown", kind)].append(s * 1000)
    m.server = {k: (nearest_rank(v, 0.5), nearest_rank(v, 0.9), max(v)) for k, v in durations.items()}
    m.over_budget = Counter(h or "unknown" for (h,) in db.execute(
        "SELECT harness FROM moments WHERE status = 'over_budget' AND received_at >= ? AND received_at < ?", window))
    if "settled_at" in _columns(db, "deliveries"):
        for harness, unconfirmed, served in db.execute(
                """SELECT harness, sum(stage != 'settled'), count(*) FROM deliveries WHERE status = 200
                   AND created_at >= ? AND created_at < ? GROUP BY harness""", window):
            m.replies[harness] = (unconfirmed, served)


def _observed(db: sqlite3.Connection, ledger: sqlite3.Connection, m: Measures) -> None:
    start, end = m.start - OBSERVER_LAG, m.end - OBSERVER_LAG
    spans = {(h, s): (a, b) for h, s, a, b in db.execute(
        "SELECT harness, session, min(received_at), max(received_at) FROM moments WHERE session IS NOT NULL"
        " GROUP BY harness, session")}
    last = {(h, s): b for h, s, b in db.execute(
        """SELECT harness, session, max(received_at) FROM moments WHERE session IS NOT NULL AND received_at >= ?
           AND received_at < ? GROUP BY harness, session""", (start, end))}
    pre: dict = defaultdict(set)
    for h, s, call, status in db.execute(
            """SELECT harness, session, tool_use_id, status FROM moments WHERE kind = 'pre_tool' AND tool_use_id
               IS NOT NULL AND received_at >= ? AND received_at < ?""", (start - PRE_TOOL_LOOKBACK, end + 3600)):
        pre[(h, s, call)].add(status)
    m.idless = db.execute("""SELECT count(*) FROM moments WHERE status = 'over_budget' AND tool_use_id IS NULL
                             AND received_at >= ? AND received_at < ?""", (start, end)).fetchone()[0]
    try:
        sources = {s: (imported, tail) for s, imported, tail in ledger.execute(
            """SELECT session_id, min(imported_at), max(read_offset < size_bytes) FROM sources
               WHERE session_id IS NOT NULL GROUP BY session_id""")}
        events = ledger.execute(
            """SELECT id, session_key, family, native_id, ts FROM events WHERE ts >= ? AND ts < ?
               AND family IN ('tool_call', 'tool_result')""", (start, end)).fetchall()
    except sqlite3.Error as exc:
        raise ObserverError(str(exc)) from exc
    m.excluded["sourceless"] = sum(1 for (_, s) in last if s not in sources)
    stale = set()
    for (h, s), at in last.items():
        imported, tail = sources.get(s, (None, None))
        if imported is not None and (imported < at or tail):
            stale.add((h, s))
    for eid, key, family, call, ts in events:
        harness, _, session = key.partition(":")
        wanted = (family == "tool_result" and call.startswith("exec-")) if harness == "codex" else family == "tool_call"
        if not wanted:
            continue
        span = spans.get((harness, session))
        if span is None:
            m.excluded["no_moments"] += 1
            continue
        if not span[0] <= ts <= span[1]:
            m.excluded["outside"] += 1
            continue
        if (harness, session) in stale:
            m.excluded["not_synced"] += 1
            continue
        o = m.observer.setdefault(harness, {"judged": 0, "cut": 0, "gap": 0, "ids": []})
        o["judged"] += 1
        statuses = pre.get((harness, session, call))
        if not statuses:
            o["gap"] += 1
            o["ids"].append(eid)
        elif statuses == {"over_budget"}:
            o["cut"] += 1
    m.excluded["stale_sessions"] = len(stale)


def rates(m: Measures) -> dict:
    """(class, row label) -> (count, total, ids), for every filed rate."""
    out = {}
    for (harness, kind), c in m.cue.items():
        label, available = f"{harness} {KINDS[kind]}", c["scored"] - c["unavailable"]
        out[("cue/unavailable", label)] = (c["unavailable"], c["scored"], c["ids"]["unavailable"])
        out[("cue/timeout", label)] = (c["timeout"], available, c["ids"]["timeout"])
        out[("cue/lost", label)] = (c["lost"], available, c["ids"]["lost"])
    for harness, o in m.observer.items():
        out[("moment/unlogged", harness)] = (o["gap"], o["judged"], o["ids"])
    return out


def over_bar(count: int, total: int) -> bool:
    return total > 0 and count >= FLOOR and count / total > BAR


def regressed(now: tuple, before: tuple | None) -> bool:
    count, total, _ = now
    if not over_bar(count, total) or before is None or before[1] == 0:
        return False
    rate, previous = count / total, before[0] / before[1]
    return previous <= BAR or rate - previous >= RISE


def _comparable(previous: Measures) -> dict:
    """The previous window's rates, without Cue rates from before Cue was logged."""
    before = rates(previous)
    return before if previous.has_cue else {k: v for k, v in before.items() if not k[0].startswith("cue/")}


def findings(current: Measures, previous: Measures) -> list[tuple[str, list[dict]]]:
    """Each class with the rows that regressed, per harness and kind."""
    now, before = rates(current), _comparable(previous)
    by_class: dict = defaultdict(list)
    for (cls, label), value in sorted(now.items()):
        if regressed(value, before.get((cls, label))):
            count, total, ids = value
            prev = before[(cls, label)]
            by_class[cls].append({"harness": label, "count": count, "total": total, "rate": count / total,
                                  "unit": "tool calls" if cls == "moment/unlogged" else "Cue moments",
                                  "previous": prev[0] / prev[1], "ids": ids[:MAX_IDS]})
    return sorted(by_class.items())


def _rate(count: int, total: int) -> str:
    text = f"{count} ({count / total:.1%})" if total else f"{count}"
    return text + (" *" if over_bar(count, total) else "")


def _previous(names: list[str], label: str, before: dict) -> str:
    parts = []
    for name in names:
        prev = before.get((name, label))
        if prev is None or prev[1] == 0:
            return "previous unavailable"
        parts.append(f"{name.split('/')[1]} {prev[0] / prev[1]:.1%}")
    return "previous " + ", ".join(parts)


def render(current: Measures, previous: Measures) -> list[str]:
    before = _comparable(previous)
    lines = []
    for (harness, kind), c in sorted(current.cue.items()):
        available = c["scored"] - c["unavailable"]
        rt = (f"round trip p50/p90 {nearest_rank(c['ms'], 0.5):.0f}/{nearest_rank(c['ms'], 0.9):.0f} ms"
              if c["ms"] else "round trip -")
        other = f", other {c['other']}" if c["other"] else ""
        label = f"{harness} {KINDS[kind]}"
        lines.append(
            f"Cue {label}: scored {c['scored']}, unavailable {_rate(c['unavailable'], c['scored'])}, available"
            f" {available}, timeout {_rate(c['timeout'], available)}, lost {_rate(c['lost'], available)}, ok"
            f" {c['ok']}{other}; {rt}; {_previous(['cue/timeout', 'cue/lost'], label, before)}")
    if current.cue:
        lines.append("Round trips are over ok rows only, so they understate the slow tail.")
    for (harness, kind), (p50, p90, top) in sorted(current.server.items()):
        lines.append(f"Server {harness} {SERVER_KINDS[kind]}: p50/p90/max {p50:.0f}/{p90:.0f}/{top:.0f} ms")
    for harness, n in sorted(current.over_budget.items()):
        lines.append(f"Over budget {harness}: {n}")
    if current.replies:
        lines.append("Served replies not confirmed by the hook's settle: " + ", ".join(
            f"{h} {u} of {n}" for h, (u, n) in sorted(current.replies.items())))
    for harness, o in sorted(current.observer.items()):
        unit = "completed shell execs" if harness == "codex" else "tool calls"
        lines.append(f"Observer {harness}: judged {o['judged']} {unit}, server cut {o['cut']}, no pre-tool row"
                     f" {_rate(o['gap'], o['judged'])}; {_previous(['moment/unlogged'], harness, before)}")
    x = current.excluded
    lines.append(f"Not judged: {x['no_moments']} calls in sessions with no moments (coverage unknown),"
                 f" {x['outside']} outside their session's first-to-last moment, {x['not_synced']} in"
                 f" {x['stale_sessions']} sessions not synced, {x['sourceless']} served sessions with no Observer"
                 f" source. over_budget rows without ids: {current.idless}")
    lines.append("* at least 10 events and over 2%; filed only when it got worse than the previous window.")
    return lines
