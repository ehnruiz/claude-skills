import { chmodSync, existsSync, mkdirSync, mkdtempSync, readFileSync, realpathSync, writeFileSync } from "node:fs";
import { tmpdir } from "node:os";
import path from "node:path";
import { randomUUID } from "node:crypto";
import { describe, expect, it } from "./compat.mjs";
import { runHook } from "./hookRun.mjs";
// Each hook run starts Node, and the stop-check ones start the project's tools. The tools here are small stand-ins in
// node_modules/.bin, so the suite needs no eslint or TypeScript install.
const HOOKS = path.resolve(import.meta.dirname, "../../hooks");
const editedHook = path.join(HOOKS, "edited-files.mjs");
const stopHook = path.join(HOOKS, "stop-check.mjs");
const hookRun = (hook, root, event) => runHook(hook, event, { root });

const FAKE_ESLINT = `#!/usr/bin/env node
const fs = require("node:fs");
let bad = false;
for (const file of process.argv.slice(2)) {
  if (/\\bvar\\b/.test(fs.readFileSync(file, "utf8"))) {
    console.log(file + "\\n  1:1  error  Unexpected var, use let or const  no-var");
    bad = true;
  }
}
process.exit(bad ? 1 : 0);
`;
// Fails on a file with a type error, and on one that uses `LayoutProps` unless `next typegen` ran first.
const FAKE_TSC = `#!/usr/bin/env node
const fs = require("node:fs");
const path = require("node:path");
let bad = false;
const walk = (dir) => {
  for (const e of fs.readdirSync(dir, { withFileTypes: true })) {
    if (e.name === "node_modules") continue;
    const p = path.join(dir, e.name);
    if (e.isDirectory()) walk(p);
    else if (/\\.tsx?$/.test(e.name)) {
      const text = fs.readFileSync(p, "utf8");
      const rel = path.relative(process.cwd(), p);
      if (text.includes('number = "x"')) {
        console.log(rel + "(1,14): error TS2322: Type 'string' is not assignable to type 'number'.");
        bad = true;
      }
      if (text.includes("LayoutProps") && !fs.existsSync(".typegen-ran")) {
        console.log(rel + "(1,1): error TS2304: Cannot find name 'LayoutProps'.");
        bad = true;
      }
    }
  }
};
walk(process.cwd());
process.exit(bad ? 1 : 0);
`;
const FAKE_NEXT = `#!/usr/bin/env node
require("node:fs").writeFileSync(".typegen-ran", process.argv.slice(2).join(" "));
`;

/**
 * A throwaway project with a tsconfig.json, an eslint config and stand-in eslint, tsc and next binaries. `opts` leaves
 * a piece out to test the skip rules.
 */
