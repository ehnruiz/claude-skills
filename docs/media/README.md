# Images

| File | Used by | Made with |
| --- | --- | --- |
| `regression-proof.gif` | the top of the main README | `render-demo.cjs`, then `frames_to_gif.py` |
| `social-preview.png` | the repository's social preview (Settings, General, Social preview), shown when a link to the repository is shared | `render-preview.cjs` |

The GIF replays the `regression-proof` recipe from [example 1 in docs/examples.md](../examples.md#1-regression-proof-show-that-a-test-fails-without-its-fix) on commit `356016b`. Its output lines are copied from a real run of that recipe on 2026-10-20 (Python 3.9.6, macOS), trimmed to the result lines: the failing test's name, the test count, the verdict and the exit code. If the recipe or its output changes, update the `steps` list in `render-demo.cjs` from a new run.

To rebuild both, with Node 20+, Python 3.9+ and Pillow:

```bash
npm install --no-save playwright && npx playwright install chromium
node docs/media/render-demo.cjs /tmp/frames
python3 docs/media/frames_to_gif.py /tmp/frames docs/media/regression-proof.gif
node docs/media/render-preview.cjs docs/media/social-preview.png
```

GitHub has no API for the social preview, so a new `social-preview.png` is uploaded by hand in the repository's settings.
