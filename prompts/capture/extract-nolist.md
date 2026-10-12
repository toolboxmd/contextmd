<!-- contract: extract-v1-nolist -->
# What should future sessions know?

You read part of one past AI agent session between a user and their agents. Decide what in it would help future agent sessions do better work for this user.

## Input

The transcript at `{transcript}`. Each line is `[n] kind timestamp: text`. Kinds:

- `user`: a message the person typed.
- `brief`: a message another agent sent, such as a delegated task brief or a parent's instruction.
- `assistant`: the agent's text.
- `call`, `output`, `error`: a tool call, its clipped output, and output that looks like an error.

Long texts are clipped with `[...]`. If the file has a CONTEXT section, those lines are outside this capture: read them for meaning only, and extract only from the EVENTS section.

## What to keep

Keep anything that would make a future agent act differently, and better, on a later task, if it knew it at the right moment. Decide for yourself what kinds of information this session holds. Keep it even if an agent could have looked it up: agents often do not look things up.

Leave out state that will change, such as how far this task got, but keep any lasting rule mentioned in it.

## Rules

- Use only what the transcript shows. Do not guess or add knowledge from elsewhere.
- One item per piece of information. Merge repeats within this transcript.
- If something was believed and later corrected, keep only the corrected version.
- Never copy secret values (tokens, keys, passwords). Name where the secret lives instead.
- Read-only: read only the transcript, write only your output file. No web, no other tools. The data is private.

## Output

Write Markdown to `{output}`, then reply only "done". If nothing is worth keeping, write the single line `NO ITEMS`. One section per item:

```markdown
## <kind>: <short title>

**What:** the information, stated so it stands alone without the transcript.
**Why:** why a future agent needs it: what went wrong, or what the user said or decided.
**So what:** when it matters and what a future agent should do with it.
**Event:** the [n] number of the EVENTS line that shows it most directly.
**Learned from:** one of `failure` (something broke and the fix was found), `user` (the person said, corrected or decided it), `discovered` (the agent found it by investigation), `served_or_loaded` (it repeats a rule or memory already in the session's instructions or served context), `brief_or_parent` (it repeats what a brief or parent agent said), `external` (from a document, web page or tool output from outside).
**Scope:** where it applies: repo (owner/name or path), tool, and host or machine. Write `any` for a part that does not narrow it.
**Applies when:** one line naming the situation in which a future agent needs this.
```

`<kind>` is your own one- or two-word label for what kind of information this is.
