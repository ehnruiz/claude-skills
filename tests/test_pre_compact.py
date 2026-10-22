import json
import os
import pathlib
import subprocess
import sys
import tempfile
import unittest
from datetime import datetime
from unittest import mock

ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "hooks"))  # a script run by path has its own folder first; an import by test does not
import handoff_notice  # noqa: E402
import pre_compact  # noqa: E402

GIT_ENV = {"GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@t", "GIT_COMMITTER_NAME": "t", "GIT_COMMITTER_EMAIL": "t@t"}


class PreCompactTest(unittest.TestCase):
    def setUp(self):
        self.tmp = pathlib.Path(tempfile.mkdtemp())
        self.home = self.tmp / "home"
        self.repo = self.tmp / "shop"
        self.repo.mkdir()
        env = {**os.environ, **GIT_ENV}
        subprocess.run(["git", "init", "-q", "-b", "main", str(self.repo)], check=True)
        subprocess.run(["git", "-C", str(self.repo), "commit", "-q", "--allow-empty", "-m", "init"], check=True, env=env)
        self.log = handoff_notice.handoff_dir(str(self.repo), self.home) / "compactions.log"

    def event(self, **fields):
        return {"session_id": "s1", "trigger": "auto", "cwd": str(self.repo), **fields}

    def lines(self):
        return self.log.read_text(encoding="utf-8").splitlines()

    def test_logs_time_session_trigger_branch_and_dirty_files(self):
        (self.repo / "a.txt").write_text("a")
        (self.repo / "b.txt").write_text("b")
        pre_compact.record(self.event(), self.home)
        lines = self.lines()
        self.assertEqual(len(lines), 1)
        when, session, trigger, branch, count, paths = lines[0].split("\t")
        self.assertEqual(datetime.fromisoformat(when.replace("Z", "+00:00")).utcoffset().total_seconds(), 0)
        self.assertEqual([session, trigger, branch, count], ["s1", "auto", "main", "2"])
        self.assertEqual(sorted(paths.split(" ")), ["a.txt", "b.txt"])

    def test_counts_every_untracked_file_in_a_new_folder(self):
        (self.repo / "drafts").mkdir()
        (self.repo / "drafts" / "a.md").write_text("a")
        (self.repo / "drafts" / "b.md").write_text("b")
        pre_compact.record(self.event(), self.home)
        fields = self.lines()[0].split("\t")
        self.assertEqual(fields[4], "2")
        self.assertEqual(sorted(fields[5].split(" ")), ["drafts/a.md", "drafts/b.md"])

    def test_a_clean_tree_logs_zero_and_no_paths(self):
        pre_compact.record(self.event(), self.home)
        self.assertEqual(self.lines()[0].split("\t")[4:], ["0", ""])

    def test_a_modified_tracked_file_keeps_its_whole_name(self):
        (self.repo / "a.txt").write_text("a")
        env = {**os.environ, **GIT_ENV}
        subprocess.run(["git", "-C", str(self.repo), "add", "a.txt"], check=True)
        subprocess.run(["git", "-C", str(self.repo), "commit", "-q", "-m", "a"], check=True, env=env)
        (self.repo / "a.txt").write_text("changed")
        pre_compact.record(self.event(), self.home)
        self.assertEqual(self.lines()[0].split("\t")[5], "a.txt")

    def test_lists_only_the_first_ten_paths_but_counts_all(self):
        for i in range(12):
            (self.repo / f"f{i:02}.txt").write_text("x")
        pre_compact.record(self.event(), self.home)
        fields = self.lines()[0].split("\t")
        self.assertEqual(fields[4], "12")
        self.assertEqual(len(fields[5].split(" ")), 10)

    def test_keeps_the_last_200_lines(self):
        self.log.parent.mkdir(parents=True)
        self.log.write_text("".join(f"t{i}\tx\tauto\tmain\t0\t\n" for i in range(205)))
        pre_compact.record(self.event(), self.home)
        lines = self.lines()
        self.assertEqual(len(lines), 200)
        self.assertTrue(lines[0].startswith("t6\t"))
        self.assertIn("\ts1\t", lines[-1])

    def test_a_folder_outside_git_logs_a_dash_for_the_branch(self):
        plain = self.tmp / "plain"
        plain.mkdir()
        pre_compact.record(self.event(cwd=str(plain), session_id="s9", trigger="manual"), self.home)
        log = handoff_notice.handoff_dir(str(plain), self.home) / "compactions.log"
        fields = log.read_text(encoding="utf-8").split("\t")
        self.assertEqual(fields[1:5], ["s9", "manual", "-", "0"])

    def test_a_missing_trigger_is_a_dash(self):
        event = self.event()
        del event["trigger"]
        pre_compact.record(event, self.home)
        self.assertEqual(self.lines()[0].split("\t")[2], "-")

    def test_a_worktree_logs_beside_the_main_checkouts_handoffs(self):
        tree = self.tmp / "shop-feature"
        subprocess.run(["git", "-C", str(self.repo), "worktree", "add", "-q", "-b", "feature", str(tree)], check=True)
        pre_compact.record(self.event(cwd=str(tree)), self.home)
        self.assertEqual(self.lines()[0].split("\t")[3], "feature")

    def test_malformed_input_writes_nothing_and_never_raises(self):
        for event in ({}, {"session_id": ""}, {"session_id": 7}, {"session_id": "s1", "cwd": 5}, [], "text", None):
            pre_compact.record(event, self.home)
        self.assertFalse(self.log.exists())

    def run_script(self, stdin, home=None):
        env = {**os.environ, "HOME": str(home or self.home)}
        return subprocess.run([sys.executable, str(ROOT / "hooks" / "pre_compact.py")], input=stdin, capture_output=True,
                              text=True, env=env, cwd=self.tmp)

    def test_run_by_path_it_imports_the_shared_folder_logic_and_prints_nothing(self):
        (self.repo / "a.txt").write_text("a")
        done = self.run_script(json.dumps(self.event()))
        self.assertEqual((done.returncode, done.stdout, done.stderr), (0, "", ""))
        self.assertEqual(self.lines()[0].split("\t")[3:5], ["main", "1"])

    def test_run_by_path_with_an_unwritable_home_exits_zero_quietly(self):
        blocker = self.tmp / "blocker"
        blocker.write_text("a file where the home folder should be")
        done = self.run_script(json.dumps(self.event()), home=blocker)
        self.assertEqual((done.returncode, done.stdout, done.stderr), (0, "", ""))

    def test_run_by_path_with_garbage_on_stdin_exits_zero_quietly(self):
        for stdin in ("{not json", "", "[1]"):
            done = self.run_script(stdin)
            self.assertEqual((done.returncode, done.stdout, done.stderr), (0, "", ""), stdin)
        self.assertFalse(self.log.exists())


class RegistrationTest(unittest.TestCase):
    def test_pre_compact_is_registered_and_resume_reaches_the_session_start_hook(self):
        hooks = json.loads((ROOT / "hooks" / "hooks.json").read_text())["hooks"]
        pre = [h["command"] for group in hooks["PreCompact"] for h in group["hooks"]]
        self.assertEqual(pre, ['python3 "${CLAUDE_PLUGIN_ROOT}/hooks/pre_compact.py"'])
        start = [g for g in hooks["SessionStart"] if "handoff_notice.py" in g["hooks"][0]["command"]]
        self.assertEqual(len(start), 1)
        self.assertEqual(set(start[0]["matcher"].split("|")), {"startup", "clear", "compact", "resume"})


if __name__ == "__main__":
    unittest.main()
