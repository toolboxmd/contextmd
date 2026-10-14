"""contextmd-serve: hook install, off switch, log and store check.

    contextmd-serve install <harness> --base DIR     install the harness hook for the Chromeria server on DIR
    contextmd-serve hooks <harness> --base DIR       print the hooks block (or OpenCode plugin) instead
    contextmd-serve off | on | status --base DIR     the one switch for all serving through DIR
    contextmd-serve log --base DIR [--session ID]    the serving log as JSON lines
    contextmd-serve check                            load the exact-key and Cue stores and report errors
    contextmd-serve report --base DIR [--since DATE] [--json]
                                                     what each served entry did: shown, read, late, Observer record
    contextmd-serve health --base DIR [--from DATE --to DATE] [--dry-run]
                                                     rate the serving log since the last run; file defects at their owners
    contextmd-serve card --base DIR --candidate ID [--json]
                                                     one serve as the outcome judge reads it (prompts/serve/label.md)
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
    check = sub.add_parser("check")
    inputs = check.add_mutually_exclusive_group()
    inputs.add_argument("--entry", nargs="+", type=Path, help="strictly validate newly authored YAML entries")
    inputs.add_argument("--project", type=Path, help="also check this project's conventional Library")
    rp = sub.add_parser("report")
    rp.add_argument("--base", required=True, type=Path)
    rp.add_argument("--since", help="ISO date or time, UTC unless it names a zone")
    rp.add_argument("--observer-db", type=Path, help="Agent Observer's ledger, read-only"
                    " (default: $AGENT_OBSERVER_DB, else Observer's default path)")
    rp.add_argument("--json", action="store_true", help="one JSON row per serve instead of cards")
    hp = sub.add_parser("health")
    hp.add_argument("--base", required=True, type=Path)
    hp.add_argument("--from", dest="start", help="slice start, ISO date or time (UTC unless it names a zone)")
    hp.add_argument("--to", dest="end", help="slice end; a slice never moves the stored window")
    hp.add_argument("--dry-run", action="store_true", help="report and say what would be filed; write nothing")
    hp.add_argument("--config", type=Path, help="health configuration (default ~/.config/contextmd/health.json)")
    hp.add_argument("--state", type=Path, help="stored window (default ~/.local/state/contextmd/health.json)")
    hp.add_argument("--gh", default="gh", help="the GitHub CLI")
    hp.add_argument("--observer-db", type=Path, help="Agent Observer's ledger, read-only"
                    " (default: $AGENT_OBSERVER_DB, else Observer's default path)")
    hp.add_argument("--now", help=argparse.SUPPRESS)
    cp = sub.add_parser("card")
    cp.add_argument("--base", required=True, type=Path)
    cp.add_argument("--candidate", required=True, type=int, help="a candidate id from the serving log")
    cp.add_argument("--observer-db", type=Path, help="Agent Observer's ledger, read-only"
                    " (default: $AGENT_OBSERVER_DB, else Observer's default path)")
    cp.add_argument("--observer", default="agent-observer", help="Agent Observer's command, for `event show`")
    cp.add_argument("--json", action="store_true", help="the card as JSON, for the outcome judge")
    ns = ap.parse_args(argv)
    if ns.cmd == "check":
        if ns.entry:
            entries, errors = [], []
            for file in ns.entry:
                try:
                    parsed = store.parse_file(file.expanduser().absolute())
                    exact, intent = parsed.views()
                    entries.append({"file": parsed.path, "id": parsed.id, "version": parsed.version,
                                    "matchers": (["exact_key"] if exact else []) + (["intent"] if intent else []),
                                    "long": parsed.long})
                except (store.EntryError, OSError) as exc:
                    errors.append(f"{file}: {exc}")
            print(json.dumps({"entries": entries, "errors": errors}, indent=2))
            return 1 if errors else 0
        cfg = config.load()
        directory = store.library_dir(str(ns.project)) if ns.project else None
        sources = ([store.Source(directory, False)] if directory else []) + cfg.exact_keys + cfg.cue
        library = store.load(sources)
        exact_files = {e.path for e in library.entries}
        cue_files = {e.path for e in library.cue}
        def row(e):
            return {"id": e.id, "version": e.version, "target": e.target, "file": e.path,
                    "bytes": len(e.text.encode()), "long": e.long, "legacy": e.legacy,
                    "matchers": (["exact_key"] if e.path in exact_files else []) + (["intent"] if e.path in cue_files else [])}
        report = {
            "exact_keys": {"entries": [{**row(e), "must_know_before": e.must_know_before} for e in library.entries],
                           "owners": library.owners, "errors": library.errors, "legacy": library.legacy},
            "cue": {"url": cfg.cue_url,
                    "entries": [{**row(e), "name": e.name, "applies_chars": len(e.applies)} for e in library.cue],
                    "owners": library.owners, "errors": library.errors, "legacy": library.legacy},
        }
        if ns.project:
            project_entries = {e.path: e for e in [*library.entries, *library.cue]
                               if directory and Path(e.path).parent == Path(directory)}
            report["project"] = {"entries": [row(e) for e in project_entries.values()],
                                 "owners": {directory: library.owners[directory]} if directory in library.owners else {},
                                 "errors": library.errors, "legacy": library.legacy}
        print(json.dumps(report, indent=2))
        return 1 if library.errors else 0
    base = ns.base.expanduser().resolve()
    if ns.cmd == "health":
        return _health(ns, base)
    if ns.cmd == "card":
        from . import card, report
        db = base / "userdata" / "contextmd.sqlite"
        try:
            if not db.exists():
                raise card.CardError(f"no serving log at {db}")
            built = card.build(db, report.Observer(ns.observer_db), ns.candidate, ns.observer)
        except (card.CardError, report.LogError) as exc:
            print(f"contextmd-serve card: {exc}")
            return 1
        print(json.dumps(built, indent=2) if ns.json else card.render(built))
        return 0
    if ns.cmd == "report":
        from . import report
        db = base / "userdata" / "contextmd.sqlite"
        if not db.exists():
            print(f"No serving log at {db}.")
            return 0
        try:
            since = report.parse_since(ns.since)
        except ValueError:
            print(f"contextmd-serve report: --since needs an ISO date or time, not {ns.since!r}")
            return 2
        try:
            served, canaries = report.rows(db, report.Observer(ns.observer_db), since)
        except report.LogError as exc:
            print(f"contextmd-serve report: {exc}")
            return 1
        print("\n".join(json.dumps(r) for r in served) if ns.json else report.render(served, canaries))
        return 0
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


def _health(ns, base: Path) -> int:
    from datetime import datetime, timezone

    from . import health, measures, report
    try:
        now = report.parse_since(ns.now) if ns.now else datetime.now(timezone.utc).timestamp()
        start, end = report.parse_since(ns.start), report.parse_since(ns.end)
    except ValueError:
        print("contextmd-serve health: --from, --to and --now need an ISO date or time")
        return 2
    if (start is None) != (end is None):
        print("contextmd-serve health: --from and --to go together")
        return 2
    cfg = health.load_config((ns.config or Path(health.CONFIG)).expanduser())
    state = (ns.state or Path(health.STATE)).expanduser()
    db = base / "userdata" / "contextmd.sqlite"
    sliced = start is not None
    if not sliced:
        start, end = health.window(state, db, now), now
    result = health.measure(db, start, end, cfg["expected_harnesses"], now)
    if not result.unreadable:
        try:
            current = measures.measure(db, ns.observer_db, start, end)
            previous = measures.measure(db, ns.observer_db, 2 * start - end, start)
        except measures.ObserverError as exc:
            print(health.report(result, cfg["expected_harnesses"], cfg["machine"]))
            print(f"contextmd-serve health: Agent Observer's ledger is unreadable: {exc}")
            return 1
        result.measures = measures.render(current, previous)
        result.findings += [health.Finding(cls, rows) for cls, rows in measures.findings(current, previous)]
    print(health.report(result, cfg["expected_harnesses"], cfg["machine"]))
    try:
        actions = health.file(result, health.GitHub(ns.gh, ns.dry_run), cfg["machine"], cfg["seed"])
    except RuntimeError as exc:
        print(f"contextmd-serve health: {exc}")
        return 1
    for action in actions:
        print(action)
    if not (sliced or ns.dry_run or result.unreadable):
        health.save_window(state, db, end)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
