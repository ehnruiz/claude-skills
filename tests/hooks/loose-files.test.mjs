import { mkdirSync, mkdtempSync, writeFileSync } from "node:fs";
import { tmpdir } from "node:os";
import path from "node:path";
import { describe, expect, it } from "./compat.mjs";
import { runHook } from "./hookRun.mjs";
import { bashVerdict, decide, rootAllowlist, writeVerdict } from "../../hooks/loose-files.mjs";

const HOOKS = path.resolve(import.meta.dirname, "../../hooks");
const hook = path.resolve(HOOKS, "loose-files.mjs");
const root = "/work/project";

const hookRun = (dir, event) => runHook(hook, event, { root: dir });
const tempProject = () => mkdtempSync(path.join(tmpdir(), "claude-hook-"));

const write = (dir, rel) => ({
  hook_event_name: "PreToolUse",
  tool_name: "Write",
  cwd: dir,
  tool_input: { file_path: path.join(dir, rel) },
});
const bash = (dir, command, cwd = dir) => ({
  hook_event_name: "PreToolUse",
  tool_name: "Bash",
  cwd,
  tool_input: { command },
});

/** Writes `.claude/root-files` in a project. */
function allowlist(dir, text) {
  mkdirSync(path.join(dir, ".claude"), { recursive: true });
  writeFileSync(path.join(dir, ".claude", "root-files"), text);
}

describe("loose-files hook, end to end", () => {
  it("denies a new document at the root and points to docs/ and the allowlist", async () => {
    const dir = tempProject();
    const { stdout, code } = await hookRun(dir, write(dir, "brief.md"));
    expect(code).toBe(0);
    const out = JSON.parse(stdout);
    expect(out.hookSpecificOutput.permissionDecision).toBe("deny");
    const reason = out.hookSpecificOutput.permissionDecisionReason;
    expect(reason).toContain("brief.md");
    expect(reason).toContain("docs/");
    expect(reason).toContain(".claude/root-files");
    expect(reason).not.toContain("notes/");
  });

  it("denies a heredoc written through the shell", async () => {
    const dir = tempProject();
    const { stdout } = await hookRun(dir, bash(dir, "cat > brief.md <<'EOF'\nhello\nEOF"));
    expect(JSON.parse(stdout).hookSpecificOutput.permissionDecision).toBe("deny");
  });

  it("allows an existing root file to be edited, with no output", async () => {
    const dir = tempProject();
    writeFileSync(path.join(dir, "brief.md"), "x");
    const { stdout, code } = await hookRun(dir, write(dir, "brief.md"));
    expect(code).toBe(0);
    expect(stdout).toBe("");
  });

  it("allows a name listed in .claude/root-files, with no output", async () => {
    const dir = tempProject();
    allowlist(dir, "brief.md\n");
    const { stdout, code } = await hookRun(dir, write(dir, "brief.md"));
    expect(code).toBe(0);
    expect(stdout).toBe("");
  });

  it("never fails the tool on empty or malformed input", async () => {
    const dir = tempProject();
    for (const bad of ["", "{not json", "[]"]) {
      const { stdout, code } = await hookRun(dir, bad);
      expect(code).toBe(0);
      expect(stdout).toBe("");
    }
  });
});

