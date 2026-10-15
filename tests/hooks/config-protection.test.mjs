import { mkdirSync, mkdtempSync, symlinkSync, writeFileSync } from "node:fs";
import { tmpdir } from "node:os";
import path from "node:path";
import { afterEach, beforeEach, describe, expect, it } from "./compat.mjs";
import { runHook } from "./hookRun.mjs";
import { decide, globToRegExp, isProtected, projectExtras } from "../../hooks/config-protection.mjs";
const HOOKS = path.resolve(import.meta.dirname, "../../hooks");
const hook = path.resolve(HOOKS, "config-protection.mjs");
// The shell helpers are reached by a dynamic import of a path, like the stop-check test does, so no type directive is needed.
const { segmentsWithCwd } = await import(path.resolve(HOOKS, "shell.mjs"));
const hookRun = (root, event) => runHook(hook, event, { root });
function project() {
    const root = mkdtempSync(path.join(tmpdir(), "claude-hook-"));
    mkdirSync(path.join(root, ".github", "workflows"), { recursive: true });
    mkdirSync(path.join(root, "src"), { recursive: true });
    writeFileSync(path.join(root, "eslint.config.js"), "export default [];\n");
    writeFileSync(path.join(root, ".github", "workflows", "ci.yml"), "name: ci\n");
    writeFileSync(path.join(root, "src", "a.ts"), "export const a = 1;\n");
    mkdirSync(path.join(root, "app"), { recursive: true });
    writeFileSync(path.join(root, "app", "page.tsx"), "export default function Page() {\n  return null;\n}\n");
    mkdirSync(path.join(root, ".git", "x"), { recursive: true });
    writeFileSync(path.join(root, ".git", "x", "tsconfig.json"), "{}\n");
    for (const name of [
        "tsconfig.json",
        "next.config.ts",
        "playwright.config.ts",
        "vitest.config.mts",
        "jest.config.js",
        "biome.json",
        ".prettierrc",
        "prettier.config.mjs",
        ".github/dependabot.yml",
    ]) {
        writeFileSync(path.join(root, name), "\n");
    }
    return root;
}
const edit = (root, rel, tool = "Edit") => ({
    hook_event_name: "PreToolUse",
    tool_name: tool,
    cwd: root,
    tool_input: { file_path: path.join(root, rel) },
});
describe("config-protection hook, the default files", () => {
    it.each([
        "tsconfig.json",
        "playwright.config.ts",
        "vitest.config.mts",
        "jest.config.js",
        "biome.json",
        ".prettierrc",
        "prettier.config.mjs",
        ".github/dependabot.yml",
    ])("denies an edit to %s", async (rel) => {
        const root = project();
        const { stdout, code } = await hookRun(root, edit(root, rel));
        expect(code).toBe(0);
        expect(JSON.parse(stdout).hookSpecificOutput).toMatchObject({ permissionDecision: "deny" });
    });
    it.each([".claude/hooks/lib.mjs"])("denies an edit to %s and says it is a hook, not configuration", async (rel) => {
        const root = project();
        mkdirSync(path.dirname(path.join(root, rel)), { recursive: true });
        writeFileSync(path.join(root, rel), "export {};\n");
        const { stdout } = await hookRun(root, edit(root, rel));
        const output = JSON.parse(stdout).hookSpecificOutput;
        expect(output.permissionDecision).toBe("deny");
        expect(output.permissionDecisionReason).toContain("is a session hook");
        expect(output.permissionDecisionReason).not.toContain("configuration file");
    });
    it.each(["app/page.tsx", "next.config.ts", ".git/x/tsconfig.json"])("allows an edit to %s", async (rel) => {
        const root = project();
        expect(await hookRun(root, edit(root, rel))).toMatchObject({ code: 0, stdout: "" });
    });
    it("denies a redirection into tsconfig.json and allows one into next.config.ts and a page", async () => {
        const root = project();
        const bash = (command) => ({
            hook_event_name: "PreToolUse",
            tool_name: "Bash",
            cwd: root,
            tool_input: { command },
        });
        expect(JSON.parse((await hookRun(root, bash("echo x > tsconfig.json"))).stdout).hookSpecificOutput
            .permissionDecision).toBe("deny");
        expect((await hookRun(root, bash("echo x > app/page.tsx"))).stdout).toBe("");
    expect((await hookRun(root, bash("echo x > next.config.ts"))).stdout).toBe("");
    });
});
describe("config-protection hook", () => {
    it("denies an edit to an existing lint configuration, naming the file", async () => {
        const root = project();
        const { stdout, code } = await hookRun(root, edit(root, "eslint.config.js"));
        expect(code).toBe(0);
        const out = JSON.parse(stdout);
        expect(out.hookSpecificOutput.permissionDecision).toBe("deny");
        expect(out.hookSpecificOutput.permissionDecisionReason).toContain("eslint.config.js");
    });
    it("denies a Write to a CI workflow", async () => {
        const root = project();
        const { stdout } = await hookRun(root, edit(root, ".github/workflows/ci.yml", "Write"));
        expect(JSON.parse(stdout).hookSpecificOutput.permissionDecision).toBe("deny");
    });
    it("allows source files, so the hook costs nothing on ordinary edits", async () => {
        const root = project();
        const { stdout, code } = await hookRun(root, edit(root, "src/a.ts"));
        expect(code).toBe(0);
        expect(stdout).toBe("");
    });
    it("allows creating a configuration file that does not exist yet", async () => {
        const root = project();
        const { stdout } = await hookRun(root, edit(root, "vitest.config.ts", "Write"));
        expect(stdout).toBe("");
    });
    it("allows a protected name outside the project root", async () => {
        const root = project();
        const other = project();
        const event = {
            ...edit(root, "src/a.ts"),
            tool_input: { file_path: path.join(other, "eslint.config.js") },
        };
        const { stdout } = await hookRun(root, event);
        expect(stdout).toBe("");
    });
    it("reads the file_path of a MultiEdit, whose edits carry no path of their own", async () => {
        const root = project();
        const event = {
            hook_event_name: "PreToolUse",
            tool_name: "MultiEdit",
            cwd: root,
            tool_input: {
                file_path: path.join(root, "eslint.config.js"),
                edits: [{ old_string: "a", new_string: "b" }],
            },
        };
        const { stdout } = await hookRun(root, event);
        expect(JSON.parse(stdout).hookSpecificOutput.permissionDecision).toBe("deny");
    });
    it("runs its main block through a symlinked path such as /tmp on macOS", async () => {
        const root = project();
        const link = path.join(mkdtempSync(path.join(tmpdir(), "claude-link-")), "hooks");
        symlinkSync(path.dirname(hook), link);
        const { stdout } = await runHook(path.join(link, path.basename(hook)), edit(root, "eslint.config.js"), { root });
        expect(JSON.parse(stdout).hookSpecificOutput.permissionDecision).toBe("deny");
    });
    it("never fails the tool on empty or malformed input", async () => {
        const root = project();
        for (const bad of ["", "{not json", "[]"]) {
            const { stdout, code } = await hookRun(root, bad);
            expect(code).toBe(0);
            expect(stdout).toBe("");
        }
    });
});
/** `decide` reads the project root from CLAUDE_PROJECT_DIR, as Claude Code sets it for a hook process. */
function withProjectDir(root) {
    let previous;
    beforeEach(() => {
        previous = process.env.CLAUDE_PROJECT_DIR;
        process.env.CLAUDE_PROJECT_DIR = root;
    });
    afterEach(() => {
        if (previous === undefined)
            delete process.env.CLAUDE_PROJECT_DIR;
        else
            process.env.CLAUDE_PROJECT_DIR = previous;
    });
}
describe("protected names at any depth", () => {
    withProjectDir("/work/project");
    it.each([
        "eslint.config.js",
        "eslint.config.mjs",
        ".claude/worktrees/x/eslint.config.js",
        "tsconfig.json",
        "packages/a/tsconfig.build.json",
        "vitest.config.mts",
        "a/vitest.config.ts",
        "playwright.config.ts",
        "a/playwright.config.js",
        "jest.config.ts",
        "jest.config.json",
        ".prettierrc",
        ".prettierrc.json",
        "packages/a/prettier.config.js",
        "biome.json",
        "biome.jsonc",
        "a/.github/workflows/ci.yml",
        ".github/workflows/deploy.yaml",
        ".github/dependabot.yml",
        // The guards decide what "green" means too: a session that could edit them could switch a check off.
        ".claude/hooks/config-protection.mjs",
        ".claude/hooks/shell.mjs",
        ".claude/hooks/commit-secrets.mjs",
    ])("protects %s", (rel) => {
        expect(isProtected(rel)).toBe(true);
    });
    it.each([
        "tests/fixtures/site/tsconfig.json",
        "vendor/x/tsconfig.json",
        "node_modules/pkg/eslint.config.js",
        ".next/types/tsconfig.json",
        ".git/x/tsconfig.json",
        "next.config.ts",
        "next.config.mjs",
        "scripts/gates.mjs",
        "app/page.tsx",
        "lib/run.ts",
        "components.json",
        "postcss.config.mjs",
        ".github/skills/x/SKILL.md",
        "my-eslint.config.js",
        "jest.config.notes.md",
        ".prettierignore",
        "scripts/other.mjs",
        ".claude/hooks/README.md",
        ".claude/skills/tdd/SKILL.md",
        "tests/unit/hooks/shell.test.ts",
    ])("does not protect %s", (rel) => {
        expect(isProtected(rel)).toBe(false);
    });
    it("denies a worktree configuration edited from the main session", () => {
        const root = "/work/project";
        const file = `${root}/.claude/worktrees/x/eslint.config.js`;
        const input = { tool_name: "Edit", cwd: root, tool_input: { file_path: file } };
        expect(decide(input, () => true)).toContain(".claude/worktrees/x/eslint.config.js");
    });
    it("allows a fixture or vendored configuration", () => {
        const root = "/work/project";
        for (const rel of [
            "tests/fixtures/site/tsconfig.json",
            "vendor/x/tsconfig.json",
            ".git/x/tsconfig.json",
        ]) {
            const input = { tool_name: "Write", cwd: root, tool_input: { file_path: `${root}/${rel}` } };
            expect(decide(input, () => true)).toBeNull();
        }
    });
});
describe("Bash commands that write a protected file", () => {
    const root = "/work/project";
    withProjectDir(root);
    const present = () => true;
    const run = (command, opts = {}) => decide({ tool_name: "Bash", cwd: opts.cwd ?? root, tool_input: { command } }, opts.exists ?? present);
    it.each([
        ["sed -i '' 's/a/b/' eslint.config.js", "eslint.config.js"],
        ["sed -i.bak -e 's/a/b/' tsconfig.json", "tsconfig.json"],
        ["sed --in-place 's/a/b/' playwright.config.ts", "playwright.config.ts"],
        ["sed -n -i 's/a/b/' .github/workflows/ci.yml", ".github/workflows/ci.yml"],
        ["echo semi > playwright.config.ts", "playwright.config.ts"],
        ["echo x >> eslint.config.mjs", "eslint.config.mjs"],
        ['echo x > "tsconfig.json"', "tsconfig.json"],
        ["cat a | tee tsconfig.json", "tsconfig.json"],
        ["cat a | tee -a vitest.config.ts", "vitest.config.ts"],
        ["cp x eslint.config.js", "eslint.config.js"],
        ["mv x .github/workflows/ci.yml", ".github/workflows/ci.yml"],
        ["cd .claude/worktrees/x && echo x > eslint.config.js", "eslint.config.js"],
        ["echo x > ./.claude/worktrees/x/eslint.config.js", ".claude/worktrees/x/eslint.config.js"],
    ])("denies %s", (command, name) => {
        expect(run(command)).toContain(name);
    });
    it.each([
        ["mv eslint.config.js eslint.old.js", "eslint.config.js"],
        ["mv -f tsconfig.json /tmp/t.json", "tsconfig.json"],
        ["perl -pi -e 's/a/b/' eslint.config.js", "eslint.config.js"],
        ["perl -i.bak -pe 's/a/b/' tsconfig.json", "tsconfig.json"],
        ["perl -i -pe 's/a/b/' playwright.config.ts", "playwright.config.ts"],
    ])("denies moving away or rewriting in place: %s", (command, name) => {
        expect(run(command)).toContain(name);
    });
    it.each([
        "mv docs/a.md docs/b.md",
        "perl -pe 's/a/b/' eslint.config.js",
        "perl -Mstrict script.pl eslint.config.js",
    ])("allows %s", (command) => {
        expect(run(command)).toBeNull();
    });
    it("resolves a relative target against the event cwd", () => {
        const exists = (f) => f === `${root}/.claude/worktrees/x/eslint.config.js`;
        expect(run("echo x > eslint.config.js", { cwd: `${root}/.claude/worktrees/x`, exists })).toContain(".claude/worktrees/x/eslint.config.js");
        expect(run("echo x > eslint.config.js", { cwd: root, exists })).toBeNull();
    });
    describe("a cd earlier in the command moves where the target lands", () => {
        const exists = (f) => f === `${root}/tsconfig.json`;
        it("allows creating a new file in another directory when the root one exists", () => {
            expect(run("cd docs && echo '{}' > tsconfig.json", { exists })).toBeNull();
            expect(run('cd "my docs" && echo x > tsconfig.json', { exists })).toBeNull();
            expect(run("pushd docs && echo x > tsconfig.json", { exists })).toBeNull();
        });
        it("denies a write that climbs back out to the protected file", () => {
            expect(run("cd docs && sed -i 's/a/b/' ../tsconfig.json", { exists })).toContain("tsconfig.json");
            expect(run("cd docs && cd .. && echo x > tsconfig.json", { exists })).toContain("tsconfig.json");
        });
        it("keeps denying a write at the root, before or without a cd", () => {
            expect(run("echo x > tsconfig.json", { exists })).toContain("tsconfig.json");
            expect(run("echo x > tsconfig.json && cd docs", { exists })).toContain("tsconfig.json");
        });
        it("judges a write after a cd it cannot follow against the last known directory and the root", () => {
            expect(run("cd $DIR && echo x > tsconfig.json", { exists })).toContain("tsconfig.json");
            expect(run("cd $HOME/x && echo x > tsconfig.json", { exists })).toContain("tsconfig.json");
            expect(run("cd - && echo x > tsconfig.json", { exists })).toContain("tsconfig.json");
            expect(run("cd src/* && echo x > tsconfig.json", { exists })).toContain("tsconfig.json");
            expect(run("cd $(mktemp -d) && echo x > tsconfig.json", { exists })).toContain("tsconfig.json");
            expect(run("cd && echo x > tsconfig.json", { exists })).toContain("tsconfig.json");
            const inDocs = (f) => f === `${root}/docs/tsconfig.json`;
            expect(run("cd docs && cd $DIR && echo x > tsconfig.json", { exists: inDocs })).toContain("docs/tsconfig.json");
        });
        it("stays silent after an unknown cd on a file that is not protected", () => {
            expect(run("cd $DIR && echo x > notes.json", { exists: () => true })).toBeNull();
        });
        it("follows an absolute cd again after an unknown one", () => {
            expect(run("cd $DIR && cd /tmp && echo x > tsconfig.json", { exists })).toBeNull();
            expect(run(`cd $DIR && cd ${root} && echo x > tsconfig.json`, { exists })).toContain("tsconfig.json");
        });
        it("pops back with popd, so a write after pushd and popd lands at the root", () => {
            expect(run("pushd docs && popd && echo x > tsconfig.json", { exists })).toContain("tsconfig.json");
            expect(run("pushd docs && echo x > tsconfig.json && popd", { exists })).toBeNull();
            expect(run("popd && echo x > tsconfig.json", { exists })).toContain("tsconfig.json");
        });
        it("restores the directory when a parenthesized group closes", () => {
            expect(run("cd docs && (cd .. && echo x > tsconfig.json)", { exists })).toContain("tsconfig.json");
            const second = (f) => f === `${root}/docs/tsconfig.json`;
            const inSub = (f) => f === `${root}/docs/sub/tsconfig.json`;
            const command = "cd docs && (cd sub && echo x > tsconfig.json) && echo y > tsconfig.json";
            expect(run(command, { exists: second })).toContain("docs/tsconfig.json");
            expect(run(command, { exists: inSub })).toContain("docs/sub/tsconfig.json");
            expect(run(command, { exists: (f) => f === `${root}/tsconfig.json` })).toBeNull();
        });
        it("keeps a cd made inside braces", () => {
            expect(run("{ cd docs; } && echo x > tsconfig.json", {
                exists: (f) => f === `${root}/docs/tsconfig.json`,
            })).toContain("docs/tsconfig.json");
            expect(run("{ cd docs; } && echo x > tsconfig.json", { exists })).toBeNull();
        });
        it("does not let a cd inside a subshell leak out", () => {
            expect(run("(cd docs && true); echo x > tsconfig.json", { exists })).toContain("tsconfig.json");
        });
        it("resolves each segment against its own directory", () => {
            expect(run("cd docs && echo a > a.md && cd .. && echo x > tsconfig.json", { exists })).toContain("tsconfig.json");
        });
    });
    it("falls back to the project directory without a cwd", () => {
        const input = { tool_name: "Bash", tool_input: { command: "echo x > eslint.config.js" } };
        expect(decide(input, present)).toContain("eslint.config.js");
    });
    it.each([
        "cat eslint.config.js",
        "grep x tsconfig.json",
        "git diff eslint.config.js",
        "node scripts/x.mjs > reports/out.json",
        "sed -n 1,5p eslint.config.js",
        "sed -i s/a/b/ src/a.ts",
        "cp eslint.config.js backup/",
        "cp eslint.config.js /tmp/x.js",
        "echo x > tests/fixtures/site/tsconfig.json",
        'echo "write to > eslint.config.js"',
        'git commit -m "sed -i x eslint.config.js"',
        "echo x > /somewhere/else/eslint.config.js",
        "",
    ])("allows %s", (command) => {
        expect(run(command)).toBeNull();
    });
    it("allows writing a configuration file that does not exist yet", () => {
        expect(run("echo x > eslint.config.js", { exists: () => false })).toBeNull();
    });
    it("does not read a heredoc body as a command", () => {
        expect(run("cat > docs/x.md <<'EOF'\nsed -i x eslint.config.js\necho y > tsconfig.json\nEOF")).toBeNull();
    });
});
describe("segmentsWithCwd", () => {
    const cwds = (command) => segmentsWithCwd(command, "/r", "/home/u").map((p) => p.cwd);
    it("follows cd, including cd ..", () => {
        expect(cwds("cd docs && echo a && cd .. && echo b")).toEqual([
            "/r",
            "/r/docs",
            "/r/docs",
            "/r",
        ]);
    });
    it("follows a pushd and its popd, and ignores a popd with an empty stack", () => {
        expect(cwds("pushd docs && echo a && popd && echo b")).toEqual([
            "/r",
            "/r/docs",
            "/r/docs",
            "/r",
        ]);
        expect(cwds("pushd a && pushd b && popd && echo x && popd && echo y")).toEqual([
            "/r",
            "/r/a",
            "/r/a/b",
            "/r/a",
            "/r/a",
            "/r",
        ]);
        expect(cwds("popd && echo a")).toEqual(["/r", "/r"]);
    });
    it("applies a cd in a group to what follows inside it and restores the directory at the close", () => {
        expect(cwds("(cd sub && echo a) && echo b")).toEqual(["/r", "/r/sub", "/r"]);
        expect(cwds("cd docs && (cd .. && echo a) && echo b")).toEqual([
            "/r",
            "/r/docs",
            "/r",
            "/r/docs",
        ]);
    });
    it("nests groups, and a brace group keeps its cd", () => {
        expect(cwds("(cd a && (cd b && echo x) && echo y) && echo z")).toEqual([
            "/r",
            "/r/a",
            "/r/a/b",
            "/r/a",
            "/r",
        ]);
        expect(cwds("{ cd sub; } && echo b").at(-1)).toBe("/r/sub");
    });
    it("marks the directory unknown after a cd it cannot follow, with the last known one, until an absolute cd", () => {
        const parts = segmentsWithCwd("cd docs && cd $D && echo a && cd /tmp && echo b", "/r", "/home/u");
        expect(parts.map((p) => p.cwd)).toEqual([
            "/r",
            "/r/docs",
            null,
            null,
            "/tmp",
        ]);
        expect(parts[3].lastKnown).toBe("/r/docs");
        expect(parts[4].lastKnown).toBe("/tmp");
    });
    it("does not read a cd inside a quoted string or a heredoc body as a cd", () => {
        expect(cwds('echo "x; cd docs" && echo a')).toEqual(["/r", "/r"]);
        expect(cwds("echo 'cd docs' && echo a")).toEqual(["/r", "/r"]);
        expect(cwds("cat <<EOF\ncd docs\nEOF\necho a")).toEqual(["/r", "/r"]);
    });
});
describe("config-protection hook, Bash end to end", () => {
    it("denies a redirection onto an existing configuration file", async () => {
        const root = project();
        const event = {
            hook_event_name: "PreToolUse",
            tool_name: "Bash",
            cwd: root,
            tool_input: { command: "echo x > eslint.config.js" },
        };
        const { stdout, code } = await hookRun(root, event);
        expect(code).toBe(0);
        expect(JSON.parse(stdout).hookSpecificOutput.permissionDecision).toBe("deny");
    });
    it.each([
        "echo x>eslint.config.js",
        "echo x>>eslint.config.js",
        "echo x 2>eslint.config.js",
        "echo x>|eslint.config.js",
        'echo x>"eslint.config.js"',
        "echo x>>'eslint.config.js'",
    ])("denies an attached redirection onto an existing configuration file: %s", async (command) => {
        const root = project();
        const event = { hook_event_name: "PreToolUse", tool_name: "Bash", cwd: root, tool_input: { command } };
        const { stdout } = await hookRun(root, event);
        expect(JSON.parse(stdout).hookSpecificOutput.permissionDecision).toBe("deny");
    });
    it.each([
        "grep -n \\<foo\\> eslint.config.js",
        "find . -name x \\> eslint.config.js",
        "git log --format=%h->%s -1 eslint.config.js",
        "node -e a=>b eslint.config.js",
    ])("stays silent when a > is escaped or part of an arrow: %s", async (command) => {
        const root = project();
        const event = { hook_event_name: "PreToolUse", tool_name: "Bash", cwd: root, tool_input: { command } };
        const { stdout, code } = await hookRun(root, event);
        expect(code).toBe(0);
        expect(stdout).toBe("");
    });
    it("stays silent on a read", async () => {
        const root = project();
        const event = {
            hook_event_name: "PreToolUse",
            tool_name: "Bash",
            cwd: root,
            tool_input: { command: "cat eslint.config.js" },
        };
        expect((await hookRun(root, event)).stdout).toBe("");
    });
});

