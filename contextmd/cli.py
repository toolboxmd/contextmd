"""contextmd capture: prepare, next, record, commit, abandon, report, cites, apply and the experiment commands. Stdlib only; prints JSON."""

from __future__ import annotations

import argparse
import hashlib
import json
import sys
from pathlib import Path

from .capture import apply, cites, compare, config, dedupe, observer, report
from .capture.passes import Capture, CaptureError
from .capture.store import Busy, write_json


def _day_bounds(day: str) -> tuple[str, str]:
    from datetime import date, timedelta
    d = date.fromisoformat(day)
    return f"{d.isoformat()}T00:00:00Z", f"{(d + timedelta(days=1)).isoformat()}T00:00:00Z"


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(prog="contextmd")
    ap.add_argument("--config", default=None)
    sub = ap.add_subparsers(dest="area", required=True)
    cap = sub.add_parser("capture", help="extract and judge sessions into the local inbox")
    cs = cap.add_subparsers(dest="cmd", required=True)
    p = cs.add_parser("prepare", help="start a pass: sync Observer, cut segments")
    p.add_argument("--day", help="UTC day YYYY-MM-DD (sets --since and --until)")
    p.add_argument("--since")
    p.add_argument("--until")
    p.add_argument("--thread", action="append", help="limit to these thread ids")
    p.add_argument("--whole", action="store_true", help="one segment per thread")
    p.add_argument("--experiment", help="name; items stay in the pass, cursors do not move")
    cs.add_parser("next", help="jobs to dispatch now, and dispatched jobs still waiting")
    r = cs.add_parser("record", help="save a dispatched job's T3 task id, or mark it failed")
    r.add_argument("job")
    r.add_argument("--task-id")
    r.add_argument("--child-thread")
    r.add_argument("--failed", action="store_true")
    r.add_argument("--note")
    cs.add_parser("commit", help="write judged segments to the inbox and move cursors")
    ab = cs.add_parser("abandon", help="close the open pass; the next pass captures its uncommitted events again")
    ab.add_argument("--reason", required=True)
    rp = cs.add_parser("report", help="counts per step and thread kind, cost per model step")
    rp.add_argument("--pass", dest="pass_id")
    cp = cs.add_parser("compare", help="boundary check: whole-thread pass versus segmented pass")
    cp.add_argument("--whole-pass", required=True)
    cp.add_argument("--segmented-pass", required=True)
    cp.add_argument("--thread", required=True)
    cp.add_argument("--result", action="store_true", help="read the compare job's output")
    dd = cs.add_parser("dedupe", help="group a finished pass's kept lessons across segments (one judge job)")
    dd.add_argument("--pass", dest="pass_id", required=True)
    dd.add_argument("--result", action="store_true", help="apply the judge's groups to the pass's items")
    rp2 = cs.add_parser("replay", help="experiment pass over exact segment transcripts of an earlier pass")
    rp2.add_argument("--from-pass", required=True)
    rp2.add_argument("--segment", action="append", required=True)
    rp2.add_argument("--experiment", required=True)
    rp2.add_argument("--extract-prompt", default="extract.md", help="prompt file under prompts/capture/")
    rp2.add_argument("--reuse-extracts", action="store_true",
                     help="copy the source pass's extractor outputs; only the judge runs again")
    ct = cs.add_parser("cites", help="check the cites in a context packets file in place")
    ct.add_argument("packets", help="JSON object of packets keyed by item number")
    ap_ = cs.add_parser("apply", help="write one shaped entry for a kept inbox item: one commit, pushed")
    ap_.add_argument("item", help="inbox item name (its key hash)")
    ap_.add_argument("--entry", required=True, help="the shaped entry file")
    ap_.add_argument("--store", required=True, help="exact-key store directory, inside a git checkout")
    ns = ap.parse_args(argv)
    cfg = config.load(ns.config)
    if ns.cmd == "cites":
        packets = json.loads(Path(ns.packets).read_text())
        con = observer.connect(cfg["observer_db"])
        try:
            out = cites.check_packets(packets, con)
        finally:
            con.close()
        write_json(Path(ns.packets), packets)
        print(json.dumps(out, indent=1))
        return 0
    if ns.cmd == "apply":
        try:
            out = apply.apply(cfg, ns.item, Path(ns.entry), Path(ns.store))
        except (CaptureError, Busy) as exc:
            print(f"capture apply: {exc}", file=sys.stderr)
            return 1
        print(json.dumps(out, indent=1))
        return 0
    cap_ = Capture(cfg)
    try:
        if ns.cmd in ("report", "compare", "dedupe"):
            with cap_.store.locked():
                out = _read_side(ns, cap_)
        elif ns.cmd == "prepare":
            since, until = _day_bounds(ns.day) if ns.day else (ns.since, ns.until)
            if not since or not until:
                ap.error("prepare needs --day or --since and --until")
            pass_ = cap_.prepare(since, until, thread_ids=ns.thread, whole=ns.whole,
                               experiment=ns.experiment)
            out = {"pass_id": pass_["pass_id"], "segments": len(pass_["segments"]),
                   "jobs": len(pass_["jobs"]),
                   "threads": report.build(pass_, [])["steps"]}
        elif ns.cmd == "next":
            out = cap_.next_jobs()
        elif ns.cmd == "record":
            out = cap_.record(ns.job, task_id=ns.task_id, child_thread=ns.child_thread,
                              failed=ns.failed, note=ns.note)
        elif ns.cmd == "commit":
            out = cap_.commit()
        elif ns.cmd == "abandon":
            out = cap_.abandon(ns.reason)
        elif ns.cmd == "replay":
            pass_ = cap_.replay(ns.from_pass, ns.segment, ns.experiment, ns.extract_prompt, ns.reuse_extracts)
            out = {"pass_id": pass_["pass_id"], "segments": len(pass_["segments"]), "jobs": len(pass_["jobs"]),
                   "contracts": pass_["contracts"]}
    except (CaptureError, Busy, observer.SyncFailed) as exc:
        print(f"capture: {exc}", file=sys.stderr)
        return 1
    print(json.dumps(out, indent=1, ensure_ascii=False))
    return 0


