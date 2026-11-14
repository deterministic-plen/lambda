#!/usr/bin/env python3
"""Render a binary Gray-Scott frame dump as a GIF.

Frame file format: uint32 width, height, frames (little endian), then
frames*width*height float32 values (row-major V field).

    python tools/gray_scott_gif.py frames.bin gray_scott.gif --size 512
"""

from __future__ import annotations

import argparse
import struct

from PIL import Image


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("frames")
    ap.add_argument("output")
    ap.add_argument("--size", type=int, default=0, help="resize edge length (default: native)")
    ap.add_argument("--duration", type=int, default=90, help="ms per frame")
    args = ap.parse_args()

    with open(args.frames, "rb") as f:
        w, h, n = struct.unpack("<III", f.read(12))
        raw = f.read(w * h * n * 4)
    if len(raw) != w * h * n * 4:
        raise SystemExit("truncated frame file")
    values = struct.unpack("<%df" % (w * h * n), raw)
    lo = min(values)
    hi = max(values)
    if hi <= lo:
        hi = lo + 1e-9
    out_size = args.size or max(w, h)

    def color(v: float) -> tuple[int, int, int]:
        q = max(0.0, min(1.0, (v - lo) / (hi - lo)))
        # warm reaction front on a cool background
        r = int(255 * max(0.0, min(1.0, 1.6 * q - 0.15)))
        g = int(255 * max(0.0, min(1.0, 1.5 - 2.0 * abs(q - 0.55))))
        b = int(255 * max(0.0, min(1.0, 1.25 - 1.8 * q)))
        return r, g, b

    images = []
    for i in range(n):
        start = i * w * h
        im = Image.new("RGB", (w, h))
        im.putdata([color(v) for v in values[start:start + w * h]])
        if out_size != w:
            im = im.resize((out_size, out_size), Image.Resampling.LANCZOS)
        images.append(im)
    images[0].save(args.output, save_all=True, append_images=images[1:],
                   duration=args.duration, loop=0, optimize=True)
    images[-1].save(args.output.rsplit(".", 1)[0] + "_final.png")
    print(f"wrote {args.output}: {w}x{h}, {n} frames, value range {lo:.6g}..{hi:.6g}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
