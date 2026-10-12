"""Capture configuration: defaults, overridden by ``~/.config/contextmd/capture.json``.

Model targets are T3 ``delegate_task`` targets. Changing a model is a config
change; each queue item records the model that actually answered.
"""

from __future__ import annotations

import copy
import json
import os
import socket
from pathlib import Path

DEFAULTS = {
    "machine": None,  # default: short host name
    "state_dir": "~/.local/state/contextmd/capture",
    "chromeria_db": "~/.t3/userdata/chromeria-v2.sqlite",
    "observer_bin": "agent-observer",
    "observer_db": "~/.local/state/agent-observer/observer.db",
    "extractors": {
        "sol": {"providerInstanceId": "codex", "model": "gpt-6.1-sol",
                "options": [{"id": "reasoningEffort", "value": "high"}]},
        "grok": {"providerInstanceId": "grok", "model": "grok-4.7",
                 "options": [{"id": "reasoningEffort", "value": "high"}]},
    },
    "judge": {"providerInstanceId": "claudeAgent", "model": "claude-opus-5-5",
              "options": [{"id": "effort", "value": "high"}]},
    # Rendered characters per segment; larger threads are cut into segments.
    "segment_chars": 150000,
    # Rendered characters of earlier events shown before a segment as context.
    "context_chars": 12000,
    "batch_min_siblings": 4,
    # clientRequestId prefix of capture's own delegated jobs; those threads are skipped.
    "own_task_prefix": "contextmd-capture-",
}

CONFIG_PATH = "~/.config/contextmd/capture.json"


def load(path: str | None = None) -> dict:
    cfg = copy.deepcopy(DEFAULTS)
    p = Path(os.path.expanduser(path or os.environ.get("CONTEXTMD_CAPTURE_CONFIG") or CONFIG_PATH))
    if p.exists():
        cfg.update(json.loads(p.read_text()))
    cfg["machine"] = cfg["machine"] or socket.gethostname().split(".")[0].lower()
    for k in ("state_dir", "chromeria_db", "observer_db"):
        cfg[k] = os.path.expanduser(cfg[k])
    cfg["observer_bin"] = os.path.expanduser(cfg["observer_bin"])
    return cfg