describe("writeVerdict", () => {
  it.each([
    "brief.md",
    "triage.HTML",
    "out.csv",
    "a.png",
    "b.zip",
    "c.xlsx",
    "d.PDF",
    "e.docx",
    "f.pptx",
    "g.svg",
    "h.txt",
    "i.tsv",
  ])("denies a new root %s", (name) => {
    expect(writeVerdict(path.join(root, name), root, false)).toContain(name);
  });

  it("gives the full reason text", () => {
    expect(writeVerdict(`${root}/brief.md`, root, false)).toBe(
      "New documents do not go in the repository root. Put brief.md in docs/ if it is project documentation, or outside the repository or in a git-ignored folder if it is a personal note or draft. If the name belongs at the root, list it in .claude/root-files.",
    );
  });

  it.each(["docs/x.md", "notes/x.md", "src/x.ts", ".github/x.md", "package.json", "eslint.config.js"])(
    "allows %s",
    (rel) => {
      expect(writeVerdict(path.join(root, rel), root, false)).toBeNull();
    },
  );

  it.each([
    "README.md",
    "CHANGELOG.md",
    "CLAUDE.md",
    "REVIEW.md",
    "CONTRIBUTING.md",
    "SECURITY.md",
    "LICENSE",
    "LICENSE.md",
    "CODE_OF_CONDUCT.md",
    "AGENTS.md",
  ])("allows the root name %s", (name) => {
    expect(writeVerdict(path.join(root, name), root, false)).toBeNull();
  });

  it.each([
    "CMakeLists.txt",
    "requirements.txt",
    "requirements-dev.txt",
    "requirements_test.txt",
    "constraints.txt",
    "index.html",
    "LICENSE.txt",
    "LICENSE.rst",
    "COPYING",
    "COPYING.txt",
    "COPYING.LESSER",
    "NOTICE",
    "NOTICE.txt",
    "AUTHORS",
    "AUTHORS.md",
    "CODEOWNERS",
    "robots.txt",
    "llms.txt",
    "llms-full.txt",
    "humans.txt",
    "favicon.svg",
    "favicon.png",
    "favicon.ico",
    "GEMINI.md",
    "CONVENTIONS.md",
    "ARCHITECTURE.md",
    "ROADMAP.md",
    "SUPPORT.md",
    "GOVERNANCE.md",
    "MAINTAINERS.md",
    "HISTORY.md",
    "CHANGES.md",
    "NEWS.md",
  ])("allows the conventional root file %s through a Write", (name) => {
    expect(writeVerdict(path.join(root, name), root, false)).toBeNull();
  });

  it.each(["brief.md", "index.htm.bak.md", "requirements-notes.md", "licensing-brief.md", "notes.txt"])(
    "still denies the look-alike %s",
    (name) => {
      expect(writeVerdict(path.join(root, name), root, false)).toContain(name);
    },
  );

  it("allows editing an existing root document", () => {
    expect(writeVerdict(`${root}/brief.md`, root, true)).toBeNull();
  });

  it("allows a document outside the project root", () => {
    expect(writeVerdict("/tmp/brief.md", root, false)).toBeNull();
  });

  it("resolves a relative path against the root", () => {
    expect(writeVerdict("brief.md", root, false)).toContain("brief.md");
  });
});

describe(".claude/root-files", () => {
  it("returns no names when the file is missing", () => {
    expect(rootAllowlist(tempProject())).toEqual([]);
  });

  it("allows a basename and a glob, and still denies other names", () => {
    const dir = tempProject();
    allowlist(dir, "ROADMAP.md\nrelease-*.md\nv?.txt\n");
    const extras = rootAllowlist(dir);
    expect(writeVerdict(`${root}/ROADMAP.md`, root, false, extras)).toBeNull();
    expect(writeVerdict(`${root}/release-1.7.md`, root, false, extras)).toBeNull();
    expect(writeVerdict(`${root}/v1.txt`, root, false, extras)).toBeNull();
    expect(writeVerdict(`${root}/v10.txt`, root, false, extras)).toContain("v10.txt");
    expect(writeVerdict(`${root}/brief.md`, root, false, extras)).toContain("brief.md");
    expect(writeVerdict(`${root}/ROADMAP.md.bak`, root, false, extras)).toBeNull();
    expect(writeVerdict(`${root}/xROADMAP.md`, root, false, extras)).toContain("xROADMAP.md");
  });

  it("ignores comment lines, blank lines and surrounding whitespace", () => {
    const dir = tempProject();
    allowlist(dir, "# brief.md\n\n   \n  plan.md  \r\n");
    const extras = rootAllowlist(dir);
    expect(writeVerdict(`${root}/brief.md`, root, false, extras)).toContain("brief.md");
    expect(writeVerdict(`${root}/plan.md`, root, false, extras)).toBeNull();
  });

  it("does not treat a dot or other regex characters in a name as wildcards", () => {
    const dir = tempProject();
    allowlist(dir, "a.md\nb+c.md\n");
    const extras = rootAllowlist(dir);
    expect(writeVerdict(`${root}/aXmd`, root, false, extras)).toBeNull();
    expect(writeVerdict(`${root}/a.md`, root, false, extras)).toBeNull();
    expect(writeVerdict(`${root}/bbc.md`, root, false, extras)).toContain("bbc.md");
    expect(writeVerdict(`${root}/b+c.md`, root, false, extras)).toBeNull();
  });

  it("applies to a shell write too", () => {
    const dir = tempProject();
    allowlist(dir, "*.csv\n");
    expect(bashVerdict("echo a > out.csv", dir, dir, () => false, rootAllowlist(dir))).toBeNull();
    expect(bashVerdict("echo a > out.md", dir, dir, () => false, rootAllowlist(dir))).toContain("out.md");
  });

  it("is read by decide from the project root", () => {
    const dir = tempProject();
    const event = write(dir, "PLAN.md");
    expect(decide(event)).toContain("PLAN.md");
    allowlist(dir, "PLAN.md\n");
    expect(decide(event)).toBeNull();
  });
});

