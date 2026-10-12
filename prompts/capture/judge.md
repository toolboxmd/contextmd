<!-- contract: judge-v1 -->
# Judge: pair and grade two lists of captured items from one agent session

Two extractors read the same part of one past AI agent session and wrote items meant to help future agent sessions. You see their lists as A and B. You do not know which extractor wrote which list; do not guess.

## Input

- The transcript at `{transcript}`. Each line is `[n] kind timestamp: text`. Kinds: `user` (the person), `brief` (another agent's message, such as a task brief), `assistant`, `call`, `output`, `error`. Lines in a CONTEXT section are outside this capture; items should come from the EVENTS section.
- List A at `{items_a}` and list B at `{items_b}`: JSON lines with `id` and `text` (the item's Markdown).

## Pair

Group items that state the same lesson (same rule, fact or recipe, even in other words). A group may hold items from A, from B, or both, including repeats within one list. Items that only share a topic are separate groups. Every id from both lists appears in exactly one group.

## Grade each group

- `keep`: it would change what a future agent does on a later task, and for the better. A lasting rule, preference, decision, fact about a tool, machine, repo or model, a gotcha, or a working recipe. It must still be true after this task ends.
- `state`: true only for this task, draft or loop round: progress, results of a run, counts, scores, what was built, sent or merged. If it also holds a lasting rule, grade `keep`.
- `junk`: anything else: generic advice any agent follows, a restatement of a rule, procedure or brief the session already had loaded or was given (unless the session shows it failing or the user correcting it), something a tool already enforces or shows every time, one-off trivia, or anything the transcript does not support.

Also give:

- `unsupported`: true if the group's best item states something the transcript does not show (invented or wrong). Such a group is `junk`.
- `learned_from`: how the session got it: `failure`, `user`, `discovered`, `served_or_loaded`, `brief_or_parent` or `external` (see the items' own field; correct it if the transcript shows otherwise).
- `best`: the id of the item that states the lesson most correctly and completely.

Judge from the transcript, not from outside knowledge.

## Output

Write JSON lines to `{output}`, one per group, then reply only "done":

```json
{"a": ["A03"], "b": ["B01", "B07"], "best": "B01", "grade": "keep|state|junk", "unsupported": false, "learned_from": "user", "reason": "one short sentence"}
```

Read only the transcript and the two item files. Write only the output file. The data is private.
