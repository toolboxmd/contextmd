#!/usr/bin/env bash
# Write one host's canary hooks into a throwaway workspace.
#   setup.sh claude|grok|opencode <workspace>
#   setup.sh codex <workspace> <codex-home>   (hooks go into the temp CODEX_HOME)
# The log is <workspace>/.canary/log.jsonl.
set -euo pipefail
host=$1 ws=$(cd "$2" && pwd) here=$(cd "$(dirname "$0")" && pwd)
log="$ws/.canary/log.jsonl"
mkdir -p "$ws/.canary"

hooks_json() {
  local cmd="python3 $here/canary-hook.py --host $host --log $log${2:+ $2}" events=$1 out="" sep=""
  for e in $events; do
    out+="$sep\"$e\":[{\"hooks\":[{\"type\":\"command\",\"command\":\"$cmd\",\"timeout\":5}]}]"
    sep=","
  done
  printf '{"hooks":{%s}}\n' "$out"
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
    sed "s|__LOG__|$log|" "$here/opencode-canary.js" >"$ws/.opencode/plugin/canary.js" ;;
  *) echo "unknown host: $host" >&2; exit 2 ;;
esac
echo "canary hooks for $host written; log: $log"
