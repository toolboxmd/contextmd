"""The serve evidence report: what each served entry did, from the serving log and Agent Observer, no model.

One row per served candidate: every outcome of a settled delivery (``SERVED``), including a deny
whose text Chromeria's check did not find in the call's recorded output (``hidden``):

- ``shown``: ``yes`` when native evidence proves the context reached the model, or Chromeria found
  the deny text in the call's recorded output; ``no`` for a recorded negative, ``unproven`` otherwise.
  Claude request records and Codex rollout input can prove context receipt; hook success cannot;
- ``lift``: for a deny, how its rule lifted (``read``, ``delivered_inline``, ``not_read_capped``),
  ``open`` when the session went on without that, or ``pending`` when nothing came after it yet;
- ``late``: a must-know-before entry served after a call its own key matched: as context on that
  call's result, or as a Grok carry selected there;
- ``evidence``: ``anchored`` to the Observer event of the served tool call, or why not: ``not_imported``,
  ``no_native_record``, ``observer_failed``.

The rows are also the evidence step of the serve-outcome judge (contextmd#15): it only labels
anchored rows, and only trusts ``ignored`` where ``shown`` is ``yes``.
"""

from __future__ import annotations

import json
import math
import os
import sqlite3
from collections import Counter
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from pathlib import Path

SERVED = ("injected", "truncated", "unverified", "awaiting_evidence", "hidden")
CANARY_PREFIX = "live-canary-"  # the per-host canary entries (#21); test serves, not use
DEFAULT_LEDGER = "~/.local/state/agent-observer/observer.db"  # Agent Observer's default
SOURCES = {"claude": "claude_request_replay", "codex": "codex_rollout"}
SHOWN_REASONS = frozenset({
    "not_checked", "no_evidence_source", "final_input_unrecorded", "absent_from_request", "no_transcript",
    "transcript_unreadable", "transcript_too_large", "unrecognized_format", "not_found", "ambiguous",
    "no_model_response", "compacted", "rolled_back", "request_unverifiable",
})


class LogError(Exception):
    """The serving log is not one this report can read."""


def _seconds(value):
    if type(value) not in (int, float):
        return None
    try:
        number = float(value)
    except OverflowError:
        return None
    return number if math.isfinite(number) else None


def _ident(value):
    return type(value) is str and 0 < len(value) <= 512


@dataclass(frozen=True)
class Consumption:
    """A native context-receipt proof, validated before it can grant a label."""
    source: str
    record_id: str
    record_at: float
    turn: str | None
    id: str
    call: str | None
    at: float
    request: str | None


def consumption(harness, fact, raw) -> Consumption | None:
    source = SOURCES.get(harness) if type(harness) is str else None
    if source is None or fact != "shown" or type(raw) is not str:
        return None
    try:
        ev = json.loads(raw)
    except (ValueError, RecursionError):
        return None
    if type(ev) is not dict or type(ev.get("v")) is not int or ev["v"] != 1 or ev.get("source") != source:
        return None
    rec, con, checked = ev.get("record"), ev.get("consumed"), _seconds(ev.get("checked_at"))
    if type(rec) is not dict or type(con) is not dict or checked is None:
        return None
    rec_at, con_at = _seconds(rec.get("at")), _seconds(con.get("at"))
    slot, turn, call, request = rec.get("slot"), rec.get("turn"), con.get("call"), con.get("request")
    if not (_ident(rec.get("id")) and type(slot) is int and slot >= 0 and rec_at is not None
            and _ident(con.get("id")) and (call is None or _ident(call)) and con_at is not None):
        return None
    if source == "codex_rollout" and not (_ident(turn) and request is None):
        return None
    if source == "claude_request_replay" and not (turn is None and _ident(request)):
        return None
    if not rec_at <= con_at <= checked:
        return None
    return Consumption(source, rec["id"], rec_at, turn, con["id"], call, con_at, request)


def _reason(raw):
    try:
        ev = json.loads(raw) if type(raw) is str else None
    except (ValueError, RecursionError):
        return "unreadable"
    value = ev.get("reason") if type(ev) is dict else None
    return value if type(value) is str and value in SHOWN_REASONS else "unreadable"


