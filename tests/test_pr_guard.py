import importlib.util
import json
import os
import pathlib
import subprocess
import sys
import tempfile
import unittest

ROOT = pathlib.Path(__file__).resolve().parents[1]
HOOK = ROOT / "hooks" / "pr_guard.py"
SPEC = importlib.util.spec_from_file_location("pr_guard", HOOK)
hook = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(hook)

PR = "https://github.com/example/docs/pull/57"
PR2 = "https://github.com/example/app/pull/408"

FAKE_GH = """#!/usr/bin/env python3
import os, sys
with open(os.environ["FAKE_GH_MARKER"], "a") as f:
    f.write(" ".join(sys.argv[1:]) + "\\n")
if os.environ.get("FAKE_GH_EXIT"):
    sys.stderr.write("gh: boom\\nsecond line\\n")
    sys.exit(int(os.environ["FAKE_GH_EXIT"]))
sys.stdout.write(open(os.environ["FAKE_GH_OUT"]).read())
"""


def tool_use(name, tool_id, **input_):
    return {"type": "assistant", "message": {"role": "assistant",
            "content": [{"type": "tool_use", "id": tool_id, "name": name, "input": input_}]}}


def tool_result(tool_id, content):
    return {"type": "user", "message": {"role": "user",
            "content": [{"type": "tool_result", "tool_use_id": tool_id, "content": content}]}}


def text(body):
    return {"type": "assistant", "message": {"role": "assistant", "content": [{"type": "text", "text": body}]}}


def created(url, tool_id="toolu_c", content=None):
    return [tool_use("Bash", tool_id, command="gh pr create --repo example/docs --base main"),
            tool_result(tool_id, url + "\n" if content is None else content)]


def threads(*specs):
    nodes = [{"isResolved": resolved, "isOutdated": False, "path": path, "line": line,
              "comments": {"nodes": [{"author": {"login": author}, "body": body}]}}
             for resolved, path, line, author, body in specs]
    return {"data": {"repository": {"pullRequest": {"reviewThreads": {"nodes": nodes}}}}}


