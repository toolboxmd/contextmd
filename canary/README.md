# Seam canary

The canary shows which served text each agent host reads before its next
action. A hook serves a fresh token (`CMDCANARY-<SEAM>-<hex>`) at one seam, and
the prompt asks the model to quote every token it sees before its next tool
call. A seam counts as delivered only when the model quotes that token before
its next action. Serving (#3) builds only on seams this table proves.

## Result (2026-10-12)

All runs ran inside Chromeria 0.0.45 on macOS, one ordinary model per host:
Claude Code 2.1.292 with `claude-haiku-4-5`, Codex 0.160.1 with `gpt-6-astra`
(low effort), Grok Build 1.0.13 with `grok-build`, and OpenCode 1.18.31 with
`opencode/big-pickle`. Each cell names the mechanism and the result. The time in
parentheses runs from the hook's exit to the model message that quotes the
token, so it is mostly model latency.

| Host | User message | Tool result | Tool failure | Pre-tool deny | Before done |
| --- | --- | --- | --- | --- | --- |
| Claude Code | `UserPromptSubmit` context: delivered before (3.5 s) | `PostToolUse` context: delivered before (1.3 s) | `PostToolUseFailure` context: delivered before (1.5 s) | deny reason: delivered before (1.9 s) | `Stop` block reason: delivered before (1.4 s) |
| Codex | `UserPromptSubmit` context, as a developer message: delivered before (2.5 s) | `PostToolUse` context, as a developer message after the output: delivered before (2.7 s) | no separate event; a non-zero exit fires `PostToolUse`: delivered before (3.0 s) | deny reason: delivered before (2.5 s) | `Stop` block reason, as a hook prompt: delivered before (2.5 s) |
| Grok Build | `UserPromptSubmit` context: discarded | `PostToolUse` context: discarded. `PreToolUse` context arrives with the result: delivered before (3.0 s) | a non-zero exit fires `PostToolUse`, not `PostToolUseFailure`. `PreToolUse` context: delivered before (4.8 s) | deny reason: delivered before (2.2 s) | `Stop` block reason: delivered before (4.2 s) |
| OpenCode | text part added in `experimental.chat.messages.transform`: delivered before (5.4 s) | text appended to the output in `tool.execute.after`: delivered before (2.7 s) | a non-zero exit still runs `tool.execute.after`: delivered before (2.4 s) | error thrown in `tool.execute.before`: delivered before (2.8 s) | no seam: the plugin API has no stop hook |

"Discarded" means the token never entered the host's session history and the
model answered "no canary". The Grok prompt-hook result matches Grok's hook
guide ("stdout of an allowing hook is discarded"). The Grok `PostToolUse`
result held in both the Chromeria run and a direct `grok -p` run.

Serving adds no model round trip at the context seams. The Python hook took
about 20 ms per fire: the median wall time of 20 `/usr/bin/time` runs of the
whole process, Python startup included. A deny and a before-done block each cost one
extra model round.

### Size limits

The tool-result seam also served one payload of about 12,000 characters with a
token at each end.

| Host | Behavior over the limit |
| --- | --- |
| Claude Code | The 11.7 KB context was saved to a file. The model saw a 2 KB preview and the file path, so it quoted only the head token. The exact threshold was not measured. |
| Codex | Context of 2,988 tokens was cut in the middle with `Warning: truncated output (original token count: 2988)`. The head and tail tokens both arrived. Codex's app-server protocol documents the default `additionalContext` spill threshold as 2,500 tokens (`HookMetadata.additional_context_limit`). |
| Grok Build | The context was clipped at 10,000 characters. The tail token was lost. |
| OpenCode | All 11,951 appended characters arrived. No limit was seen at this size. |

### Findings that change the serving design

- Grok Build drops `additionalContext` from `PostToolUse` and from
  `UserPromptSubmit`. Serve Grok's tool-result text as `PreToolUse`
  `additionalContext`, which arrives with the result. Other output fields at
  those two events were not probed.
- Grok loads project hooks only inside a git repository. With
  `GROK_FOLDER_TRUST=0` set in every run, no project hook fired in a plain
  folder, in Chromeria and in a direct `grok -p` run; after `git init`, they
  fired. Trusting a folder with `/hooks-trust` instead was not tested.
- Grok sends snake_case event names (`pre_tool_use`) to command hooks.
- Codex has no `PostToolUseFailure`; failures arrive at `PostToolUse`.
- OpenCode's appended text is part of the tool output, so it is visible in the
  Chromeria UI.
- OpenCode's user-message part is added in a messages transform that is
  not saved, and the canary adds it only on the first model call after a user
  message. Later calls in the same turn do not see it, so a serving plugin
  must add it again on every call it should reach.
- Keep each served entry under the smallest limit: under 2,500 tokens for
  Codex and under 10,000 characters for Grok. Claude Code spilled at 11.7 KB;
  entries of about 200 characters arrived inline.

## Run it

Each host runs in its own throwaway folder; Chromeria scratch threads work.
Create the thread idle, write the hooks into its folder, then send
[`prompt.md`](prompt.md) as the first message. Each fire appends a line to
`<folder>/.canary/log.jsonl` with the served tokens and times.

- **Claude Code:** `canary/setup.sh claude <folder>` writes
  `<folder>/.claude/settings.json`.
- **Codex:** use a temporary `CODEX_HOME` holding a copy of `auth.json` and a
  `config.toml`. Run `canary/setup.sh codex <folder> <codex-home>`, then
  `canary/codex-trust.py <codex-home> <folder>` to record the hooks' trust
  hashes. Add a Chromeria Codex provider instance whose home path is that
  directory, and delete the copied `auth.json` afterwards.
- **Grok Build:** `canary/setup.sh grok <folder>` runs `git init` and writes
  `<folder>/.grok/hooks/canary.json` with `--pre-context`. The folder needs
  trust: the runs added a Chromeria Grok provider instance with the
  environment variable `GROK_FOLDER_TRUST=0`.
- **OpenCode:** `canary/setup.sh opencode <folder>` writes
  `<folder>/.opencode/plugin/canary.js`.

Remove any temporary provider instances when the runs finish.

## Limits

- One run per host and seam, with one model per host. Quoting depends on the
  model following the prompt; a missing quote alone does not prove a discard,
  so each "discarded" cell was also checked in the host's session history.
- Only shell commands were probed. File-edit and MCP tools were not.
- The Codex runs used a temporary home, so the user's own Codex hooks and
  plugins were not loaded. The Claude Code run loaded the user's global hooks
  alongside the canary.
