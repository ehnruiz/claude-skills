/**
 * Shell-command tokenizing shared by the hooks that read a Bash command (config-protection, commit-secrets,
 * git-guardrails).
 *
 * Pure string functions, no I/O. They are a heuristic, not a shell parser: they take the command text, remove what is
 * data rather than command (heredoc bodies, quoted strings) and report where the command writes. When in doubt they
 * report nothing, because a hook that denies a legitimate command is worse than one that misses an exotic one.
 */

import { homedir } from "node:os";
import path from "node:path";

/**
 * The command without its heredoc bodies: the lines between `<<EOF` (or `<<-EOF`, `<<'EOF'`, `<<"EOF"`) and the
 * terminator are file content, not commands, and a test or a document written this way often quotes a redirection.
 * A here-string (`<<<`) opens no heredoc.
 * @param {string} command
 */
export function withoutHeredocs(command) {
  const out = [];
  let terminator = null;
  for (const line of command.split("\n")) {
    if (terminator !== null) {
      if (line.trim() === terminator) terminator = null;
      continue;
    }
    out.push(line);
    const m = /(?<!<)<<(?!<)-?\s*(?:'([^']+)'|"([^"]+)"|([A-Za-z_][A-Za-z0-9_]*))/.exec(line);
    if (m) terminator = m[1] ?? m[2] ?? m[3];
  }
  return out.join("\n");
}

/**
 * Characters that make the `>` after them something other than a redirection: the second `>` of `>>`, the `>` of
 * `<>`, an escaped `\>` (as in `grep \<word\>`), and arrows such as `a=>b` or `%h->%s` in a bare argument. Missing an
 * exotic `a=>file` is the lesser harm; denying an ordinary grep is the greater.
 */
const NOT_A_REDIRECT_BEFORE = "<>\\\\=\\-";

/**
 * Whether the text before a quote makes the quoted string a write target: after a redirection operator, and (unless
 * `keep` is `'redirect'`) after `tee` (and its options) or anywhere in a `cp` or `mv` command.
 * @param {string} before
 * @param {'all' | 'redirect'} keep
 */
