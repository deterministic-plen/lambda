"""Ahead-of-time compiler for a linear Pi subset.

This is deliberately a *small, native* target rather than the interpreter in a
bundle: it emits C, links a worker-protocol runtime, and runs with no Python.
Supported: one program, `scope`, sequential bindings, worker pools, `await
call`, `iterate`, `after`, scalar/payload expressions, and emit/report calls.
Not supported yet: elements/systems, channels, `par`, `select`, `serve`,
`on_error`, recovery, and dynamic topology.
"""

from __future__ import annotations

import os
import shutil
import subprocess
import sys
from typing import Dict, List, Optional, Tuple

from . import syntax as S
from .checker import check, walk
from .errors import PiCompileError
from .parser import load_module
from .worker import ToolchainError, build_worker, find_lambda_home


class AotError(Exception):
    pass


def _cstr(s: str) -> str:
    out = ['"']
    for ch in s:
        if ch == "\\":
            out.append("\\\\")
        elif ch == '"':
            out.append('\\"')
        elif ch == "\n":
            out.append("\\n")
        elif ch == "\r":
            out.append("\\r")
        elif ch == "\t":
            out.append("\\t")
        else:
            out.append(ch)
    out.append('"')
    return "".join(out)


def _ctype_name(name: str) -> str:
    safe = "".join(ch if ch.isalnum() or ch == "_" else "_" for ch in name)
    return "v_" + safe


