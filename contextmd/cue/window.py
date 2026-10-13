"""The Cue window: the transcript text Cue scores, built exactly as its training windows were.

The deployed adapter was trained on the ``raw10k`` format: the intent pilot's ``code/windows_v2.build`` (one
10,000-character budget over the last earlier user messages, the relays and agent replies since the previous user
message, the agent's previous reply and its plan, oldest trimmed first, the current message cut from its start down
to its last 2,500 characters), rendered as ``loop/student/winfmt.py render`` (judge ``--windows v3``, ``--context
none``). ``build`` and ``render`` below are that code, ported unchanged apart from items being ``(kind, text)``
pairs and malformed citation markup being left as text; ``tests/test_cue.py`` pins it with literal windows, and
it reproduces all 2,305 training windows byte for byte. One difference stays: training counted the agent's plan
(its text after the message) against the budget, which serving cannot see yet, so about 15% of live windows are
trimmed less than their training counterparts. The format belongs to the adapter, so it lives
with the Cue service and changes only with a retrained adapter (#4).

Items are the thread's messages in time order: ``("user", text)``, ``("asst", text)`` or ``("tool", text)``. A
message moment scores the window at the last user message. A tool moment (before a call, or a failed call) is
outside the training data: it scores the same window plus the agent's work since that message, the proposed call
and, after a failure, the end of its output. The moment kind is logged with every score, so #4 can measure it.
"""

from __future__ import annotations

import json
import re
import urllib.parse

RELAY = re.compile(r"^\[(Message from|Subagent|Assistant quote\]\(t3-citation)|^Answer independently")
BUDGET, KEEP_CUR = 10000, 2500
CIT_BLOCK = re.compile(r"<assistant_citations>(.*?)</assistant_citations>", re.S)
CIT_LINK = re.compile(r"\[Assistant quote\]\((t3-citation://[^\s)]*)\)")
MARK = "[...]"
# What a tool moment adds after the trained window, in characters.
WORK_CHARS, CALL_CHARS, OUTPUT_CHARS = 2500, 1000, 1000


def quote(text, qcap):
    text = (text or "").strip()
    if qcap is not None and len(text) > qcap:
        text = text[:qcap] + MARK
    return f'QUOTED REPLY: "{text}"' if text else ""


def cite(text, comment, qcap):
    return "\n".join(x for x in (quote(text, qcap), f"USER COMMENT: {comment.strip()}" if comment else "") if x)


def render_citations(t, qcap=None):
    """Replace citation markup with QUOTED REPLY / USER COMMENT lines. qcap shortens quoted text only."""
    cits = []

    def block(m):
        body = m.group(1)
        try:
            parsed = json.loads(body[body.index("["):body.rindex("]") + 1])
        except ValueError:
            return m.group(0)
        if not isinstance(parsed, list) or not all(isinstance(c, dict) for c in parsed):
            return m.group(0)  # markup in a shape the training data never had: keep it as text
        cits.append([(c.get("id"), c.get("citation") or c) for c in parsed])
        return f"\x00{len(cits) - 1}\x00"

    out = CIT_BLOCK.sub(block, t)
    for n, group in enumerate(cits):
        unplaced = []
        for cid, c in group:
            if not isinstance(c, dict):
                c = {}
            r = cite(c.get("text"), c.get("comment"), qcap)
            if cid and f"[{cid}]" in out:
                out = out.replace(f"[{cid}]", "\n" + r + "\n", 1)
            else:
                unplaced.append(r)
        out = out.replace(f"\x00{n}\x00", "\n".join(unplaced))

    def link(m):
        q = urllib.parse.parse_qs(urllib.parse.urlsplit(m.group(1)).query)
        return "\n[Assistant quote]\n" + cite(q.get("text", [""])[0], q.get("comment", [""])[0], qcap) + "\n"
    out = CIT_LINK.sub(link, out)
    return re.sub(r"\n{3,}", "\n\n", re.sub(r"[ \t]+\n", "\n", out)).strip()


def head(t, n):
    return t if len(t) <= n else (t[:max(n - len(MARK), 0)] + MARK if n > len(MARK) else "")


def tail(t, n):
    return t if len(t) <= n else (MARK + t[-(n - len(MARK)):] if n > len(MARK) else "")


def middle(t, n):
    if len(t) <= n:
        return t
    if n <= len(MARK):
        return ""
    h = (n - len(MARK)) * 3 // 5
    return t[:h] + MARK + t[len(t) - (n - len(MARK) - h):]


class Seg:
    """One budgeted text. raw is the source message, kind picks how it shrinks."""

    def __init__(self, raw, kind):
        self.raw, self.kind, self.limit, self.qcap = raw, kind, None, None
        self.cut = False

    @property
    def text(self):
        t = render_citations(self.raw, self.qcap) if self.kind in ("user", "cur", "relay") else self.raw
        if self.limit is None or len(t) <= self.limit:
            return t
        f = {"user": middle, "relay": head, "agent": tail, "prev": tail, "plan": head, "cur": tail}[self.kind]
        return f(t, self.limit)

    def shrink(self, need, floor):
        """Remove up to `need` characters without going below `floor`. Quoted text goes first."""
        before = len(self.text)
        for qcap in (200, 0):
            if self.kind in ("plan", "prev", "agent") or len(self.text) <= floor or before - len(self.text) >= need:
                break
            if "QUOTED REPLY" in self.text and (self.qcap is None or self.qcap > qcap):
                self.qcap = qcap
                self.cut = True
        now = len(self.text)
        left = need - (before - now)
        if left > 0 and now > floor:
            self.limit = max(floor, now - left)
            self.cut = True
        return before - len(self.text)


