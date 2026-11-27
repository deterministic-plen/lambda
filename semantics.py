"""Optional program analyses for termination and borrow inference.

These analyses are deliberately conservative and *warnings-first*:

* :func:`check_halting` proves the common structural/numeric recursion pattern
  with a bounded, strictly decreasing rank variable.  When it cannot prove
  termination it prints a warning that the program *may* not halt; it never
  turns a program into an error.
* :func:`infer_borrows` turns a parameter that is inspected but never escapes
  into a borrow.  Explicit ``&`` annotations always win; ``: linear`` forces a
  parameter to stay consuming.

Both passes run after name resolution (and borrow inference after type
inference) so bindings and representation types are available.
"""

from __future__ import annotations

from typing import Dict, Iterable, List, Optional, Set, Tuple

from lambda_ast import (
    App, Binding, Block, Borrow, DataCtor, Def, If, Lam, Lit, Match, Node, PRIM,
    RecordLit, SumLit, Var, walk,
)
from typecheck import BOX, TCon, TList, TNumVar, TVar, prune, rep_of
from errors import TypeError_

# Primitives that only inspect their arguments.  A parameter passed to one of
# these does not escape the callee.  Scalar arithmetic/comparison is included;
# boxed values cannot reach those operations after type checking.
READONLY_PRIMS = {
    "arrayf-get", "arrayi-get", "arrayb-get", "arrayf-get-ro",
    "arrayi-get-ro", "arrayb-get-ro", "arrayf-length", "arrayi-length",
    "arrayb-length", "arrayf-sum", "arrayi-sum",
    "head", "tail", "empty?",
    "str-len", "str-byte-at", "str-byte-length", "str-utf8-at",
    "str-codepoint-at", "str-codepoint-length", "str-valid-utf8",
    "str->utf16le", "str->list", "str->arrayb", "str->utf16le-bytes",
    "str->int", "str->float", "str-append", "arrayb->str",
    "utf16le->str", "utf16le-bytes->str",
    "+", "-", "*", "/", "%", "==", "<", "<=", ">", ">=",
    "not", "and", "or", "bit-and", "bit-or", "bit-xor", "shl", "shr",
    "popcount", "abs", "int->float", "float->int",
}

# Primitives that build a new aggregate from their arguments (so passing a
# parameter to them stores it).
STORING_PRIMS = {
    "cons", "arrayf-set", "arrayi-set", "arrayb-set", "array-set",
    "array-of", "array-new", "write-file", "write-bytes", "write-f64s",
    "write-i64s", "fix", "immortalize",
}


def _iter_defs(program: Node):
    """Yield every ``Def`` in the program.

    ``walk`` deliberately visits definition *values*, not the ``Def`` wrapper
    (the resolver only needs the values), so definition-level analyses need
    their own traversal over every block.
    """

    for node in walk(program):
        if isinstance(node, Block):
            for d in node.defs:
                yield d


def _reachable_defs(program: Node) -> Set[int]:
    """Binding uids of definitions that can run from program entry.

    Every non-function top-level definition is evaluated at startup, and the
    final expression runs, so both are roots.  A function is included when a
    reached expression refers to it; this keeps warnings about unused library
    loops out of a user program's output.
    """

    defs = {d.binding.uid: d for d in _iter_defs(program) if d.binding is not None}
    roots: List[Node] = []
    if isinstance(program, Block):
        roots.append(program.body)
        for d in program.defs:
            if not isinstance(d.value, Lam):
                roots.append(d.value)
    else:
        roots.append(program)
    reached: Set[int] = set()
    queue: List[int] = []

    def scan(node: Node) -> None:
        for n in walk(node):
            if isinstance(n, Var) and n.binding is not None:
                uid = n.binding.uid
                if uid in defs and uid not in reached:
                    reached.add(uid)
                    queue.append(uid)

    for r in roots:
        scan(r)
    while queue:
        uid = queue.pop()
        d = defs[uid]
        scan(d.value)
    return reached


# ---------------------------------------------------------------------------
# Halting / rank analysis
# ---------------------------------------------------------------------------
def _app_parts(node: Node) -> Tuple[Optional[Binding], List[Node]]:
    if isinstance(node, App) and isinstance(node.func, Var):
        return node.func.binding, node.args
    return None, []


def _const_int(node: Node) -> Optional[int]:
    if isinstance(node, Lit) and isinstance(node.value, int) and not isinstance(
            node.value, bool):
        return node.value
    return None


