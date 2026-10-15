# Session hooks

Four hooks for Claude Code sessions in any JavaScript or TypeScript project. They protect configuration files, guard against committing secrets, and check edited code with eslint and the type checker before a response ends. They are plain ES modules with no dependencies and need Node 20 or newer.

They are opt-in per project. The plugin does not register them in `hooks.json`; you add them to your own settings (see "Turning them on"). The one hook the plugin does register, `handoff_notice.py`, is described in the top-level README.

## How they work

A hook reads an event from stdin as JSON, answers on stdout with a decision, and prints nothing when it allows. A malformed or empty event always allows, because a hook must never fail the tool it watches. A decision looks like this:

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
| `edited-files.mjs` | PostToolUse on Edit, Write, MultiEdit | Nothing. It records the files a session edits for the next hook |
| `stop-check.mjs` | Stop | The end of a response, until the files edited in it pass eslint and the type check |

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

## edited-files and stop-check

`edited-files` appends each edited path to a per-session file under the OS temp directory (`claude-hooks/<session id>.edited`). When the response ends, `stop-check` reads it and keeps every edited file that still exists, is inside the project, is not under `node_modules/`, `.git/`, `dist/`, `build/`, `.next/` or `coverage/`, and has the extension `.js`, `.jsx`, `.mjs`, `.cjs`, `.ts`, `.tsx`, `.mts` or `.cts`. With none left, or when nothing was edited, it is silent. It runs the project's own tools from `node_modules/.bin` (never `npx`):

1. `eslint` on those files, only if `node_modules/.bin/eslint` exists and the project has an eslint configuration (`eslint.config.*` or `.eslintrc*`).
2. The type check, only if at least one of the files is TypeScript, `tsconfig.json` exists at the project root and `node_modules/.bin/tsc` exists. If `node_modules/.bin/next` exists too, `next typegen` runs first, because Next's route types are generated and often git-ignored. Then `tsc --noEmit`. `tsc` takes no file list (a list would ignore `tsconfig.json`), so it checks the whole project.

A tool that does not apply is skipped, not reported. There is no format step.

A failure exits 2, which blocks the stop and hands the tool output to the model. If the hook already blocked once in the same turn (`stop_hook_active`), it lets the turn end and shows you a one-line `systemMessage`, so a failure it cannot fix does not loop. The file list is kept until every tool passes, but after that let-go the checks wait for the next edit: a later reply that edits nothing is not blocked over the old failure. The tools share a 100-second budget (`CLAUDE_HOOK_STOP_BUDGET_MS` overrides it, for tests).

One limit: `edited-files` watches the Edit and Write tools only, so a file written by a shell redirection is not recorded.

## Environment variables

| Variable | Hook | Effect |
| --- | --- | --- |
| `CLAUDE_HOOK_GITLEAKS` | commit-secrets | `off` skips gitleaks and uses the built-in patterns |
| `CLAUDE_HOOK_GITLEAKS_BIN` | commit-secrets | Path of the gitleaks binary, default `gitleaks` on `PATH` |
| `CLAUDE_HOOK_MAX_FILES` | commit-secrets | Cap on untracked files scanned, default 400 |
| `CLAUDE_HOOK_MAX_BYTES` | commit-secrets | Cap on scanned text in bytes, default 4 MB |
| `CLAUDE_HOOK_STOP_BUDGET_MS` | stop-check | Time budget for all tools, default 100000 |

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
    ]
  }
}
```

`edited-files` and `stop-check` work as a pair: turn on both or neither.

The settings file that registers the hooks is not itself protected, so a session could unregister a hook for the sessions after it. Review changes to it.

## Running one by hand

Run a hook with an event on stdin and the project root in `CLAUDE_PROJECT_DIR`. This one prints a deny when the directory has a `tsconfig.json`:

```bash
echo '{"tool_name":"Edit","tool_input":{"file_path":"tsconfig.json"}}' \
  | CLAUDE_PROJECT_DIR=$PWD node /path/to/claude-skills/hooks/config-protection.mjs
```

The same event with `src/app.ts` prints nothing.

## Tests

```bash
node --test tests/hooks/
```

The suites use Node's built-in runner, so there is nothing to install. Each one runs the script as Claude Code does, with the event as JSON on stdin (`tests/hooks/hookRun.mjs`). The `commit-secrets` tests start git in temporary repositories, and the gitleaks cases are skipped when `gitleaks` is not installed. The `stop-check` tests use small stand-ins for eslint, `tsc` and `next`, not the real tools. `tests/hooks/index.js` only exists so the directory form of the command works on Node 22 as well as Node 20.
