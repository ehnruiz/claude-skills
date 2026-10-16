# Install, requirements and checks

## What you need

| Component | Needs | Why |
| --- | --- | --- |
| Skills and subagents | Claude Code | Skills are `SKILL.md` folders and subagents are Markdown files with frontmatter; nothing runs outside Claude Code |
| `handoff_notice.py` hook, `ticket_lint.py`, `find_collisions.py` | Python 3.9 or newer, `git` on `PATH` | The scripts use `str.removesuffix`, which arrived in 3.9. On Python 3.8 the test suite fails with `AttributeError: 'str' object has no attribute 'removesuffix'` |
| The four opt-in hooks under `hooks/*.mjs` | Node 20 or newer, `git` on `PATH` | Plain ES modules with no dependencies. `gitleaks` is optional for `commit-secrets` |

Measured on 2026-10-16 on macOS 15 (Darwin 24.6) with Claude Code 2.1.296:

| Check | Versions | Result |
| --- | --- | --- |
| `python3 -m unittest discover -s tests` | Python 3.14.7 and 3.9.6 | 38 tests OK, exit 0 on both |
| `python3 -m unittest discover -s tests` | Python 3.8 | exit 1, `AttributeError` on `removesuffix` |
| `node --test tests/hooks/` | Node 22.23.2 and 20.20.0 | 253 tests pass, exit 0 on both |
| `claude plugin validate .`, marketplace add, plugin install, list, details | Claude Code 2.1.296 | exit 0 each |

CI (`.github/workflows/test.yml`) is configured to run both suites on Ubuntu with Python 3.9 and 3.12 and Node 20 and 22, on every push to `main` and every pull request. The badge on the pull request or the Actions tab says whether the last run passed.

Windows is untested. The Node hooks are started as `node <script>`, which the Claude Code hooks reference says works on every platform. The SessionStart hook is started as `python3 <script>`, and that reference gives no guidance on the Python interpreter name on Windows, so expect to edit that command if `python3` is not on your `PATH`.

The `skills` CLI (`npx skills add ehnruiz/claude-skills`) can also copy the skills into other coding agents with `--agent`. Only the skills travel that way: the subagents and hooks are Claude Code features. Running it with `--list` against this repository printed "Found 21 skills" and exited 0; installs into other agents were not run.

## Three ways to install

**As a plugin**, from inside Claude Code:

```
/plugin marketplace add ehnruiz/claude-skills
/plugin install agent-workflow@ehnruiz
```

The Claude Code plugin docs say the second command opens the plugin's details so you can pick a scope (user, project or local) before it installs. From a shell the same two steps are:

```bash
claude plugin marketplace add ehnruiz/claude-skills
claude plugin install agent-workflow@ehnruiz
```

The ids come from the manifests: `agent-workflow` is `name` in `.claude-plugin/plugin.json`, `ehnruiz` is `name` in `.claude-plugin/marketplace.json`. Per the Claude Code docs, a plugin's skills are namespaced, so once installed the slash commands read `/agent-workflow:handoff`, `/agent-workflow:pick-up` and so on, and the subagents register as `agent-workflow:scout`, `agent-workflow:reviewer` and so on. The same docs give the order for same-named skills: a skill in `~/.claude/skills/` outranks one in a project's `.claude/skills/`, and a plugin's copy loads alongside both. That order is why `supervisor`, `handoff`, `pick-up`, `tdd` and `diagnosing-bugs` each start by checking for a project copy, as the README's "Your own copy wins" section explains.

**With the `skills` CLI**, which can also install into other coding agents:

```bash
npx skills add ehnruiz/claude-skills --list        # see the 21 skills without installing
npx skills add ehnruiz/claude-skills --skill tdd   # one skill
npx skills add ehnruiz/claude-skills               # pick from the list
```

**By copying** only the parts you want:

```bash
git clone https://github.com/ehnruiz/claude-skills
cp -R claude-skills/skills/ticket-write ~/.claude/skills/
cp claude-skills/agents/*.md ~/.claude/agents/
```

A copy install does not register the SessionStart hook; the README's Hooks section has the settings snippet for it.

## Check that it worked

After a plugin install, from any shell:

```
$ claude plugin list
Installed plugins:

  ❯ agent-workflow@ehnruiz
    Version: 1.5.0
    Scope: user
    Status: ✔ enabled
```

`claude plugin details agent-workflow@ehnruiz` lists the components the plugin loaded. On 1.5.0 it reports 21 skills, 6 agents and 1 hook (SessionStart), with a projected always-on cost of about 2,400 tokens per session for the skill and agent descriptions. That figure is Claude Code's own estimate and will move as descriptions change.

After a copy install, check the files landed where Claude Code reads them:

```bash
ls ~/.claude/skills/*/SKILL.md ~/.claude/agents/*.md
```

Two of the scripts run on their own, so they are a quick check that Python and the paths are right. From a clone of this repository:

```
$ python3 hooks/handoff_notice.py --print-dir
/Users/you/.claude/projects/-Users-you-projects-claude-skills/handoffs

$ python3 skills/skill-collision-audit/scripts/find_collisions.py skills
21 skills, 1 overlapping pairs, 0 duplicate names
...
```

The first prints where the `handoff` skill will save notes for this checkout. The second exits 1 because one pair is flagged; [docs/examples.md](examples.md) walks through why that pair is fine.

To check a hook by hand, give it an event on stdin. This prints a deny when the current directory has a `tsconfig.json`, and nothing otherwise:

```bash
echo '{"tool_name":"Edit","tool_input":{"file_path":"tsconfig.json"}}' \
  | CLAUDE_PROJECT_DIR=$PWD node hooks/config-protection.mjs
```

## Run the test suite

From a clone:

```bash
python3 -m unittest discover -s tests -v
node --test tests/hooks/
claude plugin validate .
```

Both suites exit 0 on a clean checkout. The Node suite took about four minutes here, because the `commit-secrets` tests create real git repositories in a temporary directory.

## Updating

The Claude Code docs say updates for a marketplace other than Anthropic's are off unless you turn auto-update on under `/plugin`, Marketplaces tab. To pull a new version:

```bash
claude plugin marketplace update ehnruiz
claude plugin update agent-workflow@ehnruiz
```

Claude Code decides whether there is an update by comparing the `version` in `plugin.json`, so every release that changes a skill, agent or hook bumps that field. [releasing.md](releasing.md) has the rule.
