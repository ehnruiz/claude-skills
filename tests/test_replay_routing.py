import contextlib
import importlib.util
import io
import json
import os
import pathlib
import subprocess
import sys
import tempfile
import unittest

ROOT = pathlib.Path(__file__).resolve().parents[1]
SCRIPTS = ROOT / "skills/skill-collision-audit/scripts"
sys.path.insert(0, str(SCRIPTS))
spec = importlib.util.spec_from_file_location("replay_routing", SCRIPTS / "replay_routing.py")
replay_routing = importlib.util.module_from_spec(spec)
spec.loader.exec_module(replay_routing)

SKILLS = [
    {"name": "content-plan", "path": "a", "description": "Plan this month's social posts. For one post, use post-writer."},
    {"name": "post-writer", "path": "b", "description": "Write one social post. For a month of posts, use content-plan."},
    {"name": "invoice-chase", "path": "c", "description": "Draft payment reminders for overdue invoices."},
]
PAIR = frozenset({"content-plan", "post-writer"})
REQUESTS = [("plan May's posts", "content-plan"), ("write a post about the launch", "post-writer"), ("book a flight to Paris", None)]


def fake_claude(answers):
    """An `ask` that answers each request from a table and records the system prompts it was given."""
    seen = []

    def ask(system, request):
        seen.append(system)
        return answers[request]

    ask.seen = seen
    return ask


