"""capture apply: write one shaped entry into an exact-key store, add-only.

One kept inbox item becomes one entry file and one commit in the store's
repository, with ``Capture-Key`` and ``Capture-Pass`` trailers as the receipt.
The key in the log is the only progress record: a re-run whose key is already
committed adds nothing, and pushes that commit if an earlier run died before
its push. The item moves from ``inbox/`` to ``applied/`` only after the push.

Before writing, the store and then the store with the new entry must pass the
real ``contextmd-serve check``, and every command key must be a word agents
actually typed: in the source session's tool input, or in at least two
sessions on this machine in the last 14 days (Agent Observer, read-only).
"""

from __future__ import annotations

import json
import os
import re
import subprocess
import tempfile
import time
from datetime import datetime, timezone
from pathlib import Path

from . import observer
from .passes import CaptureError
from .store import Store, read_json, write_json

MAX_BODY_BYTES = 1800
MAX_ID_CHARS = 120
RECENT_DAYS = 14
RECENT_SESSIONS = 2
# Command words exactly as the serving matcher cuts them (contextmd/serve/keys.py).
WORD_BREAK = re.compile(r"[\s;&|()<>`$\"'\\{}]+")


class Refused(CaptureError):
    """The entry or the store is not fit to write; nothing was changed."""


def apply(cfg: dict, name: str, entry_file: Path, store_dir: Path, now: float | None = None) -> dict:
    store = Store(cfg["state_dir"])
    with store.locked():
        return _apply(cfg, store, name, Path(entry_file), Path(store_dir).expanduser(), now or time.time())


def _apply(cfg, store: Store, name: str, entry_file: Path, store_dir: Path, now: float) -> dict:
    applied_dir = store.root / "applied"
    inbox_path = store.inbox / f"{name}.json"
    item = read_json(inbox_path)
    if item is None:
        done = read_json(applied_dir / f"{name}.json")
        if done is None:
            raise CaptureError(f"no inbox item {name}")
        return {"state": "already_applied", "sha": done["applied"]["sha"], "entry": done["applied"]["entry"]}
    if Store.key_name(item["key"]) != name:
        raise CaptureError(f"inbox item {name} holds key {Store.key_name(item['key'])}")
    _git(store_dir, "rev-parse", "--show-toplevel")  # a git checkout, or CaptureError
    repo = store_dir  # git runs here; pathspecs are entry file names

    sha = _committed(repo, name)
    if sha:
        state = "already_applied"
        if not _pushed(repo, sha):
            _require_unpushed(repo, [sha])
            _push(repo, sha)
            state = "pushed"
        entry = store_dir / _git(repo, "show", "--name-only", "--format=", sha).split("/")[-1]
        _finish(store, applied_dir, inbox_path, item, sha, entry)
        return {"state": state, "sha": sha, "entry": str(entry)}

    if item.get("grade") != "keep":
        raise Refused(f"item {name} is graded {item.get('grade')!r}; only keep items are applied")
    raw = entry_file.read_text(encoding="utf-8")
    fields, body = _parse(raw)
    entry_id = fields["id"]
    _check_entry(fields, body)

    before = _serve_check(cfg, store_dir)
    if before["errors"]:
        raise Refused(f"store {store_dir} fails contextmd-serve check: {before['errors']}")
    target = store_dir / f"{entry_id}.md"
    if target.exists() or any(e["id"] == entry_id for e in before["entries"]):
        raise Refused(f"id {entry_id!r} already used in {store_dir}")
    _check_keys(cfg, item, fields, now)
    _require_unpushed(repo, [])
    _require_clean(repo)

    rel = target.name
    message = (f"capture: add entry {entry_id}\n\n"
               f"Capture-Key: {name}\nCapture-Pass: {item['pass']['pass_id']}\n")
    target.write_text(raw, encoding="utf-8")
    try:
        after = _serve_check(cfg, store_dir)
        if after["errors"]:
            raise Refused(f"entry fails contextmd-serve check: {after['errors']}")
        _git(repo, "add", "--", rel)
        _git(repo, "commit", "-q", "-m", message, "--", rel)
    except BaseException:
        # Nothing is committed yet: remove only the file this run wrote, so a retry starts clean.
        subprocess.run(["git", "-C", str(repo), "reset", "-q", "--", rel], capture_output=True)
        target.unlink(missing_ok=True)
        raise
    sha = _git(repo, "rev-parse", "HEAD")
    _push(repo, sha)
    _finish(store, applied_dir, inbox_path, item, sha, target)
    return {"state": "applied", "sha": sha, "entry": str(target)}


def _parse(raw: str) -> tuple[dict, str]:
    lines = raw.split("\n")
    if not lines or lines[0].strip() != "---" or "---" not in lines[1:]:
        raise Refused("entry has no front matter")
    end = lines.index("---", 1)
    fields: dict = {}
    for line in lines[1:end]:
        name, sep, value = line.partition(":")
        if sep:
            fields.setdefault(name.strip(), []).append(value.strip())
    if not fields.get("id"):
        raise Refused("entry has no id")
    fields["id"] = fields["id"][-1]
    return fields, "\n".join(lines[end + 1:]).strip()


def _check_entry(fields: dict, body: str) -> None:
    if len(fields["id"]) > MAX_ID_CHARS or not re.fullmatch(r"[a-z0-9][a-z0-9-]*", fields["id"]):
        raise Refused(f"id {fields['id']!r} is not a slug of at most {MAX_ID_CHARS} characters")
    size = len(body.encode("utf-8"))
    if size > MAX_BODY_BYTES:
        raise Refused(f"body is {size} UTF-8 bytes, over 1,800")
    for target in fields.get("target", []):
        if target.startswith("/"):
            raise Refused(f"absolute target {target!r}: use ~/... or a path relative to the entry")
    if fields.get("path") or fields.get("error"):
        raise Refused("only command keys can be checked against tool input; path and error keys are not applied yet")
    if not fields.get("command"):
        raise Refused("entry has no command key")


