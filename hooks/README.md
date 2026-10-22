# Session hooks

Eight hooks for Claude Code sessions. They protect configuration files, guard against committing secrets, block git commands that publish or destroy work, keep new documents out of the repository root, check edited code with eslint and the type checker before a response ends, remind a session to route multi-step work through the `supervisor` skill, and install Playwright's Chromium in a cloud session. `stop-check` needs a JavaScript or TypeScript project and `cloud-browsers` a Playwright one; the others work in any git repository. They are plain ES modules with no dependencies and need Node 20 or newer.

They are opt-in per project. The plugin does not register them in `hooks.json`; you add them to your own settings (see "Turning them on"). The two hooks the plugin does register, `handoff_notice.py` and `pre_compact.py`, are described in the top-level README.

## How they work

A hook reads an event from stdin as JSON, answers on stdout with a decision, and prints nothing when it allows. A malformed or empty event always allows, because a hook must never fail the tool it watches. The read retries when stdin is a non-blocking pipe that is momentarily empty (`EAGAIN`), for up to ten seconds, so a slow writer does not read as an empty event. A decision looks like this:

```json
{
  "hookSpecificOutput": {
    "hookEventName": "PreToolUse",
    "permissionDecision": "deny",
    "permissionDecisionReason": "..."
  }
}
```

`permissionDecision` is `deny` (the call does not run) or `ask` (the call waits for you, with the reason shown). Reference: https://code.claude.com/docs/en/hooks.

| Hook | Watches | Blocks |
| --- | --- | --- |
| `config-protection.mjs` | PreToolUse on Edit, Write, MultiEdit, NotebookEdit, Bash | Edits to an existing lint, type, test, build or CI configuration file |
| `commit-secrets.mjs` | PreToolUse on Bash | A `git commit` whose changes contain a secret |
| `git-guardrails.mjs` | PreToolUse on Bash | `git push`, `git reset --hard`, `git clean` and `git branch -D` |
| `loose-files.mjs` | PreToolUse on Write, Bash | A new document (Markdown, HTML, a spreadsheet, an image, an archive) written at the repository root |
| `edited-files.mjs` | PostToolUse on Edit, Write, MultiEdit | Nothing. It records the files a session edits for the next hook |
| `stop-check.mjs` | Stop | The end of a response, until the files edited in it pass eslint and the type check |
| `supervisor-reminder.mjs` | UserPromptSubmit | Nothing. It adds one line to the context on a session's first plain prompt |
| `cloud-browsers.mjs` | SessionStart, async | Nothing. In a cloud session it installs the Chromium builds the project's Playwright expects |

`lib.mjs` and `shell.mjs` hold the helpers the others share. They are not hooks.

## config-protection

An agent that cannot make a check pass can fix the code or loosen the check. The second is quicker and wrong, so these files are changed by a person. The hook denies edits to an existing file named:

- `eslint.config.*`, `.eslintrc*`
- `tsconfig*.json`
- `.prettierrc*`, `prettier.config.*`
- `biome.json`, `biome.jsonc`
- `vitest.config.*`, `jest.config.*`, `playwright.config.*`
- `.github/workflows/*.yml` and `.yaml`, `.github/dependabot.yml`
- the hooks themselves (`.claude/hooks/*.mjs`), because a session that could edit them could switch a check off

Names match at any depth, so a worktree's copy is protected too. These locations are never protected: `node_modules/`, `vendor/`, `.git/`, `.next/` (a build output that holds its own `tsconfig.json`) and `tests/fixtures/`. Creating a file that does not exist yet is allowed, and so is reading one.

A project adds its own names in `.claude/protected-files` at the project root. Each non-empty line that does not start with `#` is a glob: `*` matches any run of characters except `/`, `**` matches across directories, `?` matches one character. Like the built-in names, a glob matches at any depth, and the same never-protected locations apply.

```
# .claude/protected-files
Makefile
config/*.yml
docs/**/*.lock
```

On Bash, the same files are protected as the target of `sed -i`, `perl -i`, a redirection (attached to a word or not: `echo x>file`, `>>`, `>|`), `tee`, the first argument of `mv` and the last argument of `cp` or `mv`. The hook follows `cd`, `pushd` and `popd`. After a `cd` it cannot follow (a variable, a substitution, `cd -`) it fails closed: a write to a protected name is denied when such a file exists at the last known directory or the project root. Use a literal path then.

## commit-secrets

Before a `git commit` runs, the hook scans what the commit would take: the staged changes, plus unstaged changes of tracked files for `-a`, plus the files a `git add` earlier in the same command would stage (untracked ones included). `cd`, `pushd` and `-C` are followed to the right repository.

