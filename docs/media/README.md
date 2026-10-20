# Images

| File | Used by | Made with |
| --- | --- | --- |
| `regression-proof.gif` | the top of the main README | `render-demo.cjs`, then `frames_to_gif.py` |
| `social-preview.png` | the repository's social preview (Settings, General, Social preview), shown when a link to the repository is shared | `render-preview.cjs` |

The GIF replays the `regression-proof` recipe from [example 1 in docs/examples.md](../examples.md#1-regression-proof-show-that-a-test-fails-without-its-fix) on commit `356016b`, from a real run on 2026-10-20 (Python 3.9.6, macOS). It is shortened in three ways: it skips the `git diff --name-only` step that lists the test files the fix changed, it shows only the result lines of each test run, and its `FAIL:` line drops the test's class name, `(tests.test_handoff_notice.HandoffNoticeTest)`. The numbers, the verdicts and the exit codes are as printed. If the recipe or its output changes, update the `steps` list in `render-demo.cjs` from a new run.

To rebuild both, with Node 20+, Python 3.9+ and Pillow 9.1+. Playwright goes in a temporary folder, because this repository has no `package.json` and `npm install` here would write to a parent folder's project or to an untracked `node_modules/`:

```bash
pw=$(mktemp -d)
npm install --prefix "$pw" playwright && "$pw/node_modules/.bin/playwright" install chromium
NODE_PATH="$pw/node_modules" node docs/media/render-demo.cjs "$pw/frames"
python3 docs/media/frames_to_gif.py "$pw/frames" docs/media/regression-proof.gif
NODE_PATH="$pw/node_modules" node docs/media/render-preview.cjs docs/media/social-preview.png
```

The scripts ask for Menlo and the system font, so a rebuild matches the committed images only on macOS; elsewhere the browser substitutes fonts.

GitHub has no API for the social preview, so a new `social-preview.png` is uploaded by hand in the repository's settings.
