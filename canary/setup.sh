#!/usr/bin/env bash
# Write one host's canary hooks into a throwaway workspace.
#   setup.sh claude|grok|opencode <workspace>
#   setup.sh codex <workspace> <codex-home>   (hooks go into the temp CODEX_HOME)
# The log is <workspace>/.canary/log.jsonl.
set -euo pipefail
host=$1 ws=$(cd "$2" && pwd) here=$(cd "$(dirname "$0")" && pwd)
log="$ws/.canary/log.jsonl"
mkdir -p "$ws/.canary"

# hooks_json "<events>" [extra hook flag]: paths are shell-quoted in the
# command and JSON-escaped by Python, so spaces and quotes are safe.
hooks_json() {
  python3 - "$here/canary-hook.py" "$host" "$log" "$1" "${2:-}" <<'EOF'
import json, shlex, sys
hook, host, log, events, extra = sys.argv[1:]
cmd = " ".join(["python3", shlex.quote(hook), "--host", host, "--log", shlex.quote(log)] + ([extra] if extra else []))
group = [{"hooks": [{"type": "command", "command": cmd, "timeout": 5}]}]
print(json.dumps({"hooks": {event: group for event in events.split()}}))
EOF
}

case $host in
  claude)
    mkdir -p "$ws/.claude"
    hooks_json "UserPromptSubmit PreToolUse PostToolUse PostToolUseFailure Stop" >"$ws/.claude/settings.json" ;;
  grok)
    # Grok loads project hooks only inside a git repository.
    [ -d "$ws/.git" ] || git -C "$ws" init -q
    mkdir -p "$ws/.grok/hooks"
    hooks_json "UserPromptSubmit PreToolUse PostToolUse PostToolUseFailure Stop" --pre-context >"$ws/.grok/hooks/canary.json" ;;
  codex)
    home=$3
    mkdir -p "$home"
    hooks_json "UserPromptSubmit PreToolUse PostToolUse Stop" >"$home/hooks.json" ;;
  opencode)
    mkdir -p "$ws/.opencode/plugin"
    python3 - "$here/opencode-canary.js" "$log" >"$ws/.opencode/plugin/canary.js" <<'EOF'
import json, sys
src, log = sys.argv[1:]
sys.stdout.write(open(src).read().replace('"__LOG__"', json.dumps(log)))
EOF
    ;;
  *) echo "unknown host: $host" >&2; exit 2 ;;
esac
echo "canary hooks for $host written; log: $log"
