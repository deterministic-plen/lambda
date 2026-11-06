"""Semantic analysis passes.

* capture analysis (flat closures with explicit captured parameters),
* purity analysis,
* common-subexpression sharing ("memoize" repeated pure expressions),
* top-level initialisation ordering.

The sharing pass runs on a resolved AST.  It introduces fresh ``__share_N``
definitions; the driver then re-resolves and re-infers the transformed program.
"""

from __future__ import annotations

from collections import defaultdict
from typing import Dict, List, Optional, Set, Tuple

from ast import (
    App, Binding, Block, Def, If, Lam, Lit, LOCAL_DEF, Node, PARAM, PRIM,
    TOP_DEF, Var, walk,
)
from errors import CompileError
from parser import ARITH_OPS, COMPARE_OPS, OP_CANON, PRIMITIVES

IMPURE_PRIMITIVES = {
    "print", "println", "read-file", "read_line", "read_int", "read_float",
    "map", "pmap",
    # Arrays are a controlled low-level effect: a set consumes and mutates its
    # buffer.  Keep all array operations out of CSE and λp/pmap.
    "arrayf", "arrayi", "arrayb",
    "arrayf_get", "arrayf_set", "arrayf_length", "arrayf_clone",
    "arrayi_get", "arrayi_set", "arrayi_length", "arrayi_clone",
    "arrayb_get", "arrayb_set", "arrayb_length", "arrayb_clone",
    "arrayi_pmap",
    "arrayi_pmap_chunk",
    "write_bytes", "write_f64s", "write_i64s",
}

PRIM_ARITY = {
    "add": 2, "sub": 2, "mul": 2, "div": 2, "mod": 2,
    "eq": 2, "lt": 2, "le": 2, "gt": 2, "ge": 2,
    "cons": 2, "head": 1, "tail": 1, "empty": 1, "nil": 0,
    "str_to_int": 1, "int_to_str": 1, "str_to_list": 1, "str_len": 1,
    "str_append": 2, "read_file": 1, "print": 1, "println": 1, "not": 1,
    "fix": 1,
    "int_to_float": 1, "float_to_int": 1, "str_to_float": 1, "float_to_str": 1,
    "sqrt": 1, "sin": 1, "cos": 1, "tan": 1, "asin": 1, "acos": 1, "atan": 1,
    "atan2": 2, "exp": 1, "ln": 1, "log10": 1, "log2": 1,
    "floor": 1, "ceil": 1, "round": 1, "trunc": 1, "pow": 2, "abs": 1,
    "pi": 0, "e": 0, "sum": 1, "product": 1,
    "bit_and": 2, "bit_or": 2, "bit_xor": 2, "bit_not": 1,
    "shl": 2, "shr": 2, "popcount": 1,
    "range": 2, "map": 2, "pmap": 2, "immortalize": 1,
    "arrayf": 2, "arrayi": 2, "arrayb": 2,
    "arrayf_get": 2, "arrayf_set": 3, "arrayf_length": 1, "arrayf_clone": 1,
    "arrayi_get": 2, "arrayi_set": 3, "arrayi_length": 1, "arrayi_clone": 1,
    "arrayb_get": 2, "arrayb_set": 3, "arrayb_length": 1, "arrayb_clone": 1,
    "arrayf_get_ro": 2, "arrayi_get_ro": 2, "arrayb_get_ro": 2,
    "arrayi_pmap": 2, "arrayi_pmap_chunk": 3,
    "write_bytes": 1, "write_f64s": 1, "write_i64s": 1,
    "read_line": 1, "read_int": 1, "read_float": 1,
}


# ---------------------------------------------------------------------------
# Capture analysis
# ---------------------------------------------------------------------------
def _all_lams(node: Node) -> List[Lam]:
    return [n for n in walk(node) if isinstance(n, Lam)]