def _var_uid(node: Node) -> Optional[int]:
    if isinstance(node, Var) and node.binding is not None:
        return node.binding.uid
    return None


def _guard_bounds(cond: Node, uid: int) -> bool:
    """True when *cond* bounds a recursion variable against a constant."""
    b, args = _app_parts(cond)
    if b is None or b.kind != PRIM or len(args) != 2:
        return False
    name = b.name
    if name not in ("<", "<=", ">", ">=", "=="):
        return False
    left, right = args
    if _var_uid(left) == uid and _const_int(right) is not None:
        return True
    if _var_uid(right) == uid and _const_int(left) is not None:
        return True
    return False


def _guard_empty(cond: Node, uid: int) -> bool:
    b, args = _app_parts(cond)
    return (b is not None and b.kind == PRIM and b.name == "empty?"
            and len(args) == 1 and _var_uid(args[0]) == uid)


def _guard_upper(cond: Node, uid: int) -> Optional[Node]:
    """Return the upper bound when *cond* says ``p < limit`` (or similar)."""
    b, args = _app_parts(cond)
    if b is None or b.kind != PRIM or len(args) != 2:
        return None
    left, right = args
    if _var_uid(left) == uid and b.name in ("<", "<=", ">", ">="):
        return right
    if _var_uid(right) == uid and b.name in (">", ">=", "<", "<="):
        return left
    return None


def _increments(arg: Node, uid: int) -> bool:
    b, args = _app_parts(arg)
    if b is None or b.kind != PRIM or len(args) != 2:
        return False
    if b.name == "+" and _var_uid(args[0]) == uid:
        k = _const_int(args[1])
        return k is not None and k >= 1
    return False


def _stable_limit(limit: Node, call: App, lam: Lam) -> bool:
    """Whether every variable in *limit* is passed unchanged by *call*."""
    for n in walk(limit):
        if not isinstance(n, Var) or n.binding is None:
            continue
        pos = None
        for i, p in enumerate(lam.param_bindings):
            if p.uid == n.binding.uid:
                pos = i
                break
        if pos is None:
            continue
        if pos >= len(call.args) or _var_uid(call.args[pos]) != n.binding.uid:
            return False
    return True


def _decrements(arg: Node, uid: int) -> bool:
    b, args = _app_parts(arg)
    if b is None or b.kind != PRIM or len(args) != 2:
        return False
    if b.name == "-" and _var_uid(args[0]) == uid:
        k = _const_int(args[1])
        return k is not None and k >= 1
    # (+ p -k) is written as (+ p (- 0 k)) occasionally; accept the direct
    # literal form (- p k) only, which keeps the analysis predictable.
    return False


def _tail_of(arg: Node, uid: int) -> bool:
    b, args = _app_parts(arg)
    return (b is not None and b.kind == PRIM and b.name == "tail"
            and len(args) == 1 and _var_uid(args[0]) == uid)


def _collect_calls(node: Node, uid: int, out: List[App]) -> None:
    """Collect direct self-calls, not descending into nested lambdas."""
    if isinstance(node, Lam):
        return
    if isinstance(node, App):
        b = node.func.binding if isinstance(node.func, Var) else None
        if b is not None and b.uid == uid:
            out.append(node)
        _collect_calls(node.func, uid, out)
        for a in node.args:
            _collect_calls(a, uid, out)
        return
    if isinstance(node, Block):
        for d in node.defs:
            _collect_calls(d.value, uid, out)
        _collect_calls(node.body, uid, out)
    elif isinstance(node, If):
        _collect_calls(node.cond, uid, out)
        _collect_calls(node.then, uid, out)
        _collect_calls(node.else_, uid, out)
    elif isinstance(node, Match):
        _collect_calls(node.subject, uid, out)
        for _, arm in node.arms:
            _collect_calls(arm, uid, out)
    elif isinstance(node, (Borrow,)):
        _collect_calls(node.value, uid, out)


