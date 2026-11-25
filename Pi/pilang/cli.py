"""The `pi` command: check | build | run | test | analyze  (spec 15.3)."""

from __future__ import annotations

import argparse
import glob
import json
import os
import sys
import subprocess
from typing import List

from . import syntax as S
from .cache import CacheError
from .checker import check, walk
from .errors import PiCompileError
from .frontend import run_frontend
from .parser import load_module
from .worker import ToolchainError, build_worker, find_lambda_home, schema_hex


def _load_checked(path: str):
    mod = load_module(path)
    return mod, check(mod)


def _load(path: str, quiet: bool = False):
    # P5: parse/check on the large frontend stack; deep but flat programs must
    # not fail with a bare RecursionError.
    mod, diags = run_frontend(lambda: _load_checked(path))
    errs = [d for d in diags if d.severity == "error"]
    if not quiet:
        for d in diags:
            print(f"{os.path.basename(path)}: {d}" if d.loc is None else str(d), file=sys.stderr)
    return mod, diags, errs


def _walk_all(node):
    """Materialise a recursive walk on the frontend stack (P5)."""
    return run_frontend(lambda: list(walk(node)))


def cmd_check(a) -> int:
    try:
        mod, diags, errs = _load(a.file)
    except PiCompileError as e:
        print(e.format(), file=sys.stderr)
        return 2
    except RuntimeError as e:
        print(f"pi check: {e}", file=sys.stderr)
        return 1
    n = (len(mod.protocols), len(mod.elems), len(mod.systems), len(mod.programs))
    print(f"{a.file}: {n[0]} protocol(s), {n[1]} element(s), {n[2]} system(s), {n[3]} program(s); "
          f"{len(errs)} error(s), {len(diags) - len(errs)} warning(s)")
    return 1 if errs else 0


def cmd_cpp(a) -> int:
    """Run through the C++ AST interpreter (Python is frontend only)."""
    from .cpp import run_cpp
    try:
        return run_cpp(a.file, outdir=a.out, lambda_home=a.lambda_home,
                       builddir=a.builddir, keep_plan=a.keep_plan)
    except Exception as e:
        print(f"pi cpp: {e}", file=sys.stderr)
        return 1


def cmd_aot(a) -> int:
    """Compile the linear Pi subset to a native executable (no Python runtime)."""
    from .aot import AotError, compile_aot
    try:
        exe = compile_aot(a.file, a.output, lambda_home=a.lambda_home,
                          builddir=a.builddir, outdir=a.out, keep_c=a.keep_c,
                          steps=a.steps)
    except (AotError, ToolchainError, CacheError) as e:
        print(f"pi aot: {e}", file=sys.stderr)
        return 1
    print(f"aot: {exe}")
    return 0


def cmd_build(a) -> int:
    """Check the program and compile every Lambda worker it names, with the protocol's schema hash."""
    try:
        mod, diags, errs = _load(a.file)
    except PiCompileError as e:
        print(e.format(), file=sys.stderr)
        return 2
    except RuntimeError as e:
        print(f"pi build: {e}", file=sys.stderr)
        return 1
    if errs:
        return 1
    base = os.path.dirname(os.path.abspath(a.file))
    built = 0
    wanted: List[tuple] = []
    for sysd in mod.systems.values():
        for inst in sysd.instances:
            for wname, src in inst.using.items():
                el = mod.elems[inst.elem]
                proto = next(w.protocol for w in el.workers if w.name == wname)
                wanted.append((src, mod.protocols[proto]))
    for spec in a.worker or []:
        proto, _, src = spec.partition("=")
        wanted.append((src, mod.protocols[proto]))
    for prog in mod.programs.values():
        for n in _walk_all(prog.body):
            if isinstance(n, S.Call) and isinstance(n.fn, S.Name) and n.fn.id == "Pool" and n.args \
                    and isinstance(n.args[0], S.Lit) and len(mod.protocols) == 1:
                wanted.append((n.args[0].value, next(iter(mod.protocols.values()))))
    for src, proto in wanted:
        path = src if os.path.isabs(src) else os.path.join(base, src)
        try:
            exe = build_worker(path, proto, lambda_home=a.lambda_home, builddir=a.builddir)
        except (ToolchainError, CacheError) as e:
            print(e, file=sys.stderr)
            return 1
        print(f"built {src} [{proto.name} schema {schema_hex(proto)[:16]}…] -> {exe}")
        built += 1
    print(f"ok: {built} worker(s) built")
    return 0


def cmd_run(a) -> int:
    """Run a Pi program through the C++ AST runtime."""
    from .cpp import run_cpp
    try:
        return run_cpp(a.file, outdir=a.out, lambda_home=a.lambda_home,
                       builddir=a.builddir, keep_plan=a.keep_plan)
    except Exception as e:  # noqa: BLE001
        print(f"pi run: {e}", file=sys.stderr)
        return 1


def cmd_view(a) -> int:
    """Open the universal WebGL viewer for a Pi output directory or data file."""
    root = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
    viewer = os.path.join(root, "tools", "viewer.py")
    if not os.path.exists(viewer):
        print(f"pi view: viewer not found at {viewer}", file=sys.stderr)
        return 1
    cmd = [sys.executable, viewer, a.path]
    if a.no_open:
        cmd.append("--no-open")
    if a.port:
        cmd += ["--port", str(a.port)]
    return subprocess.call(cmd)