def _bound_inside(lam: Lam) -> Set[int]:
    """Bindings introduced inside *lam* (parameters of every nested lambda and
    every definition in every nested block)."""

    bound: Set[int] = {b.uid for b in lam.param_bindings}

    def visit(n: Node) -> None:
        if isinstance(n, Lam):
            for b in n.param_bindings:
                bound.add(b.uid)
            visit(n.body)
        elif isinstance(n, Block):
            for d in n.defs:
                if d.binding is not None:
                    bound.add(d.binding.uid)
                visit(d.value)
            visit(n.body)
        elif isinstance(n, If):
            visit(n.cond)
            visit(n.then)
            visit(n.else_)
        elif isinstance(n, App):
            visit(n.func)
            for a in n.args:
                visit(a)

    visit(lam.body)
    return bound


def _collect_refs(lam: Lam) -> Tuple[Set[int], Set[int]]:
    """Return (value binding uids, referenced function-def binding uids)."""

    values: Set[int] = set()
    funcs: Set[int] = set()

    def visit(n: Node) -> None:
        if isinstance(n, Var):
            b = n.binding
            if b is None:
                return
            if b.kind in (PARAM, LOCAL_DEF):
                if b.is_function:
                    funcs.add(b.uid)
                else:
                    values.add(b.uid)
            return
        if isinstance(n, Lam):
            visit(n.body)
        elif isinstance(n, Block):
            for d in n.defs:
                visit(d.value)
            visit(n.body)
        elif isinstance(n, If):
            visit(n.cond)
            visit(n.then)
            visit(n.else_)
        elif isinstance(n, App):
            visit(n.func)
            for a in n.args:
                visit(a)

    visit(lam.body)
    return values, funcs


def compute_captures(program: Node) -> None:
    """Annotate every ``Lam`` with its captured value bindings.

    Function definitions are *not* captured as values: a reference to one is
    turned into a closure on demand from that function's own captures.  This
    keeps recursive and mutually recursive functions out of the capture graph
    (and therefore cycle-free).
    """

    lams = _all_lams(program)
    bound: Dict[int, Set[int]] = {}
    value_refs: Dict[int, Set[int]] = {}
    edge_uids: Dict[int, Set[int]] = {}

    for lam in lams:
        bound[id(lam)] = _bound_inside(lam)
        v, f = _collect_refs(lam)
        value_refs[id(lam)] = v
        edge_uids[id(lam)] = f

    # Map function-definition uid -> its Lam node.
    by_uid: Dict[int, Lam] = {}
    for n in walk(program):
        if isinstance(n, Block):
            for d in n.defs:
                if d.binding is not None and d.binding.is_function and isinstance(d.value, Lam):
                    by_uid[d.binding.uid] = d.value

    caps: Dict[int, Set[int]] = {}
    for lam in lams:
        caps[id(lam)] = set(value_refs[id(lam)]) - bound[id(lam)]

    changed = True
    rounds = 0
    while changed:
        changed = False
        rounds += 1
        if rounds > max(8, len(lams) + 2):
            break
        for lam in lams:
            add: Set[int] = set()
            for uid in edge_uids[id(lam)]:
                target = by_uid.get(uid)
                if target is not None and target is not lam:
                    add |= caps.get(id(target), set())
            add -= bound[id(lam)]
            before = len(caps[id(lam)])
            caps[id(lam)] |= add
            if len(caps[id(lam)]) != before:
                changed = True

    # Bindings by uid, for ordering.
    all_bindings: Dict[int, Binding] = {}
    for n in walk(program):
        if isinstance(n, Block):
            for d in n.defs:
                if d.binding is not None:
                    all_bindings[d.binding.uid] = d.binding
        if isinstance(n, Lam):
            for b in n.param_bindings:
                all_bindings[b.uid] = b

    for lam in lams:
        selected = [all_bindings[u] for u in caps[id(lam)] if u in all_bindings]
        selected.sort(key=lambda b: b.uid)
        lam.captures = selected
        lam.capture_set = {b.uid for b in selected}


# ---------------------------------------------------------------------------
# Purity
# ---------------------------------------------------------------------------
def _head_and_args(app: App) -> Tuple[Node, int]:
    head: Node = app.func
    nargs = len(app.args)
    while isinstance(head, App):
        nargs += len(head.args)
        head = head.func
    return head, nargs


