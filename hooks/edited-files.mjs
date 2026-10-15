#!/usr/bin/env node
/**
 * PostToolUse hook on Edit, Write and MultiEdit: remember which project files a session edited, for the stop-check
 * hook to verify when the response ends.
 *
 * One path per line in a per-session file under the OS temp directory. Duplicates are cheap here and removed by
 * stop-check. Prints nothing and never fails the tool.
 */
import { appendFileSync, mkdirSync, rmSync } from "node:fs";
import os from "node:os";
import path from "node:path";
import { isMain, projectDir, readHookInput, relativeToProject, targetPaths } from "./lib.mjs";

/**
 * Where a session's edited paths accumulate.
 * @param {string} sessionId
 */
export function accumulatorPath(sessionId) {
  return path.join(
    os.tmpdir(),
    "claude-hooks",
    `${String(sessionId).replace(/[^A-Za-z0-9._-]/g, "_")}.edited`,
  );
}

/**
 * Set by stop-check when it lets a response end with its checks still failing; an edit clears it, so the next stop
 * checks again and a reply that edits nothing is not blocked over an old failure.
 * @param {string} sessionId
 */
export function releasedPath(sessionId) {
  return `${accumulatorPath(sessionId)}.released`;
}

/** @param {Record<string, any>} input the hook event */
export function record(input) {
  if (typeof input.session_id !== "string" || !input.session_id) return;
  const root = projectDir(input);
  const files = targetPaths(input, root).filter((file) => relativeToProject(file, root));
  if (files.length === 0) return;
  const target = accumulatorPath(input.session_id);
  mkdirSync(path.dirname(target), { recursive: true });
  appendFileSync(target, files.map((f) => `${f}\n`).join(""));
  rmSync(releasedPath(input.session_id), { force: true });
}

if (isMain(import.meta.url)) {
  try {
    record(readHookInput());
  } catch {
    // A bookkeeping failure must never fail the edit.
  }
}
