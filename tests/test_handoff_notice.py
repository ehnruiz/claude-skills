import importlib.util
import os
import pathlib
import subprocess
import tempfile
import time
import unittest
from unittest import mock

ROOT = pathlib.Path(__file__).resolve().parents[1]
SPEC = importlib.util.spec_from_file_location("handoff_notice", ROOT / "hooks" / "handoff_notice.py")
hook = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(hook)


class HandoffNoticeTest(unittest.TestCase):
    def setUp(self):
        self.tmp = pathlib.Path(tempfile.mkdtemp())
        self.home = self.tmp / "home"
        self.repo = self.tmp / "shop"
        self.repo.mkdir()
        subprocess.run(["git", "init", "-q", str(self.repo)], check=True)
        self.notes = self.home / ".claude" / "projects" / hook.project_slug(self.repo.resolve()) / "handoffs"
        env = mock.patch.dict(os.environ, {}, clear=False)
        env.start()
        os.environ.pop("CLAUDE_PROJECT_DIR", None)
        self.addCleanup(env.stop)

    def write(self, name, text, age=0):
        self.notes.mkdir(parents=True, exist_ok=True)
        path = self.notes / name
        path.write_text(text)
        stamp = time.time() - age
        os.utime(path, (stamp, stamp))
        return path

    def test_quiet_without_notes(self):
        self.assertIsNone(hook.notice({"cwd": str(self.repo)}, self.home))

    def test_names_the_newest_note_and_previews_it(self):
        self.write("2026-01-01-old.md", "old goal", age=100)
        newest = self.write("2026-01-02-new.md", "# Goal\nship it")
        context = hook.notice({"cwd": str(self.repo)}, self.home)
        self.assertIn(str(newest), context)
        self.assertIn("ship it", context)
        self.assertNotIn("old goal", context)
        self.assertIn("pick-up", context)

    def test_preview_is_cut_at_the_limit(self):
        self.write("2026-01-02-long.md", "x" * (hook.PREVIEW_CHARS + 50))
        context = hook.notice({"cwd": str(self.repo)}, self.home)
        self.assertIn("x" * hook.PREVIEW_CHARS + "\n[...]", context)
        self.assertNotIn("x" * (hook.PREVIEW_CHARS + 1), context)

    def test_quiet_when_the_repository_has_its_own_handoff_skill(self):
        self.write("2026-01-02-new.md", "goal")
        (self.repo / ".claude" / "skills" / "handoff").mkdir(parents=True)
        (self.repo / ".claude" / "skills" / "handoff" / "SKILL.md").write_text("own")
        os.environ["CLAUDE_PROJECT_DIR"] = str(self.repo)
        self.assertIsNone(hook.notice({"cwd": str(self.repo)}, self.home))

    def test_a_worktree_shares_the_main_checkouts_notes(self):
        subprocess.run(["git", "-C", str(self.repo), "commit", "-q", "--allow-empty", "-m", "init"],
                       check=True, env={**os.environ, "GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@t",
                                        "GIT_COMMITTER_NAME": "t", "GIT_COMMITTER_EMAIL": "t@t"})
        tree = self.tmp / "shop-feature"
        subprocess.run(["git", "-C", str(self.repo), "worktree", "add", "-q", str(tree)], check=True)
        self.assertEqual(hook.handoff_dir(str(tree), self.home), self.notes)

    def test_outside_git_uses_the_folder_itself(self):
        plain = self.tmp / "notes-folder"
        plain.mkdir()
        folder = hook.handoff_dir(str(plain), self.home)
        self.assertEqual(folder.name, "handoffs")
        self.assertEqual(folder.parent.name, hook.project_slug(plain.resolve()))

    def test_slug_matches_claude_codes_project_folder_name(self):
        self.assertEqual(hook.project_slug(pathlib.Path("/Users/me/projects/my.repo_v2")),
                         "-Users-me-projects-my-repo-v2")

if __name__ == "__main__":
    unittest.main()
