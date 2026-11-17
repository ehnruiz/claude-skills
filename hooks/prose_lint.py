#!/usr/bin/env python3
"""Prose lint for text that leaves the machine, in two modes chosen by the event.

PreToolUse on Bash: a command that commits, tags, or posts to GitHub (git commit, git tag, gh pr
create/edit/comment/review, gh issue create/comment, gh release create) is scanned, and any finding
turns into an "ask" so the maintainer sees it before the text is published. It asks and does not
deny, because a quoted reviewer comment may legitimately hold curly quotes.

Stop: the last assistant message that has text is scanned, plus scratchpad paths the user cannot
open. A finding blocks the stop (exit 2, findings on stderr) so the model fixes its final message.
When the stop hook is already active the hook only adds context, so a finding the model cannot fix
does not loop.

The findings are curly quotes, em dashes, British spellings and AI attribution. A malformed or empty
event always allows.
"""

import json
import re
import sys

COMMAND_MARKERS = (
    "git commit", "git tag", "gh pr create", "gh pr edit", "gh pr comment", "gh pr review",
    "gh issue create", "gh issue comment", "gh release create",
)

CURLY_QUOTES = "‘’“”"
EM_DASH = "—"

BRITISH_WORDS = (
    r"colour(?:s|ed|ful|ing)?", r"favour(?:s|ed|ite|ites|able)?", r"behaviour(?:s|al)?",
    r"honour(?:s|ed|able)?", r"humour", r"labour", r"neighbour(?:s|hood)?", r"flavour(?:s)?",
    r"organis(?:e|es|ed|ing|ation|ations)", r"realis(?:e|es|ed|ing|ation)",
    r"recognis(?:e|es|ed|ing)", r"optimis(?:e|es|ed|ing|ation)", r"minimis(?:e|es|ed|ing)",
    r"maximis(?:e|es|ed|ing)", r"summaris(?:e|es|ed|ing)", r"prioritis(?:e|es|ed|ing)",
    r"customis(?:e|es|ed|ing|ation)",
    # "analyses" is the US plural noun, so only these three forms of the verb are listed.
    r"analyse|analysed|analysing",
    r"centre(?:s|d)?", r"metre(?:s)?", r"litre(?:s)?", r"defence", r"licence(?:s)?", r"programme(?:s)?",
    r"travelling", r"travelled", r"modelling", r"modelled", r"labelled", r"labelling", r"whilst",
    r"amongst", r"learnt", r"fulfil", r"enrol", r"enrolment", r"artefact(?:s)?", r"aluminium",
    r"cheque(?:s)?",
)

ATTRIBUTION_PATTERNS = (
    r"generated (?:with|by) claude",
    r"co-authored-by:[^\n]*claude",
    r"claude\.ai/code/session",
    r"claude-session:",
    "\U0001F916 generated",
)

SCRATCHPAD_PATH = r"(?:/private)?/tmp/claude-\S+"

CONTEXT_CHARS = 15
EXCERPT_CHARS = 40

URL = re.compile(r"https?://\S+")
BRITISH = re.compile(r"\b(?:" + "|".join(BRITISH_WORDS) + r")\b", re.IGNORECASE)
ATTRIBUTION = re.compile("|".join(ATTRIBUTION_PATTERNS), re.IGNORECASE)
SCRATCHPAD = re.compile(SCRATCHPAD_PATH)

REMINDER = "Text that leaves this machine uses straight quotes, no em dashes, American spelling and no AI attribution."


def excerpt(text, match):
    start = max(0, match.start() - CONTEXT_CHARS)
    end = min(len(text), match.end() + CONTEXT_CHARS)
    return text[start:end].replace("\n", " ")[:EXCERPT_CHARS]


def scan(text, kind, pattern):
    return [(kind, excerpt(text, m)) for m in pattern.finditer(text)]


def findings(text):
    """Every (kind, excerpt) the text trips. URLs are not scanned, except for the attribution link."""
    bare = URL.sub(" ", text)
    found = []
    found += scan(bare, "curly quote", re.compile("[" + CURLY_QUOTES + "]"))
    found += scan(bare, "em dash", re.compile(EM_DASH))
    found += scan(bare, "British spelling", BRITISH)
    found += scan(text, "AI attribution", ATTRIBUTION)
    return found


def bash_decision(command):
    """The ask decision for a publishing command with findings, else None."""
    if not any(marker in command for marker in COMMAND_MARKERS):
        return None
    found = findings(command)
    if not found:
        return None
    lines = [f'{kind}: "{text}"' for kind, text in found] + [REMINDER]
    return {
        "hookSpecificOutput": {
            "hookEventName": "PreToolUse",
            "permissionDecision": "ask",
            "permissionDecisionReason": "\n".join(lines),
        }
    }


def last_assistant_text(path):
    """Text blocks of the last assistant line that has any, joined; None when unreadable or absent."""
    try:
        with open(path, encoding="utf-8", errors="replace") as handle:
            lines = handle.read().splitlines()
    except OSError:
        return None
    for line in reversed(lines):
        try:
            entry = json.loads(line)
        except ValueError:
            continue
        if not isinstance(entry, dict) or entry.get("type") != "assistant":
            continue
        message = entry.get("message")
        content = message.get("content") if isinstance(message, dict) else None
        if not isinstance(content, list):
            continue
        texts = [b.get("text", "") for b in content if isinstance(b, dict) and b.get("type") == "text"]
        if texts:
            return "\n".join(t for t in texts if isinstance(t, str))
    return None


def stop_findings(text):
    found = findings(text)
    found += [("path the user cannot open", excerpt(text, m)) for m in SCRATCHPAD.finditer(text)]
    return found


def handle_stop(event):
    path = event.get("transcript_path")
    if not isinstance(path, str) or not path:
        return 0
    text = last_assistant_text(path)
    if not text:
        return 0
    found = stop_findings(text)
    if not found:
        return 0
    if event.get("stop_hook_active"):
        kinds = ", ".join(dict.fromkeys(kind for kind, _ in found))
        context = f"prose-lint still sees: {kinds}"
        print(json.dumps({"hookSpecificOutput": {"hookEventName": "Stop", "additionalContext": context}}))
        return 0
    for kind, snippet in found:
        print(f'{kind}: "{snippet}"', file=sys.stderr)
    print("Fix these in your final message before finishing.", file=sys.stderr)
    return 2


def main():
    try:
        event = json.load(sys.stdin)
    except ValueError:
        return 0
    if not isinstance(event, dict):
        return 0
    name = event.get("hook_event_name")
    if name == "Stop":
        return handle_stop(event)
    if name == "PreToolUse" and event.get("tool_name") == "Bash":
        tool_input = event.get("tool_input")
        command = tool_input.get("command") if isinstance(tool_input, dict) else None
        decision = bash_decision(command) if isinstance(command, str) else None
        if decision:
            print(json.dumps(decision))
    return 0


if __name__ == "__main__":
    sys.exit(main())
