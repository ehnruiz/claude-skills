import { spawn } from "node:child_process";
import { mkdtempSync } from "node:fs";
import { tmpdir } from "node:os";
import path from "node:path";
import { describe, expect, it } from "./compat.mjs";
import { runHook } from "./hookRun.mjs";
import { blockedGit, decide } from "../../hooks/git-guardrails.mjs";

const hook = path.resolve(import.meta.dirname, "../../hooks/git-guardrails.mjs");
const root = mkdtempSync(path.join(tmpdir(), "claude-hook-"));
const bash = (command) => ({ hook_event_name: "PreToolUse", tool_name: "Bash", cwd: root, tool_input: { command } });

describe("git-guardrails hook, run as Claude Code runs it", () => {
  it.each([
    ["git push", "git push"],
    ["git push --force origin main", "git push"],
    ["git reset --hard HEAD~1", "git reset --hard"],
    ["git clean -fd", "git clean"],
    ["git branch -D old-work", "git branch -D"],
  ])("denies %s", async (command, label) => {
    const { stdout, code } = await runHook(hook, bash(command), { root });
    expect(code).toBe(0);
    const out = JSON.parse(stdout).hookSpecificOutput;
    expect(out).toMatchObject({ hookEventName: "PreToolUse", permissionDecision: "deny" });
    expect(out.permissionDecisionReason).toContain(`\`${label}\``);
    expect(out.permissionDecisionReason).toContain("leave it to the user");
  });

  it.each(["git status", "git commit -m 'stop before git push'", "git reset --soft HEAD~1", "git branch -d merged"])(
    "prints nothing for %s",
    async (command) => {
      const { stdout, code } = await runHook(hook, bash(command), { root });
      expect(code).toBe(0);
      expect(stdout).toBe("");
    },
  );

  it("ignores a tool other than Bash", async () => {
    const event = { tool_name: "Write", tool_input: { file_path: "notes.md", content: "git push" } };
    const { stdout, code } = await runHook(hook, event, { root });
    expect(code).toBe(0);
    expect(stdout).toBe("");
  });

  it.each(["", "not json", "[]"])("allows on a malformed or empty event (%s)", async (raw) => {
    const { stdout, code } = await runHook(hook, raw, { root });
    expect(code).toBe(0);
    expect(stdout).toBe("");
  });

  it("reads an event that arrives in two parts with a pause between them", async () => {
    const raw = JSON.stringify(bash("git push origin main"));
    const result = await new Promise((resolve) => {
      const child = spawn(process.execPath, [hook], { env: { ...process.env, CLAUDE_PROJECT_DIR: root } });
      let stdout = "";
      child.stdout.on("data", (d) => (stdout += d));
      child.on("close", (code) => resolve({ stdout, code }));
      child.stdin.write(raw.slice(0, 20));
      setTimeout(() => child.stdin.end(raw.slice(20)), 300);
    });
    expect(result.code).toBe(0);
    expect(JSON.parse(result.stdout).hookSpecificOutput.permissionDecision).toBe("deny");
  });
});

describe("blockedGit, the commands it blocks", () => {
  it.each([
    ["git push", "git push"],
    ["git push --dry-run", "git push"],
    ["git -C ../other push", "git push"],
    ["git -c core.pager=cat --no-pager push", "git push"],
    ["/usr/bin/git push", "git push"],
    ["sudo git push", "git push"],
    ["env GIT_TRACE=1 git push", "git push"],
    ["GIT_TRACE=1 git push", "git push"],
    ["git status && git push", "git push"],
    ["git fetch; git push", "git push"],
    ["sleep 1 & git push", "git push"],
    ["if git push; then echo ok; fi", "git push"],
    ["(cd sub && git push)", "git push"],
    ['echo "$(git push)"', "git push"],
    ["x=`git push`", "git push"],
    ["x=$(cd sub; git push)", "git push"],
    ['bash -c "git push"', "git push"],
    ["sh -lc 'git reset --hard'", "git reset --hard"],
    ['eval "git push"', "git push"],
    ["git reset --hard", "git reset --hard"],
    ["git reset --hard origin/main", "git reset --hard"],
    ["git clean -f", "git clean"],
    ["git clean -xdf", "git clean"],
    ["git clean --force -d", "git clean"],
    ["git clean -d", "git clean"],
    ["git branch -D old", "git branch -D"],
    ["git branch -Dr origin/old", "git branch -D"],
    ["git branch --delete --force old", "git branch -D"],
    ["git branch -d -f old", "git branch -D"],
    ["git branch -df old", "git branch -D"],
    ["timeout 60 bash -c 'git push origin main'", "git push"],
    ["sudo bash -c 'git push'", "git push"],
    ["nohup sh -c 'git push' &", "git push"],
    ["sudo -u deploy git push", "git push"],
    ["sudo -E git push", "git push"],
    ["env -i git push", "git push"],
    ["nice -n 10 git push", "git push"],
    ["xargs -I {} git branch -D {}", "git branch -D"],
    ["git \\\n  push origin main", "git push"],
    ["git -C repo \\\n  push", "git push"],
    ["git push&", "git push"],
    ["git push>/dev/null", "git push"],
    ["sleep 1& git push", "git push"],
    ["git reset --har HEAD", "git reset --hard"],
    ["git clean -e -n -fd", "git clean"],
    ["git clean --exclude -n -fd", "git clean"],
    ["git branch --del --force old", "git branch -D"],
  ])("blocks %s", (command, label) => {
    expect(blockedGit(command)?.label).toBe(label);
  });
});

describe("blockedGit, the commands it lets through", () => {
  it.each([
    "git status",
    "git log --oneline -5",
    "git fetch origin",
    "git pull --ff-only",
    "git stash push -m wip",
    "git reset HEAD file.txt",
    "git reset --soft HEAD~1",
    "git reset --mixed",
    "git clean -n",
    "git clean --dry-run -d",
    "git clean -nfd",
    "git branch -d merged",
    "git branch --delete merged",
    "git branch -f topic main",
    "git branch -u origin/main",
    "git --version",
    "git commit -m 'before git push, run the tests'",
    'git commit -m "reset --hard is blocked now"',
    "git commit -m '$(git push)'",
    "echo git push",
    'grep -rn "git push" docs',
    "cat <<EOF\ngit push\nEOF",
    "git commit -F - <<'EOF'\nDo not git reset --hard here.\nEOF",
    "# git push",
    "git log # then git push",
    "npm run push",
    "gitk --all",
    "env echo git push",
    "time echo git push",
    "xargs -n1 echo git branch -D",
    "git clean -e build -n",
    "git clean --dry -fd",
  ])("allows %s", (command) => {
    expect(blockedGit(command)).toBeNull();
  });
});

describe("decide", () => {
  it("names the command and what it would do", () => {
    const reason = decide(bash("git clean -fdx"));
    expect(reason).toContain("Blocked by the git-guardrails hook");
    expect(reason).toContain("deletes untracked files");
  });

  it("allows an event with no command", () => {
    expect(decide({ tool_name: "Bash", tool_input: {} })).toBeNull();
    expect(decide({ tool_name: "Bash", tool_input: { command: "   " } })).toBeNull();
    expect(decide({})).toBeNull();
  });
});
