"""Run a Pi program on the C++ AST interpreter.

The Python part is the frontend only: parse, check, build worker executables,
and serialize a plan.  The plan is executed by `csrc/pi_runtime.cpp` with no
Python in the loop.

The runtime binary lives in the private per-user cache (PR.md 1.2 / 2.6), is
keyed by a content hash of the source plus the compiler identity, and is
installed atomically, so concurrent or interrupted builds cannot give another
process a stale or half-written executable.
"""

from __future__ import annotations

import hashlib
import json
import os
import shutil
import subprocess
import tempfile
from typing import Optional

from .cache import (CACHE_LAYOUT_VERSION, cache_subdir, install_temp_output,
                    is_regular_file, make_temp_output)
from .frontend import run_frontend
from .plan import build_plan

RUNTIME_CACHE_VERSION = 3


def _runtime_source() -> str:
    root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    src = os.path.join(root, "csrc", "pi_runtime.cpp")
    if not os.path.exists(src):
        raise RuntimeError(f"missing C++ runtime source {src}")
    return src


def _find_cxx() -> str:
    cc = os.environ.get("CXX") or shutil.which("clang++") or shutil.which("g++")
    if not cc:
        raise RuntimeError("no C++ compiler (need clang++ or g++)")
    return cc


def _compiler_identity(cc: str) -> str:
    try:
        out = subprocess.run([cc, "--version"], capture_output=True, text=True,
                             timeout=30).stdout
    except (OSError, subprocess.SubprocessError):
        out = cc
    return f"{cc}\n{out}"


def _runtime_binary() -> str:
    src = _runtime_source()
    with open(src, "rb") as f:
        source = f.read()
    cc = _find_cxx()
    flags = ["-O2", "-std=c++17", "-pthread"]
    h = hashlib.sha256()
    h.update(f"pi-cpp-runtime-v{RUNTIME_CACHE_VERSION}-layout{CACHE_LAYOUT_VERSION}".encode())
    h.update(source)
    h.update("\0".join(flags).encode())
    h.update(_compiler_identity(cc).encode("utf-8", "replace"))
    key = h.hexdigest()
    builddir = cache_subdir("runtime")
    out = os.path.join(builddir, f"pi-cpp-runtime-{key[:24]}")
    if os.name == "nt":
        out += ".exe"
    if is_regular_file(out):
        return out
    tmp = make_temp_output(builddir, stem="pi-cpp-runtime")
    try:
        proc = subprocess.run([cc, *flags, src, "-o", tmp],
                              capture_output=True, text=True, encoding="utf-8",
                              errors="replace")
        if proc.returncode != 0:
            raise RuntimeError("C++ runtime build failed:\n" + (proc.stderr or proc.stdout))
        produced = tmp
        if os.name == "nt" and not os.path.exists(produced) and os.path.exists(produced + ".exe"):
            produced += ".exe"
        if not os.path.exists(produced):
            raise RuntimeError("C++ runtime build reported success but produced no output")
        install_temp_output(produced, out)
        return out
    finally:
        for leftover in (tmp, tmp + ".exe"):
            try:
                os.remove(leftover)
            except OSError:
                pass


def _write_plan(path: str, *, lambda_home=None, builddir=None, outdir: str = "") -> str:
    """Build the plan and write it to a ``mkstemp`` JSON file (frontend thread)."""
    plan = build_plan(path, lambda_home=lambda_home, builddir=builddir, outdir=outdir)
    fd, plan_path = tempfile.mkstemp(prefix="pi-plan-", suffix=".json")
    try:
        with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as f:
            json.dump(plan, f, ensure_ascii=False)
    except BaseException:
        try:
            os.remove(plan_path)
        except OSError:
            pass
        raise
    return plan_path


def run_cpp(path: str, *, outdir: Optional[str] = None,
            lambda_home: Optional[str] = None, builddir: Optional[str] = None,
            keep_plan: bool = False) -> int:
    # P5: parse/check/encode run on a 64 MB thread with a raised recursion limit.
    plan_path = run_frontend(lambda: _write_plan(
        path, lambda_home=lambda_home, builddir=builddir, outdir=outdir or ""))
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