def prev_at(it, i):
    """How many of the earlier non-relay user messages of build(it, i) precede the agent's latest reply."""
    u = [j for j in range(i) if it[j][0] == "user" and not RELAY.match(it[j][1])][-2:]
    e = i - 1
    while e >= 0 and it[e][0] != "asst":
        e -= 1
    return sum(j < e for j in u) if e >= 0 else len(u)


def build(it, i):
    """The window fields at user message ``it[i]`` (``windows_v2.build`` without the labeler-only ``after``)."""
    users = [x[1] for x in it[:i] if x[0] == "user" and not RELAY.match(x[1])][-2:]
    k = i - 1
    while k >= 0 and not (it[k][0] == "user" and not RELAY.match(it[k][1])):
        k -= 1
    context = []
    for kind, t in it[k + 1:i]:
        if kind == "user":
            context.append(["relay", t])
        elif kind == "asst":
            if context and context[-1][0] == "agent":
                context[-1][1] += "\n" + t
            else:
                context.append(["agent", t])
    e = i - 1
    while e >= 0 and it[e][0] != "asst":
        e -= 1
    s = e
    while s >= 0 and it[s][0] != "user":
        s -= 1
    prev = "\n".join(x[1] for x in it[s + 1:e + 1] if x[0] == "asst")
    plan, j = [], i + 1
    while j < len(it) and it[j][0] == "asst":
        plan.append(it[j][1])
        j += 1
    useg = [Seg(u, "user") for u in users]
    cseg = [Seg(t, kind) for kind, t in context]
    agents = [c for c in cseg if c.kind == "agent"]
    if agents:  # the agent's latest reply is the last agent item of the span
        pseg = agents[-1]
        pseg.kind = "prev"
    else:  # the agent wrote nothing since the previous user message: prev precedes it
        pseg = Seg(prev, "prev")
    plseg, cur = Seg("\n".join(plan), "plan"), Seg(it[i][1], "cur")
    segs = useg + cseg + ([] if agents else [pseg]) + [plseg, cur]
    order = useg + [c for c in cseg if c is not pseg] + [pseg, plseg, cur]
    for floors in ({"user": 300, "relay": 300, "agent": 300, "prev": 1500, "plan": 1500, "cur": KEEP_CUR},
                   {"user": 0, "relay": 0, "agent": 0, "prev": 0, "plan": 0, "cur": KEEP_CUR}):
        for x in order:
            need = sum(len(y.text) for y in segs) - BUDGET
            if need <= 0:
                break
            x.shrink(need, floors[x.kind])
    return {
        "users": [u.text for u in useg] + [cur.text],
        "prev": pseg.text,
        "context": [{"kind": "relay" if c.kind == "relay" else "agent", "text": c.text} for c in cseg],
        "prev_at": prev_at(it, i),
        "cut": [x.kind for x in segs if x.cut],
    }


def render(w):
    """judge.py window_text with --windows v3, --context none, --citations raw (no opening, no plan)."""
    users = w["users"]
    us = [f"## {'USER MESSAGE (current turn)' if k == len(users) - 1 else f'EARLIER USER MESSAGE {k + 1}'}\n\n{u}"
          for k, u in enumerate(users)]
    ctx = w["context"]
    items = [f"## AGENT'S REPLY\n\n{c['text']}" if c["kind"] == "agent"
             else f"## RELAYED MESSAGE (another agent or a quote)\n\n{c['text']}" for c in ctx]
    for k, it in enumerate(items):
        us.insert(len(users) - 1 + k, it)
    if not any(c["kind"] == "agent" for c in ctx):
        us.insert(w.get("prev_at", len(users) - 1), f"## AGENT'S PREVIOUS REPLY\n\n{w.get('prev') or '(none)'}")
    return "\n\n".join(us)


class NoMessage(ValueError):
    """The items hold no user message to score."""


def window(items, kind="user_message", call=None, output=None):
    """The text Cue scores for one moment.

    ``items``: ``[(kind, text), ...]`` in time order. ``kind``: the moment kind. ``call``: the tool call the moment
    is about (``pre_tool``, ``tool_failure``). ``output``: the failed call's output (``tool_failure``).
    """
    items = [(k, t if isinstance(t, str) else "") for k, t in items if k in ("user", "asst", "tool")]
    users = [n for n, (k, _) in enumerate(items) if k == "user"]
    if not users:
        raise NoMessage("no user message")
    i = users[-1]
    text = render(build(items[:i + 1], i))
    if kind not in ("pre_tool", "tool_result", "tool_failure"):
        return text
    work = "\n".join(t if k == "asst" else f"Tool call: {t}" for k, t in items[i + 1:])
    parts = [text]
    if work:
        parts.append(f"## AGENT'S WORK THIS TURN (latest last)\n\n{tail(work, WORK_CHARS)}")
    if call and kind == "pre_tool":
        parts.append(f"## AGENT'S NEXT TOOL CALL\n\n{head(call, CALL_CHARS)}")
    if call and kind == "tool_failure":
        parts.append(f"## TOOL CALL THAT FAILED\n\n{head(call, CALL_CHARS)}\n\nEnd of its output:\n"
                     f"{tail(output or '', OUTPUT_CHARS)}")
    return "\n\n".join(parts)
