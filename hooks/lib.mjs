/**
 * Shared helpers for the hooks in this folder.
 *
 * A hook is a plain ES module that Node runs with the hook event on stdin as JSON and answers on stdout. Plain
 * JavaScript rather than TypeScript so a hook starts in the time of a `node` launch, with nothing to compile, on
 * every Edit and Write of a session. The input contract is the Claude Code hooks reference
 * (https://code.claude.com/docs/en/hooks): every event carries `session_id`, `cwd` and `hook_event_name`; tool events
 * add `tool_name` and `tool_input`.
 *
 * A hook must never crash the tool it watches: a malformed or empty stdin reads as an empty event and the hook
 * allows the call.
 */
import { readSync, realpathSync } from "node:fs";
import path from "node:path";
import process from "node:process";
import { fileURLToPath } from "node:url";

/**
 * Everything on stdin, read to its end. Stdin can be a non-blocking pipe (seen on Linux when a hook is spawned by a
 * Node test runner), and then a read that finds the pipe momentarily empty throws EAGAIN instead of waiting. A
 * `readFileSync(0)` gave up on that error, so the hook saw an empty event and allowed the call. This reads in a
 * loop, waits a few milliseconds on EAGAIN, and stops at end of file or at the deadline, whichever comes first.
 * @param {(buf: Buffer) => number} read bytes read into `buf`, 0 at end of file; the default reads fd 0
 * @param {number} waitMs how long to wait after an EAGAIN before reading again
 * @param {number} deadlineMs how long to keep retrying in all, after which what was read so far is returned
 * @returns {string}
 */
export function readStdin(read = (buf) => readSync(0, buf, 0, buf.length, null), waitMs = 5, deadlineMs = 10_000) {
  const chunks = [];
  const buf = Buffer.alloc(64 * 1024);
  const sleeper = new Int32Array(new SharedArrayBuffer(4));
  const deadline = Date.now() + deadlineMs;
  for (;;) {
    let n;
    try {
      n = read(buf);
    } catch (e) {
      if (e && e.code === "EAGAIN") {
        if (Date.now() >= deadline) break;
        Atomics.wait(sleeper, 0, 0, waitMs);
        continue;
      }
      if (e && e.code === "EOF") break;
      throw e;
    }
    if (n === 0) break;
    chunks.push(Buffer.from(buf.subarray(0, n)));
  }
  return Buffer.concat(chunks).toString("utf8");
}

/** @returns {Record<string, any>} the hook event, or `{}` when stdin is empty or not JSON. */
export function readHookInput() {
  let raw;
  try {
    raw = readStdin();
  } catch {
    return {};
  }
  if (!raw.trim()) return {};
  try {
    const parsed = JSON.parse(raw);
    return parsed && typeof parsed === "object" ? parsed : {};
  } catch {
    return {};
  }
}

/**
 * The project root the session started in. `CLAUDE_PROJECT_DIR` is set by Claude Code for every hook process; the
 * event's `cwd` and the process cwd are the fallbacks for a hook run by hand or from a test.
 * @param {Record<string, any>} input
 */
export function projectDir(input) {
  return path.resolve(process.env.CLAUDE_PROJECT_DIR || input.cwd || process.cwd());
}

/**
 * The files a file tool is about to touch, absolute: `file_path` for Edit, Write and MultiEdit (whose `edits` carry
 * no path of their own), `notebook_path` for NotebookEdit.
 * @param {Record<string, any>} input
 * @param {string} root
 * @returns {string[]}
 */
export function targetPaths(input, root) {
  const tool = input.tool_input ?? {};
  const seen = new Set();
  for (const p of [tool.file_path, tool.notebook_path])
    if (typeof p === "string" && p.trim()) seen.add(path.resolve(root, p));
  return [...seen];
}

/**
 * Whether the module at `metaUrl` is the script Node was started with, so a hook runs its main block only when
 * invoked as a hook and not when a test imports it. Compared on real paths: `/tmp` is a symlink on macOS, and a
 * `file://` URL built by hand from `argv[1]` would also differ from `import.meta.url` on a path with `#` or `%`.
 * @param {string} metaUrl `import.meta.url` of the caller
 */
export function isMain(metaUrl) {
  try {
    return (
      !!process.argv[1] && realpathSync(fileURLToPath(metaUrl)) === realpathSync(process.argv[1])
    );
  } catch {
    return false;
  }
}

/**
 * A file's path relative to the project root, or null when it lies outside the project.
 * @param {string} file absolute
 * @param {string} root absolute
 */
export function relativeToProject(file, root) {
  const rel = path.relative(root, file);
  if (!rel || rel.startsWith("..") || path.isAbsolute(rel)) return null;
  return rel.split(path.sep).join("/");
}

/**
 * Deny the tool call. The structured form, so the reason reaches the model as the permission decision rather than
 * as a hook error.
 * @param {string} reason
 */
export function deny(reason) {
  const out = {
    hookSpecificOutput: {
      hookEventName: "PreToolUse",
      permissionDecision: "deny",
      permissionDecisionReason: reason,
    },
  };
  process.stdout.write(`${JSON.stringify(out)}\n`);
}

/**
 * Let the user decide. Same shape as `deny` with `ask`: the call waits for the user with the reason shown.
 * @param {string} reason
 */
export function ask(reason) {
  const out = {
    hookSpecificOutput: {
      hookEventName: "PreToolUse",
      permissionDecision: "ask",
      permissionDecisionReason: reason,
    },
  };
  process.stdout.write(`${JSON.stringify(out)}\n`);
}
