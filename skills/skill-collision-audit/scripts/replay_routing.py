#!/usr/bin/env python3
"""Ask Claude which skill each saved request needs, and fail when one lands somewhere else.

Usage: replay_routing.py --reviewed FILE [--model MODEL] ROOT [ROOT ...]

FILE is the reviewed-pairs.json the audit writes. For each saved request, Claude
sees every skill in the ROOTs that can fire on its own, as a list of names and
descriptions, and names the one it would use, or "none". A request saved with a
skill must reach that skill. A request saved with null must reach neither skill
of its pair: "none" or a third skill both pass.

This approximates Claude Code's routing rather than reproducing it: a real
session has the full system prompt and may answer without any skill, while here
Claude must pick one or say none. It answers whether the descriptions alone
separate the requests.

Each request is one `claude -p` call in safe mode (no CLAUDE.md, skills,
plugins, hooks or MCP servers), with no tools and no saved session, run from an
empty temporary folder. The request goes on stdin, so one that starts with "-"
is not read as a flag. The default model is the one `claude` uses; --model
picks another. The answer can vary between runs, so a request that fails once is
worth replaying before rewriting a description.

Exits 0 when every request lands where it was saved, 1 when one does not,
2 on a usage error, an unknown skill in the file, or a failed `claude` call.
"""

import argparse
import pathlib
import re
import subprocess
import sys
import tempfile

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent))
from find_collisions import load_reviewed, load_skills  # noqa: E402

SYSTEM = """You route requests to skills, the way Claude Code chooses a skill before it starts work.

The following skills are available. A skill is used only when its description fits the request.

{skills}

Reply with only the name of the one skill you would use for the user's request, or none if no skill fits. No other words."""


class ClaudeError(RuntimeError):
    """`claude` could not be run or exited with an error."""


def routable(skills):
    """The skills Claude can pick by description, leaving out those that run only when typed. Two skills
    with one name (two plugins can ship the same name) both stay, so neither competitor is hidden; an
    answer with that name counts as that name."""
    return [skill for skill in skills if not skill.get("manual")]


def system_prompt(skills):
    return SYSTEM.format(skills="\n".join(f"- {s['name']}: {s['description']}" for s in skills))


def read_answer(raw, skills):
    """The skill name or "none" in Claude's reply, or None when it names neither."""
    first = raw.strip().splitlines()[0] if raw.strip() else ""
    word = re.sub(r"^[`'\"*/\s]+|[`'\"*.\s]+$", "", first).lower()
    names = {s["name"].lower(): s["name"] for s in skills}
    if word == "none":
        return "none"
    return names.get(word)


def claude_command(system, model=None):
    """The `claude` call for one request, which goes on stdin. Safe mode and a strict empty MCP config keep
    the user's CLAUDE.md, plugins, hooks and MCP servers out of the answer."""
    command = ["claude", "-p", "--safe-mode", "--strict-mcp-config", "--system-prompt", system, "--tools", "",
               "--disable-slash-commands", "--no-session-persistence", "--output-format", "text"]
    if model:
        command += ["--model", model]
    return command


def ask_claude(model=None, run=subprocess.run):
    """An `ask(system, request)` that runs one `claude -p` call from an empty temporary folder."""

    def ask(system, request):
        with tempfile.TemporaryDirectory() as folder:
            try:
                done = run(claude_command(system, model), input=request, cwd=folder, capture_output=True,
                           text=True, timeout=300)
            except (OSError, subprocess.TimeoutExpired) as error:
                raise ClaudeError(f"could not run claude: {error}") from error
        if done.returncode != 0:
            raise ClaudeError(f"claude exited {done.returncode}: {(done.stderr or done.stdout).strip()[:300]}")
        return done.stdout

    return ask


def replay(skills, reviewed, ask):
    """Replay every saved request and print one line each. Returns the failures."""
    skills = routable(skills)
    system = system_prompt(skills)
    failures = []
    for pair in sorted(reviewed, key=sorted):
        label = " <-> ".join(sorted(pair))
        for request, expect in reviewed[pair]["requests"]:
            raw = ask(system, request)
            got = read_answer(raw, skills)
            if got is None:
                failure = f"{label}: {request!r} named no skill: {raw.strip()[:80]!r}"
            elif expect is None and got in pair:
                failure = f"{label}: {request!r} expected neither skill, got {got}"
            elif expect is not None and got != expect:
                failure = f"{label}: {request!r} expected {expect}, got {got}"
            else:
                failure = None
            print(f"{'FAIL' if failure else 'ok  '} {expect or 'neither':<24} {got or '?':<24} {request}")
            if failure:
                failures.append(failure)
    return failures


def main(argv=None, ask=None):
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("roots", nargs="+")
    parser.add_argument("--reviewed", required=True, help="reviewed-pairs.json with the saved requests")
    parser.add_argument("--model", help="the model `claude -p` uses; default is its own")
    args = parser.parse_args(argv)
    missing = [r for r in args.roots if not pathlib.Path(r).expanduser().is_dir()]
    if missing:
        print("not a directory: " + ", ".join(missing), file=sys.stderr)
        return 2
    if not pathlib.Path(args.reviewed).expanduser().is_file():
        print(f"no reviewed pairs file: {args.reviewed}", file=sys.stderr)
        return 2
    try:
        reviewed = load_reviewed(args.reviewed)
    except ValueError as error:
        print(error, file=sys.stderr)
        return 2

    skills = load_skills(args.roots)
    names = {s["name"] for s in routable(skills)}
    unknown = sorted({n for pair, r in reviewed.items() for n in pair | {e for _, e in r["requests"] if e}} - names)
    if unknown:
        print(f"not in the library: {', '.join(unknown)}", file=sys.stderr)
        return 2

    try:
        failures = replay(skills, reviewed, ask or ask_claude(args.model))
    except ClaudeError as error:
        print(error, file=sys.stderr)
        return 2
    for failure in failures:
        print(f"\n{failure}")
    total = sum(len(r["requests"]) for r in reviewed.values())
    print(f"\n{total - len(failures)} of {total} requests landed where they were saved")
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(main())
