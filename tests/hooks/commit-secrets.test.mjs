import { execFileSync } from "node:child_process";
import { randomInt } from "node:crypto";
import { mkdirSync, mkdtempSync, writeFileSync } from "node:fs";
import { tmpdir } from "node:os";
import path from "node:path";
import { describe, expect, it } from "./compat.mjs";
import { runHook } from "./hookRun.mjs";
import { buildScanText, commitRepo, indexDiff, locateInDiff, scanDiff, scanPlan, unscannableWrites, } from "../../hooks/commit-secrets.mjs";
const HOOKS = path.resolve(import.meta.dirname, "../../hooks");
const hook = path.resolve(HOOKS, "commit-secrets.mjs");
const gitleaksAvailable = (() => {
    try {
        execFileSync("gitleaks", ["version"], { stdio: "ignore" });
        return true;
    }
    catch {
        return false;
    }
})();
const hookRun = (root, event, env = {}, cwd) => runHook(hook, event, { root, env, cwd });
const bash = (root, command) => ({
    hook_event_name: "PreToolUse",
    tool_name: "Bash",
    cwd: root,
    tool_input: { command },
});
/** A fabricated AWS-shaped key: AKIA plus 16 random uppercase letters and digits. */
function fakeKey() {
    const alphabet = "ABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789";
    // the 16 characters are all different: gitleaks ignores low-entropy matches, which made a random key flaky
    const chars = new Set();
    while (chars.size < 16)
        chars.add(alphabet[randomInt(alphabet.length)]);
    return `AKIA${[...chars].join("")}`;
}
/** A temp repository with one staged file. */
function repo(content) {
    const dir = mkdtempSync(path.join(tmpdir(), "claude-commit-hook-"));
    const git = (...args) => execFileSync("git", args, { cwd: dir, stdio: "ignore" });
    git("init", "-q");
    git("config", "user.email", "test@example.com");
    git("config", "user.name", "Test");
    writeFileSync(path.join(dir, "settings.ts"), content);
    git("add", "settings.ts");
    return dir;
}
const off = { CLAUDE_HOOK_GITLEAKS: "off" };
describe("commit-secrets hook", () => {
    it("ignores commands that are not a commit", async () => {
        const root = repo(`export const k = '${fakeKey()}';\n`);
        const { stdout, code } = await hookRun(root, bash(root, "npm test"), off);
        expect(code).toBe(0);
        expect(stdout).toBe("");
    });
    it("allows a commit of a clean staged change (fallback scanner)", async () => {
        const root = repo("export const a = 1;\n");
        const { stdout, code } = await hookRun(root, bash(root, "git commit -m x"), off);
        expect(code).toBe(0);
        expect(stdout).toBe("");
    });
    it("asks when the fallback finds a key, naming the file and never the key", async () => {
        const key = fakeKey();
        const root = repo(`export const k = '${key}';\n`);
        const { stdout, code } = await hookRun(root, bash(root, "git commit -m x"), off);
        expect(code).toBe(0);
        const out = JSON.parse(stdout).hookSpecificOutput;
        expect(out.permissionDecision).toBe("ask");
        expect(out.permissionDecisionReason).toContain("settings.ts:1");
        expect(out.permissionDecisionReason).not.toContain(key);
    });
    it.skipIf(!gitleaksAvailable)("denies with gitleaks when a key is staged, with a rule id and no key value", async () => {
        const key = fakeKey();
        const root = repo(`const awsAccessKeyId = "${key}";\nconst awsSecretAccessKey = "${key}${key}";\n`);
        const { stdout, code } = await hookRun(root, bash(root, "git commit -m x"));
        expect(code).toBe(0);
        const out = JSON.parse(stdout).hookSpecificOutput;
        expect(out.permissionDecision).toBe("deny");
        expect(out.permissionDecisionReason).toMatch(/\S+ in settings\.ts:\d+/);
        expect(out.permissionDecisionReason).not.toContain(key);
    });
    it("scans the directory given with git -C, from another cwd", async () => {
        const other = mkdtempSync(path.join(tmpdir(), "claude-commit-hook-other-"));
        const root = repo(`export const k = '${fakeKey()}';\n`);
        const { stdout } = await hookRun(other, bash(other, `git -C ${root} commit -m x`), off, other);
        expect(JSON.parse(stdout).hookSpecificOutput.permissionDecision).toBe("ask");
    });
    it("recognizes commits in chains and rejects other git commands", () => {
        expect(commitRepo("cd foo && git commit -am x", "/p")).toBe("/p/foo");
        expect(commitRepo("git add . ; git commit -m x", "/p")).toBe("/p");
        expect(commitRepo("git -C /tmp/r commit -m x", "/p")).toBe("/tmp/r");
        expect(commitRepo("git log --oneline", "/p")).toBeNull();
        expect(commitRepo("npm test", "/p")).toBeNull();
    });
    it("scans only added lines of a diff", () => {
        const key = fakeKey();
        const diff = [
            "diff --git a/a.ts b/a.ts",
            "--- a/a.ts",
            "+++ b/a.ts",
            "@@ -1,2 +1,2 @@",
            ` const ctx = '${key}';`,
            `-const old = '${key}';`,
            "+const fresh = 1;",
        ].join("\n");
        expect(scanDiff(diff)).toEqual([]);
        const added = diff.replace("+const fresh = 1;", `+const k = '${key}';`);
        expect(scanDiff(added)).toEqual([{ name: expect.any(String), file: "a.ts", line: 2 }]);
    });
    it("never fails the tool on empty or malformed input", async () => {
        const root = repo("export const a = 1;\n");
        for (const bad of ["", "{not json", "[]"]) {
            const { stdout, code } = await hookRun(root, bad, off);
            expect(code).toBe(0);
            expect(stdout).toBe("");
        }
    });
});
/** Two lines gitleaks reports (an access key id and a secret key); the same key makes the fallback fire too. */
const keyLines = (key) => `const awsAccessKeyId = "${key}";\nconst awsSecretAccessKey = "${key}${key}";\n`;
/** A temp repository whose files are committed, then changed by `after` (written but not staged). */
function committedRepo(files, after = {}) {
    const dir = mkdtempSync(path.join(tmpdir(), "claude-commit-hook-"));
    const git = (...args) => execFileSync("git", args, { cwd: dir, stdio: "ignore" });
    git("init", "-q");
    git("config", "user.email", "test@example.com");
    git("config", "user.name", "Test");
    const write = (all) => {
        for (const [name, content] of Object.entries(all)) {
            mkdirSync(path.dirname(path.join(dir, name)), { recursive: true });
            writeFileSync(path.join(dir, name), content);
        }
    };
    write(files);
    git("add", "-A");
    git("commit", "-q", "-m", "init");
    write(after);
    return dir;
}
const decision = (stdout) => stdout ? JSON.parse(stdout).hookSpecificOutput.permissionDecision : "allow";
describe("commit-secrets hook: what the command is about to commit", () => {
    it("plans the staged diff always, tracked changes for -a, and the paths of an earlier git add", () => {
        expect(scanPlan("npm test")).toBeNull();
        expect(scanPlan("git commit -m x")).toEqual({ staged: true, unstaged: false, adds: [] });
        for (const c of [
            "git commit -am x",
            "git commit -qa",
            "git commit --all -m x",
            "git commit -sa -m x",
        ]) {
            expect(scanPlan(c)?.unstaged, c).toBe(true);
        }
        // a message that merely contains the letter a is not -a
        expect(scanPlan('git commit -m "all fixed"')?.unstaged).toBe(false);
        expect(scanPlan("git commit -ma")?.unstaged).toBe(false);
        expect(scanPlan("git add -A && git commit -m x")?.adds).toMatchObject([
            { paths: ["."], trackedOnly: false },
        ]);
        expect(scanPlan("git add . ; git commit -m x")?.adds).toMatchObject([
            { paths: ["."], trackedOnly: false },
        ]);
        expect(scanPlan("git add --all && git commit")?.adds).toMatchObject([
            { paths: ["."], trackedOnly: false },
        ]);
        expect(scanPlan("git add -u && git commit")?.adds).toMatchObject([
            { paths: ["."], trackedOnly: true },
        ]);
        expect(scanPlan('git add src/ok.ts "my dir/b.ts" && git commit')?.adds).toMatchObject([
            { paths: ["src/ok.ts", "my dir/b.ts"], trackedOnly: false },
        ]);
        // an add that comes after the commit has not run when the commit does
        expect(scanPlan("git commit -m x && git add -A")?.adds).toEqual([]);
    });
    it("builds one diff text, keeping real diffs and writing untracked files as added lines", () => {
        const real = [
            "diff --git a/a.ts b/a.ts",
            "--- a/a.ts",
            "+++ b/a.ts",
            "@@ -1 +1 @@",
            "-old",
            "+new",
            "",
        ].join("\n");
        const text = buildScanText({
            staged: real,
            unstaged: "",
            addDiffs: [real],
            untracked: [{ path: "n.ts", content: "one\ntwo\n" }],
        });
        expect(text.split("+++ b/a.ts").length - 1).toBe(1); // the same diff twice is kept once
        expect(text).toContain("+++ b/n.ts\n@@ -0,0 +1,2 @@\n+one\n+two\n");
        expect(buildScanText({ staged: "", unstaged: "", addDiffs: [], untracked: [] })).toBe("");
    });
    it("maps a line of the composed text (a gitleaks StartLine) back to file:line across two files", () => {
        const key = fakeKey();
        const staged = [
            "diff --git a/a.ts b/a.ts",
            "--- a/a.ts",
            "+++ b/a.ts",
            "@@ -4,0 +5,2 @@",
            "+const one = 1;",
            `+const a = '${key}';`,
            "",
        ].join("\n");
        const text = buildScanText({
            staged,
            unstaged: "",
            addDiffs: [],
            untracked: [{ path: "dir/n.ts", content: `x\ny\n${keyLines(key)}` }],
        });
        const lines = text.split("\n");
        const inA = lines.findIndex((l) => l.includes(`const a = '${key}'`)) + 1;
        const inN = lines.findIndex((l) => l.includes("awsAccessKeyId")) + 1;
        expect(locateInDiff(text, inA)).toEqual({ file: "a.ts", line: 6 });
        expect(locateInDiff(text, inN)).toEqual({ file: "dir/n.ts", line: 3 });
        // headers, removed lines and numbers past the end are not findings
        expect(locateInDiff(text, 1)).toBeNull();
        expect(locateInDiff(text, lines.length + 50)).toBeNull();
    });
    it("asks for a key in a tracked file changed but not staged, with git commit -am (fallback)", async () => {
        const key = fakeKey();
        const root = committedRepo({ "settings.ts": "export const a = 1;\n" }, { "settings.ts": `export const k = '${key}';\n` });
        const { stdout } = await hookRun(root, bash(root, "git commit -am x"), off);
        const out = JSON.parse(stdout).hookSpecificOutput;
        expect(out.permissionDecision).toBe("ask");
        expect(out.permissionDecisionReason).toContain("settings.ts:1");
        expect(out.permissionDecisionReason).not.toContain(key);
    });
    it("allows an unstaged key when the commit does not take it (no -a, nothing added)", async () => {
        const root = committedRepo({ "settings.ts": "export const a = 1;\n" }, { "settings.ts": `export const k = '${fakeKey()}';\n` });
        const { stdout } = await hookRun(root, bash(root, "git commit -m x"), off);
        expect(decision(stdout)).toBe("allow");
    });
    it.skipIf(!gitleaksAvailable)("denies with gitleaks for git commit -am with the key unstaged", async () => {
        const key = fakeKey();
        const root = committedRepo({ "settings.ts": "export const a = 1;\n" }, { "settings.ts": keyLines(key) });
        const { stdout } = await hookRun(root, bash(root, "git commit -am x"));
        const out = JSON.parse(stdout).hookSpecificOutput;
        expect(out.permissionDecision).toBe("deny");
        expect(out.permissionDecisionReason).toMatch(/\S+ in settings\.ts:[12]/);
        expect(out.permissionDecisionReason).not.toContain(key);
    });
    it("asks for a key in a new untracked file with git add -A && git commit (fallback)", async () => {
        const key = fakeKey();
        const root = committedRepo({ "ok.ts": "export const a = 1;\n" }, { "sub/new.ts": `export const k = '${key}';\n` });
        const { stdout } = await hookRun(root, bash(root, "git add -A && git commit -m x"), off);
        const out = JSON.parse(stdout).hookSpecificOutput;
        expect(out.permissionDecision).toBe("ask");
        expect(out.permissionDecisionReason).toContain("sub/new.ts:1");
        expect(out.permissionDecisionReason).not.toContain(key);
    });
    it.skipIf(!gitleaksAvailable)("denies with gitleaks for git add -A && git commit with the key in an untracked file", async () => {
        const key = fakeKey();
        const root = committedRepo({ "ok.ts": "export const a = 1;\n" }, { "sub/new.ts": keyLines(key) });
        const { stdout } = await hookRun(root, bash(root, "git add -A && git commit -m x"));
        const out = JSON.parse(stdout).hookSpecificOutput;
        expect(out.permissionDecision).toBe("deny");
        expect(out.permissionDecisionReason).toMatch(/\S+ in sub\/new\.ts:[12]/);
        expect(out.permissionDecisionReason).not.toContain(key);
    });
    it("allows when the only key is in an untracked file outside the added paths", async () => {
        const root = committedRepo({ "README.md": "hi\n" }, {
            "src/ok.ts": "export const a = 1;\n",
            "other/secret.ts": `export const k = '${fakeKey()}';\n`,
        });
        const { stdout } = await hookRun(root, bash(root, "git add src/ok.ts && git commit -m x"), off);
        expect(decision(stdout)).toBe("allow");
    });
    it("asks, naming one file not scanned, for an untracked text file over 1 MB", async () => {
        const big = `export const k = '${fakeKey()}';\n${"// padding\n".repeat(200_000)}`;
        const root = committedRepo({ "README.md": "hi\n" });
        writeFileSync(path.join(root, "big.ts"), big);
        expect(big.length).toBeGreaterThan(2_000_000);
        const { stdout, code } = await hookRun(root, bash(root, "git add -A && git commit -m x"), off);
        expect(code).toBe(0);
        const out = JSON.parse(stdout).hookSpecificOutput;
        expect(out.permissionDecision).toBe("ask");
        expect(NOT_SCANNED.exec(out.permissionDecisionReason)?.[1]).toBe("1");
    });
    it("stays silent on an untracked binary file holding a NUL byte, even over 1 MB", async () => {
        const key = fakeKey();
        const small = Buffer.concat([
            Buffer.from(`k = '${key}'\n`),
            Buffer.from([0]),
            Buffer.from("rest"),
        ]);
        const large = Buffer.concat([Buffer.from([0]), Buffer.alloc(2_000_000, 65)]);
        const root = committedRepo({ "README.md": "hi\n" });
        writeFileSync(path.join(root, "blob.bin"), small);
        writeFileSync(path.join(root, "large.bin"), large);
        const { stdout, code } = await hookRun(root, bash(root, "git add -A && git commit -m x"), off);
        expect(code).toBe(0);
        expect(stdout).toBe("");
    });
    it("scans the repository a cd earlier in the chain moves to, from another cwd", async () => {
        const other = mkdtempSync(path.join(tmpdir(), "claude-commit-hook-other-"));
        const root = repo(`export const k = '${fakeKey()}';\n`);
        const { stdout } = await hookRun(other, bash(other, `cd ${root} && git commit -m x`), off, other);
        expect(decision(stdout)).toBe("ask");
    });
    it("uses the event's cwd rather than the project dir when there is no cd or -C", async () => {
        const project = mkdtempSync(path.join(tmpdir(), "claude-commit-hook-project-"));
        const root = repo(`export const k = '${fakeKey()}';\n`);
        const { stdout } = await hookRun(project, bash(root, "git commit -m x"), off, project);
        expect(decision(stdout)).toBe("ask");
    });
});
describe("commit-secrets hook: a directory change between git add and git commit", () => {
    it("records the directory in force at each git add, apart from the commit", () => {
        expect(scanPlan("git add . && cd sub && git commit -m x", "/p")?.adds[0].dir).toBe("/p");
        expect(scanPlan("cd sub && git add . && cd .. && git commit -m x", "/p")?.adds[0].dir).toBe("/p/sub");
        expect(scanPlan("git -C other add . && git commit -m x", "/p")?.adds[0].dir).toBe("/p/other");
    });
    it("scans an add made before a cd, in the directory it ran in", async () => {
        const key = fakeKey();
        const root = committedRepo({ "sub/a.ts": "export const a = 1;\n" }, { "top.ts": `export const k = '${key}';\n` });
        const { stdout } = await hookRun(root, bash(root, "git add . && cd sub && git commit -m x"), off);
        const out = JSON.parse(stdout).hookSpecificOutput;
        expect(out.permissionDecision).toBe("ask");
        expect(out.permissionDecisionReason).toContain("top.ts:1");
        expect(out.permissionDecisionReason).not.toContain(key);
    });
    it("scans only sub for cd sub && git add . && cd .. && git commit", async () => {
        const command = "cd sub && git add . && cd .. && git commit -m x";
        const outside = committedRepo({ "sub/a.ts": "export const a = 1;\n" }, { "top.ts": `export const k = '${fakeKey()}';\n` });
        expect(decision((await hookRun(outside, bash(outside, command), off)).stdout)).toBe("allow");
        const inside = committedRepo({ "sub/a.ts": "export const a = 1;\n" }, { "sub/new.ts": `export const k = '${fakeKey()}';\n` });
        const out = JSON.parse((await hookRun(inside, bash(inside, command), off)).stdout).hookSpecificOutput;
        expect(out.permissionDecision).toBe("ask");
        expect(out.permissionDecisionReason).toContain("sub/new.ts:1");
    });
});
describe("commit-secrets hook: which repository and which commits", () => {
    it("resolves the base from cwd, then cd or pushd, then -C, quote-aware", () => {
        expect(commitRepo("git commit -m x", "/base")).toBe("/base");
        expect(commitRepo("cd /r && git commit -m x", "/base")).toBe("/r");
        expect(commitRepo("cd sub; pushd ../x && git commit", "/base")).toBe("/base/x");
        expect(commitRepo("cd /r && git -C sub commit -m x", "/base")).toBe("/r/sub");
        expect(commitRepo('git -C "/my repo" commit -m x', "/base")).toBe("/my repo");
        expect(commitRepo("git -C '/my repo' commit -m x", "/base")).toBe("/my repo");
    });
    it("recognizes git with -c values holding spaces and git by absolute path", () => {
        expect(commitRepo('git -c user.name="Jane Doe" commit -m x', "/b")).toBe("/b");
        expect(commitRepo("git -c user.name='Jane Doe' -c a.b=c commit -m x", "/b")).toBe("/b");
        expect(commitRepo("/usr/bin/git commit -m x", "/b")).toBe("/b");
        expect(commitRepo('git -c user.name="Jane Doe" log', "/b")).toBeNull();
        expect(commitRepo('echo "a && git commit"', "/b")).toBeNull();
    });
});
const NOT_SCANNED = /(\d+) files \/ [\d.]+ MB not scanned by commit-secrets: too much for one commit; split it or confirm/;
/** A committed repository plus `count` untracked files `f000.ts`, `f001.ts` ... of about `size` bytes each. */
function repoWithUntracked(count, size, extra = {}) {
    const after = { ...extra };
    for (let i = 0; i < count; i++)
        after[`f${String(i).padStart(3, "0")}.ts`] =
            `export const n = ${i};\n${"// pad\n".repeat(Math.max(0, Math.floor(size / 7)))}`;
    return committedRepo({ "README.md": "hi\n" }, after);
}
describe("commit-secrets hook: a cap on what one commit scans", () => {
    it("asks, saying what was not scanned, when untracked files pass the file cap", async () => {
        const root = repoWithUntracked(5, 100);
        const { stdout, code } = await hookRun(root, bash(root, "git add -A && git commit -m x"), {
            ...off,
            CLAUDE_HOOK_MAX_FILES: "3",
        });
        expect(code).toBe(0);
        const out = JSON.parse(stdout).hookSpecificOutput;
        expect(out.permissionDecision).toBe("ask");
        expect(NOT_SCANNED.exec(out.permissionDecisionReason)?.[1]).toBe("2");
    });
    it("asks when the composed text passes the byte cap, and scans what fits", async () => {
        const root = repoWithUntracked(4, 100_000);
        const { stdout } = await hookRun(root, bash(root, "git add -A && git commit -m x"), {
            ...off,
            CLAUDE_HOOK_MAX_BYTES: "250000",
        });
        const out = JSON.parse(stdout).hookSpecificOutput;
        expect(out.permissionDecision).toBe("ask");
        expect(NOT_SCANNED.exec(out.permissionDecisionReason)?.[1]).toBe("2");
    });
    it("still reports a finding in what fits, with the note, and not one past the cap", async () => {
        const early = fakeKey();
        const late = fakeKey();
        const root = repoWithUntracked(4, 100, {
            "a-early.ts": `export const k = '${early}';\n`,
            "z-late.ts": `export const k = '${late}';\n`,
        });
        const { stdout } = await hookRun(root, bash(root, "git add -A && git commit -m x"), {
            ...off,
            CLAUDE_HOOK_MAX_FILES: "3",
        });
        const out = JSON.parse(stdout).hookSpecificOutput;
        expect(out.permissionDecision).toBe("ask");
        expect(out.permissionDecisionReason).toContain("a-early.ts:1");
        expect(out.permissionDecisionReason).not.toContain("z-late.ts");
        expect(out.permissionDecisionReason).toMatch(NOT_SCANNED);
        expect(out.permissionDecisionReason).not.toContain(early);
    });
    it("scans the staged diff before any untracked file", async () => {
        const key = fakeKey();
        const root = repoWithUntracked(3, 100);
        writeFileSync(path.join(root, "staged.ts"), `export const k = '${key}';\n`);
        execFileSync("git", ["add", "staged.ts"], { cwd: root, stdio: "ignore" });
        const { stdout } = await hookRun(root, bash(root, "git add -A && git commit -m x"), {
            ...off,
            CLAUDE_HOOK_MAX_FILES: "1",
        });
        const out = JSON.parse(stdout).hookSpecificOutput;
        expect(out.permissionDecisionReason).toContain("staged.ts:1");
        expect(out.permissionDecisionReason).toMatch(NOT_SCANNED);
    });
    it("stays silent when the commit is under both caps", async () => {
        const root = repoWithUntracked(3, 100);
        const { stdout } = await hookRun(root, bash(root, "git add -A && git commit -m x"), {
            ...off,
            CLAUDE_HOOK_MAX_FILES: "3",
            CLAUDE_HOOK_MAX_BYTES: "100000",
        });
        expect(decision(stdout)).toBe("allow");
    });
    it("asks on 500 small untracked files with the default caps, within seconds", async () => {
        const root = repoWithUntracked(500, 100);
        const started = Date.now();
        const { stdout } = await hookRun(root, bash(root, "git add -A && git commit -m x"), off);
        expect(Date.now() - started).toBeLessThan(15_000);
        const out = JSON.parse(stdout).hookSpecificOutput;
        expect(out.permissionDecision).toBe("ask");
        expect(NOT_SCANNED.exec(out.permissionDecisionReason)?.[1]).toBe("100");
    });
    it.skipIf(!gitleaksAvailable)("denies with gitleaks and appends the note when something was not scanned", async () => {
        const key = fakeKey();
        const root = repoWithUntracked(4, 100, { "a-early.ts": keyLines(key) });
        const { stdout } = await hookRun(root, bash(root, "git add -A && git commit -m x"), {
            CLAUDE_HOOK_MAX_FILES: "3",
        });
        const out = JSON.parse(stdout).hookSpecificOutput;
        expect(out.permissionDecision).toBe("deny");
        expect(out.permissionDecisionReason).toContain("a-early.ts");
        expect(out.permissionDecisionReason).toMatch(NOT_SCANNED);
    });
});
describe("commit-secrets hook: the line index", () => {
    const diffOf = (n) => [
        "diff --git a/big.ts b/big.ts",
        "--- a/big.ts",
        "+++ b/big.ts",
        `@@ -0,0 +1,${n} @@`,
        ...Array.from({ length: n }, (_, i) => `+const v${i} = ${i};`),
        "",
    ].join("\n");
    it("builds the index once and agrees with locateInDiff on every line", () => {
        const text = diffOf(20);
        const index = indexDiff(text);
        const total = text.split("\n").length;
        for (let n = 0; n <= total + 2; n++)
            expect(index.get(n) ?? null, `line ${n}`).toEqual(locateInDiff(text, n));
        expect(index.get(5)).toEqual({ file: "big.ts", line: 1 });
    });
    it("looks up thousands of findings in a large text without rebuilding", () => {
        const text = diffOf(100_000);
        const index = indexDiff(text);
        const started = Date.now();
        for (let n = 5; n < 3005; n++)
            expect(index.get(n)).toBeDefined();
        expect(Date.now() - started).toBeLessThan(2000);
    });
});
describe("commit-secrets hook: heredoc bodies and comments are not commands", () => {
    it("does not read a git commit inside a heredoc body as a commit", async () => {
        const root = repo(`export const k = '${fakeKey()}';\n`);
        const command = [
            "cat > notes/howto.md <<'EOF'",
            "To ship: git add . && git commit -m wip",
            "EOF",
        ].join("\n");
        expect(commitRepo(command, "/p")).toBeNull();
        expect(scanPlan(command)).toBeNull();
        const { stdout } = await hookRun(root, bash(root, command), off);
        expect(decision(stdout)).toBe("allow");
    });
    it("does not read a git commit in a full-line comment as a commit", () => {
        expect(commitRepo("# then git commit -a\nnpm test", "/p")).toBeNull();
        expect(scanPlan("  # git commit -am x")).toBeNull();
    });
    it("still sees a real commit after a heredoc and a comment", () => {
        const command = [
            "# ship it",
            "cat <<EOF > msg.txt",
            "git commit -a",
            "EOF",
            'git -C "/my repo" commit -am x',
        ].join("\n");
        expect(commitRepo(command, "/p")).toBe("/my repo");
        expect(scanPlan(command)?.unstaged).toBe(true);
    });
});
describe("commit-secrets hook: gitleaks trouble falls back to the patterns", () => {
    /** A `gitleaks` that passes the version probe and fails everything else, as an older build without `stdin` does. */
    function fakeGitleaks(body) {
        const dir = mkdtempSync(path.join(tmpdir(), "claude-fake-gitleaks-"));
        const file = path.join(dir, "gitleaks");
        writeFileSync(file, `#!/bin/sh\n[ "$1" = version ] && exit 0\n${body}\n`, { mode: 0o755 });
        return file;
    }
    it("runs the pattern scan when gitleaks exits 1", async () => {
        const root = repo(`export const k = '${fakeKey()}';\n`);
        const { stdout } = await hookRun(root, bash(root, "git commit -m x"), {
            CLAUDE_HOOK_GITLEAKS_BIN: fakeGitleaks("exit 1"),
        });
        const out = JSON.parse(stdout).hookSpecificOutput;
        expect(out.permissionDecision).toBe("ask");
        expect(out.permissionDecisionReason).toContain("settings.ts:1");
    });
    it("runs the pattern scan when gitleaks exits with an unexpected status", async () => {
        const root = repo(`export const k = '${fakeKey()}';\n`);
        const { stdout } = await hookRun(root, bash(root, "git commit -m x"), {
            CLAUDE_HOOK_GITLEAKS_BIN: fakeGitleaks("exit 3"),
        });
        expect(decision(stdout)).toBe("ask");
    });
    it("runs the pattern scan when gitleaks reports findings that map to no added line", async () => {
        const root = repo(`export const k = '${fakeKey()}';\n`);
        // line 1 of the scanned text is the diff header, not an added line
        const body = 'while [ $# -gt 0 ]; do [ "$1" = --report-path ] && r="$2"; shift; done\necho \'[{"RuleID":"fake","StartLine":1}]\' > "$r"\nexit 2';
        const { stdout } = await hookRun(root, bash(root, "git commit -m x"), {
            CLAUDE_HOOK_GITLEAKS_BIN: fakeGitleaks(body),
        });
        const out = JSON.parse(stdout).hookSpecificOutput;
        expect(out.permissionDecision).toBe("ask");
        expect(out.permissionDecisionReason).toContain("settings.ts:1");
    });
    it("allows a clean change when gitleaks fails", async () => {
        const root = repo("export const a = 1;\n");
        const { stdout } = await hookRun(root, bash(root, "git commit -m x"), {
            CLAUDE_HOOK_GITLEAKS_BIN: fakeGitleaks("exit 1"),
        });
        expect(decision(stdout)).toBe("allow");
    });
    it("uses the pattern scan when the gitleaks binary does not exist", async () => {
        const root = repo(`export const k = '${fakeKey()}';\n`);
        const { stdout } = await hookRun(root, bash(root, "git commit -m x"), {
            CLAUDE_HOOK_GITLEAKS_BIN: "/nonexistent/gitleaks",
        });
        expect(decision(stdout)).toBe("ask");
    });
});
describe("commit-secrets patterns stay silent on legitimate text", () => {
    const found = (line) => scanDiff(["diff --git a/a.ts b/a.ts", "--- a/a.ts", "+++ b/a.ts", "@@ -0,0 +1 @@", `+${line}`].join("\n"));
    it("does not read a hyphenated name that ends in sk as an API key", () => {
        expect(found('describe("task-manager-integration-tests", () => {});')).toEqual([]);
        expect(found('const id = "risk-assessment-for-every-microsite";')).toEqual([]);
        // built at runtime: the source file holds no token-shaped string for the fallback scanner to ask about
        const sk = "sk-" + "a".repeat(24);
        expect(found(`const k = "${sk}";`)).toHaveLength(1);
        expect(found(`const k = '${sk}';`)).toHaveLength(1);
    });
    it("bounds the other token shapes the same way", () => {
        const alnum = (n) => "a1B2c3D4e5F6g7H8i9J0".repeat(3).slice(0, n);
        const slack = "xoxb" + "-1234567890-" + "abcdef";
        expect(found(`const x = "myghp_${alnum(36)}";`)).toEqual([]);
        expect(found(`const x = "ghp_${alnum(36)}";`)).toHaveLength(1);
        expect(found(`const x = "some${slack}";`)).toEqual([]);
        expect(found(`const x = "${slack}";`)).toHaveLength(1);
        expect(found(`const x = "NotAIza${alnum(35)}";`)).toEqual([]);
        expect(found(`const x = "AIza${alnum(35)}";`)).toHaveLength(1);
        expect(found('const x = "XAKIAABCDEFGHIJKLMNOPQRS";')).toEqual([]);
    });
    it("does not take a line starting with ++ in an added hunk for a file header", () => {
        const key = fakeKey();
        const diff = [
            "diff --git a/a.ts b/a.ts",
            "--- a/a.ts",
            "+++ b/a.ts",
            "@@ -0,0 +1,2 @@",
            "+++ not a header",
            `+k = '${key}'`,
        ].join("\n");
        expect(scanDiff(diff)).toEqual([{ name: expect.any(String), file: "a.ts", line: 2 }]);
    });
});
describe("commit-secrets cloud credential patterns", () => {
    const names = (line) => scanDiff(["diff --git a/a.ts b/a.ts", "--- a/a.ts", "+++ b/a.ts", "@@ -0,0 +1 @@", `+${line}`].join("\n")).map((f) => f.name);
    // Every value is built at runtime, so this file holds no secret-shaped string for the scanner to ask about.
    const random = (n, alphabet = "abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789") => Array.from({ length: n }, () => alphabet[randomInt(alphabet.length)]).join("");
    it("finds an Entra client secret by its 8Q~ marker", () => {
        const secret = `${random(3)}8Q${"~"}${random(34)}`;
        expect(names(`AUTH_X = "${secret}"`)).toEqual(["Entra client secret"]);
        expect(names(`AUTH_X = "${"8Q"}${"~"}${random(34)}"`)).toEqual([]);
        expect(names(`const label = "ab8Q${"~"}short";`)).toEqual([]);
    });
    it("does not flag an application's own AUTH_SECRET assignment: only the cloud credential shapes are built in", () => {
        expect(names(`AUTH_SECRET=${random(32)}`)).toEqual([]);
        expect(names("AUTH_SECRET=")).toEqual([]);
        expect(names("const secret = process.env.AWS_SECRET_ACCESS_KEY;")).toEqual([]);
    });
    it("finds AWS keys, including temporary ones, and a secret access key assignment", () => {
        expect(names(`const id = "${"AKIA"}${random(16, "ABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789")}";`)).toEqual(["AWS access key"]);
        expect(names(`const id = "${"ASIA"}${random(16, "ABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789")}";`)).toEqual(["AWS access key"]);
        expect(names(`AWS_SECRET_ACCESS_KEY=${random(40)}`)).toEqual(["AWS secret access key"]);
        expect(names("AWS_SECRET_ACCESS_KEY=")).toEqual([]);
        expect(names("AWS_REGION=us-east-1")).toEqual([]);
        expect(names(`const id = "${"ASIA"}tion-is-a-word";`)).toEqual([]);
    });
    it("finds an Azure storage account key and a SAS signature, not the account name or an unsigned URL", () => {
        const key = random(86, "abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789+/");
        expect(names(`DefaultEndpointsProtocol=https;AccountName=examplereports;AccountKey=${key}==;EndpointSuffix=core.windows.net`)).toEqual(["Azure storage account key"]);
        expect(names("DefaultEndpointsProtocol=https;AccountName=examplereports;EndpointSuffix=core.windows.net")).toEqual([]);
        expect(names(`https://examplereports.blob.core.windows.net/reports/a.json?sv=2024-01-01&sig=${random(43)}%3D`)).toEqual(["Azure SAS signature"]);
        expect(names("https://examplereports.blob.core.windows.net/reports/a.json?sv=2024-01-01")).toEqual([]);
        expect(names('const sig = "short";')).toEqual([]);
    });
    it("asks before a commit that stages an AWS_SECRET_ACCESS_KEY with a real value, and allows the empty example", async () => {
        const root = repo("");
        writeFileSync(path.join(root, ".env.example"), "AWS_SECRET_ACCESS_KEY=\n");
        execFileSync("git", ["add", ".env.example"], { cwd: root });
        expect((await hookRun(root, bash(root, "git commit -m x"), off)).stdout).toBe("");
        writeFileSync(path.join(root, ".env.example"), `AWS_SECRET_ACCESS_KEY=${random(40)}\n`);
        execFileSync("git", ["add", ".env.example"], { cwd: root });
        const { stdout } = await hookRun(root, bash(root, "git commit -m x"), off);
        expect(JSON.parse(stdout).hookSpecificOutput.permissionDecision).toBe("ask");
        expect(stdout).toContain("AWS secret access key in .env.example:1");
    });
});
describe("a write in the same command, against a real repository", () => {
    it("stays silent with -a on a file outside the repository and on a new untracked file", async () => {
        const root = committedRepo({ "src/keep.ts": "export const a = 1;\n" });
        for (const command of [
            "echo x > /tmp/claude-log.txt && git commit -am x",
            "echo x > src/new.ts && git commit -am x",
            "echo x > src/new.ts && git add -u && git commit -m x",
        ]) {
            const { stdout } = await hookRun(root, bash(root, command), off);
            expect(decision(stdout), command).toBe("allow");
        }
    });
    it("still asks for a write to a tracked file with -a, and for a new file that a covering add follows", async () => {
        const root = committedRepo({ "src/keep.ts": "export const a = 1;\n" });
        for (const command of [
            "echo x >> src/keep.ts && git commit -am x",
            "echo x > src/new.ts && git add src && git commit -m x",
        ]) {
            const { stdout } = await hookRun(root, bash(root, command), off);
            expect(decision(stdout), command).toBe("ask");
        }
    });
    it("stays silent on a write after the add that would cover it, or after the commit", async () => {
        const root = committedRepo({ "src/keep.ts": "export const a = 1;\n" });
        for (const command of [
            "git add src && echo x > src/new.ts && git commit -m x",
            "git commit -am x && echo x >> src/keep.ts",
        ]) {
            const { stdout } = await hookRun(root, bash(root, command), off);
            expect(decision(stdout), command).toBe("allow");
        }
    });
    it("stays silent when the add is in another repository", async () => {
        const root = committedRepo({ "src/keep.ts": "export const a = 1;\n" });
        const other = committedRepo({ "b.ts": "export const b = 1;\n" });
        const { stdout } = await hookRun(root, bash(root, `echo x > src/new.ts && git -C ${other} add . && git commit -m x`), off);
        expect(decision(stdout)).toBe("allow");
    });
});
describe("git commit -a commits the working tree of tracked files, not the index", () => {
    it("stays silent on a key staged and then removed in the working tree", async () => {
        const root = committedRepo({ "a.ts": "export const a = 1;\n" }, { "a.ts": `export const k = '${fakeKey()}';\n` });
        execFileSync("git", ["add", "a.ts"], { cwd: root, stdio: "ignore" });
        writeFileSync(path.join(root, "a.ts"), "export const a = 2;\n");
        const { stdout, code } = await hookRun(root, bash(root, "git commit -am clean"), off);
        expect(code).toBe(0);
        expect(stdout).toBe("");
    });
    it("still asks for a key in the working tree with -a, staged or not", async () => {
        const root = committedRepo({ "a.ts": "export const a = 1;\n" }, { "a.ts": "export const a = 2;\n" });
        execFileSync("git", ["add", "a.ts"], { cwd: root, stdio: "ignore" });
        writeFileSync(path.join(root, "a.ts"), `export const k = '${fakeKey()}';\n`);
        const { stdout } = await hookRun(root, bash(root, "git commit -am x"), off);
        expect(decision(stdout)).toBe("ask");
    });
    it("keeps scanning the staged diff without -a", async () => {
        const root = committedRepo({ "a.ts": "export const a = 1;\n" }, { "a.ts": `export const k = '${fakeKey()}';\n` });
        execFileSync("git", ["add", "a.ts"], { cwd: root, stdio: "ignore" });
        writeFileSync(path.join(root, "a.ts"), "export const a = 2;\n");
        const { stdout } = await hookRun(root, bash(root, "git commit -m x"), off);
        expect(decision(stdout)).toBe("ask");
    });
});
describe("an add in another repository", () => {
    it("is left out of the scan of this repository's commit", async () => {
        const key = fakeKey();
        const main = committedRepo({ "a.ts": "export const a = 1;\n" }, {});
        writeFileSync(path.join(main, "a.ts"), "export const a = 2;\n");
        execFileSync("git", ["add", "a.ts"], { cwd: main, stdio: "ignore" });
        const other = committedRepo({ "b.ts": "export const b = 1;\n" }, { "leak.ts": `export const k = '${key}';\n` });
        const { stdout, code } = await hookRun(main, bash(main, `git -C ${other} add . && git commit -m clean`), off);
        expect(code).toBe(0);
        expect(stdout).toBe("");
    });
});
describe("commit-secrets hook: a file written and added in the same command", () => {
    const SENTENCE = "are written and added in the same command, so the commit-secrets hook cannot scan them before they exist. Run the write first, then add and commit in a separate command.";
    /** Tracked means "under /p", the repository of these pure tests; every add is in that repository unless a test says otherwise. */
    const writes = (command, deps = {}) => unscannableWrites(command, scanPlan(command, "/p"), {
        base: "/p",
        isTracked: (f) => f.startsWith("/p/"),
        ...deps,
    });
    it.each([
        [
            'printf "x" > src/planted.ts && git add src/planted.ts && git commit -m x',
            ["src/planted.ts"],
        ],
        ["echo key >> a.ts && git commit -am x", ["a.ts"]],
        ["cp /tmp/leak.ts src/ && git add src && git commit -m x", ["src/"]],
        ["echo x | tee src/deep/t.ts && git add src && git commit -m x", ["src/deep/t.ts"]],
        ['sed -i "s/a/b/" src/a.ts && git add . && git commit -m x', ["src/a.ts"]],
        ["echo x > a.ts && git add -A && git commit -m x", ["a.ts"]],
        ["cd sub && echo x >> a.ts && cd .. && git commit -am x", ["a.ts"]],
        ["echo secret>src/a.ts && git add src/a.ts && git commit -m x", ["src/a.ts"]],
        ["echo secret>>a.ts && git commit -am x", ["a.ts"]],
        ['echo secret>"src/a.ts" && git add src/a.ts && git commit -m x', ["src/a.ts"]],
    ])("lists the targets the commit would include: %s", (command, expected) => {
        expect(writes(command)).toEqual(expected);
    });
    it.each([
        ["echo x > /tmp/log.txt && git add src && git commit -m x"],
        ["echo x > other/a.ts && git add src && git commit -m x"],
        ["echo x > srcs/a.ts && git add src && git commit -m x"],
        ["echo x > src/a.ts && git commit -m x"],
        ["git add src && git commit -m x"],
        ["cat > notes/x.md <<'EOF'\necho a > src/b.ts\nEOF\ngit add src && git commit -m x"],
        ['echo "a > src/b.ts" && git add src && git commit -m x'],
        ['echo "a>src/b.ts" && git add src && git commit -m x'],
        ["echo x 2>&1 && git add src && git commit -m x"],
        ["grep \\<foo\\> README.md && git add . && git commit -m x"],
        ["git log --format=%h->%s -1 && git add . && git commit -m x"],
        ["node -e a=>b && git add -A && git commit -m x"],
        ["npm test > src/out.txt"],
    ])("stays empty when nothing written is covered: %s", (command) => {
        expect(writes(command)).toEqual([]);
    });
    describe("with -a only tracked files of this repository enter the commit", () => {
        it("stays empty for a target outside the repository", () => {
            expect(writes("echo x > /tmp/log.txt && git commit -am x")).toEqual([]);
        });
        it("stays empty for a target that is not tracked, and counts it once it is", () => {
            expect(writes("echo x > new.ts && git commit -am x", { isTracked: () => false })).toEqual([]);
            expect(writes("echo x > new.ts && git commit -am x", { isTracked: () => true })).toEqual([
                "new.ts",
            ]);
        });
    });
    describe("only a write before the add, or before the commit with -a, enters that commit", () => {
        it.each([
            ["git add a.ts && echo x > a.ts && git commit -m x"],
            ["git add . && echo x > a.ts && git commit -m x"],
            ["git commit -am x && echo x > a.ts"],
        ])("stays empty when the write comes later: %s", (command) => {
            expect(writes(command)).toEqual([]);
        });
        it("counts the write ahead of the add even when an earlier add covers something else", () => {
            expect(writes("git add b.ts && echo x > a.ts && git add a.ts && git commit -m x")).toEqual([
                "a.ts",
            ]);
        });
    });
    describe("git add -u stages no new file", () => {
        it("stays empty for an untracked target, and counts a tracked one", () => {
            expect(writes("echo x > src/new.ts && git add -u && git commit -m x", { isTracked: () => false })).toEqual([]);
            expect(writes("echo x > src/new.ts && git add -u && git commit -m x", { isTracked: () => true })).toEqual(["src/new.ts"]);
        });
    });
    describe("an add in another repository stages nothing here", () => {
        it("is skipped, and an add in this repository still counts", () => {
            const sameRepo = (dir) => dir !== "/p/other";
            expect(writes("echo x > a.ts && git -C other add . && git commit -m x", { sameRepo })).toEqual([]);
            expect(writes("echo x > a.ts && git -C other add . && git add a.ts && git commit -m x", {
                sameRepo,
            })).toEqual(["a.ts"]);
        });
    });
    it("asks and names the path when a write and an add share a command", async () => {
        const root = committedRepo({ "src/keep.ts": "export const a = 1;\n" });
        for (const [command, name] of [
            [
                'printf "x" > src/planted.ts && git add src/planted.ts && git commit -m x',
                "src/planted.ts",
            ],
            ["echo key >> src/keep.ts && git commit -am x", "src/keep.ts"],
            ["cp /tmp/leak.ts src/ && git add src && git commit -m x", "src/"],
        ]) {
            const { stdout } = await hookRun(root, bash(root, command), off);
            const out = JSON.parse(stdout).hookSpecificOutput;
            expect(out.permissionDecision, command).toBe("ask");
            expect(out.permissionDecisionReason, command).toContain(`${name} ${SENTENCE}`);
        }
    });
    it("counts a heredoc target", async () => {
        const root = committedRepo({ "src/keep.ts": "export const a = 1;\n" });
        const command = [
            "cat > notes/x.md <<'EOF'",
            "text",
            "EOF",
            "git add notes && git commit -m x",
        ].join("\n");
        const { stdout } = await hookRun(root, bash(root, command), off);
        const out = JSON.parse(stdout).hookSpecificOutput;
        expect(out.permissionDecision).toBe("ask");
        expect(out.permissionDecisionReason).toContain(`notes/x.md ${SENTENCE}`);
    });
    it("stays silent on a target outside the add paths and on a write in a heredoc body or a quoted string", async () => {
        const root = committedRepo({ "src/keep.ts": "export const a = 1;\n" });
        const commands = [
            "echo x > /tmp/log.txt && git add src && git commit -m x",
            [
                "cat > /tmp/n.md <<'EOF'",
                "echo a > src/b.ts",
                "EOF",
                "git add src && git commit -m x",
            ].join("\n"),
            'echo "a > src/b.ts" && git add src && git commit -m x',
        ];
        for (const command of commands) {
            const { stdout } = await hookRun(root, bash(root, command), off);
            expect(decision(stdout), command).toBe("allow");
        }
    });
    it("still asks about a key in a file that exists already, without the same-command sentence", async () => {
        const root = committedRepo({ "src/keep.ts": "export const a = 1;\n" }, { "src/a.ts": `export const k = '${fakeKey()}';\n` });
        const { stdout } = await hookRun(root, bash(root, "git add src/a.ts && git commit -m x"), off);
        const out = JSON.parse(stdout).hookSpecificOutput;
        expect(out.permissionDecision).toBe("ask");
        expect(out.permissionDecisionReason).toContain("src/a.ts:1");
        expect(out.permissionDecisionReason).not.toContain("same command");
    });
    it("appends the sentence to a finding in what could be scanned", async () => {
        const root = committedRepo({ "src/keep.ts": "export const a = 1;\n" }, { "src/a.ts": `export const k = '${fakeKey()}';\n` });
        const { stdout } = await hookRun(root, bash(root, "echo x > src/new.ts && git add src && git commit -m x"), off);
        const out = JSON.parse(stdout).hookSpecificOutput;
        expect(out.permissionDecision).toBe("ask");
        expect(out.permissionDecisionReason).toContain("src/a.ts:1");
        expect(out.permissionDecisionReason).toContain(`src/new.ts ${SENTENCE}`);
    });
});
