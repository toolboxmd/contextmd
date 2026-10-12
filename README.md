# contextmd
Local-first context and memory system for AI agents, built on Markdown.

## Capture

Capture turns one machine's agent sessions into judged candidate lessons in a
local transit queue. It reads Chromeria threads on the machine where they were
recorded and never copies transcripts elsewhere. Two extractors (Sol and Grok)
read each segment with the same contract in `prompts/capture/extract.md`; one
judge (Opus) pairs and grades both lists blind with `prompts/capture/judge.md`.

Requirements: Python 3.12 (the version tested), Chromeria V2 (`~/.t3/userdata/chromeria-v2.sqlite`),
and Agent Observer 0.13 or later. Capture runs `agent-observer sync` first; a
sync failure fails the command.

A run is driven by hand from a Chromeria thread, because model jobs go through
T3 `delegate_task`:

1. `bin/contextmd capture prepare --day 2026-10-10` syncs Agent Observer, skips
   worker-batch threads and capture's own threads, and cuts each remaining
   thread's new events into segments.
2. `bin/contextmd capture next` prints the jobs to dispatch now, each with its
   `target`, `clientRequestId` and `task` text. Dispatch each with
   `delegate_task`, then save the returned ids:
   `bin/contextmd capture record <job> --task-id <taskId> --child-thread <childThreadId>`.
   Judge jobs appear once both extractor lists for a segment are finished.
3. `bin/contextmd capture commit` writes judged segments to the queue and
   moves each thread's cursor past them. Run it again until the run is
   `complete`.
4. `bin/contextmd capture report` prints counts per step and per thread kind,
   and tokens and wall time per model step.

A stopped run resumes with `capture next`: a job with a saved task id is never
handed out again. A job's output counts only after its Chromeria run has
completed. `next` lists a job under `invalid` when its output breaks the
contract or its run failed; mark it with
`capture record <job> --failed --note <reason>` and it is handed out as a new
attempt that writes its own output file. Only one run can be open at a time;
`capture abandon --reason <text>` closes a stuck run, and its uncommitted
segments are captured again by the next run.

The first time a run sees a thread, it records where capture starts, so a
first capture that never commits is retried from there. While a thread is
still running, its segment stops before the first turn item still being
written, so a cursor never moves past text that may still change. A segment
captured again never replaces another run's queue items. A segment whose lesson
has no Agent Observer event to anchor to stays uncommitted, with the reason in
`run.json`.

State lives in `~/.local/state/contextmd/capture/` (`queue/`, `cursors.json`,
`runs/`). Models and paths can be overridden in
`~/.config/contextmd/capture.json`; see `contextmd/capture/config.py` for the
keys.

Segments overlap: each one ends with the first 12,000 rendered characters of
the next as ordinary events, so a lesson that runs across a cut is seen whole.
A lesson cited only in that overlap is kept and marked `moment.cited: overlap`.

After a run is complete, `capture dedupe --run <run>` writes one judge brief
that groups the run's kept lessons across segments and threads, and
`--result` gives each item its group and the group's combined `found_by`.
`capture report` then counts `found_by` per distinct lesson as well as per item.

To compare prompts on identical input, `capture replay --from-run <run>
--segment <id> ... --experiment <name> --extract-prompt <file>` re-runs chosen
segments' exact transcripts with another prompt under `prompts/capture/`
(`extract-v1.md` is the prompt of the first recorded run).

To check segment boundaries, run the same threads whole with
`capture prepare --whole --experiment whole --thread <id>`, finish that run,
then `capture compare --whole-run <run> --segmented-run <run> --thread <id>`
writes a compare brief for one judge job, and `--result` reads its output.
Experiment runs keep their items inside the run and never move cursors.

Run the tests with `python3 -m unittest discover -s tests -t .`.
