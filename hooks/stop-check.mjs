#!/usr/bin/env node
/**
 * Stop hook: run eslint and the type check on the JS/TS files edited during the response, before the session may finish.
 *
 * edited-files.mjs records the paths; this hook reads and deletes them, keeps the checkable ones and runs the
 * project's own tools from node_modules/.bin (never npx).  There is no format step. Exit 2 blocks the stop and hands the report to the model.
 * When the hook already blocked once this turn (`stop_hook_active`), it does not block again: it adds a one-line
 * context note and lets the turn end, so a failure it cannot fix does not loop.
 */
import { spawnSync } from "node:child_process";
import { existsSync, readdirSync, readFileSync, rmSync, writeFileSync } from "node:fs";
import path from "node:path";
import process from "node:process";
import { accumulatorPath, releasedPath } from "./edited-files.mjs";
import { isMain, projectDir, readHookInput, relativeToProject } from "./lib.mjs";

/**
 * The whole run must end inside the hook's 120 s budget in settings, with margin for Node start-up and the report.
 * `CLAUDE_HOOK_STOP_BUDGET_MS` overrides it so a test can exhaust the budget without waiting.
 */
const BUDGET_MS =
  Number(process.env.CLAUDE_HOOK_STOP_BUDGET_MS) > 0
    ? Number(process.env.CLAUDE_HOOK_STOP_BUDGET_MS)
    : 100_000;
const TOOL_TIMEOUT_MS = 50_000;
const MAX_LINES = 40;
// Any JS or TS source file; generated and vendored folders are not checked.
const SOURCE = /\.(js|jsx|mjs|cjs|ts|tsx|mts|cts)$/;
const SKIPPED = /(^|\/)(node_modules|\.git|dist|build|\.next|coverage)\//;
const TS = /\.(ts|tsx|mts|cts)$/;

/**
 * Which edited files are worth checking: unique, inside the project, a JS/TS source outside node_modules, .git, dist, build, .next and coverage, and still on disk.
 * @param {string[]} files absolute paths
 * @param {string} root
 * @returns {string[]} absolute paths
 */
export function select(files, root) {
  const out = new Set();
  for (const file of files) {
    const rel = relativeToProject(file, root);
    if (rel && SOURCE.test(rel) && !SKIPPED.test(rel) && existsSync(file)) out.add(file);
  }
  return [...out];
}

/**
 * Whether the project has an eslint configuration (flat or legacy), so eslint is not run into a "no config" error.
 * @param {string} root
 */
function hasEslintConfig(root) {
  try {
    return readdirSync(root).some((n) => /^(eslint\.config\..+|\.eslintrc(\..*)?)$/.test(n));
  } catch {
    return false;
  }
}

/**
 * The tools to run for these files: eslint on the files when the project has an eslint binary and configuration, and,
 * when one file is TypeScript and the root has a tsconfig.json and a tsc binary, `tsc --noEmit` (preceded by
 * `next typegen` when the project has Next: its route types are generated and often git-ignored, so tsc alone fails on
 * a fresh checkout). tsc takes no file list (a list would ignore tsconfig.json), so it checks the whole project, which
 * also catches an edit that breaks a file it was not run on. A tool that does not apply is left out, silently.
 * @param {string[]} files
 * @param {string} root
 * @returns {{ name: string, bin: string, args: string[] }[]}
 */
export function plan(files, root) {
  if (files.length === 0) return [];
  const bin = (name) => existsSync(path.join(root, "node_modules", ".bin", name));
  const tools = [];
  if (bin("eslint") && hasEslintConfig(root))
    tools.push({ name: "eslint", bin: "eslint", args: [...files] });
  if (files.some((f) => TS.test(f)) && existsSync(path.join(root, "tsconfig.json")) && bin("tsc")) {
    if (bin("next")) tools.push({ name: "next typegen", bin: "next", args: ["typegen"] });
    tools.push({ name: "tsc", bin: "tsc", args: ["--noEmit"] });
  }
  return tools;
}

/**
 * How long the next tool may run: its own ceiling, or what is left of the budget, whichever is smaller.
 * @param {number} elapsedMs
 */
export function toolTimeout(elapsedMs) {
  return Math.max(0, Math.min(TOOL_TIMEOUT_MS, BUDGET_MS - elapsedMs));
}

/**
 * @param {string} root
 * @param {{ name: string, bin: string, args: string[] }} tool
 * @param {number} timeoutMs
 * @returns {{ output: string, timedOut: boolean } | null} the failure, or null when the tool passed or is not installed
 */
function execute(root, tool, timeoutMs) {
  const bin = path.join(root, "node_modules", ".bin", tool.bin);
  if (!existsSync(bin)) return null;
  if (timeoutMs <= 0)
    return { output: `${tool.name} was not run: the stop-check budget is spent`, timedOut: true };
  const res = spawnSync(bin, tool.args, { cwd: root, encoding: "utf8", timeout: timeoutMs });
  const timedOut = res.error?.code === "ETIMEDOUT" || res.signal === "SIGTERM";
  if (res.error) return { output: `${tool.name} did not finish: ${res.error.message}`, timedOut };
  if (res.status === 0) return null;
  return {
    output:
      `${res.stdout ?? ""}${res.stderr ?? ""}`.trim() || `${tool.name} exited with ${res.status}`,
    timedOut,
  };
}

/** @param {string} text */
const head = (text) => text.split("\n").slice(0, MAX_LINES).join("\n");

/** @param {Record<string, any>} input the hook event */
export function main(input) {
  if (typeof input.session_id !== "string" || !input.session_id) return 0;
  const root = projectDir(input);
  const acc = accumulatorPath(input.session_id);
  const released = releasedPath(input.session_id);
  // A failure was already let go and nothing has been edited since: a later reply is not blocked over it.
  if (existsSync(released)) return 0;
  let raw;
  try {
    raw = readFileSync(acc, "utf8");
  } catch {
    return 0;
  }
  const files = select(
    raw.split("\n").filter((l) => l.trim()),
    root,
  );
  const failures = [];
  const started = Date.now();
  for (const tool of plan(files, root)) {
    const failure = execute(root, tool, toolTimeout(Date.now() - started));
    if (failure !== null) failures.push({ name: tool.name, output: failure.output });
  }
  // The list is consumed only once every tool passed. After a failure or a timeout it stays, so the next stop checks
  // the same files again instead of forgetting them: a response that ends without editing the failing file is blocked
  // once more, and Claude Code killing a slow hook loses nothing either.
  if (failures.length === 0) {
    rmSync(acc, { force: true });
    rmSync(released, { force: true });
    return 0;
  }
  // Already blocked once this response: let it end. The note goes to the user as a systemMessage, because a Stop
  // hook's additionalContext continues the conversation, the loop this branch exists to end. The list stays and the
  // released marker holds the checks back until the next edit.
  if (input.stop_hook_active === true) {
    const lines = failures.map((f) => f.name).join(", ");
    writeFileSync(released, "");
    process.stdout.write(`${JSON.stringify({ systemMessage: `stop-check still failing: ${lines}` })}\n`);
    return 0;
  }
  const report = failures.map((f) => `## ${f.name}\n${head(f.output)}`).join("\n\n");
  process.stderr.write(
    `${report}\n\nFix these before finishing; the stop-check hook runs eslint and the type check on the files edited this turn.\n`,
  );
  return 2;
}

if (isMain(import.meta.url)) {
  try {
    process.exitCode = main(readHookInput());
  } catch {
    process.exitCode = 0;
  }
}
