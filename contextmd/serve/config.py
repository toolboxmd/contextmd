"""Serving configuration: ``~/.config/contextmd/serving.json``.

``CONTEXTMD_SERVING_CONFIG`` points at another file (tests, a trial session).
Keys, all optional:

- ``exact_keys``: store directories (default ``~/.local/share/contextmd/exact-keys``)
- ``session_start``: files served at session start and after compaction, each
  ``{"path": ..., "max_chars": ...}``; they sit outside the turn budget
- ``turn_budget``: served characters per user turn (default 10,000)
- ``state_dir``: database, off switch and errors (default ``~/.local/state/contextmd/serving``)
"""

from __future__ import annotations

import json
import os
from dataclasses import dataclass, field
from pathlib import Path

ENV = "CONTEXTMD_SERVING_CONFIG"
DEFAULT_PATH = "~/.config/contextmd/serving.json"


@dataclass
class StartFile:
    path: str
    max_chars: int = 4000


@dataclass
class Config:
    exact_keys: list[str] = field(default_factory=lambda: ["~/.local/share/contextmd/exact-keys"])
    session_start: list[StartFile] = field(default_factory=list)
    turn_budget: int = 10_000
    state_dir: str = "~/.local/state/contextmd/serving"

    @property
    def state(self) -> Path:
        return Path(os.path.expanduser(self.state_dir))

    @property
    def off_switch(self) -> Path:
        return self.state / "off"


def path() -> Path:
    return Path(os.path.expanduser(os.environ.get(ENV) or DEFAULT_PATH))


def load(file: Path | None = None) -> Config:
    file = file or path()
    if not file.exists():
        return Config()
    raw = json.loads(file.read_text())
    unknown = set(raw) - {"exact_keys", "session_start", "turn_budget", "state_dir"}
    if unknown:
        raise ValueError(f"{file}: unknown keys {sorted(unknown)}")
    cfg = Config()
    if "exact_keys" in raw:
        cfg.exact_keys = list(raw["exact_keys"])
    if "session_start" in raw:
        cfg.session_start = [StartFile(**f) for f in raw["session_start"]]
    if "turn_budget" in raw:
        cfg.turn_budget = int(raw["turn_budget"])
    if "state_dir" in raw:
        cfg.state_dir = raw["state_dir"]
    return cfg
