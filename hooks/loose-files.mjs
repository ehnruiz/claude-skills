#!/usr/bin/env node
/**
 * PreToolUse hook on Write and Bash: keep new documents out of the repository root.
 *
 * Sessions tend to leave briefs, exports and drafts in the root: a Markdown plan, an HTML page, a zip, a
 * spreadsheet. They are never reviewed, they clutter `git status`, and nobody remembers who wanted them. Project
 * documentation belongs in `docs/`; personal notes and drafts belong outside the repository or in a git-ignored
 * folder. The hook denies a Write of a new document at the root, and a shell command that creates one there by
 * redirection, `tee`, `cp` or `mv`. A document is a file with an extension in the list below; the conventional root
 * files (README.md, CHANGELOG.md, LICENSE, AGENTS.md, CLAUDE.md and the like) are always allowed. Editing a file that
 * exists already is allowed, from Write and from Bash.
 *
 * A project adds names in `.claude/root-files`, one basename or glob per line (`*` any run of characters, `?` one
 * character), `#` lines and blank lines ignored.
 *
 * A Bash command is judged in the directory the event's cwd and the `cd`, `pushd` and `popd` before the write leave
 * it. Deliberately not judged, because a false positive costs more than a miss: a write after a `cd` the hook cannot
 * follow (to a variable, a substitution or `-`), a target with a variable, a glob or a substitution, and quoted text
 * that is not itself a write target.
 *
 * Opt-in per project; see hooks/README.md.
 */
import { existsSync, readFileSync } from "node:fs";
import path from "node:path";
import { deny, isMain, projectDir, readHookInput } from "./lib.mjs";
import { segmentsWithCwd, wordsOf, writeTargets } from "./shell.mjs";

/** Extensions of the files that are documents or exports rather than code or configuration, lower case. */
const DOCUMENT_EXTENSIONS = new Set([
  "md",
  "markdown",
  "txt",
  "html",
  "htm",
  "svg",
  "png",
  "jpg",
  "jpeg",
  "gif",
  "webp",
  "pdf",
  "zip",
  "csv",
  "tsv",
  "doc",
  "docx",
  "xls",
  "xlsx",
  "ppt",
  "pptx",
]);

/**
 * A basename glob as a regular expression over the whole name: `*` is any run of characters, `?` one character,
 * anything else is literal.
 * @param {string} glob
 */
function globToRegExp(glob) {
  let out = "";
  for (const c of glob) {
    if (c === "*") out += ".*";
    else if (c === "?") out += ".";
    else out += c.replace(/[.+^${}()|[\]\\]/g, "\\$&");
  }
  return new RegExp(`^${out}$`);
}

/**
 * Conventional files that belong at the root of a repository, as basename globs (same matching as
 * `.claude/root-files`). Tools look for several of these only at the root: a build file, a dependency list, a Vite
 * entry page, a license, a crawler file, a favicon, an agent instruction file.
 */
const ALLOWED_ROOT_GLOBS = [
  "README.*",
  "CHANGELOG.md",
  "CLAUDE.md",
  "REVIEW.md",
  "CONTRIBUTING.md",
  "SECURITY.md",
  "CODE_OF_CONDUCT.md",
  "AGENTS.md",
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
  "LICENSE",
  "LICENSE.*",
  "COPYING*",
  "NOTICE*",
  "AUTHORS*",
  "CODEOWNERS",
  "CMakeLists.txt",
  "requirements*.txt",
  "constraints.txt",
  "index.html",
  "robots.txt",
  "llms.txt",
  "llms-full.txt",
  "humans.txt",
  "favicon.*",
].map(globToRegExp);

/**
 * The extra names a project lists in `.claude/root-files`: one basename or glob per line, blank lines and `#` lines
 * ignored. A missing or unreadable file means no extras.
 * @param {string} root absolute project root
 * @returns {RegExp[]}
 */