def _defined_arity(binding: Binding) -> Optional[int]:
    if binding.lam is not None:
        return len(binding.lam.params)
    if binding.kind == PRIM:
        op = OP_CANON.get(binding.name)
        return PRIM_ARITY.get(op) if op else None
    return None


def compute_purity(program: Node) -> Dict[int, bool]:
    """Fixpoint purity for function definitions.

    A function is pure if every call it performs is a call to a pure primitive
    or a pure function, and it does not call a value of unknown origin.  Only
    *executed* code counts: bodies of nested lambdas run later and are not
    scanned.
    """

    funcs: Dict[int, Binding] = {}
    for n in walk(program):
        if isinstance(n, Block):
            for d in n.defs:
                b = d.binding
                if b is not None and b.is_function and b.lam is not None:
                    funcs[b.uid] = b

    pure: Dict[int, bool] = {uid: True for uid in funcs}

    def call_pure(app: App) -> bool:
        head, nargs = _head_and_args(app)
        if not isinstance(head, Var) or head.binding is None:
            return False
        b = head.binding
        if b.kind == PRIM:
            op = OP_CANON.get(b.name)
            if op is None:
                return False
            if op in IMPURE_PRIMITIVES:
                return False
            arity = PRIM_ARITY.get(op)
            return arity is not None and nargs <= arity
        if b.is_function:
            arity = _defined_arity(b)
            if arity is None or nargs > arity:
                return False
            return pure.get(b.uid, False)
        return False

    def scan(n: Node) -> bool:
        if isinstance(n, Lam):
            # Creating a closure has no effects; the body runs when called.
            return True
        if isinstance(n, Block):
            for d in n.defs:
                if isinstance(d.value, Lam):
                    continue
                if not scan(d.value):
                    return False
            return scan(n.body)
        if isinstance(n, If):
            return scan(n.cond) and scan(n.then) and scan(n.else_)
        if isinstance(n, App):
            if not call_pure(n):
                return False
            if not scan(n.func):
                return False
            return all(scan(a) for a in n.args)
        return True

    changed = True
    while changed:
        changed = False
        for uid, b in funcs.items():
            if pure[uid] and not scan(b.lam.body):
                pure[uid] = False
                changed = True
    return pure


# ---------------------------------------------------------------------------
# Lambda purity (λp and pmap)
# ---------------------------------------------------------------------------
def compute_lambda_purity(program: Node) -> Dict[int, bool]:
    """Purity of every lambda, including anonymous ones.

    This is the guarantee behind ``λp`` and parallel map: if a lambda is pure
    and only reads immutable captured values, separate applications of it are
    independent and may run on different threads.
    """

    lams = [n for n in walk(program) if isinstance(n, Lam)]
    pure: Dict[int, bool] = {id(lam): True for lam in lams}

    def lam_of(binding: Optional[Binding]) -> Optional[Lam]:
        if binding is not None and binding.is_function and binding.lam is not None:
            return binding.lam
        return None

    def callable_pure(n: Node) -> bool:
        if isinstance(n, Lam):
            return pure.get(id(n), False)
        if isinstance(n, Var) and n.binding is not None:
            b = n.binding
            if b.kind == PRIM:
                op = OP_CANON.get(b.name)
                if op is None or op in IMPURE_PRIMITIVES:
                    return False
                # map/pmap need a known pure function argument; as a bare
                # function value we cannot prove that here.
                return op not in ("map", "pmap")
            lam = lam_of(b)
            if lam is not None:
                return pure.get(id(lam), False)
        return False

    def expr_pure(n: Node) -> bool:
        if isinstance(n, (Lit, Var, Lam)):
            return True
        if isinstance(n, If):
            return (expr_pure(n.cond) and expr_pure(n.then)
                    and expr_pure(n.else_))
        if isinstance(n, Block):
            for d in n.defs:
                if not isinstance(d.value, Lam) and not expr_pure(d.value):
                    return False
            return expr_pure(n.body)
        if isinstance(n, App):
            head, _ = _head_and_args(n)
            if not all(expr_pure(a) for a in n.args):
                return False
            if isinstance(head, Var) and head.binding is not None:
                b = head.binding
                if b.kind == PRIM:
                    op = OP_CANON.get(b.name)
                    if op is None:
                        return False
                    if op in ("map", "pmap"):
                        if len(n.args) < 2:
                            return False
                        return (callable_pure(n.args[0])
                                and expr_pure(n.args[1])
                                and all(expr_pure(a) for a in n.args[2:]))
                    if op in IMPURE_PRIMITIVES:
                        return False
                    # Pure primitives, including math and fix.
                    return True
                lam = lam_of(b)
                if lam is not None:
                    return pure.get(id(lam), False)
                return False
            if isinstance(head, Lam):
                return pure.get(id(head), False)
            return False
        return False

    changed = True
    while changed:
        changed = False
        for lam in lams:
            if pure[id(lam)] and not expr_pure(lam.body):
                pure[id(lam)] = False
                changed = True
    return pure


