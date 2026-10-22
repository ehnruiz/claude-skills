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

    def test_a_submodule_keys_on_its_own_checkout(self):
        env = {**os.environ, "GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@t",
               "GIT_COMMITTER_NAME": "t", "GIT_COMMITTER_EMAIL": "t@t"}
        lib = self.tmp / "lib"
        subprocess.run(["git", "init", "-q", str(lib)], check=True)
        subprocess.run(["git", "-C", str(lib), "commit", "-q", "--allow-empty", "-m", "init"], check=True, env=env)
        subprocess.run(["git", "-C", str(self.repo), "-c", "protocol.file.allow=always", "submodule", "add", "-q",
                        str(lib), "vendor/lib"], check=True, env=env)
        checkout = (self.repo / "vendor" / "lib").resolve()
        self.assertEqual(hook.main_checkout(str(checkout)), checkout)
        tree = self.tmp / "lib-feature"
        subprocess.run(["git", "-C", str(checkout), "worktree", "add", "-q", str(tree)], check=True, env=env)
        self.assertEqual(hook.main_checkout(str(tree)), checkout)

    def test_a_trailing_space_in_a_submodule_path_is_kept(self):
        env = {**os.environ, "GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@t",
               "GIT_COMMITTER_NAME": "t", "GIT_COMMITTER_EMAIL": "t@t"}
        lib = self.tmp / "lib"
        subprocess.run(["git", "init", "-q", str(lib)], check=True)
        subprocess.run(["git", "-C", str(lib), "commit", "-q", "--allow-empty", "-m", "init"], check=True, env=env)
        subprocess.run(["git", "-C", str(self.repo), "-c", "protocol.file.allow=always", "submodule", "add", "-q",
                        str(lib), "vendor/lib "], check=True, env=env)
        checkout = (self.repo / "vendor" / "lib ").resolve()
        self.assertEqual(hook.main_checkout(str(checkout)), checkout)

    def test_a_global_core_worktree_cannot_redirect_the_folder(self):
        env = {**os.environ, "GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@t",
               "GIT_COMMITTER_NAME": "t", "GIT_COMMITTER_EMAIL": "t@t"}
        subprocess.run(["git", "-C", str(self.repo), "commit", "-q", "--allow-empty", "-m", "init"], check=True, env=env)
        tree = self.tmp / "shop-feature"
        subprocess.run(["git", "-C", str(self.repo), "worktree", "add", "-q", str(tree)], check=True, env=env)
        config = self.tmp / "gitconfig"
        config.write_text("[core]\n\tworktree = /nowhere\n")
        with mock.patch.dict(os.environ, {"GIT_CONFIG_GLOBAL": str(config)}):
            self.assertEqual(hook.main_checkout(str(tree)), self.repo.resolve())

    def test_a_separate_git_dir_and_its_worktree_share_a_folder(self):
        env = {**os.environ, "GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@t",
               "GIT_COMMITTER_NAME": "t", "GIT_COMMITTER_EMAIL": "t@t"}
        main = self.tmp / "sep-main"
        subprocess.run(["git", "init", "-q", "--separate-git-dir", str(self.tmp / "sep.git"), str(main)], check=True)
        subprocess.run(["git", "-C", str(main), "commit", "-q", "--allow-empty", "-m", "init"], check=True, env=env)
        tree = self.tmp / "sep-tree"
        subprocess.run(["git", "-C", str(main), "worktree", "add", "-q", str(tree)], check=True, env=env)
        self.assertEqual(hook.main_checkout(str(tree)), hook.main_checkout(str(main)))

    def test_outside_git_uses_the_folder_itself(self):
        plain = self.tmp / "notes-folder"
        plain.mkdir()
        folder = hook.handoff_dir(str(plain), self.home)
        self.assertEqual(folder.name, "handoffs")
        self.assertEqual(folder.parent.name, hook.project_slug(plain.resolve()))

    def test_slug_matches_claude_codes_project_folder_name(self):
        self.assertEqual(hook.project_slug(pathlib.Path("/Users/me/projects/my.repo_v2")),
                         "-Users-me-projects-my-repo-v2")

    def log_compaction(self, session="s1", when="2026-10-20T14:03:00.000Z", branch="main", count="2", paths="a.txt b.txt"):
        self.notes.mkdir(parents=True, exist_ok=True)
        with open(self.notes / "compactions.log", "a", encoding="utf-8") as log:
            log.write("\t".join([when, session, "auto", branch, count, paths]) + "\n")

    def resumed(self, source="resume", session="s1"):
        return hook.notice({"cwd": str(self.repo), "source": source, "session_id": session}, self.home)

    def test_resume_and_compact_show_the_sessions_last_compaction_without_a_note(self):
        self.log_compaction()
        for source in ("resume", "compact"):
            self.assertEqual(self.resumed(source),
                             "Last compaction: 2026-10-20T14:03:00.000Z, branch main, 2 dirty files: a.txt b.txt.")

    def test_the_last_compaction_follows_the_note(self):
        self.write("2026-01-02-new.md", "ship it")
        self.log_compaction()
        context = self.resumed()
        self.assertIn("ship it", context)
        self.assertTrue(context.endswith("Last compaction: 2026-10-20T14:03:00.000Z, branch main, 2 dirty files: a.txt b.txt."))

    def test_the_latest_line_for_the_session_wins_and_another_sessions_line_is_not_shown(self):
        self.log_compaction(when="2026-10-20T10:00:00.000Z", branch="old")
        self.log_compaction(session="s2", when="2026-10-20T11:00:00.000Z", branch="other")
        self.log_compaction(when="2026-10-20T12:00:00.000Z", branch="new")
        self.log_compaction(session="s2", when="2026-10-20T13:00:00.000Z", branch="other-again")
        context = self.resumed()
        self.assertIn("2026-10-20T12:00:00.000Z, branch new", context)
        self.assertNotIn("other", context)
        self.assertNotIn("branch old", context)

    def test_a_session_with_no_line_gets_nothing_extra(self):
        self.log_compaction(session="s2")
        self.assertIsNone(self.resumed(session="s1"))
        self.write("2026-01-02-new.md", "ship it")
        self.assertNotIn("Last compaction", self.resumed(session="s1"))

    def test_startup_and_clear_do_not_show_the_compaction(self):
        self.log_compaction()
        self.write("2026-01-02-new.md", "ship it")
        for source in ("startup", "clear", None):
            event = {"cwd": str(self.repo), "session_id": "s1"}
            if source:
                event["source"] = source
            self.assertNotIn("Last compaction", hook.notice(event, self.home))

    def test_a_clean_tree_reads_as_zero_dirty_files(self):
        self.log_compaction(count="0", paths="")
        self.assertEqual(self.resumed(), "Last compaction: 2026-10-20T14:03:00.000Z, branch main, 0 dirty files.")

    def test_the_compaction_line_comes_off_the_preview_budget(self):
        self.write("2026-01-02-long.md", "Q" * (hook.PREVIEW_CHARS * 2))
        self.log_compaction()
        line = "Last compaction: 2026-10-20T14:03:00.000Z, branch main, 2 dirty files: a.txt b.txt."
        context = self.resumed()
        shown = context.count("Q")
        self.assertLessEqual(shown + len(line), hook.PREVIEW_CHARS)
        self.assertGreater(shown + len(line), hook.PREVIEW_CHARS - 10)
        self.assertIn("\n[...]", context)
        self.assertIn(line, context)
        self.assertIn("pick-up", context)

    def test_a_compaction_line_longer_than_the_budget_leaves_no_preview(self):
        self.write("2026-01-02-long.md", "Q" * (hook.PREVIEW_CHARS * 10))
        self.log_compaction(count="10", paths=" ".join("p" * 200 for _ in range(10)))
        context = self.resumed()
        self.assertEqual(context.count("Q"), 0)
        self.assertIn("pick-up", context)

    def test_a_malformed_log_line_is_skipped(self):
        self.notes.mkdir(parents=True)
        (self.notes / "compactions.log").write_text("garbage\n\n")
        self.assertIsNone(self.resumed())

    def test_quiet_about_compactions_when_the_repository_has_its_own_handoff_skill(self):
        self.log_compaction()
        (self.repo / ".claude" / "skills" / "handoff").mkdir(parents=True)
        (self.repo / ".claude" / "skills" / "handoff" / "SKILL.md").write_text("own")
        os.environ["CLAUDE_PROJECT_DIR"] = str(self.repo)
        self.assertIsNone(self.resumed())


if __name__ == "__main__":
    unittest.main()
