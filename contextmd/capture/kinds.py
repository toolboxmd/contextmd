"""Thread kinds, and which kinds capture skips.

Capture skips two kinds (contextmd#2, user decision 2026-10-12):

- worker-batch: parallel workers started on one shared brief. Their items
  mostly restate the brief.
- capture: capture's own extract and judge threads, so capture never reads
  its own output.
"""

from __future__ import annotations

import re
from collections import Counter

from .chromeria import Thread

SKIPPED = ("worker-batch", "capture")

# Trailing short segments that number parallel siblings: final-v3-a-0 -> final-v3.
_SIBLING_SUFFIX = re.compile(r"(?:-[A-Za-z0-9]{1,2})+$")
_REVIEW = re.compile(r"review|audit|check|verif|roast|critique", re.I)
_RESEARCH = re.compile(r"research|council|recheck|investigat|survey", re.I)


def sibling_stem(name: str) -> str:
    return _SIBLING_SUFFIX.sub("", name) or name


def classify(threads: dict[str, Thread], own_prefix: str,
             batch_min_siblings: int) -> dict[str, str]:
    """Kind of every thread in ``threads``.

    Worker-batch membership is decided against all threads given, so pass the
    whole inventory, not only the threads being captured.
    """
    stems = Counter()
    for t in threads.values():
        if t.task_name and t.parent_id:
            stems[(t.parent_id, sibling_stem(t.task_name))] += 1
    kinds = {}
    for tid, t in threads.items():
        name = t.task_name or ""
        if name.startswith(own_prefix) or t.title.startswith("contextmd capture"):
            kind = "capture"
        elif t.task_name and t.parent_id and \
                stems[(t.parent_id, sibling_stem(name))] >= batch_min_siblings:
            kind = "worker-batch"
        elif "spectrum." in tid:
            kind = "spectrum"
        elif t.created_by == "user" or not t.parent_id:
            kind = "user-started"
        elif _RESEARCH.search(t.title) or _RESEARCH.search(name):
            kind = "research"
        elif _REVIEW.search(t.title) or _REVIEW.search(name):
            kind = "reviewer"
        else:
            kind = "worker"
        kinds[tid] = kind
    return kinds
