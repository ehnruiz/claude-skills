#!/usr/bin/env node
/**
 * SessionStart hook, cloud sessions only: install the Chromium build the project's Playwright expects, so its
 * Playwright tests run.
 *
 * A Claude Code cloud container comes with an older Chromium under PLAYWRIGHT_BROWSERS_PATH, and its network policy
 * blocks cdn.playwright.dev, so `npx playwright install` fails. Playwright's Chromium is Chrome for Testing, which
 * Google serves from storage.googleapis.com, so this fetches the same build from there. The revision and version come
 * from `playwright-core/browsers.json`, so a Playwright upgrade needs no edit here.
 *
 * When `playwright-core/browsers.json` is missing and the project has a `package-lock.json`, `npm ci` runs first,
 * whether or not `node_modules` exists, which also makes lint and tests work from the session's first command. With
 * another lockfile (pnpm, yarn, bun) no installer is guessed: the session is told which lockfile was found and that the
 * dependencies must be installed with that tool first.
 *
 * Only playwright-core 1.57.0 and later are served. Chrome for Testing's zips unpack into `chrome-linux64/` (and
 * `chrome-headless-shell-linux64/`), the folders those versions launch (`lib/server/registry/index.js`, linux-x64);
 * 1.56.x launches `chrome-linux/`, so a build written for it would be marked complete and never run. Below 1.57.0, or
 * with a version that cannot be read, nothing is installed and the session is told so.
 *
 * The hook acts only when package.json lists `playwright`, `@playwright/test` or `playwright-core`, or when
 * `node_modules/playwright-core/browsers.json` already exists. A Playwright that arrives only through another
 * package, with nothing installed yet, is out of scope: the hook cannot see it before `npm ci`, and does not run
 * `npm ci` to look.
 *
 * Registered with `async: true`, so the session starts while it works. Claude Code hands the `additionalContext` of its
 * JSON line to the model on the next turn: which builds it installed, or that the Playwright tests cannot run. Silent
 * when there was nothing to do, outside a cloud session (`CLAUDE_CODE_REMOTE` is not `true`) and in a project that does
 * not use Playwright, which is left untouched. Never fails the start.
 */
import { execFileSync } from "node:child_process";
import {
  existsSync,
  mkdirSync,
  mkdtempSync,
  readFileSync,
  renameSync,
  rmSync,
  writeFileSync,
} from "node:fs";
import os from "node:os";
import path from "node:path";
import process from "node:process";
import { isMain, projectDir, readHookInput } from "./lib.mjs";

const CFT_BASE = "https://storage.googleapis.com/chrome-for-testing-public";
/**
 * Everything the hook does, `npm ci` included, stays inside this. Claude Code enforces no timeout on an async hook, so
 * without it a hung download or `npm ci` would keep running, and never report, for the rest of the session.
 */
const BUDGET_MS = 540_000;
/** The packages whose presence in package.json means the project runs Playwright. */
const PLAYWRIGHT_PACKAGES = ["playwright", "@playwright/test", "playwright-core"];
/** The first playwright-core that launches `chrome-linux64/` (and `chrome-headless-shell-linux64/`) on linux-x64. */
const MIN_PLAYWRIGHT = [1, 57, 0];
/** Lockfiles of the installers this hook does not run, with the name of each installer. */
const OTHER_LOCKFILES = new Map([
  ["pnpm-lock.yaml", "pnpm"],
  ["yarn.lock", "yarn"],
  ["bun.lockb", "bun"],
  ["bun.lock", "bun"],
]);

/** Playwright's folder name and the Chrome for Testing zip for each Chromium build, keyed by its browsers.json name. */
const BUILDS = new Map([
  ["chromium", { prefix: "chromium", zip: "chrome-linux64" }],
  [
    "chromium-headless-shell",
    { prefix: "chromium_headless_shell", zip: "chrome-headless-shell-linux64" },
  ],
]);

/**
 * The Chromium builds `browsersJson` names that are not yet installed under `browsersPath`. A revision or version
 * that is not plain digits and dots is skipped, since both become part of a path the hook may delete.
 * @param {{ browsers?: { name: string, revision: string, browserVersion?: string }[] }} browsersJson
 * @param {string} browsersPath
 * @param {(file: string) => boolean} exists
 * @param {string} base the Chrome for Testing mirror, without a trailing slash
 * @returns {{ dir: string, zip: string, url: string }[]}
 */
