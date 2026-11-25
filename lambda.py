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
from runtime_ext import RUNTIME_EXTRA_C
import semantics
import type_algebra
import typecheck
from typecheck import show_type


def _cache_dir() -> Optional[str]:
    """Per-user runtime-object cache, or None when it cannot be trusted."""
    if os.name == "nt":
        base = os.environ.get("LOCALAPPDATA") or os.path.expanduser("~")
    else:
        base = os.environ.get("XDG_CACHE_HOME") or os.path.join(
            os.path.expanduser("~"), ".cache")
    path = os.path.join(base, "lambda")
    try:
        os.makedirs(path, mode=0o700, exist_ok=True)
        if os.name != "nt":
            st = os.stat(path)
            if st.st_uid != os.getuid():
                return None
            os.chmod(path, 0o700)
        return path
    except OSError:
        return None


def _runtime_object(workdir: str, clang: str, atomic_rc: bool = False) -> str:
    """Compile the C runtime, reusing a per-user cache entry when safe.

    Compiling the runtime per build cost about a second per program.  The
    cache is keyed by the full runtime source and flags, created mode 0700 in
    a per-user directory, and installed with ``os.replace`` so concurrent
    builds cannot observe a partial object.  If the cache is unavailable the
    old private-workdir build is used.
    """

    variant = "atomic" if atomic_rc else "plain"
    key = hashlib.sha256(
        (RUNTIME_C + RUNTIME_EXTRA_C + "\x00" + variant + "\x00" + clang + "\x00"
         + sys.platform).encode("utf-8")).hexdigest()[:32]
    cache = _cache_dir()
    obj = os.path.join(cache, f"runtime-{key}.o") if cache else None
    if obj and os.path.isfile(obj):
        return obj
    src = os.path.join(workdir, f"runtime-{variant}.c")
    with open(src, "w", encoding="utf-8") as f:
        f.write(RUNTIME_C)
        f.write("\n")
        f.write(RUNTIME_EXTRA_C)
    cmd = [clang, "-O3", "-Wno-override-module"]
    if atomic_rc:
        cmd.append("-DLAMBDA_ATOMIC_RC=1")
    if os.name != "nt":
        cmd.append("-pthread")
    tmp_obj = None
    if cache:
        fd, tmp_obj = tempfile.mkstemp(dir=cache, prefix="runtime-", suffix=".tmp")
        os.close(fd)
        out_obj = tmp_obj
    else:
        out_obj = os.path.join(workdir, f"runtime-{variant}.o")
    cmd.extend(["-c", src, "-o", out_obj])
    proc = subprocess.run(cmd, capture_output=True, text=True)
    if proc.returncode != 0:
        if tmp_obj:
            try:
                os.unlink(tmp_obj)
            except OSError:
                pass
        raise CompileError(
            "failed to compile the C runtime:\n" + proc.stderr.strip())
    if tmp_obj and obj:
        os.replace(tmp_obj, obj)
        return obj
    return out_obj


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


