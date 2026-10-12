"""Merge the same lesson captured in several segments before counting ``found_by``.

The judge pairs the two extractors' items only inside one segment, so a lesson
found by Sol in one segment and by Grok in another counts as two single-extractor
lessons. One judge job groups a run's kept lessons across segments and threads;
code then gives each item its group and the group's combined ``found_by``.
Items stay in the queue: reflection still decides what reaches an owner.
"""

from __future__ import annotations

import json
from pathlib import Path

from .store import read_json, write_json

PROMPT = Path(__file__).resolve().parents[2] / "prompts" / "capture" / "dedupe.md"


def item_files(run_dir: Path, queue_dir: Path, run_id: str) -> list[tuple[Path, dict]]:
    src = run_dir / "items"
    paths = sorted(src.glob("*.json")) if src.exists() else sorted(queue_dir.glob("*.json"))
    out = []
    for p in paths:
        it = read_json(p)
        if it and it["run"]["run_id"] == run_id:
            out.append((p, it))
    return out


def prepare(out_dir: Path, files: list[tuple[Path, dict]]) -> dict:
    out_dir.mkdir(parents=True, exist_ok=True)
    ids = {}
    with open(out_dir / "items.jsonl", "w") as fh:
        for n, (p, it) in enumerate(f for f in files if f[1]["grade"] == "keep"):
            ident = f"D{n + 1:03d}"
            ids[ident] = p.name
            fh.write(json.dumps({"id": ident, "title": it["title"], "what": it["what"]},
                                ensure_ascii=False) + "\n")
    write_json(out_dir / "ids.json", ids)
    output = out_dir / "groups.jsonl"
    (out_dir / "brief.md").write_text(PROMPT.read_text().replace("{items}", str(out_dir / "items.jsonl"))
                                      .replace("{output}", str(output)))
    return {"keep_lessons": len(ids), "brief": str(out_dir / "brief.md"), "output": str(output)}


def apply(out_dir: Path, files: list[tuple[Path, dict]]) -> dict:
    """Validate the groups and write each kept item's group and combined ``found_by``."""
    ids = read_json(out_dir / "ids.json")
    by_name = {p.name: (p, it) for p, it in files}
    seen, groups = set(), []
    for n, line in enumerate((out_dir / "groups.jsonl").read_text().splitlines(), 1):
        line = line.strip()
        if not line or line.startswith("```"):
            continue
        members = json.loads(line).get("ids") or []
        for m in members:
            if m not in ids or m in seen:
                raise ValueError(f"group line {n} names {m}, unknown or already grouped")
            seen.add(m)
        if len(members) > 1:
            groups.append(members)
    groups += [[m] for m in ids if m not in seen]
    for g, members in enumerate(groups):
        found = sorted({e for m in members for e in by_name[ids[m]][1]["found_by"]})
        for m in members:
            p, it = by_name[ids[m]]
            it["dedupe"] = {"group": f"G{g + 1:03d}", "size": len(members), "found_by": found}
            write_json(p, it)
    return {"keep_lessons": len(ids), "distinct_lessons": len(groups),
            "groups_merging_items": sum(1 for g in groups if len(g) > 1)}
