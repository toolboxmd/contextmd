"""Run report: counts per step and per thread kind, cost per model step."""

from __future__ import annotations

import statistics
from collections import Counter, defaultdict
from pathlib import Path

from .store import read_json


def build(run: dict, items: list[dict]) -> dict:
    threads = run["threads"]
    by_kind = defaultdict(Counter)
    for t in threads.values():
        by_kind[t["kind"]]["threads"] += 1
        by_kind[t["kind"]][t["status"] or "unknown"] += 1
    seg_kind = {s["id"]: threads[s["thread"]]["kind"] for s in run["segments"]}
    for s in run["segments"]:
        by_kind[seg_kind[s["id"]]]["segments"] += 1
    extracted = Counter()
    for it in items:
        k = it["thread_kind"]
        by_kind[k]["lessons"] += 1
        by_kind[k][f"grade:{it['grade']}"] += 1
        if it["grade"] == "keep":
            by_kind[k]["keep:found_by:" + "+".join(it["found_by"])] += 1
        for m in it["members"]:
            extracted[(k, m["extractor"])] += 1
    for (k, name), n in extracted.items():
        by_kind[k][f"items:{name}"] += n
    steps = {
        "threads_active": len(threads),
        "threads_skipped": dict(Counter(t["status"] for t in threads.values()
                                        if (t["status"] or "").startswith("skipped"))),
        "threads_captured": sum(1 for t in threads.values() if t["status"] == "captured"),
        "threads_no_new_events": sum(1 for t in threads.values() if t["status"] == "no-new-events"),
        "threads_held_back": sum(1 for t in threads.values() if t.get("held_back")),
        "lessons_left_to_next_segment": sum(s.get("handed_off", 0) for s in run["segments"]),
        "segment_errors": {s["id"]: s["error"] for s in run["segments"] if s.get("error")},
        "segments": len(run["segments"]),
        "segments_committed": sum(1 for s in run["segments"] if s["committed"]),
        "extracted_items": dict(Counter(m["extractor"] for it in items for m in it["members"])),
        "lessons": len(items),
        "grades": dict(Counter(it["grade"] for it in items)),
        "unsupported": sum(1 for it in items if it["unsupported"]),
        "keep_found_by": dict(Counter("+".join(it["found_by"]) for it in items if it["grade"] == "keep")),
        # After dedupe: one count per distinct lesson, with found_by over all its copies.
        "keep_found_by_distinct": (dict(Counter("+".join(g["found_by"]) for g in {
            it["dedupe"]["group"]: it["dedupe"] for it in items if it.get("dedupe")}.values()))
            if any(it.get("dedupe") for it in items) else None),
        "learned_from_keep": dict(Counter(it["learned_from"] for it in items if it["grade"] == "keep")),
        "anchors": dict(Counter((it["moment"]["observer"] or {}).get("anchor", "none") for it in items)),
        "cited": dict(Counter(it["moment"]["cited"] for it in items)),
    }
    cost = defaultdict(lambda: {"jobs": 0, "attempts": 0, "failed_attempts": 0, "wall_s": [],
                                "input_tokens": 0, "cached_input_tokens": 0, "output_tokens": 0,
                                "reasoning_tokens": 0, "observed_models": Counter()})
    for job in run["jobs"].values():
        name = job.get("extractor") or "judge"
        c = cost[name]
        if job["attempts"]:
            c["jobs"] += 1
        c["attempts"] += len(job["attempts"])
        c["failed_attempts"] += sum(1 for a in job["attempts"] if a.get("status") == "failed")
        ev = job.get("evidence")
        if ev:
            if ev.get("wall_s") is not None:
                c["wall_s"].append(ev["wall_s"])
            for k in ("input_tokens", "cached_input_tokens", "output_tokens", "reasoning_tokens"):
                c[k] += (ev.get("usage") or {}).get(k, 0)
            for m in ev.get("observed") or []:
                c["observed_models"][m] += 1
    cost_out = {}
    for name, c in cost.items():
        w = c.pop("wall_s")
        c["wall_s_median"] = statistics.median(w) if w else None
        c["wall_s_total"] = sum(w) if w else None
        c["observed_models"] = dict(c["observed_models"])
        cost_out[name] = c
    return {"run_id": run["run_id"], "machine": run["machine"], "since": run["since"],
            "until": run["until"], "state": run["state"], "steps": steps,
            "by_kind": {k: dict(v) for k, v in sorted(by_kind.items())}, "cost": cost_out}


def run_items(run_dir: Path, queue_dir: Path, run_id: str) -> list[dict]:
    src = run_dir / "items"
    paths = sorted(src.glob("*.json")) if src.exists() else sorted(queue_dir.glob("*.json"))
    out = [read_json(p) for p in paths]
    return [i for i in out if i and i["run"]["run_id"] == run_id]
