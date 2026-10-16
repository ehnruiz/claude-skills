# Changelog

All notable changes to the `agent-workflow` plugin. The version is `version` in `.claude-plugin/plugin.json`; the rule for bumping it and cutting a release is in [docs/releasing.md](docs/releasing.md).

Versions before 1.5.0 were not tagged. Their entries below are taken from the commits that bumped the version, with the commit hash and date, and no tags were created for them after the fact.

## Unreleased

### Added

- `docs/examples.md`: three examples run against this repository with their commands and exit codes, for `regression-proof`, `skill-collision-audit` and `supervisor`.
- `docs/install.md`: requirements, what was tested on which platform, the three install paths and how to check each one.
- `docs/releasing.md` and this changelog.
- Layout tests for the docs: relative links in the README, `hooks/README.md` and `docs/` resolve, the README's install command matches the manifests, and the changelog has a section for the current version.
- The CI matrix runs the Python suite on 3.9 and 3.12 and the Node suite on 20 and 22, the floors the docs state.

### Changed

- README: a shorter opening on what the library is for, a five-minute quick start, and pointers to the new docs. The skill, subagent and hook tables are unchanged.

## 1.5.0 (2026-10-15, tag `v1.5.0`)

### Added

- Four opt-in session hooks for JavaScript and TypeScript projects: `config-protection`, `commit-secrets`, `edited-files` and `stop-check`, with their Node test suite and `hooks/README.md` (commit `b28d878`).
- `explain-the-number`: like-for-like checks before a measured number is reported or credited to a change (commit `6e97839`).
- `writing-for-agents/PRESSURE-TESTS.md`: how to prove a new rule red then green in fresh subagents.
- The README shows how to install with `npx skills add`.

### Changed

- Tests wait on the condition rather than on the clock.
- Tickets route away from `plain-writing` to `ticket-write` and `ticket-trim`.

## 1.4.0 (2026-10-15, commit `dd12a4b`)

### Added

- `epic-review`, `confluence-jira-links`, `leader-1-1-prep`, `plain-writing` and `llm-integration`.

## 1.3.0 (2026-10-15, commit `0b3e11b`)

### Added

- `grilling`, `tdd`, `diagnosing-bugs`, `regression-proof`, `research` and `writing-for-agents`, and the `researcher` subagent.

## 1.2.0 (2026-10-12, commit `57c5c89`)

### Added

- The `handoff_notice.py` SessionStart hook, registered in `hooks/hooks.json`, which points a new session at the project's newest handoff note.

### Changed

- `supervisor`, `handoff`, `pick-up`, `tdd` and `diagnosing-bugs` each check for a project copy of themselves first and follow that one when it exists.

## 1.1.0 (2026-10-11, commit `0b5120d`)

### Added

- `skill-collision-audit` and its `find_collisions.py` script.

## 1.0.0 (2026-10-11, commit `f7b3bf1`)

### Added

- First release: `supervisor`, `handoff`, `pick-up`, `no-ai-attribution`, `open-in-my-browser`, `ticket-write`, `ticket-trim` and `ticket-digest`, with the `scout`, `doc-writer`, `implementer`, `tester` and `reviewer` subagents.
