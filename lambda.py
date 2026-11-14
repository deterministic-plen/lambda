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
from lambda_ast import Lam, walk
from loader import load_program
from resolve import Resolver
from runtime import RUNTIME_C
import type_algebra
import typecheck
from typecheck import show_type


def _runtime_object(workdir: str, clang: str, atomic_rc: bool = False) -> str:
    """Compile the C runtime into this build's private work directory.

    The old predictable /tmp cache let another local user pre-place an object
    that was later linked into generated executables.  Compiling per build costs
    little and removes that trust boundary entirely.
    """
    variant = "atomic" if atomic_rc else "plain"
    obj = os.path.join(workdir, f"runtime-{variant}.o")
    src = os.path.join(workdir, f"runtime-{variant}.c")
    with open(src, "w", encoding="utf-8") as f:
        f.write(RUNTIME_C)
    cmd = [clang, "-O3", "-Wno-override-module"]
    if atomic_rc:
        cmd.append("-DLAMBDA_ATOMIC_RC=1")
    if os.name != "nt":
        cmd.append("-pthread")
    cmd.extend(["-c", src, "-o", obj])
    proc = subprocess.run(cmd, capture_output=True, text=True)
    if proc.returncode != 0:
        raise CompileError(
            "failed to compile the C runtime:\n" + proc.stderr.strip())
    return obj


def data_layouts(input_file: str) -> int:
    program = load_program(input_file)
    decls = getattr(program, "data_decls", [])
    if not decls:
        print("no data declarations")
        return 0
    for d in decls:
        parts = []
        for v in d.variants:
            if v.fields:
                fields = ", ".join(f"{f.name}: {f.type_text}" for f in v.fields)
                parts.append(f"{v.name} ({fields})")
            else:
                parts.append(v.name)
        params = "<" + ", ".join(d.params) + ">" if d.params else ""
        text = f"data {d.name}{params} = " + " | ".join(parts)
        alg = type_algebra.parse_data(text)
        type_algebra.validate_decl(alg)
        node = alg.body()
        lay = type_algebra.layout(node)
        print(f"{d.name}{params}: {type_algebra.pretty(type_algebra.normalize(node))} -> {lay.kind}")
    return 0


def build(input_file: str, output: str, *, strict: bool = False,
          dynamic: bool = False, keep_ir: bool = False,
          use_musttail: bool = True, debug: bool = False,
          dump_ir: bool = False, allow_input: bool = False,
          warn_infix: bool = True, allow_data: bool = False,
          search_paths=None, exit_code: bool = False) -> str:
    if not os.path.exists(input_file):
        raise CompileError(f"file '{input_file}' not found")

    program = load_program(input_file, search_paths=search_paths,
                           warn_infix=warn_infix)
    program = Resolver().resolve_program(program)

    # Sharing needs resolved bindings (for purity/arity), but not types.
    program = share_common_subexpressions(program)
    program = Resolver().resolve_program(program)

    is_dynamic = dynamic
    if not is_dynamic:
        try:
            result_type = typecheck.infer_program(program)
            if debug:
                print(f"type of program: {show_type(result_type)}", file=sys.stderr)
        except TypeError_ as exc:
            if strict:
                raise
            is_dynamic = True
            print(
                "warning: type inference failed"
                + (f" ({exc.message})" if exc.message else "")
                + "; compiling in dynamic (boxed) mode",
                file=sys.stderr,
            )

    if is_dynamic:
        # The dynamic backend has a uniform boxed ABI, so borrowed parameters
        # would otherwise be retained twice or released by the wrong frame.
        for node in walk(program):
            if isinstance(node, Lam):
                node.param_borrows = [False] * len(node.params)
                for b in node.param_bindings:
                    b.borrowed = False

    compute_captures(program)
    lambda_purity = compute_lambda_purity(program)
    validate_parallel_lambdas(program, lambda_purity)

    compiler = Compiler(dynamic=is_dynamic, use_musttail=use_musttail, debug=debug,
                        lambda_purity=lambda_purity, allow_input=allow_input,
                        allow_data=allow_data, exit_code=exit_code)
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

    try:
        runtime_obj = _runtime_object(workdir, clang, compiler.atomic_rc)
        cmd = [clang, "-O3", "-fuse-ld=lld", "-Wno-override-module", ir_file,
               runtime_obj, "-o", exe]
        if os.name != "nt":
            # The C runtime uses libm; glibc keeps it outside libc.
            cmd.append("-lm")
            cmd.append("-pthread")
        if sys.platform.startswith("linux"):
            # Older glibc releases require libdl for dlopen/dlsym.
            cmd.append("-ldl")
        proc = subprocess.run(cmd, capture_output=True, text=True)
        if proc.returncode != 0:
            raise CompileError(
                "clang failed to build the program:\n"
                + (proc.stderr.strip() or proc.stdout.strip()))
        return exe
    finally:
        shutil.rmtree(workdir, ignore_errors=True)


def main(argv=None) -> int:
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8")
        except (AttributeError, ValueError):
            pass
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
    ap.add_argument("-L", "--lib", action="append", default=[], metavar="DIR",
                    help="add a library/include directory (repeatable)")
    ap.add_argument("--no-warn-infix", action="store_false",
                    dest="warn_infix", default=True,
                    help="Suppress the likely-infix warning for z == 1-style applications")
    ap.add_argument("--debug", action="store_true",
                    help="Print the inferred program type")
    ap.add_argument("--exit-code", action="store_true", dest="exit_code",
                    help="require an Int program result and use it as the process exit status (no final print)")
    ap.add_argument("--layout", action="store_true",
                    help="parse data declarations and print their type-algebra layouts; do not compile")
    args = ap.parse_args(argv)

    if args.layout:
        try:
            return data_layouts(args.input_file)
        except (LambdaError, ValueError, OSError) as exc:
            print(f"lambda: layout: {exc}", file=sys.stderr)
            return 1
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
            search_paths=args.lib,
            exit_code=args.exit_code,
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

