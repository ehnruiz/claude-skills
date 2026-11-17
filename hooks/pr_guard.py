#!/usr/bin/env python3
"""Stop hook: stop a turn that opens a pull request without the PR monitor, or that claims review threads are resolved while GitHub still lists open ones.

Two corrections repeat across sessions. The agent opens a pull request and ends the turn without
turning on the desktop app's PR monitor, so the user has to ask for it. The agent also reports
review threads as resolved or addressed while threads remain open on GitHub. The hook reads the
session transcript and makes both checks executable. The monitor check runs only when the
transcript shows the desktop app's PR tools (a CLI session has no monitor). The thread check runs
only when the final message sounds like a resolution claim, and then asks GitHub through the gh
CLI (the binary can be replaced with PR_GUARD_GH). A problem found exits 2 with the findings on
stderr; when the stop hook is already active it reports through additionalContext instead, so it
never loops. A malformed event, a missing transcript or a failed gh call always allows.
"""

import json
import os
import re
import subprocess
import sys

PR_URL = re.compile(r"https://github\.com/([\w.-]+)/([\w.-]+)/pull/(\d+)")
CLAIM = re.compile(
    r"\b(resolved|addressed|fixed|handled)\b[^.\n]{0,80}\b(thread|comment|review|finding|feedback)s?\b"
    r"|\b(thread|comment|review|finding|feedback)s?\b[^.\n]{0,80}\b(resolved|addressed|fixed|handled)\b",
    re.IGNORECASE,
)
MONITOR_TOOL = "mcp__ccd_pr__set_monitor"
# `gh pr create` run as a command (at the start of a line or after ; & | ( then do, with optional VAR=value
# prefixes), so the words quoted inside an echo or a test fixture do not count as opening a pull request.
RUNS_PR_CREATE = re.compile(r"(?:^|[;&|(\n]|\bthen\b|\bdo\b)\s*(?:\w+=\S*\s+)*gh\s+pr\s+create\b", re.MULTILINE)
BIND_TOOL = "mcp__ccd_pr__bind_pr"
QUERY = (
    "query($owner:String!,$repo:String!,$number:Int!){repository(owner:$owner,name:$repo){"
    "pullRequest(number:$number){reviewThreads(first:100){nodes{isResolved isOutdated path line "
    "comments(first:1){nodes{author{login} body}}}}}}}"
)
LAST_PRS = 3
MAX_THREADS = 10
GH_TIMEOUT = 20


def urls_in(value):
    """Pull request URLs in a string, without a trailing slash."""
    return [m.group(0) for m in PR_URL.finditer(value)] if isinstance(value, str) else []


def result_text(content):
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        return "\n".join(b.get("text", "") for b in content if isinstance(b, dict) and isinstance(b.get("text"), str))
    return ""


def read_records(path):
    records = []
    with open(path, encoding="utf-8", errors="replace") as f:
        for line in f:
            try:
                record = json.loads(line)
            except ValueError:
                continue
            if isinstance(record, dict):
                records.append(record)
    return records


def blocks(record):
    message = record.get("message")
    content = message.get("content") if isinstance(message, dict) else None
    return [b for b in content if isinstance(b, dict)] if isinstance(content, list) else []


def add(items, url):
    if url not in items:
        items.append(url)


def analyze(records, raw=""):
    """created, known, monitored (all in order of first appearance), desktop and last_text."""
    created, known, monitored = [], [], []
    create_ids = set()
    last_text = ""
    for record in records:
        if record.get("type") == "pr-link":
            for url in urls_in(record.get("prUrl")):
                add(known, url)
        for block in blocks(record):
            kind = block.get("type")
            data = block.get("input") if isinstance(block.get("input"), dict) else {}
            if kind == "tool_use":
                name = block.get("name")
                if name == "Bash" and RUNS_PR_CREATE.search(str(data.get("command", ""))):
                    create_ids.add(block.get("id"))
                elif name in (MONITOR_TOOL, BIND_TOOL):
                    for url in urls_in(data.get("url")):
                        add(known, url)
                        if name == MONITOR_TOOL and data.get("auto_fix") is True:
                            add(monitored, url)
            elif kind == "tool_result" and block.get("tool_use_id") in create_ids:
                for url in urls_in(result_text(block.get("content"))):
                    add(created, url)
                    add(known, url)
        if record.get("type") == "assistant":
            texts = [b["text"] for b in blocks(record) if b.get("type") == "text" and isinstance(b.get("text"), str)]
            if texts:
                last_text = "\n".join(texts)
    return created, known, monitored, MONITOR_TOOL in raw, last_text


