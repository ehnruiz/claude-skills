#!/usr/bin/env node
/**
 * PreToolUse hook on Bash: block the git commands that publish work or destroy it, before they run.
 *
 * `git push` publishes. `git reset --hard` discards uncommitted changes, `git clean` deletes untracked files and
 * `git branch -D` deletes a branch whether or not it was merged. None of them can be undone from inside the session,
 * so in a project that turns this hook on, the user runs them. A dry run (`git clean -n`) is allowed.
 *
 * The command is read as shell, not searched as text: a quoted argument (`git commit -m "before git push"`), a
 * heredoc body and a comment are data, so they never match. A git invocation is found in command position, after
 * `sudo`, `env`, `command` and similar wrappers, behind git's own options (`git -C dir push`), inside `$( ... )` and
 * backticks, and inside the string of `bash -c`, `sh -c` and `eval`.
 *
 * Not handled: git aliases (`git config alias.p push`, then `git p`), a heredoc fed to a shell (`bash <<EOF`), and
 * commands built from variables. This is a guardrail against a session's mistakes, not a sandbox.
 *
 * Adapted from Matt Pocock's git-guardrails-claude-code (https://github.com/mattpocock/skills, MIT). Opt-in per
 * project; see hooks/README.md.
 */
import path from "node:path";
import { deny, isMain, readHookInput } from "./lib.mjs";
import { segmentsWithCwd, wordsOf } from "./shell.mjs";

/** Git's own options that take their value as the next word, as in `git -C dir push`. */
const GIT_VALUE_OPTIONS = new Set([
  "-C",
  "-c",
  "--git-dir",
  "--work-tree",
  "--namespace",
  "--super-prefix",
  "--config-env",
]);

/** Words that run the command after them: `sudo git push` is a push. */
const WRAPPERS = new Set(["sudo", "doas", "env", "command", "exec", "nohup", "time", "nice", "timeout", "xargs"]);

/** Shell keywords that can stand before a command in the same part: `if git push; then`. */
const KEYWORDS = new Set(["if", "then", "else", "elif", "do", "while", "until", "!", "{", "("]);

/** Shells whose `-c` string is a command of its own. */
const SHELLS = new Set(["sh", "bash", "zsh", "dash", "ksh"]);

/** How deep `bash -c`, `eval` and substitutions are followed, so a pathological command cannot loop. */
const MAX_DEPTH = 5;

/**
 * The short and long options of a git subcommand's arguments, up to `--`. A short group such as `-fd` gives `f` and
 * `d`; the group stops at a letter in `takesValue`, whose value is the rest of the group, or the next word when the
 * letter ends the group (`-e -n` excludes `-n`, it is no dry run). A long option in `longValues` written without `=`
 * takes the next word the same way.
 * @param {string[]} args
 * @param {string} [takesValue] short options that take a value
 * @param {string[]} [longValues] long options that take a value
 * @returns {{ short: Set<string>, long: Set<string> }}
 */
function options(args, takesValue = "", longValues = []) {
  const short = new Set();
  const long = new Set();
  for (let i = 0; i < args.length; i++) {
    const arg = args[i];
    if (arg === "--") break;
    if (arg.startsWith("--")) {
      const name = arg.split("=")[0];
      long.add(name);
      if (!arg.includes("=") && longValues.some((v) => isLong(name, v))) i++;
    } else if (arg.startsWith("-") && arg.length > 1) {
      const letters = arg.slice(1);
      for (let k = 0; k < letters.length; k++) {
        short.add(letters[k]);
        if (takesValue.includes(letters[k])) {
          if (k === letters.length - 1) i++;
          break;
        }
      }
    }
  }
  return { short, long };
}

/**
 * Whether `written` names the long option `full`. Git accepts any unambiguous prefix, so `git reset --har` is a hard
 * reset; a prefix of four characters or more counts.
 * @param {string} written
 * @param {string} full
 */
function isLong(written, full) {
  return written.length >= 4 && full.startsWith(written);
}

/** @param {Set<string>} long @param {string} full */
const hasLong = (long, full) => [...long].some((w) => isLong(w, full));

