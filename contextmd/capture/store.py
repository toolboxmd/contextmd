"""Capture's local state: lock, per-thread cursors and the transit inbox.

Everything lives under the state directory on the capturing machine, outside
any repository. An inbox item is transit, not a store: later steps classify it,
and the writer deletes it once an owner commit carries its key.
"""

from __future__ import annotations

import contextlib
import fcntl
import hashlib
import json
import os
from pathlib import Path


class Busy(RuntimeError):
    pass


def write_json(path: Path, value) -> None:
    """Atomic write: a reader sees the old file or the new one, never half."""
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + ".tmp")
    with open(tmp, "w", encoding="utf-8") as fh:
        json.dump(value, fh, indent=1, ensure_ascii=False, sort_keys=True)
        fh.write("\n")
        fh.flush()
        os.fsync(fh.fileno())
    os.replace(tmp, path)


def read_json(path: Path, default=None):
    try:
        with open(path, encoding="utf-8") as fh:
            return json.load(fh)
    except FileNotFoundError:
        return default


def _renamed_pass(value):
    """An item's or cursor's pass reference written under the old names."""
    if isinstance(value, dict) and "run_id" in value:
        value = {("pass_id" if k == "run_id" else k): v for k, v in value.items()}
    return value


def migrate(root: Path) -> bool:
    """Move state written before "run" became "pass" and "queue" became "inbox".

    Idempotent: directories and files are renamed in place and JSON keys are
    rewritten; returns whether anything changed. Runs before any command reads
    state, under the command lock.
    """
    changed = False
    for old, new in (("runs", "passes"), ("queue", "inbox"), ("active-run", "active-pass")):
        if (root / old).exists() and not (root / new).exists():
            os.replace(root / old, root / new)
            changed = True
    for run_json in sorted((root / "passes").glob("*/run.json")) if (root / "passes").exists() else []:
        data = read_json(run_json)
        data["pass_id"] = data.pop("run_id", data.get("pass_id"))
        for seg in data.get("segments", []):
            if isinstance(seg.get("source"), dict) and "run" in seg["source"]:
                seg["source"]["pass"] = seg["source"].pop("run")
        write_json(run_json.with_name("pass.json"), data)
        run_json.unlink()
        changed = True
    item_dirs = [root / "inbox"] + (sorted((root / "passes").glob("*/items")) if (root / "passes").exists() else [])
    for d in item_dirs:
        for p in sorted(d.glob("*.json")) if d.exists() else []:
            item = read_json(p)
            if isinstance(item, dict) and "run" in item and "pass" not in item:
                item["pass"] = _renamed_pass(item.pop("run"))
                write_json(p, item)
                changed = True
    cur_path = root / "cursors.json"
    cur = read_json(cur_path, {})
    if any("run" in c for c in cur.values()):
        for c in cur.values():
            if "run" in c:
                c["pass"] = c.pop("run")
        write_json(cur_path, cur)
        changed = True
    return changed


class Store:
    def __init__(self, root: str | Path):
        self.root = Path(root).expanduser()
        self.inbox = self.root / "inbox"
        self.passes = self.root / "passes"

    @contextlib.contextmanager
    def locked(self):
        """Exclusive lock for one capture command; a second one fails at once."""
        self.root.mkdir(parents=True, exist_ok=True)
        fh = open(self.root / "capture.lock", "a+")
        try:
            try:
                fcntl.flock(fh, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError as exc:
                raise Busy("another capture command is running") from exc
            migrate(self.root)
            yield
        finally:
            fh.close()

    # Active pass: at most one pass is open at a time.
    def active_pass(self) -> str | None:
        p = self.root / "active-pass"
        return p.read_text().strip() or None if p.exists() else None

    def set_active_pass(self, pass_id: str | None) -> None:
        p = self.root / "active-pass"
        if pass_id is None:
            p.unlink(missing_ok=True)
        else:
            self.root.mkdir(parents=True, exist_ok=True)
            tmp = p.with_name(p.name + ".tmp")
            tmp.write_text(pass_id + "\n")
            os.replace(tmp, p)

    def cursors(self) -> dict:
        return read_json(self.root / "cursors.json", {})

    def set_cursor(self, thread_id: str, cursor: dict) -> None:
        cur = self.cursors()
        cur[thread_id] = cursor
        write_json(self.root / "cursors.json", cur)

    @staticmethod
    def key_name(key: dict) -> str:
        raw = "|".join(str(key[k]) for k in ("machine", "thread", "event", "index"))
        return hashlib.sha256(raw.encode()).hexdigest()[:24]

    def put(self, item: dict) -> Path:
        path = self.inbox / f"{self.key_name(item['key'])}.json"
        write_json(path, item)
        return path

    def inbox_items(self) -> list[dict]:
        if not self.inbox.exists():
            return []
        return [read_json(p) for p in sorted(self.inbox.glob("*.json"))]
