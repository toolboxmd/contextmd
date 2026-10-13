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

### Cue entries

Cue serves knowledge whose moment has no trigger words, such as a research
procedure that applies whenever a request asks for facts beyond what is at
hand. Each Cue entry is one Markdown file in a Cue directory, which must not
lie inside an exact-key directory (each loader rejects the other's fields):

```markdown
---
id: agentsmd-research
name: Research
applies: The request asks a question whose answer is facts beyond what is at hand. Not: ...
target: ../skills/operations/workflows/research/index.md
---
AgentsMD: follow the research procedure.
```

- Cue scores `<name>: <applies>`, so those texts must match what the deployed
  adapter was trained on; edit them only together with retraining (#4).
- An entry that scores at or above the adapter's threshold is served with its
  `applies` line first (cut at 600 characters), so the agent can dismiss a
  wrong fire in one read, then the body and "Read `<target>` in full."
- `target` is optional and relative to the entry. The body, `id` and version
  follow the exact-key rules.

`bin/contextmd-serve check` loads both stores and lists bad entries for each.

### What is served when

| Moment | Served |
| --- | --- |
| Session start | The session-start files, outside the turn budgets (Claude Code). After compaction or `/clear` a new context epoch starts and they are served again. |
| User message | A new Chromeria run is a new turn: an 8,000-byte serve budget and a 2,000-byte deny budget. Cue scores the conversation and its fires are served at once (on Grok, at the next `PreToolUse`). |
| Before a tool call | A matching must-know-before entry denies the call. A pointer rule keeps denying until the agent reads its target in this session and context epoch; an inline rule lifts once the model was shown it whole. After 3 denies without that, the next call passes, logged `not_read_capped`. One deny carries every whole rule that fits; the rest deny their next matching call. |
| Tool result or failure | Other matching entries and the Cue fires, first fit within the serve budget and 2,000 bytes per output. On Grok, which discards this output, they are printed at the next `PreToolUse`. |
| Before done | Logged only. |

Cue runs on the GPU serving host (see [Cue service](#cue-service)). A user
message or a failed call is scored at once and waits at most 650 ms. Before a
tool call Cue only starts scoring, and the tool result collects the score, so a
tool slower than Cue costs no wait. One session in ten, fixed per session, is
the holdout: Cue's fires there are logged as `holdout` and never served. If the
service refuses a connection, Chromeria stops calling it for every session on
that machine until a probe every 30 seconds sees it answer; a slow reply is a
`timeout` for that call only. Exact keys never wait for Cue.

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
  "session_start": [{"path": "~/notes/PACK.md", "max_chars": 4000}],
  "cue": ["~/dev/toolboxmd/agentsmd/contextmd-cue"],
  "cue_url": "http://100.116.0.2:8710"
}
```

Cue is off unless both `cue` and `cue_url` are set. Add them only once every
Chromeria server reading the file has the Cue lane: an older server rejects a
config with unknown keys and then serves nothing.

Install the hook per harness for the Chromeria base directory:

```sh
bin/contextmd-serve install claude --base ~/.t3     # also: codex, grok, opencode
```

Install issues an ordinary Chromeria session for the hooks with Chromeria's
`t3 auth session issue` (subject `contextmd-hook`, scope `orchestration:read`,
365 days) and stores it in `<base>/userdata/contextmd/hook-credential`, mode
0600; installing again reuses it until 30 days before it expires, and
`--renew` replaces it at once. A replaced session is revoked; if that fails,
the install fails and the next one retries it. Install writes the harness's
user-level hook configuration (for Codex, the Codex home of every Chromeria
Codex instance, with trust hashes recorded where that instance's Codex reads
them), keeps other hooks, file permissions and symlinks, and backs up each
changed file as `<file>.contextmd-backup`. The hook finds the server through
Chromeria's `<base>/userdata/server-runtime.json`. With the server down, the
hook gives up within a second and the call runs unserved.

### Off switch and log

`bin/contextmd-serve off --base ~/.t3` turns serving off for that Chromeria:
hooks then call nothing. `on` turns it back on; `status` shows the state and
the credential's expiry. `bin/contextmd-serve log --base ~/.t3 [--session ID]`
prints the serving log as JSON lines: per moment its run, epoch and status,
per entry its outcome. It never holds prompt, command or result text. Each
moment also records Cue's status (`ok`, `started`, `not_called`, `timeout`,
`connect_failed`, `overload`, `lost`, `error`), the adapter, the round trip
(`cue_ms`) and how long the moment waited for it (`cue_wait_ms`); each Cue
candidate records its score and an outcome such as `below_threshold`,
`holdout` or `injected`.

### Serve evidence report

`bin/contextmd-serve report --base ~/.t3 [--since 2026-10-13] [--json]` shows
what each served entry did, from the serving log and Agent Observer, with no
model. It covers every serve the hook settled (`injected`, `truncated`,
`unverified`, `awaiting_evidence`) and every deny whose text Chromeria's
check did not find in the call's recorded output (`hidden`). Per serve it
records:

- `shown`: `yes` when Chromeria found the deny text in the call's recorded
  output, `no` when its check did not find it there, `unproven` otherwise
  (context serves, denies on Codex and OpenCode, and denies whose check never
  arrived);
- `lift`: how a deny's rule lifted (`read`, `delivered_inline`,
  `not_read_capped`), `open` when the session went on without that, or
  `pending` when nothing came after it yet;
- `late`: a must-know-before entry served after its own command ran, as
  context on its result or as a Grok carry selected there;
- `evidence`: `anchored` to the Observer event of the served call, or
  `not_imported`, `no_native_record` (Codex keeps no record of a denied call)
  or `observer_failed`.

Without `--json` it prints one short card per problem it finds: what happened,
why it matters, then the counts. Canary entries (`live-canary-*`) are left
out. The report changes nothing. It opens Agent Observer's ledger read-only
with SQLite, because Observer's `trace` command opens it read-write
(toolboxmd/agent-observer#73). The ledger path comes from `--observer-db`,
else `$AGENT_OBSERVER_DB`, else Observer's default
`~/.local/state/agent-observer/observer.db`. The report reads only ledger
schema 3 (Observer 1.0.1); with any other schema every serve is
`observer_failed`.

### Serve card

`bin/contextmd-serve card --base ~/.t3 --candidate ID [--json]` shows one
served entry the way the outcome judge reads it:

- the rule as it was served, with its source: the entry file, the entry at the
  served version from its store's Git history, the deny text, or the reply
  that carried it;
- how it was delivered;
- the events from the served call up to the next prompt or interrupt a
  person sent into the session, or at most 60 events. A parent agent's
  message into a child session does not end the window yet
  (toolboxmd/agent-observer#73).

The first lines say what happened and why it matters, then give the facts.
The judge labels the card with
[`prompts/serve/label.md`](prompts/serve/label.md), using only the card's
`allowed_labels`. `ignored` is allowed only on a serve proven shown
(`shown = yes`), and a breach on any other serve stays unlabeled. A card the judge must not
label names its gap under `no_evidence`:

- the report's evidence gap;
- `window_cap`, when the window hit 60 events;
- `window_open`, when the session went on past what Observer has imported;
- `rule_text`, when the served text cannot be recovered.

The card reads Observer's ledger read-only, like the report, and gets tool
input and results from `agent-observer event show`, which is a read-only
command. Cards hold transcript text, so keep them on the machine.

Run the tests with `python3 -m unittest discover -s tests -t .`. The contract
suite in `tests/test_contract.py` runs the real hook against a Chromeria dev
server when `CONTEXTMD_CHROMERIA` names a Chromeria checkout.

## Cue service

The Cue service runs on the GPU serving host (bestia, RTX 3090) as the user
unit `contextmd-cue.service`. It scores each request's window against the
entries the request names, one request at a time; a request still queued after
its deadline (1.5 s) is dropped with `503`. It builds the window from the
conversation items it receives, in the format the adapter was trained on
(`contextmd/cue/window.py`), and keeps windows only in memory. It binds only
after its warm-up, so while it starts, callers see a refused connection.

Variants are named in `~/.config/contextmd-cue/service.json` (template
[`deploy/cue/service.example.json`](deploy/cue/service.example.json)): base
model, adapter, quantization, compile and the variant's own threshold. One is
`active`, the one before it `previous`.

| Canonical source | Deployed copy on bestia | Mode |
| --- | --- | --- |
| `bin/contextmd-cue`, `contextmd/` | `~/.local/share/contextmd-cue/app/` | files `0644`, launcher `0755` |
| `deploy/cue/contextmd-cue.service` | `~/.config/systemd/user/contextmd-cue.service` | `0644` |
| `deploy/cue/requirements.txt` | `~/.local/share/contextmd-cue/venv` (Python 3.12) | |
| `deploy/cue/service.example.json` | `~/.config/contextmd-cue/service.json` | `0600` |

All owned by `luke:luke`. The adapter is copied to
`~/.local/share/contextmd-cue/adapters/` and checked by SHA-256.

- **Validate, then install or update:** check out the commit to deploy and run
  `python3 -m unittest discover -s tests -t .`; install only on a pass. Move
  the current `app/` to `app.previous/`, copy `bin/contextmd-cue` and
  `contextmd/` from that commit into a new `app/` (`install -m 0755` for the
  launcher, `0644` for the rest) and the unit with `install -m 0644`. Then
  `systemctl --user daemon-reload`, `systemctl --user restart contextmd-cue`,
  and wait for `curl -s http://100.116.0.2:8710/health` to answer `"ok": true`
  (a compiled variant warms up for 1.5 to 4 minutes). Record the deployed
  commit, the app tree hash, the unit and config hashes and the adapter's
  SHA-256 in the runbook's `bestia/CHANGES.md`.
- **Roll back the variant:** `bin/contextmd-cue rollback ~/.config/contextmd-cue/service.json`
  swaps `active` and `previous`, restarts the unit and appends the switch and
  the restart result to `~/.local/state/contextmd-cue/deployments.jsonl`;
  `deploy <id>` makes another variant active the same way. Every serving-log
  moment records the adapter that scored it.
- **Roll back the code:** swap `app/` and `app.previous/`, restart the unit,
  check `/health`, and record the restored commit in `bestia/CHANGES.md`.
- **Drift check (read-only):** compare `sha256sum` of each deployed file with
  the canonical file at the recorded commit.
- **Remove:** `systemctl --user disable --now contextmd-cue`, delete the unit
  and `~/.local/share/contextmd-cue`, `~/.config/contextmd-cue`,
  `~/.cache/contextmd-cue` and `~/.local/state/contextmd-cue`, then remove
  `cue` and `cue_url` from each machine's `serving.json`.