describe("decide, a file that exists already", () => {
  it("allows a Write to an existing root document and denies a new one", () => {
    const dir = tempProject();
    writeFileSync(path.join(dir, "there.md"), "x");
    expect(decide(write(dir, "there.md"))).toBeNull();
    expect(decide(write(dir, "missing.md"))).toContain("missing.md");
  });

  it("allows appending to an existing root document from Bash", () => {
    const dir = tempProject();
    writeFileSync(path.join(dir, "there.md"), "x");
    expect(decide(bash(dir, "echo x >> there.md"))).toBeNull();
    expect(decide(bash(dir, "echo x >> missing.md"))).toContain("missing.md");
  });

  it("ignores tools other than Write and Bash", () => {
    const dir = tempProject();
    const event = { ...write(dir, "brief.md"), tool_name: "Edit" };
    expect(decide(event)).toBeNull();
  });
});

describe("bashVerdict", () => {
  it.each([
    "cat > brief.md <<'EOF'",
    "echo hi >> ./notes.txt",
    'echo hi > "quoted.md"',
    "echo hi > 'quoted.md'",
    "printf x | tee out.csv",
    "printf x | tee -a out.csv",
    "cp a.png ./b.png",
    "mv x.zip y.zip",
    "ls && mv x.zip y.zip",
    "echo hi 2> err.txt",
  ])("denies %s", (command) => {
    expect(bashVerdict(command, root, root)).toContain("docs/");
  });

  it("names the basename in the reason", () => {
    expect(bashVerdict("cp a.png ./b.png", root, root)).toContain("b.png");
  });

  it.each([
    "cat > notes/brief.md",
    "npm test > out.log",
    "echo hi > /tmp/x.md",
    "cd /tmp && cat > x.md",
    "git status",
    "cat > README.md",
    "cp a.md docs/b.md",
    "mv x.zip notes/",
    "cp a.png b",
    'echo "a -> b.md"',
    "node a.js 2>&1",
    "echo hi > /dev/null",
    "",
  ])("allows %s", (command) => {
    expect(bashVerdict(command, root, root)).toBeNull();
  });

  it.each([
    "pip freeze > requirements.txt",
    "pip freeze > requirements-dev.txt",
    "echo cmake_minimum_required > CMakeLists.txt",
    "echo '<html>' > index.html",
    "cat license-text > LICENSE.txt",
    "echo 'User-agent: *' > robots.txt",
    "curl -s https://example.com/llms.txt > llms.txt",
    "echo x > llms-full.txt",
    "cp logo.svg favicon.svg",
    "mv logo.svg favicon.svg",
    "echo x | tee GEMINI.md",
    "echo x > CONVENTIONS.md",
    "echo x >> ROADMAP.md",
    "cp -t docs brief.md",
    "cp --target-directory=docs brief.md",
    "cp --target-directory docs a.md b.md",
    "cp -tdocs brief.md",
    "mv -t docs brief.md",
    "mv --target-directory=docs a.md",
    "mv -t ./docs/ a.md b.md",
    "cp -rt docs a.md",
    "cp -t /tmp brief.md",
    "cp -t $DEST brief.md",
    "cp -t . docs/*.md",
    'cp -t . "$name.md"',
    "mv -t . build/$(date).html",
    "echo x > README.txt",
  ])("allows %s", (command) => {
    expect(bashVerdict(command, root, root)).toBeNull();
  });

  it.each([
    ["cp -t . brief.md", "brief.md"],
    ["cp --target-directory=. brief.md", "brief.md"],
    ["mv -t ./ docs/a.png", "a.png"],
    ["cp -t /work/project a.zip b.zip", "a.zip"],
    ["cp -t '.' brief.md", "brief.md"],
  ])("still denies %s, which writes into the root", (command, name) => {
    expect(bashVerdict(command, root, root)).toContain(name);
  });

  it("judges the -t directory against the shell's current directory", () => {
    expect(bashVerdict("cd docs && cp -t .. brief.md", root, root)).toContain("brief.md");
    expect(bashVerdict("cd docs && cp -t . brief.md", root, root)).toBeNull();
  });

  it("denies a root write after a comment with an apostrophe, which opens no quote", () => {
    expect(bashVerdict("# don't\necho hi > brief.md", root, root)).toContain("brief.md");
    expect(bashVerdict("  # it's fine\necho hi > brief.md", root, root)).toContain("brief.md");
  });

  it("keeps the spaces of a quoted target, and denies it at the root", () => {
    expect(bashVerdict('echo x > "my brief.md"', root, root)).toContain("my brief.md");
    expect(bashVerdict("echo x > 'my brief.md'", root, root)).toContain("my brief.md");
  });

  it("does not judge a target with a variable, a glob or a substitution", () => {
    expect(bashVerdict('echo x > "$dir/brief.md"', root, root)).toBeNull();
    expect(bashVerdict("echo x > $f.md", root, root)).toBeNull();
    expect(bashVerdict("echo x > *.md", root, root)).toBeNull();
    expect(bashVerdict("echo x > $(pwd).md", root, root)).toBeNull();
  });

  it("follows a cd into a subdirectory and out of it", () => {
    expect(bashVerdict("echo x > brief.md; cd docs", root, root)).toContain("brief.md");
    expect(bashVerdict("echo x > brief.md && pushd docs", root, root)).toContain("brief.md");
    expect(bashVerdict("cd docs && echo x > brief.md", root, root)).toBeNull();
    expect(bashVerdict("cd docs\necho x > brief.md", root, root)).toBeNull();
    expect(bashVerdict("echo a > one.md; cd docs; echo b > two.md", root, root)).toContain("one.md");
    expect(bashVerdict("cd docs && cd .. && echo x > brief.md", root, root)).toContain("brief.md");
    expect(bashVerdict("cd docs && echo x > ../brief.md", root, root)).toContain("brief.md");
    expect(bashVerdict("(cd docs && echo x > a.md); echo y > b.md", root, root)).toContain("b.md");
  });

  it("follows a cd into the root from another directory", () => {
    expect(bashVerdict("cd /work/project && echo x > brief.md", "/tmp", root)).toContain("brief.md");
    expect(bashVerdict("cd /work/project/docs && echo x > brief.md", "/tmp", root)).toBeNull();
  });

  it("does not judge what it cannot resolve after a cd to a variable", () => {
    expect(bashVerdict('cd "$dir" && echo x > brief.md', root, root)).toBeNull();
    expect(bashVerdict("cd $(mktemp -d) && echo x > brief.md", root, root)).toBeNull();
    expect(bashVerdict("cd - && echo x > brief.md", root, root)).toBeNull();
  });

  it("denies tee at the root even after 2>&1, so the case is deliberate", () => {
    expect(bashVerdict("npm test 2>&1 | tee test-output.txt", root, root)).toContain("test-output.txt");
  });

  it("allows the same redirection when the cwd is not the project root", () => {
    expect(bashVerdict("cat > brief.md", "/work/project/docs", root)).toBeNull();
    expect(bashVerdict("cat > brief.md", undefined, root)).toBeNull();
  });

  it("judges an absolute path to a file in the root", () => {
    expect(bashVerdict("cat > /work/project/brief.md", "/tmp", root)).toContain("brief.md");
  });
});