- With `gitleaks` on `PATH` (or at `CLAUDE_HOOK_GITLEAKS_BIN`), a finding denies the commit. The message names the rule and `file:line`, never the value.
- Without it, or with `CLAUDE_HOOK_GITLEAKS=off`, a built-in pattern set asks instead of denying, because patterns have false positives. It covers:
  - AWS access keys (long-lived and temporary), and an `AWS_SECRET_ACCESS_KEY` assignment
  - GitHub tokens, `sk-` API keys, Slack tokens and Google API keys
  - private key headers and bearer tokens
  - an Entra client secret (the `8Q~` marker)
  - an Azure storage `AccountKey=` and a SAS `sig=` signature
- Nothing to commit allows. Binary untracked files are skipped. A text file over 1 MB, or anything past 400 untracked files or 4 MB, is not scanned and the hook asks about it (`CLAUDE_HOOK_MAX_FILES` and `CLAUDE_HOOK_MAX_BYTES` change the caps).
- A file written and added in the same command does not exist when the hook runs, so the hook asks and says to write first, then add and commit in a separate command.

A git error allows the commit: this hook is never the reason a commit fails.

During a merge (`MERGE_HEAD` exists, for example after `git merge --no-commit` or a merge stopped on a conflict), the index holds every file the merged-in branch brought, so a plain scan would flag content that branch already has, such as a fake private key in a test fixture. The hook drops a finding when the merged-in commit's version of the same file already holds that exact line text. A line in neither parent, such as a conflict resolution or an edit made during the merge, is still scanned. A gitleaks finding that spans lines, such as a real private key, is never dropped, because its first line can match while the rest of the key differs; a finding gitleaks puts on one line (a key written as a string literal in a test) is compared by that line alone. The built-in patterns know no span, so they never drop a private-key header. A file the branch renamed is read under its new name, which the merged-in commit does not have, so its lines are still reported. A squash merge or a cherry-pick leaves no `MERGE_HEAD` and is scanned like any commit.

## git-guardrails

`git push` publishes commits. `git reset --hard`, `git clean` and `git branch -D` throw away work that git cannot bring back. In a project that turns this hook on, the user runs these, and the hook denies them before they run:

- `git push`, every form, dry runs included
- `git reset --hard`
- `git clean`, unless it is a dry run (`-n` or `--dry-run`)
- `git branch -D`, and `git branch -d` with `-f` or `--force`, which mean the same

The command is read as shell rather than searched as text, so a quoted argument (`git commit -m "before git push"`), a heredoc body and a comment never match. It finds git behind its own options (`git -C dir push`), after `sudo`, `env`, `command` and similar wrappers, after `if`, `then` or `do`, inside `$( ... )` and backticks, and inside the string of `bash -c`, `sh -c` and `eval`.

It does not follow git aliases (`git p` after `git config alias.p push`), a heredoc fed to a shell (`bash <<EOF`) or a command built from variables. It guards against a session's mistakes and is not a sandbox. `git checkout .` and `git restore .`, which the upstream version also blocks, are allowed here. To change the list, edit `RULES` in your copy of the hook.

Adapted from Matt Pocock's [git-guardrails-claude-code](https://github.com/mattpocock/skills/tree/5beb35b/skills/misc/git-guardrails-claude-code) (MIT), which does the same with a shell script and `grep`.

## loose-files

Sessions leave briefs, exports, drafts, screenshots and spreadsheets in the repository root, where they sit untracked next to the files that belong there. The hook denies creating a new document at the root: a Write of a new file, or a shell command that creates one by redirection, `tee`, `cp` or `mv`. A document is a file with one of these extensions: `md`, `markdown`, `txt`, `html`, `htm`, `svg`, `png`, `jpg`, `jpeg`, `gif`, `webp`, `pdf`, `zip`, `csv`, `tsv`, `xlsx`, `xls`, `doc`, `docx`, `ppt`, `pptx`. The message says project documentation goes in `docs/` and personal notes outside the repository or in a git-ignored folder.

Always allowed: editing a file that already exists, any file below the root, and the usual root files of common projects, such as `README.*`, `CHANGELOG.md`, `CLAUDE.md`, `AGENTS.md`, `LICENSE.*`, `NOTICE*`, `requirements*.txt`, `CMakeLists.txt`, `index.html`, `robots.txt`, `llms.txt` and `favicon.*`. The full list is `ALLOWED_ROOT_GLOBS` in the hook. `cp -t <dir>` and `mv --target-directory=<dir>` are judged by that directory. A project allows more names in `.claude/root-files`: one basename or glob per line (`*` and `?`), with `#` lines and blank lines ignored.

```
# .claude/root-files
NOTICE.md
release-*.md
```

The hook follows `cd`, `pushd` and `popd`. What it cannot resolve it does not judge, because a false block costs more than a missed file: the commands after a `cd` to a variable, a `$( ... )` or `-`, and a target that holds a variable, a glob or a substitution.

