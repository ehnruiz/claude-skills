#!/usr/bin/env python3
"""PreCompact hook: log each compaction beside the project's handoff notes.

One tab-separated line per compaction goes to `compactions.log` in the handoff folder (the same
folder handoff_notice.py reads, found by the same function): ISO time (UTC), session id, trigger,
branch, dirty-file count, and the first 10 dirty paths. The log keeps its last 200 lines.
handoff_notice.py shows the session's newest line when the session resumes or restarts after a
compaction, because the branch and the dirty files at that moment are what a compaction loses.
Prints nothing and never fails: bookkeeping must not block a compaction.

Run by path, Python puts this script's folder first on `sys.path`, which is how it imports
handoff_notice for `handoff_dir`.
"""

import datetime
import json
import os
import pathlib
import re
import subprocess
import sys

from handoff_notice import COMPACTIONS_LOG, git, handoff_dir

MAX_LINES = 200
MAX_PATHS = 10


def field(value):
    """A value safe for one tab-separated field."""
    return re.sub(r"[\t\r\n]", " ", str(value))


def record(event, home):
    if not isinstance(event, dict):
        return
    session_id = event.get("session_id")
    if not isinstance(session_id, str) or not session_id:
        return
    cwd = event.get("cwd")
    cwd = cwd if isinstance(cwd, str) and cwd else os.getcwd()
    try:
        branch = git(cwd, "rev-parse", "--abbrev-ref", "HEAD").strip() or "-"
        dirty = [line[3:].strip() for line in git(cwd, "status", "--porcelain", "-uall").split("\n") if line]
    except (OSError, subprocess.SubprocessError):
        branch, dirty = "-", []
    trigger = event.get("trigger")
    now = datetime.datetime.now(datetime.timezone.utc).isoformat(timespec="milliseconds").replace("+00:00", "Z")
    line = "\t".join(map(field, [
        now, session_id, trigger if isinstance(trigger, str) and trigger else "-", branch, len(dirty),
        " ".join(dirty[:MAX_PATHS]),
    ]))
    folder = handoff_dir(cwd, home)
    folder.mkdir(parents=True, exist_ok=True)
    log = folder / COMPACTIONS_LOG
    with open(log, "a", encoding="utf-8") as out:
        out.write(line + "\n")
    lines = [l for l in log.read_text(encoding="utf-8", errors="replace").split("\n") if l]
    if len(lines) > MAX_LINES:
        log.write_text("\n".join(lines[-MAX_LINES:]) + "\n", encoding="utf-8")


def main():
    try:
        record(json.load(sys.stdin), pathlib.Path.home())
    except Exception:  # noqa: BLE001 - a bookkeeping failure must never block compaction
        pass


if __name__ == "__main__":
    main()