class Observer:
    """Agent Observer's ledger, opened read-only, one query per session, cached.

    The report reads the ledger itself with SQLite ``mode=ro``: it never creates, migrates or
    changes it, and it does not depend on Observer's CLI. Only the schema it was written for is
    read.
    """

    SCHEMAS = ("3",)  # Agent Observer 1.0.1

    def __init__(self, ledger: Path | None = None):
        self.ledger = Path(ledger or os.environ.get("AGENT_OBSERVER_DB") or DEFAULT_LEDGER).expanduser()
        self.cache: dict[str, list[dict] | None] = {}
        self._db: sqlite3.Connection | None = None
        self.error: str | None = None

    def connect(self) -> sqlite3.Connection | None:
        if self._db is None and self.error is None:
            try:
                if not self.ledger.is_file():
                    raise sqlite3.OperationalError(f"no Observer ledger at {self.ledger}")
                db = sqlite3.connect(f"{self.ledger.absolute().as_uri()}?mode=ro", uri=True)
                db.row_factory = sqlite3.Row
                row = db.execute("SELECT value FROM schema_meta WHERE key = 'schema_version'").fetchone()
                if row is None or row[0] not in self.SCHEMAS:
                    db.close()
                    raise sqlite3.OperationalError(f"Observer ledger schema {row and row[0]} is not one of {self.SCHEMAS}")
                self._db = db
            except sqlite3.Error as exc:
                self.error = str(exc)
        return self._db

    def events(self, session_key: str) -> list[dict] | None:
        if session_key not in self.cache:
            db = self.connect()
            try:
                self.cache[session_key] = None if db is None else [
                    {**dict(r), "detail": json.loads(r["detail_json"]) if r["detail_json"] else None}
                    for r in db.execute(
                        """SELECT id, family, native_id, ts, ordinal_num, name, target, detail_json FROM events
                           WHERE session_key = ? ORDER BY COALESCE(ts, 0), ordinal_num""", (session_key,))]
            except (sqlite3.Error, ValueError):
                self.cache[session_key] = None
        return self.cache[session_key]

    def next_message(self, session_key: str, after: float) -> dict | None:
        """The first message a person sent into the session after ``after``: a prompt or an interrupt."""
        db = self.connect()
        if db is None:
            return None
        row = db.execute(
            """SELECT native_id, ts, kind, text_excerpt FROM user_messages
               WHERE session_key = ? AND ts > ? AND kind IN ('genuine', 'interrupt')
               ORDER BY ts LIMIT 1""", (session_key, after)).fetchone()
        return dict(row) if row else None


def evidence(observer: Observer, harness: str, session: str, tool_use_id: str | None, at: float):
    """(evidence, Observer event id) for one served tool call."""
    events = observer.events(f"{harness}:{session}")
    if events is None:
        return "observer_failed", None
    calls = [e for e in events if tool_use_id and e.get("native_id") == tool_use_id]
    if calls:  # the call itself, not its result or another event of the same id
        return "anchored", min(calls, key=lambda e: (e["family"] != "tool_call", e["id"]))["id"]
    if not events or max(e.get("ts") or 0 for e in events) < at:
        return "not_imported", None
    return "no_native_record", None


