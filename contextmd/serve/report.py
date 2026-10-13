"""The serve evidence report: what each served entry did, from the serving log and Agent Observer, no model.

One row per served candidate (settled ``injected``, or a deny another hook ``hidden``):

- ``shown``: ``yes`` when Chromeria saw the deny text in what the model was shown, ``no`` when it was
  hidden, ``unproven`` otherwise (every context serve today, and denies on hosts without that check);
- ``lift``: for a deny, how it lifted (``read``, ``delivered_inline``, ``not_read_capped``) or ``open``;
- ``late``: a must-know-before entry served as context on the result of a call its own key matched;
- ``evidence``: ``anchored`` to the Observer event of the served tool call, or why not: ``not_imported``,
  ``no_native_record``, ``observer_failed``.

The rows are also the evidence step of the serve-outcome judge (contextmd#15): it only labels
anchored rows, and only trusts ``ignored`` where ``shown`` is ``yes``.
"""

from __future__ import annotations

import json
import sqlite3
import subprocess
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path

CANARY_PREFIX = "live-canary-"  # the per-host canary entries (#21); test serves, not use
OBSERVER_LIMIT = 1_000_000


class Observer:
    """Agent Observer's read-only ``trace``, one call per session, cached."""

    def __init__(self, command: str = "agent-observer"):
        self.command = command
        self.cache: dict[str, list[dict] | None] = {}

    def events(self, session_key: str) -> list[dict] | None:
        if session_key not in self.cache:
            try:
                out = subprocess.run([self.command, "trace", "--session", session_key, "--json",
                                      "--limit", str(OBSERVER_LIMIT)], capture_output=True, text=True, timeout=60)
                self.cache[session_key] = json.loads(out.stdout)["events"] if out.returncode == 0 else None
            except (OSError, subprocess.TimeoutExpired, ValueError, KeyError):
                self.cache[session_key] = None
        return self.cache[session_key]


def evidence(observer: Observer, harness: str, session: str, tool_use_id: str | None, at: float):
    """(evidence, Observer event id) for one served tool call."""
    events = observer.events(f"{harness}:{session}")
    if events is None:
        return "observer_failed", None
    for e in events:
        if tool_use_id and e.get("native_id") == tool_use_id:
            return "anchored", e["id"]
    if not events or max(e.get("ts") or 0 for e in events) < at:
        return "not_imported", None
    return "no_native_record", None


def rows(db_path: Path, observer: Observer, since: float | None = None):
    """(rows, canary serves left out), oldest first."""
    db = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
    db.row_factory = sqlite3.Row
    try:
        served = db.execute(
            """SELECT c.id AS candidate_id, c.entry, c.version, c.seam, c.outcome,
                      m.id AS moment_id, m.received_at, m.harness, m.kind, m.session, m.epoch, m.tool_use_id,
                      (SELECT checked FROM claims cl WHERE cl.candidate_id = c.id AND cl.settled = 1
                        ORDER BY cl.checked IS NULL LIMIT 1) AS checked,
                      d.lifted, d.lift_outcome,
                      EXISTS (SELECT 1 FROM deny_state k WHERE k.entry = c.entry AND k.version = c.version) AS must_know
               FROM candidates c JOIN moments m ON m.id = c.moment_id
               LEFT JOIN deny_state d ON c.seam = 'deny' AND d.harness = m.harness AND d.session = m.session
                    AND d.epoch = m.epoch AND d.entry = c.entry AND d.version = c.version
               WHERE c.outcome IN ('injected', 'hidden') AND m.received_at >= ?
               ORDER BY m.received_at, c.id""", (since or 0,)).fetchall()
    finally:
        db.close()
    out, canaries = [], 0
    for r in served:
        if (r["entry"] or "").startswith(CANARY_PREFIX):
            canaries += 1
            continue
        ev, event_id = evidence(observer, r["harness"], r["session"], r["tool_use_id"], r["received_at"])
        out.append({
            "moment_id": r["moment_id"], "candidate_id": r["candidate_id"], "at": r["received_at"],
            "harness": r["harness"], "session": r["session"], "tool_use_id": r["tool_use_id"],
            "entry": r["entry"], "version": r["version"], "seam": r["seam"], "moment": r["kind"],
            "shown": "no" if r["outcome"] == "hidden" else "yes" if r["checked"] == "shown" else "unproven",
            "lift": None if r["seam"] != "deny" else (r["lift_outcome"] if r["lifted"] else "open"),
            "late": bool(r["seam"] == "context" and r["kind"] in ("tool_result", "tool_failure") and r["must_know"]),
            "evidence": ev, "event_id": event_id,
        })
    return out, canaries