describe("heredoc bodies", () => {
  const heredoc = (target) =>
    `cat > ${target} <<'EOF'\nit('denies', () => expect(bashVerdict('cat > brief.md', root, root)).toBeTruthy());\nEOF`;

  it("does not judge a redirection quoted inside a heredoc body", () => {
    expect(bashVerdict(heredoc("tests/x.test.ts"), root, root)).toBeNull();
  });

  it("still judges the heredoc target itself", () => {
    expect(bashVerdict(heredoc("brief.md"), root, root)).toContain("brief.md");
  });

  it("resumes judging after the terminator, with <<- and double quotes too", () => {
    const command = 'cat <<-"END" > notes/ok.md\n\t> inside.md\n\tEND\ncp a.png b.png';
    expect(bashVerdict(command, root, root)).toContain("b.png");
  });
});

describe("quoted text is not a command", () => {
  const commit = "git commit -m \"$(cat <<'EOF'\nFix the thing\n\n2> errors.txt\nEOF\n)\"";
  const prBody = 'gh pr create --title t --body "First line\n> notes.md is ignored\nlast line"';
  const nested = 'echo "result: $(printf x > inner.md)"';

  it.each([
    ["a commit message with a heredoc", commit],
    ["a PR body with a markdown blockquote across lines", prBody],
    ["a double-quoted string with an arrow", 'echo "a > b.md"'],
    ["a single-quoted string", "echo 'x > y.md'"],
    ["a redirection inside $(...) inside double quotes", nested],
    ["pushd, which is judged like cd", "pushd /tmp && cat > x.md"],
  ])("allows %s", (_label, command) => {
    expect(bashVerdict(command, root, root)).toBeNull();
  });

  it("still judges a quoted string that is the target of a redirection, tee, cp or mv", () => {
    expect(bashVerdict('echo hi > "brief.md"', root, root)).toContain("brief.md");
    expect(bashVerdict("echo hi >> 'brief.md'", root, root)).toContain("brief.md");
    expect(bashVerdict('echo hi | tee "brief.md"', root, root)).toContain("brief.md");
    expect(bashVerdict('cp a.png "b.png"', root, root)).toContain("b.png");
    expect(bashVerdict('mv a.png "b.png"', root, root)).toContain("b.png");
  });

  it("keeps judging a real redirection that follows a quoted string", () => {
    expect(bashVerdict('echo "hello" > brief.md', root, root)).toContain("brief.md");
  });

  it("allows a target that exists at the root already, and denies one that does not", () => {
    expect(bashVerdict("echo x >> c110.md", root, root, () => true)).toBeNull();
    expect(bashVerdict("cp a.png b.png", root, root, () => true)).toBeNull();
    expect(bashVerdict("echo x >> c110.md", root, root, () => false)).toContain("c110.md");
  });

  it("checks existence against the root by default", () => {
    const dir = tempProject();
    writeFileSync(path.join(dir, "there.md"), "x");
    expect(bashVerdict("echo x >> there.md", dir, dir)).toBeNull();
    expect(bashVerdict("echo x >> missing.md", dir, dir)).toContain("missing.md");
  });
});

