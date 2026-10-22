import { chmodSync, existsSync, mkdirSync, mkdtempSync, readdirSync, readFileSync, writeFileSync } from "node:fs";
import { tmpdir } from "node:os";
import path from "node:path";
import { describe, expect, it } from "./compat.mjs";
import { runHook } from "./hookRun.mjs";
import { browsersPathFor, missingBrowsers } from "../../hooks/cloud-browsers.mjs";
// Offline: every download is a `file://` mirror built here, and `npm` is a stand-in script on PATH, so the suite needs
// nothing but Node 20 or newer and, for the install tests on linux-x64, the `curl`, `zip`-format-reading `unzip` and
// `sh` that ubuntu-latest ships. The zips are built in this file, so `zip` itself is not needed.
const hook = path.resolve(import.meta.dirname, "../../hooks/cloud-browsers.mjs");

const BROWSERS = {
  browsers: [
    { name: "chromium", revision: "1248", browserVersion: "156.0.8078.4" },
    { name: "chromium-headless-shell", revision: "1248", browserVersion: "156.0.8078.4" },
    { name: "firefox", revision: "1500", browserVersion: "150.0" },
  ],
};

const CRC_TABLE = Array.from({ length: 256 }, (_, n) => {
  let c = n;
  for (let k = 0; k < 8; k++) c = c & 1 ? 0xedb88320 ^ (c >>> 1) : c >>> 1;
  return c >>> 0;
});

/** CRC-32 of a buffer; `zlib.crc32` only exists from Node 20.15, and the hooks support Node 20. */
function crc32(buf) {
  let c = 0xffffffff;
  for (const byte of buf) c = CRC_TABLE[(c ^ byte) & 0xff] ^ (c >>> 8);
  return (c ^ 0xffffffff) >>> 0;
}

/**
 * A zip of stored (uncompressed) entries, enough for `unzip`; built here so no fixture binary is checked in. With
 * `badCrc`, `unzip` writes the files and then fails, as a download cut short would.
 * @param {Record<string, string>} files
 * @param {boolean} [badCrc]
 * @returns {Buffer}
 */
function zipOf(files, badCrc = false) {
  const local = [];
  const central = [];
  let offset = 0;
  for (const [name, text] of Object.entries(files)) {
    const data = Buffer.from(text);
    const nameBuf = Buffer.from(name);
    const crc = (crc32(data) ^ (badCrc ? 1 : 0)) >>> 0;
    const head = Buffer.alloc(30);
    head.writeUInt32LE(0x04034b50, 0);
    head.writeUInt16LE(10, 4);
    head.writeUInt32LE(crc, 14);
    head.writeUInt32LE(data.length, 18);
    head.writeUInt32LE(data.length, 22);
    head.writeUInt16LE(nameBuf.length, 26);
    const dir = Buffer.alloc(46);
    dir.writeUInt32LE(0x02014b50, 0);
    dir.writeUInt16LE(20, 4);
    dir.writeUInt16LE(10, 6);
    dir.writeUInt32LE(crc, 16);
    dir.writeUInt32LE(data.length, 20);
    dir.writeUInt32LE(data.length, 24);
    dir.writeUInt16LE(nameBuf.length, 28);
    dir.writeUInt32LE(offset, 42);
    local.push(head, nameBuf, data);
    central.push(dir, nameBuf);
    offset += head.length + nameBuf.length + data.length;
  }
  const centralBuf = Buffer.concat(central);
  const end = Buffer.alloc(22);
  end.writeUInt32LE(0x06054b50, 0);
  end.writeUInt16LE(Object.keys(files).length, 8);
  end.writeUInt16LE(Object.keys(files).length, 10);
  end.writeUInt32LE(centralBuf.length, 12);
  end.writeUInt32LE(offset, 16);
  return Buffer.concat([...local, centralBuf, end]);
}

/**
 * A `file://` mirror laid out like Chrome for Testing's, holding the given zips for version 156.0.8078.4.
 * @param {Record<string, Buffer>} zips
 */
