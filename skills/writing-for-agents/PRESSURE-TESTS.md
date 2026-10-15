# Pressure tests

The test branch of [`writing-for-agents`](SKILL.md): how to prove that a new or reworded rule
changes what an agent does before it ships. It is `tdd` applied to prose. The test is a
**scenario** run in a fresh subagent; red is the agent breaking the rule, green is the agent
keeping it. Run it from the main session: a subagent that cannot spawn agents cannot test its
own rule.

The method follows Jesse Vincent's `testing-skills-with-subagents` in
[obra/clank](https://github.com/obra/clank) (read 2026-10-14). This file is a rewrite; no text is
copied.

## What earns a test

A **discipline rule** earns one: a rule with a cost the agent can see right now and a payoff it
cannot. "Judge a gate by its exit code, never through a pipe", "every heuristic ships with a
negative test", "verify before you report done". Under time pressure or near the finish line, an
agent explains these rules away. Reference material (a routing table, a list of files to touch)
has nothing to explain away, and its test is whether the agent can find the fact. That is the
pointer's job, covered in `SKILL.md`.

## The cycle

Run each scenario three times, each time in a fresh agent: one run is a sample, not a result.

1. **Red.** Run the scenario without the rule (see Isolation). If any run breaks it, copy that
   run's reasoning **verbatim**: the exact excuse ("the output is long, so tail is fine here") is
   what the rule has to answer.
2. **Green.** Write the rule against the excuses you collected, in the positive form `SKILL.md`
   asks for: the target behavior and why. Run the same scenario with the rule in place. Green is
   all three runs keeping the rule and naming it as the reason.
3. **Refactor.** Write a new scenario with a different mix of pressures. A fresh excuse goes into
   the document, and then **every** scenario is re-run, the old ones included, because a new
   counter can weaken an old one. Stop when two new scenarios in a row turn up no fresh excuse.

If no red run breaks the rule in an isolated setup, the rule is a **no-op** (see Pruning in
`SKILL.md`) and earns no line. The cycle is done when every scenario is green on the **cheapest
model that reads the document**. A rule in `AGENTS.md` or `CLAUDE.md` reaches every agent,
including the cheap subagents, and a cheaper model explains rules away in its own ways, so a pass
on the strongest model proves nothing for the cheapest.

## Writing a scenario

- **Stack three or more pressures.** One pressure alone rarely breaks a rule. Mix them: time
  ("the release goes out in ten minutes"), sunk cost ("four hours in, everything else works"),
  authority ("the lead said to just ship it"), the finish line ("this is the last step"), and
  making it look reasonable ("the full output is 3,000 lines").
- **Make the agent act.** End with a forced choice between options, or with a task the agent
  carries out. Ask "choose and act", never "what should one do": an agent asked for its opinion
  recites the rule, while an agent asked to act shows what it actually does.
- **Make it concrete.** Use real file names, real commands and a real branch from the
  repository. A vague scenario gets a vague answer.
- **Say the facts are given.** An agent checks the scenario against the checkout, and an invented
  test, field or failure it cannot find becomes the finding ("this test does not exist") instead
  of the rule. Open with "the facts below are given and are not in this checkout".

## Isolation

A subagent gets the session's instructions wherever it runs: the project's `AGENTS.md` or
`CLAUDE.md`, the user's own global instructions, any memory the agent loads, its agent definition
and the skills it preloads. Before a red run, find every place the rule is stated (search the
repository and the user's agent folder). Delete the project copies in a throwaway worktree and
run the red scenario there. When a copy lives in a layer you cannot take out for one run (the
user's own global instructions or memory), every red run keeps the rule and proves nothing. Mark
the scenario **unisolated** and keep the rule: a teammate without those personal files still
needs it.

## Keeping the evidence

Put the scenarios, the verbatim red excuses and the green results in the PR description that
changes the rule, under **Pressure tests**. When the rule is edited later, those scenarios are
its regression suite: re-run them against the new wording.
