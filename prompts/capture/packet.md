<!-- contract: packet-v1 -->
# Checker: build the context packet for one captured item

A person will decide what to do with one item captured from a past AI agent session. They have not seen that session. Your packet gives them the item's story and the facts the decision depends on, each fact with a cite they can check. You do not decide. Write no verdict and no verdict words (keep, state, junk, defect, store, discard, worth keeping), and do not say what the person should conclude.

## Input

- The item: `{item}`.
- The session part it came from: `{transcript}`. Each line is `[n] kind timestamp: text`. A FOLLOWING section, when present, holds later events.
- Leads from an earlier check: `{leads}` (`none` when there are none). Treat every lead as a claim to verify, not a fact. Drop or correct a lead you cannot confirm.
- Do not open any judge, extractor, label or ruling file (`judge*`, `extract-*`, `items/`, `labels*`, `ROUND*`, `*rulings*`, `*answers*`). They hold verdicts.

## What to read

Read the transcript first. To check facts, you may read the files, repositories, Issues, PRs and pages it names, and Agent Observer's ledger on this machine (rocky). Everything is read-only:

- `git -C <repo> log`, `show` or `grep`, and `gh issue view`, `gh pr view` or `gh search`. Never use checkout, switch, reset, pull, fetch or any other write.
- Observer: `agent-observer event show <id>`, or python `sqlite3.connect('file:<db>?mode=ro', uri=True)` on `~/.local/state/agent-observer/observer.db`. The `sqlite3` CLI cannot open it. Never run `agent-observer sync`.

## The packet

**Story:** one or two plain sentences each.

- `before`: what the agent was doing, and why this came up.
- `what`: the item itself, in plain words.
- `after`: what the session did next: fixed it (with what), worked around it, decided something, or nothing.
- `now`: the state today, from what you checked: is the problem still there, does the fact still hold.

**Facts:** each has `text`, one or two sentences, and `cites`. When you cannot check a fact, write `unknown: <why>` and name the observation that would settle it. An empty search is never proof that something does not exist.

- `fixed_later`:
  - Name the fix artifact (a commit, PR, setting or release).
  - Say whether the fix is in what the failing session ran: the installed version, the live build, or the changed setting.
  - Say whether a later run of the same action succeeded, or the user confirmed it.
  - A merged fix alone is "fix found, resolution unknown".
- `owner`: who owns the thing that would change, as a reference with a locator (a repo, file, URL, app and setting path, or vendor and account). Say who can apply the remedy: an agent, only the user (an account, app, hardware setting, credentials or money), a third party, or nobody because the behavior is intended. Name any existing open Issue or record.
- `already_written`: is this already written where an agent reads it (instructions, docs, a skill, a script's own help), or shown by a tool every time (a check that fails, a warning printed on each run)? Cite the file and line or the tool output. For an owner you cannot search, write `unknown: owner not searchable`.
- `recurrence`: has the same failure happened again? Same failure means all three of these, read from event content, not counted from tool names:
  - the same symptom (error text or wrong behavior);
  - the same component (harness, provider or adapter, tool, and version where known);
  - a comparable triggering action.

  Search rocky's Observer only. Report one of:
  - `confirmed: <n> sessions, <event ids>`, deduplicated per session, each repeat read and matched;
  - `none confirmed in <coverage>`;
  - `unknown: <reason>`.

  Coverage states the window searched, the queries run, the number of candidates, how many you read, and whether a limit was hit. This is rocky only, so it is partial coverage. Never write "one-off", "never recurred" or "isolated".

**Cites:** a list of objects. Code checks the first four types before the person sees them, so get the fields exact:

- `{"type": "commit", "repo": "<local clone path>", "sha": "<full sha>", "path": "<file it touched, optional>", "in": "<tag or ref that should contain it, optional>"}`
- `{"type": "line", "file": "<absolute path>", "line": <n>, "quote": "<words copied from that line>"}`, or with `"repo"`, `"rev"` and `"path"` instead of `"file"` to cite a line at a revision.
- `{"type": "event", "id": <Observer event id>, "session_key": "<optional>"}`
- `{"type": "user_message", "id": "<Observer user message native_id>"}`
- `{"type": "url", "ref": "<Issue, PR or page URL>"}` or `{"type": "other", "ref": "<transcript line [n] or anything else>"}`: these are shown as your claim.

**Coverage:** `as_of` is `{as_of}`. `ledger_latest` is rocky's Observer latest event time (`SELECT max(ts) FROM events`, as UTC ISO). `read` is what you read, in one sentence.

## Output

Write one JSON object to `{output}`, then reply only "done":

```json
{"before": "...", "what": "...", "after": "...", "now": "...",
 "facts": {"fixed_later": {"text": "...", "cites": []}, "owner": {"text": "...", "cites": []},
           "already_written": {"text": "...", "cites": []}, "recurrence": {"text": "...", "cites": []}},
 "as_of": "...", "ledger_latest": "...", "read": "..."}
```

Write in plain, short sentences for a person reading quickly. Keep commands, paths, versions and quoted errors exact. Write only the output file. The data is private.
