import { describe, expect, it } from "./compat.mjs";
import { readStdin } from "../../hooks/lib.mjs";

/** A stdin stand-in that answers each call from `script`: a string is bytes to deliver, "EAGAIN" is the error, 0 is end of file. */
function stdinOf(script) {
  const calls = [];
  const read = (buf) => {
    const step = script.shift();
    calls.push(step);
    if (step === "EAGAIN") throw Object.assign(new Error("resource temporarily unavailable"), { code: "EAGAIN" });
    if (step === 0) return 0;
    return buf.write(step);
  };
  return { read, calls };
}

describe("readStdin", () => {
  it("reads to end of file across EAGAIN, keeping every byte in order", () => {
    const stdin = stdinOf(["EAGAIN", '{"tool_name":', "EAGAIN", "EAGAIN", '"Bash"}', 0]);
    expect(readStdin(stdin.read, 1)).toBe('{"tool_name":"Bash"}');
    expect(stdin.calls).toHaveLength(6);
  });

  it("returns what it has once the deadline passes, so a hook answers instead of hanging", () => {
    const script = Array.from({ length: 1000 }, () => "EAGAIN");
    const start = Date.now();
    expect(readStdin(stdinOf(script).read, 1, 20)).toBe("");
    expect(Date.now() - start).toBeLessThan(5000);
    expect(script.length).toBeGreaterThan(0);
  });

  it("treats an EOF error like end of file", () => {
    const read = (buf) => {
      if (read.done) throw Object.assign(new Error("EOF"), { code: "EOF" });
      read.done = true;
      return buf.write("x");
    };
    expect(readStdin(read, 1)).toBe("x");
  });

  it("rethrows any other error", () => {
    const read = () => {
      throw Object.assign(new Error("bad fd"), { code: "EBADF" });
    };
    let thrown;
    try {
      readStdin(read, 1);
    } catch (e) {
      thrown = e;
    }
    expect(thrown?.code).toBe("EBADF");
  });
});
