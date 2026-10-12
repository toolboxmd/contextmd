# contextmd
Local-first context and memory system for AI agents, built on Markdown.

## Serving

Serving gives an agent stored knowledge before its next decision, inside
Chromeria. Each harness hook (Claude Code, Codex, Grok Build, OpenCode)
forwards its raw event to the serving module of the local Chromeria server,
which matches it against the stores below, decides what to serve or deny, and
logs the moment. Serving state and the serving log live in Chromeria
(`<base>/userdata/contextmd.sqlite`); this repository holds the knowledge, the
store check and the hook installer. Sessions outside Chromeria are not served.
Requirements: Python 3.12, `curl`, and a Chromeria build with the serving
module.

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

- `command` matches wherever its words appear in that order in the shell
  command. The command is not parsed: it is cut into words at whitespace,
  quotes, backslashes and shell punctuation, so the key also matches inside
  quotes, heredocs, `bash -c` strings and substitutions, and `cd` or `env`
  before it does not matter. The first word may be an executable path
  (`/usr/bin/gh`). Words that only appear as data also match; that rare false
  hit costs one extra model round and still delivers the rule.
- `path` is a glob on the repository-relative path of a file tool (`**` spans
  directories).
- `error` matches a substring of the failure text or of the tool's output,
  compared lowercase with digits and whitespace collapsed. Output counts
  because agents often run `cmd; echo "exit=$?"`, so the call itself succeeds,
  and Codex reports no failure at all. A printed non-zero exit (`exit=1`,
  `exit code 2`) also marks the call failed.
- `target` (must-know-before entries only) makes a pointer rule: the deny names
  that file, relative to the entry, and the rule lifts once the agent is seen
  reading it. Without `target` the rule text is in the deny itself.
- Keys may repeat; any one match serves the entry. The body must stay under
  1,800 UTF-8 bytes, because Claude Code shows only a 2 KB preview of larger
  hook context.
- `id` defaults to the file name and must stay within 120 characters.
- The entry version is a hash of the file, so an edited entry counts as new.

`bin/contextmd-serve check` loads the store and lists bad entries.

### What is served when

| Moment | Served |
| --- | --- |
| Session start | The session-start files, outside the turn budgets (Claude Code). After compaction or `/clear` a new context epoch starts and they are served again. |
| User message | A new Chromeria run is a new turn: an 8,000-byte serve budget and a 2,000-byte deny budget. |
| Before a tool call | A matching must-know-before entry denies the call. A pointer rule keeps denying until the agent reads its target in this session and context epoch; an inline rule lifts once the model was shown it whole. After 3 denies without that, the next call passes, logged `not_read_capped`. One deny carries every whole rule that fits; the rest deny their next matching call. |
| Tool result or failure | Other matching entries, first fit within the serve budget and 2,000 bytes per output. On Grok, which discards this output, they are printed at the next `PreToolUse`. |
| Before done | Logged only. |

Served text says it comes from the user's own ContextMD store. On OpenCode it is
added to the user's message rather than the tool output.

A deny counts as shown only with evidence. On Claude Code and Grok, Chromeria
records the text the model saw for a denied call; when another hook's deny
replaced this one, the rule stays armed and that deny does not count toward
the cap. Elsewhere, the hook confirming it wrote the reply is the evidence.

### Set up

The stores and session-start files are named in `~/.config/contextmd/serving.json`
(or the file in `CONTEXTMD_SERVING_CONFIG`, read by the Chromeria server):

```json
{
  "exact_keys": ["~/.local/share/contextmd/exact-keys"],
  "session_start": [{"path": "~/notes/PACK.md", "max_chars": 4000}]
}
```

Install the hook per harness for the Chromeria base directory:

```sh
bin/contextmd-serve install claude --base ~/.t3     # also: codex, grok, opencode
```

Install issues an ordinary Chromeria session for the hooks with Chromeria's
`t3 auth session issue` (subject `contextmd-hook`, scope `orchestration:read`,
365 days) and stores it in `<base>/userdata/contextmd/hook-credential`, mode
0600. It writes the harness's user-level hook configuration (for Codex, every
Codex home the Chromeria instances use, with trust hashes), keeps other hooks,
and backs up each changed file as `<file>.contextmd-backup`. Installing again
issues a new session and revokes the old one. The hook finds the server through
Chromeria's `<base>/userdata/server-runtime.json`. With the server down, the
hook gives up within a second and the call runs unserved.

### Off switch and log

`bin/contextmd-serve off --base ~/.t3` turns serving off for that Chromeria:
hooks then call nothing. `on` turns it back on; `status` shows the state and
the credential's expiry. `bin/contextmd-serve log --base ~/.t3 [--session ID]`
prints the serving log as JSON lines: per moment its run, epoch and status,
per entry its outcome. It never holds prompt, command or result text.

Run the tests with `python3 -m unittest discover -s tests -t .`. The contract
suite in `tests/test_contract.py` runs the real hook against a Chromeria dev
server when `CONTEXTMD_CHROMERIA` names a Chromeria checkout.