function mirror(zips) {
  const dir = mkdtempSync(path.join(tmpdir(), "cloud-browsers-cft-"));
  mkdirSync(path.join(dir, "156.0.8078.4/linux64"), { recursive: true });
  for (const [name, bytes] of Object.entries(zips))
    writeFileSync(path.join(dir, "156.0.8078.4/linux64", `${name}.zip`), bytes);
  return `file://${dir}`;
}

/**
 * A project root with Playwright's browsers.json and an empty browser folder; a closed port stands in for the mirror.
 * `browsersJson: false` leaves playwright-core out of node_modules; `playwright: false` leaves Playwright out of
 * package.json too. `version` is the playwright-core version it reports (1.58.0 by default, which uses the layout the
 * hook writes); `version: false` leaves out the package.json that holds it.
 * @param {{ installed?: string[], browsersJson?: boolean, playwright?: boolean, version?: string | false }} [opts]
 */
function setup(opts = {}) {
  const root = mkdtempSync(path.join(tmpdir(), "cloud-browsers-root-"));
  const browsersPath = mkdtempSync(path.join(tmpdir(), "cloud-browsers-pw-"));
  if (opts.playwright !== false) {
    writeFileSync(
      path.join(root, "package.json"),
      JSON.stringify({ name: "demo", devDependencies: { "@playwright/test": "^1.50.0" } }),
    );
  }
  if (opts.browsersJson !== false) {
    mkdirSync(path.join(root, "node_modules/playwright-core"), { recursive: true });
    writeFileSync(
      path.join(root, "node_modules/playwright-core/browsers.json"),
      JSON.stringify(BROWSERS),
    );
    if (opts.version !== false)
      writeFileSync(
        path.join(root, "node_modules/playwright-core/package.json"),
        JSON.stringify({ name: "playwright-core", version: opts.version ?? "1.58.0" }),
      );
  }
  for (const dir of opts.installed ?? []) {
    mkdirSync(path.join(browsersPath, dir), { recursive: true });
    writeFileSync(path.join(browsersPath, dir, "INSTALLATION_COMPLETE"), "");
  }
  return { root, browsersPath };
}

const start = (s, remote, extra = {}) =>
  runHook(
    hook,
    { hook_event_name: "SessionStart", session_id: "s1", source: "startup", cwd: s.root },
    {
      root: s.root,
      env: {
        CLAUDE_CODE_REMOTE: remote ?? "",
        PLAYWRIGHT_BROWSERS_PATH: s.browsersPath,
        CLOUD_BROWSERS_CFT_BASE: "http://127.0.0.1:9/chrome-for-testing-public",
        ...extra,
      },
    },
  );

/**
 * A directory with a stand-in `npm` first on PATH. It appends its arguments to `npm-calls` in the directory it runs in,
 * so a test can tell whether it ran; `body` is the rest of the script (`$1` is the npm subcommand).
 * @param {string} body
 * @returns {string} the PATH to give the hook
 */
function fakeNpm(body) {
  const bin = mkdtempSync(path.join(tmpdir(), "cloud-browsers-bin-"));
  writeFileSync(path.join(bin, "npm"), `#!/bin/sh\necho "$@" >> "${bin}/npm-calls"\n${body}\n`);
  chmodSync(path.join(bin, "npm"), 0o755);
  return `${bin}${path.delimiter}${process.env.PATH ?? ""}`;
}
const npmCalls = (envPath) => path.join(envPath.split(path.delimiter)[0], "npm-calls");
/** Shell that "installs" playwright-core 1.58.0 by writing its browsers.json and package.json, as `npm ci` would. */
const WRITE_JSON = `mkdir -p node_modules/playwright-core && printf '%s' '${JSON.stringify(BROWSERS).replace(/'/g, "'\\''")}' > node_modules/playwright-core/browsers.json && printf '%s' '{"name":"playwright-core","version":"1.58.0"}' > node_modules/playwright-core/package.json`;

/**
 * The text an async SessionStart hook hands the session: `additionalContext` of its one JSON line, or "" when silent.
 * @param {string} stdout
 */
