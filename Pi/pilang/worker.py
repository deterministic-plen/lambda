"""Worker toolchain support: schema hashing and Lambda compilation.

Process transport, framing, and supervision live in the C++ runtime
(``csrc/pi_runtime.cpp``).  This module is deliberately only the Python frontend
needed to build and identify worker executables.
"""

from __future__ import annotations

import hashlib
import os
import struct
import subprocess
import sys
import tempfile
from typing import Optional, Tuple

from . import syntax as S

HERE = os.path.dirname(os.path.abspath(__file__))
PI_LIB = os.path.join(HERE, "lib")


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
    cands = [explicit, os.environ.get("LAMBDA_HOME"),
             os.path.join(os.path.dirname(HERE), "..", "deterministic-plen", "lambda"),
             os.path.expanduser("~/deterministic-plen/lambda"),
             "/home/claude/deterministic-plen/lambda"]
    for c in cands:
        if c and os.path.exists(os.path.join(c, "lambda.py")):
            return os.path.abspath(c)
    raise ToolchainError(
        "cannot find the Lambda compiler (lambda.py); set LAMBDA_HOME or pass --lambda DIR")


def build_worker(src: str, proto: Optional[S.Protocol], *, lambda_home: Optional[str],
                 builddir: Optional[str]) -> str:
    """Compile a Lambda worker (or return it as-is if it is an executable / Python script).
    The protocol's schema hash is injected as `pi_schema_<Proto>.lambda`, which the worker
    includes, so its HELLO frame carries the hash of the protocol it was built against."""
    if not src.endswith(".lambda"):
        return src
    home = find_lambda_home(lambda_home)
    builddir = builddir or os.path.join(tempfile.gettempdir(), "pi-build")
    os.makedirs(builddir, exist_ok=True)

    h = hashlib.sha256()
    with open(src, "rb") as f:
        h.update(f.read())
    for lib in sorted(os.listdir(PI_LIB)):
        with open(os.path.join(PI_LIB, lib), "rb") as f:
            h.update(f.read())
    schema_dir = os.path.join(builddir, "schema")
    os.makedirs(schema_dir, exist_ok=True)
    if proto is not None:
        hi, lo = schema_hash(proto)
        h.update(f"{hi},{lo}".encode())
        with open(os.path.join(schema_dir, f"pi_schema_{proto.name}.lambda"), "w",
                  encoding="utf-8") as f:
            f.write(f"schema-hi → {hi}\nschema-lo → {lo}\n")
            # Readable op/error constants for workers; they do not participate
            # in the schema hash.
            for i, op in enumerate(proto.ops):
                name = "op-" + "".join(ch if ch.isalnum() or ch == "_" else "_"
                                       for ch in op.name)
                f.write(f"{name} → {i}\n")
            for code, err in proto.errors.items():
                name = "error-" + "".join(ch if ch.isalnum() or ch == "_" else "_"
                                          for ch in err.name)
                f.write(f"{name} → {code}\n")
    # pi_schema files are per-protocol; two protocols never share a name
    lam_hash = hashlib.sha256()
    for root in (home,):
        for fn in ("lambda.py", "compiler.py", "runtime.py", "typecheck.py"):
            p = os.path.join(root, fn)
            if os.path.exists(p):
                with open(p, "rb") as f:
                    lam_hash.update(f.read())
    h.update(lam_hash.digest())
    exe = os.path.join(builddir, f"{os.path.basename(src)[:-7]}-{h.hexdigest()[:12]}")
    if os.name == "nt":
        exe += ".exe"
    if os.path.exists(exe):
        return exe

    env = dict(os.environ)
    env.setdefault("PYTHONIOENCODING", "utf-8")
    # Lambda's driver links with a bare `clang -O3 ... -o exe`; on Linux libm must be asked
    # for explicitly.  clang honours CCC_OVERRIDE_OPTIONS, so we do not touch Lambda's source.
    if os.name != "nt":
        # clang on ELF needs libm and pthread; MSVC's linker resolves them itself.
        env["CCC_OVERRIDE_OPTIONS"] = "#+-lm +-lpthread"
    lp = [PI_LIB, schema_dir, os.path.dirname(os.path.abspath(src))]
    if env.get("LAMBDA_PATH"):
        lp.append(env["LAMBDA_PATH"])
    env["LAMBDA_PATH"] = os.pathsep.join(lp)
    tmp_exe = exe + ".tmp"
    cmd = [sys.executable, os.path.join(home, "lambda.py"), os.path.abspath(src),
           "-o", tmp_exe, "--input", "--data", "--no-warn-infix"]
    proc = subprocess.run(cmd, cwd=home, env=env, capture_output=True, text=True)
    if proc.returncode != 0:
        raise ToolchainError(f"Lambda failed to compile {src}:\n{(proc.stderr or proc.stdout).strip()}")
    produced = tmp_exe + ".exe" if os.name == "nt" else tmp_exe
    if not os.path.exists(produced):
        produced = tmp_exe
    os.replace(produced, exe)
    return exe
