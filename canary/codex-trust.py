#!/usr/bin/env python3
"""Trust the canary hooks in a temporary CODEX_HOME.

    codex-trust.py <codex-home> <workspace>

Asks `codex app-server` (hooks/list) for each hook's key and current hash and
appends `[hooks.state."<key>"] trusted_hash` to <codex-home>/config.toml.
Only hooks whose source file is <codex-home>/hooks.json are trusted. Keys
already present in config.toml are left alone, so a rerun is safe.
"""
import json
import os
import subprocess
import sys

home, ws = os.path.abspath(sys.argv[1]), os.path.abspath(sys.argv[2])
proc = subprocess.Popen(["codex", "app-server"], stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                        env={**os.environ, "CODEX_HOME": home}, text=True)


def fail(message):
    proc.terminate()
    sys.exit(message)


def call(msg):
    proc.stdin.write(json.dumps(msg) + "\n")
    proc.stdin.flush()
    if "id" not in msg:
        return None
    for line in proc.stdout:
        reply = json.loads(line)
        if reply.get("id") == msg["id"]:
            if "error" in reply:
                fail(f"{msg['method']}: {reply['error']}")
            return reply.get("result") or {}
    fail("app-server closed")


call({"id": 1, "method": "initialize", "params": {"clientInfo": {"name": "contextmd-canary", "version": "0"}}})
call({"method": "initialized"})
result = call({"id": 2, "method": "hooks/list", "params": {"cwds": [ws]}})
proc.terminate()

config = os.path.join(home, "config.toml")
existing = open(config).read() if os.path.exists(config) else ""
hooks_file = os.path.realpath(os.path.join(home, "hooks.json"))
lines, found = [], 0
for entry in result.get("data", []):
    for hook in entry.get("hooks", []):
        if os.path.realpath(hook["sourcePath"]) != hooks_file:
            continue
        found += 1
        # json.dumps gives a valid TOML basic string for keys and hashes.
        table = f'[hooks.state.{json.dumps(hook["key"])}]'
        if table not in existing:
            lines.append(f'\n{table}\ntrusted_hash = {json.dumps(hook["currentHash"])}\n')
if not found:
    sys.exit(f"no hooks from {hooks_file} in hooks/list: {json.dumps(result)[:500]}")
with open(config, "a") as fh:
    fh.writelines(lines)
print(f"trusted {len(lines)} new hooks ({found - len(lines)} already trusted)")