def _guards(node: Node, uid: int, out: List[Node]) -> None:
    """Collect conditions that bound *uid* anywhere in the body."""
    if isinstance(node, Lam):
        return
    if isinstance(node, If):
        if (_guard_bounds(node.cond, uid) or _guard_empty(node.cond, uid)
                or _guard_upper(node.cond, uid) is not None):
            out.append(node.cond)
        _guards(node.cond, uid, out)
        _guards(node.then, uid, out)
        _guards(node.else_, uid, out)
    elif isinstance(node, Block):
        for d in node.defs:
            _guards(d.value, uid, out)
        _guards(node.body, uid, out)
    elif isinstance(node, Match):
        _guards(node.subject, uid, out)
        for _, arm in node.arms:
            _guards(arm, uid, out)
    elif isinstance(node, App):
        for a in node.args:
            _guards(a, uid, out)
        _guards(node.func, uid, out)
    elif isinstance(node, Borrow):
        _guards(node.value, uid, out)


def _proved_for(lam: Lam, uid: int) -> Tuple[bool, Optional[str]]:
    calls: List[App] = []
    _collect_calls(lam.body, uid, calls)
    if not calls:
        return True, None
    for pidx, param in enumerate(lam.param_bindings):
        guards: List[Node] = []
        _guards(lam.body, param.uid, guards)
        if not guards:
            continue
        bounded_numeric = any(_guard_bounds(g, param.uid) for g in guards)
        bounded_list = any(_guard_empty(g, param.uid) for g in guards)
        upper = None
        for g in guards:
            upper = _guard_upper(g, param.uid)
            if upper is not None:
                break
        if not (bounded_numeric or bounded_list or upper is not None):
            continue
        ok = True
        for call in calls:
            if pidx >= len(call.args):
                ok = False
                break
            arg = call.args[pidx]
            if bounded_list:
                decreases = _tail_of(arg, param.uid)
            elif bounded_numeric:
                decreases = _decrements(arg, param.uid)
            else:
                decreases = False
            if decreases:
                continue
            if (upper is not None and _increments(arg, param.uid)
                    and _stable_limit(upper, call, lam)):
                continue
            ok = False
            break
        if ok:
            return True, param.name
    return False, "recursive argument does not strictly decrease a bounded rank"


def check_halting(program: Node, source: str = "<input>",
                  enabled: bool = True) -> List[str]:
    """Warn about recursive functions whose termination cannot be shown.

    A function is accepted when every direct self-call passes a parameter that
    is (a) guarded against a constant and (b) decremented by a positive
    constant, or (a) guarded by ``empty?`` and (b) replaced by ``tail``.  This
    is a proof for that variable; all other recursion is a warning only.
    """

    if not enabled:
        return []
    messages: List[str] = []
    seen: Set[int] = set()
    reachable = _reachable_defs(program)
    for node in _iter_defs(program):
        if not isinstance(node, Def) or node.binding is None:
            continue
        if node.binding.uid not in reachable:
            continue
        lam = node.value if isinstance(node.value, Lam) else None
        if lam is None:
            continue
        uid = node.binding.uid
        if uid in seen:
            continue
        seen.add(uid)
        calls: List[App] = []
        _collect_calls(lam.body, uid, calls)
        if not calls:
            continue
        proved, reason = _proved_for(lam, uid)
        if not proved:
            loc = node.loc
            messages.append(
                f"{loc.source}:{loc.line}:{loc.col}: warning: recursion in "
                f"'{node.name}' may not halt ({reason}); add a base case with "
                f"a strictly decreasing, bounded rank variable, or restructure "
                f"as a structural fold")
    for msg in messages:
        print(msg, file=__import__("sys").stderr)
    return messages