export function missingBrowsers(browsersJson, browsersPath, exists, base) {
  const missing = [];
  for (const b of browsersJson.browsers ?? []) {
    const build = BUILDS.get(b.name);
    if (!build || !/^\d+$/.test(b.revision) || !/^\d+(\.\d+)+$/.test(b.browserVersion ?? ""))
      continue;
    const dir = path.join(browsersPath, `${build.prefix}-${b.revision}`);
    if (exists(path.join(dir, "INSTALLATION_COMPLETE"))) continue;
    missing.push({ dir, zip: build.zip, url: `${base}/${b.browserVersion}/linux64/${build.zip}.zip` });
  }
  return missing;
}

/**
 * Where Playwright looks for browsers, by its own rule: `PLAYWRIGHT_BROWSERS_PATH`, else
 * `$XDG_CACHE_HOME/ms-playwright` (`~/.cache` by default). Undefined for `0` (inside node_modules) or a relative
 * path, which Playwright resolves against a directory this hook does not know.
 * @param {Record<string, string | undefined>} env
 * @returns {string | undefined}
 */
export function browsersPathFor(env) {
  const set = env.PLAYWRIGHT_BROWSERS_PATH;
  if (set) return set !== "0" && path.isAbsolute(set) ? set : undefined;
  return path.join(env.XDG_CACHE_HOME || path.join(os.homedir(), ".cache"), "ms-playwright");
}

/**
 * Whether the project's package.json lists Playwright in its dependencies or devDependencies. An absent or unreadable
 * package.json says no.
 * @param {string} root
 */
function declaresPlaywright(root) {
  try {
    const pkg = JSON.parse(readFileSync(path.join(root, "package.json"), "utf8"));
    return PLAYWRIGHT_PACKAGES.some(
      (name) => pkg?.dependencies?.[name] !== undefined || pkg?.devDependencies?.[name] !== undefined,
    );
  } catch {
    return false;
  }
}

/**
 * The version in `node_modules/playwright-core/package.json`, or undefined when it cannot be read.
 * @param {string} root
 * @returns {string | undefined}
 */
function playwrightVersion(root) {
  try {
    const v = JSON.parse(readFileSync(path.join(root, "node_modules/playwright-core/package.json"), "utf8"))?.version;
    return typeof v === "string" ? v : undefined;
  } catch {
    return undefined;
  }
}

/**
 * Whether a playwright-core version launches the folder layout this hook writes. Only the leading `x.y.z` counts, so a
 * prerelease of 1.57.0 passes; anything that does not start with three numbers does not.
 * @param {string | undefined} version
 */
export function supportsLayout(version) {
  const m = /^(\d+)\.(\d+)\.(\d+)/.exec(version ?? "");
  if (!m) return false;
  const parts = [Number(m[1]), Number(m[2]), Number(m[3])];
  for (let i = 0; i < 3; i++) if (parts[i] !== MIN_PLAYWRIGHT[i]) return parts[i] > MIN_PLAYWRIGHT[i];
  return true;
}

/** @param {number} deadline */
const left = (deadline) => deadline - Date.now();

/**
 * Move an unpacked build, already marked complete, into place. A rename is one step, so the folder appears whole or
 * not at all, and another run sharing the browsers folder never sees half of it. When the rename finds the folder
 * already there, it counts as installed only if that folder is marked complete (another run published first); an
 * unmarked folder is someone else's and is refused. The folder is checked before the rename because on Linux a rename
 * onto an existing empty directory replaces it; a folder that appears between the check and the rename is the one gap
 * left, and only an empty one is lost to it.
 * @param {string} staging
 * @param {string} dir
 * @returns {"installed" | "blocked"}
 */
