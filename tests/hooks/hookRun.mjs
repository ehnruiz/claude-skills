import { execFile } from "node:child_process";

/**
 * Runs a hook script as Claude Code does: the event as JSON on stdin, the project root in `CLAUDE_PROJECT_DIR`. The
 * event is written to the child's stdin by hand and the stream closed: the asynchronous `execFile` has no `input`
 * option, and a hook reading stdin to its end would otherwise wait until the test timed out.
 * @param {string} hook absolute path of the script
 * @param {unknown} event an object (sent as JSON) or a raw string
 * @param {{ root: string, env?: Record<string, string>, cwd?: string }} opts
 * @returns {Promise<{ stdout: string, stderr: string, code: number }>}
 */
export function runHook(hook, event, opts) {
  return new Promise((resolve) => {
    const child = execFile(
      process.execPath,
      [hook],
      { cwd: opts.cwd, env: { ...process.env, CLAUDE_PROJECT_DIR: opts.root, ...opts.env } },
      (error, stdout, stderr) => {
        resolve({ stdout, stderr, code: error ? (error.code ?? 1) : 0 });
      },
    );
    child.stdin?.end(typeof event === "string" ? event : JSON.stringify(event));
  });
}