function contextOf(stdout) {
  if (!stdout.trim()) return "";
  const lines = stdout.trim().split("\n");
  expect(lines).toHaveLength(1);
  const out = JSON.parse(lines[0]);
  expect(out.hookSpecificOutput?.hookEventName).toBe("SessionStart");
  return out.hookSpecificOutput?.additionalContext ?? "";
}

describe("missingBrowsers", () => {
  it("names the Chrome for Testing download for each Chromium build Playwright expects", () => {
    expect(missingBrowsers(BROWSERS, "/pw", () => false, "https://mirror")).toEqual([
      {
        dir: "/pw/chromium-1248",
        zip: "chrome-linux64",
        url: "https://mirror/156.0.8078.4/linux64/chrome-linux64.zip",
      },
      {
        dir: "/pw/chromium_headless_shell-1248",
        zip: "chrome-headless-shell-linux64",
        url: "https://mirror/156.0.8078.4/linux64/chrome-headless-shell-linux64.zip",
      },
    ]);
  });

  it("NEGATIVE: leaves out a build whose folder is already complete, and every non-Chromium browser", () => {
    const installed = (file) => file === "/pw/chromium-1248/INSTALLATION_COMPLETE";
    expect(
      missingBrowsers(BROWSERS, "/pw", installed, "https://mirror").map((b) => b.dir),
    ).toEqual(["/pw/chromium_headless_shell-1248"]);
  });

  it("NEGATIVE: skips a revision or version that is not digits and dots", () => {
    const odd = {
      browsers: [
        { name: "chromium", revision: "../1248", browserVersion: "156.0.8078.4" },
        { name: "chromium-headless-shell", revision: "1248", browserVersion: "156/../x" },
      ],
    };
    expect(missingBrowsers(odd, "/pw", () => false, "https://mirror")).toEqual([]);
  });
});

describe("browsersPathFor", () => {
  it("follows Playwright: the variable when absolute, else the XDG cache, else ~/.cache", () => {
    expect(browsersPathFor({ PLAYWRIGHT_BROWSERS_PATH: "/opt/pw-browsers" })).toBe("/opt/pw-browsers");
    expect(browsersPathFor({ XDG_CACHE_HOME: "/xdg" })).toBe(path.join("/xdg", "ms-playwright"));
    expect(browsersPathFor({})).toMatch(/\.cache[\\/]ms-playwright$/);
  });

  it("NEGATIVE: gives no folder for 0 or a relative path, which Playwright resolves elsewhere", () => {
    expect(browsersPathFor({ PLAYWRIGHT_BROWSERS_PATH: "0" })).toBeUndefined();
    expect(browsersPathFor({ PLAYWRIGHT_BROWSERS_PATH: "browsers" })).toBeUndefined();
  });
});

const linuxX64 = process.platform === "linux" && process.arch === "x64";

