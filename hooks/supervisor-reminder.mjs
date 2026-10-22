#!/usr/bin/env node
/**
 * UserPromptSubmit hook: on the first plain prompt of a session, remind the model to route the task through the
 * `supervisor` skill before doing the work itself.
 *
 * A project's CLAUDE.md can say so, but a session can still run every step on the session model with no subagent and
 * no skill call. A line next to the user's own request is harder to skip than one in a long file read at startup.
 * What a UserPromptSubmit hook prints on stdout with exit 0 is added to the model's context. Once per session, marked
 * by a file under the OS temp directory; a slash command is a route the user already chose, so it is left alone and
 * does not use up the reminder. Never fails the prompt.
 */
import { mkdirSync, writeFileSync } from "node:fs";
import os from "node:os";
import path from "node:path";
import process from "node:process";
import { isMain, readHookInput } from "./lib.mjs";

export const REMINDER =
  'Before a task with more than one step, call the Skill tool with "supervisor" ("agent-workflow:supervisor" when it came with the plugin): ' +
  "lookups go to `scout`, a scoped ticket to `implementer`, tests to `tester`, and every diff to `reviewer` before a pull request. " +
  "A one-step ask (one file and a handful of lines, a single lookup, an answer you already hold) skips it.";

/**
 * Where a session's "already reminded" marker lives.
 * @param {string} sessionId
 */
export function markerPath(sessionId) {
  return path.join(os.tmpdir(), "claude-hooks", `${String(sessionId).replace(/[^A-Za-z0-9._-]/g, "_")}.supervisor`);
}

/** @param {Record<string, any>} input the hook event @returns {string} the context line, or '' */
export function remind(input) {
  if (typeof input.session_id !== "string" || !input.session_id) return "";
  // A missing or blank prompt is malformed input: stay silent and keep the reminder for the first real one.
  if (typeof input.prompt !== "string" || !input.prompt.trim()) return "";
  if (input.prompt.trimStart().startsWith("/")) return "";
  const marker = markerPath(input.session_id);
  mkdirSync(path.dirname(marker), { recursive: true });
  try {
    writeFileSync(marker, "", { flag: "wx" }); // fails when the marker exists: this session was reminded already
  } catch {
    return "";
  }
  return REMINDER;
}

if (isMain(import.meta.url)) {
  try {
    const out = remind(readHookInput());
    if (out) process.stdout.write(`${out}\n`);
  } catch {
    // A reminder is a courtesy; never fail the prompt.
  }
}
