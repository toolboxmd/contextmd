<!-- contract: shape-entry-v1 -->
# Shape: turn one kept lesson into an exact-key entry

A capture pass kept a lesson from a past AI agent session. Write it as one ContextMD exact-key entry: a short rule that is shown to a future agent when a command it runs matches the entry's key.

## Input

- The lesson at `{item}`: JSON with `title`, `what`, `why`, `so_what`, `applies_when`, `scope`, `source_time` and `moment`.
- The source session's tool calls, read-only: in the SQLite file `{observer_db}`, table `events`, rows with `session_key = '{session_key}'` and `family = 'tool_call'`. Column `target` holds the tool input (for a shell tool, the command).

## Entry

```markdown
---
id: <slug>
command: <words>
must-know-before: true
---
<Rule with its trigger words>. <Reason, one clause>. (<scope>, <date>)
```

## Field rules

- **`id`:** lowercase words joined by `-`, at most 120 characters, naming the rule, not the session.
- **Keys:** one or more `command:` lines; any one match serves the entry. Do not write `path:` or `error:` keys: the writer cannot yet check them against tool input and refuses them. If only a path or an error marks the moment, write `NO ENTRY` (below).
  - Copy keys from the source session's tool input. Do not invent a form an agent might type.
  - A command key is whole words, contiguous, as agents type them: `git -C x commit` does not match `git commit`, and a URL is one word. Words break at whitespace, quotes, backslashes and shell punctuation, so `"file:x.db?mode=ro"` is one word.
  - A command key's first word also matches the last part of a path: the key `chromeria-v2.sqlite` matches `~/.t3/userdata/chromeria-v2.sqlite`.
  - A path that agents reach through shell commands is a `command:` key (its file name, or the whole path as typed).
  - Prefer the fewest keys that cover the trigger. A key that matches unrelated work denies that work.
- **`must-know-before: true`** only when acting once without the rule causes a side effect or a silent wrong result. Otherwise leave the line out: the entry is then served after the tool result.
- **`target`:** only for a pointer rule whose full text lives in another file. Home-relative (`~/...`) or relative to the entry file, never an absolute `/Users/...` path. Leave it out for an inline rule.
- **Body:** the rule first, with its trigger words (the key words appear in the body, so the reader sees why it fired), then the reason in one clause, then the scope and the lesson's date (`source_time`, as `YYYY-MM-DD`) in parentheses. At most 1,800 UTF-8 bytes. Keep paths home-relative.
- **No transcript quotes, no secrets, no thread or task names.** State the lasting rule, not the session's story.

## Example

```markdown
---
id: chromeria-serverversion-not-commit
command: http://127.0.0.1:3773/.well-known/t3/environment
---
`serverVersion` in this response is the runtime package version (for example 0.0.45), not the fork commit. For the running build sha use `chromeria-watchdog status`. (Chromeria, 2026-10-09)
```

## When no entry fits

If the lesson has no trigger words in the source session's tool input, write one line `NO ENTRY: <reason>` instead of an entry. Do not invent keys.

## Output

Write the entry, or the `NO ENTRY` line, to `{output}`, then reply only "done".
