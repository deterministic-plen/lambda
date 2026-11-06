"""Command-line driver: .lambda -> native executable via LLVM IR + C runtime."""

from __future__ import annotations

import argparse
import hashlib
import os
import shutil
import subprocess
import sys
import tempfile

import analysis
from analysis import (
    compute_captures, compute_lambda_purity, share_common_subexpressions,
    validate_parallel_lambdas,
)
from compiler import Compiler
from errors import CompileError, LambdaError, TypeError_
from loader import load_program
from resolve import Resolver
from runtime import RUNTIME_C
import typecheck
from typecheck import show_type


def _runtime_object(workdir: str, clang: str, atomic_rc: bool = False) -> str:
    """Compile (or reuse) the C runtime object."""

    digest = hashlib.sha256(RUNTIME_C.encode("utf-8")).hexdigest()[:16]
    cache_dir = os.path.join(tempfile.gettempdir(), "lambda-runtime-cache")
    os.makedirs(cache_dir, exist_ok=True)
    variant = "atomic" if atomic_rc else "plain"
    obj = os.path.join(cache_dir, f"runtime-{digest}-{variant}.o")
    if os.path.exists(obj) and os.path.getmtime(obj) >= _runtime_mtime():
        return obj
    src = os.path.join(workdir, "runtime.c")
    with open(src, "w", encoding="utf-8") as f:
        f.write(RUNTIME_C)
    cmd = [clang, "-O3", "-Wno-override-module"]
    if atomic_rc:
        cmd.append("-DLAMBDA_ATOMIC_RC=1")
    cmd.extend(["-c", src, "-o", obj])
    proc = subprocess.run(cmd, capture_output=True, text=True)
    if proc.returncode != 0:
        raise CompileError(
            "failed to compile the C runtime:\n" + proc.stderr.strip())
    return obj


def _runtime_mtime() -> float:
    try:
        return os.path.getmtime(__file__)
    except OSError:
        return 0.0


def build(input_file: str, output: str, *, strict: bool = False,
          dynamic: bool = False, keep_ir: bool = False,
          use_musttail: bool = True, debug: bool = False,
          dump_ir: bool = False, allow_input: bool = False,
          warn_infix: bool = True, allow_data: bool = False) -> str:
    if not os.path.exists(input_file):
        raise CompileError(f"file '{input_file}' not found")

    program = load_program(input_file, warn_infix=warn_infix)
    program = Resolver().resolve_program(program)

    # Sharing needs resolved bindings (for purity/arity), but not types.
    program = share_common_subexpressions(program)
    program = Resolver().resolve_program(program)

    is_dynamic = dynamic
    inference_error = None
    if not is_dynamic:
        try:
            result_type = typecheck.infer_program(program)
            if debug:
                print(f"type of program: {show_type(result_type)}", file=sys.stderr)
        except TypeError_ as exc:
            inference_error = exc
            if strict:
                raise
            is_dynamic = True
            print(
                "warning: type inference failed"
                + (f" ({exc.message})" if exc.message else "")
                + "; compiling in dynamic (boxed) mode",
                file=sys.stderr,
            )

    compute_captures(program)
    lambda_purity = compute_lambda_purity(program)
    validate_parallel_lambdas(program, lambda_purity)

    compiler = Compiler(dynamic=is_dynamic, use_musttail=use_musttail, debug=debug,
                        lambda_purity=lambda_purity, allow_input=allow_input,
                        allow_data=allow_data)
    ir = compiler.compile_program(program)

    exe = output
    if sys.platform == "win32" and not exe.lower().endswith(".exe"):
        exe += ".exe"

    workdir = tempfile.mkdtemp(prefix="lambda-build-")
    ir_file = os.path.join(workdir, "program.ll")
    with open(ir_file, "w", encoding="utf-8") as f:
        f.write(ir)

    if keep_ir:
        with open(os.path.splitext(exe)[0] + ".ll", "w", encoding="utf-8") as f:
            f.write(ir)

    if dump_ir:
        print(ir)

    clang = shutil.which("clang")
    if clang is None:
        raise CompileError("clang was not found on PATH")

    runtime_obj = _runtime_object(workdir, clang, compiler.uses_parallel)
    cmd = [clang, "-O3", "-Wno-override-module", ir_file, runtime_obj, "-o", exe]
    proc = subprocess.run(cmd, capture_output=True, text=True)
    if proc.returncode != 0:
        raise CompileError(
            "clang failed to build the program:\n"
            + (proc.stderr.strip() or proc.stdout.strip()))
    return exe


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(
        description="Compile Lambda Calculus to native executables via LLVM.")
    ap.add_argument("input_file", help="Path to the .lambda source file")
    ap.add_argument("-o", "--output", default="lambda_exe", help="Output executable name")
    ap.add_argument("--strict", action="store_true",
                    help="Reject programs whose types cannot be inferred")
    ap.add_argument("--dynamic", action="store_true",
                    help="Skip type inference and use the fully boxed backend")
    ap.add_argument("--keep-ir", action="store_true",
                    help="Keep the generated .ll file next to the executable")
    ap.add_argument("--dump-ir", action="store_true",
                    help="Print the generated LLVM IR to stdout")
    ap.add_argument("--no-musttail", action="store_true",
                    help="Disable musttail emission (debugging)")
    ap.add_argument("--data", action="store_true",
                    help="Enable binary data output (write-bytes/write-f64s/write-i64s); suppress the final print")
    ap.add_argument("--input", action="store_true",
                    help="Enable terminal input primitives (read-line/read-int/read-float)")
    ap.add_argument("--no-warn-infix", action="store_false",
                    dest="warn_infix", default=True,
                    help="Suppress the likely-infix warning for z == 1-style applications")
    ap.add_argument("--debug", action="store_true",
                    help="Print the inferred program type")
    args = ap.parse_args(argv)

    try:
        exe = build(
            args.input_file,
            args.output,
            strict=args.strict,
            dynamic=args.dynamic,
            keep_ir=args.keep_ir,
            use_musttail=not args.no_musttail,
            debug=args.debug,
            dump_ir=args.dump_ir,
            allow_input=args.input,
            warn_infix=args.warn_infix,
            allow_data=args.data,
        )
    except LambdaError as exc:
        if exc.loc and exc.loc.line:
            where = f"{exc.loc.source}:{exc.loc.line}:{exc.loc.col}"
        else:
            where = args.input_file
        print(f"{where}: {exc.kind}: {exc.message}", file=sys.stderr)
        return 1
    except Exception as exc:  # pragma: no cover - internal diagnostics
        print(f"compilation error: {exc}", file=sys.stderr)
        if args.debug:
            raise
        return 1

    print(f"Success! Run ./{os.path.basename(exe)}")
    return 0


if __name__ == "__main__":
    sys.exit(main())

