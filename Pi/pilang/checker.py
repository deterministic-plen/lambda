"""Static checks (spec section 7): the rules that make failure handling total and resume sound.

Implemented:
  7.3  Resume        `resume` is rejected in any element that has a `state` field, and outside handlers.
  7.4  errors        a protocol must declare `errors`; WorkerError(c) patterns and the declared codes agree.
  7.5  Scope         call / serve / par / race / par for must sit inside a scope, program or ctrl.
  7.6  Exhaustive    every on_error / on_failure covers every Err constructor or has a wildcard.
  5.1  Linearity     (straight-line) a handle consumed by await / cancel / drop / close is not used again.
  12   Contract      `recovery = resume-capable` is checked against the presence of a state field.
Not implemented: scheduler attribute checks (7.7) and the full sequent calculus of 7.1.
"""

from __future__ import annotations

import dataclasses
from typing import Dict, Iterator, List, Optional, Set

from . import syntax as S
from .errors import Diagnostic, Loc

ERR_CTORS = ("WorkerDied", "Deadline", "Transport", "Rejected", "BadFrame", "WorkerError",
             "Cancelled", "NoQuorum", "Incomplete", "Unsupported")
PROCESS_CALLS = {"call"}


def children(n) -> Iterator[S.Node]:
    if isinstance(n, S.Node):
        for f in dataclasses.fields(n):
            if f.name == "loc":
                continue
            yield from _iter(getattr(n, f.name))


def _iter(v) -> Iterator[S.Node]:
    if isinstance(v, S.Node):
        yield v
    elif isinstance(v, (list, tuple)):
        for x in v:
            yield from _iter(x)
    elif isinstance(v, dict):
        for x in v.values():
            yield from _iter(x)


def walk(n) -> Iterator[S.Node]:
    yield n
    for c in children(n):
        yield from walk(c)


