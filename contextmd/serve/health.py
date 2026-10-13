"""The daily serving health check (#40): rate one machine's serving log and file defects at their owners.

It reads only stored values, read-only: ``moments.status`` per harness, ``candidates.outcome`` per
harness, and ``heartbeats.at``. No model, no transcript text. Cue speed, server time and tool calls
with no pre-tool row (#57) are ``measures``; their rows file through the same Issues.

- **Window:** from the end of the last successful run to now (first run: the last 24 hours). The end
  is stored only after a successful run, so a spike repaired between runs is still seen. An
  explicit ``--from``/``--to`` slice never touches the stored window.
- **Moment status:** filed when a status other than ``ok`` has at least ``MIN_EVENTS`` moments and
  more than ``MAX_RATE`` of that harness's moments, with at least ``MIN_MOMENTS`` moments. Any
  status outside ``STATUSES`` is reported as unknown and judged by the same rule.
- **Candidate outcome:** a class in ``FILED_OUTCOMES`` is filed at ``MIN_EVENTS`` rows. Every other
  outcome is reported, never filed. ``unsupported_key`` is reported: every stored row so far is a
  tool with no command or path to key on, which is normal (#40 comment).
- **No data:** an expected harness with no moments is ``no calls, coverage unknown`` with a fresh
  heartbeat and ``no calls, server stale`` with an old one. An unreadable log is filed.

Filing: one open Issue per class in the owner's repository, found by its ``ContextMD-Class:`` line
(or a seeded Issue); an open one gets a comment. Each body or comment carries a
``ContextMD-Window: <machine> <class> <start>-<end>`` line, and an observation already recorded on
the Issue is not posted again, so an identical rerun writes nothing while another machine's report
for the same window, or another class seeded to the same Issue, still posts. Bodies hold the machine label, harness, kind, counts, rates, the
UTC window, the heartbeat age and at most ``MAX_IDS`` opaque ids per body.
"""

from __future__ import annotations

import json
import os
import socket
import sqlite3
import subprocess
from collections import Counter, defaultdict
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path

STATUSES = ("ok", "unjoined", "ambiguous", "unmapped", "bad_config", "bad_event", "oversize", "unauthorized",
            "bad_request")
FILED_OUTCOMES = ("delivery_unverified", "unverified", "hidden", "no_seam", "carry_expired")
MIN_EVENTS, MIN_MOMENTS, MAX_RATE = 10, 100, 0.05
FIRST_WINDOW = 24 * 3600
STALE_HEARTBEAT = 120
MAX_IDS = 10
CONTEXTMD, CHROMERIA = "toolboxmd/contextmd", "toolboxmd/chromeria"
OWNERS = {
    **{f"moment/{s}": CONTEXTMD for s in ("bad_event", "bad_request", "oversize", "unauthorized", "bad_config")},
    **{f"moment/{s}": CHROMERIA for s in ("unjoined", "ambiguous", "unmapped")},
    **{f"candidate/{o}": CHROMERIA for o in FILED_OUTCOMES},
    "cue/timeout": CHROMERIA, "cue/lost": CHROMERIA, "cue/unavailable": CONTEXTMD, "moment/unlogged": CONTEXTMD,
}
CONFIG = "~/.config/contextmd/health.json"
STATE = "~/.local/state/contextmd/health.json"


def owner(cls: str) -> str:
    """Unknown statuses and an unreadable log belong to contextmd."""
    return OWNERS.get(cls, CONTEXTMD)


@dataclass
class Finding:
    cls: str
    rows: list[dict] = field(default_factory=list)  # per harness: count, total, rate, kinds, ids


@dataclass
class Result:
    start: float
    end: float
    heartbeat_age: float | None = None
    harnesses: dict = field(default_factory=dict)
    findings: list[Finding] = field(default_factory=list)
    unreadable: str | None = None
    measures: list[str] = field(default_factory=list)  # the Cue and lost-call lines (#57)


