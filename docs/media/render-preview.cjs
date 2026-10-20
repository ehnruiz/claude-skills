// Renders the repository's social preview card (1280x640, the size GitHub recommends).
// Usage: node render-preview.cjs <out.png> (see README.md in this folder)
const { chromium } = require('playwright');

const html = `<!doctype html><html><head><meta charset="utf-8"><style>
  * { box-sizing: border-box; margin: 0; }
  body { width: 1280px; height: 640px; background: #0d1117; color: #e6edf3; font-family: -apple-system, "Helvetica Neue", Arial, sans-serif; padding: 72px 88px; display: flex; flex-direction: column; }
  .mono { font-family: Menlo, "SF Mono", monospace; }
  .kicker { color: #7ee787; font-size: 26px; letter-spacing: .02em; }
  h1 { font-size: 92px; line-height: 1; margin: 14px 0 22px; letter-spacing: -.02em; }
  .tag { font-size: 32px; line-height: 1.3; color: #c9d1d9; white-space: nowrap; }
  .rows { margin-top: 40px; display: flex; flex-direction: column; gap: 14px; }
  .row { font-size: 27px; color: #c9d1d9; display: flex; gap: 16px; align-items: baseline; }
  .row b { color: #7ee787; font-weight: 600; min-width: 290px; }
  .foot { margin-top: auto; display: flex; justify-content: space-between; align-items: center; font-size: 24px; color: #8b949e; }
  .cmd { line-height: 1.5; white-space: nowrap; background: #161b22; border: 1px solid #30363d; border-radius: 8px; padding: 10px 16px; color: #e6edf3; }
  .cmd span { color: #7ee787; }
</style></head><body>
  <div class="kicker mono">github.com/ehnruiz/claude-skills</div>
  <h1>claude-skills</h1>
  <div class="tag">Claude Code skills, subagents and hooks for an agent that proves its work.</div>
  <div class="rows">
    <div class="row"><b class="mono">supervisor</b><span>sends each step to the cheapest model that can do it</span></div>
    <div class="row"><b class="mono">regression-proof</b><span>shows that a fix's new test fails without the fix</span></div>
    <div class="row"><b class="mono">handoff</b><span>leaves a note the next session picks up</span></div>
  </div>
  <div class="foot"><span class="cmd mono"><span>/plugin</span> marketplace add ehnruiz/claude-skills<br><span>/plugin</span> install agent-workflow@ehnruiz</span></div>
</body></html>`;

if (!process.argv[2]) { console.error('usage: node render-preview.cjs <out.png>'); process.exit(2); }

(async () => {
  const browser = await chromium.launch();
  const page = await browser.newPage({ viewport: { width: 1280, height: 640 }, deviceScaleFactor: 1 });
  await page.setContent(html);
  await page.screenshot({ path: process.argv[2] });
  await browser.close();
})();