function project(files = {}, opts = {}) {
    const { eslint = true, eslintConfig = true, tsc = true, tsconfig = true, next = true } = opts;
    // The real path: tools resolve their base path through macOS's /var -> /private/var link.
    const root = realpathSync(mkdtempSync(path.join(tmpdir(), "claude-stop-")));
    mkdirSync(path.join(root, "node_modules", ".bin"), { recursive: true });
    const bins = { eslint: [eslint, FAKE_ESLINT], tsc: [tsc, FAKE_TSC], next: [next, FAKE_NEXT] };
    for (const [name, [wanted, body]] of Object.entries(bins)) {
        if (!wanted) continue;
        const bin = path.join(root, "node_modules", ".bin", name);
        writeFileSync(bin, body);
        chmodSync(bin, 0o755);
    }
    if (tsconfig) writeFileSync(path.join(root, "tsconfig.json"), "{}\n");
    if (eslintConfig) writeFileSync(path.join(root, "eslint.config.mjs"), "export default [];\n");
    for (const [rel, body] of Object.entries(files)) {
        mkdirSync(path.dirname(path.join(root, rel)), { recursive: true });
        writeFileSync(path.join(root, rel), body);
    }
    return root;
}
const sessionId = () => `test-${randomUUID()}`;
const post = (root, sid, rel, tool = "Edit") => ({
    hook_event_name: "PostToolUse",
    session_id: sid,
    tool_name: tool,
    cwd: root,
    tool_input: { file_path: path.join(root, rel) },
});
const stop = (root, sid, active = false) => ({
    hook_event_name: "Stop",
    session_id: sid,
    cwd: root,
    stop_hook_active: active,
});
async function accumulatorFor(sid) {
    const mod = await import(editedHook);
    return mod.accumulatorPath(sid);
}
describe("edited-files hook", () => {
    it("records an edited path once, however often it is edited", async () => {
        const root = project({ "lib/a.ts": "export const a = 1;\n" });
        const sid = sessionId();
        await hookRun(editedHook, root, post(root, sid, "lib/a.ts"));
        const second = await hookRun(editedHook, root, post(root, sid, "lib/a.ts", "Write"));
        expect(second).toMatchObject({ code: 0, stdout: "", stderr: "" });
        const lines = readFileSync(await accumulatorFor(sid), "utf8")
            .split("\n")
            .filter(Boolean);
        expect(lines.map((l) => l.trim())).toContain(path.join(root, "lib/a.ts"));
        expect(new Set(lines).size).toBe(1);
    });
    it("does nothing without a session id", async () => {
        const root = project();
        const event = { ...post(root, "x", "lib/a.ts"), session_id: undefined };
        expect(await hookRun(editedHook, root, event)).toMatchObject({
            code: 0,
            stdout: "",
            stderr: "",
        });
    });
});
describe("stop-check select and plan", () => {
    it("keeps JS and TS files that exist inside the project, and ignores everything else", async () => {
        const files = {
            "lib/a.ts": "export const a = 1;\n",
            "src/c.tsx": "export const c = 1;\n",
            "index.js": "export const i = 1;\n",
            "src/b.jsx": "export const b = 1;\n",
            "scripts/run.mjs": "export const r = 1;\n",
            "scripts/run.cjs": "module.exports = 1;\n",
            "src/m.mts": "export const m = 1;\n",
            "src/k.cts": "export const k = 1;\n",
            ".claude/hooks/h.mjs": "export const h = 1;\n",
            "scripts/nested/deep.mjs": "export const d = 1;\n",
            "README.md": "# hi\n",
            "lib/data.json": "{}\n",
            "lib/styles.css": "a {}\n",
            "node_modules/pkg/index.js": "export const n = 1;\n",
            "packages/x/node_modules/pkg/index.ts": "export const n = 1;\n",
            ".git/hooks/pre-commit.js": "export const g = 1;\n",
            "dist/out.js": "export const o = 1;\n",
            "build/out.js": "export const o = 1;\n",
            ".next/types/routes.ts": "export const t = 1;\n",
            "coverage/lcov.js": "export const c = 1;\n",
        };
        const root = project(files);
        const { select } = await import(stopHook);
        const abs = (r) => path.join(root, r);
        const kept = [
            "lib/a.ts",
            "src/c.tsx",
            "index.js",
            "src/b.jsx",
            "scripts/run.mjs",
            "scripts/run.cjs",
            "src/m.mts",
            "src/k.cts",
            ".claude/hooks/h.mjs",
            "scripts/nested/deep.mjs",
        ];
        const input = [...Object.keys(files), "lib/missing.ts", "lib/a.ts"].map(abs);
        input.push(path.join(tmpdir(), "outside.ts"));
        expect(select(input, root).sort()).toEqual(kept.map(abs).sort());
    });
    it("runs eslint, next typegen and tsc for TypeScript in a Next project, and only eslint for a JavaScript file", async () => {
        const { plan } = await import(stopHook);
        const root = project();
        const names = (files) => plan(files.map((f) => path.join(root, f)), root).map((t) => t.name);
        expect(names(["lib/a.ts"])).toEqual(["eslint", "next typegen", "tsc"]);
        expect(names(["components/c.tsx"])).toEqual(["eslint", "next typegen", "tsc"]);
        expect(names(["scripts/h.mjs"])).toEqual(["eslint"]);
        expect(names(["lib/a.js", "lib/b.cjs"])).toEqual(["eslint"]);
        expect(names([])).toEqual([]);
    });
    it("skips next typegen when the project has no next binary", async () => {
        const { plan } = await import(stopHook);
        const root = project({}, { next: false });
        expect(plan([path.join(root, "lib/a.ts")], root).map((t) => t.name)).toEqual(["eslint", "tsc"]);
    });
    it("skips eslint without a binary or without an eslint configuration, and says nothing", async () => {
        const { plan } = await import(stopHook);
        const noBin = project({}, { eslint: false });
        expect(plan([path.join(noBin, "lib/a.ts")], noBin).map((t) => t.name)).toEqual(["next typegen", "tsc"]);
        const noConfig = project({}, { eslintConfig: false });
        expect(plan([path.join(noConfig, "lib/a.ts")], noConfig).map((t) => t.name)).toEqual(["next typegen", "tsc"]);
        const legacy = project({ ".eslintrc.json": "{}\n" }, { eslintConfig: false });
        expect(plan([path.join(legacy, "lib/a.ts")], legacy).map((t) => t.name)).toContain("eslint");
    });
    it("skips the type check without a tsconfig.json at the root, without a tsc binary, or with no TypeScript file", async () => {
        const { plan } = await import(stopHook);
        const noConfig = project({}, { tsconfig: false });
        expect(plan([path.join(noConfig, "lib/a.ts")], noConfig).map((t) => t.name)).toEqual(["eslint"]);
        const noTsc = project({}, { tsc: false });
        expect(plan([path.join(noTsc, "lib/a.ts")], noTsc).map((t) => t.name)).toEqual(["eslint"]);
        const js = project();
        expect(plan([path.join(js, "lib/a.js")], js).map((t) => t.name)).toEqual(["eslint"]);
    });
    it("runs no formatter", async () => {
        const { plan } = await import(stopHook);
        const root = project();
        expect(plan([path.join(root, "lib/a.ts")], root).map((t) => t.bin)).not.toContain("prettier");
    });
    it("type-checks the whole project, not the edited files alone, so tsconfig.json still applies", async () => {
        const { plan } = await import(stopHook);
        const root = project();
        const tsc = plan([path.join(root, "lib/a.ts")], root).find((t) => t.name === "tsc");
        expect(tsc.args).toEqual(["--noEmit"]);
    });
});
describe("stop-check hook", () => {
    it("is silent when nothing was edited", async () => {
        const root = project();
        expect(await hookRun(stopHook, root, stop(root, sessionId()))).toMatchObject({
            code: 0,
            stdout: "",
            stderr: "",
        });
    });
    it("exits 0 silently on malformed stdin", async () => {
        const root = project();
        expect(await hookRun(stopHook, root, "{not json")).toMatchObject({
            code: 0,
            stdout: "",
            stderr: "",
        });
        expect(await hookRun(editedHook, root, "")).toMatchObject({ code: 0, stdout: "", stderr: "" });
    });
    it("passes a clean file silently and clears the accumulator (the layout needs the typegen types)", async () => {
        const root = project({
            "lib/a.ts": "export const a = 1;\n",
            "app/layout.tsx": 'export default function Layout({ children }: LayoutProps<"/">) {\n  return children;\n}\n',
        });
        const sid = sessionId();
        await hookRun(editedHook, root, post(root, sid, "lib/a.ts"));
        const result = await hookRun(stopHook, root, stop(root, sid));
        expect(result).toMatchObject({ code: 0, stdout: "", stderr: "" });
        expect(existsSync(await accumulatorFor(sid))).toBe(false);
    });
    it("does not check a file under node_modules, dist or other skipped folders, or a non-code file, even a broken one", async () => {
        const root = project({ "dist/bad.js": "var x = 1;\n", "node_modules/p/bad.js": "var y = 1;\n", "docs/notes.md": "# hi\n" });
        const sid = sessionId();
        await hookRun(editedHook, root, post(root, sid, "dist/bad.js"));
        await hookRun(editedHook, root, post(root, sid, "node_modules/p/bad.js"));
        await hookRun(editedHook, root, post(root, sid, "docs/notes.md"));
        expect(await hookRun(stopHook, root, stop(root, sid))).toMatchObject({
            code: 0,
            stdout: "",
            stderr: "",
        });
    });
    it("blocks on an eslint failure alone, without a tsc report", async () => {
        const root = project({ "lib/old.ts": "var x = 1;\nexport { x };\n" });
        const sid = sessionId();
        await hookRun(editedHook, root, post(root, sid, "lib/old.ts"));
        const blocked = await hookRun(stopHook, root, stop(root, sid));
        expect(blocked.code).toBe(2);
        expect(blocked.stderr).toContain("## eslint");
        expect(blocked.stderr).toContain("lib/old.ts");
        expect(blocked.stderr).not.toContain("## tsc");
        expect(blocked.stderr).toContain("Fix these before finishing");
    });
    it("blocks on a type error alone, without an eslint report, then lets the response end with a note to the user", async () => {
        const root = project({ "lib/bad.ts": 'export const n: number = "x";\n' });
        const sid = sessionId();
        await hookRun(editedHook, root, post(root, sid, "lib/bad.ts"));
        const blocked = await hookRun(stopHook, root, stop(root, sid));
        expect(blocked.code).toBe(2);
        expect(blocked.stderr).toContain("## tsc");
        expect(blocked.stderr).toContain("lib/bad.ts");
        expect(blocked.stderr).toMatch(/TS2322/);
        expect(blocked.stderr).not.toContain("## eslint");
        await hookRun(editedHook, root, post(root, sid, "lib/bad.ts"));
        const again = await hookRun(stopHook, root, stop(root, sid, true));
        expect(again.code).toBe(0);
        // A systemMessage is shown to the user; additionalContext would continue the conversation, which is the loop the
        // second stop exists to end.
        const output = JSON.parse(again.stdout);
        expect(output.hookSpecificOutput).toBeUndefined();
        expect(output.systemMessage).toContain("stop-check still failing");
        expect(output.systemMessage).toMatch(/tsc/);
    });
});
describe("stop-check in a project without the tools", () => {
    it("is silent when no tool applies: no eslint config and no tsconfig, even for a broken file", async () => {
        const root = project({ "lib/bad.ts": "var x = 1;\nexport { x };\n" }, { eslintConfig: false, tsconfig: false });
        const sid = sessionId();
        await hookRun(editedHook, root, post(root, sid, "lib/bad.ts"));
        expect(await hookRun(stopHook, root, stop(root, sid))).toMatchObject({ code: 0, stdout: "", stderr: "" });
    });
    it("checks a plain JavaScript project with eslint alone", async () => {
        const root = project({ "src/old.js": "var x = 1;\nmodule.exports = x;\n" }, { tsc: false, next: false, tsconfig: false });
        const sid = sessionId();
        await hookRun(editedHook, root, post(root, sid, "src/old.js"));
        const blocked = await hookRun(stopHook, root, stop(root, sid));
        expect(blocked.code).toBe(2);
        expect(blocked.stderr).toContain("## eslint");
        expect(blocked.stderr).not.toContain("## tsc");
    });
});
describe("stop-check time budget", () => {
    it("gives each tool its own ceiling while the budget lasts, then only what is left, then nothing", async () => {
        const { toolTimeout } = await import(stopHook);
        expect(toolTimeout(0)).toBe(50_000);
        expect(toolTimeout(40_000)).toBe(50_000);
        expect(toolTimeout(70_000)).toBe(30_000);
        expect(toolTimeout(100_000)).toBe(0);
        expect(toolTimeout(130_000)).toBe(0);
    });
});
describe("stop-check after a timeout", () => {
    it("keeps the edited-file list for the next stop and still blocks once", async () => {
        const root = project({ "lib/a.ts": "export const a = 1;\n" });
        const sid = sessionId();
        await hookRun(editedHook, root, post(root, sid, "lib/a.ts"));
        const spent = await runHook(stopHook, stop(root, sid), {
            root,
            env: { CLAUDE_HOOK_STOP_BUDGET_MS: "1" },
        });
        expect(spent.code).toBe(2);
        expect(spent.stderr).toContain("budget is spent");
        expect(existsSync(await accumulatorFor(sid))).toBe(true);
        const retry = await hookRun(stopHook, root, stop(root, sid));
        expect(retry).toMatchObject({ code: 0, stdout: "", stderr: "" });
        expect(existsSync(await accumulatorFor(sid))).toBe(false);
    });
});
describe("stop-check after a failure", () => {
    it("keeps the list so a response that did not fix the file is blocked again, and clears it once green", async () => {
        const root = project({ "lib/bad.ts": "var x = 1;\nexport { x };\n" });
        const sid = sessionId();
        await hookRun(editedHook, root, post(root, sid, "lib/bad.ts"));
        const first = await hookRun(stopHook, root, stop(root, sid));
        expect(first.code).toBe(2);
        expect(existsSync(await accumulatorFor(sid))).toBe(true);
        const second = await hookRun(stopHook, root, stop(root, sid, true));
        expect(second.code).toBe(0);
        expect(existsSync(await accumulatorFor(sid))).toBe(true);
        writeFileSync(path.join(root, "lib/bad.ts"), "export const x = 1;\n");
        await hookRun(editedHook, root, post(root, sid, "lib/bad.ts"));
        const third = await hookRun(stopHook, root, stop(root, sid));
        expect(third).toMatchObject({ code: 0, stdout: "", stderr: "" });
        expect(existsSync(await accumulatorFor(sid))).toBe(false);
    });
});
describe("stop-check once a failure has been let go", () => {
    it("stays silent on later responses that edit nothing, and checks again after the next edit", async () => {
        const root = project({ "lib/bad.ts": "var x = 1;\nexport { x };\n" });
        const sid = sessionId();
        await hookRun(editedHook, root, post(root, sid, "lib/bad.ts"));
        expect((await hookRun(stopHook, root, stop(root, sid))).code).toBe(2);
        expect((await hookRun(stopHook, root, stop(root, sid, true))).code).toBe(0);
        // A later question, no edit: nothing runs, nothing blocks, and the list is kept for the next edit.
        const question = await hookRun(stopHook, root, stop(root, sid));
        expect(question).toMatchObject({ code: 0, stdout: "", stderr: "" });
        expect(existsSync(await accumulatorFor(sid))).toBe(true);
        // The next edit re-arms the check, with the earlier file still on the list.
        writeFileSync(path.join(root, "lib/other.ts"), "export const y = 1;\n");
        await hookRun(editedHook, root, post(root, sid, "lib/other.ts"));
        const rearmed = await hookRun(stopHook, root, stop(root, sid));
        expect(rearmed.code).toBe(2);
        expect(rearmed.stderr).toContain("lib/bad.ts");
    });
});