def utc(ts: float) -> str:
    return datetime.fromtimestamp(ts, timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC")


def measure(db_path: Path, start: float, end: float, expected: list[str], now: float) -> Result:
    result = Result(start, end)
    try:
        db = sqlite3.connect(f"{db_path.absolute().as_uri()}?mode=ro", uri=True)
        try:
            beat = db.execute("SELECT max(at) FROM heartbeats").fetchone()[0]
            moments = db.execute("SELECT id, harness, kind, status FROM moments WHERE received_at >= ? AND"
                                 " received_at < ?", (start, end)).fetchall()
            outcomes = db.execute("""SELECT c.id, m.harness, c.outcome FROM candidates c JOIN moments m
                                     ON m.id = c.moment_id WHERE m.received_at >= ? AND m.received_at < ?""",
                                  (start, end)).fetchall()
        finally:
            db.close()
    except sqlite3.Error as exc:
        result.unreadable = str(exc)
        result.findings.append(Finding("database/unreadable", [{"error": "the serving log did not open"}]))
        return result
    result.heartbeat_age = None if beat is None else now - beat
    by_harness: dict[str, list] = defaultdict(list)
    for m in moments:
        by_harness[m[1] or "unknown"].append(m)
    out_by_harness: dict[str, list] = defaultdict(list)
    for c in outcomes:
        out_by_harness[c[1] or "unknown"].append(c)
    filed: dict[str, Finding] = {}
    for harness in sorted(set(by_harness) | set(out_by_harness) | set(expected)):
        rows = by_harness.get(harness, [])
        statuses = Counter(m[3] for m in rows)
        results = Counter(c[2] for c in out_by_harness.get(harness, []))
        result.harnesses[harness] = {"moments": len(rows), "statuses": dict(statuses), "outcomes": dict(results)}
        for status, count in statuses.items():
            if status == "ok" or count < MIN_EVENTS or len(rows) < MIN_MOMENTS or count / len(rows) <= MAX_RATE:
                continue
            hit = [m for m in rows if m[3] == status]
            filed.setdefault(f"moment/{status}", Finding(f"moment/{status}")).rows.append(
                {"harness": harness, "count": count, "total": len(rows), "rate": count / len(rows),
                 "kinds": dict(Counter(m[2] for m in hit)), "ids": [m[0] for m in hit[:MAX_IDS]]})
        for outcome, count in results.items():
            if outcome not in FILED_OUTCOMES or count < MIN_EVENTS:
                continue
            hit = [c for c in out_by_harness[harness] if c[2] == outcome]
            filed.setdefault(f"candidate/{outcome}", Finding(f"candidate/{outcome}")).rows.append(
                {"harness": harness, "count": count, "total": len(rows), "ids": [c[0] for c in hit[:MAX_IDS]]})
    result.findings = [filed[k] for k in sorted(filed)]
    return result


def report(result: Result, expected: list[str], machine: str) -> str:
    lines = [f"Serving health on {machine}: {utc(result.start)} to {utc(result.end)}"]
    if result.unreadable:
        return "\n".join(lines + [f"The serving log is unreadable: {result.unreadable}"])
    age = result.heartbeat_age
    lines.append("Heartbeat: none recorded" if age is None else f"Heartbeat: {int(age)} s old")
    stale = age is None or age > STALE_HEARTBEAT
    for harness, h in result.harnesses.items():
        if not h["moments"]:
            lines.append(f"{harness}: no calls, {'server stale' if stale else 'coverage unknown'}")
            continue
        bad = {s: n for s, n in h["statuses"].items() if s != "ok"}
        parts = [f"{harness}: {h['moments']} moments"]
        if bad:
            parts.append("; ".join(f"{'unknown status ' if s not in STATUSES else ''}{s} {n}"
                                   f" ({n / h['moments']:.1%})" for s, n in sorted(bad.items())))
        if h["outcomes"]:
            parts.append("outcomes " + ", ".join(f"{o} {n}" for o, n in sorted(h["outcomes"].items())))
        lines.append(". ".join(parts))
    lines += result.measures
    if not result.findings:
        lines.append("Nothing to file.")
    return "\n".join(lines)


def section(finding: Finding, result: Result, machine: str) -> str:
    """The window's numbers for one class: counts and opaque ids only."""
    lines = [f"## {utc(result.start)} to {utc(result.end)}, machine {machine}", ""]
    if finding.cls == "database/unreadable":
        lines.append("The serving log did not open read-only.")
    else:
        lines += ["| Harness | Count | Rate | Kinds |", "| --- | --- | --- | --- |"]
        for r in finding.rows:
            rate = f"{r['rate']:.1%}" if "rate" in r else "n/a"
            kinds = ", ".join(f"{k} {n}" for k, n in sorted((r.get("kinds") or {}).items())) or "n/a"
            if "previous" in r:
                kinds = f"previous window {r['previous']:.1%}"
            count = (f"{r['count']} of {r['total']} {r['unit']}" if "unit" in r
                     else f"{r['count']} of {r['total']} moments" if finding.cls.startswith("moment/")
                     else f"{r['count']} candidate rows, of {r['total']} moments")
            lines.append(f"| {r['harness']} | {count} | {rate} | {kinds} |")
        ids = [i for r in finding.rows for i in r["ids"]][:MAX_IDS]
        lines += ["", "Example ids: " + ", ".join(map(str, ids))]
    age = result.heartbeat_age
    lines += ["", f"Heartbeat age at the run: {'none' if age is None else f'{int(age)} s'}",
              window_mark(finding, result, machine)]
    return "\n".join(lines)


def window_mark(finding: Finding, result: Result, machine: str) -> str:
    """One observation: this machine's numbers for this class over this window. The Issue is shared
    per class (and a seeded Issue may hold several classes), so all three identify it."""
    return f"ContextMD-Window: {machine} {finding.cls} {result.start:.0f}-{result.end:.0f}"


def body(finding: Finding, result: Result, machine: str) -> str:
    return "\n".join([
        f"**What:** The serving health check found `{finding.cls}` on {machine} over its threshold.",
        "**Why:** The daily check (toolboxmd/contextmd#40, #57) rates the serving log's stored statuses, outcomes"
        " and Cue results, and tool calls Agent Observer saw; this class is a defect this repository owns.",
        "**So what:** An agent finds the cause and fixes it here; the check comments with each later window"
        " over the threshold.",
        "",
        f"ContextMD-Class: {finding.cls}",
        "",
        section(finding, result, machine),
        "",
        "## Elon record",
        "- **Requirements and who asked:** catch serving incidents within a day (user, 2026-10-13, #40).",
        "- **Deleted:** transcript text, reasons, keys and paths from this body.",
        "- **Bottleneck:** finding the cause of this class.",
        "- **Checked myself:** the serving log's stored counts for the window above, read-only.",
    ])


class GitHub:
    def __init__(self, command: str, dry_run: bool):
        self.command, self.dry_run = command, dry_run

    def run(self, *args: str, stdin: str | None = None) -> str:
        out = subprocess.run([self.command, *args], input=stdin, capture_output=True, text=True, timeout=120)
        if out.returncode != 0:
            raise RuntimeError(f"gh {' '.join(args[:2])} failed: {(out.stderr or out.stdout).strip()[:300]}")
        return out.stdout

    def target(self, cls: str, seed: dict) -> tuple[str, int | None]:
        """The repository and open Issue number for a class, or None for a new Issue."""
        if cls in seed:
            repo, _, number = seed[cls].partition("#")
            if json.loads(self.run("issue", "view", number, "-R", repo, "--json", "state,body,comments"))["state"] == "OPEN":
                return repo, int(number)
        repo, mark = owner(cls), f"ContextMD-Class: {cls}"
        found = json.loads(self.run("issue", "list", "-R", repo, "--state", "open", "--search", f'"{mark}" in:body',
                                    "--json", "number,body", "--limit", "20"))
        match = [i["number"] for i in found if mark in i["body"].splitlines()]
        return repo, (min(match) if match else None)

    def posted(self, repo: str, number: int, window: str) -> bool:
        issue = json.loads(self.run("issue", "view", str(number), "-R", repo, "--json", "state,body,comments"))
        return any(window in text.splitlines() for text in [issue["body"], *(c["body"] for c in issue["comments"])])


def file(result: Result, gh: GitHub, machine: str, seed: dict) -> list[str]:
    actions = []
    for finding in result.findings:
        repo, number = gh.target(finding.cls, seed)
        if number is not None and gh.posted(repo, number, window_mark(finding, result, machine)):
            actions.append(f"already posted {repo}#{number} {finding.cls}")
        elif gh.dry_run:
            actions.append(f"would {'comment on ' + repo + '#' + str(number) if number else 'file ' + repo}"
                           f" {finding.cls}")
        elif number is not None:
            gh.run("issue", "comment", str(number), "-R", repo, "--body-file", "-",
                   stdin=section(finding, result, machine))
            actions.append(f"commented {repo}#{number} {finding.cls}")
        else:
            url = gh.run("issue", "create", "-R", repo, "--title", f"Serving health: {finding.cls} on {machine}",
                         "--body-file", "-", stdin=body(finding, result, machine)).strip()
            actions.append(f"filed {url} {finding.cls}")
    return actions


def load_config(path: Path) -> dict:
    raw = json.loads(path.read_text()) if path.exists() else {}
    unknown = set(raw) - {"machine", "expected_harnesses", "seed"}
    if unknown:
        raise ValueError(f"{path}: unknown keys {sorted(unknown)}")
    return {"machine": raw.get("machine") or socket.gethostname().split(".")[0].lower(),
            "expected_harnesses": list(raw.get("expected_harnesses", [])), "seed": dict(raw.get("seed", {}))}


def window(state_path: Path, db_path: Path, now: float) -> float:
    state = json.loads(state_path.read_text()) if state_path.exists() else {}
    return float(state.get(str(db_path), {}).get("window_end", now - FIRST_WINDOW))


def save_window(state_path: Path, db_path: Path, end: float) -> None:
    state = json.loads(state_path.read_text()) if state_path.exists() else {}
    state[str(db_path)] = {"window_end": end}
    state_path.parent.mkdir(parents=True, exist_ok=True)
    tmp = state_path.with_suffix(".tmp")
    tmp.write_text(json.dumps(state, indent=2))
    os.replace(tmp, state_path)