def _top(counter: Counter, n: int = 4) -> str:
    items = counter.most_common()
    text = ", ".join(f"{k} {v}" for k, v in items[:n])
    return text + (f", {len(items) - n} more" if len(items) > n else "")


# Each card: who it selects, what happened, why it matters. Every sentence ends with a full stop.
CARDS = [
    ("Late", lambda r: r["late"],
     "A before-action rule was shown after its own command had already run.",
     "It came too late for that action and used the turn's serve budget (toolboxmd/chromeria#231)."),
    ("Hidden", lambda r: r["shown"] == "no",
     "Another hook's deny replaced this rule before the model saw it.",
     "The action ran without the rule; the rule stays armed for the next call."),
    ("Passed without reading", lambda r: r["lift"] == "not_read_capped",
     "The agent was denied three times without reading the pointed file, then the call passed.",
     "This is the one kind of ignored rule the log can see without a model."),
    ("Never lifted", lambda r: r["lift"] == "open",
     "A deny was served but the agent never read the pointed file afterwards.",
     "The agent dropped the action or worked around it; only the transcript can tell which."),
    ("Not proven shown", lambda r: r["shown"] == "unproven",
     "The hook delivered these serves, but nothing records that the model saw them.",
     "An ignored label on them cannot be trusted, so the outcome judge will not use one."),
    ("No Observer record", lambda r: r["evidence"] != "anchored",
     "Agent Observer has no event for these served tool calls.",
     "The outcome judge cannot label a serve without its transcript."),
]


def render(served: list[dict], canaries: int) -> str:
    entries = Counter(r["entry"] for r in served)
    head = [f"# Serve evidence: {len(served)} serve{'s' if len(served) != 1 else ''}"
            f" of {len(entries)} entr{'ies' if len(entries) != 1 else 'y'}"]
    if served:
        start = datetime.fromtimestamp(served[0]["at"], timezone.utc).strftime("%Y-%m-%d %H:%M")
        end = datetime.fromtimestamp(served[-1]["at"], timezone.utc).strftime("%Y-%m-%d %H:%M")
        head.append(f"From {start} to {end} UTC; {canaries} canary serve{'s' if canaries != 1 else ''} left out.")
        head.append(f"By seam: {_top(Counter(r['seam'] for r in served))}. "
                    f"By host: {_top(Counter(r['harness'] for r in served))}.")
    parts = ["\n".join(head)]
    for title, picks, happened, why in CARDS:
        hit = [r for r in served if picks(r)]
        if not hit:
            continue
        facts = f"{len(hit)} of {len(served)} serves. Entries: {_top(Counter(r['entry'] for r in hit))}."
        if title == "No Observer record":
            facts += f" Reasons: {_top(Counter(r['evidence'] for r in hit))}."
        else:
            facts += f" Hosts: {_top(Counter(r['harness'] for r in hit))}."
        parts.append(f"## {title}: {len(hit)}\nWhat happened: {happened}\nWhy it matters: {why}\nFacts: {facts}")
    return "\n\n".join(parts)


def parse_since(text: str | None) -> float | None:
    if not text:
        return None
    when = datetime.fromisoformat(text)
    return (when if when.tzinfo else when.replace(tzinfo=timezone.utc)).timestamp()
