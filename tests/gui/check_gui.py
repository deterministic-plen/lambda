"""Headless GUI smoke check: compile a GUI program, render a PPM, validate it.

`run_tests.py` can compile GUI programs but cannot let them open a window, so
this script runs the program with LAMBDA_GUI_HEADLESS=1 and checks the PPM
header and payload size.  It is used by CI and can be run locally on any OS.
"""

from __future__ import annotations

import os
import subprocess
import sys
import tempfile

ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))


def main() -> int:
    source = os.path.join(ROOT, "tests", "gui", "gui_smoke.lambda")
    with tempfile.TemporaryDirectory(prefix="lambda-gui-") as tmp:
        exe = os.path.join(tmp, "gui_smoke.exe")
        proc = subprocess.run(
            [sys.executable, os.path.join(ROOT, "lambda.py"), source,
             "-o", exe], capture_output=True, text=True)
        if proc.returncode != 0:
            print(proc.stderr, file=sys.stderr)
            return 1
        out = os.path.join(tmp, "gui.ppm")
        env = dict(os.environ)
        env["LAMBDA_GUI_HEADLESS"] = "1"
        env["LAMBDA_GUI_OUT"] = out
        proc = subprocess.run([exe], env=env, capture_output=True, text=True)
        if proc.returncode != 0:
            print(proc.stderr, file=sys.stderr)
            return 1
        with open(out, "rb") as f:
            data = f.read()
        header = data.split(b"\n", 3)
        if len(header) < 4 or header[0] != b"P6":
            print("not a P6 PPM", file=sys.stderr)
            return 1
        width, height = (int(x) for x in header[1].split())
        if width != 64 or height != 48:
            print(f"unexpected size {width}x{height}", file=sys.stderr)
            return 1
        expected = (len(b"P6\n") + len(header[1]) + 1 + len(b"255\n")
                    + width * height * 3)
        if len(data) != expected:
            print(f"payload size {len(data)} != {expected}", file=sys.stderr)
            return 1
        if b"lambda-gui: wrote" not in proc.stderr.encode():
            print("missing headless notice", file=sys.stderr)
            return 1
    print("GUI headless PPM: ok (64x48)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
