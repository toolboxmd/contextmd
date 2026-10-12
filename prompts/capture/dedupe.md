<!-- contract: dedupe-v1 -->
# Find the same lesson captured more than once

The items below are lessons a judge kept from one machine's agent sessions on one day. Each was captured from one part of one session. The same lesson often appears from several parts of a long session, or from sibling sessions working on the same project.

Input: `{items}`, JSON lines with `id`, `title` and `what`.

Group items that state the same lasting information: the same rule, fact or recipe, even in other words, even if one is a little more specific. Items that only share a topic are not duplicates.

Write JSON lines to `{output}`, one per group of two or more items, then reply only "done":

```json
{"ids": ["D003", "D117"], "lesson": "one short sentence naming the shared information"}
```

Every id may appear in at most one group. Read only the items file. Write only the output file. The data is private.
