"""Serialize a checked Pi module to the C++ runtime plan format.

The C++ runtime is an AST interpreter.  This frontend only parses/checks Pi and
builds the worker executables; execution has no Python in the loop.
"""

from __future__ import annotations

from typing import Any, Dict

from . import syntax as S
from .checker import check
from .parser import load_module
from .worker import build_worker, find_lambda_home, schema_hex


def _loc(n) -> Dict[str, int]:
    loc = getattr(n, "loc", None)
    if loc is None:
        return {}
    return {"line": loc.line, "col": loc.col}


def encode(node: Any) -> Any:
    if isinstance(node, S.Node):
        d: Dict[str, Any] = {"_": type(node).__name__}
        if isinstance(node, S.Lit):
            d["value"] = node.value
            d["kind"] = node.kind
        elif isinstance(node, S.Name):
            d["id"] = node.id
        elif isinstance(node, S.Block):
            d["stmts"] = [encode(x) for x in node.stmts]
        elif isinstance(node, S.Bind):
            d["pat"] = encode(node.pat)
            d["expr"] = encode(node.expr)
        elif isinstance(node, S.Assign):
            d["name"] = node.name
            d["expr"] = encode(node.expr)
        elif isinstance(node, S.If):
            d["cond"] = encode(node.cond)
            d["then"] = encode(node.then)
            d["orelse"] = encode(node.orelse) if node.orelse else None
        elif isinstance(node, S.Iterate):
            d["n"] = encode(node.n)
            d["body"] = encode(node.body)
        elif isinstance(node, S.Par):
            d["branches"] = [encode(x) for x in node.branches]
        elif isinstance(node, S.Serve):
            d["chan"] = encode(node.chan)
            d["pat"] = encode(node.pat)
            d["body"] = encode(node.body)
            d["attrs"] = {a.name: a.value for a in node.attrs}
        elif isinstance(node, S.For):
            d["var"] = encode(node.var)
            d["iter"] = encode(node.iter)
            d["body"] = encode(node.body)
        elif isinstance(node, S.Loop):
            d["body"] = encode(node.body)
        elif isinstance(node, S.ParFor):
            d["pat"] = encode(node.pat)
            d["iter"] = encode(node.iter)
            d["body"] = encode(node.body)
        elif isinstance(node, S.Select):
            d["arms"] = [encode(x) for x in node.arms]
        elif isinstance(node, S.SelectArm):
            d["guard"] = encode(node.guard)
            d["body"] = encode(node.body)
        elif isinstance(node, S.Timeout):
            d["duration"] = encode(node.duration)
            d["body"] = encode(node.body)
        elif isinstance(node, S.Retry):
            d["opts"] = {k: encode(v) for k, v in node.opts.items()}
            d["body"] = encode(node.body)
        elif isinstance(node, S.OnError):
            d["body"] = encode(node.body)
            d["arms"] = [encode(x) for x in node.arms]
        elif isinstance(node, S.HandlerArm):
            d["pat"] = encode(node.pat)
            d["action"] = encode(node.action)
        elif isinstance(node, S.AfterBlock):
            d["duration"] = encode(node.duration)
            d["body"] = encode(node.body)
        elif isinstance(node, S.ScopeE):
            d["body"] = encode(node.body)
        elif isinstance(node, S.Await):
            d["expr"] = encode(node.expr)
        elif isinstance(node, S.Call):
            d["fn"] = encode(node.fn)
            d["args"] = [encode(x) for x in node.args]
            d["kwargs"] = {k: encode(v) for k, v in node.kwargs.items()}
        elif isinstance(node, S.Member):
            d["obj"] = encode(node.obj)
            d["name"] = node.name
        elif isinstance(node, S.Index):
            d["obj"] = encode(node.obj)
            d["index"] = encode(node.index)
        elif isinstance(node, S.RangeE):
            d["lo"] = encode(node.lo)
            d["hi"] = encode(node.hi)
        elif isinstance(node, S.BinOp):
            d["op"] = node.op
            d["left"] = encode(node.left)
            d["right"] = encode(node.right)
        elif isinstance(node, S.TupleE):
            d["items"] = [encode(x) for x in node.items]
        elif isinstance(node, S.ListE):
            d["items"] = [encode(x) for x in node.items]
        elif isinstance(node, S.PName):
            d["id"] = node.id
        elif isinstance(node, S.PWild):
            pass
        elif isinstance(node, S.PTuple):
            d["items"] = [encode(x) for x in node.items]
        elif isinstance(node, S.PCtor):
            d["name"] = node.name
            d["args"] = [encode(x) for x in node.args]
        else:
            d["_unsupported"] = type(node).__name__
        d["loc"] = _loc(node)
        return d
    if isinstance(node, list):
        return [encode(x) for x in node]
    if isinstance(node, dict):
        return {k: encode(v) for k, v in node.items()}
    return node


