---
name: skill-collision-audit
description: Find skills whose descriptions fight over the same request, so Claude picks one at random or the wrong one, and fix the descriptions. Use when the user asks to audit, clean up or deduplicate their skills, when a skill fires that should not have, after installing a plugin, or before adding a new skill to a large library.
---

# Skill collision audit

Claude chooses a skill by reading descriptions. Two descriptions that answer the same request, such as "what should I post?" for both a content-strategy skill and a social-calendar skill, make a **collision**: the choice depends on wording luck, and the wrong skill costs tokens and time. This audit finds collisions and makes each request route to exactly one skill.

## Steps

1. **List the libraries.** Ask which ones count, or take the defaults: `~/.claude/skills`, the project's `.claude/skills`, and the skills of enabled plugins. A marketplace cache holds plugins that are not installed; leave it out unless the user wants it.
2. **Shortlist candidates** with the script, which scores every pair of descriptions by shared distinctive words:

   ```bash
   python3 scripts/find_collisions.py ~/.claude/skills .claude/skills
   ```

   The path is relative to this skill's folder. `--threshold` (default 0.25) widens or narrows the list; `--json` gives machine-readable output. It also lists **duplicate names**: the same skill installed twice, where only one copy loads. Skills marked `disable-model-invocation: true` only run when typed by name, so they are never paired.
3. **Judge each pair.** A high score is a reason to look, not a verdict. For each pair, read both descriptions in full and write three realistic requests a user would type for either one. For each request, decide which skill should fire.
   - **Collision:** at least one request could reasonably fire either skill, and neither description tells Claude which to prefer.
   - **Resolved:** the descriptions already draw the line, typically with a "not for …, use the X skill" clause, or by triggering on different nouns.
   - **Fine:** they share vocabulary but serve different requests.
4. **Fix each collision** with the smallest change that works, in this order of preference:
   1. **Draw the line in both descriptions.** Each names its own trigger and points to the other for the neighboring case: "For a month of scheduled posts, use `social-calendar`."
   2. **Narrow a trigger.** Replace a broad phrase ("what to post") with the specific one that skill handles ("a posting plan for this month").
   3. **Merge** two skills that do the same job, keeping the better body.
   4. **Remove** a duplicate or an unused skill.
5. **Show the user** a table of pairs (verdict, the request that collides, the proposed fix) before changing anything. A skill from a plugin the user does not maintain gets a suggested change to send upstream, not an edit to the installed copy, which an update would overwrite.
6. **Check the fix.** Run the script again and replay the three requests against the new descriptions. Each one now routes to a single skill.

## Writing descriptions that route cleanly

- Lead with what the skill does, then list the distinct situations that should trigger it, one phrase each.
- When a neighbor exists, name it and say where the boundary is.
- Keep a description about one job. A skill that routes to other skills says so ("routes each email to the skill that handles it") so it does not compete with them.
