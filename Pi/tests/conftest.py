"""Shared helpers for the Pi integration tests.

These tests exercise the real toolchain: the Lambda compiler (to build worker
executables), the C++ runtime and the AOT path.  They are skipped when the
Lambda compiler or a C compiler is unavailable.  Build products go to a
throw-away ``PI_CACHE_DIR`` so the developer's per-user cache is not touched.
"""

from __future__ import annotations

import os
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

import pytest

PI_ROOT = Path(__file__).resolve().parents[1]
REPO_ROOT = PI_ROOT.parent
LAMBDA_HOME = Path(os.environ.get("LAMBDA_HOME", str(REPO_ROOT)))
PYTHON = sys.executable

_session_cache: str | None = None
_session_fake_worker: str | None = None


def cache_dir() -> str:
    global _session_cache
    if _session_cache is None:
        _session_cache = tempfile.mkdtemp(prefix="pi-test-cache-")
    return _session_cache


def require_toolchain() -> None:
    if not (LAMBDA_HOME / "lambda.py").is_file():
        pytest.skip(f"Lambda compiler not found at {LAMBDA_HOME} (set LAMBDA_HOME)")
    if not (shutil.which(os.environ.get("CC") or "clang") or shutil.which("cc")):
        pytest.skip("no C compiler (clang/cc) on PATH")


def pi_env(**extra: str) -> dict:
    env = dict(os.environ)
    env["LAMBDA_HOME"] = str(LAMBDA_HOME)
    env["PI_CACHE_DIR"] = cache_dir()
    env["PYTHONPATH"] = str(PI_ROOT) + (
        os.pathsep + env["PYTHONPATH"] if env.get("PYTHONPATH") else "")
    env.update(extra)
    return env


def run_pi(*args: str, cwd: Path | None = None, timeout: float = 240,
           bytes_stdout: bool = False, **env_extra: str):
    """Run ``python -m pilang ARGS`` from the Pi checkout."""
    cmd = [PYTHON, "-m", "pilang", *args]
    return subprocess.run(
        cmd, cwd=str(cwd or PI_ROOT), timeout=timeout,
        capture_output=True, text=not bytes_stdout,
        env=pi_env(**env_extra))


def write_worker(path: Path, protocol_name: str, body: str) -> None:
    path.write_text(
        f'include "pi_worker.lambda"\ninclude "pi_schema_{protocol_name}.lambda"\n\n'
        + body, encoding="utf-8", newline="\n")


def fake_worker_c_source() -> str:
    return r"""
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <stdint.h>
#ifdef _WIN32
#include <io.h>
#include <fcntl.h>
#endif

static void put_hello(uint64_t hi, uint64_t lo) {
    unsigned char h[16] = {'L','M','B','1',2,0,0,0, 0,0,0,0, 24,0,0,0};
    unsigned char body[24];
    uint64_t version = 2;
    memcpy(body, &hi, 8);
    memcpy(body + 8, &lo, 8);
    memcpy(body + 16, &version, 8);
    fwrite(h, 1, 16, stdout);
    fwrite(body, 1, 24, stdout);
    fflush(stdout);
}

static void put_reply(long corr, const double *vals, long count) {
    unsigned char h[16] = {'L','M','B','1',1,0,0,0, 0,0,0,0, 0,0,0,0};
    uint32_t sid = (uint32_t)corr, n = (uint32_t)(count * 8);
    memcpy(h + 8, &sid, 4);
    memcpy(h + 12, &n, 4);
    fwrite(h, 1, 16, stdout);
    fwrite(vals, sizeof(double), (size_t)count, stdout);
    fflush(stdout);
}

int main(void) {
#ifdef _WIN32
    _setmode(_fileno(stdout), _O_BINARY);
#endif
    const char *mode = getenv("PI_FAKE_MODE");
    const char *hex = getenv("PI_FAKE_SCHEMA");
    uint64_t hi = 0, lo = 0;
    char line[512];
    long corr, op, nargs;
    if (!mode) mode = "bad_schema";
    if (hex && strlen(hex) >= 32) {
        char a[17], b[17];
        memcpy(a, hex, 16); a[16] = 0;
        memcpy(b, hex + 16, 16); b[16] = 0;
        hi = (uint64_t)strtoull(a, NULL, 16);
        lo = (uint64_t)strtoull(b, NULL, 16);
    }
    if (strcmp(mode, "bad_schema") == 0) {
        put_hello(0x1122334455667788ull, 0x99aabbccddeeff00ull);
        return 0;
    }
    put_hello(hi, lo);
    if (!fgets(line, sizeof(line), stdin)) return 0;
    corr = strtoll(line, NULL, 10);
    if (!fgets(line, sizeof(line), stdin)) return 0;
    op = strtoll(line, NULL, 10);
    if (!fgets(line, sizeof(line), stdin)) return 0;
    nargs = strtoll(line, NULL, 10);
    for (long i = 0; i < nargs; i++)
        if (!fgets(line, sizeof(line), stdin)) break;
    if (strcmp(mode, "stats_negative") == 0) {
        double vals[2] = {1.0, -1.0};
        put_reply(corr, vals, 2);
        return 0;
    }
    {
        double ok[1] = {0.0};
        put_reply(corr, ok, 1);
    }
    return 0;
}
"""


def fake_worker() -> str:
    """Compile (once per session) a C worker that speaks the LMB1 protocol."""
    global _session_fake_worker
    if _session_fake_worker:
        return _session_fake_worker
    require_toolchain()
    outdir = Path(tempfile.mkdtemp(prefix="pi-test-fake-worker-"))
    src = outdir / "fake_worker.c"
    src.write_text(fake_worker_c_source(), encoding="utf-8", newline="\n")
    exe = outdir / ("fake_worker.exe" if os.name == "nt" else "fake_worker")
    cc = os.environ.get("CC") or shutil.which("clang") or shutil.which("cc")
    proc = subprocess.run([cc, "-O1", str(src), "-o", str(exe)],
                          capture_output=True, text=True)
    if proc.returncode != 0:
        pytest.skip("cannot compile the fake worker: " + (proc.stderr or proc.stdout))
    _session_fake_worker = str(exe)
    return _session_fake_worker