class AotGen:
    def __init__(self, mod, program, pools: Dict[str, str], outdir: str, steps: int = 100):
        self.mod = mod
        self.program = program
        self.pools = pools
        self.outdir = outdir
        self.steps = steps
        self.env: Dict[str, str] = {}
        self.lines: List[str] = []
        self.indent = 1
        protocols = list(mod.protocols.values())
        self.proto = protocols[0] if len(protocols) == 1 else None
        self.op_index = {o.name: i for i, o in enumerate(self.proto.ops)} if self.proto else {}

    def fail(self, node, msg: str):
        loc = getattr(node, "loc", None)
        raise AotError(f"{loc}: {msg}" if loc else msg)

    def emit(self, line: str):
        self.lines.append("    " * self.indent + line)

    def block(self, node) -> None:
        if isinstance(node, S.Block):
            for st in node.stmts:
                self.stmt(st)
        else:
            self.stmt(node)

    def stmt(self, node) -> None:
        if isinstance(node, S.ScopeE):
            self.block(node.body)
        elif isinstance(node, S.Block):
            for st in node.stmts:
                self.stmt(st)
        elif isinstance(node, S.Bind):
            if not isinstance(node.pat, S.PName):
                self.fail(node, "AOT supports only name bindings")
            expr = self.expr(node.expr)
            cname = _ctype_name(node.pat.id)
            self.emit(f"PiVal {cname} = {expr};")
            self.env[node.pat.id] = cname
        elif isinstance(node, S.AfterBlock):
            self.emit(f"pi_sleep_ms(pi_as_int({self.expr(node.duration)}));")
            self.block(node.body)
        elif isinstance(node, S.Serve):
            attrs = {a.name: a.value for a in node.attrs}
            rate = float(attrs.get("rate") or 0.0)
            deadline = float(attrs.get("deadline") or 0.0)
            priority = int(attrs.get("priority") or 0)
            self.emit(f"/* static task: period={rate:g}s deadline={deadline:g}s priority={priority} */")
            self.emit(f"for (long __tick = 0; __tick < {self.steps}; ++__tick) {{")
            self.indent += 1
            self.block(node.body)
            if rate > 0:
                self.emit(f"pi_sleep_ms({int(round(rate * 1000))});")
            self.indent -= 1
            self.emit("}")
        elif isinstance(node, S.Iterate):
            n = self.expr(node.n)
            self.emit(f"for (long __i = 0; __i < pi_as_int({n}); ++__i) {{")
            self.indent += 1
            self.block(node.body)
            self.indent -= 1
            self.emit("}")
        elif isinstance(node, S.If):
            c = self.expr(node.cond)
            self.emit(f"if (pi_as_int({c})) {{")
            self.indent += 1
            self.block(node.then)
            self.indent -= 1
            if node.orelse is not None:
                self.emit("} else {")
                self.indent += 1
                self.block(node.orelse)
                self.indent -= 1
            self.emit("}")
        elif isinstance(node, S.Call):
            self.emit(f"(void){self.expr(node)};")
        elif isinstance(node, S.Await):
            self.emit(f"(void){self.expr(node.expr)};")
        elif isinstance(node, (S.Lit, S.Name, S.Index, S.BinOp, S.Member)):
            self.emit(f"(void){self.expr(node)};")
        elif isinstance(node, S.Assign):
            if node.name not in self.env:
                self.fail(node, f"assignment to unknown name '{node.name}'")
            self.emit(f"{self.env[node.name]} = {self.expr(node.expr)};")
        else:
            self.fail(node, f"AOT does not support {type(node).__name__} statements")

    def pool_gpu(self, node: S.Call) -> int:
        if len(node.args) > 2 and isinstance(node.args[2], S.Call):
            for k, v in node.args[2].kwargs.items():
                if k == "gpus":
                    try:
                        return int(getattr(v, "value", 0))
                    except Exception:
                        return 0
        return 0

    def pool_exe(self, node: S.Call) -> str:
        if not isinstance(node.fn, S.Name) or node.fn.id != "Pool":
            self.fail(node, "expected Pool(...) before .recruit")
        if not node.args or not isinstance(node.args[0], S.Lit) or node.args[0].kind != "string":
            self.fail(node, "AOT needs a literal worker path in Pool(...)")
        if len(node.args) > 1:
            count = getattr(node.args[1], "value", None)
            if count not in (None, 1):
                self.fail(node, "AOT supports one worker per Pool")
        src = node.args[0].value
        if src not in self.pools:
            self.fail(node, f"worker '{src}' was not built")
        return self.pools[src]

    def expr(self, node) -> str:
        if isinstance(node, S.Lit):
            kind = node.kind
            if kind in ("int", "float"):
                return f"pi_num({float(node.value)!r})"
            if kind == "bool":
                return f"pi_num({1.0 if node.value else 0.0!r})"
            if kind == "string":
                return f"pi_str({_cstr(node.value)})"
            if kind == "unit":
                return "pi_unit()"
            if kind == "duration":
                return f"pi_num({float(node.value) * 1000.0!r})"
            self.fail(node, f"AOT does not support {kind} literals")
        if isinstance(node, S.Name):
            if node.id in self.env:
                return self.env[node.id]
            self.fail(node, f"AOT does not support free name '{node.id}'")
        if isinstance(node, S.Await):
            return self.expr(node.expr)
        if isinstance(node, S.Index):
            return f"pi_index({self.expr(node.obj)}, pi_as_int({self.expr(node.index)}))"
        if isinstance(node, S.BinOp):
            op = {"+": "pi_add", "-": "pi_sub", "*": "pi_mul", "/": "pi_div",
                  "<": "pi_lt", "<=": "pi_le", ">": "pi_gt", ">=": "pi_ge",
                  "==": "pi_eq"}.get(node.op)
            if op is None:
                self.fail(node, f"AOT does not support operator '{node.op}'")
            return f"{op}({self.expr(node.left)}, {self.expr(node.right)})"
        if isinstance(node, S.Member):
            if node.name != "recruit":
                self.fail(node, f"AOT does not support member '.{node.name}'")
            if not isinstance(node.obj, S.Call):
                self.fail(node, "AOT supports only Pool(...).recruit(n)")
            exe = self.pool_exe(node.obj)
            gpu = self.pool_gpu(node.obj)
            return f"pi_spawn({_cstr(exe)}, {1 if gpu else 0})"
        if isinstance(node, S.Call):
            return self.call_expr(node)
        self.fail(node, f"AOT does not support expression {type(node).__name__}")

    def call_expr(self, node: S.Call) -> str:
        if isinstance(node.fn, S.Member):
            if node.fn.name != "recruit" or len(node.args) != 1:
                self.fail(node, "AOT supports only Pool(...).recruit(1)")
            return self.expr(node.fn)
        if not isinstance(node.fn, S.Name):
            self.fail(node, "AOT supports only direct calls")
        name = node.fn.id
        if name == "call":
            if len(node.args) < 2:
                self.fail(node, "call needs a worker and an op name")
            worker = self.expr(node.args[0])
            if not isinstance(node.args[1], S.Lit) or node.args[1].kind != "string":
                self.fail(node, "AOT needs a literal op name")
            op = node.args[1].value
            if op not in self.op_index:
                self.fail(node, f"protocol has no op '{op}'")
            idx = self.op_index[op]
            vals = [self.expr(a) for a in node.args[2:]]
            if vals:
                arr = "(PiVal[]){" + ", ".join(vals) + "}"
                return f"pi_call(({worker}).worker, {idx}, {len(vals)}, {arr})"
            return f"pi_call(({worker}).worker, {idx}, 0, NULL)"
        if name == "payload":
            if len(node.args) != 1:
                self.fail(node, "AOT payload(...) takes one argument")
            return self.expr(node.args[0])
        if name == "range":
            if len(node.args) != 2:
                self.fail(node, "AOT range takes two arguments")
            return f"pi_range(pi_as_int({self.expr(node.args[0])}), pi_as_int({self.expr(node.args[1])}))"
        if name == "after":
            if len(node.args) != 1:
                self.fail(node, "AOT after takes one duration")
            ms = self.expr(node.args[0])
            return f"(pi_sleep_ms((long)({ms}).num), pi_unit())"
        if name == "emit":
            if not node.args:
                self.fail(node, "emit needs a value")
            vals = [self.expr(a) for a in node.args]
            return f"(pi_emit_args({len(vals)}, (PiVal[]){{{', '.join(vals)}}}), pi_unit())"
        if name == "emit_text":
            if len(node.args) < 2:
                self.fail(node, "emit_text(name, text)")
            return f"(pi_emit_text({self.expr(node.args[1])}.str), pi_unit())"
        if name == "emit_series":
            if len(node.args) < 2:
                self.fail(node, "emit_series(name, y)")
            namev = self.expr(node.args[0])
            x = self.expr(node.args[1])
            y = self.expr(node.args[2]) if len(node.args) > 2 else x
            return f"(pi_emit_series({namev}.str, {x}, {y}), pi_unit())"
        if name == "emit_grid":
            if len(node.args) < 2:
                self.fail(node, "emit_grid(name, values, rows, cols)")
            namev = self.expr(node.args[0])
            val = self.expr(node.args[1])
            rows = self.expr(node.args[2]) if len(node.args) > 2 else "pi_num(0)"
            cols = self.expr(node.args[3]) if len(node.args) > 3 else "pi_num(0)"
            return (f"(pi_emit_grid({namev}.str, {val}, pi_as_int({rows}), "
                    f"pi_as_int({cols})), pi_unit())")
        if name == "emit_animation_frame":
            if len(node.args) < 4:
                self.fail(node, "emit_animation_frame(name, values, rows, cols)")
            namev = self.expr(node.args[0])
            val = self.expr(node.args[1])
            rows = self.expr(node.args[2])
            cols = self.expr(node.args[3])
            return (f"(pi_emit_animation_frame({namev}.str, {val}, pi_as_int({rows}), "
                    f"pi_as_int({cols})), pi_unit())")
        if name == "Pool":
            self.fail(node, "Pool(...) is only used as Pool(...).recruit(n)")
        self.fail(node, f"AOT does not support call '{name}'")