describe("config-protection hook, .claude/protected-files", () => {
    const listed = (lines) => {
        const root = project();
        mkdirSync(path.join(root, ".claude"), { recursive: true });
        writeFileSync(path.join(root, ".claude", "protected-files"), lines);
        mkdirSync(path.join(root, "config"), { recursive: true });
        for (const name of ["Makefile", "config/app.yml", "config/other.json", "src/keep.ts"])
            writeFileSync(path.join(root, name), "x\n");
        return root;
    };
    const denied = async (root, rel) => (await hookRun(root, edit(root, rel))).stdout !== "";
    it("protects the extra globs, skipping blank and # lines", async () => {
        const root = listed("# build\n\nMakefile\nconfig/*.yml\n");
        expect(await denied(root, "Makefile")).toBe(true);
        expect(await denied(root, "config/app.yml")).toBe(true);
    });
    it("leaves everything the list does not name alone", async () => {
        const root = listed("# build\n\nMakefile\nconfig/*.yml\n");
        expect(await denied(root, "config/other.json")).toBe(false);
        expect(await denied(root, "src/keep.ts")).toBe(false);
    });
    it("keeps the built-in names when the list exists", async () => {
        const root = listed("Makefile\n");
        expect(await denied(root, "tsconfig.json")).toBe(true);
    });
    it("protects an extra name from a Bash write and allows creating a new one", async () => {
        const root = listed("Makefile\nnew.cfg\n");
        const bash = (command) => ({ hook_event_name: "PreToolUse", tool_name: "Bash", cwd: root, tool_input: { command } });
        expect((await hookRun(root, bash("echo x > Makefile"))).stdout).not.toBe("");
        expect((await hookRun(root, bash("echo x > new.cfg"))).stdout).toBe("");
    });
    it("matches ** across directories and treats a missing list as no extras", () => {
        expect(globToRegExp("docs/**/*.lock").test("docs/a/b/x.lock")).toBe(true);
        expect(globToRegExp("docs/**/*.lock").test("docs/x.lock")).toBe(true);
        expect(globToRegExp("*.lock").test("a/b/x.lock")).toBe(true);
        expect(globToRegExp("*.lock").test("x.lockx")).toBe(false);
        expect(projectExtras("/nonexistent/root")).toEqual([]);
    });
});
