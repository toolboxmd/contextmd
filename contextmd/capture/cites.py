"""Mechanical checks on the cites in context packets, before the user sees them.

A packet fact carries cites. Code checks the ones it can: a commit exists,
touches the cited path and is in the cited version; a file line holds the
quoted words; an Observer event or user message exists. Each cite gets a
``check`` of ``ok``, ``failed: <why>`` or ``unchecked`` (cites code cannot
check, such as URLs, stay the checker's claim).
"""

from __future__ import annotations

import sqlite3
import subprocess
from pathlib import Path


def _git(repo: str, *args: str) -> subprocess.CompletedProcess:
    return subprocess.run(["git", "-C", repo, *args], capture_output=True, text=True, timeout=60)


def _words(text: str) -> str:
    return " ".join(text.split()).casefold()


def _commit(c: dict) -> str:
    repo, sha = c["repo"], c["sha"]
    if _git(repo, "cat-file", "-e", f"{sha}^{{commit}}").returncode != 0:
        return f"failed: no commit {sha} in {repo}"
    if c.get("path"):
        changed = _git(repo, "show", "--name-only", "--format=", sha).stdout.splitlines()
        if not any(p == c["path"] or p.startswith(c["path"].rstrip("/") + "/") for p in changed):
            return f"failed: {sha} does not touch {c['path']}"
    if c.get("in") and _git(repo, "merge-base", "--is-ancestor", sha, c["in"]).returncode != 0:
        return f"failed: {sha} is not in {c['in']}"
    return "ok"


def _line(c: dict) -> str:
    n = c["line"]
    if not isinstance(n, int) or n < 1 or not isinstance(c["quote"], str):
        raise KeyError("line")
    if c.get("repo"):
        shown = _git(c["repo"], "show", f"{c['rev']}:{c['path']}")
        if shown.returncode != 0:
            return f"failed: {c['path']} not found at {c['rev']}"
        lines, where = shown.stdout.splitlines(), f"{c['path']}@{c['rev']}"
    else:
        path = Path(c["file"]).expanduser()
        if not path.is_file():
            return f"failed: {path} not found"
        lines, where = path.read_text(errors="replace").splitlines(), str(path)
    if n > len(lines) or _words(c["quote"]) not in _words(lines[n - 1]):
        return f"failed: {where} line {n} does not contain the quote"
    return "ok"


def _ledger(c: dict, con: sqlite3.Connection) -> str:
    if c["type"] == "event":
        q, args = "SELECT 1 FROM events WHERE id=?", [c["id"]]
        what = f"no event {c['id']}"
    else:
        q, args = "SELECT 1 FROM user_messages WHERE native_id=?", [c["id"]]
        what = f"no user message {c['id']}"
    if c.get("session_key"):
        q += " AND session_key=?"
        args.append(c["session_key"])
        what += f" in {c['session_key']}"
    return "ok" if con.execute(q, args).fetchone() else f"failed: {what}"


def check_cite(cite: dict, con: sqlite3.Connection) -> dict:
    """Return ``cite`` with ``status`` set; never raises on a bad cite."""
    kind = cite.get("type")
    try:
        if kind == "commit":
            status = _commit(cite)
        elif kind == "line":
            status = _line(cite)
        elif kind in ("event", "user_message"):
            status = _ledger(cite, con)
        else:
            status = "unchecked"
    except (KeyError, TypeError):
        status = f"failed: malformed {kind} cite"
    except (OSError, subprocess.TimeoutExpired, sqlite3.Error) as exc:
        status = f"failed: could not check ({exc})"
    return dict(cite, status=status)


def check_packets(packets: dict, con: sqlite3.Connection) -> dict:
    """Set ``check`` on every cite of every fact in place; return counts."""
    counts = {"ok": 0, "failed": 0, "unchecked": 0}
    for packet in packets.values():
        for fact in (packet.get("facts") or {}).values():
            for cite in fact.get("cites") or []:
                cite["check"] = check_cite(cite, con)["status"]
                counts[cite["check"].split(":")[0]] += 1
    return counts
