#!/usr/bin/env node
/**
 * PreToolUse hook on Edit, Write, MultiEdit, NotebookEdit and Bash: block edits to the files that decide what "green" means.
 *
 * An agent that cannot make lint, format, typecheck or CI pass has two ways out: fix the code, or loosen the check.
 * The second is quicker and wrong. This hook removes the second way: an existing configuration file in the list below is edited
 * by a person, not by a session. Creating one that does not exist yet is allowed, so a new tool can still be set up.
 * The names match at any depth (a worktree has its own), except under node_modules, vendor, .git, .next and
 * tests/fixtures. A project adds names in `.claude/protected-files`, one glob per line. A Bash command is judged by its write targets: `sed -i`, `perl -i`, a redirection, `tee`, the first
 * argument of `mv` and the last argument of `cp` and `mv`.
 *
 * Opt-in per project; see hooks/README.md.
 */
import { existsSync, readFileSync } from "node:fs";
import path from "node:path";
import { deny, isMain, projectDir, readHookInput, relativeToProject, targetPaths } from "./lib.mjs";
import { moveSources, sedInPlaceArguments, segmentsWithCwd, writeTargets } from "./shell.mjs";

/** Names as regular expressions over forward-slash paths, at any depth: a worktree or a package has its own. */
export const PROTECTED = [
  /(^|\/)eslint\.config\.(js|mjs|cjs|ts|mts|cts)$/,
  /(^|\/)\.eslintrc(\..*)?$/,
  /(^|\/)tsconfig(\..*)?\.json$/,
  /(^|\/)\.prettierrc(\..*)?$/,
  /(^|\/)prettier\.config\.(js|mjs|cjs|ts|mts|cts)$/,
  /(^|\/)biome\.jsonc?$/,
  /(^|\/)vitest\.config\.(js|mjs|cjs|ts|mts|cts)$/,
  /(^|\/)jest\.config\.(js|mjs|cjs|ts|mts|cts|json)$/,
  /(^|\/)playwright\.config\.(js|mjs|cjs|ts|mts|cts)$/,
  /(^|\/)\.github\/workflows\/[^/]+\.ya?ml$/,
  /(^|\/)\.github\/dependabot\.ya?ml$/,
  // The guards decide what "green" means too: a session that could edit them could switch a check off.
  /(^|\/)\.claude\/hooks\/[^/]+\.mjs$/,
];

/**
 * Directories whose configuration files are somebody else's, generated or test data: never protected. `.git` is
 * repository internals, `.next` is a build output that holds its own tsconfig.json.
 */
const EXCLUDED = /(^|\/)(node_modules|vendor|\.git|\.next)\/|(^|\/)tests\/fixtures\//;

/**
 * A glob from `.claude/protected-files` as a regular expression over forward-slash paths, matched at any depth like
 * the built-in names: `*` is any run of characters except `/`, `**` any run including `/`, `?` one character.
 * @param {string} glob
 */
export function globToRegExp(glob) {
  let out = "";
  const g = glob.replace(/^\.?\//, "");
  for (let i = 0; i < g.length; i++) {
    const c = g[i];
    if (c === "*") {
      if (g[i + 1] === "*") {
        i++;
        if (g[i + 1] === "/") i++;
        out += "(?:.*/)?";
      } else out += "[^/]*";
    } else if (c === "?") out += "[^/]";
    else out += c.replace(/[.+^${}()|[\]\\]/g, "\\$&");
  }
  return new RegExp(`(^|/)${out}$`);
}

/**
 * The extra globs a project lists in `.claude/protected-files`: one per line, blank lines and `#` lines ignored.
 * A missing or unreadable file means no extras.
 * @param {string} root
 * @returns {RegExp[]}
 */
export function projectExtras(root) {
  let raw;
  try {
    raw = readFileSync(path.join(root, ".claude", "protected-files"), "utf8");
  } catch {
    return [];
  }
  return raw
    .split(/\r?\n/)
    .map((l) => l.trim())
    .filter((l) => l && !l.startsWith("#"))
    .map(globToRegExp);
}

/**
 * @param {string} rel project-relative path with forward slashes
 * @param {RegExp[]} extras
 */
export function isProtected(rel, extras = []) {
  return !EXCLUDED.test(rel) && [...PROTECTED, ...extras].some((re) => re.test(rel));
}

/** @param {string} rel */
function message(rel) {
  if (/(^|\/)\.claude\/hooks\/[^/]+\.mjs$/.test(rel)) {
    return (
      `Blocked by the config-protection hook: ${rel} is a session hook, which decides what passing ` +
      "means. Say what should change and let the user edit it by hand."
    );
  }
  return (
    `Blocked by the config-protection hook: ${rel} is a lint, format, type or CI configuration file. ` +
    "Fix the code it checks instead. If the configuration itself is wrong, say so and let the user change it by hand."
  );
}

/**
 * The first existing protected file a Bash command writes: the target of `sed -i` or `perl -i`, a redirection,
 * `tee`, the first argument of `mv` (moved away) or the last argument of `cp` and `mv`, relative to the directory the
 * event's cwd and the `cd`, `pushd` and `popd` before the command leave; after one the hook cannot follow, a target is judged
 * from the last known directory and from the root, and denied when either is an existing protected file.
 * @param {string} command
 * @param {string} cwd absolute
 * @param {string} root absolute
 * @param {(file: string) => boolean} exists
 * @param {RegExp[]} extras
 * @returns {string | null} the project-relative path
 */
function bashTarget(command, cwd, root, exists, extras) {
  for (const part of segmentsWithCwd(command, cwd)) {
    const candidates = [
      ...writeTargets(part.segment).map((t) => t.target),
      ...sedInPlaceArguments(part.segment),
      ...moveSources(part.segment),
    ];
    for (const candidate of candidates) {
      // An unknown directory fails closed: judge the target from the last known directory and from the root.
      for (const base of part.cwd === null ? [part.lastKnown, root] : [part.cwd]) {
        const file = path.resolve(base, candidate);
        const rel = relativeToProject(file, root);
        if (rel && isProtected(rel, extras) && exists(file)) return rel;
      }
    }
  }
  return null;
}

/**
 * @param {Record<string, any>} input the hook event
 * @param {(file: string) => boolean} exists
 * @returns {string | null} the reason to deny, or null to allow
 */
export function decide(input, exists = existsSync) {
  const root = projectDir(input);
  const extras = projectExtras(root);
  if (input.tool_name === "Bash") {
    const command = input.tool_input?.command;
    if (typeof command !== "string" || !command.trim()) return null;
    const cwd = typeof input.cwd === "string" && input.cwd ? path.resolve(input.cwd) : root;
    const rel = bashTarget(command, cwd, root, exists, extras);
    return rel ? message(rel) : null;
  }
  for (const file of targetPaths(input, root)) {
    const rel = relativeToProject(file, root);
    if (rel && isProtected(rel, extras) && exists(file)) return message(rel);
  }
  return null;
}

if (isMain(import.meta.url)) {
  const reason = decide(readHookInput());
  if (reason) deny(reason);
}