describe("cloud-browsers hook, on any platform", () => {
  it("NEGATIVE: does nothing outside a cloud session", async () => {
    const s = setup();
    const { stdout, code } = await start(s, undefined);
    expect(code).toBe(0);
    expect(stdout).toBe("");
    expect(readdirSync(s.browsersPath)).toEqual([]);
  });

  it("NEGATIVE: is silent in a project that does not use Playwright", async () => {
    const s = setup({ browsersJson: false, playwright: false });
    writeFileSync(path.join(s.root, "package.json"), JSON.stringify({ dependencies: { react: "^19.0.0" } }));
    const { stdout, code } = await start(s, "true");
    expect(code).toBe(0);
    expect(stdout).toBe("");
    expect(readdirSync(s.browsersPath)).toEqual([]);
  });

  it("NEGATIVE: never touches a project that does not use Playwright, even with a package-lock.json and no node_modules", async () => {
    const s = setup({ browsersJson: false, playwright: false });
    writeFileSync(path.join(s.root, "package.json"), JSON.stringify({ dependencies: { react: "^19.0.0" } }));
    writeFileSync(path.join(s.root, "package-lock.json"), "{}");
    const PATH = fakeNpm(WRITE_JSON);
    const { stdout, code } = await start(s, "true", { PATH });
    expect(code).toBe(0);
    expect(stdout).toBe("");
    expect(existsSync(npmCalls(PATH))).toBe(false);
    expect(existsSync(path.join(s.root, "node_modules"))).toBe(false);
  });

  it("NEGATIVE: is silent with no package.json at all", async () => {
    const s = setup({ browsersJson: false, playwright: false });
    const { stdout, code } = await start(s, "true");
    expect(code).toBe(0);
    expect(stdout).toBe("");
  });

  it.each([["playwright"], ["@playwright/test"], ["playwright-core"]])(
    "counts %s in dependencies or devDependencies as using Playwright",
    async (name) => {
      const s = setup({ browsersJson: false, playwright: false });
      writeFileSync(path.join(s.root, "package.json"), JSON.stringify({ dependencies: { [name]: "^1.50.0" } }));
      const { stdout, code } = await start(s, "true");
      expect(code).toBe(0);
      expect(contextOf(stdout)).toContain("Playwright is not installed");
    },
  );

  it("NEGATIVE: with a pnpm lockfile it runs no installer and says the dependencies must be installed with pnpm first", async () => {
    const s = setup({ browsersJson: false });
    writeFileSync(path.join(s.root, "pnpm-lock.yaml"), "lockfileVersion: '9.0'\n");
    const PATH = fakeNpm("exit 1");
    const { stdout, code } = await start(s, "true", { PATH });
    expect(code).toBe(0);
    expect(contextOf(stdout)).toContain("pnpm-lock.yaml");
    expect(contextOf(stdout)).toContain("must be installed with pnpm first");
    expect(contextOf(stdout)).not.toContain("npm ci failed");
    expect(contextOf(stdout)).not.toContain("no lockfile");
    expect(existsSync(npmCalls(PATH))).toBe(false);
    expect(existsSync(path.join(s.root, "node_modules"))).toBe(false);
  });

  it.each([
    ["yarn.lock", "yarn"],
    ["bun.lockb", "bun"],
    ["bun.lock", "bun"],
  ])("NEGATIVE: with %s it runs no installer and names the lockfile and its tool", async (lockfile, manager) => {
    const s = setup({ browsersJson: false });
    writeFileSync(path.join(s.root, lockfile), "");
    const PATH = fakeNpm("exit 1");
    const { stdout, code } = await start(s, "true", { PATH });
    expect(code).toBe(0);
    expect(contextOf(stdout)).toContain(lockfile);
    expect(contextOf(stdout)).toContain(`must be installed with ${manager} first`);
    expect(contextOf(stdout)).not.toContain("no lockfile");
    expect(existsSync(npmCalls(PATH))).toBe(false);
  });

  it.each([
    ["pnpm-lock.yaml", "pnpm"],
    ["yarn.lock", "yarn"],
    ["bun.lock", "bun"],
  ])("NEGATIVE: with %s and a node_modules that lacks Playwright it still names the lockfile, not a missing one", async (lockfile, manager) => {
    const s = setup({ browsersJson: false });
    mkdirSync(path.join(s.root, "node_modules"));
    writeFileSync(path.join(s.root, lockfile), "");
    const PATH = fakeNpm("exit 1");
    const { stdout, code } = await start(s, "true", { PATH });
    expect(code).toBe(0);
    expect(contextOf(stdout)).toContain(lockfile);
    expect(contextOf(stdout)).toContain(`must be installed with ${manager} first`);
    expect(contextOf(stdout)).not.toContain("no lockfile");
    expect(existsSync(npmCalls(PATH))).toBe(false);
  });

  it("says there is no lockfile only when there is none, with or without a node_modules", async () => {
    for (const withModules of [false, true]) {
      const s = setup({ browsersJson: false });
      if (withModules) mkdirSync(path.join(s.root, "node_modules"));
      const { stdout, code } = await start(s, "true");
      expect(code).toBe(0);
      expect(contextOf(stdout)).toContain("Playwright is not installed");
      expect(contextOf(stdout)).toContain("no lockfile");
    }
  });

  it("runs npm ci when the project declares Playwright, node_modules exists without it, and package-lock.json exists", async () => {
    const s = setup({ browsersJson: false });
    mkdirSync(path.join(s.root, "node_modules/left-pad"), { recursive: true }); // as after `npm install --omit=dev`
    writeFileSync(path.join(s.root, "package-lock.json"), "{}");
    const PATH = fakeNpm(`[ "$1" = ci ] || exit 1\n${WRITE_JSON}`);
    const { stdout, code } = await start(s, "true", { PATH });
    expect(code).toBe(0);
    expect(readFileSync(npmCalls(PATH), "utf8")).toContain("ci --no-audit --no-fund");
    expect(existsSync(path.join(s.root, "node_modules/playwright-core/browsers.json"))).toBe(true);
    expect(contextOf(stdout)).not.toContain("no lockfile");
    expect(contextOf(stdout)).not.toContain("Playwright is not installed");
  });

  it("NEGATIVE: does not run npm ci when playwright-core's browsers.json is already there", async () => {
    const s = setup();
    writeFileSync(path.join(s.root, "package-lock.json"), "{}");
    const PATH = fakeNpm("exit 1");
    await start(s, "true", { PATH });
    expect(existsSync(npmCalls(PATH))).toBe(false);
  });

  it("does not claim a missing lockfile when npm ci ran and still left Playwright out", async () => {
    const s = setup({ browsersJson: false });
    writeFileSync(path.join(s.root, "package-lock.json"), "{}");
    const PATH = fakeNpm("exit 0");
    const { stdout, code } = await start(s, "true", { PATH });
    expect(code).toBe(0);
    expect(contextOf(stdout)).toContain("ran npm ci");
    expect(contextOf(stdout)).toContain("browsers.json");
    expect(contextOf(stdout)).not.toContain("no lockfile");
    expect(contextOf(stdout)).not.toContain("npm ci failed");
  });

  it("installs nothing and names the version when playwright-core is older than the chrome-linux64 layout (1.56.0)", async () => {
    const s = setup({ version: "1.56.0" });
    const { stdout, code } = await start(s, "true");
    expect(code).toBe(0);
    expect(contextOf(stdout)).toContain("1.56.0");
    expect(contextOf(stdout)).toContain("not supported");
    expect(contextOf(stdout)).not.toContain("can run now");
    expect(readdirSync(s.browsersPath)).toEqual([]);
  });

  it("installs nothing and says the version is not supported when it cannot be read", async () => {
    const s = setup({ version: false });
    const { stdout, code } = await start(s, "true");
    expect(code).toBe(0);
    expect(contextOf(stdout)).toContain("not supported");
    expect(contextOf(stdout)).toContain("could not be read");
    expect(readdirSync(s.browsersPath)).toEqual([]);
  });

  it("stays silent on an older playwright-core whose builds are already installed", async () => {
    const s = setup({ version: "1.56.0", installed: ["chromium-1248", "chromium_headless_shell-1248"] });
    const { stdout, code } = await start(s, "true");
    expect(code).toBe(0);
    expect(stdout).toBe("");
  });

  it.each([["1.57.0"], ["1.58.0"], ["2.0.1"]])(
    "NEGATIVE: playwright-core %s, which uses the chrome-linux64 layout, is not refused",
    async (version) => {
      const s = setup({ version });
      const { stdout, code } = await start(s, "true");
      expect(code).toBe(0);
      expect(contextOf(stdout)).not.toContain("not supported");
    },
  );
});

