# Changelog

This project follows [Semantic Versioning](https://semver.org/). While the
major version is zero, minor releases may include breaking changes when they
are clearly documented.

## [Unreleased]

### Added

- Task-oriented user documentation with sanitized guides and screenshots for setup, trial configuration, run inspection, model profiles, and troubleshooting.

## [0.1.1] - 2026-08-29

### Security

- Restrict bundled web serving to an explicit asset allowlist.
- Derive response media types from a fixed registry instead of request-influenced filenames.
- Add regression coverage for encoded path traversal and response-header injection attempts.

## [0.1.0] - 2026-08-29

### Added

- Local web control center for launching and monitoring MazeBench trials.
- Exact interaction, reasoning, action, and context-compaction journals.
- Official-engine 3D replay and synchronized model-observation inspection.
- ASCII and structured JSON observation conditions.
- Generic automatic compaction and endpoint-managed context modes.
- Verified checkpoint forks, analytics, sanitized exports, and portable model profiles.

[Unreleased]: https://github.com/CedricCaruzzo/mazebench-control-center/compare/v0.1.1...HEAD
[0.1.1]: https://github.com/CedricCaruzzo/mazebench-control-center/compare/v0.1.0...v0.1.1
[0.1.0]: https://github.com/CedricCaruzzo/mazebench-control-center/releases/tag/v0.1.0
