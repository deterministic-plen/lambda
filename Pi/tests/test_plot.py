"""Pi orchestration of a Lambda GUI plotter (`pi plot`)."""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent


def test_plot_headless_writes_ppm(tmp_path):
    out = tmp_path / "plot.ppm"
    env = dict(os.environ)
    env["PI_CACHE_DIR"] = str(tmp_path / "cache")
    proc = subprocess.run(
        [sys.executable, "-m", "pilang.cli", "plot", "--headless",
         "--out", str(out)],
        cwd=ROOT, env=env, capture_output=True, text=True)
    assert proc.returncode == 0, proc.stderr
    data = out.read_bytes()
    assert data.startswith(b"P6\n")
    header = data.split(b"\n", 3)
    width, height = (int(x) for x in header[1].split())
    assert (width, height) == (640, 400)
    assert len(data) == (len(b"P6\n") + len(header[1]) + 1 + len(b"255\n")
                          + width * height * 3)
