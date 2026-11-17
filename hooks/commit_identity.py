#!/usr/bin/env python3
"""PreToolUse hook on Bash: refuse a git commit that would not be authored and committed by the person.

On 2026-10-15 a session committed as "Claude <noreply@anthropic.com>" and the commits had to be
rewritten and force-pushed. Every commit must carry the person's identity, never an AI one. The hook
looks at the commands that create commits (commit, merge, cherry-pick, revert, rebase, am), works out
the author and committer git would use, including `-c`, `GIT_*` and `--author` overrides written in
the command, and denies the call before the commit exists when either differs from the expected
identity or looks like an AI one.

The expected identity is COMMIT_IDENTITY_EMAIL and COMMIT_IDENTITY_NAME, else the global git config.
With no expected email anywhere (a fresh cloud session) the hook denies and says how to set one.
A malformed event, a directory that is not a repository, or a git error allows: the hook must never
break a command that is not a commit.
"""

import json
import os
import re
import subprocess
import sys

SUBCOMMANDS = ("commit", "merge", "cherry-pick", "revert", "rebase", "am")
VAL = r"""(?:"[^"]*"|'[^']*'|\S+)"""
GIT_OPTIONS = rf"(?:\s+(?:-C\s+{VAL}|-c\s+[\w.-]+={VAL}|--[\w-]+(?:={VAL})?))*"
TRIGGER = re.compile(rf"(?:^|[\s;&|(])git{GIT_OPTIONS}\s+(?:{'|'.join(SUBCOMMANDS)})(?![\w-])")
DIR_C = re.compile(rf"(?:^|[\s;&|(])git\s+(?:-c\s+[\w.-]+={VAL}\s+)*-C\s+({VAL})")
DIR_CD = re.compile(rf"^\s*cd\s+({VAL})\s*(?:&&|;)")
ENV_ASSIGN = re.compile(rf"(?:^|[\s;&|(])(GIT_(?:AUTHOR|COMMITTER)_(?:NAME|EMAIL))=({VAL})")
DASH_C = re.compile(rf"(?:^|\s)-c\s+user\.(name|email)=({VAL})")
AUTHOR = re.compile(r"""--author(?:=|\s+)(?:"([^"]*)"|'([^']*)'|([^<>]*<[^>]*>))""")
IDENT = re.compile(r"^(.*?)\s*<([^<>]*)>\s+\d+\s+[+-]\d{4}\s*$")
NAME_EMAIL = re.compile(r"^\s*(.*?)\s*<([^<>]*)>\s*$")
CLAUDE = re.compile(r"\bclaude\b", re.IGNORECASE)


def run_git(cwd, *args):
    """Git's output without the trailing newline; raises when git fails."""
    out = subprocess.run(["git", *args], cwd=cwd, capture_output=True, text=True, timeout=5, check=True).stdout
    return out.removesuffix("\n")


def unquote(value):
    if len(value) >= 2 and value[0] == value[-1] and value[0] in "\"'":
        return value[1:-1]
    return value


def creates_commit(command):
    if "--dry-run" in command or "--no-commit" in command:
        return False
    return TRIGGER.search(command) is not None


def repo_dir(command, cwd):
    """The directory the git command runs in: `git -C`, else a leading `cd`, else the event's cwd."""
    found = DIR_C.search(command)
    target = unquote(found.group(1)) if found else None
    if target is None:
        found = DIR_CD.match(command)
        target = unquote(found.group(1)) if found else None
    if target is None:
        return cwd
    return os.path.normpath(os.path.join(cwd, os.path.expanduser(target)))


def parse_ident(text):
    """Split `Name <email> <timestamp> <tz>`, reading from the right so a name may hold spaces."""
    found = IDENT.match(text)
    return (found.group(1), found.group(2)) if found else None


def parse_name_email(text):
    found = NAME_EMAIL.match(text)
    return (found.group(1), found.group(2)) if found else (text.strip(), "")


def expected_identity():
    """The (name, email) the person's commits must carry; the email is empty when none is set."""

    def configured(env_key, git_key):
        value = os.environ.get(env_key, "").strip()
        if value:
            return value
        try:
            return run_git(None, "config", "--global", git_key).strip()
        except (OSError, subprocess.SubprocessError):
            return ""

    return configured("COMMIT_IDENTITY_NAME", "user.name"), configured("COMMIT_IDENTITY_EMAIL", "user.email")


def effective_identities(command, cwd):
    """{'author': (name, email), 'committer': (name, email)} as git would record them."""
    result = {}
    for role in ("author", "committer"):
        parsed = parse_ident(run_git(cwd, "var", f"GIT_{role.upper()}_IDENT"))
        if parsed is None:
            raise ValueError("unreadable identity")
        result[role] = list(parsed)
    for key, value in ENV_ASSIGN.findall(command):
        _, role, field = key.split("_")
        result[role.lower()][0 if field == "NAME" else 1] = unquote(value)
    for field, value in DASH_C.findall(command):
        for role in result:
            result[role][0 if field == "name" else 1] = unquote(value)
    found = AUTHOR.search(command)
    if found:
        name, email = parse_name_email(next(group for group in found.groups() if group is not None))
        result["author"] = [name, email]
    return {role: tuple(pair) for role, pair in result.items()}


def problem(name, email, expected_name, expected_email):
    if email.strip().lower().endswith("@anthropic.com") or CLAUDE.search(name) or CLAUDE.search(email):
        return True
    if email.strip().lower() != expected_email.strip().lower():
        return True
    return bool(expected_name) and name.strip() != expected_name.strip()


def describe(name, email):
    return f"{name} <{email}>"


def fix(expected_name, expected_email):
    return (
        f'Set it with: git config user.name "{expected_name or "<your name>"}" && '
        f"git config user.email {expected_email} (add --global for every repository), then retry."
    )


def verdict(command, cwd):
    """A denial reason, or None to allow."""
    if not creates_commit(command):
        return None
    directory = repo_dir(command, cwd)
    try:
        run_git(directory, "rev-parse", "--git-dir")
    except (OSError, subprocess.SubprocessError):
        return None
    expected_name, expected_email = expected_identity()
    if not expected_email:
        return (
            "No git identity is configured for this machine. Run: git config --global user.name "
            '"<Your Name>" and git config --global user.email <you@example.com>, then retry the commit.'
        )
    try:
        identities = effective_identities(command, directory)
    except (OSError, subprocess.SubprocessError, ValueError):
        return None
    expected = describe(expected_name, expected_email) if expected_name else expected_email
    for role, (name, email) in identities.items():
        if problem(name, email, expected_name, expected_email):
            return f'{role} is "{describe(name, email)}", expected "{expected}". {fix(expected_name, expected_email)}'
    return None


def main():
    try:
        event = json.load(sys.stdin)
    except ValueError:
        return
    if not isinstance(event, dict):
        return
    if event.get("hook_event_name") != "PreToolUse" or event.get("tool_name") != "Bash":
        return
    tool_input = event.get("tool_input")
    command = tool_input.get("command") if isinstance(tool_input, dict) else None
    if not isinstance(command, str):
        return
    reason = verdict(command, event.get("cwd") or os.getcwd())
    if reason:
        print(
            json.dumps(
                {
                    "hookSpecificOutput": {
                        "hookEventName": "PreToolUse",
                        "permissionDecision": "deny",
                        "permissionDecisionReason": reason,
                    }
                }
            )
        )


if __name__ == "__main__":
    main()
