<!-- contract: judge-v4 -->
# Judge: pair and grade two lists of captured items from one agent session

Two extractors read the same part of one past AI agent session and wrote items meant to help future agent sessions. You see their lists as A and B. You do not know which extractor wrote which list; do not guess.

## Input

- The transcript at `{transcript}`. Each line is `[n] kind timestamp: text`. Kinds: `user` (the person), `brief` (another agent's message, such as a task brief), `assistant`, `call`, `output`, `error`. Lines in a CONTEXT section are outside this capture; items should come from the EVENTS section. A FOLLOWING section, when present, holds later events: use it to see what happened next.
- List A at `{items_a}` and list B at `{items_b}`: JSON lines with `id` and `text` (the item's Markdown).

## Pair

Group items that state the same lesson (same rule, fact or recipe, even in other words). A group may hold items from A, from B, or both, including repeats within one list. Items that only share a topic are separate groups. Every id from both lists appears in exactly one group.

## Check the facts first

A grade often depends on facts the item does not show: does a definition or fact still hold in the current files, is a rule or instruction already written down somewhere (instructions, docs, a script's own help or docstring), did the user have to repeat it, was a problem fixed later. Check those facts from the transcript and, read-only, from the files and repositories it names. Do not guess. If a fact the grade depends on cannot be checked, say so in `reason`.

## Grade each group

First apply these checks in order. The first one that matches decides the grade, even if the item would be useful:

0. It is a problem the session noticed (a bug, a defect, a broken setting or behavior). Follow the transcript, including FOLLOWING, to the outcome. If it is still not fixed at the end of what you can read, or was only worked around: `defect`. A setup problem (a wrong configuration, missing file or wrong instructions) that made an agent miss something is a `defect` too. If it was fixed, the problem itself is not knowledge: grade only a lasting recipe or gotcha that remains (`keep`), else `state`.
1. It restates a procedure, rule or reference that is already written down (in the session's instructions, a loaded skill or project rules, a brief, a parent's message, the docs, or a script's own help), and the session does not show it failing or the user correcting it: `junk`. If the user had to repeat an instruction that was already written down, that is a `defect` in how it is served. A decision the user made in this session is not a restatement.
2. A tool already enforces it or shows it every time (a hook that blocks, a check that fails loudly, an error message that says what to do): `junk`.
3. It is true only for this task, draft or loop round, or it describes one project's or experiment's current setup (its files, flags, data formats, assignments, which run produced a figure): `state`. Progress, a run's results, counts, scores, a choice made for this task only, and what was built, sent or merged are `state`. If it also holds a lasting rule, judge that rule instead.
4. It is one-off trivia that will not recur: `junk`.

Otherwise:

- `keep`: it would change what a future agent does on a later task, and for the better. A lasting rule, preference, a decision the user made, a fact about a tool, machine or model, a gotcha, or a working recipe. It must still be true after this task ends.
- `junk`: generic advice any agent already follows, or anything the transcript does not support.

Also give:

- `unsupported`: true if the group's best item states something the transcript does not show (invented or wrong). Such a group is `junk`.
- `learned_from`: how the session got it: `failure`, `user`, `discovered`, `served_or_loaded`, `brief_or_parent` or `external` (see the items' own field; correct it if the transcript shows otherwise).
- `best`: the id of the item that states the lesson most correctly and completely.
- `outcome`: one line on what happened next: fixed (and by what), worked around, still open, decided, or `not a problem`.
- `owner`: for a `defect`, the repository or system that owns it as the transcript names it (for example `owner/repo`, a tool, or a machine); else null.

Judge from the transcript, not from outside knowledge.

## Output

Write JSON lines to `{output}`, one per group, then reply only "done":

```json
{"a": ["A03"], "b": ["B01", "B07"], "best": "B01", "grade": "keep|state|junk|defect", "unsupported": false, "learned_from": "user", "outcome": "fixed by commit abc123", "owner": null, "reason": "one short sentence"}
```

Read the transcript and the two item files; to check facts you may read, never change, other files and repositories the transcript names. Write only the output file. The data is private.
