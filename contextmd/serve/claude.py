"""Claude Code adapter: hook event JSON in, hook output JSON out.

Seams proven by the canary (#5): ``UserPromptSubmit``, ``PostToolUse`` and
``PostToolUseFailure`` context, the ``PreToolUse`` deny reason and the
``Stop`` block reason are all read before the model's next action;
``SessionStart`` context is the session-start pack.
"""

from __future__ import annotations

import json

from .server import Context, Deny, Moment, Nothing, Reply

HOST = "claude-code"
EVENTS = {
    "SessionStart": "session_start",
    "UserPromptSubmit": "user_message",
    "PreToolUse": "pre_tool",
    "PostToolUse": "tool_result",
    "PostToolUseFailure": "tool_failure",
    "Stop": "before_done",
}


def moment(event: dict) -> Moment:
    name = event.get("hook_event_name")
    if name not in EVENTS:
        raise ValueError(f"unsupported Claude Code event {name!r}")
    kind = EVENTS[name]
    extra = {}
    if event.get("tool_use_id"):
        extra["moment_id"] = f"{kind}:{event['tool_use_id']}"
    error = event.get("error")
    return Moment(host=HOST, session=str(event.get("session_id") or ""), kind=kind,
                  cwd=event.get("cwd"), tool=event.get("tool_name"),
                  tool_input=event.get("tool_input"),
                  error=error if isinstance(error, str) else (json.dumps(error) if error else None),
                  source=event.get("source"), **extra)


def render(event: dict, reply: Reply) -> str:
    name = event.get("hook_event_name")
    match reply:
        case Nothing():
            return ""
        case Deny(reason=reason):
            return json.dumps({"hookSpecificOutput": {
                "hookEventName": name, "permissionDecision": "deny",
                "permissionDecisionReason": reason}})
        case Context(text=text):
            return json.dumps({"hookSpecificOutput": {"hookEventName": name, "additionalContext": text}})
    raise TypeError(reply)


def hooks(command: str) -> dict:
    """The ``hooks`` block of a Claude Code settings file that forwards every moment."""
    group = [{"hooks": [{"type": "command", "command": command, "timeout": 5}]}]
    return {"hooks": {name: group for name in EVENTS}}
