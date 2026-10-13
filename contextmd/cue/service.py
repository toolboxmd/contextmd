"""The Cue service: one HTTP process on the GPU serving host that scores a moment's window against Cue entries.

    contextmd-cue serve CONFIG          run the active variant (the systemd unit runs this)
    contextmd-cue deploy CONFIG ID      make variant ID active; the active one becomes previous; restart
    contextmd-cue rollback CONFIG       swap active and previous; restart
    contextmd-cue status CONFIG         active, previous and the live /health

API (bound to one address, the tailnet's; the caller is a Chromeria serving module):

    POST /score  {"items": [[kind, text], ...], "moment": {"kind": ..., "call": ..., "output": ...},
                  "entries": [{"id": ..., "doc": "<name>: <applies>"}], "deadline_ms": 1500}
             ->  {"adapter": id, "threshold": t, "scores": {id: score}, "ms": n, "queue_ms": n, "window_chars": n,
                  "window_tokens": n, "window_cut": bool}
                 503 {"error": "expired"} when the request waited past its deadline for the GPU
    GET  /health -> {"ok": true, "adapter": id, "threshold": t}

The window is built here (``window.py``), because its format belongs to the adapter. Requests run one at a time on
the GPU; one still waiting when its deadline passes is dropped, not run. Windows exist only in memory: nothing about
a request is written to disk, and the caller's serving log keeps ids, scores and timing. The process binds only
after its warm-up (a compiled variant takes minutes), so a starting service refuses connections instead of stalling
callers. Deploys and rollbacks are appended to the deployments log.
"""

from __future__ import annotations

import datetime
import json
import os
import subprocess
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

from . import window as W

MAX_BODY = 1_048_576
MAX_ENTRIES = 64
MAX_DOC_CHARS = 4000
MAX_ITEMS = 400
UNIT = "contextmd-cue.service"


class Variant:
    """One deployable scorer: base model, adapter, quantization, compile and its own threshold."""

    def __init__(self, vid: str, spec: dict):
        self.id = vid
        self.spec = spec
        self.threshold = float(spec["threshold"])


def load_config(path: Path) -> dict:
    cfg = json.loads(Path(path).read_text())
    for key in ("host", "port", "active", "variants", "deployments"):
        if key not in cfg:
            raise ValueError(f"{path}: missing {key}")
    if cfg["active"] not in cfg["variants"]:
        raise ValueError(f"{path}: active variant {cfg['active']!r} is not in variants")
    return cfg


def parse_request(raw: bytes) -> dict:
    """The validated request, or ValueError naming what is wrong."""
    req = json.loads(raw)
    if not isinstance(req, dict):
        raise ValueError("request is not an object")
    items = req.get("items")
    if not isinstance(items, list) or not items or len(items) > MAX_ITEMS:
        raise ValueError(f"items must be a list of 1 to {MAX_ITEMS} [kind, text] pairs")
    pairs = []
    for it in items:
        if not (isinstance(it, list) and len(it) == 2 and it[0] in ("user", "asst", "tool")
                and isinstance(it[1], str)):
            raise ValueError("each item must be [user|asst|tool, text]")
        pairs.append((it[0], it[1]))
    entries = req.get("entries")
    if not isinstance(entries, list) or not entries or len(entries) > MAX_ENTRIES:
        raise ValueError(f"entries must be a list of 1 to {MAX_ENTRIES}")
    for e in entries:
        if not (isinstance(e, dict) and isinstance(e.get("id"), str) and isinstance(e.get("doc"), str)
                and e["doc"] and len(e["doc"]) <= MAX_DOC_CHARS):
            raise ValueError(f"each entry needs an id and a doc of at most {MAX_DOC_CHARS} characters")
    moment = req.get("moment") or {}
    if not isinstance(moment, dict):
        raise ValueError("moment must be an object")
    deadline = req.get("deadline_ms", 1500)
    if not isinstance(deadline, (int, float)) or deadline <= 0:
        raise ValueError("deadline_ms must be a positive number")
    return {"items": pairs, "entries": entries, "moment": moment, "deadline_ms": float(deadline)}


def make_server(host: str, port: int, variant: Variant, score) -> ThreadingHTTPServer:
    """A server for one variant. ``score(window, docs) -> (scores, info)`` runs on the GPU, one call at a time."""
    gpu = threading.Lock()

    class Handler(BaseHTTPRequestHandler):
        def reply(self, code, obj):
            body = json.dumps(obj).encode()
            self.send_response(code)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def do_GET(self):
            if self.path != "/health":
                return self.reply(404, {"error": "not found"})
            self.reply(200, {"ok": True, "adapter": variant.id, "threshold": variant.threshold})

        def do_POST(self):
            arrived = time.monotonic()
            if self.path != "/score":
                return self.reply(404, {"error": "not found"})
            length = int(self.headers.get("Content-Length") or 0)
            if length <= 0 or length > MAX_BODY:
                return self.reply(413 if length > MAX_BODY else 400, {"error": "body size"})
            try:
                req = parse_request(self.rfile.read(length))
                m = req["moment"]
                text = W.window(req["items"], m.get("kind", "user_message"), m.get("call"), m.get("output"))
            except (ValueError, TypeError) as exc:
                return self.reply(400, {"error": str(exc)})
            with gpu:
                waited = time.monotonic() - arrived
                if waited * 1000 > req["deadline_ms"]:
                    return self.reply(503, {"error": "expired", "queue_ms": round(waited * 1000, 1)})
                t = time.monotonic()
                try:
                    scores, info = score(text, [e["doc"] for e in req["entries"]])
                except ValueError as exc:
                    return self.reply(400, {"error": str(exc)})
                ms = (time.monotonic() - t) * 1000
            self.reply(200, {"adapter": variant.id, "threshold": variant.threshold,
                             "scores": {e["id"]: s for e, s in zip(req["entries"], scores)},
                             "ms": round(ms, 1), "queue_ms": round(waited * 1000, 1), "window_chars": len(text),
                             **info})

        def log_message(self, *args):  # no access log: windows and moments stay off this host's disk
            pass

    server = ThreadingHTTPServer((host, port), Handler)
    server.daemon_threads = True
    return server