class ReplayRoutingTest(unittest.TestCase):
    def replay(self, answers, requests=REQUESTS):
        with contextlib.redirect_stdout(io.StringIO()) as out:
            failures = replay_routing.replay(SKILLS, {PAIR: {"hash": "", "requests": requests}}, fake_claude(answers))
        return failures, out.getvalue()

    def test_every_request_landing_where_it_was_saved_passes(self):
        answers = {"plan May's posts": "content-plan", "write a post about the launch": "post-writer", "book a flight to Paris": "none"}
        failures, out = self.replay(answers)
        self.assertEqual(failures, [])
        self.assertEqual([line[:2] for line in out.splitlines()], ["ok"] * 3)

    def test_a_request_that_moves_to_the_other_skill_fails(self):
        answers = {"plan May's posts": "post-writer", "write a post about the launch": "post-writer", "book a flight to Paris": "none"}
        failures, _ = self.replay(answers)
        self.assertEqual(len(failures), 1)
        self.assertIn("plan May's posts", failures[0])
        self.assertIn("expected content-plan, got post-writer", failures[0])

    def test_a_neither_request_passes_on_none_or_a_third_skill_and_fails_on_the_pair(self):
        for answer, ok in (("none", True), ("invoice-chase", True), ("content-plan", False)):
            with self.subTest(answer=answer):
                failures, _ = self.replay({"book a flight to Paris": answer}, [("book a flight to Paris", None)])
                self.assertEqual(failures == [], ok, failures)

    def test_an_answer_is_read_the_way_people_write_a_skill_name(self):
        for raw in ("`content-plan`", "/content-plan", "Content-Plan.", "**content-plan**", "content-plan\nIt plans the month."):
            with self.subTest(raw=raw):
                self.assertEqual(replay_routing.read_answer(raw, SKILLS), "content-plan")
        self.assertEqual(replay_routing.read_answer("None.", SKILLS), "none")
        self.assertIsNone(replay_routing.read_answer("I would use the planner", SKILLS))

    def test_an_answer_that_names_no_skill_fails(self):
        failures, _ = self.replay({"plan May's posts": "the planning one"}, [("plan May's posts", "content-plan")])
        self.assertIn("named no skill", failures[0])

    def test_claude_sees_every_skill_that_can_fire_and_none_typed_only_by_name(self):
        ask = fake_claude({"plan May's posts": "content-plan"})
        manual = {"name": "release", "path": "d", "description": "Cut a release.", "manual": True}
        with contextlib.redirect_stdout(io.StringIO()):
            replay_routing.replay(SKILLS + [manual], {PAIR: {"hash": "", "requests": [("plan May's posts", "content-plan")]}}, ask)
        for skill in SKILLS:
            self.assertIn(f"- {skill['name']}: {skill['description']}", ask.seen[0])
        self.assertNotIn("release", ask.seen[0])

    def test_exit_codes(self):
        with tempfile.TemporaryDirectory() as tmp:
            library = pathlib.Path(tmp) / "skills"
            for skill in SKILLS:
                (library / skill["name"]).mkdir(parents=True)
                (library / skill["name"] / "SKILL.md").write_text(
                    f"---\nname: {skill['name']}\ndescription: {skill['description']}\n---\n", encoding="utf-8"
                )
            reviewed = library / "reviewed-pairs.json"
            entry = {"skills": sorted(PAIR), "hash": "", "requests": [{"request": r, "expect": e} for r, e in REQUESTS]}
            reviewed.write_text(json.dumps({"pairs": [entry]}), encoding="utf-8")
            right = {"plan May's posts": "content-plan", "write a post about the launch": "post-writer", "book a flight to Paris": "none"}
            wrong = dict(right, **{"plan May's posts": "post-writer"})

            def main(*args, answers=right, ask=None):
                with contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(io.StringIO()):
                    return replay_routing.main([str(a) for a in args], ask=ask or fake_claude(answers))

            def broken(system, request):
                raise replay_routing.ClaudeError("claude exited 1: not logged in")

            self.assertEqual(main("--reviewed", reviewed, library), 0)
            self.assertEqual(main("--reviewed", reviewed, library, answers=wrong), 1)
            self.assertEqual(main("--reviewed", library / "absent.json", library), 2)
            self.assertEqual(main("--reviewed", reviewed, library / "missing"), 2)
            self.assertEqual(main("--reviewed", reviewed, library, ask=broken), 2)
            self.assertEqual(main("--reviewed", library, library), 2)
            entry["requests"][0]["expect"] = "gone-skill"
            reviewed.write_text(json.dumps({"pairs": [entry]}), encoding="utf-8")
            self.assertEqual(main("--reviewed", reviewed, library), 2)

    def test_a_failed_claude_call_stops_the_replay(self):
        def broken(system, request):
            raise replay_routing.ClaudeError("claude exited 1: not logged in")

        with contextlib.redirect_stdout(io.StringIO()), self.assertRaises(replay_routing.ClaudeError):
            replay_routing.replay(SKILLS, {PAIR: {"hash": "", "requests": REQUESTS}}, broken)

    def test_the_claude_command_runs_without_the_users_setup_tools_or_a_saved_session(self):
        command = replay_routing.claude_command("system text", model="haiku")
        self.assertEqual(command[:2], ["claude", "-p"])
        # Without safe mode and a strict MCP config, the user's CLAUDE.md and MCP instructions reach the call.
        for flag in ("--safe-mode", "--strict-mcp-config", "--disable-slash-commands", "--no-session-persistence"):
            self.assertIn(flag, command)
        self.assertEqual(command[command.index("--system-prompt") + 1], "system text")
        self.assertEqual(command[command.index("--tools") + 1], "")
        self.assertEqual(command[command.index("--model") + 1], "haiku")
        self.assertNotIn("--model", replay_routing.claude_command("s"))

    def test_ask_claude_sends_the_request_on_stdin_from_an_empty_folder(self):
        calls = []

        def run(command, **options):
            calls.append((command, options, os.listdir(options["cwd"])))
            return subprocess.CompletedProcess(command, 0, stdout="content-plan\n", stderr="")

        answer = replay_routing.ask_claude(run=run)("system text", "--plan May's posts")
        self.assertEqual(answer, "content-plan\n")
        command, options, files = calls[0]
        self.assertEqual(options["input"], "--plan May's posts")
        self.assertNotIn("--plan May's posts", command)
        self.assertEqual(files, [])
        self.assertFalse(pathlib.Path(options["cwd"]).exists())
        self.assertIn("timeout", options)

    def test_ask_claude_raises_on_a_failed_or_missing_claude(self):
        failed = lambda command, **options: subprocess.CompletedProcess(command, 1, stdout="", stderr="not logged in")
        with self.assertRaisesRegex(replay_routing.ClaudeError, "claude exited 1: not logged in"):
            replay_routing.ask_claude(run=failed)("s", "r")

        def missing(command, **options):
            raise FileNotFoundError("claude")

        with self.assertRaisesRegex(replay_routing.ClaudeError, "could not run claude"):
            replay_routing.ask_claude(run=missing)("s", "r")


if __name__ == "__main__":
    unittest.main()
