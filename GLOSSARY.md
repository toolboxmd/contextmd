# ContextMD Glossary

Terms only ContextMD uses. Terms shared across the user's system, such as
capture, lesson, write step, moment, owner, Cue (intent detector), harness, session,
turn, machine, correction, policy denial and recurring problem, are defined
once in Chromeria's system glossary,
[`toolboxmd/chromeria` `GLOSSARY.md`](https://github.com/toolboxmd/chromeria/blob/main/GLOSSARY.md).

## Serving

**Entry**:
One Markdown file in an entry directory that ContextMD can serve: its keys and
a body.
Its version is a hash of the file, so an edited entry counts as new.

**Matcher**:
How an entry is found at a moment: `exact_key`, `retrieval` or `intent`
(Cue). Session-start files are served without a matcher; serve logs record
them as `session_start`.
_Avoid_: lane

**Exact key**:
An entry key (`command`, `path` or `error`) matched literally against a tool
call's command, file path or error.

**Must-know-before**:
An entry flag: a match before a tool call denies the call so the agent reads
the entry before acting.

**Deny-once**:
The rule that a must-know-before entry denies a matching tool call once per
entry version per session; the retry passes.

**Budget**:
The characters ContextMD may add in one turn (`turn_budget`), split into a
serve share and a deny share. Session-start files have their own limits.
_Avoid_: pool

**Context epoch**:
The part of a session between compactions or `/clear`; a new epoch serves the
session-start files again.

**Seam**:
A harness hook point where served text reaches the model, such as Claude
Code's `PreToolUse` deny. A seam counts only when a canary proves the model
reads it before its next action.

**Holdout**:
The comparison group of sessions, one in ten, that Cue does not serve.

**Serve card**:
One served entry as the outcome judge and the user read it: the rule as
served, how it was delivered, and the events from the served call to the next
message into the session.

**Outcome label**:
The judge's verdict on one serve card: `irrelevant`, `wrong`, `followed`,
`ignored` or `no_governed_action`, citing events.

**Missed serving**:
An entry that was not served when it applied, counted only when the session
then learned it the hard way: a failure, a user correction or rediscovery.

## Capture

**Pass**:
One capture over a window of sessions.
_Avoid_: run

**Segment**:
The part of one thread's new events that extractors read, with read-only
context before and after it.

**Cursor**:
The point in a thread up to which events are captured.

**Extractor**:
A model that reads a segment and writes candidate items under the extract
contract.

**Item**:
One candidate lesson from an extractor, citing the event it comes from.

**Capture judge**:
The model that pairs both extractors' items blind and grades each `keep`,
`state`, `junk` or `defect`.
_Avoid_: judge (alone)

**Ruling**:
The user's label on an item the capture judge or extractors disagree about,
used to calibrate the capture judge.

**Inbox**:
Where judged items wait between capture and the write step: first on the
capturing machine, then on the writer machine. Never served.
_Avoid_: queue

**Writer machine**:
The one machine that runs reflect and the write step for every owner.

**Receipt**:
One commit trailer per write-step decision, holding only opaque keys; the
record that the decision was applied.

**Experiment**:
A pass or replay run to test a prompt or segmenting change; it keeps its items
inside the pass and never moves cursors.

**Replay**:
Re-running chosen segments' exact transcripts under another prompt.