# Warm-up windows: synthetic, of growing length, so a compiled variant has traced its dynamic shapes before it
# binds. No transcript text is needed or kept.
def warmup_windows():
    sentence = "The agent reads the request, checks the repository and plans the next step. "
    return [W.window([("user", sentence * n)]) for n in (2, 8, 20, 45, 90, 140)]


WARMUP_DOCS = [f"Entry {k}: The agent is about to do step {k} of a task and needs procedure {k}." for k in range(9)]


def serve(config_path: Path) -> int:
    cfg = load_config(config_path)
    variant = Variant(cfg["active"], cfg["variants"][cfg["active"]])
    from .qwen import Scorer
    s = variant.spec
    t0 = time.time()
    scorer = Scorer(s["model"], s["instruction"], s.get("adapter"), s.get("quant", "none"), bool(s.get("compile")))
    loaded = time.time() - t0
    for text in warmup_windows():
        scorer.score(text, WARMUP_DOCS)
    server = make_server(cfg["host"], int(cfg["port"]), variant, scorer.score)
    print(json.dumps({"serving": variant.id, "threshold": variant.threshold, "host": cfg["host"],
                      "port": cfg["port"], "load_s": round(loaded, 1),
                      "warmup_s": round(time.time() - t0 - loaded, 1)}), flush=True)
    server.serve_forever()
    return 0


def _write_config(path: Path, cfg: dict) -> None:
    tmp = Path(f"{path}.tmp")
    tmp.write_text(json.dumps(cfg, indent=2) + "\n")
    os.replace(tmp, path)


def _record(cfg: dict, action: str, before: str, after: str) -> dict:
    rec = {"ts": datetime.datetime.now(datetime.timezone.utc).isoformat(timespec="seconds"), "action": action,
           "from": before, "to": after}
    log = Path(os.path.expanduser(cfg["deployments"]))
    log.parent.mkdir(parents=True, exist_ok=True)
    with log.open("a") as fh:
        fh.write(json.dumps(rec) + "\n")
    return rec


def switch(config_path: Path, action: str, target: str | None = None) -> dict:
    """Deploy ``target`` or roll back to the previous variant: rewrite the config and log it."""
    cfg = load_config(config_path)
    before = cfg["active"]
    if action == "rollback":
        target = cfg.get("previous")
        if not target:
            raise ValueError("no previous variant to roll back to")
    if target not in cfg["variants"]:
        raise ValueError(f"unknown variant {target!r}")
    if target == before:
        raise ValueError(f"{target} is already active")
    cfg["active"], cfg["previous"] = target, before
    _write_config(config_path, cfg)
    return _record(cfg, action, before, target)


def restart() -> None:
    subprocess.run(["systemctl", "--user", "restart", UNIT], check=True)


def status(config_path: Path) -> dict:
    import urllib.request
    cfg = load_config(config_path)
    try:
        with urllib.request.urlopen(f"http://{cfg['host']}:{cfg['port']}/health", timeout=2) as r:
            live = json.loads(r.read())
    except OSError as exc:
        live = {"ok": False, "error": str(exc)}
    return {"active": cfg["active"], "previous": cfg.get("previous"), "live": live}


def main(argv=None) -> int:
    import argparse
    ap = argparse.ArgumentParser(prog="contextmd-cue")
    sub = ap.add_subparsers(dest="cmd", required=True)
    sub.add_parser("serve").add_argument("config", type=Path)
    d = sub.add_parser("deploy")
    d.add_argument("config", type=Path)
    d.add_argument("variant")
    r = sub.add_parser("rollback")
    r.add_argument("config", type=Path)
    for p in (d, r):
        p.add_argument("--no-restart", action="store_true", help="only rewrite the config and log it")
    sub.add_parser("status").add_argument("config", type=Path)
    ns = ap.parse_args(argv)
    if ns.cmd == "serve":
        return serve(ns.config)
    if ns.cmd == "status":
        print(json.dumps(status(ns.config)))
        return 0
    try:
        rec = switch(ns.config, ns.cmd, getattr(ns, "variant", None))
    except ValueError as exc:
        print(f"contextmd-cue {ns.cmd}: {exc}", file=sys.stderr)
        return 1
    if not ns.no_restart:
        restart()
    print(json.dumps(rec))
    return 0
