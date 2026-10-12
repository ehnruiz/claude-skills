# claude-skills

Skills and subagents for [Claude Code](https://docs.claude.com/en/docs/claude-code) that I use every day. They do five jobs:

- **Spend less on each task.** The main session plans and checks the work. Cheaper models do the searching, writing and building.
- **Pick up where you left off.** A session writes a handoff note, and the next session continues from it without re-asking.
- **Write tickets people read.** A short description for the product owner, with the engineering detail in the first comment.
- **Keep a growing skill library predictable.** Find skills that compete for the same request before they misfire.
- **Publish under your own name.** No "generated with" footers or AI co-author lines in your commits and PRs.

## Install

As a plugin, from inside Claude Code:

```
/plugin marketplace add ehnruiz/claude-skills
/plugin install agent-workflow@ehnruiz
```

Or copy only the parts you want:

```bash
git clone https://github.com/ehnruiz/claude-skills
cp -R claude-skills/skills/ticket-write ~/.claude/skills/
cp claude-skills/agents/*.md ~/.claude/agents/
```

## Skills

| Skill | What it does |
| --- | --- |
| [`supervisor`](skills/supervisor/SKILL.md) | Splits a multi-step task into tickets for cheaper subagents, in a fixed order: clarify, scout, plan, build, verify, review, ship. A subagent that fails twice moves up one model tier. |
| [`handoff`](skills/handoff/SKILL.md) | Writes a handoff note at the end of a session: the goal, what is done and how it was verified, decisions, open questions and next steps. You invoke it yourself. |
| [`pick-up`](skills/pick-up/SKILL.md) | Loads the newest handoff, checks the repository for anything that changed since, and starts on the next step. |
| [`no-ai-attribution`](skills/no-ai-attribution/SKILL.md) | Removes AI credit lines and AI co-author trailers from commits, PRs, issues, comments and tickets before they are posted. |
| [`open-in-my-browser`](skills/open-in-my-browser/SKILL.md) | Opens links you share in your own Chrome through the Claude in Chrome extension, so pages behind your sign-ins and VPN load. |
| [`ticket-write`](skills/ticket-write/SKILL.md) | Drafts bugs, tasks, stories, spikes and epics with word budgets, then checks the draft with a lint script before filing. Works with Jira, Linear or GitHub Issues. |
| [`ticket-trim`](skills/ticket-trim/SKILL.md) | Shortens an existing ticket that is too long. It saves the original text in a comment first. |
| [`ticket-digest`](skills/ticket-digest/SKILL.md) | Turns today's new tickets into one message to paste into chat, one line per ticket. |
| [`skill-collision-audit`](skills/skill-collision-audit/SKILL.md) | Finds skills whose descriptions compete for the same request, then fixes the descriptions so each request goes to exactly one skill. A script shortlists the overlapping pairs and same-name duplicates; Claude judges each pair against realistic requests. |

### Ticket budgets

| Part | Ceiling |
| --- | --- |
| Title | 80 characters |
| Description (bug, task, story) | 150 words |
| Description (spike, epic) | 80 words |
| Acceptance criteria | 2 to 5 bullets |
| Code identifiers in the description | 3 |
| Technical notes comment | 250 words |

`skills/ticket-write/scripts/ticket_lint.py` checks a draft against these limits and lists the identifiers that should move to the technical notes:

```
$ python3 ticket_lint.py --type bug --title "Product pages lose search data" draft.md
ok    title characters: 30 (limit 80)
ok    description words: 36 (limit 150)
ok    headings: 3 (limit 6)
ok    code identifiers: 0 (limit 3)
ok    acceptance criteria: 2 (limit 2-5)
```

### Collision audit

`skills/skill-collision-audit/scripts/find_collisions.py` scores every pair of skill descriptions by the distinctive words they share:

```
$ python3 find_collisions.py ~/.claude/skills .claude/skills
55 skills, 14 overlapping pairs, 0 duplicate names

0.73  built-in-browser  <->  chrome-browser
      shared: browser, tool, mcp, chrome, own, before
```

A high score is a reason to look, not a verdict: that pair already says in both descriptions which browser each one is for. The skill has Claude test each pair with realistic requests and fix only the real collisions.

## Subagents

Each subagent's model is set in its file. The `supervisor` skill sends work to them.

| Agent | Model | Job |
| --- | --- | --- |
| [`scout`](agents/scout.md) | Haiku | Finds things and returns `file:line` pointers. It changes nothing. |
| [`doc-writer`](agents/doc-writer.md) | Haiku | Writes prose from the facts it is given, and writes `TODO(fact)` where a fact is missing. |
| [`implementer`](agents/implementer.md) | Sonnet | Builds one ticket test-first and reports each command's exit code. It does not commit. |
| [`tester`](agents/tester.md) | Sonnet | Writes and runs tests, and touches only tests and fixtures. |
| [`reviewer`](agents/reviewer.md) | Opus | Reviews a diff against the ticket and the repository's standards, giving a failure scenario for each finding. |

## Development

```bash
python3 -m unittest discover -s tests -v
claude plugin validate .
```

The tests check the lint script, the collision script, each skill's and subagent's frontmatter, links inside the skills, and that this README lists every skill and subagent.

## Credits

The handoff skill grew out of the handoff idea in [Matt Pocock's skills](https://github.com/mattpocock/skills).

## License

[MIT](LICENSE)