class Checker:
    def __init__(self, mod: S.Module):
        self.m = mod
        self.diags: List[Diagnostic] = []

    def err(self, msg: str, loc: Optional[Loc] = None):
        self.diags.append(Diagnostic("error", msg, loc))

    def warn(self, msg: str, loc: Optional[Loc] = None):
        self.diags.append(Diagnostic("warning", msg, loc))

    # ------------------------------------------------------------------ driver
    def scheduler(self):
        """Spec 7.7: scheduler attributes are meaningful, not decoration."""
        for n in _walk_nodes(self.m):
            if not isinstance(n, S.Serve) or not n.attrs:
                continue
            a = {x.name: x.value for x in n.attrs}
            rate = a.get("rate")
            deadline = a.get("deadline")
            priority = a.get("priority")
            if rate is not None:
                if not isinstance(rate, (int, float)) or rate <= 0:
                    self.err("@rate must be a positive duration", n.loc)
                if deadline is not None and isinstance(deadline, (int, float)) and deadline > rate:
                    self.err(f"@deadline {deadline:g}s exceeds the @rate period {rate:g}s; "
                             "every iteration would miss its deadline", n.loc)
            if deadline is not None and (not isinstance(deadline, (int, float)) or deadline <= 0):
                self.err("@deadline must be a positive duration", n.loc)
            if priority is not None:
                if not isinstance(priority, int) or priority < 0:
                    self.err("@priority must be a non-negative integer", n.loc)
                elif priority > 99:
                    self.warn("@priority above 99 is outside the portable range", n.loc)
            if a.get("sporadic") and rate is not None:
                self.warn("@sporadic with @rate is treated as periodic", n.loc)
            if a.get("periodic") and rate is None:
                self.warn("@periodic without @rate falls back to a plain serve loop", n.loc)

    def run(self) -> List[Diagnostic]:
        for w in self.m.warnings:
            self.diags.append(Diagnostic("warning", w.split("warning: ", 1)[-1], None))
        for p in self.m.protocols.values():
            self.protocol(p)
        for e in self.m.elems.values():
            self.elem(e)
        for s in self.m.systems.values():
            self.system(s)
        for p in self.m.programs.values():
            self.body(p.body, in_scope=False, elem=None, protos=None, handler_depth=0)
            self.linearity(p.body)
        for f in self.m.funs.values():
            self.body(f.body, in_scope=False, elem=None, protos=None, handler_depth=0)
            self.linearity(f.body)
        self.scheduler()
        return self.diags

    # ---------------------------------------------------------------- protocols
    def protocol(self, p: S.Protocol):
        if not p.has_errors:
            self.err(f"protocol {p.name} has no `errors` declaration (required since v2.0, spec 10.1)", p.loc)
        seen: Set[str] = set()
        for o in p.ops:
            if o.name in seen:
                self.err(f"protocol {p.name}: duplicate op '{o.name}'", o.loc)
            seen.add(o.name)
        for code, d in p.errors.items():
            if code < 10000:
                self.warn(f"protocol {p.name}: error code {code} is inside the Pi-reserved range 1-9999 "
                          f"(spec 10.1 reserves it; the spec's own examples use it)", d.loc)
        if p.purity not in ("Pure", "Deterministic", "Impure"):
            self.err(f"protocol {p.name}: unknown purity '{p.purity}'", p.loc)

    # ------------------------------------------------------------------ elements
    def elem(self, e: S.Elem):
        protos: Dict[str, S.Protocol] = {}
        for w in e.workers:
            p = self.m.protocols.get(w.protocol)
            if p is None:
                self.err(f"element {e.name}: worker {w.name} uses unknown protocol '{w.protocol}'", w.loc)
            else:
                protos[w.name] = p
        stateless = bool(e.states)
        rec = e.contract.get("recovery", "")
        if "resume-capable" in rec.replace(" ", "") and stateless:
            self.err(f"element {e.name}: contract claims recovery = resume-capable but the element has a "
                     f"`state` field (Pi holds the state; a failed call consumes it)", e.loc)
        if e.on_failure is not None:
            self.handlers(e.on_failure, e, protos, e.loc)
        for c in e.ctrls:
            self.body(c.body, in_scope=True, elem=e, protos=protos, handler_depth=0)
            self.linearity(c.body)
        if e.on_failure is not None:
            for arm in e.on_failure:
                self.body(arm.action, in_scope=True, elem=e, protos=protos, handler_depth=1)

    def system(self, s: S.System):
        for i in s.instances:
            if i.elem not in self.m.elems:
                self.err(f"system {s.name}: instance {i.name} uses unknown element '{i.elem}'", i.loc)
            else:
                el = self.m.elems[i.elem]
                if len(i.targs) != len(el.tparams):
                    self.err(f"system {s.name}: {i.elem} takes {len(el.tparams)} type argument(s), "
                             f"got {len(i.targs)}", i.loc)
        for name, body in s.expose.items():
            if isinstance(body, S.Call) and isinstance(body.fn, S.Name) and body.fn.id == "push":
                fam = body.args[0].obj.id if isinstance(body.args[0], S.Member) else None
                if fam not in {i.name for i in s.instances}:
                    self.err(f"system {s.name}: expose {name} pushes into unknown instance family '{fam}'", s.loc)

    # ---------------------------------------------------------------- handlers
    def handlers(self, arms: List[S.HandlerArm], elem: Optional[S.Elem], protos, loc):
        covered: Set[str] = set()
        wildcard = False
        union_codes: Set[int] = set()
        if protos:
            for p in protos.values():
                union_codes |= set(p.errors)
        for arm in arms:
            pat = arm.pat
            if isinstance(pat, (S.PWild, S.PName)):
                wildcard = True
            elif isinstance(pat, S.PCtor):
                if pat.name not in ERR_CTORS:
                    self.err(f"handler pattern '{pat.name}' is not an Err constructor "
                             f"({', '.join(ERR_CTORS)})", pat.loc)
                    continue
                if not pat.args or isinstance(pat.args[0], (S.PWild, S.PName)):
                    covered.add(pat.name)                    # covers every argument
                elif pat.name == "WorkerError" and isinstance(pat.args[0], S.Lit):
                    code = pat.args[0].value
                    if protos and code not in union_codes:
                        self.err(f"handler matches WorkerError({code}) but no protocol of this element "
                                 f"declares code {code}", pat.loc)
            else:
                self.err("invalid handler pattern", arm.loc)
        missing = [c for c in ERR_CTORS if c not in covered]
        if not wildcard and missing:
            self.err("non-exhaustive on_error: unhandled " + ", ".join(missing)
                     + " (add `| _ => escalate`, spec 7.6)", loc)

    # -------------------------------------------------------------- body checks
    def body(self, n, *, in_scope: bool, elem: Optional[S.Elem], protos, handler_depth: int):
        if n is None:
            return
        if isinstance(n, S.OnError):
            self.handlers(n.arms, elem, protos, n.loc)
            self.body(n.body, in_scope=in_scope, elem=elem, protos=protos, handler_depth=handler_depth)
            for arm in n.arms:
                self.body(arm.action, in_scope=in_scope, elem=elem, protos=protos, handler_depth=handler_depth + 1)
            return
        if isinstance(n, S.Name) and n.id == "resume":
            if handler_depth == 0:
                self.err("`resume` is only valid inside an on_error / on_failure handler", n.loc)
            elif elem is not None and elem.states:
                self.err("resume requires stateful worker; state consumed by failed call "
                         f"(element {elem.name} has a `state` field)", n.loc)
        if isinstance(n, S.ScopeE):
            in_scope = True
        if isinstance(n, S.Call) and isinstance(n.fn, S.Name):
            fn = n.fn.id
            if fn in PROCESS_CALLS and not in_scope:
                self.err(f"`{fn}` outside any scope (spec 5.2: spawn outside a scope is ill-typed)", n.loc)
            if fn == "call" and protos and n.args and isinstance(n.args[0], S.Name) and n.args[0].id in protos \
                    and len(n.args) > 1 and isinstance(n.args[1], (S.Lit, S.Name)):
                op = n.args[1].value if isinstance(n.args[1], S.Lit) else n.args[1].id
                p = protos[n.args[0].id]
                if op not in {o.name for o in p.ops}:
                    self.err(f"protocol {p.name} has no op '{op}'", n.loc)
                else:
                    decl = next(o for o in p.ops if o.name == op)
                    nargs = len(n.args) - 2
                    if nargs != len(decl.args) and not (len(decl.args) == 1 and nargs == 0):
                        self.warn(f"op {p.name}.{op} takes {len(decl.args)} argument(s); call passes {nargs}", n.loc)
        if isinstance(n, (S.Serve, S.Par, S.ParFor, S.Race)) and not in_scope:
            self.err(f"{type(n).__name__.lower()} outside any scope (spec 7.5)", n.loc)
        for c in children(n):
            self.body(c, in_scope=in_scope, elem=elem, protos=protos, handler_depth=handler_depth)

    # ---------------------------------------------------------------- linearity
    def linearity(self, body: S.Node):
        """Straight-line linearity: a name bound to a Future, Tx or Rx that is consumed by
        await / cancel / drop / close must not be mentioned afterwards."""
        for blk in (n for n in walk(body) if isinstance(n, S.Block)):
            handles: Set[str] = set()
            consumed: Dict[str, Loc] = {}
            for st in blk.stmts:
                if isinstance(st, S.Bind):
                    root = _root_call(st.expr)
                    if root in ("call", "chan"):
                        for nm in _pattern_names(st.pat):
                            handles.add(nm)
                for u in _uses_in_order(st):
                    name, how, loc = u
                    if name not in handles:
                        continue
                    if name in consumed:
                        self.err(f"linear handle '{name}' used after it was consumed at {consumed[name]}", loc)
                    elif how == "consume":
                        consumed[name] = loc

    # ----------------------------------------------------------------- summary
    @property
    def errors(self) -> List[Diagnostic]:
        return [d for d in self.diags if d.severity == "error"]


