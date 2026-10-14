"""Serving knowledge configuration: ``~/.config/contextmd/serving.json``.

``CONTEXTMD_SERVING_CONFIG`` points at another file (tests, a trial session).
Keys, all optional:

- ``exact_keys``: store directories (default ``~/.local/share/contextmd/exact-keys``)
- ``session_start``: files served at session start and after compaction, each
  ``{"path": ..., "max_chars": ...}``; they sit outside the turn budgets
- ``cue``: registered Library directories (default none); header fields choose each entry's matchers
- ``cue_url``: the Cue service, such as ``http://100.116.0.2:8710``; without it Cue is off
- ``great_library``: the index file named by the first-input and compaction pointer

Serving state (budgets, deny state, the log) lives in the Chromeria serving module.
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
    cue: list[str] = field(default_factory=list)
    cue_url: str | None = None
    great_library: str | None = None


def great_library_path(value: object) -> str:
    """Validate the pointer's single-line path after expanding the server user's home."""
    if not isinstance(value, str) or not value or not value.startswith(("/", "~/")):
        raise ValueError("great_library must be a nonempty absolute path or start with ~/")
    expanded = os.path.expanduser(value)
    if any(ord(c) < 32 or 127 <= ord(c) <= 159 or ord(c) in (0x2028, 0x2029)
           or 0xD800 <= ord(c) <= 0xDFFF for c in expanded):
        raise ValueError("great_library must not contain controls, line separators or lone surrogates")
    if len(expanded.encode("utf-8")) > 1024:
        raise ValueError("great_library expanded path exceeds 1024 UTF-8 bytes")
    return expanded


def path() -> Path:
    return Path(os.path.expanduser(os.environ.get(ENV) or DEFAULT_PATH))


def load(file: Path | None = None) -> Config:
    file = file or path()
    if not file.exists():
        return Config()
    raw = json.loads(file.read_text())
    unknown = set(raw) - {"exact_keys", "session_start", "cue", "cue_url", "great_library"}
    if unknown:
        raise ValueError(f"{file}: unknown keys {sorted(unknown)}")
    cfg = Config()
    if "exact_keys" in raw:
        cfg.exact_keys = list(raw["exact_keys"])
    if "session_start" in raw:
        cfg.session_start = [StartFile(**f) for f in raw["session_start"]]
    if "cue" in raw:
        cfg.cue = list(raw["cue"])
    if "cue_url" in raw:
        cfg.cue_url = raw["cue_url"]
    if "great_library" in raw:
        cfg.great_library = great_library_path(raw["great_library"])
    return cfg
