# Changelog

Changes to the `agent-workflow` plugin, by version. The version is `version` in `.claude-plugin/plugin.json`; the rule for bumping it and cutting a release is in [docs/releasing.md](docs/releasing.md).

Versions before 1.5.0 were not tagged. Their entries below are taken from the commits that bumped the version, with the commit hash and date, and no tags were created for them after the fact.

## Unreleased

## 1.7.0 (2026-10-22)

### Added

- `supervisor-reminder.mjs`: an opt-in UserPromptSubmit hook. On the first plain prompt of a session it adds one line to the context saying to route a multi-step task through `supervisor`. It fires once per session, leaves slash commands alone, and never fails the prompt.
- `loose-files.mjs`: an opt-in PreToolUse hook that denies creating a new document (Markdown, HTML, a spreadsheet, an image, an archive) at the repository root, by Write or by a shell redirection, `tee`, `cp` or `mv`. Existing files and the usual root files are allowed, and a project allows more names in `.claude/root-files`.
- `cloud-browsers.mjs`: an opt-in async SessionStart hook for Claude Code cloud sessions. In a project that declares Playwright 1.57 or newer, it runs `npm ci` when Playwright is not installed yet and installs the Chromium builds Playwright expects from Google's Chrome for Testing mirror, since the cloud network blocks Playwright's own download host.
- `pre_compact.py`: a PreCompact hook the plugin registers. It logs each compaction (time, session, trigger, branch, uncommitted files) beside the handoff notes, and `handoff_notice.py` now shows a resumed or compacted session its last compaction line, with or without a handoff note. The SessionStart registration adds `resume` to its matcher.
- `hooks/README.md` has the settings entries for the opt-in hooks.
- The `implementer` subagent lists every caller of a shared function, constant or default before it changes one, names what each relies on in its report, and fixes a root cause in the shared code rather than in the one caller the ticket names.

### Changed

- `supervisor`: a spawned ticket is not finished until its report is read, and the answer to the user comes after the last worker reports.
- The collision test keeps a hash of both descriptions for each reviewed pair, with the requests the pair was judged on. Editing either description fails the test until someone judges the pair again. Before, deleting the line a description draws could lower the pair's score under the threshold and leave the test green.

### Fixed

- `commit-secrets` no longer flags content a merge brings in from the other branch. During a merge, a finding is dropped when the merged-in commit's version of the same file already holds that line. A conflict resolution, an edit made during the merge and a private key that spans lines are still reported. Paths with spaces and a user's `diff.noprefix` or `diff.mnemonicPrefix` setting no longer confuse the scan.

## 1.6.0 (2026-10-20, tag `v1.6.0`)

### Added

Two items adapted from Matt Pocock's skills (`main` at `5beb35b`, after v1.3.1):

- `retro`: a skill you invoke after a coding session. It reads the session, lists what slowed the agent down or let a mistake through, most severe first, and builds the one you pick as a test, check or doc in the project. A new check has to fail on the mistake before it counts. It loads `writing-for-agents` for anything it writes.
- `git-guardrails.mjs`: an opt-in PreToolUse hook that denies `git push`, `git reset --hard`, `git clean` (except a dry run) and `git branch -D` before they run. It reads the command as shell, so a quoted commit message or a heredoc that mentions `git push` is not blocked. It is not registered in `hooks.json`; `hooks/README.md` has the settings entry.

## 1.5.2 (2026-10-19, tag `v1.5.2`)

### Changed

Three changes ported from Matt Pocock's skills (`main` at `5beb35b`, after v1.3.1):

- `grilling` words each question so "yes" accepts the recommended answer, so agreeing never means answering "no".
- `tdd` gives each proposed seam a one-line note on what it catches and what it misses.
- `diagnosing-bugs` has the agent `diff` a forced mutation against an untouched copy before trusting the red, so an edit that changed nothing cannot pass as a failing test.

## 1.5.1 (2026-10-16, tag `v1.5.1`)

### Fixed

- The hooks read stdin to its end even when it is a non-blocking pipe that is momentarily empty. Before, an `EAGAIN` on the read made `config-protection`, `commit-secrets`, `edited-files` and `stop-check` see an empty event and allow the call; the `config-protection` suite failed this way in 5 of 12 runs on Linux with Node 20.

### Added

- `docs/examples.md`: three examples run against this repository with their commands and exit codes, for `regression-proof`, `skill-collision-audit` and `supervisor`.
- `docs/install.md`: requirements, what was tested on which platform, the three install paths and how to check each one.
- `docs/releasing.md` and this changelog.
- Layout tests for the docs: relative links in the README, the changelog and `docs/` resolve, the install commands in the README and `docs/install.md` match the manifests, and the changelog has a section for the current version.
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

- The `tdd` skill adds a rule: a test waits on the condition it needs, never on a timer.
- Tickets route away from `plain-writing` to `ticket-write` and `ticket-trim`.

## 1.4.0 (2026-10-15, commit `dd12a4b`)

### Added

- `epic-review`, `confluence-jira-links`, `leader-1-1-prep`, `plain-writing` and `llm-integration`.

## 1.3.0 (2026-10-15, commit `0b3e11b`)

### Added

- `grilling`, `tdd`, `diagnosing-bugs`, `regression-proof`, `research` and `writing-for-agents`, and the `researcher` subagent.

### Changed

- Handoff notes are saved in Claude Code's own project folder for the main checkout, so sessions in git worktrees and other tools that follow the same convention share them (commit `a2d8761`).

### Fixed

- The handoff hook finds the main checkout from inside a submodule, reads `core.worktree` from the repository's own config only, keeps a separate git directory's worktrees, and keeps a trailing space in a submodule's path (commits `df338f1`, `905dbb7`, `356016b`).
- The collision test writes its fixture skills to a temporary folder, so installers never pick them up as real skills (commit `4256a31`).

## 1.2.0 (2026-10-12, commit `57c5c89`)

### Added

- The `handoff_notice.py` SessionStart hook, registered in `hooks/hooks.json`, which points a new session at the project's newest handoff note.

### Changed

- `supervisor`, `handoff`, `pick-up`, `tdd` and `diagnosing-bugs` each check for a project copy of themselves first and follow that one when it exists.
- `skill-collision-audit` asks for a second pass at `--threshold 0.12` for short descriptions, which can collide while scoring low (commit `4f89302`). The README's collision sample is taken from a real run (commit `ceb50eb`).

## 1.1.0 (2026-10-11, commit `0b5120d`)

### Added

- `skill-collision-audit` and its `find_collisions.py` script.

## 1.0.0 (2026-10-11, commit `f7b3bf1`)

### Added

- First release: `supervisor`, `handoff`, `pick-up`, `no-ai-attribution`, `open-in-my-browser`, `ticket-write`, `ticket-trim` and `ticket-digest`, with the `scout`, `doc-writer`, `implementer`, `tester` and `reviewer` subagents.