function publish(staging, dir) {
  writeFileSync(path.join(staging, "INSTALLATION_COMPLETE"), "");
  if (existsSync(dir)) return existsSync(path.join(dir, "INSTALLATION_COMPLETE")) ? "installed" : "blocked";
  try {
    renameSync(staging, dir);
    return "installed";
  } catch {
    return existsSync(path.join(dir, "INSTALLATION_COMPLETE")) ? "installed" : "blocked";
  }
}

/**
 * Download each build and unpack it into a staging folder of this run's own beside its destination, then publish
 * it. A failure removes only the staging folder: an install another run finished, or a folder that was already
 * there, is never deleted or changed. Returns the folders that failed and those an unmarked folder stood in the way
 * of.
 * @param {{ dir: string, zip: string, url: string }[]} builds
 * @param {number} deadline
 * @returns {{ failed: string[], blocked: string[] }}
 */
function install(builds, deadline) {
  const failed = [];
  const blocked = [];
  const tmp = mkdtempSync(path.join(os.tmpdir(), "cloud-browsers-cft-"));
  try {
    for (const b of builds) {
      const zipFile = path.join(tmp, `${b.zip}.zip`);
      let staging;
      try {
        if (left(deadline) < 5000) throw new Error("out of time");
        const seconds = String(Math.floor(left(deadline) / 1000));
        execFileSync("curl", ["-sSfL", "--retry", "1", "--max-time", seconds, "-o", zipFile, b.url], {
          stdio: "ignore",
          timeout: left(deadline),
        });
        mkdirSync(path.dirname(b.dir), { recursive: true });
        staging = mkdtempSync(path.join(path.dirname(b.dir), `.${path.basename(b.dir)}-`));
        execFileSync("unzip", ["-qo", zipFile, "-d", staging], {
          stdio: "ignore",
          timeout: Math.max(1, left(deadline)),
        });
        if (publish(staging, b.dir) === "blocked") blocked.push(path.basename(b.dir));
      } catch {
        failed.push(path.basename(b.dir));
      } finally {
        rmSync(zipFile, { force: true });
        if (staging) rmSync(staging, { recursive: true, force: true });
      }
    }
  } finally {
    rmSync(tmp, { recursive: true, force: true });
  }
  return { failed, blocked };
}

/**
 * Playwright's browsers.json, running `npm ci` first when it is missing and the project has a `package-lock.json`
 * (`node_modules` may exist, as after `npm install --omit=dev`). `npm ci` can fail after unpacking playwright-core (in the prepare script, say), so its failure
 * is returned beside the json. A project with only another lockfile gets no installer: `otherLockfile` names it.
 * @param {string} root
 * @param {number} deadline
 * @returns {{ json: any, ranNpmCi: boolean, npmCiFailed: boolean, otherLockfile?: string }}
 */
function readBrowsersJson(root, deadline) {
  const file = path.join(root, "node_modules/playwright-core/browsers.json");
  let ranNpmCi = false;
  let npmCiFailed = false;
  let otherLockfile;
  if (!existsSync(file)) {
    if (existsSync(path.join(root, "package-lock.json"))) {
      try {
        execFileSync("npm", ["ci", "--no-audit", "--no-fund"], {
          cwd: root,
          stdio: "ignore",
          timeout: left(deadline),
        });
        ranNpmCi = true;
      } catch {
        npmCiFailed = true;
      }
    } else {
      otherLockfile = [...OTHER_LOCKFILES.keys()].find((name) => existsSync(path.join(root, name)));
    }
  }
  try {
    return { json: JSON.parse(readFileSync(file, "utf8")), ranNpmCi, npmCiFailed, otherLockfile };
  } catch {
    return { json: undefined, ranNpmCi, npmCiFailed, otherLockfile };
  }
}

/**
 * The hook's one JSON line: an async hook's plain-text stdout is not promised to reach the session, `additionalContext`
 * is.
 * @param {string} text
 */
function report(text) {
  console.log(
    JSON.stringify({
      hookSpecificOutput: { hookEventName: "SessionStart", additionalContext: `cloud-browsers: ${text}` },
    }),
  );
}

