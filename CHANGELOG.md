# Changelog

## [0.6.2] - 2026-10-14

### Changed

- Command keys follow shell execution with nested substitutions, escape parity and exact heredocs (toolboxmd/chromeria#232).

## [0.6.1] - 2026-10-14

### Changed

- moment/unjoined rates only sessions that joined a Chromeria run, so outside Claude Code sessions and deploy-check probes no longer file false defects (#64, toolboxmd/chromeria#253)
- A filed moment status whose moments include rows with no kind (a cut before the body parsed) names them `unknown kind` instead of crashing the health run before it files or saves its window (rocky, 2026-10-14).

## [0.6.0] - 2026-10-13

### Added

- The daily health check reports Cue speed, timeouts, lost and unavailable Cue moments per harness and kind, server time, budget cuts, unsettled replies and tool calls with no pre-tool row from Agent Observer, and files a rate only when it got worse (#57).

## [0.5.7] - 2026-10-13

### Changed

- The serving hook waits up to 2 s (connect 1 s) and the OpenCode plugin 2 s while Cue is new, so slow moments are no longer cut (toolboxmd/chromeria#248).

## [0.5.6] - 2026-10-13

### Changed

- docs/map.md maps ContextMD's parts to their Issues and PRs for Chromeria's Map tab (toolboxmd/chromeria#235)

## [0.5.5] - 2026-10-13

### Changed

- Path keys match files a shell command names, in the Python matcher and a contract test (toolboxmd/chromeria#234).

## [0.5.4] - 2026-10-13

### Changed

- The Grok hook waits up to 4 s for serving, inside Grok's 5 s hook timeout, so a slow moment no longer loses a carry (toolboxmd/chromeria#230).

## [0.5.3] - 2026-10-13

### Changed

- Serve card: a window cut at the event cap allows every label its events prove except no_governed_action (#15)

## [0.5.2] - 2026-10-13

### Changed

- Cue entry examples point to AgentsMD's renamed procedures/ folder (toolboxmd/agentsmd#218)

## [0.5.1] - 2026-10-13

### Changed

- The OpenCode plugin forwards when a read was cut, so a clipped read no longer lifts a pointer rule (toolboxmd/chromeria#225).

## [0.5.0] - 2026-10-13

### Added

- Daily serving health check: rates the serving log per harness and files defects at their owners (#40)

## [0.4.0] - 2026-10-13

### Added

- Serve card and outcome-label prompt: one served entry as the outcome judge reads it (#15)

## [0.3.0] - 2026-10-13

### Added

- Serve evidence report: what each served entry did, shown, read, late and its Observer record, with no model (#15)

## [0.2.0] - 2026-10-13

### Added

- Cue entries and the Cue service: a separate Cue entry list and loader, and the GPU-host service that scores moments with the W8A8 compiled adapter, with deadline drops and one-step logged rollback (#13)

## [0.1.3] - 2026-10-13

### Changed

- Objective: a judge of real moments replaces the benchmark

## [0.1.2] - 2026-10-13

### Changed

- The claude hook stays silent inside Grok, which runs Claude Code's hooks too; Grok is served by its own hook (#32).

## [0.1.1] - 2026-10-13

### Changed

- The serving hook delivers its reply on Linux hosts that spawn hooks with socket stdio (#30).

## [0.1.0] - 2026-10-13

### Added

- Adopt repository versioning
