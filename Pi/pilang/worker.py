"""Worker toolchain support: schema hashing and Lambda compilation.

Process transport, framing, and supervision live in the C++ runtime
(``csrc/pi_runtime.cpp``).  This module is deliberately only the Python frontend
needed to build and identify worker executables.

Build products are cached in the private per-user cache directory (PR.md 1.2 /
2.5 / 2.6 / P8):

* the cache key covers the worker source, Pi's worker library, the Lambda
  compiler sources, the Lambda standard library, the protocol schema and the
  *transitive* ``include`` closure of the worker -- not just four compiler files;
* each key gets its own directory, so two protocols that share a name but have
  different ops can never race on a shared ``pi_schema_<Proto>.lambda``;
* the executable is built to a ``mkstemp`` path and installed with
  ``os.replace``, so a concurrent/interrupted build cannot leave a partial
  binary at the final path.
"""

from __future__ import annotations

import functools
import glob
import hashlib
import os
import re
import shutil
import struct
import subprocess
import sys
from typing import Optional, Tuple

from . import syntax as S
from .cache import (CACHE_LAYOUT_VERSION, atomic_write, cache_root,
                    install_temp_output, is_regular_file, make_temp_output)

HERE = os.path.dirname(os.path.abspath(__file__))
PI_LIB = os.path.join(HERE, "lib")
PI_ROOT = os.path.dirname(HERE)

#: Bumped when the build invocation or layout changes.
WORKER_CACHE_VERSION = 3


# ------------------------------------------------------------ schema hashing

def schema_hash(proto: S.Protocol) -> Tuple[int, int]:
    """128-bit schema hash of a protocol, as two signed 64-bit words (spec 4.5)."""
    d = hashlib.sha256(proto.canonical.encode("utf-8")).digest()[:16]
    hi, lo = struct.unpack(">qq", d)
    return hi, lo


def schema_hex(proto: S.Protocol) -> str:
    hi, lo = schema_hash(proto)
    return f"{hi & (2**64 - 1):016x}{lo & (2**64 - 1):016x}"


# ------------------------------------------------------------ Lambda toolchain

class ToolchainError(Exception):
    pass


def find_lambda_home(explicit: Optional[str] = None) -> str:
    # The repo layout keeps Pi/ next to the Lambda compiler; try that first so
    # `pi run` works in a checkout without LAMBDA_HOME.
    repo_home = os.path.dirname(PI_ROOT)
    cands = [explicit, os.environ.get("LAMBDA_HOME"),
             repo_home,
             os.path.join(os.path.dirname(HERE), "..", "deterministic-plen", "lambda"),
             os.path.expanduser("~/deterministic-plen/lambda"),
             "/home/claude/deterministic-plen/lambda"]
    for c in cands:
        if c and os.path.exists(os.path.join(c, "lambda.py")):
            return os.path.abspath(c)
    raise ToolchainError(
        "cannot find the Lambda compiler (lambda.py); set LAMBDA_HOME or pass --lambda DIR")


def _read_bytes(path: str) -> bytes:
    with open(path, "rb") as f:
        return f.read()


def _iter_compiler_sources(home: str):
    """Every ``*.py`` next to lambda.py, plus the bundled Lambda library."""
    for path in sorted(glob.glob(os.path.join(home, "*.py"))):
        if os.path.isfile(path):
            yield path
    lib = os.path.join(home, "lib")
    if os.path.isdir(lib):
        for path in sorted(glob.glob(os.path.join(lib, "**", "*.lambda"), recursive=True)):
            yield path


def _iter_pi_lib_sources():
    for path in sorted(glob.glob(os.path.join(PI_LIB, "**", "*"), recursive=True)):
        if os.path.isfile(path):
            yield path