function main() {
  if (process.env.CLAUDE_CODE_REMOTE !== "true") return;
  const deadline = Date.now() + BUDGET_MS;
  // Claude Code sets CLAUDE_PROJECT_DIR; stdin is read only without it, since no timeout would end a read that hangs.
  const root = process.env.CLAUDE_PROJECT_DIR ? projectDir({}) : projectDir(readHookInput());
  // Decide first whether the project uses Playwright; one that does not is never touched, `npm ci` included.
  if (!declaresPlaywright(root) && !existsSync(path.join(root, "node_modules/playwright-core/browsers.json"))) return;
  const { json: browsersJson, ranNpmCi, npmCiFailed, otherLockfile } = readBrowsersJson(root, deadline);
  // A failed npm ci is always said, and the tests are never called ready while node_modules is incomplete.
  const npmNote = npmCiFailed
    ? "npm ci failed, so node_modules is incomplete (run npm ci to see why); "
    : ranNpmCi
      ? "ran npm ci, so node_modules is ready; "
      : "";
  const ready = npmCiFailed
    ? "the project's Playwright tests can run once npm ci succeeds."
    : "the project's Playwright tests can run now.";
  if (!browsersJson) {
    if (npmCiFailed) report(`${npmNote}no browser was installed.`);
    else if (ranNpmCi)
      report(
        "ran npm ci, but node_modules/playwright-core/browsers.json is still missing (the lockfile may not include Playwright), so no browser was installed.",
      );
    else if (otherLockfile) {
      const manager = OTHER_LOCKFILES.get(otherLockfile);
      report(
        `Playwright is not installed, and this project's lockfile is ${otherLockfile} (${manager}), which this hook does not run, so no browser was installed. The dependencies must be installed with ${manager} first.`,
      );
    } else report("Playwright is not installed and the project has no lockfile to install it from, so no browser was installed.");
    return;
  }
  const browsersPath = browsersPathFor(process.env);
  if (!browsersPath) {
    report("PLAYWRIGHT_BROWSERS_PATH is 0 or relative, so no browser was installed.");
    return;
  }
  const base = process.env.CLOUD_BROWSERS_CFT_BASE || CFT_BASE;
  const missing = missingBrowsers(browsersJson, browsersPath, existsSync, base);
  // Builds already in place need nothing, whatever the version: the version and platform matter only to an install.
  if (!missing.length) {
    if (npmCiFailed) report(`${npmNote}the Chromium builds were already installed; ${ready}`);
    else if (ranNpmCi) report(`${npmNote}the Chromium builds were already installed.`);
    return;
  }
  const version = playwrightVersion(root);
  if (!supportsLayout(version)) {
    report(
      `this Playwright version (${version ?? "unknown: node_modules/playwright-core/package.json could not be read"}) is not supported by the hook, which writes the chrome-linux64 layout that playwright-core ${MIN_PLAYWRIGHT.join(".")} and later launch; no browser was installed.`,
    );
    return;
  }
  // Only Chrome for Testing's linux64 build is fetched.
  if (process.platform !== "linux" || process.arch !== "x64") {
    report(
      `no Chrome for Testing build is fetched for ${process.platform}-${process.arch}; install Playwright's browsers another way.`,
    );
    return;
  }
  const { failed, blocked } = install(missing, deadline);
  const installed = missing
    .map((b) => path.basename(b.dir))
    .filter((name) => !failed.includes(name) && !blocked.includes(name));
  if (!failed.length && !blocked.length) {
    report(`${npmNote}installed ${installed.join(", ")} under ${browsersPath}; ${ready}`);
    return;
  }
  const problems = [];
  if (failed.length) problems.push(`could not install ${failed.join(", ")} from ${base}`);
  if (blocked.length)
    problems.push(
      `${blocked.join(", ")} is already there without INSTALLATION_COMPLETE, so it was left alone (remove it to retry)`,
    );
  const done = installed.length ? `installed ${installed.join(", ")}, but ` : "";
  report(
    `${npmNote}${done}${problems.join(", and ")}; the project's Playwright tests will not run in this session.`,
  );
}

if (isMain(import.meta.url)) {
  try {
    main();
  } catch {
    // A session start is never blocked by a browser install, but the session is told it did not finish.
    report(
      "could not finish installing the Chromium builds (an unexpected error, such as a full or missing temp folder); the project's Playwright tests may not run in this session.",
    );
  }
}