def rows(db_path: Path, observer: Observer, since: float | None = None, candidate: int | None = None):
    """(rows, canary serves left out), oldest first; only ``candidate`` when given."""
    db = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
    db.row_factory = sqlite3.Row
    try:
        has_fact = {"shown", "shown_json"} <= {c[1] for c in db.execute("PRAGMA table_info(deliveries)")}
        extra = ", dl.shown AS context_shown, dl.shown_json" if has_fact else ", NULL AS context_shown, NULL AS shown_json"
        join = "LEFT JOIN deliveries dl ON dl.delivery = m.delivery" if has_fact else ""
        served = db.execute(
            f"""SELECT c.id AS candidate_id, c.entry, c.version, c.key, c.seam, c.outcome, m.owners_json,
                      m.id AS moment_id, m.received_at, m.harness, m.kind, m.session, m.epoch, m.tool_use_id,
                      (SELECT checked FROM claims cl WHERE cl.candidate_id = c.id
                        ORDER BY cl.checked IS NULL LIMIT 1) AS checked,
                      d.lifted, d.lift_outcome,
                      EXISTS (SELECT 1 FROM moments later WHERE later.harness = m.harness
                              AND later.session = m.session AND later.received_at > m.received_at) AS went_on,
                      EXISTS (SELECT 1 FROM deny_state k WHERE k.entry = c.entry AND k.version = c.version) AS must_know,
                      (SELECT om.kind FROM claims pc JOIN carries ca ON ca.delivery = pc.delivery
                         AND ca.entry = c.entry AND ca.version = c.version
                         JOIN candidates oc ON oc.id = ca.candidate_id JOIN moments om ON om.id = oc.moment_id
                       WHERE pc.candidate_id = c.id LIMIT 1) AS carried_at {extra}
               FROM candidates c JOIN moments m ON m.id = c.moment_id
               LEFT JOIN deny_state d ON c.seam = 'deny' AND d.harness = m.harness AND d.session = m.session
                    AND d.epoch = m.epoch AND d.entry = c.entry AND d.version = c.version
               {join}
               WHERE c.outcome IN ({",".join("?" * len(SERVED))}) AND m.received_at >= ?
                 AND (? IS NULL OR c.id = ?)
               ORDER BY m.received_at, c.id""", (*SERVED, since or 0, candidate, candidate)).fetchall()
    except sqlite3.Error as exc:
        raise LogError(f"cannot read the serving log {db_path}: {exc}") from exc
    finally:
        db.close()
    out, canaries = [], 0
    for r in served:
        if (r["entry"] or "").startswith(CANARY_PREFIX):
            canaries += 1
            continue
        after_call = (r["seam"] == "context" and r["kind"] in ("tool_result", "tool_failure")) or (
            r["seam"] == "carry_print" and r["carried_at"] in ("tool_result", "tool_failure"))
        ev, event_id = evidence(observer, r["harness"], r["session"], r["tool_use_id"], r["received_at"])
        consumed = None
        shown_reason = None
        if r["seam"] in ("context", "carry_print"):
            consumed = consumption(r["harness"], r["context_shown"], r["shown_json"])
            shown = "yes" if consumed else "no" if r["context_shown"] == "not_shown" else "unproven"
            shown_reason = None if consumed else _reason(r["shown_json"])
        else:
            shown = ("no" if r["checked"] == "hidden" or r["outcome"] == "hidden"
                     else "yes" if r["checked"] == "shown" else "unproven")
        out.append({
            "moment_id": r["moment_id"], "candidate_id": r["candidate_id"], "at": r["received_at"],
            "harness": r["harness"], "session": r["session"], "epoch": r["epoch"], "tool_use_id": r["tool_use_id"],
            "entry": r["entry"], "version": r["version"], "key": r["key"], "seam": r["seam"], "moment": r["kind"],
            "owners": json.loads(r["owners_json"] or "{}"),
            "outcome": r["outcome"],
            "shown": shown, "shown_source": consumed.source if consumed else None,
            "shown_request": consumed.request if consumed else None, "shown_reason": shown_reason,
            "consumed": asdict(consumed) if consumed else None,
            "lift": None if r["seam"] != "deny" else r["lift_outcome"] if r["lifted"]
                    else "open" if r["went_on"] else "pending",
            "late": bool(after_call and r["must_know"]),
            "evidence": ev, "event_id": event_id,
        })
    return out, canaries


def rule(r: dict) -> tuple:
    """One deny rule in one session and context epoch: the unit its lift belongs to."""
    return r["harness"], r["session"], r["epoch"], r["entry"], r["version"]


def _top(counter: Counter, n: int = 4) -> str:
    items = counter.most_common()
    text = ", ".join(f"{k} {v}" for k, v in items[:n])
    return text + (f", {len(items) - n} more" if len(items) > n else "")