// The hook installs Chrome for Testing's linux64 build only, and says so and stops anywhere else (a Mac, arm64). The
// install tests run on linux-x64 (CI's ubuntu-latest, a cloud session) and are skipped elsewhere.
const onLinuxX64 = it.skipIf(!linuxX64);
const elsewhere = it.skipIf(linuxX64);

describe("cloud-browsers hook, platform", () => {
  elsewhere("says no build is fetched for this platform, and installs nothing", async () => {
    const s = setup();
    const { stdout, code } = await start(s, "true");
    expect(code).toBe(0);
    expect(contextOf(stdout)).toContain(`no Chrome for Testing build is fetched for ${process.platform}-${process.arch}`);
    expect(readdirSync(s.browsersPath)).toEqual([]);
  });
});

describe("cloud-browsers hook on linux-x64", () => {
  onLinuxX64("is silent when every build is already installed", async () => {
    const s = setup({ installed: ["chromium-1248", "chromium_headless_shell-1248"] });
    const { stdout, code } = await start(s, "true");
    expect(code).toBe(0);
    expect(stdout).toBe("");
  });

  onLinuxX64("says the Playwright tests cannot run when a download fails, and still exits 0", async () => {
    const s = setup();
    const { stdout, code } = await start(s, "true");
    expect(code).toBe(0);
    expect(contextOf(stdout)).toContain("could not install chromium-1248");
    expect(contextOf(stdout)).toContain("Playwright tests will not run");
    expect(readdirSync(s.browsersPath)).toEqual([]); // no half-written folder is left to look installed
  });

  onLinuxX64("says so when Playwright is not installed and there is no lockfile to install it from", async () => {
    const s = setup({ browsersJson: false });
    const { stdout, code } = await start(s, "true");
    expect(code).toBe(0);
    expect(contextOf(stdout)).toContain("Playwright is not installed");
  });

  onLinuxX64("unpacks each build and marks it complete only after it unpacked", async () => {
    const s = setup();
    const base = mirror({
      "chrome-linux64": zipOf({ "chrome-linux64/chrome": "stand-in" }),
      "chrome-headless-shell-linux64": zipOf({
        "chrome-headless-shell-linux64/chrome-headless-shell": "stand-in",
      }),
    });
    const { stdout, code } = await start(s, "true", { CLOUD_BROWSERS_CFT_BASE: base });
    expect(code).toBe(0);
    expect(contextOf(stdout)).toContain("installed chromium-1248, chromium_headless_shell-1248");
    expect(contextOf(stdout)).toContain("the project's Playwright tests can run now");
    expect(contextOf(stdout)).not.toContain("could not");
    expect(existsSync(path.join(s.browsersPath, "chromium-1248/chrome-linux64/chrome"))).toBe(true);
    expect(existsSync(path.join(s.browsersPath, "chromium-1248/INSTALLATION_COMPLETE"))).toBe(true);
    expect(
      existsSync(
        path.join(
          s.browsersPath,
          "chromium_headless_shell-1248/chrome-headless-shell-linux64/chrome-headless-shell",
        ),
      ),
    ).toBe(true);
  });

  onLinuxX64("removes a folder it created when the zip unpacks only partly, and keeps the build that did", async () => {
    const s = setup();
    const base = mirror({
      "chrome-linux64": zipOf({ "chrome-linux64/chrome": "cut short" }, true),
      "chrome-headless-shell-linux64": zipOf({
        "chrome-headless-shell-linux64/chrome-headless-shell": "x",
      }),
    });
    const { stdout, code } = await start(s, "true", { CLOUD_BROWSERS_CFT_BASE: base });
    expect(code).toBe(0);
    expect(contextOf(stdout)).toContain(
      "installed chromium_headless_shell-1248, but could not install chromium-1248 ",
    );
    expect(contextOf(stdout)).toContain("will not run");
    expect(contextOf(stdout)).not.toContain("can run now");
    expect(readdirSync(s.browsersPath)).toEqual(["chromium_headless_shell-1248"]);
  });

  onLinuxX64("NEGATIVE: leaves a folder that was already there, unmarked, when its zip fails to unpack", async () => {
    const s = setup({ installed: ["chromium_headless_shell-1248"] });
    mkdirSync(path.join(s.browsersPath, "chromium-1248/chrome-linux64"), { recursive: true });
    writeFileSync(path.join(s.browsersPath, "chromium-1248/chrome-linux64/chrome"), "provisioned by hand");
    const base = mirror({ "chrome-linux64": zipOf({ "chrome-linux64/other": "cut short" }, true) });
    const { stdout, code } = await start(s, "true", { CLOUD_BROWSERS_CFT_BASE: base });
    expect(code).toBe(0);
    expect(contextOf(stdout)).toContain("could not install chromium-1248");
    expect(existsSync(path.join(s.browsersPath, "chromium-1248/chrome-linux64/chrome"))).toBe(true);
    expect(existsSync(path.join(s.browsersPath, "chromium-1248/INSTALLATION_COMPLETE"))).toBe(false);
  });

  onLinuxX64("runs npm ci when node_modules is missing and the project has a package-lock.json", async () => {
    const s = setup({
      browsersJson: false,
      installed: ["chromium-1248", "chromium_headless_shell-1248"],
    });
    writeFileSync(path.join(s.root, "package-lock.json"), "{}");
    // A stand-in npm that "installs" playwright-core by writing its browsers.json, so the test never reaches a registry.
    const PATH = fakeNpm(`[ "$1" = ci ] || exit 1\n${WRITE_JSON}`);
    const { stdout, code } = await start(s, "true", { PATH });
    expect(code).toBe(0);
    expect(contextOf(stdout)).toBe(
      "cloud-browsers: ran npm ci, so node_modules is ready; the Chromium builds were already installed.",
    );
    expect(existsSync(path.join(s.root, "node_modules/playwright-core/browsers.json"))).toBe(true);
  });

  onLinuxX64("says so when PLAYWRIGHT_BROWSERS_PATH is 0, and installs nothing", async () => {
    const s = setup();
    const { stdout, code } = await start(s, "true", { PLAYWRIGHT_BROWSERS_PATH: "0" });
    expect(code).toBe(0);
    expect(contextOf(stdout)).toContain("PLAYWRIGHT_BROWSERS_PATH is 0 or relative");
    expect(readdirSync(s.browsersPath)).toEqual([]);
  });

  onLinuxX64("says npm ci failed and never that the tests can run, when npm ci exits non-zero after writing browsers.json", async () => {
    const s = setup({ browsersJson: false });
    writeFileSync(path.join(s.root, "package-lock.json"), "{}");
    // A stand-in npm that unpacks playwright-core and then fails, as a failing prepare script would.
    const PATH = fakeNpm(`${WRITE_JSON}\nexit 1`);
    const { stdout, code } = await start(s, "true", { PATH });
    expect(code).toBe(0);
    expect(contextOf(stdout)).toContain("npm ci failed");
    expect(contextOf(stdout)).not.toContain("can run now");
    expect(readdirSync(s.browsersPath)).toEqual([]);
  });

  onLinuxX64("keeps a finished install when another run sharing the folder fails at the same time", async () => {
    const s = setup({ installed: ["chromium_headless_shell-1248"] });
    const good = mirror({ "chrome-linux64": zipOf({ "chrome-linux64/chrome": "stand-in" }) });
    const bad = mirror({ "chrome-linux64": zipOf({ "chrome-linux64/chrome": "cut short" }, true) });
    const [ok, failed] = await Promise.all([
      start(s, "true", { CLOUD_BROWSERS_CFT_BASE: good }),
      start(s, "true", { CLOUD_BROWSERS_CFT_BASE: bad }),
    ]);
    expect(ok.code).toBe(0);
    expect(failed.code).toBe(0);
    expect(existsSync(path.join(s.browsersPath, "chromium-1248/INSTALLATION_COMPLETE"))).toBe(true);
    expect(readFileSync(path.join(s.browsersPath, "chromium-1248/chrome-linux64/chrome"), "utf8")).toBe("stand-in");
    expect(readdirSync(s.browsersPath).sort()).toEqual(["chromium-1248", "chromium_headless_shell-1248"]); // no staging folder left
  });

  onLinuxX64("NEGATIVE: leaves a folder that was already there, unmarked, untouched even when its zip unpacks, and says to remove it", async () => {
    const s = setup({ installed: ["chromium_headless_shell-1248"] });
    mkdirSync(path.join(s.browsersPath, "chromium-1248"), { recursive: true });
    writeFileSync(path.join(s.browsersPath, "chromium-1248/note"), "provisioned by hand");
    const base = mirror({ "chrome-linux64": zipOf({ "chrome-linux64/chrome": "stand-in" }) });
    const { stdout, code } = await start(s, "true", { CLOUD_BROWSERS_CFT_BASE: base });
    expect(code).toBe(0);
    expect(contextOf(stdout)).toContain("chromium-1248 is already there without INSTALLATION_COMPLETE");
    expect(contextOf(stdout)).not.toContain("can run now");
    expect(readdirSync(path.join(s.browsersPath, "chromium-1248"))).toEqual(["note"]);
    expect(readdirSync(s.browsersPath).sort()).toEqual(["chromium-1248", "chromium_headless_shell-1248"]);
  });

  onLinuxX64("NEGATIVE: refuses a folder that was already there, unmarked, even when it is empty, and leaves it empty", async () => {
    const s = setup({ installed: ["chromium_headless_shell-1248"] });
    mkdirSync(path.join(s.browsersPath, "chromium-1248"));
    const base = mirror({ "chrome-linux64": zipOf({ "chrome-linux64/chrome": "stand-in" }) });
    const { stdout, code } = await start(s, "true", { CLOUD_BROWSERS_CFT_BASE: base });
    expect(code).toBe(0);
    expect(contextOf(stdout)).toContain("chromium-1248 is already there without INSTALLATION_COMPLETE");
    expect(readdirSync(path.join(s.browsersPath, "chromium-1248"))).toEqual([]);
  });

  onLinuxX64("two runs installing the same build at once both end with it complete, and neither half-fills it", async () => {
    const s = setup({ installed: ["chromium_headless_shell-1248"] });
    const base = mirror({ "chrome-linux64": zipOf({ "chrome-linux64/chrome": "stand-in" }) });
    const runs = await Promise.all([1, 2, 3].map(() => start(s, "true", { CLOUD_BROWSERS_CFT_BASE: base })));
    // A run that starts after another published the build finds nothing missing and stays silent.
    const said = runs.map((r) => {
      expect(r.code).toBe(0);
      return contextOf(r.stdout);
    });
    for (const text of said) expect(text === "" || text.includes("installed chromium-1248")).toBe(true);
    expect(said.some((text) => text.includes("installed chromium-1248"))).toBe(true);
    expect(said.join(" ")).not.toMatch(/could not|without INSTALLATION_COMPLETE/);
    expect(readdirSync(path.join(s.browsersPath, "chromium-1248")).sort()).toEqual(["INSTALLATION_COMPLETE", "chrome-linux64"]);
    expect(readdirSync(s.browsersPath).sort()).toEqual(["chromium-1248", "chromium_headless_shell-1248"]);
  });

  onLinuxX64("still installs the browsers after a failed npm ci that wrote browsers.json, saying the tests wait on npm ci", async () => {
    const s = setup({ browsersJson: false });
    writeFileSync(path.join(s.root, "package-lock.json"), "{}");
    const PATH = fakeNpm(`${WRITE_JSON}\nexit 1`);
    const base = mirror({
      "chrome-linux64": zipOf({ "chrome-linux64/chrome": "stand-in" }),
      "chrome-headless-shell-linux64": zipOf({
        "chrome-headless-shell-linux64/chrome-headless-shell": "stand-in",
      }),
    });
    const { stdout, code } = await start(s, "true", { CLOUD_BROWSERS_CFT_BASE: base, PATH });
    expect(code).toBe(0);
    expect(contextOf(stdout)).toContain("npm ci failed");
    expect(contextOf(stdout)).toContain("installed chromium-1248, chromium_headless_shell-1248");
    expect(contextOf(stdout)).toContain("once npm ci succeeds");
    expect(contextOf(stdout)).not.toContain("can run now");
    expect(existsSync(path.join(s.browsersPath, "chromium-1248/INSTALLATION_COMPLETE"))).toBe(true);
  });

  onLinuxX64("reports an unexpected failure, such as no usable temp folder, instead of going silent", async () => {
    const s = setup();
    const { stdout, code } = await start(s, "true", { TMPDIR: path.join(s.root, "no-such-dir") });
    expect(code).toBe(0);
    expect(contextOf(stdout)).toContain("could not finish");
  });
});
