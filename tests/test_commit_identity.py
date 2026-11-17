import importlib.util
import json
import os
import pathlib
import subprocess
import sys
import tempfile
import unittest

ROOT = pathlib.Path(__file__).resolve().parents[1]
HOOK = ROOT / "hooks" / "commit_identity.py"
SPEC = importlib.util.spec_from_file_location("commit_identity", HOOK)
hook = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(hook)

EMAIL = "ada@example.com"
NAME = "Ada Example"


class CommitIdentityTest(unittest.TestCase):
    def setUp(self):
        self.tmp = pathlib.Path(tempfile.mkdtemp())
        self.home = self.tmp / "home"
        self.home.mkdir()
        self.global_config = self.tmp / "gitconfig"
        self.global_config.write_text("")
        self.repo = self.tmp / "shop"
        self.repo.mkdir()
        subprocess.run(["git", "init", "-q", str(self.repo)], check=True, env=self.env())
        self.other = self.tmp / "elsewhere"
        self.other.mkdir()

    def env(self, **extra):
        env = {k: v for k, v in os.environ.items() if not k.startswith(("GIT_", "COMMIT_IDENTITY_"))}
        env.update(HOME=str(self.home), GIT_CONFIG_GLOBAL=str(self.global_config), GIT_CONFIG_NOSYSTEM="1")
        env.update(extra)
        return env

    def set_local(self, name, email):
        subprocess.run(["git", "-C", str(self.repo), "config", "user.name", name], check=True, env=self.env())
        subprocess.run(["git", "-C", str(self.repo), "config", "user.email", email], check=True, env=self.env())

    def run_hook(self, command, cwd=None, expected_email=EMAIL, expected_name=NAME, raw=None):
        extra = {}
        if expected_email:
            extra["COMMIT_IDENTITY_EMAIL"] = expected_email
        if expected_name:
            extra["COMMIT_IDENTITY_NAME"] = expected_name
        event = {
            "hook_event_name": "PreToolUse",
            "tool_name": "Bash",
            "tool_input": {"command": command},
            "cwd": str(cwd or self.repo),
        }
        return subprocess.run(
            [sys.executable, str(HOOK)],
            input=json.dumps(event) if raw is None else raw,
            capture_output=True,
            text=True,
            env=self.env(**extra),
            cwd=str(self.tmp),
        )

    def decision(self, result):
        self.assertEqual(result.returncode, 0, result.stderr)
        if not result.stdout.strip():
            return None, ""
        out = json.loads(result.stdout)["hookSpecificOutput"]
        self.assertEqual(out["hookEventName"], "PreToolUse")
        return out["permissionDecision"], out["permissionDecisionReason"]

    def test_denies_a_claude_identity_in_local_config(self):
        self.set_local("Claude", "noreply@anthropic.com")
        decision, reason = self.decision(self.run_hook("git commit -m x"))
        self.assertEqual(decision, "deny")
        self.assertIn('author is "Claude <noreply@anthropic.com>"', reason)
        self.assertIn(f'expected "{NAME} <{EMAIL}>"', reason)
        self.assertIn(f'git config user.name "{NAME}" && git config user.email {EMAIL}', reason)
        self.assertIn("(add --global for every repository), then retry.", reason)

    def test_allows_the_expected_identity(self):
        self.set_local(NAME, EMAIL)
        self.assertEqual(self.decision(self.run_hook("git commit -m x")), (None, ""))

    def test_denies_a_claude_author_override(self):
        self.set_local(NAME, EMAIL)
        decision, reason = self.decision(self.run_hook('git commit --author="Claude <noreply@anthropic.com>" -m x'))
        self.assertEqual(decision, "deny")
        self.assertIn("Claude", reason)

    def test_dash_c_override_fixes_a_wrong_local_config(self):
        self.set_local("Claude", "noreply@anthropic.com")
        command = f'git -c user.name="{NAME}" -c user.email={EMAIL} commit -m x'
        self.assertEqual(self.decision(self.run_hook(command)), (None, ""))

    def test_denies_a_committer_email_override(self):
        self.set_local(NAME, EMAIL)
        decision, reason = self.decision(self.run_hook("GIT_COMMITTER_EMAIL=someone@else.com git commit -m x"))
        self.assertEqual(decision, "deny")
        self.assertIn("committer", reason)
        self.assertIn("someone@else.com", reason)

    def test_denies_a_different_name(self):
        self.set_local("Someone Else", EMAIL)
        decision, reason = self.decision(self.run_hook("git commit -m x"))
        self.assertEqual(decision, "deny")
        self.assertIn("Someone Else", reason)

    def test_name_is_not_compared_when_no_expected_name(self):
        self.set_local("Anything Goes", EMAIL)
        self.assertEqual(self.decision(self.run_hook("git commit -m x", expected_name=None)), (None, ""))

    def test_denies_a_claude_word_in_the_name_even_when_email_matches(self):
        self.set_local("Claude Code", EMAIL)
        decision, _ = self.decision(self.run_hook("git commit -m x", expected_name=None))
        self.assertEqual(decision, "deny")

    def test_denies_when_no_identity_is_configured_anywhere(self):
        decision, reason = self.decision(self.run_hook("git commit -m x", expected_email=None, expected_name=None))
        self.assertEqual(decision, "deny")
        self.assertIn("No git identity is configured for this machine.", reason)
        self.assertIn("then retry the commit.", reason)

    def test_falls_back_to_the_global_config(self):
        self.global_config.write_text(f"[user]\n\tname = {NAME}\n\temail = {EMAIL}\n")
        result = self.run_hook("git commit -m x", expected_email=None, expected_name=None)
        self.assertEqual(self.decision(result), (None, ""))

    def test_dash_capital_c_resolves_the_repository(self):
        self.set_local("Claude", "noreply@anthropic.com")
        command = f"git -C {self.repo} commit -m x"
        decision, _ = self.decision(self.run_hook(command, cwd=self.other))
        self.assertEqual(decision, "deny")

    def test_quoted_dash_capital_c_resolves_the_repository(self):
        self.set_local("Claude", "noreply@anthropic.com")
        command = f"git -C '{self.repo}' commit -m x"
        decision, _ = self.decision(self.run_hook(command, cwd=self.other))
        self.assertEqual(decision, "deny")

    def test_cd_prefix_resolves_the_repository(self):
        self.set_local("Claude", "noreply@anthropic.com")
        command = f'cd "{self.repo}" && git commit -m x'
        decision, _ = self.decision(self.run_hook(command, cwd=self.other))
        self.assertEqual(decision, "deny")

    def test_relative_cd_resolves_against_cwd(self):
        self.set_local("Claude", "noreply@anthropic.com")
        decision, _ = self.decision(self.run_hook("cd shop; git commit -m x", cwd=self.tmp))
        self.assertEqual(decision, "deny")

    def test_other_git_commands_are_ignored(self):
        self.set_local("Claude", "noreply@anthropic.com")
        result = self.run_hook("git status")
        self.assertEqual((result.returncode, result.stdout), (0, ""))

    def test_dry_run_and_no_commit_are_ignored(self):
        self.set_local("Claude", "noreply@anthropic.com")
        for command in ("git commit --dry-run -m x", "git merge --no-commit topic"):
            result = self.run_hook(command)
            self.assertEqual((result.returncode, result.stdout), (0, ""), command)

    def test_other_subcommands_that_create_commits_are_checked(self):
        self.set_local("Claude", "noreply@anthropic.com")
        for command in ("git merge topic", "git cherry-pick abc", "git revert HEAD", "git rebase main", "git am p.mbox"):
            decision, _ = self.decision(self.run_hook(command))
            self.assertEqual(decision, "deny", command)

    def test_a_word_that_only_contains_the_subcommand_is_ignored(self):
        self.set_local("Claude", "noreply@anthropic.com")
        result = self.run_hook("git commit-graph write")
        self.assertEqual((result.returncode, result.stdout), (0, ""))

    def test_outside_a_repository_is_silent(self):
        result = self.run_hook("git commit -m x", cwd=self.other)
        self.assertEqual((result.returncode, result.stdout), (0, ""))

    def test_non_bash_events_are_ignored(self):
        event = {"hook_event_name": "PreToolUse", "tool_name": "Edit", "tool_input": {"command": "git commit"}}
        result = self.run_hook("", raw=json.dumps(event))
        self.assertEqual((result.returncode, result.stdout), (0, ""))

    def test_empty_stdin_allows(self):
        result = self.run_hook("", raw="")
        self.assertEqual((result.returncode, result.stdout), (0, ""))

    def test_malformed_json_allows(self):
        result = self.run_hook("", raw="{not json")
        self.assertEqual((result.returncode, result.stdout), (0, ""))

    def test_parse_ident_reads_the_name_from_the_right(self):
        self.assertEqual(
            hook.parse_ident("Mary Jane Q. Public <mj@example.com> 1760000000 -0400"),
            ("Mary Jane Q. Public", "mj@example.com"),
        )


if __name__ == "__main__":
    unittest.main()