# ---------------------------------------------------------------------------
# Borrow inference
# ---------------------------------------------------------------------------
# Uses are classified from the inside out.  A parameter is borrowable when it
# is only inspected: read by a primitive, compared, or passed to a parameter
# that is itself inferred borrowed.  Returning it, capturing it in a closure,
# storing it in a list/constructor/array, or passing it to a consuming
# function makes it escape.
def _param_uid_uses(lam: Lam, uid: int) -> Tuple[bool, Set[Tuple[int, int]]]:
    """Return (escapes, (callee_uid, position) pairs it is passed to)."""
    escapes = False
    forwarded: Set[Tuple[int, int]] = set()

    def visit(node: Node, tail: bool, nested: bool) -> None:
        nonlocal escapes
        if isinstance(node, Var):
            if node.binding is not None and node.binding.uid == uid:
                if tail or nested:
                    escapes = True
            return
        if isinstance(node, Lam):
            # Any reference inside a nested lambda would become a capture.
            for n in walk(node.body):
                if isinstance(n, Var) and n.binding is not None and n.binding.uid == uid:
                    escapes = True
                    return
            return
        if isinstance(node, App):
            target = node.func.binding if isinstance(node.func, Var) else None
            for i, arg in enumerate(node.args):
                if _var_uid(arg) == uid:
                    if target is None:
                        escapes = True
                    elif target.kind == PRIM:
                        if target.name not in READONLY_PRIMS:
                            escapes = True
                    else:
                        forwarded.add((target.uid, i))
                else:
                    visit(arg, False, nested)
            visit(node.func, False, nested)
            return
        if isinstance(node, If):
            visit(node.cond, False, nested)
            visit(node.then, tail, nested)
            visit(node.else_, tail, nested)
            return
        if isinstance(node, Match):
            visit(node.subject, False, nested)
            for _, arm in node.arms:
                visit(arm, tail, nested)
            return
        if isinstance(node, Block):
            for d in node.defs:
                visit(d.value, False, nested)
            visit(node.body, tail, nested)
            return
        if isinstance(node, (Borrow,)):
            visit(node.value, tail, nested)
            return
        # Constructors (Record/Sum/Data/List) store their arguments.
        args = getattr(node, "args", None)
        if args is not None:
            for a in args:
                if _var_uid(a) == uid:
                    escapes = True
                else:
                    visit(a, False, nested)

    visit(lam.body, True, False)
    return escapes, forwarded


def infer_borrows(program: Node) -> int:
    """Infer non-consuming parameters; return the number inferred.

    The analysis is a greatest fixed point: start by assuming every candidate
    parameter is borrowed, then demote any parameter that escapes or is passed
    to a parameter that was demoted.  Explicit ``&`` parameters are never
    demoted; ``: linear`` parameters are never candidates.
    """

    funcs: Dict[int, Tuple[Def, Lam]] = {}
    binding_uses: Dict[int, int] = {}
    for node in _iter_defs(program):
        if isinstance(node, Def) and isinstance(node.value, Lam) and node.binding:
            funcs[node.binding.uid] = (node, node.value)
    for node in walk(program):
        if isinstance(node, Var) and node.binding is not None:
            binding_uses[node.binding.uid] = binding_uses.get(node.binding.uid, 0) + 1

    # A function used as a value (not just applied) cannot carry borrowed
    # parameters: the current ABI rejects first-class functions with borrows.
    first_class: Set[int] = set()
    direct_uses: Dict[int, int] = {}
    for node in walk(program):
        if isinstance(node, App) and isinstance(node.func, Var) and node.func.binding is not None:
            direct_uses[node.func.binding.uid] = direct_uses.get(node.func.binding.uid, 0) + 1
    for uid, count in binding_uses.items():
        if uid in funcs and count > direct_uses.get(uid, 0):
            first_class.add(uid)

    borrowed: Dict[Tuple[int, int], bool] = {}
    explicit: Dict[Tuple[int, int], bool] = {}
    linear: Dict[Tuple[int, int], bool] = {}
    for uid, (d, lam) in funcs.items():
        for i, b in enumerate(lam.param_bindings):
            if b is None:
                continue
            if uid in first_class:
                borrowed[(uid, i)] = False
            else:
                borrowed[(uid, i)] = True
            explicit[(uid, i)] = bool(
                i < len(lam.param_borrows) and lam.param_borrows[i])
            if i < len(getattr(lam, "param_linear", [])) and lam.param_linear[i]:
                linear[(uid, i)] = True
                borrowed[(uid, i)] = False
            if explicit[(uid, i)]:
                borrowed[(uid, i)] = True

    # Tail calls with a temporary argument cannot use ``musttail`` when the
    # callee parameter is borrowed: the temporary needs a caller-side release,
    # which a tail call cannot perform.  Demote such parameters so recursive
    # functions keep constant-stack behaviour (e.g. string/list accumulators).
    forced_consume: Set[Tuple[int, int]] = set()
    for uid, (d, lam) in funcs.items():
        calls: List[App] = []
        _collect_calls(lam.body, uid, calls)
        for call in calls:
            for pos, arg in enumerate(call.args):
                if pos < len(lam.param_bindings) and not isinstance(arg, Var):
                    forced_consume.add((uid, pos))
    for key in forced_consume:
        borrowed[key] = False

    changed = True
    while changed:
        changed = False
        for uid, (d, lam) in funcs.items():
            if uid in first_class:
                continue
            for i, b in enumerate(lam.param_bindings):
                key = (uid, i)
                if not borrowed.get(key) or explicit.get(key):
                    continue
                esc, forwarded = _param_uid_uses(lam, b.uid)
                if esc:
                    borrowed[key] = False
                    changed = True
                    continue
                for callee_uid, pos in forwarded:
                    if callee_uid in funcs and not borrowed.get((callee_uid, pos), True):
                        borrowed[key] = False
                        changed = True
                        break

    count = 0
    for uid, (d, lam) in funcs.items():
        for i, b in enumerate(lam.param_bindings):
            ptype = prune(b.type) if b.type is not None else None
            # Auto-borrow only the representations whose non-owning ABI is
            # well-tested: flat arrays, strings and lists.  Aggregate data
            # values are matched/destructured and explicit `&` remains the
            # user's tool there.
            boxed = False
            if ptype is not None and not isinstance(ptype, (TVar, TNumVar)):
                if isinstance(ptype, TList):
                    boxed = True
                elif isinstance(ptype, TCon) and ptype.name in (
                        "ArrayF", "ArrayI", "ArrayB", "String"):
                    boxed = True
            if borrowed.get((uid, i)) and not explicit.get((uid, i)):
                if not boxed:
                    continue
                if i < len(lam.param_borrows):
                    lam.param_borrows[i] = True
                b.borrowed = True
                count += 1
            elif explicit.get((uid, i)):
                if i < len(lam.param_borrows):
                    lam.param_borrows[i] = True
                b.borrowed = True
    return count

