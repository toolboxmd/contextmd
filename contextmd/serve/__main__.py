"""contextmd-serve: hook entry point, off switch, log and store check.

    contextmd-serve hook claude-code      read one hook event on stdin, print the reply
    contextmd-serve off | on | status     the one switch for all serving on this machine
    contextmd-serve log [--session ID]    the per-moment log as JSON lines
    contextmd-serve check                 load the exact-key store and report errors
    contextmd-serve hooks claude-code     print the settings ``hooks`` block to install
"""

from __future__ import annotations

import argparse
import json
import os
import shlex
import sys
import time
import traceback
from pathlib import Path

from . import claude, config, store
from .server import Serving, log_records

ADAPTERS = {"claude-code": claude}


def _write(text: str) -> None:
    """Write straight to fd 1, so a failed write raises here and nothing stays buffered for exit."""
    data = text.encode()
    while data:
        data = data[os.write(1, data):]


def hook(host: str) -> int:
    """Fails open: any error is recorded in errors.log and the host proceeds as if unserved."""
    cfg = None
    try:
        cfg = config.load()
        if cfg.off_switch.exists():
            return 0
        event = json.load(sys.stdin)
        adapter = ADAPTERS[host]
        Serving(cfg).handle(adapter.moment(event), lambda reply: _write(adapter.render(event, reply)))
    except Exception:
        report = f"{time.time()} {host}\n{traceback.format_exc()}\n"
        try:
            if cfg is None:
                raise OSError("no config")
            cfg.state.mkdir(parents=True, exist_ok=True)
            with open(cfg.state / "errors.log", "a") as fh:
                fh.write(report)
        except OSError:
            sys.stderr.write(report)
    return 0


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(prog="contextmd-serve")
    sub = ap.add_subparsers(dest="cmd", required=True)
    for name in ("hook", "hooks"):
        sub.add_parser(name).add_argument("host", choices=sorted(ADAPTERS))
    for name in ("on", "off", "status", "check"):
        sub.add_parser(name)
    lp = sub.add_parser("log")
    lp.add_argument("--session")
    ns = ap.parse_args(argv)
    if ns.cmd == "hook":
        return hook(ns.host)
    cfg = config.load()
    if ns.cmd == "off":
        cfg.state.mkdir(parents=True, exist_ok=True)
        cfg.off_switch.touch()
    elif ns.cmd == "on":
        cfg.off_switch.unlink(missing_ok=True)
    elif ns.cmd == "hooks":
        bin_path = Path(__file__).resolve().parents[2] / "bin" / "contextmd-serve"
        command = " ".join(shlex.quote(a) for a in ("python3", "-I", str(bin_path), "hook", ns.host))
        print(json.dumps(ADAPTERS[ns.host].hooks(command), indent=2))
        return 0
    elif ns.cmd == "check":
        st = store.load(cfg.exact_keys)
        print(json.dumps({"entries": [{"id": e.id, "version": e.version, "must_know_before": e.must_know_before,
                                       "chars": len(e.text)} for e in st.entries],
                          "owners": st.owners, "errors": st.errors}, indent=2))
        return 1 if st.errors else 0
    elif ns.cmd == "log":
        if (cfg.state / "serving.db").exists():
            for rec in log_records(cfg):
                if ns.session is None or rec["session"] == ns.session:
                    print(json.dumps(rec))
        return 0
    print(json.dumps({"serving": "off" if cfg.off_switch.exists() else "on",
                      "config": str(config.path()), "state": str(cfg.state)}))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
