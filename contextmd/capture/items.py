"""Parse extractor and judge output, and merge judged groups into items."""

from __future__ import annotations

import json
import random
import re

LEARNED_FROM = ("failure", "user", "discovered", "served_or_loaded", "brief_or_parent", "external")
GRADES = ("keep", "state", "junk")
FIELDS = {"what": "What", "why": "Why", "so_what": "So what", "event": "Event",
          "learned_from": "Learned from", "scope": "Scope", "applies_when": "Applies when"}


class InvalidOutput(ValueError):
    """A model's output file does not satisfy its contract; redo the job."""


def _field(body: str, label: str) -> str | None:
    m = re.search(rf"^\*\*{re.escape(label)}:\*\*[ \t]*(.*?)(?=^\*\*[A-Z][^*\n]*:\*\*|\Z)",
                  body, re.M | re.S)
    return m.group(1).strip() if m else None


def parse_extract(text: str) -> list[dict]:
    """Items from one extractor's Markdown. ``NO ITEMS`` is a valid empty result."""
    if not text.strip():
        raise InvalidOutput("empty extractor output")
    parts = re.split(r"(?m)^## ", text)
    items = []
    for part in parts[1:]:
        heading, _, body = part.partition("\n")
        kind, _, title = heading.partition(":")
        item = {"kind": kind.strip() if title else "", "title": (title or kind).strip(),
                "markdown": "## " + part.strip()}
        for key, label in FIELDS.items():
            item[key] = _field(body, label)
        empty = [FIELDS[k] for k in FIELDS if not item[k]]
        if empty or not item["title"]:
            raise InvalidOutput(f"item '{item['title'][:40]}' lacks {', '.join(empty) or 'a title'}")
        items.append(item)
    if not items and text.strip() != "NO ITEMS":
        raise InvalidOutput("no '## ' item sections and not 'NO ITEMS'")
    return items


def event_number(value: str | None) -> int | None:
    m = re.search(r"\d+", value or "")
    return int(m.group(0)) if m else None


def learned(value: str | None) -> str | None:
    v = (value or "").strip().strip("`").lower()
    for name in LEARNED_FROM:
        if v.startswith(name):
            return name
    return None


def blind_lists(extracts: dict[str, list[dict]], seed: str) -> tuple[dict, dict]:
    """Assign the two extractors to A and B at random and id their items.

    Returns (side -> extractor name, item id -> (extractor, index)).
    """
    names = sorted(extracts)
    if len(names) != 2:
        raise ValueError("the judge pairs exactly two extractors")
    random.Random(seed).shuffle(names)
    sides = {"A": names[0], "B": names[1]}
    ids = {}
    for side, name in sides.items():
        for i, _ in enumerate(extracts[name]):
            ids[f"{side}{i + 1:02d}"] = (name, i)
    return sides, ids


def judge_input(extracts: dict[str, list[dict]], sides: dict) -> dict[str, str]:
    """JSON-lines text of list A and list B."""
    out = {}
    for side, name in sides.items():
        out[side] = "".join(json.dumps({"id": f"{side}{i + 1:02d}", "text": it["markdown"]},
                                       ensure_ascii=False) + "\n"
                            for i, it in enumerate(extracts[name]))
    return out


def parse_judge(text: str, ids: dict) -> list[dict]:
    """Judge groups, checked: every item id in exactly one group, fields valid."""
    groups, seen = [], set()
    for n, line in enumerate(text.splitlines(), 1):
        line = line.strip()
        if not line or line.startswith("```"):
            continue
        try:
            g = json.loads(line)
        except ValueError as exc:
            raise InvalidOutput(f"judge line {n} is not JSON") from exc
        members = list(g.get("a") or []) + list(g.get("b") or [])
        if not members:
            raise InvalidOutput(f"judge line {n} has no items")
        for m in members:
            if m not in ids:
                raise InvalidOutput(f"judge line {n} names unknown id {m}")
            if m in seen:
                raise InvalidOutput(f"id {m} is in two groups")
            seen.add(m)
        if g.get("grade") not in GRADES:
            raise InvalidOutput(f"judge line {n} has grade {g.get('grade')!r}")
        if learned(g.get("learned_from")) is None:
            raise InvalidOutput(f"judge line {n} has learned_from {g.get('learned_from')!r}")
        if g.get("best") not in members:
            g["best"] = members[0]
        groups.append(g)
    missing = sorted(set(ids) - seen)
    if missing:
        raise InvalidOutput(f"judge left {len(missing)} items ungrouped: {', '.join(missing[:5])}")
    return groups


def merge(groups: list[dict], ids: dict, extracts: dict[str, list[dict]]) -> list[dict]:
    """One lesson per judge group, from its best item, with ``found_by``."""
    lessons = []
    for g in groups:
        members = list(g.get("a") or []) + list(g.get("b") or [])
        name, idx = ids[g["best"]]
        best = extracts[name][idx]
        found_by = sorted({ids[m][0] for m in members})
        lessons.append({
            "item": best, "best_from": name, "found_by": found_by,
            "members": [{"extractor": ids[m][0], "index": ids[m][1]} for m in members],
            "grade": g["grade"], "unsupported": bool(g.get("unsupported")),
            "learned_from": learned(g.get("learned_from")),
            "reason": g.get("reason"),
        })
    return lessons