function quoteIsTarget(before, keep) {
  if (new RegExp(`(?:^|[^${NOT_A_REDIRECT_BEFORE}])>>?\\|?\\s*$`).test(before)) return true;
  if (keep === "redirect") return false;
  const segment = before.slice(before.search(/[^;&|\n]*$/));
  return /^[\s({]*(?:tee|cp|mv)\s/.test(segment);
}

/**
 * The command without its quoted strings: each single- or double-quoted string (a double-quoted one may span lines
 * and hold `$(...)`) becomes `""`, because what is inside is an argument, not a command. A quoted string that is
 * itself the target of a redirection, `tee`, `cp` or `mv` stays, so `> "brief.md"` is still judged; with `keep`
 * set to `'redirect'` only redirection targets stay, for a pass that must not read a `>` inside a quoted `cp` source
 * as a redirection. Run `withoutHeredocs` first.
 * @param {string} command
 * @param {'all' | 'redirect'} [keep]
 */
export function withoutQuotedText(command, keep = "all") {
  let out = "";
  for (let i = 0; i < command.length; i++) {
    const ch = command[i];
    if (ch === "\\") {
      out += ch + (command[i + 1] ?? "");
      i++;
      continue;
    }
    if (ch !== '"' && ch !== "'") {
      out += ch;
      continue;
    }
    let j = i + 1;
    while (j < command.length && command[j] !== ch) j += ch === '"' && command[j] === "\\" ? 2 : 1;
    const quoted = command.slice(i, j + 1);
    out += quoteIsTarget(out, keep) ? quoted : '""';
    i = j;
  }
  return out;
}

/**
 * Heredoc bodies, comments and quoted text removed: what is left is shell syntax and bare words. Full-line comments
 * go before quotes, because an apostrophe in `# don't` would otherwise open a quote that swallows the commands below;
 * a trailing comment (`echo done # > brief.md`) goes after, once a `#` inside a quoted argument has been masked.
 * @param {string} command
 * @param {'all' | 'redirect'} [keep] which quoted write targets survive, see `withoutQuotedText`
 */
export function shellSkeleton(command, keep = "all") {
  return withoutTrailingComments(
    withoutQuotedText(withoutHeredocs(command).replace(/^\s*#.*$/gm, ""), keep),
  );
}

/**
 * Drops a trailing comment from each line: a `#` at the start or after whitespace, outside the quoted write targets
 * that `withoutQuotedText` kept (so `> "my # brief.md"` keeps its name).
 * @param {string} text
 */
function withoutTrailingComments(text) {
  let out = "";
  let quote = null;
  for (let i = 0; i < text.length; i++) {
    const ch = text[i];
    if (quote) {
      if (ch === "\\" && quote === '"') {
        out += ch + (text[i + 1] ?? "");
        i++;
        continue;
      }
      if (ch === quote) quote = null;
      out += ch;
      continue;
    }
    if (ch === "\\") {
      // an escaped character outside quotes is literal: `\"` opens no string
      out += ch + (text[i + 1] ?? "");
      i++;
      continue;
    }
    if (ch === '"' || ch === "'") {
      quote = ch;
      out += ch;
      continue;
    }
    if (ch === "#" && (i === 0 || /\s/.test(text[i - 1]))) {
      const end = text.indexOf("\n", i);
      if (end === -1) break;
      i = end - 1;
      continue;
    }
    out += ch;
  }
  return out;
}

/**
 * How many `(` and `)` a segment holds outside quotes, and how many `(` open it (before its first word).
 * @param {string} segment
 */
function parens(segment) {
  let opens = 0;
  let closes = 0;
  let leading = 0;
  let atStart = true;
  let quote = "";
  for (let i = 0; i < segment.length; i++) {
    const c = segment[i];
    if (quote) {
      if (c === "\\" && quote === '"') i++;
      else if (c === quote) quote = "";
      continue;
    }
    if (c === "\\") {
      i++;
      atStart = false;
    } else if (c === '"' || c === "'") {
      quote = c;
      atStart = false;
    } else if (c === "(") {
      opens++;
      if (atStart) leading++;
    } else if (c === ")") {
      closes++;
      atStart = false;
    } else if (!/[\s{!]/.test(c)) {
      atStart = false;
    }
  }
  return { opens, closes, leading };
}

/**
 * The command split at `&&`, `||`, `;`, `|` and newlines outside quotes, with the directory each part runs in, as the
 * `cd`, `pushd` and `popd` before it leave it. Each `segment` is the part as written (quotes and all), so the other
 * functions here can read it. A `cd` takes an absolute path or one relative to the directory in force; `~` and `~/x`
 * resolve against `home`. `pushd` pushes the current directory on a stack and `popd` returns to it (with an empty
 * stack it changes nothing). A `cd` inside `( ... )` (or a `$( ... )`) applies to the segments after it inside the
 * group, and the directory and the stack are restored when the group closes; `{ ... }` restores nothing.
 *
 * The helper serves guards, so an unknown directory fails closed: a `cd` that cannot be followed (a variable, a
 * substitution, a glob, `cd -`, a missing argument, `popd +N`) makes `cwd` `null` until an absolute `cd`, and
 * `lastKnown` is then the last directory that was known, so the caller can judge against it and against the root.
 * @param {string} command
 * @param {string} cwd absolute directory the command starts in
 * @param {string} [home] the home directory, for `~`
 * @returns {{ segment: string, cwd: string | null, lastKnown: string }[]}
 */
export function segmentsWithCwd(command, cwd, home = homedir()) {
  const code = withoutTrailingComments(withoutHeredocs(command).replace(/^\s*#.*$/gm, ""));
  /** @type {string | null} */
  let dir = cwd;
  let last = cwd;
  /** @type {(string | null)[]} */
  let stack = [];
  /** @type {{ dir: string | null, last: string, stack: (string | null)[] }[]} */
  const frames = [];
  const out = [];
  /** @param {string | null} next */
  const moveTo = (next) => {
    dir = next;
    if (next !== null) last = next;
  };
  for (const segment of splitUnquoted(code)) {
    if (!segment.trim()) continue;
    out.push({ segment, cwd: dir, lastKnown: last });
    const { opens, closes } = parens(segment);
    for (let i = 0; i < opens; i++) frames.push({ dir, last, stack: [...stack] });
    const words = wordsOf(segment.replace(/^[\s({!]+/, ""));
    if (words[0] === "popd") {
      if (words.length > 1) moveTo(null);
      else if (stack.length > 0) moveTo(/** @type {string | null} */ (stack.pop()));
    } else if (words[0] === "cd" || words[0] === "pushd") {
      if (words[0] === "pushd") stack.push(dir);
      const arg = words
        .slice(1)
        .find((w) => (w === "--" ? false : !w.startsWith("-") || w === "-"));
      if (
        arg === undefined ||
        arg === "-" ||
        /[$`*?]/.test(arg) ||
        (arg.startsWith("~") && arg !== "~" && !arg.startsWith("~/"))
      ) {
        moveTo(null);
      } else if (arg === "~" || arg.startsWith("~/")) {
        moveTo(path.resolve(home, arg.slice(2)));
      } else if (dir !== null || path.isAbsolute(arg)) {
        moveTo(path.resolve(dir ?? "/", arg));
      }
    }
    for (let i = 0; i < closes && frames.length > 0; i++) {
      const frame = /** @type {{ dir: string | null, last: string, stack: (string | null)[] }} */ (
        frames.pop()
      );
      dir = frame.dir;
      last = frame.last;
      stack = frame.stack;
    }
  }
  return out;
}

/** The parts of a command split at `&&`, `||`, `;`, `|` and newlines that are outside quotes and not escaped. @param {string} command */
function splitUnquoted(command) {
  const parts = [];
  let current = "";
  let quote = "";
  for (let i = 0; i < command.length; i++) {
    const c = command[i];
    if (quote) {
      current += c;
      if (c === "\\" && quote === '"' && i + 1 < command.length) current += command[++i];
      else if (c === quote) quote = "";
    } else if (c === "\\" && i + 1 < command.length) {
      current += c + command[++i];
    } else if (c === '"' || c === "'") {
      quote = c;
      current += c;
    } else if (c === "\n" || c === ";" || (c === "|" && command[i - 1] !== ">") || (c === "&" && command[i + 1] === "&")) {
      // `>|` is a redirection (noclobber override), not a pipe.
      if ((c === "|" && command[i + 1] === "|") || c === "&") i++;
      parts.push(current);
      current = "";
    } else {
      current += c;
    }
  }
  parts.push(current);
  return parts;
}

/** The words of one segment: whitespace separates, quotes group and are removed, a backslash escapes. @param {string} segment */
export function wordsOf(segment) {
  const words = [];
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
      if (started || current) words.push(current);
      current = "";
      started = false;
    } else {
      current += c;
    }
  }
  if (started || current) words.push(current);
  return words;
}

/** @param {string} token a word, possibly quoted; returns it unquoted (a quoted word keeps its spaces), or null for shell syntax */
function unquote(token) {
  const m = /^(["'])(.+)\1$/.exec(token);
  if (m) return /[\n"'<>|;&$`*?()\\]/.test(m[2]) ? null : m[2];
  return /[\s"'<>|;&$`*?()\\]/.test(token) ? null : token;
}

/** A word without the `)` that closes a group after it: `tsconfig.json)` names `tsconfig.json`. @param {string} token */
function withoutClosingParens(token) {
  return token.includes("(") ? token : token.replace(/\)+$/, "");
}

/** @param {string} skeleton @returns {string[][]} the words of each simple command */
function segments(skeleton) {
  return skeleton
    .split(/&&|\|\||(?<!>)\||[;\n]/)
    .map((s) =>
      (s.replace(/^[\s({!]+/, "").match(/"[^"]*"|'[^']*'|\S+/g) ?? [])
        .map(withoutClosingParens)
        .filter(Boolean),
    )
    .filter((tokens) => tokens.length > 0);
}

/**
 * Where a command writes, as bare targets: `>`, `>>` and `2>` redirections, the files of `tee`, and the last argument
 * of `cp` and `mv`. A target with shell syntax in it (a variable, a glob, a substitution) is not reported.
 * `>&2` and `2>&1` are not redirections to a file.
 * @param {string} command
 * @returns {{ kind: 'redirect' | 'tee' | 'cp' | 'mv', target: string }[]}
 */
export function writeTargets(command) {
  const skeleton = shellSkeleton(command);
  /** @type {{ kind: 'redirect' | 'tee' | 'cp' | 'mv', target: string }[]} */
  const found = [];
  const target = "(\"[^\"\\n]+\"|'[^'\\n]+'|[^\\s\"'<>|;&)]+)";
  // Redirections are read on a skeleton that keeps only their own quoted targets: a quoted `cp` source such as
  // "a > brief.md" must not read as a redirection. Every `>` left in the skeleton is a redirection, attached to a word
  // (`echo x>file`) or not, except after the characters in NOT_A_REDIRECT_BEFORE. `>&2` and `2>&1` have no target
  // the pattern accepts, and `>|` (noclobber override) is read like `>`.
  for (const m of shellSkeleton(command, "redirect").matchAll(
    new RegExp(`(?<![${NOT_A_REDIRECT_BEFORE}])>>?\\|?\\s*${target}`, "g"),
  )) {
    const word = unquote(m[1]);
    if (word) found.push({ kind: "redirect", target: word });
  }
  for (const tokens of segments(skeleton)) {
    const [name, ...args] = tokens;
    if (name === "tee") {
      for (const arg of args) {
        if (/^\d*[<>]/.test(arg)) break;
        const word = arg.startsWith("-") ? null : unquote(arg);
        if (word) found.push({ kind: "tee", target: word });
      }
    } else if ((name === "cp" || name === "mv") && tokens.length >= 3) {
      const word = unquote(tokens[tokens.length - 1]);
      if (word && !word.startsWith("-")) found.push({ kind: name, target: word });
    }
  }
  return found;
}

/**
 * The first argument of every `mv` command, the file that moves away. Option words are skipped.
 * @param {string} command
 * @returns {string[]}
 */
export function moveSources(command) {
  const found = [];
  for (const tokens of segments(shellSkeleton(command))) {
    if (tokens[0] !== "mv") continue;
    const args = tokens.slice(1).filter((a) => !a.startsWith("-"));
    const word = args.length >= 2 ? unquote(args[0]) : null;
    if (word) found.push(word);
  }
  return found;
}

/**
 * The arguments of every in-place edit, `sed -i` / `sed --in-place` and `perl -i` / `perl -pi`: the script and the
 * files alike, because the caller decides which of them is a file it cares about.
 * @param {string} command
 * @returns {string[]}
 */
export function sedInPlaceArguments(command) {
  const found = [];
  for (const tokens of segments(shellSkeleton(command))) {
    if (tokens[0] !== "sed" && tokens[0] !== "perl") continue;
    const args = tokens.slice(1);
    const flag = tokens[0] === "perl" ? /^-[^-\sMm]*i/ : /^-[^-\s]*i/;
    if (!args.some((a) => flag.test(a) || (tokens[0] === "sed" && /^--in-place(=|$)/.test(a))))
      continue;
    for (const arg of args) {
      const word = arg.startsWith("-") ? null : unquote(arg);
      if (word) found.push(word);
    }
  }
  return found;
}