def _read_side(ns, cap_: Capture) -> dict:
    if ns.cmd == "dedupe":
        pass_ = cap_.load(ns.pass_id)
        if pass_["state"] != "complete":
            raise CaptureError(f"pass {ns.pass_id} is {pass_['state']}; dedupe needs a complete pass")
        rdir = cap_.store.passes / ns.pass_id
        files = dedupe.item_files(rdir, cap_.store.inbox, ns.pass_id)
        out = (dedupe.apply if ns.result else dedupe.prepare)(rdir / "dedupe", files)
        if ns.result:
            write_json(rdir / "dedupe" / "result.json", out)
        return out
    if ns.cmd == "compare":
        seg = cap_.load(ns.segmented_pass)
        name = hashlib.sha256(f"{ns.segmented_pass}|{ns.thread}".encode()).hexdigest()[:12]
        out_dir = cap_.store.passes / ns.whole_pass / "compare" / name
        if ns.result:
            cuts = [s_["last"]["ts"] for s_ in seg["segments"] if s_["thread"] == ns.thread][:-1]
            out = compare.result(out_dir, cuts)
            write_json(out_dir / "result.json", out)
        else:
            out = compare.prepare(
                out_dir, ns.thread,
                report.pass_items(cap_.store.passes / ns.whole_pass, cap_.store.inbox, ns.whole_pass),
                report.pass_items(cap_.store.passes / ns.segmented_pass, cap_.store.inbox, ns.segmented_pass))
    else:
        pass_ = cap_.load(ns.pass_id) if ns.pass_id else _latest(cap_)
        rdir = cap_.store.passes / pass_["pass_id"]
        out = report.build(pass_, report.pass_items(rdir, cap_.store.inbox, pass_["pass_id"]))
        write_json(rdir / "report.json", out)
    return out


def _latest(cap_: Capture) -> dict:
    active = cap_.store.active_pass()
    if active:
        return cap_.load(active)
    passes = sorted(p.name for p in cap_.store.passes.iterdir()) if cap_.store.passes.exists() else []
    if not passes:
        raise CaptureError("no capture passes yet")
    return cap_.load(passes[-1])