@functools.lru_cache(maxsize=8)
def _compiler_fingerprint(home: str) -> bytes:
    """Content hash of the whole Lambda compiler tree that builds workers."""
    h = hashlib.sha256()
    for path in _iter_compiler_sources(home):
        h.update(os.path.relpath(path, home).replace("\\", "/").encode("utf-8"))
        h.update(b"\0")
        h.update(_read_bytes(path))
        h.update(b"\0")
    # The generated binary also depends on the C compiler Lambda invokes.
    for name in ("clang", "cc"):
        cc = os.environ.get("CC") or shutil.which(name)
        if cc:
            h.update(f"cc={cc}".encode())
            try:
                out = subprocess.run([cc, "--version"], capture_output=True, text=True,
                                     timeout=30).stdout
                h.update(out.encode("utf-8", "replace"))
            except (OSError, subprocess.SubprocessError):
                pass
            break
    return h.digest()


_INCLUDE_RE = re.compile(r'^\s*include\s+"([^"]+)"', re.MULTILINE)


def _include_search_paths(home: str) -> list:
    paths = [PI_LIB, os.path.dirname(HERE), home, os.path.join(home, "lib")]
    for env_name in ("LAMBDA_PATH", "LAMBDA_LIB"):
        for entry in os.environ.get(env_name, "").split(os.pathsep):
            if entry:
                paths.append(entry)
    return [os.path.abspath(p) for p in paths if p]


def _resolve_include(name: str, base: str, search: list) -> Optional[str]:
    raw = os.path.expanduser(name)
    if os.path.isabs(raw):
        return raw if os.path.isfile(raw) else None
    candidates = [os.path.join(base, raw)]
    if not raw.lower().endswith(".lambda"):
        candidates.append(os.path.join(base, raw + ".lambda"))
    for directory in search:
        candidates.append(os.path.join(directory, raw))
        if not raw.lower().endswith(".lambda"):
            candidates.append(os.path.join(directory, raw + ".lambda"))
    for cand in candidates:
        cand = os.path.abspath(cand)
        if os.path.isfile(cand):
            return cand
    return None


def include_closure(src: str, home: str) -> list:
    """All files transitively ``include``-d by *src*, as sorted absolute paths.

    Unresolvable includes are represented by their literal name (they would make
    the build fail, but a marker still keeps the key sensitive to them).
    """
    search = _include_search_paths(home)
    seen = set()
    found = set()
    stack = [os.path.abspath(src)]
    while stack:
        path = stack.pop()
        key = os.path.normcase(os.path.realpath(path))
        if key in seen:
            continue
        seen.add(key)
        try:
            text = _read_bytes(path).decode("utf-8", "replace")
        except OSError:
            found.add(path)
            continue
        found.add(path)
        for name in _INCLUDE_RE.findall(text):
            target = _resolve_include(name, os.path.dirname(path), search)
            if target is None:
                found.add("include:" + name)
            else:
                stack.append(target)
    return sorted(found)


def worker_cache_key(src: str, proto: Optional[S.Protocol], home: str) -> str:
    """Complete content key for a worker build (PR.md 2.5).

    Covers the worker source, its transitive include closure, Pi's worker
    library, every Lambda compiler ``*.py`` file, the Lambda standard library,
    the C compiler version and the protocol schema (ops, errors, purity).
    """
    h = hashlib.sha256()
    h.update(f"pi-worker-cache-v{WORKER_CACHE_VERSION}-layout{CACHE_LAYOUT_VERSION}".encode())
    h.update(_read_bytes(src))
    h.update(b"\0")
    for path in include_closure(src, home):
        h.update(path.replace("\\", "/").encode("utf-8", "surrogateescape"))
        h.update(b"\0")
        if os.path.isfile(path):
            h.update(_read_bytes(path))
        h.update(b"\0")
    for path in _iter_pi_lib_sources():
        h.update(os.path.relpath(path, PI_LIB).replace("\\", "/").encode("utf-8"))
        h.update(_read_bytes(path))
        h.update(b"\0")
    h.update(_compiler_fingerprint(home))
    if proto is not None:
        hi, lo = schema_hash(proto)
        h.update(proto.canonical.encode("utf-8"))
        h.update(f"\0{hi},{lo}".encode())
        h.update(f"\0v{CACHE_LAYOUT_VERSION}".encode())
    return h.hexdigest()


def _safe_stem(name: str) -> str:
    stem = re.sub(r"[^A-Za-z0-9_.-]+", "_", name).strip("._")[:48]
    return stem or "worker"


