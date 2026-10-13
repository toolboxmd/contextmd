# ContextMD
Local-first context and memory system for AI agents, built on Markdown.

## Capture

Capture is ContextMD's pipeline from agent sessions to the places later
sessions load or are served from. It has five steps:

1. **Extract.** Two extractors (Sol and Grok) read each part of a session with
   the same contract, `prompts/capture/extract.md`, and write candidate items.
   Each item cites the event it comes from and says what happened next.
2. **Judge.** One judge (Opus) sees both lists blind, pairs items that state
   the same lesson, and grades each: `keep`, `state`, `junk`, or `defect` (a
   problem the session noticed and did not fix). Contract:
   `prompts/capture/judge.md`.
3. **Classify.** Each kept item gets a destination owner, a serving lane and a
   visibility. A `defect` goes to the repository that owns it as a proposed
   Issue, never to a knowledge store. (Not built yet.)
4. **Reflect.** Per owner, a model compares the candidates with the whole
   owner file and decides `add`, `update` or `remove`. (Not built yet.)
5. **Write.** Code applies those decisions to the owner, commits with
   receipts, and deletes the items from the inbox. (Not built yet.)

This repository holds steps 1 and 2 today. They read Chromeria threads on the
machine where they were recorded, never copy transcripts elsewhere, and put
judged items in a local transit **inbox**. One **pass** is one capture over
a window of sessions.

Requirements: Python 3.12 (the version tested), Chromeria V2
(`~/.t3/userdata/chromeria-v2.sqlite`), and Agent Observer 0.13 or later.
Each pass runs `agent-observer sync` first; a sync failure fails the command.

### Running a pass

A pass is driven by hand from a Chromeria thread, because model jobs go
through T3 `delegate_task`:

1. `bin/contextmd capture prepare --day 2026-10-10` syncs Agent Observer, skips
   worker-batch threads and capture's own threads, and cuts each remaining
   thread's new events into segments.
2. `bin/contextmd capture next` prints the jobs to dispatch now, each with its
   `target`, `clientRequestId` and `task` text. Dispatch each with
   `delegate_task`, then save the returned ids:
   `bin/contextmd capture record <job> --task-id <taskId> --child-thread <childThreadId>`.
   Judge jobs appear once both extractor lists for a segment are finished.
3. `bin/contextmd capture commit` writes judged segments to the inbox and
   moves each thread's cursor past them. Run it again until the pass is
   `complete`.
4. `bin/contextmd capture report` prints counts per step and per thread kind,
   and tokens and wall time per model step.

A stopped pass resumes with `capture next`: a job with a saved task id is never
handed out again. A job's output counts only after its Chromeria run has
completed. `next` lists a job under `invalid` when its output breaks the
contract or its Chromeria run failed; mark it with
`capture record <job> --failed --note <reason>` and it is handed out as a new
attempt that writes its own output file. Only one pass can be open at a time;
`capture abandon --reason <text>` closes a stuck pass, and its uncommitted
segments are captured again by the next pass.

### What a segment shows

- **Context:** up to 12,000 characters of earlier events, read-only.
- **Events:** the part to extract from, at most 150,000 characters.
- **Overlap:** the first 12,000 characters of the next segment, as ordinary
  events, so a lesson that runs across a cut is seen whole. A lesson cited
  only there is kept and marked `moment.cited: overlap`.
- **Following:** up to 40,000 characters of later events in the same thread,
  read-only, so the outcome of a noticed problem is visible.

The first time a pass sees a thread, it records where capture starts, so a
first capture that never commits is retried from there. While a thread is
still running, its segment stops before the first turn item still being
written, so a cursor never moves past text that may still change. A segment
captured again never replaces another pass's inbox items. A segment whose
lesson has no Agent Observer event to anchor to stays uncommitted, with the
reason in `pass.json`.

### Writing an entry

A model job shapes one kept item into an exact-key entry with
`prompts/capture/shape-entry.md`. Then
`bin/contextmd capture apply <item> --entry <file> --store <dir>` writes it,
add-only:

- It refuses a store or entry that fails `contextmd-serve check`, a duplicate
  id, a body over 1,800 bytes, an absolute target, and a command key that is
  neither in the source session's tool input nor in at least 2 sessions of
  the last 14 days in Agent Observer.
- It writes `<id>.md` as one commit with `Capture-Key` and `Capture-Pass`
  trailers, pushes it, and moves the item from `inbox/` to `applied/` with
  the commit sha. Undo an entry with `git revert`.
- A re-run whose `Capture-Key` is already committed adds nothing; if that
  commit was never pushed, the re-run pushes it.

### State and configuration

State lives in `~/.local/state/contextmd/capture/` (`inbox/`, `applied/`, `cursors.json`,
`passes/`). State written before passes and the inbox were named that way
(`runs/`, `queue/`) is moved in place the first time a command runs. Models
and paths can be overridden in `~/.config/contextmd/capture.json`; see
`contextmd/capture/config.py` for the keys.

### Experiments

- `capture dedupe --pass <pass>` (on a complete pass) writes one judge brief
  that groups its kept lessons across segments and threads; `--result` gives
  each item its group and the group's combined `found_by`, and
  `capture report` then counts `found_by` per distinct lesson.
- `capture replay --from-pass <pass> --segment <id> ... --experiment <name>
  --extract-prompt <file>` re-runs chosen segments' exact transcripts with
  another prompt under `prompts/capture/`. `--reuse-extracts` keeps the
  source pass's extractor outputs and re-runs only the judge. Earlier
  contracts stay as `extract-v1.md`, `extract-v2.md` and `judge-v2.md`.
- To check segment boundaries, run the same threads whole with
  `capture prepare --whole --experiment whole --thread <id>`, finish that
  pass, then `capture compare --whole-pass <pass> --segmented-pass <pass>
  --thread <id>` writes a compare brief for one judge job, and `--result`
  reads its output.

Experiment passes keep their items inside the pass and never move cursors.

Run the tests with `python3 -m unittest discover -s tests -t .`.
