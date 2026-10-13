"""The serve card: one served candidate as the outcome judge (and the user) reads it, built by code.

A card holds the rule as it was served, the served call, and what happened after it, up to the next
message a person sent into the session (a prompt or an interrupt), the end of the imported record,
or ``WINDOW_CAP`` events. Observer's ledger is read read-only (see ``report.Observer``); tool
content comes from Observer's read-only ``event show``. The judge labels it with ``prompts/serve/label.md``.
Its first lines read in under 30 seconds: what happened, why it matters, then the facts.

A card the judge must not label carries ``no_evidence``: the report's evidence gap (``not_imported``,
``no_native_record``, ``observer_failed``), ``window_cap`` when the cap cut the window,
``window_open`` when the session went on past what Observer has imported, or ``rule_text`` when
the served text cannot be recovered. Cards print transcript text, so they stay
on the machine (or in the private bench); they are never committed or posted.
"""

from __future__ import annotations

import hashlib
import json
import subprocess
from pathlib import Path

from . import report, store

LABELS = ("irrelevant", "wrong", "followed", "ignored", "no_governed_action")
WINDOW_CAP = 60
SHOWN_EVENTS = ("tool_call", "assistant_message", "permission", "compaction")
TEXT_CHARS = 300


class CardError(Exception):
    """The candidate cannot be carded."""


def _event_details(command: str, ledger: Path, ids: list[int]) -> dict:
    """Tool input and result for these events, through ``agent-observer event show``.

    ``event`` is one of Observer's read-only commands: it opens the ledger read-only and reads
    the content from the harness's own log.
    """
    if not ids:
        return {}
    out = subprocess.run([command, "--db", str(ledger), "event", "show", "--json", "--ids-from", "-"],
                         input="\n".join(map(str, ids)), capture_output=True, text=True, timeout=60)
    if out.returncode != 0:
        raise CardError(f"agent-observer event show failed: {(out.stderr or out.stdout).strip()[:200]}")
    return {d["id"]: d for d in json.loads(out.stdout)}


def _short(value, chars: int = TEXT_CHARS) -> str:
    text = value if isinstance(value, str) else json.dumps(value, ensure_ascii=False)
    text = " ".join(text.split())
    return text if len(text) <= chars else text[:chars] + f"... [{len(text) - chars} chars cut]"


def rule_text(row: dict, db_path: Path) -> dict:
    """The served entry's text at the served version: from its store's Git history, else the deny text."""
    for directory in row["owners"]:
        root = Path(directory)
        if not root.is_dir():
            continue
        for path in sorted(root.rglob("*.md")):
            try:
                entry = store.parse(path)
            except (ValueError, UnicodeDecodeError):
                continue
            if entry.id != row["entry"]:
                continue
            if entry.version == row["version"]:
                return {"source": "store", "text": entry.text, "target": entry.target}
            found = _from_history(path, row["version"])
            if found:
                return found
    import sqlite3
    db = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
    try:
        probe = db.execute("SELECT probe FROM claims WHERE candidate_id = ? AND probe IS NOT NULL LIMIT 1",
                           (row["candidate_id"],)).fetchone()
    finally:
        db.close()
    if probe and probe[0]:
        return {"source": "deny_text", "text": probe[0], "target": None}
    reply = _reply_text(db_path, row["candidate_id"])
    if reply:
        return {"source": "reply", "target": None, **reply}
    return {"source": "unavailable", "text": None, "target": None}


HEADS = ("ContextMD, the user's own rule store, applies this rule from the user here:",
         "ContextMD, the user's own rule store, stopped this call: follow this rule from the user, "
         "then run the same call again.")


