"""Run the repository's Pi examples through the C++ runtime.

Executable regression gate for the C++ AST runtime.  It exercises workers,
channels, systems, collectives, recovery, and result artifacts, and verifies
that the default CLI selects the C++ backend.
"""

from __future__ import annotations

import os
import subprocess
import sys
import tempfile

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
PY = sys.executable


def run(name: str, expect: str, artifacts=()) -> None:
    out = os.path.join(tempfile.gettempdir(), "pi-cpp-check-" + name)
    subprocess.run(["powershell", "-NoProfile", "-Command", f"if (Test-Path '{out}') {{ Remove-Item -Recurse -Force '{out}' }}"], check=False)
    cmd = [PY, "-m", "pilang", "cpp", os.path.join("examples", name + ".pi"), "--out", out]
    p = subprocess.run(cmd, cwd=ROOT, text=True, capture_output=True,
                       env={**os.environ, "PYTHONPATH": ROOT,
                            "LAMBDA_HOME": os.environ.get("LAMBDA_HOME", r"C:\Users\mumuj\Documents\lambda")})
    if p.returncode != 0:
        raise SystemExit(f"{name}: exit {p.returncode}\n{p.stdout}\n{p.stderr}")
    combined = p.stdout + p.stderr
    if expect not in combined:
        raise SystemExit(f"{name}: missing {expect!r}\n{combined}")
    for a in artifacts:
        if not os.path.exists(os.path.join(out, a)):
            raise SystemExit(f"{name}: missing artifact {a}")
    print(f"PASS {name}")


def run_default(name: str, expect: str) -> None:
    """Exercise the default CLI backend, which must select C++ for this program."""
    p = subprocess.run([PY, "-m", "pilang", "run", os.path.join("examples", name + ".pi")],
                       cwd=ROOT, text=True, capture_output=True,
                       env={**os.environ, "PYTHONPATH": ROOT,
                            "LAMBDA_HOME": os.environ.get("LAMBDA_HOME", r"C:\Users\mumuj\Documents\lambda")})
    combined = p.stdout + p.stderr
    if p.returncode != 0 or expect not in combined or "pi-cpp: done" not in combined:
        raise SystemExit(f"default backend {name}: exit {p.returncode}\n{combined}")
    print(f"PASS default {name}")


def main() -> None:
    run("field", "shape sin(x) * cos(y), 200 x 200", ["report.html", "field.csv"])
    run("drone", "shape 200 control steps", ["report.html"])
    run("heat", "after WorkerDied + restart", ["report.html", "h1_clean.csv"])
    run("heat2d_cells", "cell(1,1) after 8 ticks = 0.5", ["report.html"])
    run("reduce", "sum of squares 1..100 = [338350]", ["report.html"])
    run("fault", "b 0", ["report.html"])
    run("gray_scott", "animation frames: 60", ["gray_scott.frames.bin", "report.html"])
    run("gray_scott_multi", "animation frames: 60", ["gray_scott.frames.bin", "report.html"])
    run_default("reduce", "sum of squares 1..100 = [338350]")
    print("all C++ example checks passed")


if __name__ == "__main__":
    main()