def _write_schema(schema_dir: str, proto: S.Protocol) -> None:
    """Write this build's schema file atomically inside its private directory."""
    hi, lo = schema_hash(proto)
    lines = [f"schema-hi \u2192 {hi}\n", f"schema-lo \u2192 {lo}\n"]
    for i, op in enumerate(proto.ops):
        name = "op-" + "".join(ch if ch.isalnum() or ch == "_" else "_" for ch in op.name)
        lines.append(f"{name} \u2192 {i}\n")
    for code, err in proto.errors.items():
        name = "error-" + "".join(ch if ch.isalnum() or ch == "_" else "_" for ch in err.name)
        lines.append(f"{name} \u2192 {code}\n")
    data = "".join(lines).encode("utf-8")
    os.makedirs(schema_dir, mode=0o700, exist_ok=True)
    path = os.path.join(schema_dir, f"pi_schema_{_safe_stem(proto.name)}.lambda")
    atomic_write(path, data)


def _default_builddir() -> str:
    return os.path.join(cache_root(), "workers")


def build_worker(src: str, proto: Optional[S.Protocol], *, lambda_home: Optional[str],
                 builddir: Optional[str]) -> str:
    """Compile a Lambda worker (or return it as-is if it is an executable / Python script).

    The protocol's schema hash is injected as ``pi_schema_<Proto>.lambda``,
    which the worker includes, so its HELLO frame carries the hash of the
    protocol it was built against.
    """
    if not src.endswith(".lambda"):
        return os.path.abspath(src)
    src = os.path.abspath(src)
    home = find_lambda_home(lambda_home)
    explicit_builddir = builddir is not None
    builddir = builddir or _default_builddir()
    if explicit_builddir:
        os.makedirs(builddir, exist_ok=True)
    else:
        os.makedirs(builddir, mode=0o700, exist_ok=True)

    key = worker_cache_key(src, proto, home)
    build_dir = os.path.join(builddir, key)
    if explicit_builddir:
        os.makedirs(build_dir, exist_ok=True)
    else:
        os.makedirs(build_dir, mode=0o700, exist_ok=True)
    stem = _safe_stem(os.path.basename(src)[:-7])
    exe = os.path.join(build_dir, stem)
    if os.name == "nt":
        exe += ".exe"
    if is_regular_file(exe):
        return exe

    schema_dir = os.path.join(build_dir, "schema")
    if proto is not None:
        _write_schema(schema_dir, proto)

    env = dict(os.environ)
    env.setdefault("PYTHONIOENCODING", "utf-8")
    # Lambda's driver links with a bare `clang -O3 ... -o exe`; on Linux libm must be asked
    # for explicitly.  clang honours CCC_OVERRIDE_OPTIONS, so we do not touch Lambda's source.
    if os.name != "nt":
        # clang on ELF needs libm and pthread; MSVC's linker resolves them itself.
        env["CCC_OVERRIDE_OPTIONS"] = "#+-lm +-lpthread"
    lp = [PI_LIB, schema_dir, os.path.dirname(src)]
    if env.get("LAMBDA_PATH"):
        lp.append(env["LAMBDA_PATH"])
    env["LAMBDA_PATH"] = os.pathsep.join(lp)

    tmp_exe = make_temp_output(build_dir, stem=stem)
    cmd = [sys.executable, os.path.join(home, "lambda.py"), src,
           "-o", tmp_exe, "--input", "--data", "--no-warn-infix"]
    try:
        proc = subprocess.run(cmd, cwd=home, env=env, capture_output=True, text=True)
        if proc.returncode != 0:
            raise ToolchainError(
                f"Lambda failed to compile {src}:\n{(proc.stderr or proc.stdout).strip()}")
        produced = tmp_exe + ".exe" if os.name == "nt" else tmp_exe
        if not os.path.exists(produced):
            produced = tmp_exe
        if not os.path.exists(produced):
            raise ToolchainError(f"Lambda reported success but produced no output for {src}")
        install_temp_output(produced, exe)
        return exe
    finally:
        for leftover in (tmp_exe, tmp_exe + ".exe"):
            try:
                os.remove(leftover)
            except OSError:
                pass
