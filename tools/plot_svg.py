#!/usr/bin/env python3
"""Render a Lambda --data stream as an SVG plot with no dependencies.

Reads the same LMB1 frames as tools/stream_plot.py (f64/i64 arrays), draws
one polyline per stream id on linear or log axes, and writes a standalone
SVG that any browser or image viewer can open.

Examples:
    lambda.exe | python tools/plot_svg.py --out plot.svg
    lambda.exe > data.bin
    python tools/plot_svg.py data.bin --x 1 --out plot.svg
    python tools/plot_svg.py data.bin --x 1 --logy --title "error vs step"

The plot has axes with ticks, a legend, optional markers, and an optional
zoom on the y range.  For an interactive window use tools/stream_plot.py.
"""

from __future__ import annotations

import argparse
import math
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from stream_plot import decode, frames  # noqa: E402

PALETTE = ["#1f77b4", "#d62728", "#2ca02c", "#9467bd", "#ff7f0e",
           "#17becf", "#8c564b", "#e377c2"]
W, H = 900, 520
LEFT, RIGHT, TOP, BOTTOM = 78, 220, 40, 62


def nice_ticks(lo: float, hi: float, count: int = 6):
    if not math.isfinite(lo) or not math.isfinite(hi):
        return [lo, hi]
    if hi == lo:
        return [lo]
    span = hi - lo
    raw = span / max(1, count)
    mag = 10 ** math.floor(math.log10(abs(raw)))
    for mult in (1, 2, 2.5, 5, 10):
        step = mult * mag
        if step >= raw:
            break
    start = math.ceil(lo / step) * step
    ticks = []
    v = start
    while v <= hi + step * 1e-9:
        ticks.append(round(v, 12))
        v += step
    return ticks