def _root_call(e: S.Node) -> Optional[str]:
    """Name of the handle-producing builtin an un-awaited expression calls, if any."""
    if isinstance(e, S.Call) and isinstance(e.fn, S.Name):
        return e.fn.id
    return None


def _pattern_names(p) -> List[str]:
    if isinstance(p, S.PName):
        return [p.id]
    if isinstance(p, S.PTuple):
        return [n for x in p.items for n in _pattern_names(x)]
    return []


def _uses_in_order(n: S.Node):
    """Yield (name, 'use'|'consume', loc) for each mention of a bare name, in evaluation order."""
    if isinstance(n, S.Await) and isinstance(n.expr, S.Name):
        yield (n.expr.id, "consume", n.expr.loc)
        return
    if isinstance(n, S.Call) and isinstance(n.fn, S.Name) and n.fn.id in ("drop", "close") \
            and n.args and isinstance(n.args[0], S.Name):
        yield (n.args[0].id, "consume", n.args[0].loc)
        for a in n.args[1:]:
            yield from _uses_in_order(a)
        return
    if isinstance(n, S.Name):
        yield (n.id, "use", n.loc)
        return
    for c in children(n):
        yield from _uses_in_order(c)


def _walk_nodes(root) -> Iterator[S.Node]:
    stack = [root]
    while stack:
        n = stack.pop()
        if isinstance(n, S.Node):
            yield n
            stack.extend(children(n))
        elif isinstance(n, (list, tuple)):
            stack.extend(n)
        elif isinstance(n, dict):
            stack.extend(n.values())


def check(mod: S.Module) -> List[Diagnostic]:
    return Checker(mod).run()
