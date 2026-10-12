"""Capture's local state: lock, per-thread cursors and the transit queue.

Everything lives under the state directory on the capturing machine, outside
any repository. A queue item is transit, not a store: later steps classify it,
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


class Store:
    def __init__(self, root: str | Path):
        self.root = Path(root).expanduser()
        self.queue = self.root / "queue"
        self.runs = self.root / "runs"

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
            yield
        finally:
            fh.close()

    # Active run: at most one run is open at a time.
    def active_run(self) -> str | None:
        p = self.root / "active-run"
        return p.read_text().strip() or None if p.exists() else None

    def set_active_run(self, run_id: str | None) -> None:
        p = self.root / "active-run"
        if run_id is None:
            p.unlink(missing_ok=True)
        else:
            self.root.mkdir(parents=True, exist_ok=True)
            p.write_text(run_id + "\n")

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
        path = self.queue / f"{self.key_name(item['key'])}.json"
        write_json(path, item)
        return path

    def queued(self) -> list[dict]:
        if not self.queue.exists():
            return []
        return [read_json(p) for p in sorted(self.queue.glob("*.json"))]