def _reply_text(db_path: Path, candidate: int) -> dict | None:
    """The served text of the hook reply that carried this candidate, and how many rules it held."""
    import sqlite3
    db = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
    try:
        found = db.execute("""SELECT d.body, (SELECT count(*) FROM claims o WHERE o.delivery = d.delivery)
                              FROM claims cl JOIN deliveries d ON d.delivery = cl.delivery
                              WHERE cl.candidate_id = ? AND d.body IS NOT NULL LIMIT 1""", (candidate,)).fetchone()
    except sqlite3.Error:
        return None
    finally:
        db.close()
    if not found:
        return None
    body, count = found
    try:
        texts = list(_strings(json.loads(body)))
    except ValueError:
        texts = [body]
    text = "\n\n".join(t for t in texts if len(t) > 20).strip()
    for head in HEADS:
        text = text.replace(head, "").strip()
    return {"text": text, "rules_in_reply": count} if text else None


def _strings(value):
    if isinstance(value, str):
        yield value
    elif isinstance(value, dict):
        for v in value.values():
            yield from _strings(v)
    elif isinstance(value, list):
        for v in value:
            yield from _strings(v)


def _from_history(path: Path, version: str) -> dict | None:
    """The entry at ``version`` from the commits that touched its file."""
    def git(*args):
        return subprocess.run(["git", "-C", str(path.parent), *args], capture_output=True)
    revs = git("log", "--format=%H", "--", path.name)
    if revs.returncode != 0:
        return None
    rel = git("ls-files", "--full-name", path.name).stdout.decode().strip()
    for rev in revs.stdout.decode().split():
        raw = git("show", f"{rev}:{rel}").stdout
        if raw and hashlib.sha256(raw).hexdigest()[:12] == version:
            try:
                entry = store.parse(path, raw)
            except (ValueError, UnicodeDecodeError):
                return None
            return {"source": "store_history", "text": entry.text, "target": entry.target, "commit": rev}
    return None


def window(observer: report.Observer, command: str, row: dict) -> tuple[list[dict], str]:
    """The served call and the events after it, and why the window ends."""
    session_key = f"{row['harness']}:{row['session']}"
    events = observer.events(session_key) or []
    trigger = next(e for e in events if e["id"] == row["event_id"])
    message = observer.next_message(session_key, trigger["ts"])
    seen, after = {trigger.get("native_id")}, []
    for e in events:
        if (e.get("ts") or 0) <= trigger["ts"] or (message and e["ts"] >= message["ts"]):
            continue
        if e["family"] not in SHOWN_EVENTS or (e["family"] == "tool_call" and e.get("native_id") in seen):
            continue
        seen.add(e.get("native_id"))
        after.append(e)
    end = "next_message" if message else "end_of_record"
    if 1 + len(after) > WINDOW_CAP:  # the trigger counts toward the cap
        after, end, message = after[:WINDOW_CAP - 1], "cap", None
    details = _event_details(command, observer.ledger,
                             [e["id"] for e in [trigger, *after] if e["family"] == "tool_call"])
    out = [_event(trigger, details, "trigger")] + [_event(e, details, "after") for e in after]
    if message:
        out.append({"id": message["native_id"], "role": "message", "kind": message["kind"],
                    "text": _short(message["text_excerpt"] or "")})
    return out, end


def _event(e: dict, details: dict, role: str) -> dict:
    d = details.get(e["id"], {})
    if e["family"] == "tool_call":
        tool = d.get("tool") or e.get("name")
        given = d.get("input") or {}
        call = given.get("command") if isinstance(given, dict) and "command" in given else given
        return {"id": e["id"], "role": role, "kind": "tool", "tool": tool, "input": _short(call),
                "status": d.get("status"), "result": _short(d.get("result") or "")}
    if e["family"] == "assistant_message":
        return {"id": e["id"], "role": role, "kind": "says", "text": _short((e.get("detail") or {}).get("excerpt", ""))}
    return {"id": e["id"], "role": role, "kind": e["family"], "text": _short(e.get("name") or e.get("target") or "")}


