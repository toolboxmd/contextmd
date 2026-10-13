# Changelog

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