def validate_parallel_lambdas(program: Node, purity: Dict[int, bool]) -> None:
    """Reject ``λp`` bodies that the purity analysis cannot prove pure."""

    for n in walk(program):
        if isinstance(n, Lam) and n.is_parallel:
            if not purity.get(id(n), False):
                raise CompileError(
                    "λp function body must be pure: no printing, terminal "
                    "input, or calls through unknown function values", n.loc)


# ---------------------------------------------------------------------------
# Common-subexpression sharing
# ---------------------------------------------------------------------------
class _Share:
    def __init__(self, purity: Dict[int, bool]) -> None:
        self.purity = purity
        self.counter = 0

    # -- purity / keys ----------------------------------------------------
    def call_pure(self, app: App) -> bool:
        head, nargs = _head_and_args(app)
        if not isinstance(head, Var) or head.binding is None:
            return False
        b = head.binding
        if b.kind == PRIM:
            op = OP_CANON.get(b.name)
            if op is None or op in IMPURE_PRIMITIVES:
                return False
            arity = PRIM_ARITY.get(op)
            return arity is not None and nargs <= arity
        if b.is_function:
            arity = _defined_arity(b)
            return arity is not None and nargs <= arity and self.purity.get(b.uid, False)
        return False

    def simple_pure(self, n: Node) -> bool:
        """Is *n* a pure, straight-line expression (no lambdas/blocks/ifs)?"""

        if isinstance(n, (Lit, Var)):
            return True
        if isinstance(n, App):
            if not self.call_pure(n):
                return False
            if not self.simple_pure(n.func):
                return False
            return all(self.simple_pure(a) for a in n.args)
        return False

    def key(self, n: Node) -> Optional[str]:
        if isinstance(n, Lit):
            return f"l{type(n.value).__name__}:{n.value!r}"
        if isinstance(n, Var):
            if n.binding is None:
                return None
            return f"v{n.binding.uid}"
        if isinstance(n, App):
            fk = self.key(n.func)
            if fk is None:
                return None
            parts = [self.key(a) for a in n.args]
            if any(p is None for p in parts):
                return None
            return "a(" + fk + ";" + ",".join(parts) + ")"
        return None

    # -- counting ---------------------------------------------------------
    def count(self, n: Node, counts: Dict[str, int]) -> None:
        if isinstance(n, (Lit, Var)):
            return
        if isinstance(n, (Lam, Block)):
            return
        if isinstance(n, If):
            self.count(n.cond, counts)
            return
        if isinstance(n, App):
            self.count(n.func, counts)
            for a in n.args:
                self.count(a, counts)
            if self.simple_pure(n):
                k = self.key(n)
                if k is not None:
                    counts[k] = counts.get(k, 0) + 1
            return

    # -- rewriting --------------------------------------------------------
    def rewrite(self, n: Node, selected: Set[str], cache: Dict[str, str],
                prelude: List[Def]) -> Node:
        if isinstance(n, Lam):
            n.body = self.wrap_region(n.body)
            return n
        if isinstance(n, Block):
            return self.block(n)
        if isinstance(n, If):
            n.cond = self.rewrite(n.cond, selected, cache, prelude)
            n.then = self.wrap_region(n.then)
            n.else_ = self.wrap_region(n.else_)
            return n
        if isinstance(n, App):
            # Decide using the *original* key (children may be replaced by
            # shared variables while rewriting).
            candidate = None
            if self.simple_pure(n):
                candidate = self.key(n)
            if candidate is not None and candidate in selected:
                existing = cache.get(candidate)
                if existing is not None:
                    return Var(existing, n.loc)
                self.counter += 1
                name = f"__share_{self.counter}"
                cache[candidate] = name
                # Rewrite children first so inner sharing definitions come
                # earlier in the prelude.
                n.func = self.rewrite(n.func, selected, cache, prelude)
                n.args = [self.rewrite(a, selected, cache, prelude) for a in n.args]
                prelude.append(Def(name, n, n.loc))
                return Var(name, n.loc)
            n.func = self.rewrite(n.func, selected, cache, prelude)
            n.args = [self.rewrite(a, selected, cache, prelude) for a in n.args]
            return n
        return n

    def region(self, n: Node) -> Tuple[Node, List[Def]]:
        """Optimize one straight-line evaluation region.

        Returns the rewritten expression and the sharing definitions that must
        be evaluated at the start of the region.
        """

        if isinstance(n, Block):
            return self.block(n), []
        counts: Dict[str, int] = {}
        self.count(n, counts)
        selected = {k for k, c in counts.items() if c >= 2}
        if not selected:
            return self.rewrite(n, set(), {}, []), []
        prelude: List[Def] = []
        body = self.rewrite(n, selected, {}, prelude)
        return body, prelude

    def wrap_region(self, n: Node) -> Node:
        body, prelude = self.region(n)
        if prelude:
            return Block(prelude, body, n.loc)
        return body

    def block(self, block: Block) -> Block:
        for d in block.defs:
            value, prelude = self.region(d.value)
            d.value = Block(prelude, value, d.value.loc) if prelude else value
        body, prelude = self.region(block.body)
        block.defs.extend(prelude)
        block.body = body
        return block