/**
 * The rules, one per blocked command: the subcommand, a test on its arguments, and what the command would do.
 * @type {{ sub: string, label: string, blocks: (args: string[]) => boolean, why: string }[]}
 */
export const RULES = [
  {
    sub: "push",
    label: "git push",
    blocks: () => true,
    why: "publishes commits to a remote, where other people and CI see them",
  },
  {
    sub: "reset",
    label: "git reset --hard",
    blocks: (args) => hasLong(options(args).long, "--hard"),
    why: "discards uncommitted changes in the working tree for good",
  },
  {
    sub: "clean",
    label: "git clean",
    blocks: (args) => {
      const o = options(args, "e", ["--exclude"]);
      return !(o.short.has("n") || hasLong(o.long, "--dry-run"));
    },
    why: "deletes untracked files, which git cannot bring back",
  },
  {
    sub: "branch",
    label: "git branch -D",
    blocks: (args) => {
      const o = options(args, "u", ["--set-upstream-to"]);
      const del = o.short.has("d") || hasLong(o.long, "--delete");
      const force = o.short.has("f") || hasLong(o.long, "--force");
      return o.short.has("D") || (del && force);
    },
    why: "deletes a branch whether or not it was merged",
  },
];

/**
 * A word without what opens or closes a group or substitution around it, and without a redirection or `&` attached
 * to its end: `$(git` is `git`, `push)` and `push>/dev/null` are `push`.
 * @param {string} word
 */
