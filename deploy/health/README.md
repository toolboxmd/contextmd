# Daily serving health check: deployment

`contextmd-serve health` (toolboxmd/contextmd#40) runs once a day on each
machine that serves, as a user launchd job. This file is the canonical
source for that job under AgentsMD ADR 0001 (persistent host automation).
The machine's own record (which commit is deployed, when, and the first
run's output) belongs in that machine's private runbook.

## Mapping

| Canonical source | Runtime destination | Owner, mode | Restart scope |
| --- | --- | --- | --- |
| `bin/` and `contextmd/` at commit `<sha>` | `~/.local/share/contextmd/deployed/<sha>/` (a copy from `git archive`, never a link into a checkout) | user, `0755` directories, `0644` files, `0755` for `bin/contextmd-serve` | none; each run starts a new process |
| `deploy/health/com.toolboxmd.contextmd-health.plist.template` | `~/Library/LaunchAgents/com.toolboxmd.contextmd-health.plist`, with the placeholders filled | user, `0644` | `launchctl bootout` and `bootstrap` of this label only |
| configuration (no secrets): `{"machine": "<label>", "expected_harnesses": ["claude", "codex", "grok", "opencode"], "seed": {}}` | `~/.config/contextmd/health.json` | user, `0644` | read on each run |
| state, written by the job | `~/.local/state/contextmd/health.json` | user, `0644` | none |

Placeholders:

- `@PYTHON@`: a Python 3.12 or later, by absolute path.
- `@DEPLOYED@`: the deployed copy.
- `@BASE@`: the Chromeria base, such as `~/.t3`, expanded.
- `@GH@`: `gh` by absolute path. launchd's `PATH` does not include it.
- `@LOGS@`: a log directory.

Secrets: none in these files. Filing uses the user's own `gh` login. If
`gh` cannot read its token in a launchd context, the run prints
`gh ... failed` and exits 1, and the stored window does not move.

## Install

1. Validate the source: `python3 -m unittest discover -s tests -t .` at the
   commit to deploy.
2. Preview without writing anything:
   `bin/contextmd-serve health --base <base> --dry-run`.
3. Back up any existing plist, configuration and state files.
4. Copy the commit:
   `git archive <sha> bin contextmd | tar -x -C ~/.local/share/contextmd/deployed/<sha>`.
5. Fill and install the plist, then run
   `plutil -lint ~/Library/LaunchAgents/com.toolboxmd.contextmd-health.plist`.
6. The first run looks back 24 hours. To leave out an incident that is
   already fixed and tracked, start the window at install time:
   `{"<base>/userdata/contextmd.sqlite": {"window_end": <epoch seconds>}}` in
   the state file.
7. Load the job:
   `launchctl bootstrap gui/$(id -u) ~/Library/LaunchAgents/com.toolboxmd.contextmd-health.plist`.

## Health proof

`launchctl kickstart gui/$(id -u)/com.toolboxmd.contextmd-health` runs it
now. The out log shows the report (`Serving health on <machine>: ...`) and
any `filed` or `commented` lines. The state file's `window_end` moves to the
run's time. The err log stays empty.

## Drift check (read-only)

`git -C <checkout> archive <sha> bin contextmd | tar -t` lists the canonical
files. Compare each one's `shasum -a 256` in a fresh `git archive` extract
with the deployed copy, and diff the installed plist against the filled
template. Report missing and unmatched files; do not repair them in place.

## Rollback

`launchctl bootout gui/$(id -u)/com.toolboxmd.contextmd-health`, then
restore the backed-up plist (or remove it), configuration and state. Point
`@DEPLOYED@` back at the previous `<sha>` directory if one existed,
`bootstrap` again, and rerun the health proof. Issues the job filed stay
open; close any false ones by hand.
