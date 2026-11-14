"""Run a Pi program on the C++ AST interpreter.

The Python part is the frontend only: parse, check, build worker executables,
and serialize a plan.  The plan is executed by `csrc/pi_runtime.cpp` with no
Python in the loop.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import tempfile
from typing import Optional

from .plan import build_plan


def _runtime_binary() -> str:
    root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    src = os.path.join(root, "csrc", "pi_runtime.cpp")
    if not os.path.exists(src):
        raise RuntimeError(f"missing C++ runtime source {src}")
    out = os.path.join(tempfile.gettempdir(), "pi-cpp-runtime")
    if os.name == "nt":
        out += ".exe"
    if os.path.exists(out) and os.path.getmtime(out) >= os.path.getmtime(src):
        return out
    cc = os.environ.get("CXX") or shutil.which("clang++") or shutil.which("g++")
    if not cc:
        raise RuntimeError("no C++ compiler (need clang++ or g++)")
    proc = subprocess.run([cc, "-O2", "-std=c++17", "-pthread", src, "-o", out],
                          capture_output=True, text=True, encoding="utf-8", errors="replace")
    if proc.returncode != 0:
        raise RuntimeError("C++ runtime build failed:\n" + (proc.stderr or proc.stdout))
    return out


def run_cpp(path: str, *, outdir: Optional[str] = None,
            lambda_home: Optional[str] = None, builddir: Optional[str] = None,
            keep_plan: bool = False) -> int:
    plan = build_plan(path, lambda_home=lambda_home, builddir=builddir,
                      outdir=outdir or "")
    fd, plan_path = tempfile.mkstemp(prefix="pi-plan-", suffix=".json")
    with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as f:
        json.dump(plan, f, ensure_ascii=False)
    try:
        exe = _runtime_binary()
        proc = subprocess.run([exe, plan_path])
        return proc.returncode
    finally:
        if not keep_plan:
            try:
                os.remove(plan_path)
            except OSError:
                pass
