"""contextmd-serve: hook install, off switch, log and store check.

    contextmd-serve install <harness> --base DIR     install the harness hook for the Chromeria server on DIR
    contextmd-serve hooks <harness> --base DIR       print the hooks block (or OpenCode plugin) instead
    contextmd-serve off | on | status --base DIR     the one switch for all serving through DIR
    contextmd-serve log --base DIR [--session ID]    the serving log as JSON lines
    contextmd-serve check                            load the exact-key store and report errors
"""

from __future__ import annotations

import argparse
import json
from pathlib import Path

from . import config, install, store


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(prog="contextmd-serve")
    sub = ap.add_subparsers(dest="cmd", required=True)
    for name in ("install", "hooks"):
        p = sub.add_parser(name)
        p.add_argument("harness", choices=install.HARNESSES)
        p.add_argument("--base", required=True, type=Path)
        if name == "install":
            p.add_argument("--t3", default="t3", help="Chromeria's t3 command (issues the hooks' session)")
            p.add_argument("--renew", action="store_true", help="issue a new hook session even if the current one is valid")
    for name in ("on", "off", "status"):
        sub.add_parser(name).add_argument("--base", required=True, type=Path)
    lp = sub.add_parser("log")
    lp.add_argument("--base", required=True, type=Path)
    lp.add_argument("--session")
    sub.add_parser("check")
    ns = ap.parse_args(argv)
    if ns.cmd == "check":
        st = store.load(config.load().exact_keys)
        print(json.dumps({"entries": [{"id": e.id, "version": e.version, "must_know_before": e.must_know_before,
                                       "target": e.target, "bytes": len(e.text.encode())} for e in st.entries],
                          "owners": st.owners, "errors": st.errors}, indent=2))
        return 1 if st.errors else 0
    base = ns.base.expanduser().resolve()
    p = install.paths(base)
    if ns.cmd == "install":
        try:
            for written in install.install(ns.harness, base, ns.t3, ns.renew):
                print(written)
        except install.InstallError as exc:
            print(f"contextmd-serve install: {exc}")
            return 1
        return 0
    if ns.cmd == "hooks":
        if ns.harness == "opencode":
            print(install.plugin_source(base))
        else:
            print(json.dumps(install.hooks_block(ns.harness, base), indent=2))
        return 0
    if ns.cmd == "off":
        p["dir"].mkdir(parents=True, exist_ok=True)
        p["off"].touch()
    elif ns.cmd == "on":
        p["off"].unlink(missing_ok=True)
    elif ns.cmd == "log":
        from .log import records
        for rec in records(base / "userdata" / "contextmd.sqlite", ns.session):
            print(json.dumps(rec))
        return 0
    try:
        expires = json.loads(p["credential"].read_text()).get("expiresAt")
    except (OSError, ValueError):
        expires = None
    print(json.dumps({"serving": "off" if p["off"].exists() else "on", "base": str(base),
                      "credential_expires": expires}))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
