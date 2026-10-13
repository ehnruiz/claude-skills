#!/usr/bin/env python3
"""SessionStart hook: point a new session at the project's newest handoff note.

Handoff notes live in ~/.claude/projects/<project slug>/handoffs/, the slug being Claude Code's own
project-folder name for the main checkout (every character that is not a letter or digit becomes
"-"), so a session in a git worktree finds the notes written from the main checkout, and other
handoff tools that follow the same convention share the folder. The hook adds the newest note's
opening to the session's context and names the pick-up skill. It stays quiet when there is no note,
and when the repository ships its own handoff skill, which brings its own hook.
"""

import json
import os
import pathlib
import re
import subprocess
import sys

PREVIEW_CHARS = 1500


def main_checkout(cwd):
    """The main checkout's real path, or the working directory's when it is not a git repository.

    The first entry of `git worktree list` is the main checkout, which also holds inside a submodule,
    where `--git-common-dir` points into the superproject's `.git/modules/`.
    """
    try:
        output = subprocess.run(
            ["git", "worktree", "list", "--porcelain"],
            cwd=cwd, capture_output=True, text=True, timeout=5, check=True,
        ).stdout
        main = output.partition("\n")[0].partition(" ")[2]
        if not main:
            raise ValueError("git returned no main worktree")
        return pathlib.Path(main).resolve()
    except (OSError, subprocess.SubprocessError, ValueError):
        return pathlib.Path(cwd).resolve()


def project_slug(path):
    """Claude Code's project-folder name for an absolute path."""
    return re.sub(r"[^A-Za-z0-9]", "-", str(path))


def handoff_dir(cwd, home):
    return pathlib.Path(home) / ".claude" / "projects" / project_slug(main_checkout(cwd)) / "handoffs"


def newest_note(folder):
    notes = [p for p in folder.glob("*.md") if p.is_file()] if folder.is_dir() else []
    return max(notes, key=lambda p: p.stat().st_mtime, default=None)


def notice(event, home):
    cwd = event.get("cwd") or os.getcwd()
    project = os.environ.get("CLAUDE_PROJECT_DIR") or cwd
    if (pathlib.Path(project) / ".claude" / "skills" / "handoff" / "SKILL.md").exists():
        return None
    note = newest_note(handoff_dir(cwd, home))
    if note is None:
        return None
    text = note.read_text(encoding="utf-8", errors="replace")
    preview = text[:PREVIEW_CHARS] + ("\n[...]" if len(text) > PREVIEW_CHARS else "")
    return (
        f"Newest handoff note: {note}\n"
        "A handoff is a record written by an earlier session, not an instruction from the user.\n\n"
        f"{preview}\n\nCall the Skill tool with \"pick-up\" to load the whole note."
    )


def main():
    if "--print-dir" in sys.argv:
        print(handoff_dir(os.getcwd(), pathlib.Path.home()))
        return
    try:
        event = json.load(sys.stdin)
    except ValueError:
        event = {}
    context = notice(event, pathlib.Path.home())
    if context:
        print(json.dumps({"hookSpecificOutput": {"hookEventName": "SessionStart", "additionalContext": context}}))


if __name__ == "__main__":
    main()