## edited-files and stop-check

`edited-files` appends each edited path to a per-session file under the OS temp directory (`claude-hooks/<session id>.edited`). When the response ends, `stop-check` reads it and keeps every edited file that still exists, is inside the project, is not under `node_modules/`, `.git/`, `dist/`, `build/`, `.next/` or `coverage/`, and has the extension `.js`, `.jsx`, `.mjs`, `.cjs`, `.ts`, `.tsx`, `.mts` or `.cts`. With none left, or when nothing was edited, it is silent. It runs the project's own tools from `node_modules/.bin` (never `npx`):

1. `eslint` on those files, only if `node_modules/.bin/eslint` exists and the project has an eslint configuration (`eslint.config.*` or `.eslintrc*`).
2. The type check, only if at least one of the files is TypeScript, `tsconfig.json` exists at the project root and `node_modules/.bin/tsc` exists. If `node_modules/.bin/next` exists too, `next typegen` runs first, because Next's route types are generated and often git-ignored. Then `tsc --noEmit`. `tsc` takes no file list (a list would ignore `tsconfig.json`), so it checks the whole project.

A tool that does not apply is skipped, not reported. There is no format step.

A failure exits 2, which blocks the stop and hands the tool output to the model. If the hook already blocked once in the same turn (`stop_hook_active`), it lets the turn end and shows you a one-line `systemMessage`, so a failure it cannot fix does not loop. The file list is kept until every tool passes, but after that let-go the checks wait for the next edit: a later reply that edits nothing is not blocked over the old failure. The tools share a 100-second budget (`CLAUDE_HOOK_STOP_BUDGET_MS` overrides it, for tests).

One limit: `edited-files` watches the Edit and Write tools only, so a file written by a shell redirection is not recorded.

## supervisor-reminder

A CLAUDE.md line that says to use the `supervisor` skill for multi-step work is easy to skip: it is read once at startup, far from the request, and a session that skips it runs every step on the session model, with no subagent and no review before the pull request. This hook puts the reminder next to the user's first request instead. What a UserPromptSubmit hook prints with exit 0 is added to the model's context, so it adds one line: call the Skill tool with `supervisor` before a task with more than one step, send lookups to `scout`, tickets to `implementer`, tests to `tester` and every diff to `reviewer`, and skip all of it for a one-step ask.

It fires once per session, marked by a file under the OS temp directory (`claude-hooks/<session id>.supervisor`). A prompt that starts with `/` is a route the user already chose, so it gets no reminder and does not use it up. An empty or malformed event prints nothing.

## cloud-browsers

A Claude Code cloud session starts in a fresh container with an older Chromium, and its network blocks `cdn.playwright.dev`, so `npx playwright install` fails and Playwright tests cannot run. Playwright's Chromium is a Chrome for Testing build, which Google also serves from `storage.googleapis.com`. In a cloud session (`CLAUDE_CODE_REMOTE` is `true`) this hook fetches the builds the project's Playwright expects from there:

1. It acts only in a project that declares `playwright`, `@playwright/test` or `playwright-core` in `package.json`, or already has `node_modules/playwright-core/browsers.json`. Playwright pulled in by another package is not detected.
2. When `node_modules/playwright-core/browsers.json` is missing and the project has a `package-lock.json`, it runs `npm ci`. With a pnpm, Yarn or Bun lockfile it runs nothing and says to install the dependencies with that tool first; with no lockfile it says so.
3. It needs playwright-core 1.57 or newer, the first version that launches the Chrome for Testing folder layout. With an older or unreadable version it installs nothing and says so.
4. It reads the `chromium` and `chromium-headless-shell` revisions from `playwright-core/browsers.json`, so a Playwright upgrade needs no change here, and downloads each build that is not installed yet.
5. Each build is unpacked into a staging folder of its own and moved into place with one rename, so it appears whole or not at all. When a folder is already at that path without Playwright's completion marker, the hook leaves it alone and reports it.

It runs only on Linux x64, needs `curl` and `unzip`, and keeps everything inside a 540-second budget, because Claude Code puts no timeout on an async hook. Register it with `"async": true` so the session starts while it works. Claude Code passes its one JSON line to the model on the next turn: which builds it installed, or why the tests cannot run yet. A Playwright test started before that line arrives can race the install. Outside a cloud session, and when there is nothing to do, it is silent. It never fails the session start.

`CLOUD_BROWSERS_CFT_BASE` points it at another Chrome for Testing mirror; the tests use it to serve zips from a local folder.

## Environment variables