def build(input_file: str, output: str, *, dynamic: bool = False,
          allow_dynamic: bool = False, keep_ir: bool = False,
          use_musttail: bool = True, debug: bool = False,
          dump_ir: bool = False, allow_input: bool = False,
          warn_infix: bool = True, allow_data: bool = False,
          search_paths=None, exit_code: bool = False, status=None,
          halt_warnings: bool = True) -> str:
    if not os.path.exists(input_file):
        raise CompileError(f"file '{input_file}' not found")

    program = load_program(input_file, search_paths=search_paths,
                           warn_infix=warn_infix)
    program = Resolver().resolve_program(program)

    # Sharing needs resolved bindings (for purity/arity), but not types.
    program = share_common_subexpressions(program)
    program = Resolver().resolve_program(program)

    # Type checking is always on: there is no opt-out.  ``--allow-dynamic``
    # only chooses what to do *after* inference has failed (reject by default,
    # or warn and use the boxed backend); ``--dynamic`` forces the boxed
    # backend for a program that already type-checks.
    is_dynamic = False
    try:
        result_type = typecheck.infer_program(program)
        if debug:
            print(f"type of program: {show_type(result_type)}", file=sys.stderr)
        is_dynamic = dynamic
    except TypeError_ as exc:
        if not allow_dynamic:
            raise
        is_dynamic = True
        if status is not None:
            status["fallback"] = True
        print(
            "warning: type inference failed"
            + (f" ({exc.message})" if exc.message else "")
            + "; compiling in dynamic (boxed) mode because --allow-dynamic "
              "was given",
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

    # Optional, warning-only termination analysis (a bounded, strictly
    # decreasing rank variable).  It never rejects a program.
    semantics.check_halting(program, input_file, enabled=halt_warnings)

    # Infer non-consuming parameters for functions whose parameters never
    # escape.  Explicit & annotations are preserved; `: linear` opts out.
    if not is_dynamic:
        semantics.infer_borrows(program)

    compiler = Compiler(dynamic=is_dynamic, use_musttail=use_musttail, debug=debug,
                        lambda_purity=lambda_purity, allow_input=allow_input,
                        allow_data=allow_data, exit_code=exit_code)
    ir = compiler.compile_program(program)

    exe = output
    if sys.platform == "win32" and not exe.lower().endswith(".exe"):
        exe += ".exe"

    if keep_ir:
        with open(os.path.splitext(exe)[0] + ".ll", "w", encoding="utf-8") as f:
            f.write(ir)

    if dump_ir:
        print(ir)

    clang = shutil.which("clang")
    if clang is None:
        raise CompileError("clang was not found on PATH")

    workdir = None
    try:
        workdir = tempfile.mkdtemp(prefix="lambda-build-")
        ir_file = os.path.join(workdir, "program.ll")
        with open(ir_file, "w", encoding="utf-8") as f:
            f.write(ir)
        runtime_obj = _runtime_object(workdir, clang, compiler.atomic_rc)
        cmd = _link_command(clang, ir_file, runtime_obj, exe)
        proc = subprocess.run(cmd, capture_output=True, text=True)
        if proc.returncode != 0 and "-fuse-ld=lld" in cmd:
            # lld is optional.  If it is present but broken (or the platform
            # naming does not match), retry with the default linker and keep
            # the lld diagnostic only if that also fails.
            plain = [a for a in cmd if a != "-fuse-ld=lld"]
            retry = subprocess.run(plain, capture_output=True, text=True)
            if retry.returncode == 0:
                return exe
            raise CompileError(
                "clang failed to build the program:\n"
                + (retry.stderr.strip() or retry.stdout.strip())
                + "\n(lld attempt: "
                + (proc.stderr.strip() or proc.stdout.strip()) + ")")
        if proc.returncode != 0:
            raise CompileError(
                "clang failed to build the program:\n"
                + (proc.stderr.strip() or proc.stdout.strip()))
        return exe
    finally:
        if workdir is not None:
            shutil.rmtree(workdir, ignore_errors=True)


def _link_command(clang: str, ir_file: str, runtime_obj: str, exe: str):
    """Build the clang link command, using lld only when it is available.

    ``-fuse-ld=lld`` was hard-coded, so a machine with clang but without lld
    failed with an unexplained linker error.  Probe for the platform's lld
    driver and fall back to the default linker; ``build`` also retries without
    the flag if the probe was optimistic.
    """
    cmd = [clang, "-O3", "-Wno-override-module", ir_file, runtime_obj,
           "-o", exe]
    if any(shutil.which(n) for n in ("lld-link", "ld.lld", "lld")):
        cmd.insert(2, "-fuse-ld=lld")
    if os.name != "nt":
        # The C runtime uses libm; glibc keeps it outside libc.
        cmd.extend(["-lm", "-pthread"])
    else:
        # GUI windows use the Win32 API; the rest of the runtime is libc-only.
        cmd.extend(["-luser32", "-lgdi32"])
    if sys.platform.startswith("linux"):
        # Older glibc releases require libdl for dlopen/dlsym.
        cmd.append("-ldl")
    return cmd


# The compiler is a recursive-descent translator: long `else if` chains, deep
# applications (`(- (- (- ...)))`) and nested blocks consume several Python
# frames per source node.  The default 1000-frame limit rejects legitimate
# programs with a misleading RecursionError, so the driver raises it up front
# and reports genuine overflow as a compile error instead.
COMPILER_RECURSION_LIMIT = 40000


COMPILER_THREAD_STACK = 128 * 1024 * 1024


def main(argv=None) -> int:
    return _main_in_big_stack(argv)


def _main_in_big_stack(argv=None):
    """Run the compiler on a thread with a large stack.

    The parser, resolver, type checker and code generator are all recursive
    over the AST, so long ``else if`` dispatch chains (thousands of arms) need
    far more than the interpreter's default 8 MB C stack even when Python's
    own recursion limit is raised.  CPython 3.10 cannot run Python code on an
    enlarged main-thread stack, so the work is moved to a worker thread.
    """
    import threading

    box = {}

    def run():
        try:
            box["code"] = _main(argv)
        except SystemExit as exc:  # pragma: no cover - defensive
            box["code"] = exc.code if isinstance(exc.code, int) else 1

    try:
        threading.stack_size(COMPILER_THREAD_STACK)
    except (ValueError, RuntimeError):  # pragma: no cover - platform limits
        return _main(argv)
    thread = threading.Thread(target=run, name="lambda-compile")
    thread.start()
    thread.join()
    return box.get("code", 1)


def _main(argv=None) -> int:
    if sys.getrecursionlimit() < COMPILER_RECURSION_LIMIT:
        sys.setrecursionlimit(COMPILER_RECURSION_LIMIT)
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8")
        except (AttributeError, ValueError):
            pass
    ap = argparse.ArgumentParser(
        description="Compile Lambda Calculus to native executables via LLVM.")
    ap.add_argument("input_file", help="Path to the .lambda source file")
    ap.add_argument("-o", "--output", default="lambda_exe", help="Output executable name")
    ap.add_argument("--allow-dynamic", action="store_true",
                    help="If type inference fails, warn and compile with the "
                         "boxed backend instead of rejecting the program")
    ap.add_argument("--dynamic", action="store_true",
                    help="Use the fully boxed backend for a type-checked "
                         "program (type checking is still performed)")
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
    ap.add_argument("--no-halt-warnings", action="store_false",
                    dest="halt_warnings", default=True,
                    help="Suppress the may-not-halt recursion warnings")
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
        except RecursionError:
            print(
                f"{args.input_file}: layout: compiler stack overflow while "
                f"reading declarations (deeply nested data type)",
                file=sys.stderr)
            return 1
        except (LambdaError, ValueError, OSError) as exc:
            print(f"lambda: layout: {exc}", file=sys.stderr)
            return 1
    status = {}
    try:
        exe = build(
            args.input_file,
            args.output,
            dynamic=args.dynamic,
            allow_dynamic=args.allow_dynamic,
            keep_ir=args.keep_ir,
            use_musttail=not args.no_musttail,
            debug=args.debug,
            dump_ir=args.dump_ir,
            allow_input=args.input,
            warn_infix=args.warn_infix,
            allow_data=args.data,
            search_paths=args.lib,
            exit_code=args.exit_code,
            status=status,
            halt_warnings=args.halt_warnings,
        )
    except RecursionError:
        print(
            f"{args.input_file}: compile error: compiler stack overflow while lowering "
            "this program (deeply nested expressions, or an error raised inside a "
            "nested/untyped branch)",
            file=sys.stderr,
        )
        return 1
    except LambdaError as exc:
        if exc.loc and exc.loc.line:
            where = f"{exc.loc.source}:{exc.loc.line}:{exc.loc.col}"
        else:
            where = args.input_file
        print(f"{where}: {exc.kind}: {exc.message}", file=sys.stderr)
        return 1
    except Exception as exc:  # pragma: no cover - internal diagnostics
        # Compiler bugs must be reportable.  A one-line "compilation error"
        # hid the traceback and source location that a bug report needs.
        print(f"{args.input_file}: internal compiler error: {exc}", file=sys.stderr)
        if args.debug:
            raise
        print(
            "hint: rerun with --debug to see the Python traceback; this is a "
            "compiler bug, not necessarily a problem in your program",
            file=sys.stderr,
        )
        import traceback
        traceback.print_exc(file=sys.stderr)
        return 1

    shown = os.path.relpath(exe, os.getcwd())
    if not shown.startswith("."):
        shown = os.path.join(".", shown)
    if status.get("fallback"):
        print(f"Built with the dynamic fallback (type checking failed; "
              f"--allow-dynamic). Run {shown}")
    else:
        mode = " (boxed --dynamic backend)" if args.dynamic else ""
        print(f"Success!{mode} Run {shown}")
    return 0


if __name__ == "__main__":
    sys.exit(main())