# ---------------------------------------------------------------------------
# World linearity
# ---------------------------------------------------------------------------
def _is_world(t) -> bool:
    t = prune(t) if t is not None else None
    return isinstance(t, TCon) and t.name == "World"


def check_world_linearity(program: Node) -> int:
    """Enforce the nominal, consumed-world discipline.

    World objects are linear per step: no alias bindings, no passing one world
    twice to the same call, no storing a world in a container, and no returning
    a parameter without an explicit ``world-clone``.  Array-style use-after-
    consume is already rejected by code generation; this pass closes the alias
    and retention holes that type inference alone cannot see.
    """

    errors: List[TypeError_] = []

    def fail(message: str, loc) -> None:
        errors.append(TypeError_(message, loc))

    # Alias bindings and storing constructors.
    for d in _iter_defs(program):
        if d.binding is None or not _is_world(d.binding.type):
            continue
        if isinstance(d.value, Var) and _is_world(d.value.type):
            fail(
                f"world value '{d.name}' aliases another world; worlds are "
                f"linear per step (use world-clone only to retain a snapshot)",
                d.loc)
        if isinstance(d.value, Borrow):
            fail(f"cannot bind a borrow of a world as '{d.name}'", d.loc)

    for n in walk(program):
        if isinstance(n, App):
            seen = set()
            for a in n.args:
                if not (isinstance(a, Var) and _is_world(a.type)):
                    continue
                if a.binding is None:
                    continue
                if a.binding.uid in seen:
                    fail(
                        "the same world value is passed twice to one call; "
                        "worlds are consumed once per step", a.loc)
                seen.add(a.binding.uid)
            func = n.func
            if (isinstance(func, Var) and func.binding is not None
                    and func.binding.kind == PRIM
                    and func.binding.name in ("cons", "array-set", "array-new",
                                              "array-of")
                    and any(_is_world(a.type) for a in n.args)):
                fail("cannot store a world in a container; worlds are nominal "
                     "linear state", n.loc)
        if isinstance(n, (DataCtor, RecordLit, SumLit)):
            if any(_is_world(a.type) for a in getattr(n, "args", ())):
                fail("cannot store a world in a nominal value", n.loc)

    # Returning a world parameter without cloning retains the previous state.
    for n in walk(program):
        if not isinstance(n, Lam):
            continue
        param_uids = {b.uid for b in n.param_bindings}
        tail = n.body
        while isinstance(tail, Block):
            tail = tail.body
        if isinstance(tail, Var) and tail.binding is not None:
            if tail.binding.uid in param_uids and _is_world(tail.type):
                fail(
                    "returning a world parameter without world-clone retains "
                    "the previous state; use world-clone for an explicit "
                    "snapshot", tail.loc)

    for err in errors[:20]:
        raise err
    return len(errors)

