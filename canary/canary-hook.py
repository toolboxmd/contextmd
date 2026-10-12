#!/usr/bin/env python3
"""Seam canary for command hooks (Claude Code, Codex, Grok Build).

Reads one hook event on stdin, serves a fresh canary token at that seam, and
appends one JSON line per fire to the log. Usage:

    canary-hook.py --host <name> --log <path>

Seams: UserPromptSubmit and PostToolUse/PostToolUseFailure add context;
PreToolUse denies the first command containing `canary-deny` per session;
Stop blocks the first stop per session. A command containing `canary-big`
gets a context payload over 10,000 characters with a token at each end.
With --pre-context, other canary commands also get PreToolUse context
(Grok drops PostToolUse context and delivers PreToolUse context with the
tool result instead).
"""
import argparse
import json
import os
import secrets
import sys
import time

BIG_SIZE = 12_000


def token(seam):
    return f"CMDCANARY-{seam}-{secrets.token_hex(2).upper()}"


def served(tok):
    return f"contextmd canary {tok}. Quote the token {tok} in your next message before any other action."


def big_payload(head, tail):
    lines, n = [served(head)], 0
    while sum(len(line) + 1 for line in lines) < BIG_SIZE - 200:
        n += 1
        lines.append(f"canary filler line {n:04d}: this text only pads the payload past the host limit.")
    lines.append(served(tail))
    return "\n".join(lines)


def once(log, kind, session):
    flag = f"{log}.{kind}-{session}"
    if os.path.exists(flag):
        return False
    open(flag, "w").close()
    return True


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--host", required=True)
    ap.add_argument("--log", required=True)
    ap.add_argument("--pre-context", action="store_true")
    args = ap.parse_args()
    t0 = time.time()
    ev = json.load(sys.stdin)
    event = ev.get("hook_event_name") or ev.get("hookEventName") or ""
    # Grok sends snake_case names (pre_tool_use); serve on the same seam.
    event = "".join(part[:1].upper() + part[1:] for part in event.split("_"))
    session = str(ev.get("session_id") or ev.get("sessionId") or "s")
    tool = str(ev.get("tool_name") or ev.get("toolName") or "")
    tool_input = json.dumps(ev.get("tool_input") or ev.get("toolInput") or {})
    out, toks = None, []

    def context(text):
        return {"hookSpecificOutput": {"hookEventName": event, "additionalContext": text}}

    if event == "UserPromptSubmit":
        toks = [token("PROMPT")]
        out = context(served(toks[0]))
    elif event == "PreToolUse" and "canary-deny" in tool_input and once(args.log, "deny", session):
        toks = [token("DENY")]
        out = {"hookSpecificOutput": {"hookEventName": event, "permissionDecision": "deny",
                                      "permissionDecisionReason": served(toks[0]) + " Then run the original command again."}}
    elif event in ("PostToolUse", "PostToolUseFailure") and "canary" in tool_input or (
            event == "PreToolUse" and args.pre_context and "canary" in tool_input):
        seam = {"PostToolUse": "RESULT", "PostToolUseFailure": "FAILURE"}.get(event, "PRECTX")
        if "canary-big" in tool_input:
            toks = [token("BIGHEAD"), token("BIGTAIL")]
            out = context(big_payload(*toks))
        else:
            toks = [token(seam)]
            out = context(served(toks[0]))
    elif event == "Stop" and once(args.log, "stop", session):
        toks = [token("DONE")]
        out = {"decision": "block", "reason": served(toks[0])}

    body = json.dumps(out) if out else ""
    with open(args.log, "a") as fh:
        fh.write(json.dumps({"host": args.host, "event": event, "session": session, "tool": tool,
                             "input": tool_input[:200], "tokens": toks, "chars": len(body),
                             "t_start": t0, "t_end": time.time()}) + "\n")
    sys.stdout.write(body)


if __name__ == "__main__":
    main()