def build_plan(path: str, *, lambda_home=None, builddir=None,
               outdir: str = "") -> Dict[str, Any]:
    mod = load_module(path)
    diags = check(mod)
    errs = [d for d in diags if d.severity == "error"]
    if errs:
        raise ValueError("\n".join(str(d) for d in errs))
    protocols = {name: {"ops": [o.name for o in p.ops],
                        "rets": [o.ret.show() for o in p.ops],
                        "errors": {str(c): e.name for c, e in p.errors.items()},
                        "purity": p.purity}
                 for name, p in mod.protocols.items()}
    progs = {name: {"params": [p[0] for p in pr.params], "body": encode(pr.body)}
             for name, pr in mod.programs.items()}
    # Build every worker named by a Pool(...) in a program.
    import os
    base = os.path.dirname(os.path.abspath(path))
    workers = {}
    gpu_workers = {}
    schemas = {}
    proto = next(iter(mod.protocols.values())) if len(mod.protocols) == 1 else None
    home = find_lambda_home(lambda_home)

    def walk(n):
        yield n
        for f in getattr(n, "__dataclass_fields__", {}):
            if f == "loc":
                continue
            v = getattr(n, f)
            if isinstance(v, S.Node):
                yield from walk(v)
            elif isinstance(v, list):
                for x in v:
                    if isinstance(x, S.Node):
                        yield from walk(x)
            elif isinstance(v, dict):
                for x in v.values():
                    if isinstance(x, S.Node):
                        yield from walk(x)

    for pr in mod.programs.values():
        for n in walk(pr.body):
            if (isinstance(n, S.Call) and isinstance(n.fn, S.Name) and n.fn.id == "Pool"
                    and n.args and isinstance(n.args[0], S.Lit) and n.args[0].kind == "string"):
                src = n.args[0].value
                if src in workers:
                    continue
                p = src if os.path.isabs(src) else os.path.join(base, src)
                workers[src] = build_worker(p, proto, lambda_home=home, builddir=builddir)
                if proto is not None:
                    schemas[src] = schema_hex(proto)
                gpus = 0
                if len(n.args) > 2 and isinstance(n.args[2], S.Call):
                    for k, v in n.args[2].kwargs.items():
                        if k == "gpus":
                            gpus = int(getattr(v, "value", 0))
                gpu_workers[src] = gpus
    elems = {}
    for name, el in mod.elems.items():
        elems[name] = {
            "ports": [{"dir": p.direction, "name": p.name, "type": p.type.show()}
                      for p in el.ports],
            "workers": [{"name": w.name, "protocol": w.protocol} for w in el.workers],
            "ctrls": [{"name": c.name, "body": encode(c.body)} for c in el.ctrls],
            "vparams": [p[0] for p in el.vparams],
            "tparams": list(el.tparams),
            "on_failure": [encode(a) for a in el.on_failure],
        }
    systems = {}
    for name, sy in mod.systems.items():
        systems[name] = {
            "vparams": [p[0] for p in sy.vparams],
            "instances": [{
                "name": i.name, "idx": i.idx, "elem": i.elem,
                "args": [encode(a) for a in i.args],
                "targs": list(i.targs),
                "bindings": [[v, encode(e)] for v, e in i.bindings],
                "using": i.using,
            } for i in sy.instances],
            "couple": [encode(c) for c in sy.couple],
            "expose": {k: encode(v) for k, v in sy.expose.items()},
        }
    for sy in mod.systems.values():
        for inst in sy.instances:
            el = mod.elems[inst.elem]
            for wname, src in inst.using.items():
                wd = next((w for w in el.workers if w.name == wname), None)
                if wd is None:
                    continue
                proto = mod.protocols.get(wd.protocol)
                if src in workers:
                    continue
                p = src if os.path.isabs(src) else os.path.join(base, src)
                workers[src] = build_worker(p, proto, lambda_home=home, builddir=builddir)
                if proto is not None:
                    schemas[src] = schema_hex(proto)
                gpu_workers[src] = 0
    return {"protocols": protocols, "programs": progs, "workers": workers,
            "schemas": schemas,
            "gpu": gpu_workers, "elems": elems, "systems": systems,
            "out": outdir, "source": os.path.abspath(path)}
