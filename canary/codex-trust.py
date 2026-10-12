#!/usr/bin/env python3
"""Trust the canary hooks in a temporary CODEX_HOME.

    codex-trust.py <codex-home> <workspace>

Asks `codex app-server` (hooks/list) for each hook's key and current hash and
appends `[hooks.state."<key>"] trusted_hash` to <codex-home>/config.toml.
Only hooks whose source file is <codex-home>/hooks.json are trusted.
"""
import json
import os
import subprocess
import sys

home, ws = os.path.abspath(sys.argv[1]), os.path.abspath(sys.argv[2])
proc = subprocess.Popen(["codex", "app-server"], stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                        env={**os.environ, "CODEX_HOME": home}, text=True)


def call(msg):
    proc.stdin.write(json.dumps(msg) + "\n")
    proc.stdin.flush()
    if "id" not in msg:
        return None
    for line in proc.stdout:
        reply = json.loads(line)
        if reply.get("id") == msg["id"]:
            if "error" in reply:
                sys.exit(f"{msg['method']}: {reply['error']}")
            return reply["result"]
    sys.exit("app-server closed")


call({"id": 1, "method": "initialize", "params": {"clientInfo": {"name": "contextmd-canary", "version": "0"}}})
call({"method": "initialized"})
result = call({"id": 2, "method": "hooks/list", "params": {"cwds": [ws]}})
proc.terminate()

hooks_file = os.path.join(home, "hooks.json")
lines = []
for entry in result["data"]:
    for hook in entry.get("hooks", []):
        if os.path.realpath(hook["sourcePath"]) == os.path.realpath(hooks_file):
            lines.append(f'\n[hooks.state."{hook["key"]}"]\ntrusted_hash = "{hook["currentHash"]}"\n')
if not lines:
    sys.exit(f"no hooks from {hooks_file} in hooks/list: {json.dumps(result)[:500]}")
with open(os.path.join(home, "config.toml"), "a") as fh:
    fh.writelines(lines)
print(f"trusted {len(lines)} hooks")
