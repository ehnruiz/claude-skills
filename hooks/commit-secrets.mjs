#!/usr/bin/env node
/**
 * PreToolUse hook on Bash: scan what a `git commit` is about to commit for secrets, before it runs.
 *
 * The hook runs before the command, so the index is not yet what the commit will take. The scan therefore follows the
 * command chain: the staged diff always; the unstaged changes of tracked files when the commit has `-a`; and, for a
 * `git add <paths>` earlier in the chain, those paths' unstaged changes and untracked files. That is one unified-diff
 * text, which goes to gitleaks (`gitleaks stdin`) when it is on PATH, and otherwise (or with `CLAUDE_HOOK_GITLEAKS=off`)
 * through a short list of well-known token shapes; any gitleaks trouble (a missing binary, an exit code other than 0 or
 * 2, an unreadable report, findings that map to no added line) runs the pattern scan instead. A gitleaks finding denies the commit, a pattern finding asks
 * the user. Findings name the rule and the `file:line`, never the value.
 *
 * The composed text is capped (4 MB, 400 untracked files; `CLAUDE_HOOK_MAX_BYTES` and `CLAUDE_HOOK_MAX_FILES` override): the
 * staged and unstaged diffs go in first, then untracked files in path order until the cap. What does not fit is not
 * scanned, and the hook asks the user and says how much was left out, because a hook killed by its timeout allows the
 * commit. A git error allows the commit: this hook must never be the reason a commit fails.
 *
 * Not handled: `--git-dir`/`--work-tree` (the repository is the one `-C`, `cd` and the event's `cwd` give), `git commit
 * <paths>` and `-i`/`-o` pathspecs, `git add -f` of ignored files, shell variables and substitutions in paths, and
 * aliases.
 */
import { Buffer } from "node:buffer";
import { execFileSync, spawnSync } from "node:child_process";
import {
  closeSync,
  lstatSync,
  mkdtempSync,
  openSync,
  readFileSync,
  readSync,
  rmSync,
} from "node:fs";
import { homedir, tmpdir } from "node:os";
import path from "node:path";
import process from "node:process";
import { ask, deny, isMain, projectDir, readHookInput } from "./lib.mjs";
import { sedInPlaceArguments, withoutHeredocs, writeTargets } from "./shell.mjs";

