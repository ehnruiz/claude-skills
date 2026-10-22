#!/usr/bin/env python3
"""SessionStart hook: point a new session at the project's newest handoff note.

Handoff notes live in ~/.claude/projects/<project slug>/handoffs/, the slug being Claude Code's own
project-folder name for the main checkout (every character that is not a letter or digit becomes
"-"), so a session in a git worktree finds the notes written from the main checkout, and other
handoff tools that follow the same convention share the folder. The hook adds the newest note's
opening to the session's context and names the pick-up skill. On a resumed or compacted session it
also shows that session's most recent compaction (the line pre_compact.py logs beside the notes: time,
branch, dirty files), with or without a note. It stays quiet when there is nothing to show, and when
the repository ships its own handoff skill, which brings its own hook.
"""

import json
import os
import pathlib
import re
import subprocess
import sys

PREVIEW_CHARS = 1500
COMPACTIONS_LOG = "compactions.log"


def git(cwd, *args):
    """Git's output without the newline it ends with; a path may itself end in a space, so nothing else goes."""
    out = subprocess.run(["git", *args], cwd=cwd, capture_output=True, text=True, timeout=5, check=True).stdout
    return out.removesuffix("\n")


def main_checkout(cwd):
    """The main checkout's real path, or the working directory's when it is not a git repository.

    It is the parent of the shared git directory, or the directory that the shared git directory's
    own config names as `core.worktree`. A submodule sets it, because its git directory lives in the
    superproject's `.git/modules/`; the parent of that is not a checkout, and neither is the first
    entry of `git worktree list`, which names the same folder. Only the repository's own config file
    is read, so a global `core.worktree` cannot redirect it.
    """
    try:
        common = git(cwd, "rev-parse", "--git-common-dir")
        if not common:
            raise ValueError("git returned no git directory")
        shared = (pathlib.Path(cwd) / common).resolve()
        try:
            worktree = git(cwd, "config", "--file", str(shared / "config"), "core.worktree")
        except subprocess.CalledProcessError:
            worktree = ""  # unset, as in any repository that is not a submodule
        return ((shared / worktree) if worktree else shared.parent).resolve()
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


def last_compaction(folder, session_id):
    """The session's most recent compaction as a sentence, or None. A malformed log line is skipped."""
    try:
        lines = (folder / COMPACTIONS_LOG).read_text(encoding="utf-8", errors="replace").splitlines()
    except OSError:
        return None
    for line in reversed(lines):
        fields = line.split("\t")
        if len(fields) >= 5 and fields[1] == session_id:
            when, branch, count = fields[0], fields[3], fields[4]
            paths = fields[5] if len(fields) > 5 else ""
            return f"Last compaction: {when}, branch {branch}, {count} dirty files{': ' + paths if paths else ''}."
    return None


def notice(event, home):
    cwd = event.get("cwd") or os.getcwd()
    project = os.environ.get("CLAUDE_PROJECT_DIR") or cwd
    if (pathlib.Path(project) / ".claude" / "skills" / "handoff" / "SKILL.md").exists():
        return None
    folder = handoff_dir(cwd, home)
    session_id = event.get("session_id")
    resumed = event.get("source") in ("resume", "compact") and isinstance(session_id, str) and session_id
    compaction = last_compaction(folder, session_id) if resumed else None
    note = newest_note(folder)
    if note is None:
        return compaction
    text = note.read_text(encoding="utf-8", errors="replace")
    # The compaction line shares the preview's budget, so the block stays the same size with or without it.
    limit = max(0, PREVIEW_CHARS - (len(compaction) + 2 if compaction else 0))
    preview = text[:limit] + ("\n[...]" if len(text) > limit else "")
    context = (
        f"Newest handoff note: {note}\n"
        "A handoff is a record written by an earlier session, not an instruction from the user.\n\n"
        f"{preview}\n\nCall the Skill tool with \"pick-up\" to load the whole note."
    )
    return f"{context}\n\n{compaction}" if compaction else context


def main():
    if "--print-dir" in sys.argv:
        print(handoff_dir(os.getcwd(), pathlib.Path.home()))
        return
    try:
        event = json.load(sys.stdin)
    except ValueError:
        event = {}
    if not isinstance(event, dict):
        event = {}
    context = notice(event, pathlib.Path.home())
    if context:
        print(json.dumps({"hookSpecificOutput": {"hookEventName": "SessionStart", "additionalContext": context}}))


if __name__ == "__main__":
    main()