def share_common_subexpressions(program: Node) -> Node:
    purity = compute_purity(program)
    return _Share(purity).block(program)


# ---------------------------------------------------------------------------
# Top-level initialisation order
# ---------------------------------------------------------------------------
def top_level_order(block: Block) -> List[Def]:
    """Order non-function top-level definitions so values initialise before use."""

    value_defs: List[Def] = []
    by_uid: Dict[int, Def] = {}
    for d in block.defs:
        if d.binding is None:
            continue
        if d.is_function:
            continue
        value_defs.append(d)
        by_uid[d.binding.uid] = d

    deps: Dict[int, List[int]] = {d.binding.uid: [] for d in value_defs}

    def scan(n: Node, deps_out: List[int]) -> None:
        if isinstance(n, Var):
            b = n.binding
            if b is not None and b.uid in by_uid and b.uid not in deps_out:
                deps_out.append(b.uid)
            return
        if isinstance(n, Lam):
            # A body runs later; it does not constrain initialisation.
            return
        if isinstance(n, Block):
            for d in n.defs:
                if not isinstance(d.value, Lam):
                    scan(d.value, deps_out)
            scan(n.body, deps_out)
            return
        if isinstance(n, If):
            scan(n.cond, deps_out)
            scan(n.then, deps_out)
            scan(n.else_, deps_out)
            return
        if isinstance(n, App):
            scan(n.func, deps_out)
            for a in n.args:
                scan(a, deps_out)

    for d in value_defs:
        scan(d.value, deps[d.binding.uid])

    ordered: List[Def] = []
    state: Dict[int, int] = {}  # 0 = visiting, 2 = done

    def visit(uid: int) -> None:
        st = state.get(uid, 0)
        if st == 2:
            return
        if st == 1:
            d = by_uid[uid]
            raise CompileError(
                f"recursive value definition '{d.name}' has no base case", d.loc)
        state[uid] = 1
        for dep in deps.get(uid, ()):
            visit(dep)
        state[uid] = 2
        ordered.append(by_uid[uid])

    for d in value_defs:
        visit(d.binding.uid)
    return ordered