def build(db_path: Path, observer: report.Observer, candidate: int, command: str = "agent-observer") -> dict:
    served, _ = report.rows(db_path, observer, candidate=candidate)
    if not served:
        raise CardError(f"candidate {candidate} is not a served candidate in {db_path}")
    row = served[0]
    rule = rule_text(row, db_path)
    no_evidence, events, end = None, [], None
    if row["evidence"] != "anchored":
        no_evidence = row["evidence"]
    else:
        events, end = window(observer, command, row)
        if end == "cap":
            no_evidence = "window_cap"
        elif end == "end_of_record" and _session_went_on(db_path, row, observer):
            no_evidence = "window_open"  # Observer has not imported the rest of the session yet
    if rule["source"] == "unavailable":
        no_evidence = no_evidence or "rule_text"
    return {"candidate": candidate, "entry": row["entry"], "version": row["version"], "key": row["key"],
            "harness": row["harness"], "seam": row["seam"], "moment": row["moment"], "shown": row["shown"],
            "lift": row["lift"], "late": row["late"], "evidence": row["evidence"], "rule": rule,
            "window": {"end": end, "events": len(events)}, "events": events, "no_evidence": no_evidence}


def _session_went_on(db_path: Path, row: dict, observer: report.Observer) -> bool:
    """Whether the serving log saw the session after Observer's last imported event."""
    import sqlite3
    events = observer.events(f"{row['harness']}:{row['session']}") or []
    last = max((e.get("ts") or 0 for e in events), default=0)
    db = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
    try:
        later = db.execute("SELECT max(received_at) FROM moments WHERE harness = ? AND session = ?",
                           (row["harness"], row["session"])).fetchone()[0]
    finally:
        db.close()
    return bool(later and later > last)


SEAMS = {"deny": "a deny before", "context": "context after", "carry_print": "a carried rule before"}
SHOWN = {"yes": "the model was shown it", "no": "the model was not shown it",
         "unproven": "nothing proves the model saw it"}
ENDS = {"next_message": "ends at the next message", "end_of_record": "ends where the record ends",
        "cap": f"cut at {WINDOW_CAP} events"}


def render(card: dict) -> str:
    trigger = next((e for e in card["events"] if e["role"] == "trigger"), None)
    call = f"a {trigger['tool']} call" if trigger else "a call"
    lines = [
        f"# Serve card {card['candidate']}",
        f"What happened: `{card['entry']}` was served as {SEAMS.get(card['seam'], card['seam'])} {call} on "
        f"{card['harness']}, and {SHOWN[card['shown']]}.",
        "Why it matters: The label says whether the agent's next actions followed this rule, ignored it, "
        "or show it was wrong or did not apply.",
        f"Facts: key `{card['key']}`; lift {card['lift'] or 'none'}; late {'yes' if card['late'] else 'no'}; "
        + (f"No evidence: {card['no_evidence']}." if card["no_evidence"]
           else f"{card['window']['events']} events, {ENDS[card['window']['end']]}."),
        "",
        f"## Rule ({card['rule']['source']}, version {card['version']})"
        + (f"; the reply carried {card['rule']['rules_in_reply']} rules, this is one of them"
           if (card["rule"].get("rules_in_reply") or 1) > 1 else ""),
        card["rule"]["text"] or "(the served text is not recoverable)",
    ]
    if card["rule"].get("target"):
        lines.append(f"Pointer: read {card['rule']['target']}")
    if card["events"]:
        lines += ["", "## Events"]
        for e in card["events"]:
            mark = "trigger " if e["role"] == "trigger" else ""
            if e["kind"] == "tool":
                lines.append(f"[{e['id']}] {mark}{e['tool']}: {e['input']} -> {e['status']}: {e['result']}")
            elif e["role"] == "message":
                lines.append(f"[{e['id']}] next message ({e['kind']}): {e['text']}")
            else:
                lines.append(f"[{e['id']}] {mark}{e['kind']}: {e['text']}")
    return "\n".join(lines)
