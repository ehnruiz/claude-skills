import { mkdtempSync } from "node:fs";
import { tmpdir } from "node:os";
import path from "node:path";
import { describe, expect, it } from "./compat.mjs";
import { runHook } from "./hookRun.mjs";

const hook = path.resolve(import.meta.dirname, "../../hooks/supervisor-reminder.mjs");

/** A throwaway project root and temp folder, so one test's marker never reaches another. */
function setup() {
  const root = mkdtempSync(path.join(tmpdir(), "claude-sup-root-"));
  const tmp = mkdtempSync(path.join(tmpdir(), "claude-sup-tmp-"));
  return { root, env: { TMPDIR: tmp, TMP: tmp, TEMP: tmp } };
}

const prompt = (s, text, sid = "s1") =>
  runHook(hook, { hook_event_name: "UserPromptSubmit", session_id: sid, cwd: s.root, prompt: text }, { root: s.root, env: s.env });

describe("supervisor-reminder hook", () => {
  it("points the first prompt of a session at the supervisor skill", async () => {
    const s = setup();
    const { stdout, code } = await prompt(s, "Fix the flaky upload test");
    expect(code).toBe(0);
    expect(stdout).toContain('"supervisor"');
    expect(stdout).toContain("scout");
    expect(stdout).toContain("reviewer");
    expect(stdout.length).toBeLessThan(600);
  });

  it("stays silent on later prompts of the same session", async () => {
    const s = setup();
    await prompt(s, "first");
    const { stdout, code } = await prompt(s, "second");
    expect(code).toBe(0);
    expect(stdout).toBe("");
  });

  it("reminds each new session once", async () => {
    const s = setup();
    await prompt(s, "first", "s1");
    const { stdout } = await prompt(s, "first", "s2");
    expect(stdout).toContain('"supervisor"');
  });

  it("leaves a slash command alone and still reminds the next plain prompt", async () => {
    const s = setup();
    const slash = await prompt(s, "/code-review high");
    expect(slash.stdout).toBe("");
    const plain = await prompt(s, "Now fix what it found");
    expect(plain.stdout).toContain('"supervisor"');
  });

  it("keeps the reminder for the first real prompt after a missing, blank or non-string one", async () => {
    const s = setup();
    for (const bad of [{}, { prompt: "" }, { prompt: "   \n" }, { prompt: 42 }]) {
      const { stdout, code } = await runHook(
        hook,
        { hook_event_name: "UserPromptSubmit", session_id: "s1", cwd: s.root, ...bad },
        { root: s.root, env: s.env },
      );
      expect(code).toBe(0);
      expect(stdout).toBe("");
    }
    const { stdout } = await prompt(s, "Now the real request");
    expect(stdout).toContain('"supervisor"');
  });

  it("never fails the prompt on empty or malformed input", async () => {
    const s = setup();
    for (const bad of ["", "{not json", "[]", '{"prompt":"no session id"}']) {
      const { stdout, code } = await runHook(hook, bad, { root: s.root, env: s.env });
      expect(code).toBe(0);
      expect(stdout).toBe("");
    }
  });
});
