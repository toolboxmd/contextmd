"""Boundary check: segmented capture versus whole-thread capture of the same threads.

A whole run (``prepare --whole --experiment``) and the segmented run cover the
same window. A judge pairs the two runs' kept lessons per thread; code then
reports lessons only the whole run found (lost to segmenting), lessons only
the segmented run found, and how close each unmatched lesson sits to a
segment boundary.
"""

from __future__ import annotations

import json
from datetime import datetime
from pathlib import Path

from .store import read_json, write_json

PROMPT = Path(__file__).resolve().parents[2] / "prompts" / "capture" / "compare.md"


def _ts(s: str) -> float:
    return datetime.fromisoformat(s.replace("Z", "+00:00")).timestamp()


def _brief(item: dict, ident: str) -> dict:
    return {"id": ident, "title": item.get("title"), "what": item.get("what"),
            "so_what": item.get("so_what")}


def prepare(out_dir: Path, thread: str, whole: list[dict], segmented: list[dict]) -> dict:
    keep_w = [i for i in whole if i["grade"] == "keep" and i["key"]["thread"] == thread]
    keep_s = [i for i in segmented if i["grade"] == "keep" and i["key"]["thread"] == thread]
    out_dir.mkdir(parents=True, exist_ok=True)
    ids = {}
    with open(out_dir / "whole.jsonl", "w") as fh:
        for n, it in enumerate(keep_w, 1):
            ids[f"W{n:02d}"] = it
            fh.write(json.dumps(_brief(it, f"W{n:02d}"), ensure_ascii=False) + "\n")
    with open(out_dir / "segmented.jsonl", "w") as fh:
        for n, it in enumerate(keep_s, 1):
            ids[f"S{n:02d}"] = it
            fh.write(json.dumps(_brief(it, f"S{n:02d}"), ensure_ascii=False) + "\n")
    write_json(out_dir / "ids.json", {k: {"key": v["key"], "ts": v["source_time"],
                                          "segment": v["run"]["segment"]} for k, v in ids.items()})
    output = out_dir / "pairs.jsonl"
    brief = PROMPT.read_text().replace("{whole}", str(out_dir / "whole.jsonl")) \
        .replace("{segmented}", str(out_dir / "segmented.jsonl")).replace("{output}", str(output))
    (out_dir / "brief.md").write_text(brief)
    return {"whole": len(keep_w), "segmented": len(keep_s), "output": str(output),
            "brief": str(out_dir / "brief.md")}


def result(out_dir: Path, boundaries: list[str]) -> dict:
    """Matched and unmatched lessons; distance of unmatched ones to the nearest cut."""
    ids = read_json(out_dir / "ids.json")
    seen, pairs = set(), []
    for line in (out_dir / "pairs.jsonl").read_text().splitlines():
        if not line.strip() or line.startswith("```"):
            continue
        g = json.loads(line)
        members = list(g.get("w") or []) + list(g.get("s") or [])
        for m in members:
            if m not in ids or m in seen:
                raise ValueError(f"compare output names {m} twice or unknown")
            seen.add(m)
        pairs.append(g)
    missing = set(ids) - seen
    if missing:
        raise ValueError(f"compare output left {sorted(missing)} out")
    cuts = [_ts(b) for b in boundaries]

    def near(ident):
        if not cuts:
            return None
        return round(min(abs(_ts(ids[ident]["ts"]) - c) for c in cuts) / 60, 1)

    matched = [g for g in pairs if g.get("w") and g.get("s")]
    lost = [g for g in pairs if g.get("w") and not g.get("s")]
    extra = [g for g in pairs if g.get("s") and not g.get("w")]
    wrong = [g for g in matched if g.get("segmented_wrong")]
    return {
        "whole_keep": sum(1 for k in ids if k.startswith("W")),
        "segmented_keep": sum(1 for k in ids if k.startswith("S")),
        "matched": len(matched), "lost_by_segmenting": len(lost),
        "only_segmented": len(extra), "segmented_wrong": len(wrong),
        "lost": [{"ids": g["w"], "note": g.get("note"),
                  "minutes_from_cut": near(g["w"][0])} for g in lost],
        "only_segmented_items": [{"ids": g["s"], "note": g.get("note"),
                                  "minutes_from_cut": near(g["s"][0])} for g in extra],
        "wrong": [{"ids": g["s"], "note": g.get("note"),
                   "minutes_from_cut": near(g["s"][0])} for g in wrong],
    }