class PrGuardTest(unittest.TestCase):
    def setUp(self):
        self.tmp = pathlib.Path(tempfile.mkdtemp())
        self.gh = self.tmp / "gh"
        self.gh.write_text(FAKE_GH)
        self.gh.chmod(0o755)
        self.marker = self.tmp / "gh-calls.txt"
        self.out = self.tmp / "gh-out.json"

    def run_hook(self, records, event=None, gh_exit=None, canned=None):
        transcript = self.tmp / "t.jsonl"
        transcript.write_text("\n".join(json.dumps(r) for r in records) + "\n")
        payload = {"hook_event_name": "Stop", "transcript_path": str(transcript)}
        payload.update(event or {})
        return self.run_raw(json.dumps(payload), gh_exit, canned)

    def run_raw(self, stdin, gh_exit=None, canned=None):
        self.out.write_text(json.dumps(canned if canned is not None else threads()))
        env = {**os.environ, "PR_GUARD_GH": str(self.gh), "FAKE_GH_MARKER": str(self.marker),
               "FAKE_GH_OUT": str(self.out)}
        env.pop("FAKE_GH_EXIT", None)
        if gh_exit is not None:
            env["FAKE_GH_EXIT"] = str(gh_exit)
        return subprocess.run([sys.executable, str(HOOK)], input=stdin, capture_output=True, text=True, env=env)

    def monitor(self, url=PR, auto_fix=True):
        return [tool_use("mcp__ccd_pr__set_monitor", "toolu_m", url=url, auto_fix=auto_fix, address_comments=True)]

    # monitor check

    def test_created_pr_without_monitor_blocks(self):
        # a monitor call for another PR shows the desktop tools exist but leaves this PR unmonitored
        result = self.run_hook(created(PR) + self.monitor("https://github.com/example/other/pull/1"))
        self.assertEqual(result.returncode, 2)
        self.assertIn(f"PR {PR} was opened this session and the PR monitor is not on.", result.stderr)
        self.assertIn("Fix these before finishing.", result.stderr)

    def test_gh_pr_create_quoted_inside_a_command_is_not_a_created_pr(self):
        # a fixture written with echo holds the words and the URL, but no pull request was opened
        fixture = "echo '{\"command\":\"gh pr create --body b\"}' > t.jsonl; python3 pr_guard.py"
        records = [tool_use("Bash", "toolu_f", command=fixture), tool_result("toolu_f", PR + " was opened")]
        result = self.run_hook(records + self.monitor("https://github.com/example/other/pull/1"))
        self.assertEqual((result.returncode, result.stderr), (0, ""))

    def test_gh_pr_create_after_cd_and_env_prefix_is_a_created_pr(self):
        records = [tool_use("Bash", "toolu_e", command="cd repo && GH_TOKEN=x gh pr create --fill"),
                   tool_result("toolu_e", PR)]
        result = self.run_hook(records + self.monitor("https://github.com/example/other/pull/1"))
        self.assertEqual(result.returncode, 2)
        self.assertIn(PR, result.stderr)

    def test_created_pr_with_monitor_passes(self):
        result = self.run_hook(created(PR) + self.monitor())
        self.assertEqual((result.returncode, result.stdout, result.stderr), (0, "", ""))

    def test_monitor_without_auto_fix_blocks(self):
        result = self.run_hook(created(PR) + self.monitor(auto_fix=False))
        self.assertEqual(result.returncode, 2)
        self.assertIn(PR, result.stderr)

    def test_cli_session_without_desktop_tools_is_skipped(self):
        result = self.run_hook(created(PR))
        self.assertEqual((result.returncode, result.stdout, result.stderr), (0, "", ""))

    def test_bound_pr_not_created_here_needs_no_monitor(self):
        records = [tool_use("mcp__ccd_pr__bind_pr", "toolu_b", url=PR2), text("Looked at it.")]
        records.append(tool_use("mcp__ccd_pr__set_monitor", "toolu_m", url="https://github.com/a/b/pull/1", auto_fix=True))
        result = self.run_hook(records)
        self.assertEqual(result.returncode, 0)

    def test_two_created_one_monitored_gives_one_finding(self):
        records = created(PR, "toolu_1") + created(PR2, "toolu_2") + self.monitor(PR)
        result = self.run_hook(records)
        self.assertEqual(result.returncode, 2)
        self.assertEqual(result.stderr.count("was opened this session"), 1)
        self.assertIn(PR2, result.stderr)

    def test_tool_result_as_list_of_text_blocks(self):
        content = [{"type": "text", "text": f"Creating...\n{PR}/\n"}]
        result = self.run_hook(created(PR, content=content) + self.monitor("https://github.com/a/b/pull/1"))
        self.assertEqual(result.returncode, 2)
        self.assertIn(f"PR {PR} was opened", result.stderr)

    # review claims

    def claim(self, body="All the Devin threads are resolved."):
        return [tool_use("mcp__ccd_pr__bind_pr", "toolu_b", url=PR2), text(body)]

    def test_claim_with_unresolved_threads_blocks_and_lists_them(self):
        canned = threads((False, "src/a.py", 12, "devin", "Use a set\nhere"), (True, "x.py", 1, "bob", "done"),
                         (False, "src/b.py", 7, "alice", "Rename this"))
        result = self.run_hook(self.claim(), canned=canned)
        self.assertEqual(result.returncode, 2)
        self.assertIn(f"PR {PR2} still has 2 unresolved review thread(s)", result.stderr)
        self.assertIn("  src/a.py:12 devin: Use a set here", result.stderr)
        self.assertIn("  src/b.py:7 alice: Rename this", result.stderr)
        self.assertNotIn("x.py", result.stderr)
        self.assertIn("graphql", self.marker.read_text())

    def test_claim_with_all_resolved_passes(self):
        result = self.run_hook(self.claim(), canned=threads((True, "a.py", 1, "devin", "ok")))
        self.assertEqual((result.returncode, result.stdout, result.stderr), (0, "", ""))

    def test_reverse_order_claim_is_detected(self):
        self.run_hook(self.claim("Every review comment has been addressed."))
        self.assertTrue(self.marker.exists())

    def test_no_claim_never_calls_gh(self):
        result = self.run_hook(self.claim("Pushed the change; tests pass."), canned=threads((False, "a", 1, "d", "x")))
        self.assertEqual(result.returncode, 0)
        self.assertFalse(self.marker.exists())

    def test_claim_without_known_pr_never_calls_gh(self):
        result = self.run_hook([text("All threads resolved.")])
        self.assertEqual(result.returncode, 0)
        self.assertFalse(self.marker.exists())

    def test_gh_failure_allows_with_a_note(self):
        result = self.run_hook(self.claim(), gh_exit=1)
        self.assertEqual(result.returncode, 0)
        context = json.loads(result.stdout)["hookSpecificOutput"]
        self.assertEqual(context["hookEventName"], "Stop")
        self.assertIn(f"pr_guard could not check review threads on {PR2}: gh: boom", context["additionalContext"])
        self.assertNotIn("second line", context["additionalContext"])

    def test_unparsable_gh_output_allows_with_a_note(self):
        self.out.write_text("not json")
        transcript = self.tmp / "t.jsonl"
        transcript.write_text("\n".join(json.dumps(r) for r in self.claim()))
        env = {**os.environ, "PR_GUARD_GH": str(self.gh), "FAKE_GH_MARKER": str(self.marker),
               "FAKE_GH_OUT": str(self.out)}
        env.pop("FAKE_GH_EXIT", None)
        event = json.dumps({"hook_event_name": "Stop", "transcript_path": str(transcript)})
        result = subprocess.run([sys.executable, str(HOOK)], input=event, capture_output=True, text=True, env=env)
        self.assertEqual(result.returncode, 0)
        self.assertIn("could not check", json.loads(result.stdout)["hookSpecificOutput"]["additionalContext"])

    def test_only_the_last_three_known_prs_are_queried(self):
        records = [tool_use("mcp__ccd_pr__bind_pr", f"toolu_{i}", url=f"https://github.com/a/b/pull/{i}") for i in range(5)]
        self.run_hook(records + [text("Threads resolved.")])
        self.assertEqual(len(self.marker.read_text().splitlines()), 3)

    # output modes

    def test_stop_hook_active_reports_json_and_exits_zero(self):
        result = self.run_hook(created(PR) + self.monitor("https://github.com/a/b/pull/1"), event={"stop_hook_active": True})
        self.assertEqual(result.returncode, 0)
        context = json.loads(result.stdout)["hookSpecificOutput"]
        self.assertEqual(context["hookEventName"], "Stop")
        self.assertTrue(context["additionalContext"].startswith("pr-guard still failing: PR " + PR))

    # defensive

    def test_missing_transcript_path_allows(self):
        result = self.run_raw(json.dumps({"hook_event_name": "Stop"}))
        self.assertEqual((result.returncode, result.stdout, result.stderr), (0, "", ""))

    def test_unreadable_transcript_allows(self):
        result = self.run_raw(json.dumps({"hook_event_name": "Stop", "transcript_path": str(self.tmp / "nope.jsonl")}))
        self.assertEqual((result.returncode, result.stdout), (0, ""))

    def test_empty_stdin_allows(self):
        result = self.run_raw("")
        self.assertEqual((result.returncode, result.stdout, result.stderr), (0, "", ""))

    def test_non_stop_event_allows(self):
        result = self.run_hook(created(PR), event={"hook_event_name": "PreToolUse"})
        self.assertEqual((result.returncode, result.stdout, result.stderr), (0, "", ""))

    def test_pr_link_record_counts_as_known(self):
        records = [{"type": "pr-link", "prNumber": 408, "prUrl": PR2, "prRepository": "example/app"},
                   text("Comments addressed.")]
        self.run_hook(records)
        self.assertIn("graphql", self.marker.read_text())

    def test_helpers_claim_regex(self):
        self.assertTrue(hook.claims_resolved("I fixed the review comments"))
        self.assertFalse(hook.claims_resolved("Opened the PR for review."))


if __name__ == "__main__":
    unittest.main()