def claims_resolved(text):
    return bool(CLAIM.search(text))


def monitor_findings(created, monitored):
    return [
        f"PR {url} was opened this session and the PR monitor is not on. Call {MONITOR_TOOL} for it "
        "with auto_fix true (bind_pr first if get_status does not report it), then finish."
        for url in created
        if url not in monitored
    ]


def unresolved_threads(url, gh):
    """Unresolved review threads of one PR as display lines; raises on any failure."""
    owner, repo, number = PR_URL.fullmatch(url).groups()
    proc = subprocess.run(
        [gh, "api", "graphql", "-f", f"query={QUERY}", "-F", f"owner={owner}", "-F", f"repo={repo}",
         "-F", f"number={number}"],
        capture_output=True, text=True, timeout=GH_TIMEOUT,
    )
    if proc.returncode != 0:
        raise RuntimeError((proc.stderr.strip().splitlines() or [f"gh exited {proc.returncode}"])[0])
    nodes = json.loads(proc.stdout)["data"]["repository"]["pullRequest"]["reviewThreads"]["nodes"]
    lines = []
    for node in nodes:
        if node.get("isResolved") is False:
            first = ((node.get("comments") or {}).get("nodes") or [{}])[0]
            author = (first.get("author") or {}).get("login", "unknown")
            body = " ".join(str(first.get("body", "")).split())[:80]
            lines.append(f"  {node.get('path')}:{node.get('line')} {author}: {body}")
    return lines


def thread_findings(known, gh):
    findings, notes = [], []
    for url in known[-LAST_PRS:]:
        try:
            lines = unresolved_threads(url, gh)
        except (OSError, ValueError, KeyError, TypeError, AttributeError, RuntimeError, subprocess.SubprocessError) as err:
            notes.append(f"pr_guard could not check review threads on {url}: {str(err).splitlines()[0] if str(err) else type(err).__name__}")
            continue
        if lines:
            head = f"PR {url} still has {len(lines)} unresolved review thread(s) while the final message says they are resolved:"
            findings.append("\n".join([head, *lines[:MAX_THREADS]]))
    return findings, notes


def check(event, gh):
    """(findings, notes) for a Stop event."""
    path = event.get("transcript_path")
    if event.get("hook_event_name") != "Stop" or not isinstance(path, str):
        return [], []
    try:
        with open(path, encoding="utf-8", errors="replace") as f:
            raw = f.read()
        records = read_records(path)
    except OSError:
        return [], []
    created, known, monitored, desktop, last_text = analyze(records, raw)
    findings = monitor_findings(created, monitored) if desktop else []
    notes = []
    if known and claims_resolved(last_text):
        more, notes = thread_findings(known, gh)
        findings += more
    return findings, notes


def context_output(text):
    return json.dumps({"hookSpecificOutput": {"hookEventName": "Stop", "additionalContext": text}})


def main():
    try:
        event = json.load(sys.stdin)
    except ValueError:
        return 0
    if not isinstance(event, dict):
        return 0
    findings, notes = check(event, os.environ.get("PR_GUARD_GH") or "gh")
    if not findings:
        if notes:
            print(context_output("\n".join(notes)))
        return 0
    if event.get("stop_hook_active"):
        print(context_output("pr-guard still failing: " + " ".join(f.splitlines()[0] for f in findings)))
        return 0
    sys.stderr.write("\n".join([*findings, *notes]) + "\nFix these before finishing.\n")
    return 2


if __name__ == "__main__":
    sys.exit(main())
