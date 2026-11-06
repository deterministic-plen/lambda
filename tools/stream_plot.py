#!/usr/bin/env python3
"""Read a Lambda --data stream and export or plot its f64/i64 frames.

Frame format (little-endian):

    0..3   "LMB1"
    4      tag: 1=f64 array, 2=i64 array, 3=bytes
    5      flags
    6..7   reserved
    8..11  stream id
    12..15 payload byte length

Examples:
    lambda.exe | python tools/stream_plot.py --plot
    lambda.exe | python tools/stream_plot.py --csv data.csv
    lambda.exe > data.bin
    python tools/stream_plot.py data.bin --x 1 --plot
"""

from __future__ import annotations

import argparse
import struct
import sys
from pathlib import Path


def read_exact(stream, n: int) -> bytes:
    data = stream.read(n)
    if len(data) != n:
        raise EOFError(f"expected {n} bytes, got {len(data)}")
    return data


def frames(stream):
    while True:
        header = stream.read(16)
        if not header:
            return
        if len(header) != 16:
            raise EOFError("truncated frame header")
        magic, tag, flags, reserved, stream_id, nbytes = struct.unpack(
            "<4sBBHII", header)
        if magic != b"LMB1":
            raise ValueError(f"bad frame magic {magic!r}")
        payload = read_exact(stream, nbytes)
        yield tag, flags, stream_id, payload


def decode(tag: int, payload: bytes):
    if tag == 1:
        return struct.unpack(f"<{len(payload) // 8}d", payload)
    if tag == 2:
        return struct.unpack(f"<{len(payload) // 8}q", payload)
    return payload


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("file", nargs="?", help="input file; default stdin")
    parser.add_argument("--csv", metavar="PATH", help="write id,index,value rows")
    parser.add_argument("--x", type=int, metavar="ID",
                        help="use this stream as the x axis")
    parser.add_argument("--plot", action="store_true", help="show a matplotlib plot")
    parser.add_argument("--list", action="store_true", help="list stream ids")
    args = parser.parse_args()

    source = open(args.file, "rb") if args.file else sys.stdin.buffer
    series = {}
    for tag, flags, stream_id, payload in frames(source):
        value = decode(tag, payload)
        if tag in (1, 2):
            series.setdefault(stream_id, []).extend(value)

    if args.list or not (args.csv or args.plot):
        for stream_id, values in sorted(series.items()):
            kind = "f64" if isinstance(values[0], float) else "i64"
            print(f"{stream_id}: {kind} {len(values)} values")
        return 0

    if args.csv:
        with open(args.csv, "w", newline="", encoding="utf-8") as f:
            f.write("stream,index,value\n")
            for stream_id, values in sorted(series.items()):
                for i, value in enumerate(values):
                    f.write(f"{stream_id},{i},{value!r}\n")
        return 0

    try:
        import matplotlib.pyplot as plt
    except ImportError:
        print("matplotlib is not installed; use --csv or --list", file=sys.stderr)
        return 1

    if args.x is not None:
        xs = series.get(args.x)
        if xs is None:
            print(f"stream {args.x} not found", file=sys.stderr)
            return 1
        for stream_id, values in sorted(series.items()):
            if stream_id == args.x:
                continue
            n = min(len(xs), len(values))
            plt.plot(xs[:n], values[:n], label=f"stream {stream_id}")
    else:
        for stream_id, values in sorted(series.items()):
            plt.plot(range(len(values)), values, label=f"stream {stream_id}")

    plt.legend()
    plt.grid(True)
    plt.show()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