def fmt(v: float) -> str:
    if abs(v) >= 1e5 or (v != 0 and abs(v) < 1e-3):
        return f"{v:.3g}"
    return f"{v:.6g}"


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("file", nargs="?", help="input file; default stdin")
    ap.add_argument("--x", type=int, metavar="ID", help="stream id to use as x axis")
    ap.add_argument("--out", default="plot.svg", help="output SVG path")
    ap.add_argument("--title", default="", help="plot title")
    ap.add_argument("--logy", action="store_true", help="logarithmic y axis")
    ap.add_argument("--logx", action="store_true", help="logarithmic x axis")
    ap.add_argument("--ymin", type=float, help="clip y lower bound")
    ap.add_argument("--ymax", type=float, help="clip y upper bound")
    ap.add_argument("--points", action="store_true", help="draw markers")
    ap.add_argument("--list", action="store_true", help="only list streams")
    args = ap.parse_args(argv)

    source = open(args.file, "rb") if args.file else sys.stdin.buffer
    series = {}
    for tag, _flags, stream_id, payload in frames(source):
        if tag in (1, 2):
            series.setdefault(stream_id, []).extend(decode(tag, payload))
    if not series:
        print("no f64/i64 streams found", file=sys.stderr)
        return 1
    if args.list:
        for sid, vals in sorted(series.items()):
            print(f"{sid}: {len(vals)} values  min={min(vals):.6g} max={max(vals):.6g}")
        return 0

    xs_axis = series.get(args.x) if args.x is not None else None
    others = [sid for sid in sorted(series) if sid != args.x]

    def tr_x(v: float) -> float:
        return math.log10(v) if args.logx and v > 0 else v

    points = []
    for sid in others:
        ys = series[sid]
        if xs_axis is not None:
            n = min(len(xs_axis), len(ys))
            pairs = [(xs_axis[i], ys[i]) for i in range(n)]
        else:
            pairs = [(float(i), ys[i]) for i in range(len(ys))]
        pairs = [(x, y) for x, y in pairs
                 if math.isfinite(y) and (not args.logx or x > 0)
                 and (not args.logy or y > 0)]
        if not pairs:
            continue
        if args.ymin is not None:
            pairs = [(x, y) for x, y in pairs if y >= args.ymin]
        if args.ymax is not None:
            pairs = [(x, y) for x, y in pairs if y <= args.ymax]
        if pairs:
            points.append((sid, pairs))
    if not points:
        print("no drawable points", file=sys.stderr)
        return 1

    xlo = min(min(x for x, _ in p) for _, p in points)
    xhi = max(max(x for x, _ in p) for _, p in points)
    ylo = min(min(y for _, y in p) for _, p in points)
    yhi = max(max(y for _, y in p) for _, p in points)
    if xhi == xlo:
        xhi = xlo + 1.0
    if yhi == ylo:
        pad = abs(yhi) * 0.1 or 1.0
        ylo, yhi = ylo - pad, yhi + pad

    def sx(v: float) -> float:
        t = (tr_x(v) - tr_x(xlo)) / (tr_x(xhi) - tr_x(xlo))
        return LEFT + t * (W - LEFT - RIGHT)

    def sy(v: float) -> float:
        def ty(a):
            return math.log10(a) if args.logy else a
        t = (ty(v) - ty(ylo)) / (ty(yhi) - ty(ylo))
        return H - BOTTOM - t * (H - TOP - BOTTOM)

    out = []
    out.append(f'<svg xmlns="http://www.w3.org/2000/svg" width="{W}" height="{H}" '
               f'viewBox="0 0 {W} {H}" font-family="Segoe UI,Helvetica,Arial,sans-serif">')
    out.append(f'<rect width="{W}" height="{H}" fill="#ffffff"/>')
    if args.title:
        out.append(f'<text x="{LEFT}" y="24" font-size="17" font-weight="600" '
                   f'fill="#111">{esc(args.title)}</text>')

    for tick in nice_ticks(tr_x(xlo), tr_x(xhi)):
        x = LEFT + (tick - tr_x(xlo)) / (tr_x(xhi) - tr_x(xlo)) * (W - LEFT - RIGHT)
        label = fmt(10 ** tick) if args.logx else fmt(tick)
        out.append(f'<line x1="{x:.1f}" y1="{TOP}" x2="{x:.1f}" '
                   f'y2="{H - BOTTOM}" stroke="#e8e8e8"/>')
        out.append(f'<text x="{x:.1f}" y="{H - BOTTOM + 20}" font-size="12" '
                   f'fill="#444" text-anchor="middle">{label}</text>')
    for tick in nice_ticks(ylo if not args.logy else math.log10(ylo),
                           yhi if not args.logy else math.log10(yhi)):
        yv = 10 ** tick if args.logy else tick
        if not (ylo - 1e-12 <= yv <= yhi + 1e-12):
            continue
        y = sy(yv)
        out.append(f'<line x1="{LEFT}" y1="{y:.1f}" x2="{W - RIGHT}" '
                   f'y2="{y:.1f}" stroke="#e8e8e8"/>')
        out.append(f'<text x="{LEFT - 8}" y="{y + 4:.1f}" font-size="12" '
                   f'fill="#444" text-anchor="end">{fmt(yv)}</text>')

    out.append(f'<rect x="{LEFT}" y="{TOP}" width="{W - LEFT - RIGHT}" '
               f'height="{H - TOP - BOTTOM}" fill="none" stroke="#999"/>')
    if args.logy:
        out.append(f'<text x="{LEFT}" y="{H - 10}" font-size="12" fill="#666">'
                   f'log10 y scale</text>')

    for k, (sid, pairs) in enumerate(points):
        color = PALETTE[k % len(PALETTE)]
        step = max(1, len(pairs) // 4000)
        pts = " ".join(f"{sx(x):.1f},{sy(y):.1f}" for x, y in pairs[::step])
        out.append(f'<polyline fill="none" stroke="{color}" stroke-width="1.6" '
                   f'stroke-linejoin="round" points="{pts}"/>')
        if args.points or len(pairs) <= 60:
            for x, y in pairs[::step]:
                out.append(f'<circle cx="{sx(x):.1f}" cy="{sy(y):.1f}" r="2.2" '
                           f'fill="{color}"/>')
        ly = TOP + 14 + k * 20
        out.append(f'<line x1="{W - RIGHT + 14}" y1="{ly - 4}" '
                   f'x2="{W - RIGHT + 44}" y2="{ly - 4}" stroke="{color}" '
                   f'stroke-width="2.5"/>')
        out.append(f'<text x="{W - RIGHT + 50}" y="{ly}" font-size="12" '
                   f'fill="#333">stream {sid} ({len(pairs)} pts)</text>')

    out.append("</svg>")
    Path(args.out).write_text("\n".join(out), encoding="utf-8")
    print(f"wrote {args.out} ({len(points)} series, "
          f"x=[{fmt(xlo)}, {fmt(xhi)}], y=[{fmt(ylo)}, {fmt(yhi)}])")
    return 0


def esc(text: str) -> str:
    return (text.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;"))


if __name__ == "__main__":
    raise SystemExit(main())