def cmd_test(a) -> int:
    """Run every `*.pi` program named test_* / *_test under the given paths (each must exit 0)."""
    files: List[str] = []
    for p in a.paths:
        files += sorted(glob.glob(os.path.join(p, "**", "*.pi"), recursive=True)) if os.path.isdir(p) else [p]
    bad = 0
    for f in files:
        ns = argparse.Namespace(file=f, out=None, lambda_home=a.lambda_home,
                                builddir=a.builddir, keep_plan=False)
        rc = cmd_run(ns)
        print(("PASS " if rc == 0 else "FAIL ") + f)
        bad += rc != 0
    print(f"{len(files) - bad}/{len(files)} passed")
    return 1 if bad else 0


def cmd_analyze(a) -> int:
    """Visible cost (G4) and recovery capability of every element, read off the source."""
    try:
        mod, diags, errs = _load(a.file)
    except PiCompileError as e:
        print(e.format(), file=sys.stderr)
        return 2
    except RuntimeError as e:
        print(f"pi analyze: {e}", file=sys.stderr)
        return 1
    rows = []
    for e in mod.elems.values():
        counts = {"send": 0, "recv": 0, "call": 0}
        for c in e.ctrls:
            for n in _walk_all(c.body):
                if isinstance(n, S.Call) and isinstance(n.fn, S.Name) and n.fn.id in counts:
                    counts[n.fn.id] += 1
        rec = "restart-only (state consumed on failure)" if e.states else "resume-capable"
        rows.append((e.name, rec, counts, len(e.on_failure or [])))
    tasks = []
    for n in _walk_all(mod):
        if isinstance(n, S.Serve):
            attrs = {a.name: a.value for a in n.attrs}
            tasks.append({k: attrs.get(k) for k in ("rate", "deadline", "priority")})
    out = {"scheduler": {"tasks": tasks,
                         "note": "attributes are validated; the interpreter is not a "
                                 "hard-WCET platform (use an AOT/embedded build for proofs)"},
           "elements": {r[0]: {"recovery": r[1], "static_ops_per_ctrl": r[2], "handlers": r[3]} for r in rows},
           "protocols": {p.name: {"schema": schema_hex(p), "ops": [o.name for o in p.ops],
                                  "errors": {c: d.name for c, d in p.errors.items()}, "purity": p.purity}
                         for p in mod.protocols.values()},
           "note": "static counts are syntactic (spec G4), not a WCET proof."}
    print(json.dumps(out, indent=2, ensure_ascii=False))
    return 1 if errs else 0


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(prog="pi", description="Pi: coordination language for Lambda compute (spec v2.0)")
    sub = ap.add_subparsers(dest="cmd", required=True)

    def common(p, build=False):
        p.add_argument("--lambda", dest="lambda_home", help="Lambda compiler checkout (default: $LAMBDA_HOME)")
        p.add_argument("--builddir", help="worker build cache directory")

    p = sub.add_parser("check", help="parse and statically check")
    p.add_argument("file")
    p.set_defaults(fn=cmd_check)

    p = sub.add_parser("build", help="check and compile the Lambda workers a program uses")
    p.add_argument("file")
    p.add_argument("--worker", action="append", help="Protocol=file.lambda")
    common(p)
    p.set_defaults(fn=cmd_build)

    p = sub.add_parser("cpp", help="run through the C++ AST interpreter")
    p.add_argument("file")
    p.add_argument("--out", metavar="DIR", help="directory for CSV outputs")
    p.add_argument("--keep-plan", action="store_true", dest="keep_plan", help="keep generated plan JSON")
    common(p)
    p.set_defaults(fn=cmd_cpp)

    p = sub.add_parser("aot", help="compile the linear subset to a native executable")
    p.add_argument("file")
    p.add_argument("-o", "--output", required=True, help="output executable")
    p.add_argument("--out", metavar="DIR", help="directory for CSV outputs at run time")
    p.add_argument("--keep-c", action="store_true", dest="keep_c", help="keep generated .aot.c")
    p.add_argument("--steps", type=int, default=100, help="iterations for a static serve task")
    common(p)
    p.set_defaults(fn=cmd_aot)

    p = sub.add_parser("run", help="run a program")
    p.add_argument("file")
    p.add_argument("--out", metavar="DIR", help="write CSV/SVG/JSON/HTML result report to DIR")
    p.add_argument("--keep-plan", action="store_true", dest="keep_plan", help="keep generated plan JSON")
    common(p)
    p.set_defaults(fn=cmd_run)

    p = sub.add_parser("view", help="open the universal WebGL viewer for output/data")
    p.add_argument("path", help="Pi output directory, CSV, JSON, text, or LMB1 file")
    p.add_argument("--no-open", action="store_true", dest="no_open", help="do not open a browser")
    p.add_argument("--port", type=int, default=0, help="port (0 chooses a free port)")
    p.set_defaults(fn=cmd_view)

    p = sub.add_parser("test", help="run .pi test programs")
    p.add_argument("paths", nargs="+")
    common(p)
    p.set_defaults(fn=cmd_test)

    p = sub.add_parser("analyze", help="print protocol schemas, recovery capability and static op counts")
    p.add_argument("file")
    p.set_defaults(fn=cmd_analyze)

    a = ap.parse_args(argv)
    return a.fn(a)


if __name__ == "__main__":
    sys.exit(main())