def _collect_pools(mod, base: str, proto, lambda_home, builddir):
    wanted: List[Tuple[str, str]] = []
    seen = set()
    for prog in mod.programs.values():
        for n in walk(prog.body):
            if (isinstance(n, S.Call) and isinstance(n.fn, S.Name) and n.fn.id == "Pool"
                    and n.args and isinstance(n.args[0], S.Lit) and n.args[0].kind == "string"):
                src = n.args[0].value
                if src in seen:
                    continue
                seen.add(src)
                path = src if os.path.isabs(src) else os.path.join(base, src)
                exe = build_worker(path, proto, lambda_home=lambda_home, builddir=builddir)
                wanted.append((src, exe))
    return dict(wanted)


def compile_aot(path: str, output: str, *, lambda_home: Optional[str] = None,
                builddir: Optional[str] = None, outdir: Optional[str] = None,
                keep_c: bool = False, steps: int = 100) -> str:
    try:
        mod = load_module(path)
        diags = check(mod)
    except PiCompileError as e:
        raise AotError(e.format())
    errs = [d for d in diags if d.severity == "error"]
    if errs:
        raise AotError("\n".join(str(d) for d in errs))
    if mod.elems or mod.systems:
        raise AotError("AOT currently supports linear programs only; elements/systems are not implemented")
    protocols = list(mod.protocols.values())
    if len(protocols) > 1:
        raise AotError("AOT currently supports one protocol")
    proto = protocols[0] if protocols else None
    base = os.path.dirname(os.path.abspath(path))
    home = find_lambda_home(lambda_home)
    pools = _collect_pools(mod, base, proto, home, builddir)
    programs = list(mod.programs)
    name = "Main" if "Main" in mod.programs else (programs[0] if programs else None)
    if name is None:
        raise AotError("no program")
    prog = mod.programs[name]
    if prog.params:
        raise AotError("AOT program parameters are not implemented; specialize the source")
    gen = AotGen(mod, prog, pools, outdir or "", steps)
    gen.emit(f"pi_set_output({_cstr(outdir or '')});")
    gen.block(prog.body)
    gen.emit("pi_finish();")
    gen.emit("return 0;")
    runtime = os.path.join(os.path.dirname(os.path.abspath(__file__)), "aot_runtime.c")
    if not os.path.exists(runtime):
        raise AotError(f"missing runtime {runtime}")
    with open(runtime, encoding="utf-8") as f:
        rt = f.read()
    src = "#define _CRT_SECURE_NO_WARNINGS 1\n" + rt + "\nint main(void) {\n" + \
          "\n".join(gen.lines) + "\n}\n"
    stem = os.path.splitext(output)[0]
    cpath = stem + ".aot.c"
    with open(cpath, "w", encoding="utf-8", newline="\n") as f:
        f.write(src)
    cc = os.environ.get("CC") or shutil.which("clang") or shutil.which("cc")
    if not cc:
        raise AotError("no C compiler found (need clang or cc)")
    exe = output
    if os.name == "nt" and not exe.lower().endswith(".exe"):
        exe += ".exe"
    cmd = [cc, "-O2", "-std=c99", cpath, "-o", exe]
    proc = subprocess.run(cmd, capture_output=True, text=True, encoding="utf-8", errors="replace")
    if proc.returncode != 0:
        raise AotError("C compilation failed:\n" + (proc.stderr or proc.stdout))
    if not keep_c:
        try:
            os.remove(cpath)
        except OSError:
            pass
    return exe
