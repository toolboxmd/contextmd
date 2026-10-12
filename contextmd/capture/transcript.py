"""Render Chromeria turn items into transcript lines and cut them into segments.

Line format, shared with the extract and judge prompts:
``[n] kind timestamp: text``. Kinds: user (typed by the person), brief (a
message an agent sent, such as a delegated task brief), assistant, call,
output, error. Reasoning items are dropped. Clip lengths follow the capture
pilot so results stay comparable.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass

from .chromeria import Item

CLIP = {"user": 4000, "brief": 4000, "assistant": 2000, "call": 600,
        "output": 300, "error": 1200}
_ERR = re.compile(r"(Traceback|Error|error:|ERROR|FAIL|failed|fatal:|exit code [1-9]|"
                  r"Exit code [1-9]|not found|denied|No such file)")


@dataclass
class Line:
    kind: str
    ts: str
    text: str
    item_id: str
    ordinal: int

    def render(self, n: int) -> str:
        return f"[{n}] {self.kind} {self.ts[:19]}: {self.text}"


def _clip(value, kind: str) -> str:
    s = value if isinstance(value, str) else json.dumps(value, ensure_ascii=False)
    n = CLIP[kind]
    return s if len(s) <= n else s[:n] + " [...]"


def _output_line(item: Item, out, ts: str) -> "Line | None":
    if out is None or out == "":
        return None
    o = out if isinstance(out, str) else json.dumps(out, ensure_ascii=False)
    failed = item.payload.get("status") == "failed" or _ERR.search(o[:3000])
    kind = "error" if failed else "output"
    return Line(kind, ts, _clip(o, kind), item.item_id, item.ordinal)


def lines_of(item: Item) -> list[Line]:
    p, ts = item.payload, item.ts
    ty = item.type or p.get("type")
    mk = lambda kind, text: Line(kind, ts, _clip(text, kind), item.item_id, item.ordinal)
    if ty == "user_message":
        kind = "user" if p.get("createdBy") == "user" else "brief"
        return [mk(kind, p.get("text") or "")]
    if ty == "assistant_message":
        text = p.get("text") or ""
        return [mk("assistant", text)] if text else []
    if ty == "command_execution":
        out = [mk("call", f"exec: {p.get('input')}")]
        o = _output_line(item, p.get("output"), ts)
        return out + ([o] if o else [])
    if ty == "dynamic_tool":
        out = [mk("call", f"{p.get('toolName')}: {json.dumps(p.get('input'), ensure_ascii=False)}")]
        o = _output_line(item, p.get("output"), ts)
        return out + ([o] if o else [])
    if ty == "file_change":
        return [mk("call", f"edit {p.get('fileName')}: {p.get('diffStr') or ''}")]
    if ty == "subagent":
        return [mk("call", f"subagent: {p.get('title') or ''} {p.get('prompt') or p.get('input') or ''}")]
    return []


def render(items: list[Item]) -> list[Line]:
    out = []
    for it in items:
        out.extend(lines_of(it))
    return out


def size(lines: list[Line]) -> int:
    return sum(len(l.text) + 32 for l in lines)


def segments(lines: list[Line], max_chars: int) -> list[list[Line]]:
    """Cut lines into consecutive segments of at most ``max_chars``.

    Cuts fall between turn items, never inside one. Once a segment is 70%
    full, the next user or brief message starts a new one, so a person's
    message and the work it caused tend to stay together. A single item
    longer than ``max_chars`` becomes its own segment.
    """
    if max_chars <= 0:
        return [lines] if lines else []
    out: list[list[Line]] = []
    cur: list[Line] = []
    cur_size = 0
    i = 0
    while i < len(lines):
        j = i
        while j < len(lines) and lines[j].item_id == lines[i].item_id:
            j += 1
        group = lines[i:j]
        g = size(group)
        starts_turn = group[0].kind in ("user", "brief")
        if cur and (cur_size + g > max_chars or
                    (starts_turn and cur_size >= 0.7 * max_chars)):
            out.append(cur)
            cur, cur_size = [], 0
        cur.extend(group)
        cur_size += g
        i = j
    if cur:
        out.append(cur)
    return out


def context_before(lines: list[Line], max_chars: int) -> list[Line]:
    """The last whole lines whose size fits ``max_chars``."""
    out: list[Line] = []
    total = 0
    for line in reversed(lines):
        s = len(line.text) + 32
        if total + s > max_chars:
            break
        out.append(line)
        total += s
    return list(reversed(out))


def document(header: str, context: list[Line], body: list[Line]) -> tuple[str, dict[int, Line]]:
    """The transcript file text and line number -> Line for the body lines.

    Context lines are numbered too, but they are outside this capture and are not
    in the returned map, so an item citing one is rejected.
    """
    parts = [header]
    n = 0
    if context:
        parts.append("## CONTEXT (earlier events, outside this capture: read for meaning, do not extract from these lines)")
        for line in context:
            n += 1
            parts.append(line.render(n))
        parts.append("## EVENTS (extract from these lines only)")
    numbered = {}
    for line in body:
        n += 1
        numbered[n] = line
        parts.append(line.render(n))
    return "\n".join(parts) + "\n", numbered
