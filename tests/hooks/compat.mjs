/**
 * A thin layer over `node:test` and `node:assert/strict` that gives the suites the `describe`/`it`/`expect` shape
 * they were first written in, so the repository needs no test dependency. `it.each` and `it.skipIf` are the two
 * helpers the suites use beyond `node:test` itself; `expect` covers the matchers they call.
 */
import assert from "node:assert/strict";
import { afterEach, beforeEach, describe, it as nodeIt } from "node:test";
import util from "node:util";

export { afterEach, beforeEach, describe };

const fmt = (name, row) => {
  const args = Array.isArray(row) ? row : [row];
  let i = 0;
  const out = name.replace(/%s/g, () => String(args[i++]));
  return out;
};

export const it = Object.assign((name, fn) => nodeIt(name, fn), {
  each: (table) => (name, fn) => {
    for (const row of table) nodeIt(fmt(name, row), () => fn(...(Array.isArray(row) ? row : [row])));
  },
  skipIf: (cond) => (name, fn) => nodeIt(name, { skip: Boolean(cond) }, fn),
});

class Any {
  constructor(type) {
    this.type = type;
  }
  test(v) {
    if (this.type === String) return typeof v === "string";
    if (this.type === Number) return typeof v === "number";
    return v instanceof this.type;
  }
}

/** Deep equality in which an `expect.any(T)` matches any T; with `partial`, the actual may carry extra keys. */
function same(actual, expected, partial) {
  if (expected instanceof Any) return expected.test(actual);
  if (expected === null || typeof expected !== "object")
    return Object.is(actual, expected) || actual === expected;
  if (actual === null || typeof actual !== "object") return false;
  if (Array.isArray(expected)) {
    return (
      Array.isArray(actual) &&
      actual.length === expected.length &&
      expected.every((e, i) => same(actual[i], e, partial))
    );
  }
  const keys = Object.keys(expected);
  if (!partial && Object.keys(actual).length !== keys.length) return false;
  return keys.every((k) => k in actual && same(actual[k], expected[k], partial));
}

function matchers(actual, negate) {
  const check = (ok, message) => {
    if (negate ? ok : !ok) assert.fail(`${negate ? "not: " : ""}${message}`);
  };
  const show = (v) => util.inspect(v, { depth: 6, breakLength: 100 });
  return {
    toBe: (e) => check(Object.is(actual, e), `expected ${show(actual)} to be ${show(e)}`),
    toEqual: (e) => check(same(actual, e, false), `expected ${show(actual)} to equal ${show(e)}`),
    toMatchObject: (e) =>
      check(same(actual, e, true), `expected ${show(actual)} to match ${show(e)}`),
    toContain: (e) =>
      check(
        typeof actual === "string" ? actual.includes(e) : [...actual].includes(e),
        `expected ${show(actual)} to contain ${show(e)}`,
      ),
    toMatch: (re) => check(re.test(actual), `expected ${show(actual)} to match ${re}`),
    toHaveLength: (n) => check(actual.length === n, `expected length ${actual.length} to be ${n}`),
    toBeNull: () => check(actual === null, `expected ${show(actual)} to be null`),
    toBeDefined: () => check(actual !== undefined, "expected a defined value"),
    toBeUndefined: () => check(actual === undefined, `expected ${show(actual)} to be undefined`),
    toBeGreaterThan: (n) => check(actual > n, `expected ${show(actual)} > ${n}`),
    toBeLessThan: (n) => check(actual < n, `expected ${show(actual)} < ${n}`),
  };
}

export const expect = Object.assign(
  (actual) => ({ ...matchers(actual, false), not: matchers(actual, true) }),
  { any: (type) => new Any(type) },
);
