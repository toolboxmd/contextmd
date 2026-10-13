<!-- contract: packet-v2 -->
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

The person must understand the item in under 30 seconds. The top part is for them: plain words, no jargon, no internal ids (no commit hashes, event or thread ids, file paths, or tool and function names unless the item is about that name), one idea per sentence.

**Top part:**

- `headline`: the item in at most 10 plain words.
- `happened`: one plain sentence saying what happened in the session.
- `matters`: one plain sentence saying why it might matter to someone later.

**Facts:** a decision can hinge on any of these questions, so answer every one. Do not leave a fact implicit. Before writing, ask which fact a person would need to decide, and make sure it is answered here or marked unknown. Each fact has:

- `line`: one plain line of at most 25 words, starting with the answer (`Yes`, `No`, `Partly` or `Unknown`), with no ids or paths;
- `detail`: the evidence in one or two sentences, where ids, paths and versions belong;
- `cites`.

When you cannot check a fact, the line starts `Unknown:` and the detail names the observation that would settle it. An empty search is never proof that something does not exist.

- `still_a_problem`: is the problem still there today? For an item that is not a problem, answer "No problem involved".
  - A fix counts only if it is in what the failing session ran (the installed version, the live build or the changed setting) and a later run of the same action succeeded, or the user confirmed it.
  - A merged fix alone is "Unknown: fix found, not confirmed in use".
- `who_can_fix`: who owns the thing that would change (a repo, file, URL, app setting, or vendor and account), and who can apply the remedy: an agent, only the user (account, app, hardware, credentials, money), a third party, or nobody because the behavior is intended. Name any existing open Issue or record in the detail.
- `already_written`: is it already written where an agent reads it (instructions, docs, a skill, a script's help), or shown by a tool every time it applies (a failing check, a warning printed on each run)? For an owner you cannot search, write "Unknown: owner not searchable".
- `happened_again`: has the same failure happened again? Same failure means all three of the following, read from event content, not counted from tool names:
  - the same symptom (error text or wrong behavior);
  - the same component (harness, provider or adapter, tool, and version where known);
  - a comparable triggering action.

  Search rocky's Observer only. The line says one of: "Yes, in <n> other sessions", "None found on rocky (partial search)", or "Unknown: <why>". Never write "one-off", "never" or "isolated". Put the window, queries, candidate count, number read and any limit hit in `coverage`, not in the line.
- `stays_true`: will this still be true later? Check whether it holds in the current version, code, configuration or docs; whether the source is stable (for example the same across versions); or whether it was tied to one task, draft, run or version.

**Cites:** a list of objects. Code checks the first four types before the person sees them, so get the fields exact:

- `{"type": "commit", "repo": "<local clone path>", "sha": "<full sha>", "path": "<file it touched, optional>", "in": "<tag or ref that should contain it, optional>"}`
- `{"type": "line", "file": "<absolute path>", "line": <n>, "quote": "<words copied from that line>"}`, or with `"repo"`, `"rev"` and `"path"` instead of `"file"` to cite a line at a revision.
- `{"type": "event", "id": <Observer event id>, "session_key": "<optional>"}`
- `{"type": "user_message", "id": "<Observer user message native_id>"}`
- `{"type": "url", "ref": "<Issue, PR or page URL>"}` or `{"type": "other", "ref": "<transcript line [n] or anything else>"}`: these are shown as your claim.

**Coverage:** `as_of` is `{as_of}`. `ledger_latest` is rocky's Observer latest event time (`SELECT max(ts) FROM events`, as UTC ISO).

## Output

Write one JSON object to `{output}`, then reply only "done":

```json
{"headline": "...", "happened": "...", "matters": "...",
 "facts": {"still_a_problem": {"line": "...", "detail": "...", "cites": []},
           "who_can_fix": {"line": "...", "detail": "...", "cites": []},
           "already_written": {"line": "...", "detail": "...", "cites": []},
           "happened_again": {"line": "...", "detail": "...", "coverage": "...", "cites": []},
           "stays_true": {"line": "...", "detail": "...", "cites": []}},
 "as_of": "...", "ledger_latest": "..."}
```

Keep commands, paths, versions and quoted errors exact. Write only the output file. The data is private.