function bare(word) {
  return word.replace(/^[$({`]+/, "").replace(/[<>&].*$/, "").replace(/[)}`]+$/, "");
}

/**
 * The bodies of the command substitutions in a segment, `$( ... )` and backticks, outside single quotes. A body may
 * hold a substitution of its own; that one is found when the body is read in turn.
 * @param {string} segment
 * @returns {string[]}
 */
function substitutions(segment) {
  const bodies = [];
  let quote = "";
  for (let i = 0; i < segment.length; i++) {
    const c = segment[i];
    if (quote === "'") {
      if (c === "'") quote = "";
    } else if (c === "\\") {
      i++;
    } else if (c === "'" && !quote) {
      quote = "'";
    } else if (c === '"') {
      quote = quote ? "" : '"';
    } else if (c === "$" && segment[i + 1] === "(" && segment[i + 2] !== "(") {
      let depth = 1;
      let j = i + 2;
      for (; j < segment.length && depth > 0; j++) {
        if (segment[j] === "(") depth++;
        else if (segment[j] === ")") depth--;
      }
      bodies.push(segment.slice(i + 2, depth === 0 ? j - 1 : j));
      i = j - 1;
    } else if (c === "`") {
      const end = segment.indexOf("`", i + 1);
      bodies.push(segment.slice(i + 1, end === -1 ? segment.length : end));
      i = end === -1 ? segment.length : end;
    }
  }
  return bodies;
}

/**
 * Where the command word is: past leading variable assignments (`GIT_TRACE=1 git push`) and shell keywords.
 * @param {string[]} words
 */
function commandStart(words) {
  let at = 0;
  while (at < words.length && (KEYWORDS.has(words[at]) || /^[A-Za-z_][A-Za-z0-9_]*=/.test(words[at]))) at++;
  return at;
}

/**
 * Where the command after a run of wrappers starts: `sudo -u deploy git push` runs `git`. A wrapper's options,
 * variable assignments (`env A=1`) and numbers (`timeout 60`, `nice -n 10`) are skipped. A short option standing
 * alone is taken to have a value in the next word, unless that word is `git` (`sudo -E git push`).
 * @param {string[]} words
 * @param {number} at
 */
function afterWrappers(words, at) {
  while (at < words.length && WRAPPERS.has(path.basename(bare(words[at])))) {
    at++;
    while (at < words.length) {
      const w = words[at];
      if (/^-[A-Za-z0-9]$/.test(w) && path.basename(bare(words[at + 1] ?? "")) !== "git") at += 2;
      else if (w.startsWith("-") || /^[A-Za-z_][A-Za-z0-9_]*=/.test(w) || /^\d+(\.\d+)?[smhd]?$/.test(w)) at++;
      else break;
    }
  }
  return at;
}

/**
 * The git subcommand and its arguments when `words` run git, or null. Leading variable assignments and wrappers are
 * skipped; git's own options before the subcommand are skipped with their values.
 * @param {string[]} words
 * @returns {{ sub: string, args: string[] } | null}
 */
function gitCall(words) {
  const at = afterWrappers(words, commandStart(words));
  if (at >= words.length || path.basename(bare(words[at])) !== "git") return null;
  for (let j = at + 1; j < words.length; j++) {
    const w = bare(words[j]);
    if (GIT_VALUE_OPTIONS.has(w)) j++;
    else if (!w.startsWith("-")) return { sub: w, args: words.slice(j + 1).map(bare) };
  }
  return null;
}

/**
 * The string a shell runs from `bash -c '...'` or `eval ...`, or null, after any wrappers (`sudo bash -c '...'`).
 * `-c` may sit in a group such as `-lc`.
 * @param {string[]} words
 * @returns {string | null}
 */
function innerCommand(words) {
  const at = afterWrappers(words, commandStart(words));
  const name = path.basename(bare(words[at] ?? ""));
  if (name === "eval") return words.slice(at + 1).join(" ");
  if (!SHELLS.has(name)) return null;
  const flag = words.findIndex((w, i) => i > at && /^-[A-Za-z]*c[A-Za-z]*$/.test(w));
  return flag >= 0 && flag + 1 < words.length ? words[flag + 1] : null;
}

/**
 * The words of one part split at a single `&`, standing alone or attached to the end of a word, which runs the
 * command before it in the background and starts another: `sleep 1 & git push` and `sleep 1& git push` hold two
 * commands. A word that is only whitespace, left by a backslash at the end of a line, is dropped.
 * @param {string[]} words
 * @returns {string[][]}
 */
function backgrounded(words) {
  const groups = [[]];
  for (const w of words) {
    if (/^\s+$/.test(w)) continue;
    if (w === "&") groups.push([]);
    else if (/[^&>]&$/.test(w)) {
      groups[groups.length - 1].push(w.slice(0, -1));
      groups.push([]);
    } else groups[groups.length - 1].push(w);
  }
  return groups.filter((g) => g.length > 0);
}

/**
 * The first blocked git command in a Bash command, or null when there is none.
 * @param {string} command
 * @param {number} [depth]
 * @returns {{ label: string, why: string } | null}
 */
export function blockedGit(command, depth = 0) {
  if (depth > MAX_DEPTH) return null;
  // The directory each part runs in does not matter here; segmentsWithCwd is used for its split, which drops
  // heredoc bodies and comments and keeps each part's quotes.
  for (const { segment } of segmentsWithCwd(command, path.parse(process.cwd()).root)) {
    for (const body of substitutions(segment)) {
      const found = blockedGit(body, depth + 1);
      if (found) return found;
    }
    for (const words of backgrounded(wordsOf(segment.replace(/^[\s({!]+/, "")))) {
      const inner = innerCommand(words);
      if (inner !== null) {
        const found = blockedGit(inner, depth + 1);
        if (found) return found;
      }
      const call = gitCall(words);
      if (!call) continue;
      const rule = RULES.find((r) => r.sub === call.sub && r.blocks(call.args));
      if (rule) return { label: rule.label, why: rule.why };
    }
  }
  return null;
}

/**
 * @param {Record<string, any>} input the hook event
 * @returns {string | null} the reason to deny, or null to allow
 */
export function decide(input) {
  if (input.tool_name !== "Bash") return null;
  const command = input.tool_input?.command;
  if (typeof command !== "string" || !command.trim()) return null;
  const found = blockedGit(command);
  if (!found) return null;
  return (
    `Blocked by the git-guardrails hook: \`${found.label}\` ${found.why}, so in this project the user runs it. ` +
    "Say what you would run and why, and leave it to the user."
  );
}

if (isMain(import.meta.url)) {
  try {
    const reason = decide(readHookInput());
    if (reason) deny(reason);
  } catch {
    // never fail the tool
  }
}
