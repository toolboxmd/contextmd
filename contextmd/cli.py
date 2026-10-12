"""contextmd capture prepare|next|record|commit|report. Stdlib only; prints JSON."""

from __future__ import annotations

import argparse
import hashlib
import json
import sys

from .capture import compare, config, observer, report
from .capture.run import Capture, RunError
from .capture.store import Busy, write_json


def _day_bounds(day: str) -> tuple[str, str]:
    from datetime import date, timedelta
    d = date.fromisoformat(day)
    return f"{d.isoformat()}T00:00:00Z", f"{(d + timedelta(days=1)).isoformat()}T00:00:00Z"


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(prog="contextmd")
    ap.add_argument("--config", default=None)
    sub = ap.add_subparsers(dest="area", required=True)
    cap = sub.add_parser("capture", help="extract and judge sessions into the local queue")
    cs = cap.add_subparsers(dest="cmd", required=True)
    p = cs.add_parser("prepare", help="start a run: sync Observer, cut segments")
    p.add_argument("--day", help="UTC day YYYY-MM-DD (sets --since and --until)")
    p.add_argument("--since")
    p.add_argument("--until")
    p.add_argument("--thread", action="append", help="limit to these thread ids")
    p.add_argument("--whole", action="store_true", help="one segment per thread")
    p.add_argument("--experiment", help="name; items stay in the run, cursors do not move")
    cs.add_parser("next", help="jobs to dispatch now, and dispatched jobs still waiting")
    r = cs.add_parser("record", help="save a dispatched job's T3 task id, or mark it failed")
    r.add_argument("job")
    r.add_argument("--task-id")
    r.add_argument("--child-thread")
    r.add_argument("--failed", action="store_true")
    r.add_argument("--note")
    cs.add_parser("commit", help="write judged segments to the queue and move cursors")
    ab = cs.add_parser("abandon", help="close the open run; its uncommitted segments stay uncaptured")
    ab.add_argument("--reason", required=True)
    rp = cs.add_parser("report", help="counts per step and thread kind, cost per model step")
    rp.add_argument("--run")
    cp = cs.add_parser("compare", help="boundary check: whole-thread run versus segmented run")
    cp.add_argument("--whole-run", required=True)
    cp.add_argument("--segmented-run", required=True)
    cp.add_argument("--thread", required=True)
    cp.add_argument("--result", action="store_true", help="read the compare job's output")
    ns = ap.parse_args(argv)
    cfg = config.load(ns.config)
    cap_ = Capture(cfg)
    try:
        if ns.cmd in ("report", "compare"):
            with cap_.store.locked():
                out = _read_side(ns, cap_)
        elif ns.cmd == "prepare":
            since, until = _day_bounds(ns.day) if ns.day else (ns.since, ns.until)
            if not since or not until:
                ap.error("prepare needs --day or --since and --until")
            run = cap_.prepare(since, until, thread_ids=ns.thread, whole=ns.whole,
                               experiment=ns.experiment)
            out = {"run_id": run["run_id"], "segments": len(run["segments"]),
                   "jobs": len(run["jobs"]),
                   "threads": report.build(run, [])["steps"]}
        elif ns.cmd == "next":
            out = cap_.next_jobs()
        elif ns.cmd == "record":
            out = cap_.record(ns.job, task_id=ns.task_id, child_thread=ns.child_thread,
                              failed=ns.failed, note=ns.note)
        elif ns.cmd == "commit":
            out = cap_.commit()
        elif ns.cmd == "abandon":
            out = cap_.abandon(ns.reason)
    except (RunError, Busy, observer.SyncFailed) as exc:
        print(f"capture: {exc}", file=sys.stderr)
        return 1
    print(json.dumps(out, indent=1, ensure_ascii=False))
    return 0


def _read_side(ns, cap_: Capture) -> dict:
    if ns.cmd == "compare":
        seg = cap_.load(ns.segmented_run)
        name = hashlib.sha256(ns.thread.encode()).hexdigest()[:12]
        out_dir = cap_.store.runs / ns.whole_run / "compare" / name
        if ns.result:
            cuts = [s_["last"]["ts"] for s_ in seg["segments"] if s_["thread"] == ns.thread][:-1]
            out = compare.result(out_dir, cuts)
            write_json(out_dir / "result.json", out)
        else:
            out = compare.prepare(
                out_dir, ns.thread,
                report.run_items(cap_.store.runs / ns.whole_run, cap_.store.queue, ns.whole_run),
                report.run_items(cap_.store.runs / ns.segmented_run, cap_.store.queue, ns.segmented_run))
    else:
        run = cap_.load(ns.run) if ns.run else _latest(cap_)
        rdir = cap_.store.runs / run["run_id"]
        out = report.build(run, report.run_items(rdir, cap_.store.queue, run["run_id"]))
        write_json(rdir / "report.json", out)
    return out


def _latest(cap_: Capture) -> dict:
    active = cap_.store.active_run()
    if active:
        return cap_.load(active)
    runs = sorted(p.name for p in cap_.store.runs.iterdir()) if cap_.store.runs.exists() else []
    if not runs:
        raise RunError("no capture runs yet")
    return cap_.load(runs[-1])
