# Releases and the changelog

## What a version means here

`version` in `.claude-plugin/plugin.json` is the only version this repository has. Claude Code compares that string to decide whether an installed user gets an update, so a release is any change to that string on `main`.

Bump it when a skill, subagent, hook or script changes in a way a user would notice:

| Change | Bump |
| --- | --- |
| A new skill, subagent or hook | minor (1.5.0 to 1.6.0) |
| A skill's description or body changes what it does or when it fires; a hook blocks something new or stops blocking something | minor |
| A fix to a script or hook with no new behavior; a wording fix inside a skill | patch (1.5.0 to 1.5.1) |
| Removing or renaming a skill, subagent or hook, or changing a hook's event or decision | major |
| README, docs, tests, CI | no bump |

Docs-only changes get no bump because they ship nothing to an installed user. The flip side: a change to a `SKILL.md` without a bump is invisible to plugin users until the next release, so do not leave one sitting on `main` unreleased for long.

## The release steps

1. In the pull request that makes the change, bump `version` in `plugin.json` and move the entries from the changelog's `Unreleased` section into a new heading for that version, dated the day of the merge.
2. After the merge, tag the merge commit on `main`:

   ```bash
   git tag -a v1.6.0 -m "1.6.0"
   git push origin v1.6.0
   ```

   Tags are `v<version>`, the form of the one existing tag, `v1.5.0`. Before tagging, `claude plugin tag --dry-run .` checks that `plugin.json` and the marketplace entry agree; it proposes a tag of its own form (`agent-workflow--v1.6.0`), which this repository does not use.
3. Create a GitHub release from the tag with that version's changelog section as the body:

   ```bash
   gh release create v1.6.0 --title "1.6.0" --notes-file <(sed -n '/^## 1.6.0/,/^## /p' CHANGELOG.md | sed '$d')
   ```

4. Installed users pick it up with `claude plugin marketplace update ehnruiz` then `claude plugin update agent-workflow@ehnruiz`.

## The changelog

`CHANGELOG.md` follows the Keep a Changelog layout: an `Unreleased` section at the top, then one section per version, newest first, each dated. Entries say what changed for a user of the skill, not which files moved.

Versions 1.0.0 to 1.4.0 were never tagged. Their sections in the changelog come from the commits that bumped `plugin.json`, with the date and hash of each, so the history is real but the tags do not exist. They will not be created after the fact: a tag added today for a commit from last week would look like a release that happened then.

A test in `tests/test_layout.py` checks that `CHANGELOG.md` has a heading for the version in `plugin.json`, so a bump without a changelog entry fails CI.
