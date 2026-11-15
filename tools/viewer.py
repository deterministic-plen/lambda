"""Universal Pi/Lambda data viewer.

One renderer for every run: it reads Pi output directories (results.json +
CSV + frames.bin), CSV files, plain numeric text (e.g. a simulation printing
one value per line), LMB1 data streams, or a viewer manifest.  Nothing is
embedded in the Lambda program or its output files; the renderer is shared.

Usage:
    python tools/viewer.py out/gray_scott
    python tools/viewer.py simulations/euler.txt
    python tools/viewer.py --data run.csv --no-open

The server binds only to 127.0.0.1 and serves the selected data directory plus
the static viewer assets.
"""

from __future__ import annotations

import argparse
import csv
import json
import math
import os
import struct
import sys
import threading
import webbrowser
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import quote, unquote, urlparse

HERE = Path(__file__).resolve().parent
ASSETS = HERE / "viewer"
MAX_GRID_CELLS = 16_000_000


def read_text_auto(path: Path) -> str:
    """Read UTF-8/UTF-16 text, including Windows PowerShell redirects."""
    raw = path.read_bytes()
    if raw.startswith(b"\xff\xfe"):
        return raw[2:].decode("utf-16-le", errors="replace")
    if raw.startswith(b"\xfe\xff"):
        return raw[2:].decode("utf-16-be", errors="replace")
    if raw.startswith(b"\xef\xbb\xbf"):
        raw = raw[3:]
    try:
        return raw.decode("utf-8")
    except UnicodeDecodeError:
        return raw.decode("utf-16-le", errors="replace")


def _finite(v) -> bool:
    try:
        return math.isfinite(float(v))
    except (TypeError, ValueError):
        return False


def _number(v) -> float:
    return float(v)


def load_numeric_text(path: Path) -> dict | None:
    vals = []
    for line in read_text_auto(path).splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        for tok in line.replace(",", " ").split():
            if _finite(tok):
                vals.append(float(tok))
            else:
                return None
    if len(vals) < 2:
        return None
    return {"series": [{"name": path.stem, "kind": "line",
                        "x": list(range(len(vals))), "y": vals}]}


def load_csv(path: Path) -> dict | None:
    rows = list(csv.reader(read_text_auto(path).splitlines()))
    if not rows:
        return None
    header = [h.strip().lower() for h in rows[0]]
    data = rows[1:]
    # Pi grid CSV
    if header[:3] == ["row", "col", "value"]:
        cells = []
        maxr = maxc = 0
        for r in data:
            if len(r) < 3 or not all(_finite(x) for x in r[:3]):
                continue
            rr, cc, vv = int(float(r[0])), int(float(r[1])), float(r[2])
            maxr, maxc = max(maxr, rr), max(maxc, cc)
            cells.append((rr, cc, vv))
        if maxr * maxc > MAX_GRID_CELLS:
            raise ValueError("grid too large for the viewer")
        values = [0.0] * ((maxr + 1) * (maxc + 1))
        for rr, cc, vv in cells:
            values[rr * (maxc + 1) + cc] = vv
        return {"grids": [{"name": path.stem, "rows": maxr + 1,
                           "cols": maxc + 1, "values": values}]}
    # Pi series CSV
    if header[:2] == ["x", "y"]:
        xs, ys = [], []
        for r in data:
            if len(r) >= 2 and _finite(r[0]) and _finite(r[1]):
                xs.append(float(r[0])); ys.append(float(r[1]))
        return {"series": [{"name": path.stem, "kind": "line",
                            "x": xs, "y": ys}]}
    # Generic numeric CSV: first column x, remaining columns series.
    if len(header) >= 2 and all(_finite(r[0]) for r in data if r):
        xs = [float(r[0]) for r in data if r and _finite(r[0])]
        series = []
        for ci in range(1, len(header)):
            ys = [float(r[ci]) for r in data
                  if len(r) > ci and _finite(r[ci])]
            n = min(len(xs), len(ys))
            if n:
                series.append({"name": header[ci] or f"col{ci}",
                               "kind": "line", "x": xs[:n], "y": ys[:n]})
        if series:
            return {"series": series}
    return None


def load_json(path: Path) -> dict | None:
    try:
        obj = json.loads(read_text_auto(path))
    except (OSError, json.JSONDecodeError):
        return None
    if isinstance(obj, dict) and ("series" in obj or "grids" in obj):
        return obj
    return None