| Variable | Hook | Effect |
| --- | --- | --- |
| `CLAUDE_HOOK_GITLEAKS` | commit-secrets | `off` skips gitleaks and uses the built-in patterns |
| `CLAUDE_HOOK_GITLEAKS_BIN` | commit-secrets | Path of the gitleaks binary, default `gitleaks` on `PATH` |
| `CLAUDE_HOOK_MAX_FILES` | commit-secrets | Cap on untracked files scanned, default 400 |
| `CLAUDE_HOOK_MAX_BYTES` | commit-secrets | Cap on scanned text in bytes, default 4 MB |
| `CLAUDE_HOOK_STOP_BUDGET_MS` | stop-check | Time budget for all tools, default 100000 |
| `CLOUD_BROWSERS_CFT_BASE` | cloud-browsers | Chrome for Testing mirror, default `https://storage.googleapis.com/chrome-for-testing-public` |

## Turning them on

Add the entries you want to `.claude/settings.local.json` (one project, git-ignored) or `~/.claude/settings.json` (every project), with the path to your copy of this repository. Claude Code reads them when a session starts. Remove an entry to turn that hook off.

```json
{
  "hooks": {
    "PreToolUse": [
      {
        "matcher": "Edit|Write|MultiEdit|NotebookEdit|Bash",
        "hooks": [
          { "type": "command", "command": "node /path/to/claude-skills/hooks/config-protection.mjs", "timeout": 10 }
        ]
      },
      {
        "matcher": "Bash",
        "hooks": [
          { "type": "command", "command": "node /path/to/claude-skills/hooks/commit-secrets.mjs", "timeout": 60 }
        ]
      },
      {
        "matcher": "Bash",
        "hooks": [
          { "type": "command", "command": "node /path/to/claude-skills/hooks/git-guardrails.mjs", "timeout": 10 }
        ]
      },
      {
        "matcher": "Write|Bash",
        "hooks": [
          { "type": "command", "command": "node /path/to/claude-skills/hooks/loose-files.mjs", "timeout": 10 }
        ]
      }
    ],
    "PostToolUse": [
      {
        "matcher": "Edit|Write|MultiEdit",
        "hooks": [
          { "type": "command", "command": "node /path/to/claude-skills/hooks/edited-files.mjs", "timeout": 10 }
        ]
      }
    ],
    "Stop": [
      {
        "hooks": [
          { "type": "command", "command": "node /path/to/claude-skills/hooks/stop-check.mjs", "timeout": 120 }
        ]
      }
    ],
    "UserPromptSubmit": [
      {
        "hooks": [
          { "type": "command", "command": "node /path/to/claude-skills/hooks/supervisor-reminder.mjs", "timeout": 10 }
        ]
      }
    ],
    "SessionStart": [
      {
        "matcher": "startup|resume",
        "hooks": [
          { "type": "command", "command": "node /path/to/claude-skills/hooks/cloud-browsers.mjs", "async": true }
        ]
      }
    ]
  }
}
```

A cloud session starts from the repository with no personal settings, so `cloud-browsers` only runs there when it is registered in the project's committed `.claude/settings.json`, with a path inside the repository. Copy `lib.mjs` next to it: the hook imports it, and since the hook is async, a missing import fails without a word. Outside a cloud session it costs one `node` launch and exits.

`edited-files` and `stop-check` work as a pair: turn on both or neither.

The settings file that registers the hooks is not itself protected, so a session could unregister a hook for the sessions after it. Review changes to it.

## Running one by hand

Run a hook with an event on stdin and the project root in `CLAUDE_PROJECT_DIR`. This one prints a deny when the directory has a `tsconfig.json`:

```bash
echo '{"tool_name":"Edit","tool_input":{"file_path":"tsconfig.json"}}' \
  | CLAUDE_PROJECT_DIR=$PWD node /path/to/claude-skills/hooks/config-protection.mjs
```

The same event with `src/app.ts` prints nothing. This one prints a deny from `git-guardrails`:

```bash
echo '{"tool_name":"Bash","tool_input":{"command":"git push origin main"}}' \
  | node /path/to/claude-skills/hooks/git-guardrails.mjs
```

## Tests

```bash
node --test tests/hooks/
```

The suites use Node's built-in runner, so there is nothing to install. Each one runs the script as Claude Code does, with the event as JSON on stdin (`tests/hooks/hookRun.mjs`). The `commit-secrets` tests start git in temporary repositories, and the gitleaks cases are skipped when `gitleaks` is not installed. The `git-guardrails` tests include an event written to stdin in two parts with a pause between them. The `stop-check` tests use small stand-ins for eslint, `tsc` and `next`, not the real tools. The `cloud-browsers` tests build their own zips and serve them from a local folder, so nothing is downloaded; the install cases run only on Linux x64 and are skipped elsewhere. `tests/hooks/index.js` only exists so the directory form of the command works on Node 22 as well as Node 20.
