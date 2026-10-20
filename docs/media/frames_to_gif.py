"""Assemble the PNG frames written by render-demo.cjs into one looping GIF.

Usage: python3 frames_to_gif.py <framesDir> <out.gif> (needs Pillow 9.1+)
"""
import json
import sys
from pathlib import Path

from PIL import Image

frames_dir, out = Path(sys.argv[1]), sys.argv[2]
meta = json.loads((frames_dir / "frames.json").read_text())

images = [Image.open(f["file"]).convert("RGB") for f in meta]
# One shared palette built from the last frame (it holds every color) keeps frames small and stable.
palette = images[-1].quantize(colors=64, method=Image.Quantize.MEDIANCUT)
quantized = [im.quantize(palette=palette, dither=Image.Dither.NONE) for im in images]
quantized[0].save(
    out,
    save_all=True,
    append_images=quantized[1:],
    duration=[f["ms"] for f in meta],
    loop=0,
    optimize=True,
    disposal=1,
)
print(out, Path(out).stat().st_size // 1024, "KB")
