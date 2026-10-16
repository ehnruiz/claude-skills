# Install, requirements and checks

## What you need

| Component | Needs | Why |
| --- | --- | --- |
| Skills and subagents | Claude Code | Skills are `SKILL.md` folders and subagents are Markdown files with frontmatter; nothing runs outside Claude Code |
| `handoff_notice.py` hook, `ticket_lint.py`, `find_collisions.py` | Python 3.9 or newer, `git` on `PATH` | The scripts use `str.removesuffix`, which arrived in 3.9. On Python 3.8 the test suite fails with `AttributeError: 'str' object has no attribute 'removesuffix'` |
| The four opt-in hooks under `hooks/*.mjs` | Node 20 or newer, `git` on `PATH` | Plain ES modules with no dependencies. `gitleaks` is optional for `commit-secrets` |

Tested on 2026-10-16:

| Platform | Python | Node | Claude Code | Result |
| --- | --- | --- | --- | --- |
| macOS 15 (Darwin 24.6) | 3.14.7 | 22.23.2 and 20.20.0 | 2.1.296 | Python suite 33 tests OK, Node suite 253 tests pass on both Node versions, plugin install and validate OK |
| Ubuntu (GitHub Actions) | 3.9 and 3.12 | 20 and 22 | not used | The CI matrix in `.github/workflows/test.yml` |

Windows is untested. The Node hooks are started as `node <script>`, which the Claude Code hooks reference says works on every platform. The SessionStart hook is started as `python3 <script>`, and the docs give no guidance on the Python interpreter name on Windows, so expect to edit that command if `python3` is not on your `PATH`.

The `skills` CLI (`npx skills add ehnruiz/claude-skills`) can also copy the skills into other coding agents with `--agent`. Only the skills travel that way: the subagents and hooks are Claude Code features. Running it with `--list` was verified against this repository (21 skills found); installs into other agents were not.

## Three ways to install

**As a plugin**, from inside Claude Code:

```
/plugin marketplace add ehnruiz/claude-skills
/plugin install agent-workflow@ehnruiz
```

The second command opens the plugin's details so you can pick a scope (user, project or local) before it installs. From a shell the same two steps are:

```bash
claude plugin marketplace add ehnruiz/claude-skills
claude plugin install agent-workflow@ehnruiz
```

The ids come from the manifests: `agent-workflow` is `name` in `.claude-plugin/plugin.json`, `ehnruiz` is `name` in `.claude-plugin/marketplace.json`. A plugin's skills are namespaced, so once installed the slash commands read `/agent-workflow:handoff`, `/agent-workflow:pick-up` and so on, and the subagents register as `agent-workflow:scout`, `agent-workflow:reviewer` and so on. A skill of the same name in `~/.claude/skills/` or a project's `.claude/skills/` loads alongside the plugin's copy rather than replacing it, which is why `supervisor`, `handoff`, `pick-up`, `tdd` and `diagnosing-bugs` each start by checking for a project copy.

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

Both suites exit 0 on a clean checkout. The Node suite takes a few minutes because the `commit-secrets` tests create real git repositories in a temporary directory.

## Updating

Updates for a third-party marketplace are not automatic unless you turn auto-update on in `/plugin`, Marketplaces tab. To pull a new version:

```bash
claude plugin marketplace update ehnruiz
claude plugin update agent-workflow@ehnruiz
```

Claude Code decides whether there is an update by comparing the `version` in `plugin.json`, so every release that changes a skill, agent or hook bumps that field. [releasing.md](releasing.md) has the rule.