def _words(command: str) -> list[str]:
    return [w for w in WORD_BREAK.split(command) if w]


def _has_command(words: list[str], key: list[str]) -> bool:
    """The serving matcher's rule: consecutive words; the first may be a path's last part."""
    n = len(key)
    return any((w == key[0] or os.path.basename(w) == key[0]) and words[i + 1:i + n] == key[1:]
               for i, w in enumerate(words))


def _check_keys(cfg: dict, item: dict, fields: dict, now: float) -> None:
    source = item["moment"]["observer"]["session_key"]
    con = observer.connect(cfg["observer_db"])
    try:
        for command in fields["command"]:
            key = command.split()
            like = f"%{key[0]}%"
            rows = con.execute(
                "SELECT session_key, target FROM events WHERE family='tool_call' AND target LIKE ?"
                " AND (session_key=? OR ts>=?)", (like, source, now - RECENT_DAYS * 86400)).fetchall()
            hits = {r["session_key"] for r in rows if r["target"] and _has_command(_words(r["target"]), key)}
            if source not in hits and len(hits) < RECENT_SESSIONS:
                raise Refused(
                    f"command: {command} is not in the source session's tool input and is in"
                    f" {len(hits)} session(s) of the last {RECENT_DAYS} days, under {RECENT_SESSIONS}")
    finally:
        con.close()


def _serve_check(cfg: dict, store_dir: Path) -> dict:
    """``contextmd-serve check`` on this one store directory: its ``exact_keys`` report."""
    serve_bin = os.path.expanduser(cfg.get("serve_bin") or "")
    if not serve_bin or not Path(serve_bin).exists():
        raise CaptureError(f"contextmd-serve not found at {serve_bin!r}; set serve_bin in the capture config")
    with tempfile.TemporaryDirectory() as tmp:
        conf = Path(tmp) / "serving.json"
        conf.write_text(json.dumps({"exact_keys": [str(store_dir)]}))
        proc = subprocess.run([serve_bin, "check"], capture_output=True, text=True, timeout=120,
                              env={**os.environ, "CONTEXTMD_SERVING_CONFIG": str(conf)})
    try:
        out = json.loads(proc.stdout)
    except ValueError:
        raise CaptureError(f"contextmd-serve check exited {proc.returncode} without a report: "
                           f"{proc.stderr.strip()[-500:]}") from None
    report = out.get("exact_keys") if isinstance(out, dict) else None
    if not (isinstance(report, dict) and isinstance(report.get("entries"), list)
            and isinstance(report.get("errors"), list)):
        raise CaptureError("contextmd-serve check printed no exact_keys report with entries and errors;"
                           " use contextmd 5221283 or later")
    if proc.returncode != 0 and not report["errors"]:
        report["errors"] = [f"exit {proc.returncode}"]
    return report


def _git(cwd: Path, *args: str) -> str:
    proc = subprocess.run(["git", "-C", str(cwd), *args], capture_output=True, text=True)
    if proc.returncode != 0:
        raise CaptureError(f"git {' '.join(args[:2])} failed: {proc.stderr.strip()[-500:]}")
    return proc.stdout.strip()


def _committed(repo: Path, name: str) -> str | None:
    out = _git(repo, "log", "--format=%H", "--fixed-strings", f"--grep=Capture-Key: {name}")
    return out.splitlines()[0] if out else None


def _pushed(repo: Path, sha: str) -> bool:
    upstream = _git(repo, "rev-parse", "--verify", "@{upstream}")
    proc = subprocess.run(["git", "-C", str(repo), "merge-base", "--is-ancestor", sha, upstream])
    return proc.returncode == 0


def _require_clean(repo: Path) -> None:
    """Refuse a store with staged or unstaged changes: they are the user's, and apply never touches them."""
    dirty = _git(repo, "status", "--porcelain", "--untracked-files=no", "--", ".")
    if dirty:
        raise Refused(f"the store has uncommitted changes apply did not make: {dirty.splitlines()[:5]}")


def _require_unpushed(repo: Path, allowed: list[str]) -> None:
    """Refuse unless the commits not on the upstream are exactly ``allowed``: apply publishes nothing else."""
    extra = _git(repo, "rev-list", "@{upstream}..HEAD").split()
    if extra != allowed:
        raise Refused(f"the store's branch has {len(extra)} commit(s) not on its upstream"
                      f"{' besides the receipt' if allowed else ''}; push or remove them first")


def _push(repo: Path, sha: str) -> None:
    """Push exactly ``sha`` to the branch's upstream branch."""
    branch = _git(repo, "symbolic-ref", "--short", "HEAD")
    remote = _git(repo, "config", f"branch.{branch}.remote")
    merge = _git(repo, "config", f"branch.{branch}.merge")
    proc = subprocess.run(["git", "-C", str(repo), "push", "-q", remote, f"{sha}:{merge}"],
                          capture_output=True, text=True)
    if proc.returncode != 0:
        raise CaptureError(f"push failed; the commit is kept and a re-run pushes it: {proc.stderr.strip()[-500:]}")


def _finish(store: Store, applied_dir: Path, inbox_path: Path, item: dict, sha: str, entry: Path) -> None:
    at = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    write_json(applied_dir / inbox_path.name, {**item, "applied": {"sha": sha, "entry": str(entry), "at": at}})
    inbox_path.unlink()
