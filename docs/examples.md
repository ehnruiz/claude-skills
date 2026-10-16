# Three examples, run against this repository

Each example below was run on 2026-10-16 on macOS with Python 3.14.7, Node 22.23.2 and Claude Code 2.1.296. The commands are copied from the terminal and the exit codes are the ones that came back. Nothing here is a mock-up: you can rerun every command from a clone of this repository.

## 1. `regression-proof`: show that a test fails without its fix

The [`regression-proof`](../skills/regression-proof/SKILL.md) skill has two rules. A gate passed only if its exit code was 0, and a regression test counts only if it fails without the fix. The second rule is the one people skip, because it takes a scratch worktree. Here it is applied to a real fix from this repository's history.

Commit `356016b` fixed a bug in the handoff hook: a submodule path that ends in a space was being trimmed. It changed `hooks/handoff_notice.py` and added one test to `tests/test_handoff_notice.py`. The skill's recipe, with `$S` a scratch directory outside the checkout:

```bash
base=$(git rev-parse 356016b^)          # 905dbb7, the parent commit
git worktree add --detach $S/proof "$base"
git diff --name-only --diff-filter=AM "$base" 356016b -- tests/
# tests/test_handoff_notice.py
git -C $S/proof checkout 356016b -- tests/test_handoff_notice.py
```

The worktree now has the parent's code and the fix's tests. Run the test there, then on the fix:

```
$ (cd $S/proof && python3 -m unittest tests.test_handoff_notice); echo "without_fix=$?"
FAIL: test_a_trailing_space_in_a_submodule_path_is_kept (tests.test_handoff_notice.HandoffNoticeTest...)
Ran 11 tests in 5.980s
FAILED (failures=1)
without_fix=1

$ git worktree add --detach $S/proof2 356016b
$ (cd $S/proof2 && python3 -m unittest tests.test_handoff_notice); echo "with_fix=$?"
Ran 11 tests in 4.132s
OK
with_fix=0
```

Verdict, from the skill's table: fails on an assertion without the fix, passes with it, so the test **proves** the fix. That line, with both exit codes, is what goes in the pull request's test plan. A test that had passed both ways would have been rewritten before the PR, because it would not have caught the bug.

Clean up with `git worktree remove --force $S/proof` (and `proof2`). The `--force` is needed because the copied tests are uncommitted changes in the scratch worktree.

What the skill adds over "run the tests": the first rule catches `npm test | grep` and `git push | tail -1`, where the pipe replaces the exit code and a failure reports as success. The skill lists three such cases that happened in a real project and the rule for each.

## 2. `skill-collision-audit`: find skills that compete for the same request

Claude Code picks a skill by reading descriptions. Two descriptions that both answer "what should I post?" make the choice a matter of wording luck. The [`skill-collision-audit`](../skills/skill-collision-audit/SKILL.md) skill ships a script that shortlists the pairs to look at, and then has Claude judge each pair against realistic requests.

Run against this repository's own 21 skills:

```
$ python3 skills/skill-collision-audit/scripts/find_collisions.py skills; echo "exit=$?"
21 skills, 1 overlapping pairs, 0 duplicate names

0.32  explain-the-number  <->  regression-proof
      shared: before, change, test, score, number, eval
      skills/explain-the-number/SKILL.md
      skills/regression-proof/SKILL.md
exit=1
```

The score is the cosine similarity of the two descriptions' TF-IDF vectors, so words every skill uses count for little and shared distinctive words count for a lot. The exit code is 1 whenever a pair is flagged: the script is a shortlist, not a gate, and the judgment is the next step.

Judging the pair by the skill's three verdicts (collision, resolved, fine):

- `regression-proof` ends its description with "For whether a measured number (a score, a timing, an eval result) can be credited to a change, use `explain-the-number`."
- `explain-the-number` ends with "For whether the tests and gates passed, use `regression-proof`."

Each one names the other and says where the line is, so the verdict is **resolved**. The requests "did the tests pass?" and "can I say the change improved the Lighthouse score?" each route to one skill. No edit needed.

On a larger library the shortlist is longer. The README shows a run over 55 skills (a user library plus a project's), where the top pair scored 0.73 and was also already resolved. The second pass the skill asks for, at `--threshold 0.12`, exists because a one-line description shares few words with anything and can collide while scoring low.

## 3. `supervisor`: route the work, keep the judgment

The [`supervisor`](../skills/supervisor/SKILL.md) skill makes the main session a supervisor: it clarifies, plans, verifies and answers, and sends tickets to cheaper subagents. The fixed order is clarify, scout, plan, build, verify, review, ship. The example here is the pull request that added this page, because it is the task whose routing can be checked against the result.

**What stayed with the supervisor.** The skill's rule: keep the work when it is one file and a handful of lines, a lookup you already know where to find, or something you already hold in context. Writing these docs needed facts that were already in the session's context (the README, the manifests, the test runs above), so no `doc-writer` ticket was sent. A Haiku writer would have had to re-read everything and would have had no way to know which commands had actually run.

**What was delegated.** Two tickets:

| Ticket | Agent | Why that tier |
| --- | --- | --- |
| Confirm from the official docs how plugin skills are named in a session, how users get updates, and the precedence between plugin, user and project skills | a docs-reading agent, in the background | A lookup with a citation, independent of the writing, so it ran while the writing continued |
| Review the finished diff against the ask and the repository's standards | `reviewer` (Opus) | The skill sends every diff to the strongest tier before a PR; its findings were fixed and the gates rerun |

A ticket carries the behavior wanted, where it goes, the command that proves it, the files involved and what must stay unchanged. The review ticket named the branch, the ten-point ask, and the constraints (preserve every skill, agent and hook; no unmeasured claims).

**Verification by exit code.** The supervisor ran each gate itself, one command per step, and judged it by its code rather than by a worker's summary:

| Gate | Command | Exit |
| --- | --- | --- |
| Python tests, 3.14 | `python3 -m unittest discover -s tests` | 0 (33 tests) |
| Node hook tests, 22 | `node --test tests/hooks/` | 0 (253 tests) |
| Node hook tests, 20 | `~/.nvm/versions/node/v20.20.0/bin/node --test tests/hooks/` | 0 (253 tests) |
| Plugin manifest | `claude plugin validate .` | 0 |

**Escalation.** None was needed here. The rule when it is: a worker that fails the same ticket twice moves up one tier with the same ticket and its failed report attached, Haiku to Sonnet to Opus to the supervisor. A third attempt on the same model with the same prompt is wasted.

The routing table and the escalation rule are in the skill. What each tier is good and weak at, and how to hand a ticket to an external runner such as Codex, is in [`MODELS.md`](../skills/supervisor/MODELS.md).
