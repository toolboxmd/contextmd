# contextmd
Local-first context and memory system for AI agents, built on Markdown.

## Serving

Serving gives an agent stored knowledge before its next decision. This first
path covers exact keys on Claude Code (#11). Host hooks are thin: each hook
event becomes one moment, `contextmd/serve/server.py` decides what to serve,
and the hook prints the reply. Requirements: Python 3.12, no other packages.

### Exact-key entries

Each entry is one Markdown file in a store directory:

```markdown
---
id: pr-template
command: gh pr create
must-know-before: true
---
Answer every prompt of the PR template before opening a PR.
```

- `command` matches a command prefix in any shell segment. The command is
  split on unquoted `&`, `|`, `;`, newlines and subshells; quoted text is
  always an argument, and `$(...)` or backtick substitutions, also inside
  double quotes, are segments of their own. Heredoc bodies, leading
  `cd <dir>`, `env` and `NAME=value` are ignored, and the executable is reduced
  to its basename.
- `path` is a glob on the repository-relative path of a file tool (`**` spans
  directories).
- `error` matches a substring of a failed tool's error, compared lowercase with
  digits and whitespace collapsed.
- Keys may repeat; any one match serves the entry. The body must stay under
  1,800 UTF-8 bytes, because Claude Code shows only a 2 KB preview of larger
  hook context.
- The entry version is a hash of the file, so an edited entry counts as new.

`bin/contextmd-serve check` loads the store and lists bad entries.

### What is served when

| Moment (Claude Code event) | Served |
| --- | --- |
| Session start (`SessionStart`) | The session-start files, outside the turn budget. After compaction or `/clear` a new context epoch starts and the files are served again. |
| User message (`UserPromptSubmit`) | Starts a new turn and a new 10,000-character budget. |
| Before a tool call (`PreToolUse`) | A matching `must-know-before` entry denies the call once per entry version per session, with the entry as the reason. The retry passes. |
| Tool result or failure (`PostToolUse`, `PostToolUseFailure`) | Other matching entries, as added context. First fit wins within the turn budget (characters) and the 2,000-byte limit per hook output; the rest are cut. |
| Before done (`Stop`) | Logged only; nothing is served yet. |

An entry is served at most once per turn. Every moment writes one log record
with the moment id, kind, context epoch, turn, the commit of each store
loaded, and per entry its lane, id, version, outcome and remote status.

| Outcome | Meaning |
| --- | --- |
| `pending` | Reserved; the hook has not finished writing its output yet. |
| `injected` | The hook wrote the text to the host. |
| `delivery_failed` | Writing the output failed; budget, dedupe and deny-once are released. |
| `delivery_unverified` | The hook process ended before settling; the next moment after 10 seconds releases the reservation. |
| `selected` | Matched before a tool call; served at the tool result. |
| `deduped` | Already served this turn, or its deny was already used this session. |
| `cut_budget` | Did not fit the turn budget (`cut: turn`) or the per-output limit (`cut: moment`). |
| `unsupported_key` | The tool input had a shape the exact-key lane cannot read. |
| `not_on_machine` | A session-start file is missing. |

The log never holds prompt, command or result text. Read it with
`bin/contextmd-serve log [--session <id>]`.

### Set up

Configuration lives in `~/.config/contextmd/serving.json`, or the file named by
`CONTEXTMD_SERVING_CONFIG`:

```json
{
  "exact_keys": ["~/.local/share/contextmd/exact-keys"],
  "session_start": [{"path": "~/notes/PACK.md", "max_chars": 4000}],
  "turn_budget": 10000,
  "state_dir": "~/.local/state/contextmd/serving"
}
```

All keys are optional; the values above are the defaults, except
`session_start`, which defaults to none. `bin/contextmd-serve hooks claude-code`
prints the `hooks` block for a Claude Code settings file. The hook fails open:
on any error it prints nothing and appends the traceback to
`<state_dir>/errors.log`.

### Off switch

`bin/contextmd-serve off` turns serving off on this machine: hooks then print
nothing and log nothing, and sessions run as if unserved.
`bin/contextmd-serve on` turns it back on; `status` shows the state.

Run the tests with `python3 -m unittest discover -s tests -t .`.