export function rootAllowlist(root) {
  let raw;
  try {
    raw = readFileSync(path.join(root, ".claude", "root-files"), "utf8");
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
 * @param {string} name a basename
 * @param {RegExp[]} extras
 */
function isLooseDocument(name, extras) {
  if (ALLOWED_ROOT_GLOBS.some((re) => re.test(name)) || extras.some((re) => re.test(name))) return false;
  const dot = name.lastIndexOf(".");
  return dot > 0 && DOCUMENT_EXTENSIONS.has(name.slice(dot + 1).toLowerCase());
}

/** @param {string} name */
function reason(name) {
  return (
    `New documents do not go in the repository root. Put ${name} in docs/ if it is project documentation, ` +
    "or outside the repository or in a git-ignored folder if it is a personal note or draft. " +
    "If the name belongs at the root, list it in .claude/root-files."
  );
}

/**
 * @param {string} filePath target of a Write, absolute or relative to the root
 * @param {string} root absolute project root
 * @param {boolean} exists whether the file exists already
 * @param {RegExp[]} [extras] names from `.claude/root-files`
 * @returns {string | null} the reason to deny, or null
 */
export function writeVerdict(filePath, root, exists, extras = []) {
  if (typeof filePath !== "string" || !filePath.trim()) return null;
  const file = path.resolve(root, filePath);
  if (path.dirname(file) !== path.resolve(root)) return null;
  const name = path.basename(file);
  return !exists && isLooseDocument(name, extras) ? reason(name) : null;
}

/**
 * The `-t` / `--target-directory` form of `cp` and `mv` (GNU): the destination is that directory and every other
 * non-option word is a source, so the files land in the directory under their own basenames.
 * @param {string} segment one simple command
 * @returns {{ directory: string | null, sources: string[] } | null} null when the command is not a `-t` cp or mv;
 *   a null directory when it has shell syntax the hook cannot resolve
 */
function targetDirectoryForm(segment) {
  const words = wordsOf(segment.replace(/^[\s({!]+/, ""));
  if (words[0] !== "cp" && words[0] !== "mv") return null;
  /** @type {string | null | undefined} */
  let directory;
  const sources = [];
  for (let i = 1; i < words.length; i++) {
    const w = words[i];
    if (w === "--") {
      sources.push(...words.slice(i + 1));
      break;
    }
    if (w.startsWith("--target-directory=")) directory = w.slice("--target-directory=".length);
    else if (w === "--target-directory" || /^-[A-Za-z]*t$/.test(w)) directory = words[++i] ?? null;
    else if (/^-t./.test(w)) directory = w.slice(2);
    else if (!w.startsWith("-")) sources.push(w);
  }
  if (directory === undefined) return null;
  // A word the hook cannot resolve (a variable, a glob, a substitution) is not judged, as everywhere else in the hook.
  const resolvable = (w) => !/[$`*?()]/.test(w);
  return { directory: directory && resolvable(directory) ? directory : null, sources: sources.filter(resolvable) };
}

/**
 * @param {string} commandText a Bash command line
 * @param {string | undefined} cwd the event's working directory
 * @param {string} root absolute project root
 * @param {(file: string) => boolean} [exists] whether a file exists; a target that does is an edit, not a new document
 * @param {RegExp[]} [extras] names from `.claude/root-files`
 * @returns {string | null} the reason to deny, or null
 */
export function bashVerdict(commandText, cwd, root, exists = existsSync, extras = []) {
  if (typeof commandText !== "string" || !commandText.trim()) return null;
  if (typeof cwd !== "string" || !cwd.trim()) return null;
  const projectRoot = path.resolve(root);
  for (const part of segmentsWithCwd(commandText, path.resolve(cwd))) {
    // `cd $(...)` closes its own parenthesis, and segmentsWithCwd then restores the directory it left: the hook
    // cannot follow it, so it stops judging the rest of the command.
    if (/^[\s({!]*(?:cd|pushd|popd)\s[^\n]*[$`]/.test(part.segment)) break;
    // A directory the hook cannot follow is not judged: a miss is cheaper than a false positive.
    if (part.cwd === null) continue;
    const targetForm = targetDirectoryForm(part.segment);
    const files = writeTargets(part.segment)
      .filter(({ kind }) => !(targetForm && (kind === "cp" || kind === "mv")))
      .map(({ target }) => path.resolve(part.cwd, target));
    // With -t the last argument is a source, not the destination: the files land in the -t directory.
    if (targetForm?.directory) {
      const directory = path.resolve(part.cwd, targetForm.directory);
      files.push(...targetForm.sources.map((source) => path.join(directory, path.basename(source))));
    }
    for (const file of files) {
      if (path.dirname(file) !== projectRoot) continue;
      if (isLooseDocument(path.basename(file), extras) && !exists(file)) return reason(path.basename(file));
    }
  }
  return null;
}

/**
 * @param {Record<string, any>} input the hook event
 * @param {(file: string) => boolean} [exists]
 * @returns {string | null} the reason to deny, or null to allow
 */
export function decide(input, exists = existsSync) {
  const root = projectDir(input);
  const tool = input.tool_input ?? {};
  if (input.tool_name === "Write") {
    const file = typeof tool.file_path === "string" && tool.file_path.trim() ? path.resolve(root, tool.file_path) : "";
    return file ? writeVerdict(file, root, exists(file), rootAllowlist(root)) : null;
  }
  if (input.tool_name === "Bash") return bashVerdict(tool.command, input.cwd, root, exists, rootAllowlist(root));
  return null;
}

if (isMain(import.meta.url)) {
  const message = decide(readHookInput());
  if (message) deny(message);
}
