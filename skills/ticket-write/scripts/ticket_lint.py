#!/usr/bin/env python3
"""Check a ticket draft against the ticket-write budgets.

Usage: ticket_lint.py --type {bug,task,story,spike,epic} [--title TITLE] DRAFT.md
Exits 0 when every budget holds, 1 when any is over, 2 on a usage error.
"""

import argparse
import re
import sys

DESCRIPTION_WORDS = {"bug": 150, "task": 150, "story": 150, "spike": 80, "epic": 80}
TITLE_CHARS = 80
MAX_HEADINGS = 6
MAX_IDENTIFIERS = 3
CRITERIA_RANGE = (2, 5)

HEADING = re.compile(r"^\s*(#{1,6}\s+.+|\*\*[^*]+\*\*\s*:?\s*)$")
BULLET = re.compile(r"^\s*(?:[-*+]|\d+[.)])\s+")
LINK_ONLY = re.compile(r"^\s*(?:[\w ]+:\s*)?<?https?://\S+>?\s*$")
CC_LINE = re.compile(r"^\s*cc\s+@", re.IGNORECASE)
DONE_WHEN = re.compile(r"done when|acceptance criteria", re.IGNORECASE)

# Things a non-engineer has to read past: inline code, paths, calls, snake_case, camelCase.
IDENTIFIER_PATTERNS = [
    re.compile(r"`([^`]+)`"),
    re.compile(r"\b[\w.-]+/[\w./-]+\.\w+\b"),
    re.compile(r"\b[A-Za-z_][\w.]*\(\)"),
    re.compile(r"\b[a-z]+(?:_[a-z0-9]+)+\b"),
    re.compile(r"\b[a-z]{2,}(?:[A-Z][a-z0-9]+)+\b"),
]


def is_heading(line):
    return bool(HEADING.match(line))


def count_words(lines):
    total = 0
    for line in lines:
        if not line.strip() or is_heading(line) or LINK_ONLY.match(line) or CC_LINE.match(line):
            continue
        total += len(re.findall(r"\w[\w'’-]*", BULLET.sub("", line)))
    return total


def find_identifiers(text):
    found = []
    for pattern in IDENTIFIER_PATTERNS:
        for match in pattern.finditer(text):
            value = match.group(1) if match.groups() else match.group(0)
            if value not in found:
                found.append(value)
    # A path inside backticks is one identifier, not two.
    return [v for v in found if not any(v != o and v in o for o in found)]


def count_criteria(lines):
    inside, count = False, 0
    for line in lines:
        if is_heading(line):
            inside = bool(DONE_WHEN.search(line))
            continue
        if inside and BULLET.match(line):
            count += 1
    return count


def lint(text, kind, title=None):
    lines = text.splitlines()
    rows = []

    def row(name, value, limit, ok):
        rows.append((name, value, limit, ok))

    if title is not None:
        row("title characters", len(title), TITLE_CHARS, len(title) <= TITLE_CHARS)
    words = count_words(lines)
    row("description words", words, DESCRIPTION_WORDS[kind], words <= DESCRIPTION_WORDS[kind])
    headings = sum(1 for line in lines if is_heading(line))
    row("headings", headings, MAX_HEADINGS, headings <= MAX_HEADINGS)
    identifiers = find_identifiers(text)
    row("code identifiers", len(identifiers), MAX_IDENTIFIERS, len(identifiers) <= MAX_IDENTIFIERS)
    criteria = count_criteria(lines)
    low, high = CRITERIA_RANGE
    row("acceptance criteria", criteria, f"{low}-{high}", low <= criteria <= high)
    return rows, identifiers


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--type", required=True, choices=sorted(DESCRIPTION_WORDS))
    parser.add_argument("--title")
    parser.add_argument("draft")
    args = parser.parse_args(argv)
    try:
        with open(args.draft, encoding="utf-8") as handle:
            text = handle.read()
    except OSError as error:
        print(f"cannot read {args.draft}: {error}", file=sys.stderr)
        return 2

    rows, identifiers = lint(text, args.type, args.title)
    for name, value, limit, ok in rows:
        print(f"{'ok  ' if ok else 'OVER'}  {name}: {value} (limit {limit})")
    if len(identifiers) > MAX_IDENTIFIERS:
        print("move to technical notes: " + ", ".join(identifiers))
    return 0 if all(ok for *_, ok in rows) else 1


if __name__ == "__main__":
    sys.exit(main())