def load_frames(path: Path) -> dict | None:
    """Read only the frames header; never load a multi-GB sequence eagerly."""
    try:
        size = path.stat().st_size
        with path.open("rb") as f:
            head = f.read(12)
    except OSError:
        return None
    if len(head) < 12 or size < 12:
        return None
    cols, rows, count = struct.unpack("<III", head)
    if cols == 0 or rows == 0 or count == 0:
        return None
    frame_bytes = cols * rows * 4
    count = min(count, max(0, (size - 12) // frame_bytes) if frame_bytes else 0)
    name = path.name[:-11] if path.name.endswith(".frames.bin") else path.stem
    return {"grids": [{"name": name, "rows": rows, "cols": cols,
                       "framesUrl": "/data/" + quote(path.name),
                       "frameCount": count, "frameBytes": frame_bytes,
                       "bytes": size}]}


def load_result_dir(root: Path) -> dict:
    manifest = {"title": root.name, "series": [], "grids": [], "texts": []}
    results = root / "results.json"
    names = []
    if results.exists():
        try:
            obj = json.loads(read_text_auto(results))
            for a in obj.get("artifacts", []):
                names.append((a.get("kind", ""), a.get("name", "")))
        except (OSError, json.JSONDecodeError):
            pass
    seen = set()
    for kind, name in names:
        seen.add(name)
        csv_path = root / f"{name}.csv"
        if kind == "grid":
            bases = [name]
            if name.endswith("_final"):
                bases.append(name[:-6])
            found = False
            for base in bases:
                frames = root / f"{base}.frames.bin"
                if frames.exists():
                    m = load_frames(frames)
                    if m:
                        m["grids"][0]["name"] = name
                        manifest["grids"].extend(m["grids"])
                        found = True
                        break
                for csv_name in (f"{base}.csv", f"{base}.final.csv"):
                    csv_path2 = root / csv_name
                    if csv_path2.exists():
                        m = load_csv(csv_path2)
                        if m:
                            for g in m.get("grids", []):
                                g["name"] = name
                            manifest["grids"].extend(m.get("grids", []))
                            found = True
                            break
                if found:
                    break
            continue
        if csv_path.exists():
            m = load_csv(csv_path)
            if m:
                for s in m.get("series", []):
                    s["name"] = name
                    s["kind"] = "points" if kind == "points" else ("bars" if kind == "bars" else "line")
                manifest["series"].extend(m.get("series", []))
    # Include standalone CSVs not referenced by results.json.
    for p in sorted(root.glob("*.csv")):
        if p.stem in seen or p.name.endswith(".final.csv"):
            continue
        try:
            m = load_csv(p)
        except ValueError:
            continue
        if m:
            manifest["series"].extend(m.get("series", []))
            manifest["grids"].extend(m.get("grids", []))
    # Standalone animation files not listed in results.json.
    if not manifest["grids"]:
        for p in sorted(root.glob("*.frames.bin")):
            m = load_frames(p)
            if m:
                manifest["grids"].extend(m["grids"])
                break
    if not manifest["series"] and not manifest["grids"]:
        # Fall back to numeric text files.
        for p in sorted(root.glob("*.txt")):
            m = load_numeric_text(p)
            if m:
                manifest["series"].extend(m["series"])
    return manifest


def load_stdin() -> dict:
    vals = []
    for line in sys.stdin:
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        for tok in line.replace(",", " ").split():
            if _finite(tok):
                vals.append(float(tok))
            else:
                raise ValueError("stdin contains non-numeric text")
    if len(vals) < 2:
        raise ValueError("stdin needs at least two numeric values")
    return {"title": "stdin", "series": [{"name": "stdin", "kind": "line",
                                          "x": list(range(len(vals))), "y": vals}]}


def load_source(path: Path) -> tuple[dict, Path]:
    if path.is_dir():
        return load_result_dir(path), path
    if not path.exists():
        raise FileNotFoundError(path)
    suffix = path.suffix.lower()
    data = None
    if suffix == ".json":
        data = load_json(path)
    elif suffix == ".csv":
        data = load_csv(path)
    elif suffix == ".frames.bin":
        data = load_frames(path)
    elif suffix in (".bin", ".lmb1"):
        data = None
    else:
        data = load_numeric_text(path)
    if data is None and suffix in (".bin", ".lmb1"):
        # Reuse the existing LMB1 decoder for stream files.
        sys.path.insert(0, str(HERE.parent))
        try:
            from stream_plot import decode, frames  # type: ignore
            series = {}
            with path.open("rb") as f:
                for tag, _flags, sid, payload in frames(f):
                    if tag in (1, 2):
                        series.setdefault(sid, []).extend(decode(tag, payload))
            data = {"series": [{"name": f"stream {sid}", "kind": "line",
                                "x": list(range(len(v))), "y": list(v)}
                               for sid, v in sorted(series.items())]}
        except Exception:
            data = None
    if data is None:
        raise ValueError(f"cannot interpret {path}")
    data.setdefault("title", path.name)
    return data, path if path.is_dir() else path.parent


class Handler(SimpleHTTPRequestHandler):
    root: Path
    manifest: dict

    def log_message(self, fmt, *args):  # quiet local viewer
        pass

    def do_GET(self):
        parsed = urlparse(self.path)
        p = parsed.path
        if p in ("/", "/viewer.html"):
            return self._asset("viewer.html", "text/html; charset=utf-8")
        if p == "/viewer.js":
            return self._asset("viewer.js", "application/javascript; charset=utf-8")
        if p == "/manifest.json":
            body = json.dumps(self.manifest).encode("utf-8")
            self.send_response(200)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
            return
        if p.startswith("/data/"):
            rel = unquote(p[len("/data/"):])
            target = (self.root / rel).resolve()
            try:
                target.relative_to(self.root.resolve())
            except ValueError:
                self.send_error(403)
                return
            if not target.is_file():
                self.send_error(404)
                return
            ctype = "application/octet-stream"
            if target.suffix.lower() == ".csv":
                ctype = "text/csv; charset=utf-8"
            elif target.suffix.lower() == ".json":
                ctype = "application/json"
            size = target.stat().st_size
            rng = self.headers.get("Range", "")
            start = end = None
            if rng.startswith("bytes="):
                part = rng[6:].split(",", 1)[0]
                a, _, b = part.partition("-")
                try:
                    if a:
                        start = int(a); end = int(b) if b else size - 1
                    elif b:
                        start = max(0, size - int(b)); end = size - 1
                except ValueError:
                    start = end = None
            if start is not None:
                start = max(0, min(start, size))
                if start >= size:
                    self.send_error(416)
                    return
                end = max(start, min(end if end is not None else size - 1, size - 1))
                self.send_response(206)
                self.send_header("Content-Type", ctype)
                self.send_header("Content-Range", f"bytes {start}-{end}/{size}")
                self.send_header("Content-Length", str(end - start + 1))
                self.send_header("Accept-Ranges", "bytes")
                self.end_headers()
                with target.open("rb") as f:
                    f.seek(start)
                    left = end - start + 1
                    while left > 0:
                        chunk = f.read(min(1 << 20, left))
                        if not chunk:
                            break
                        self.wfile.write(chunk); left -= len(chunk)
                return
            self.send_response(200)
            self.send_header("Content-Type", ctype)
            self.send_header("Content-Length", str(size))
            self.send_header("Accept-Ranges", "bytes")
            self.end_headers()
            with target.open("rb") as f:
                while True:
                    chunk = f.read(1 << 20)
                    if not chunk:
                        break
                    self.wfile.write(chunk)
            return
        self.send_error(404)

    def _asset(self, name: str, ctype: str):
        target = ASSETS / name
        if not target.is_file():
            self.send_error(404)
            return
        body = target.read_bytes()
        self.send_response(200)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("path", help="Pi output directory, CSV, JSON, text, LMB1, or .frames.bin")
    ap.add_argument("--port", type=int, default=0, help="port (0 chooses a free port)")
    ap.add_argument("--no-open", action="store_true", help="do not open a browser")
    ap.add_argument("--json", metavar="PATH", help="write the normalized manifest and exit")
    args = ap.parse_args(argv)
    if args.path == "-":
        manifest, root = load_stdin(), HERE.parent
    else:
        manifest, root = load_source(Path(args.path).resolve())
    if args.json:
        Path(args.json).write_text(json.dumps(manifest, indent=2), encoding="utf-8")
        print(f"manifest: {args.json}")
        return 0
    Handler.root = root
    Handler.manifest = manifest
    httpd = ThreadingHTTPServer(("127.0.0.1", args.port), Handler)
    port = httpd.server_address[1]
    url = f"http://127.0.0.1:{port}/viewer.html"
    print(f"viewer: {url}")
    print(f"data:   {root}")
    if not args.no_open:
        threading.Timer(0.25, lambda: webbrowser.open(url)).start()
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        print()
    finally:
        httpd.server_close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
