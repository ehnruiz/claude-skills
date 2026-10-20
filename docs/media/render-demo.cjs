// Renders the regression-proof terminal demo to PNG frames; frames_to_gif.py turns them into the GIF.
// Usage: node render-demo.cjs <framesDir> (see README.md in this folder)
const { chromium } = require('playwright');
const fs = require('fs');
const path = require('path');

const out = process.argv[2];
if (!out) { console.error('usage: node render-demo.cjs <framesDir>'); process.exit(2); }
fs.mkdirSync(out, { recursive: true });

// Each step: kind 'cmd' is typed, 'out' appears at once, 'note' is a dim comment, 'pause' holds.
// The output lines come from a real run of the recipe on commit 356016b (2026-10-20), shortened as docs/media/README.md says.
const steps = [
  { kind: 'cmd', text: 'S=$(mktemp -d)' },
  { kind: 'note', text: '# 1. The code from before the fix, with only the fix\'s test file copied in' },
  { kind: 'cmd', text: 'git worktree add --detach $S/proof 356016b~1' },
  { kind: 'cmd', text: 'git -C $S/proof checkout 356016b -- tests/test_handoff_notice.py' },
  { kind: 'cmd', text: '(cd $S/proof && python3 -m unittest tests.test_handoff_notice); echo "without_fix=$?"' },
  { kind: 'out', cls: 'red', text: 'FAIL: test_a_trailing_space_in_a_submodule_path_is_kept' },
  { kind: 'out', text: 'Ran 11 tests in 3.367s' },
  { kind: 'out', cls: 'red', text: 'FAILED (failures=1)' },
  { kind: 'out', cls: 'red b', text: 'without_fix=1' },
  { kind: 'pause', ms: 1400 },
  { kind: 'out', text: '' },
  { kind: 'note', text: '# 2. The fix itself' },
  { kind: 'cmd', text: 'git worktree add --detach $S/proof2 356016b' },
  { kind: 'cmd', text: '(cd $S/proof2 && python3 -m unittest tests.test_handoff_notice); echo "with_fix=$?"' },
  { kind: 'out', text: 'Ran 11 tests in 3.270s' },
  { kind: 'out', cls: 'green', text: 'OK' },
  { kind: 'out', cls: 'green b', text: 'with_fix=0' },
  { kind: 'pause', ms: 600 },
  { kind: 'verdict', text: 'Fails without the fix and passes with it, so the test proves the fix.' },
  { kind: 'pause', ms: 4000 },
];

const html = `<!doctype html><html><head><meta charset="utf-8"><style>
  * { box-sizing: border-box; margin: 0; }
  body { background: #0d1117; width: 960px; height: 524px; font: 15px/1.55 Menlo, "SF Mono", monospace; color: #c9d1d9; }
  .win { margin: 16px; height: 492px; border: 1px solid #30363d; border-radius: 10px; background: #161b22; overflow: hidden; }
  .bar { height: 34px; background: #21262d; display: flex; align-items: center; padding: 0 14px; gap: 8px; }
  .dot { width: 12px; height: 12px; border-radius: 50%; }
  .title { flex: 1; text-align: center; color: #8b949e; font-size: 13px; margin-right: 52px; }
  .term { padding: 14px 18px; white-space: pre; }
  .p { color: #7ee787; } .note { color: #8b949e; } .red { color: #ff7b72; } .green { color: #7ee787; } .b { font-weight: bold; }
  .cur { background: #c9d1d9; color: #161b22; }
  .verdict { margin-top: 10px; padding: 8px 12px; border: 1px solid #238636; border-radius: 6px; background: #12261e; color: #aff5b4; font-weight: bold; display: inline-block; white-space: normal; }
</style></head><body><div class="win"><div class="bar">
  <span class="dot" style="background:#ff5f57"></span><span class="dot" style="background:#febc2e"></span><span class="dot" style="background:#28c840"></span>
  <span class="title">regression-proof: does the new test fail without the fix?</span></div>
  <div class="term" id="t"></div></div></body></html>`;

const esc = (s) => s.replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;');

(async () => {
  const browser = await chromium.launch();
  const page = await browser.newPage({ viewport: { width: 960, height: 524 }, deviceScaleFactor: 1 });
  await page.setContent(html);
  const lines = []; // rendered HTML lines so far
  const frames = []; // { file, ms }
  let n = 0;
  const shot = async (body, ms) => {
    await page.$eval('#t', (el, h) => { el.innerHTML = h; }, body);
    const file = path.join(out, `f${String(n++).padStart(4, '0')}.png`);
    await page.screenshot({ path: file });
    frames.push({ file, ms });
  };
  const render = (extra = '') => lines.join('\n') + (lines.length ? '\n' : '') + extra;

  await shot(render('<span class="p">$</span> <span class="cur"> </span>'), 700);
  for (const s of steps) {
    if (s.kind === 'pause') { frames[frames.length - 1].ms += s.ms; continue; }
    if (s.kind === 'note') { lines.push(`<span class="note">${esc(s.text)}</span>`); await shot(render(), 900); continue; }
    if (s.kind === 'out') { lines.push(`<span class="${s.cls || ''}">${esc(s.text)}</span>`); await shot(render(), 220); continue; }
    if (s.kind === 'verdict') { lines.push(`<span class="verdict">${esc(s.text)}</span>`); await shot(render(), 300); continue; }
    // Typed command: a few characters per frame.
    const step = 5;
    for (let i = step; i < s.text.length + step; i += step) {
      const typed = esc(s.text.slice(0, i));
      await shot(render(`<span class="p">$</span> ${typed}<span class="cur"> </span>`), 45);
    }
    frames[frames.length - 1].ms = 500;
    lines.push(`<span class="p">$</span> ${esc(s.text)}`);
    await shot(render(), 350);
  }
  fs.writeFileSync(path.join(out, 'frames.json'), JSON.stringify(frames));
  await browser.close();
  console.log(`${frames.length} frames`);
})();