/** Name and shape of each token the fallback scanner knows. Each is bounded so a longer word that ends in it is not a hit. */
export const PATTERNS = [
  ["AWS access key", /(?<![A-Z0-9])(?:AKIA|ASIA)[0-9A-Z]{16}(?![A-Z0-9])/],
  ["GitHub token", /(?<![A-Za-z0-9])ghp_[A-Za-z0-9]{36}/],
  ["GitHub fine-grained token", /(?<![A-Za-z0-9])github_pat_[A-Za-z0-9_]{22,}/],
  ["API secret key (sk-)", /(?<![A-Za-z0-9-])sk-[A-Za-z0-9_-]{20,}/],
  ["Slack token", /(?<![A-Za-z0-9])xox[baprs]-[A-Za-z0-9-]{10,}/],
  ["Google API key", /(?<![A-Za-z0-9_-])AIza[0-9A-Za-z_-]{35}/],
  ["Private key", /-----BEGIN (RSA|EC|OPENSSH|PGP|DSA) PRIVATE KEY/],
  ["Bearer token", /Bearer [A-Za-z0-9._~+/=-]{20,}/],
  // Cloud credentials: the Entra client secret, AWS and Azure Blob Storage. A name is matched only with a real-looking
  // value after it, so `AWS_SECRET_ACCESS_KEY=` in .env.example or `process.env.X` stay quiet.
  ["Entra client secret", /(?<![A-Za-z0-9_.~-])[A-Za-z0-9_.-]{3}8Q~[A-Za-z0-9_.~-]{31,}/],
  [
    "AWS secret access key",
    /(?<![A-Za-z0-9_])AWS_SECRET_ACCESS_KEY["']?\s*[=:]\s*["']?[A-Za-z0-9+/]{40}(?![A-Za-z0-9+/])/,
  ],
  ["Azure storage account key", /AccountKey=[A-Za-z0-9+/]{86}==/],
  ["Azure SAS signature", /[?&]sig=[A-Za-z0-9%]{43,}/],
];

const CLOSING =
  "Remove the secret from the changes (use an environment variable) before committing. See .claude/hooks/commit-secrets.mjs.";

const MAX_UNTRACKED_BYTES = 1024 * 1024;
const MAX_TEXT_BYTES = 4 * 1024 * 1024;
const MAX_UNTRACKED_FILES = 400;
const BINARY_PROBE_BYTES = 8 * 1024;

/**
 * The command split on `&&`, `||`, `;`, `|` and newlines that are not inside quotes.
 * @param {string} command
 * @returns {string[]}
 */
function splitSegments(command) {
  const segments = [];
  let current = "";
  let quote = "";
  for (let i = 0; i < command.length; i++) {
    const c = command[i];
    if (quote) {
      current += c;
      if (c === "\\" && quote === '"' && i + 1 < command.length) current += command[++i];
      else if (c === quote) quote = "";
      continue;
    }
    if (c === "\\" && i + 1 < command.length) {
      current += c + command[++i];
    } else if (c === '"' || c === "'") {
      quote = c;
      current += c;
    } else if (c === "\n" || c === ";" || c === "|" || (c === "&" && command[i + 1] === "&")) {
      if (c === "|" && command[i + 1] === "|") i++;
      else if (c === "&") i++;
      segments.push(current);
      current = "";
    } else {
      current += c;
    }
  }
  segments.push(current);
  return segments;
}

/**
 * Shell words of one segment: whitespace separates, quotes group and are removed, a backslash escapes.
 * @param {string} segment
 * @returns {string[]}
 */
function tokenize(segment) {
  const tokens = [];
  let current = "";
  let started = false;
  let quote = "";
  for (let i = 0; i < segment.length; i++) {
    const c = segment[i];
    if (quote === "'") {
      if (c === "'") quote = "";
      else current += c;
    } else if (quote === '"') {
      if (c === '"') quote = "";
      else if (c === "\\" && i + 1 < segment.length) current += segment[++i];
      else current += c;
    } else if (c === '"' || c === "'") {
      quote = c;
      started = true;
    } else if (c === "\\" && i + 1 < segment.length) {
      current += segment[++i];
      started = true;
    } else if (/\s/.test(c)) {
      if (started || current) tokens.push(current);
      current = "";
      started = false;
    } else {
      current += c;
    }
  }
  if (started || current) tokens.push(current);
  return tokens;
}

/** Git options that take their value as the next word. */
const GIT_VALUE_OPTIONS = new Set([
  "--git-dir",
  "--work-tree",
  "--namespace",
  "--super-prefix",
  "--config-env",
  "--exec-path",
]);

/**
 * The git invocation in a segment: its `-C` directories, subcommand and the arguments after it, or null.
 * @param {string[]} tokens
 * @returns {{ dirs: string[], sub: string, args: string[] } | null}
 */
function parseGit(tokens) {
  const at = tokens.findIndex((t) => path.basename(t.replace(/^[$({`]+/, "")) === "git");
  if (at < 0) return null;
  const dirs = [];
  for (let j = at + 1; j < tokens.length; j++) {
    const t = tokens[j];
    if (t === "-C") {
      if (j + 1 < tokens.length) dirs.push(tokens[++j]);
    } else if (t === "-c" || GIT_VALUE_OPTIONS.has(t)) {
      j++;
    } else if (!t.startsWith("-")) {
      return { dirs, sub: t, args: tokens.slice(j + 1) };
    }
  }
  return null;
}

/**
 * Whether the arguments of `git commit` ask for `-a`: `--all`, or a short-flag group with `a` ahead of any flag that
 * takes a value (`-am` is `-a -m`, `-ma` is `-m a`).
 * @param {string[]} args
 */
function commitTakesAll(args) {
  for (const arg of args) {
    if (arg === "--") break;
    if (arg === "--all") return true;
    if (!/^-[A-Za-z]/.test(arg)) continue;
    for (const flag of arg.slice(1)) {
      if (flag === "a") return true;
      if ("mFCctSu".includes(flag)) break;
    }
  }
  return false;
}

/**
 * What a `git add` stages: its pathspecs, and whether only tracked files count (`-u`). Null when it adds nothing.
 * @param {string[]} args
 * @returns {{ paths: string[], trackedOnly: boolean } | null}
 */
function parseAdd(args) {
  const paths = [];
  let whole = false;
  let trackedOnly = false;
  let afterDashes = false;
  for (const arg of args) {
    if (!afterDashes && arg === "--") afterDashes = true;
    else if (!afterDashes && arg.startsWith("-")) {
      if (arg === "-A" || arg === "--all") whole = true;
      else if (arg === "-u" || arg === "--update") {
        whole = true;
        trackedOnly = true;
      }
    } else {
      paths.push(arg);
    }
  }
  if (paths.length === 0) return whole ? { paths: ["."], trackedOnly } : null;
  return { paths, trackedOnly };
}

/**
 * The segments of a command chain: heredoc bodies and full-line comments are text, not commands.
 * @param {string} command
 */
const chainSegments = (command) =>
  splitSegments(
    withoutHeredocs(command)
      .split("\n")
      .filter((line) => !/^\s*#/.test(line))
      .join("\n"),
  );

/** The directory a `cd` or `pushd` segment moves to, or null when the tokens are not a move. @param {string[]} tokens */
function moveOf(tokens) {
  if ((tokens[0] !== "cd" && tokens[0] !== "pushd") || tokens.length < 2) return null;
  const dir = tokens.slice(1).find((t) => !t.startsWith("-") || t === "-");
  return dir && dir !== "-" ? dir : null;
}

/**
 * The command chain up to its first `git commit`, or null when it holds none.
 * @param {string} command
 * @returns {{ moves: string[], dirs: string[], all: boolean, index: number, adds: { paths: string[], trackedOnly: boolean, moves: string[], dirs: string[], index: number }[] } | null}
 *   each add keeps the `cd`/`pushd` moves and `-C` directories in force where it runs, apart from the commit's; `index`
 *   is the segment of the chain (counting every `&&`, `;`, `|` and newline) the add or the commit stands in
 */
function parseChain(command) {
  if (typeof command !== "string") return null;
  const moves = [];
  const adds = [];
  const segments = chainSegments(command);
  for (let index = 0; index < segments.length; index++) {
    const tokens = tokenize(segments[index].replace(/^[\s({]+/, ""));
    const move = moveOf(tokens);
    if (move) moves.push(move);
    if ((tokens[0] === "cd" || tokens[0] === "pushd") && tokens.length > 1) continue;
    const git = parseGit(tokens);
    if (!git) continue;
    if (git.sub === "add") {
      const add = parseAdd(git.args);
      if (add) adds.push({ ...add, moves: [...moves], dirs: git.dirs, index });
    } else if (git.sub === "commit") {
      return { moves, dirs: git.dirs, all: commitTakesAll(git.args), index, adds };
    }
  }
  return null;
}

/** @param {string} base @param {string} dir */
const resolveDir = (base, dir) =>
  path.resolve(base, dir === "~" ? homedir() : dir.replace(/^~\//, `${homedir()}/`));

/**
 * The repository a command commits in, or null when the command holds no `git commit`. A `cd` or `pushd` earlier in
 * the chain moves the base, then each `-C` of the commit applies. `--git-dir` and `--work-tree` are not followed.
 * @param {string} command
 * @param {string} base the directory the command starts in: the event's `cwd`, else the project root
 * @returns {string | null}
 */
export function commitRepo(command, base) {
  const chain = parseChain(command);
  if (!chain) return null;
  let dir = base;
  for (const move of chain.moves) dir = resolveDir(dir, move);
  for (const c of chain.dirs) dir = resolveDir(dir, c);
  return dir;
}

/**
 * What a command's commit will take, worked out from the command alone.
 * @param {string} command
 * @param {string} [base] the directory the command starts in; each add's `dir` is where that `git add` runs
 * @returns {{ staged: true, unstaged: boolean, adds: { paths: string[], trackedOnly: boolean, dir: string }[] } | null} null when the command holds no `git commit`
 */
export function scanPlan(command, base = ".") {
  const chain = parseChain(command);
  if (!chain) return null;
  const adds = chain.adds.map(({ moves, dirs, paths, trackedOnly }) => {
    let dir = path.resolve(base);
    for (const d of [...moves, ...dirs]) dir = resolveDir(dir, d);
    return { paths, trackedOnly, dir };
  });
  return { staged: true, unstaged: chain.all, adds };
}

/** Whether an argument of `sed -i` / `perl -i` is a script (`s/a/b/`, `/x/d`, `3d`) rather than a file. */
const looksLikeScript = (arg) =>
  /^[sy]([^\w\s]).*\1/.test(arg) ||
  /^\/.*\/[A-Za-z]*$/.test(arg) ||
  /^[\d$,]*[dpDPgGhHxnN]$/.test(arg);

/** @param {string} repo @returns {(file: string) => boolean} whether git tracks the absolute path in `repo` (false outside it) */
const trackedIn = (repo) => (file) =>
  git(repo, ["ls-files", "--error-unmatch", "--", file]).trim() !== "";

/** @param {string} repo @returns {(dir: string) => boolean} whether `dir` is in the repository of `repo`; same test `collectText` applies to an add */
function sameRepoAs(repo) {
  const commitTop = git(repo, ["rev-parse", "--show-toplevel"]).trim();
  return (dir) => {
    const top = git(dir, ["rev-parse", "--show-toplevel"]).trim();
    return Boolean(top) && (!commitTop || top === commitTop);
  };
}

/**
 * The write targets of a command that its commit would include but the hook cannot read yet, because the command
 * writes them after the hook has run. A write counts only when it comes before what would take it in: before a
 * `git add` of the plan (in this repository) that covers it, or, when the commit takes all (`-a`), before the commit and
 * only for a file this repository tracks. `git add -u` covers tracked files only. Paths compare normalized and
 * relative to the directory of the add; a directory add covers its subtree.
 * @param {string} command
 * @param {ReturnType<typeof scanPlan>} plan
 * @param {{ base?: string, repo?: string, isTracked?: (file: string) => boolean, sameRepo?: (dir: string) => boolean }} [deps]
 *   `base` is the directory the command starts in (as given to `scanPlan`); `isTracked` takes an absolute path and
 *   says whether the commit's repository tracks it (default: `git ls-files --error-unmatch` in `repo`, else `base`);
 *   `sameRepo` says whether an add's directory is in the commit's repository (default: yes)
 * @returns {string[]} the targets as written in the command, once each
 */
export function unscannableWrites(command, plan, deps = {}) {
  const chain = plan && typeof command === "string" ? parseChain(command) : null;
  if (!plan || !chain) return [];
  const base = path.resolve(deps.base ?? ".");
  const isTracked = deps.isTracked ?? trackedIn(deps.repo ?? base);
  const sameRepo = deps.sameRepo ?? (() => true);
  // plan.adds and chain.adds are the same adds in the same order; the plan has the directories, the chain the places
  const adds = plan.adds
    .map((add, i) => ({ ...add, index: chain.adds[i].index }))
    .filter((add) => sameRepo(add.dir));
  const found = [];
  let dir = base;
  chainSegments(command).forEach((segment, index) => {
    const tokens = tokenize(segment.replace(/^[\s({]+/, ""));
    const move = moveOf(tokens);
    if (move) dir = resolveDir(dir, move);
    if (index >= chain.index) return;
    const targets = [
      ...writeTargets(segment).map((w) => w.target),
      ...sedInPlaceArguments(segment).filter((a) => !looksLikeScript(a)),
    ];
    for (const target of targets) {
      if (found.includes(target)) continue;
      const file = path.resolve(dir, target);
      const takenByAll = plan.unstaged && isTracked(file);
      const takenByAdd = adds.some(
        (add) =>
          add.index > index &&
          add.paths.some((p) => {
            const rel = path.relative(path.resolve(add.dir, p), file);
            return (
              rel === "" ||
              (rel !== ".." && !rel.startsWith(`..${path.sep}`) && !path.isAbsolute(rel))
            );
          }) &&
          (!add.trackedOnly || isTracked(file)),
      );
      if (takenByAll || takenByAdd) found.push(target);
    }
  });
  return found;
}

/**
 * One unified-diff-shaped text from git's results. Real diffs are kept (once each); an untracked file becomes a
 * new-file diff with every line added.
 * @param {{ staged?: string, unstaged?: string, addDiffs?: string[], untracked?: { path: string, content: string }[] }} parts
 * @returns {string} empty when there is nothing to scan
 */
export function buildScanText({ staged = "", unstaged = "", addDiffs = [], untracked = [] }) {
  const out = [];
  const seen = new Set();
  for (const diff of [staged, unstaged, ...addDiffs]) {
    if (!diff.trim() || seen.has(diff)) continue;
    seen.add(diff);
    out.push(diff.endsWith("\n") ? diff : `${diff}\n`);
  }
  for (const file of untracked) {
    if (!file.content) continue;
    const lines = file.content.replace(/\r?\n$/, "").split(/\r?\n/);
    out.push(
      [
        `diff --git a/${file.path} b/${file.path}`,
        "new file mode 100644",
        "--- /dev/null",
        `+++ b/${file.path}`,
        `@@ -0,0 +1,${lines.length} @@`,
        ...lines.map((l) => `+${l}`),
        "",
      ].join("\n"),
    );
  }
  return out.join("");
}

/**
 * Walks the added lines of a unified diff, with the line's place in the text and in its file.
 * @param {string} diffText
 * @returns {{ index: number, file: string, line: number, text: string }[]} `index` is 1-based, as a scanner counts lines
 */
function addedLines(diffText) {
  const added = [];
  let file = "";
  let line = 0;
  let inHunk = false;
  const rows = diffText.split("\n");
  for (let i = 0; i < rows.length; i++) {
    const text = rows[i];
    if (text.startsWith("diff --git ")) {
      inHunk = false;
    } else if (!inHunk && text.startsWith("+++ ")) {
      file = text.slice(4).replace(/^b\//, "");
    } else if (text.startsWith("@@")) {
      inHunk = true;
      const m = /\+(\d+)/.exec(text);
      line = m ? Number(m[1]) : 0;
    } else if (inHunk && text.startsWith("+")) {
      added.push({ index: i + 1, file, line, text: text.slice(1) });
      line++;
    } else if (text.startsWith(" ")) {
      line++;
    }
  }
  return added;
}

/**
 * Findings on the added lines of a unified diff (`-U0` or more context).
 * @param {string} diffText
 * @returns {{ name: string, file: string, line: number }[]}
 */
export function scanDiff(diffText) {
  const findings = [];
  for (const { file, line, text } of addedLines(diffText)) {
    for (const [name, re] of PATTERNS) if (re.test(text)) findings.push({ name, file, line });
  }
  return findings;
}

/**
 * The `file:line` of a line of a diff text, as a scanner that read the whole text reports it (gitleaks `StartLine`).
 * Null for a header, a removed line or a context line: only added lines are findings.
 * @param {string} diffText
 * @param {number} textLine 1-based
 * @returns {{ file: string, line: number } | null}
 */
export function locateInDiff(diffText, textLine) {
  return indexDiff(diffText).get(textLine) ?? null;
}

/**
 * The place of every added line of a diff text, keyed by its 1-based line in the text. Built once per scan, so looking
 * up many findings does not walk the text again.
 * @param {string} diffText
 * @returns {Map<number, { file: string, line: number }>}
 */
export function indexDiff(diffText) {
  const index = new Map();
  for (const a of addedLines(diffText)) index.set(a.index, { file: a.file, line: a.line });
  return index;
}

/** @param {{ name: string, file: string, line: number }[]} findings */
const listFindings = (findings) =>
  findings.map((f) => `${f.name} in ${f.file}:${f.line}`).join("; ");

const gitleaksBin = () => process.env.CLAUDE_HOOK_GITLEAKS_BIN || "gitleaks";

/**
 * @param {string} text the diff text, piped to `gitleaks stdin`
 * @returns {{ status: 'clean' } | { status: 'error' } | { status: 'unmapped' } | { status: 'found', found: { rule: string, file: string, line: number }[] }}
 *   `error` for anything but exit 0 or 2 and a readable report, and `unmapped` for findings that fall on no added line:
 *   the caller runs the patterns for both
 */
function gitleaksScan(text) {
  const dir = mkdtempSync(path.join(tmpdir(), "claude-gitleaks-"));
  const report = path.join(dir, "report.json");
  try {
    const args = [
      "stdin",
      "--no-banner",
      "--redact",
      "--exit-code",
      "2",
      "--report-format",
      "json",
      "--report-path",
      report,
    ];
    const result = spawnSync(gitleaksBin(), args, {
      input: text,
      stdio: ["pipe", "ignore", "ignore"],
      maxBuffer: 64 * 1024 * 1024,
    });
    if (result.error) return { status: "error" };
    if (result.status === 0) return { status: "clean" };
    if (result.status !== 2) return { status: "error" };
    const parsed = JSON.parse(readFileSync(report, "utf8"));
    if (!Array.isArray(parsed)) return { status: "error" };
    const index = indexDiff(text);
    const found = [];
    const seen = new Set();
    for (const f of parsed) {
      const where = index.get(Number(f.StartLine ?? 0));
      if (!where) continue; // a removed line or a header: taking a secret out is not a leak
      const rule = String(f.RuleID ?? "secret");
      const key = `${rule}\0${where.file}\0${where.line}`;
      if (seen.has(key)) continue;
      seen.add(key);
      found.push({ rule, ...where });
    }
    if (found.length > 0) return { status: "found", found };
    return { status: parsed.length > 0 ? "unmapped" : "clean" };
  } catch {
    return { status: "error" };
  } finally {
    rmSync(dir, { recursive: true, force: true });
  }
}

/**
 * Output of a git command in `repo`, or '' when git fails (not a repository, a bad pathspec).
 * @param {string} repo
 * @param {string[]} args
 */
function git(repo, args) {
  try {
    return execFileSync("git", ["-c", "core.quotepath=off", ...args], {
      cwd: repo,
      encoding: "utf8",
      maxBuffer: 64 * 1024 * 1024,
      stdio: ["ignore", "pipe", "ignore"],
    });
  } catch {
    return "";
  }
}

/**
 * Whether a file has a NUL byte in its first 8 KB (binary), or cannot be read.
 * @param {string} file absolute
 * @param {number} size
 */
function looksBinary(file, size) {
  try {
    const fd = openSync(file, "r");
    try {
      const probe = Buffer.alloc(Math.min(size, BINARY_PROBE_BYTES));
      readSync(fd, probe, 0, probe.length, 0);
      return probe.includes(0);
    } finally {
      closeSync(fd);
    }
  } catch {
    return true;
  }
}

/**
 * The text of an untracked file, or null when it is not a text file within the per-file cap (a NUL in its first 8 KB,
 * not a regular file, unreadable).
 * @param {string} file absolute
 * @param {number} size
 */
function readText(file, size) {
  try {
    if (size > MAX_UNTRACKED_BYTES || looksBinary(file, size)) return null;
    return readFileSync(file, "utf8");
  } catch {
    return null;
  }
}

/** @param {string | undefined} raw @param {number} fallback */
const positiveInt = (raw, fallback) =>
  Number.isInteger(Number(raw)) && Number(raw) > 0 ? Number(raw) : fallback;

/**
 * Runs git for each part of the plan and builds the text to scan, within the caps: the diffs first (cut at a line when
 * they alone pass the cap), then untracked files in path order while they fit.
 * @param {string} repo
 * @param {NonNullable<ReturnType<typeof scanPlan>>} plan
 * @returns {{ text: string, skipped: { files: number, bytes: number } }} `skipped` is what the caps left unscanned
 */
function collectText(repo, plan) {
  const maxBytes = positiveInt(process.env.CLAUDE_HOOK_MAX_BYTES, MAX_TEXT_BYTES);
  const maxFiles = positiveInt(process.env.CLAUDE_HOOK_MAX_FILES, MAX_UNTRACKED_FILES);
  const skipped = { files: 0, bytes: 0 };
  const diff = ["diff", "-U0", "--no-color"];
  const addDiffs = [];
  /** @type {Map<string, string>} absolute path to the path git reports (relative to its repository root) */
  const names = new Map();
  const commitTop = git(repo, ["rev-parse", "--show-toplevel"]).trim();
  for (const add of plan.adds) {
    // each add runs in the directory in force where it stands in the chain; its paths resolve there. An add in
    // another repository (`git -C /other add . && git commit`) stages nothing for this commit and is left out.
    const top = git(add.dir, ["rev-parse", "--show-toplevel"]).trim();
    if (!top || (commitTop && top !== commitTop)) continue;
    addDiffs.push(git(add.dir, [...diff, "--", ...add.paths]));
    if (add.trackedOnly) continue;
    for (const rel of git(add.dir, [
      "ls-files",
      "-z",
      "--others",
      "--full-name",
      "--exclude-standard",
      "--",
      ...add.paths,
    ]).split("\0")) {
      if (rel) names.set(path.join(top, rel), rel);
    }
  }
  // `-a` commits the working tree of every tracked file, so the staged version of a file it changed again is not what
  // lands: scan the working tree against HEAD instead of the index and the unstaged changes (no HEAD yet: both)
  const takesAll =
    plan.unstaged && git(repo, ["rev-parse", "--verify", "-q", "HEAD"]).trim() !== "";
  let head = buildScanText({
    staged: takesAll
      ? git(repo, [...diff, "HEAD"])
      : git(repo, ["diff", "--cached", "-U0", "--no-color"]),
    unstaged: plan.unstaged && !takesAll ? git(repo, diff) : "",
    addDiffs,
  });
  if (head.length > maxBytes) {
    const rest = head.slice(head.lastIndexOf("\n", maxBytes) + 1);
    head = head.slice(0, head.length - rest.length);
    skipped.files += Math.max(1, rest.match(/^diff --git /gm)?.length ?? 0);
    skipped.bytes += rest.length;
  }
  let used = head.length;
  let considered = 0;
  const untracked = [];
  for (const [file, rel] of [...names].sort(([a], [b]) => (a < b ? -1 : a > b ? 1 : 0))) {
    let size;
    try {
      const stat = lstatSync(file);
      if (!stat.isFile()) continue;
      size = stat.size;
    } catch {
      continue;
    }
    if (size > MAX_UNTRACKED_BYTES) {
      // a text file over the per-file cap enters the commit unread: count it so the user is asked; a binary stays silent
      if (!looksBinary(file, size)) {
        skipped.files++;
        skipped.bytes += size;
      }
      continue;
    }
    considered++;
    if (considered > maxFiles || used + size > maxBytes) {
      skipped.files++;
      skipped.bytes += size;
      continue;
    }
    const content = readText(file, size);
    if (content === null) continue;
    used += content.length;
    untracked.push({ path: rel, content });
  }
  return { text: head + buildScanText({ untracked }), skipped };
}

function gitleaksOnPath() {
  const probe = spawnSync(gitleaksBin(), ["version"], { stdio: "ignore" });
  return !probe.error && probe.status === 0;
}

function main() {
  const input = readHookInput();
  if (input.tool_name !== "Bash") return;
  const command = input.tool_input?.command;
  const root = projectDir(input);
  const base = typeof input.cwd === "string" && input.cwd ? path.resolve(root, input.cwd) : root;
  const repo = commitRepo(command, base);
  const plan = scanPlan(command, base);
  if (!repo || !plan) return;

  const { text, skipped } = collectText(repo, plan);
  const unscannable = unscannableWrites(command, plan, {
    base,
    repo,
    isTracked: trackedIn(repo),
    sameRepo: sameRepoAs(repo),
  });
  const same =
    unscannable.length > 0
      ? ` ${unscannable.join(", ")} are written and added in the same command, so the commit-secrets hook cannot scan them before they exist. Run the write first, then add and commit in a separate command.`
      : "";
  const note =
    (skipped.files > 0
      ? ` ${skipped.files} files / ${(skipped.bytes / (1024 * 1024)).toFixed(1)} MB not scanned by commit-secrets: too much for one commit; split it or confirm.`
      : "") + same;
  if (!text.trim() && !note) return;

  let clean = false;
  if (text.trim() && process.env.CLAUDE_HOOK_GITLEAKS !== "off" && gitleaksOnPath()) {
    const result = gitleaksScan(text);
    if (result.status === "found") {
      deny(
        `Blocked by the commit-secrets hook (gitleaks): ${result.found.map((f) => `${f.rule} in ${f.file}:${f.line}`).join("; ")}. ${CLOSING}${note}`,
      );
      return;
    }
    clean = result.status === "clean"; // on 'error' or 'unmapped' the patterns run instead
  }

  const findings = clean ? [] : scanDiff(text);
  if (findings.length > 0)
    ask(
      `The commit-secrets hook found what looks like a secret in the changes to commit: ${listFindings(findings)}. ${CLOSING}${note}`,
    );
  else if (note) ask(note.trim());
}

if (isMain(import.meta.url)) {
  try {
    main();
  } catch {
    // never fail the tool
  }
}