describe("comments and quoted sources", () => {
  it("ignores a trailing comment that mentions a redirection", () => {
    expect(bashVerdict("echo done # > brief.md", root, root)).toBeNull();
    expect(bashVerdict("npm test 2>&1 # logs go > out.txt later", root, root)).toBeNull();
  });

  it("does not read a quoted cp source as a redirection", () => {
    expect(bashVerdict('cp "a > brief.md" /tmp/result', root, root)).toBeNull();
    expect(bashVerdict("mv 'x > y.md' notes/kept.md", root, root)).toBeNull();
  });

  it("does not let an escaped quote before a comment keep the comment", () => {
    expect(bashVerdict('echo \\" done # > brief.md', root, root)).toBeNull();
    expect(bashVerdict("echo it\\'s fine # > brief.md", root, root)).toBeNull();
  });

  it("keeps a # inside a quoted target", () => {
    expect(bashVerdict('echo x > "my # brief.md"', root, root)).toContain("my # brief.md");
    expect(bashVerdict("echo x > 'a#b.md' # done", root, root)).toContain("a#b.md");
  });

  it("still judges a real write next to a comment or a quoted argument", () => {
    expect(bashVerdict('echo "#" > brief.md', root, root)).toContain("brief.md");
    expect(bashVerdict("echo done > brief.md # fine", root, root)).toContain("brief.md");
  });
});
