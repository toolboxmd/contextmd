<!-- contract: compare-v1 -->
# Compare two captures of the same session

The same agent session was captured twice: once whole (list W) and once cut into segments (list S). Each list holds the lessons a judge kept. Find which lessons both captures hold, which only one holds, and whether a segmented lesson is wrong where the whole capture shows the fuller picture.

## Input

- `{whole}`: list W, JSON lines with `id`, `title`, `what`, `so_what`.
- `{segmented}`: list S, same fields.

## Pair

Group items that state the same lesson (same rule, fact or recipe, even in other words, even if one is a little more specific). Every id from both lists appears in exactly one group. A group may hold several ids from one list.

For each group that has both W and S items, set `segmented_wrong` to true only if the S version states something the W version contradicts or corrects (for example, an S item keeps a belief that the W item shows was later corrected).

## Output

Write JSON lines to `{output}`, one per group, then reply only "done":

```json
{"w": ["W03"], "s": ["S01"], "segmented_wrong": false, "note": "one short sentence"}
```

Read only the two list files. Write only the output file. The data is private.