# Each card: who it selects, what happened, why it matters, and what it counts: serves, or rules
# (one deny rule per session and epoch, however often it denied). Every sentence ends with a full stop.
CARDS = [
    ("Late", lambda r: r["late"], "serves",
     "A before-action rule was served after its own command had already run.",
     "It came too late for that action and used the turn's serve budget (toolboxmd/chromeria#231)."),
    ("Hidden", lambda r: r["seam"] == "deny" and r["shown"] == "no", "serves",
     "Chromeria's check did not find this rule's text in the output recorded for the denied call.",
     "The rule may not have reached the agent and stays armed; on Grok the check misfires (toolboxmd/chromeria#228)."),
    ("Passed without reading", lambda r: r["lift"] == "not_read_capped", "rules",
     "A rule reached its cap of three denies without a read of the pointed file, so it stopped blocking.",
     "Where a deny was proven shown, the agent skipped the rule; otherwise it may never have seen it."),
    ("Never lifted", lambda r: r["lift"] == "open", "rules",
     "The session went on after a deny without the agent reading the pointed file.",
     "The agent dropped the action or worked around it; only the transcript can tell which."),
    ("Not proven shown", lambda r: r["shown"] == "unproven", "serves",
     "The hook delivered these serves, but nothing records that the model saw them.",
     "An ignored label on them cannot be trusted, so the outcome judge will not use one."),
    ("No Observer record", lambda r: r["evidence"] != "anchored", "serves",
     "Agent Observer gave no event for these served tool calls.",
     "The outcome judge cannot label a serve without its transcript."),
]


def render(served: list[dict], canaries: int) -> str:
    entries = Counter(r["entry"] for r in served)
    head = [f"# Serve evidence: {len(served)} serve{'s' if len(served) != 1 else ''}"
            f" of {len(entries)} entr{'ies' if len(entries) != 1 else 'y'}"]
    left_out = f"{canaries} canary serve{'s' if canaries != 1 else ''} left out."
    if not served:
        head.append(left_out)
    else:
        start = datetime.fromtimestamp(served[0]["at"], timezone.utc).strftime("%Y-%m-%d %H:%M")
        end = datetime.fromtimestamp(served[-1]["at"], timezone.utc).strftime("%Y-%m-%d %H:%M")
        head.append(f"From {start} to {end} UTC; {left_out}")
        head.append(f"By seam: {_top(Counter(r['seam'] for r in served))}. "
                    f"By host: {_top(Counter(r['harness'] for r in served))}.")
    parts = ["\n".join(head)]
    for title, picks, unit, happened, why in CARDS:
        hit = [r for r in served if picks(r)]
        if unit == "rules":
            hit = list({rule(r): r for r in hit}.values())
            total = len({rule(r) for r in served if r["seam"] == "deny"})
        else:
            total = len(served)
        if not hit:
            continue
        facts = f"{len(hit)} of {total} {unit}. Entries: {_top(Counter(r['entry'] for r in hit))}."
        if title == "Passed without reading":
            proven = {rule(r) for r in served if r["shown"] == "yes"}
            facts += f" Proven shown: {sum(rule(r) in proven for r in hit)} of {len(hit)}."
        if title == "No Observer record":
            facts += f" Reasons: {_top(Counter(r['evidence'] for r in hit))}."
        elif title == "Not proven shown":
            reasons = Counter(r["shown_reason"] for r in hit if r.get("shown_reason"))
            facts += f" Hosts: {_top(Counter(r['harness'] for r in hit))}."
            if reasons:
                facts += f" Reasons: {_top(reasons)}."
        else:
            facts += f" Hosts: {_top(Counter(r['harness'] for r in hit))}."
        parts.append(f"## {title}: {len(hit)}\nWhat happened: {happened}\nWhy it matters: {why}\nFacts: {facts}")
    return "\n\n".join(parts)


def parse_since(text: str | None) -> float | None:
    """An ISO date or time, UTC unless it names a zone; ValueError otherwise."""
    if not text:
        return None
    when = datetime.fromisoformat(text)
    return (when if when.tzinfo else when.replace(tzinfo=timezone.utc)).timestamp()
