"""Hindley-Milner style type inference.

The language is dynamically typed at runtime, but inference gives every
expression a static type.  Code generation uses those types to choose an
unboxed (``i64``) or boxed (``%Value*``) representation.  A program that cannot
be typed (the classic example being self-application in a fixed-point
combinator) is still accepted: the driver falls back to fully boxed/dynamic
code generation instead of rejecting it.

Base types
----------
``Int``, ``Bool``, ``String``, ``[a]`` (list) and ``a -> b`` (function).
"""

from __future__ import annotations

from typing import Dict, Iterable, List, Optional, Set, Tuple

from ast import App, Binding, Block, Def, If, Lam, Lit, Node, PRIM, Var, walk
from errors import TypeError_

I64 = "i64"
F64 = "f64"
BOX = "box"


# ---------------------------------------------------------------------------
# Types
# ---------------------------------------------------------------------------
class Type:
    __slots__ = ()


class TVar(Type):
    __slots__ = ("id", "instance")
    _next_id = 0

    def __init__(self) -> None:
        TVar._next_id += 1
        self.id = TVar._next_id
        self.instance: Optional[Type] = None

    def __repr__(self) -> str:  # pragma: no cover
        return f"t{self.id}"


class TNumVar(Type):
    """A type variable constrained to Int or Float (numeric overloading)."""

    __slots__ = ("id", "instance")
    _next_id = 0

    def __init__(self) -> None:
        TNumVar._next_id += 1
        self.id = TNumVar._next_id
        self.instance: Optional[Type] = None

    def __repr__(self) -> str:  # pragma: no cover
        return f"n{self.id}"


class TCon(Type):
    __slots__ = ("name",)

    def __init__(self, name: str) -> None:
        self.name = name

    def __repr__(self) -> str:  # pragma: no cover
        return self.name


class TList(Type):
    __slots__ = ("elem",)

    def __init__(self, elem: Type) -> None:
        self.elem = elem

    def __repr__(self) -> str:  # pragma: no cover
        return f"[{self.elem!r}]"


class TFun(Type):
    __slots__ = ("arg", "ret")

    def __init__(self, arg: Type, ret: Type) -> None:
        self.arg = arg
        self.ret = ret

    def __repr__(self) -> str:  # pragma: no cover
        return f"({self.arg!r} -> {self.ret!r})"


TINT = TCon("Int")
TBOOL = TCon("Bool")
TSTR = TCon("String")
TFLOAT = TCon("Float")
TUNIT = TCon("Unit")
TARRAYF = TCon("ArrayF")
TARRAYI = TCon("ArrayI")
TARRAYB = TCon("ArrayB")


def tfun(args: Iterable[Type], ret: Type) -> Type:
    result = ret
    args = list(args)
    for a in reversed(args):
        result = TFun(a, result)
    return result


def prune(t: Type) -> Type:
    if isinstance(t, (TVar, TNumVar)):
        chain: List[Type] = []
        while isinstance(t, (TVar, TNumVar)) and t.instance is not None:
            chain.append(t)
            t = t.instance
        for v in chain:
            v.instance = t
    return t


def occurs(v: Type, t: Type) -> bool:
    t = prune(t)
    if t is v:
        return True
    if isinstance(t, TList):
        return occurs(v, t.elem)
    if isinstance(t, TFun):
        return occurs(v, t.arg) or occurs(v, t.ret)
    return False


class UnifyError(Exception):
    def __init__(self, message: str) -> None:
        self.message = message
        super().__init__(message)


def unify(a: Type, b: Type) -> None:
    a = prune(a)
    b = prune(b)
    if a is b:
        return
    if isinstance(a, TVar):
        if occurs(a, b):
            raise UnifyError(f"cannot construct infinite type: t{a.id} = {show_type(b)}")
        a.instance = b
        return
    if isinstance(b, TVar):
        if occurs(b, a):
            raise UnifyError(f"cannot construct infinite type: t{b.id} = {show_type(a)}")
        b.instance = a
        return
    if isinstance(a, TNumVar):
        if isinstance(b, TNumVar):
            a.instance = b
            return
        if isinstance(b, TCon) and b.name in ("Int", "Float"):
            a.instance = b
            return
        raise UnifyError(
            f"numeric operator expects Int or Float, got {show_type(b)}")
    if isinstance(b, TNumVar):
        if isinstance(a, TCon) and a.name in ("Int", "Float"):
            b.instance = a
            return
        raise UnifyError(
            f"numeric operator expects Int or Float, got {show_type(a)}")
    if isinstance(a, TCon) and isinstance(b, TCon):
        if a.name != b.name:
            raise UnifyError(f"expected {a.name}, got {b.name}")
        return
    if isinstance(a, TList) and isinstance(b, TList):
        unify(a.elem, b.elem)
        return
    if isinstance(a, TFun) and isinstance(b, TFun):
        unify(a.arg, b.arg)
        unify(a.ret, b.ret)
        return
    raise UnifyError(f"expected {show_type(a)}, got {show_type(b)}")


def free_type_vars(t: Type, out: Optional[Set[Type]] = None) -> Set[Type]:
    if out is None:
        out = set()
    t = prune(t)
    if isinstance(t, (TVar, TNumVar)):
        out.add(t)
    elif isinstance(t, TList):
        free_type_vars(t.elem, out)
    elif isinstance(t, TFun):
        free_type_vars(t.arg, out)
        free_type_vars(t.ret, out)
    return out


def default_numeric(t: Type) -> None:
    """Resolve unconstrained numeric variables to Int (after inference)."""

    t = prune(t)
    if isinstance(t, TNumVar):
        if t.instance is None:
            t.instance = TINT
    elif isinstance(t, TList):
        default_numeric(t.elem)
    elif isinstance(t, TFun):
        default_numeric(t.arg)
        default_numeric(t.ret)


def parse_type_annotation(text: str, loc=None) -> Type:
    text = text.strip()
    if text.startswith("[") and text.endswith("]"):
        return TList(parse_type_annotation(text[1:-1], loc))
    simple = {
        "Int": TINT, "Float": TFLOAT, "Bool": TBOOL, "String": TSTR,
        "ArrayF": TARRAYF, "ArrayI": TARRAYI, "ArrayB": TARRAYB,
    }
    if text in simple:
        return simple[text]
    raise TypeError_(f"unknown type annotation '{text}'", loc)


class TScheme:
    __slots__ = ("type", "quantified")

    def __init__(self, type_: Type, quantified: Optional[Set[Type]] = None) -> None:
        self.type = type_
        self.quantified: Set[Type] = quantified or set()

    def __repr__(self) -> str:  # pragma: no cover
        return f"scheme({self.type!r})"


def mono(t: Type) -> TScheme:
    return TScheme(t, set())


def free_scheme_vars(scheme: TScheme) -> Set[Type]:
    return free_type_vars(scheme.type) - scheme.quantified


def env_free_vars(env: Dict[int, TScheme]) -> Set[Type]:
    out: Set[Type] = set()
    for s in env.values():
        out |= free_scheme_vars(s)
    return out


def generalize(t: Type, env: Dict[int, TScheme]) -> TScheme:
    q = free_type_vars(t) - env_free_vars(env)
    return TScheme(t, q)


def instantiate(scheme: TScheme) -> Type:
    if not scheme.quantified:
        return scheme.type
    mapping: Dict[Tuple[str, int], Type] = {}
    for v in scheme.quantified:
        key = (type(v).__name__, v.id)
        mapping[key] = TNumVar() if isinstance(v, TNumVar) else TVar()

    def copy(t: Type) -> Type:
        t = prune(t)
        if isinstance(t, (TVar, TNumVar)):
            return mapping.get((type(t).__name__, t.id), t)
        if isinstance(t, TList):
            return TList(copy(t.elem))
        if isinstance(t, TFun):
            return TFun(copy(t.arg), copy(t.ret))
        return t

    return copy(scheme.type)


def show_type(t: Type, depth: int = 0) -> str:
    if depth > 8:
        return "..."
    t = prune(t)
    if isinstance(t, TVar):
        return f"'t{t.id}"
    if isinstance(t, TNumVar):
        return f"'n{t.id}"
    if isinstance(t, TCon):
        return t.name
    if isinstance(t, TList):
        return f"[{show_type(t.elem, depth + 1)}]"
    if isinstance(t, TFun):
        left = show_type(t.arg, depth + 1)
        if isinstance(prune(t.arg), TFun):
            left = f"({left})"
        return f"{left} -> {show_type(t.ret, depth + 1)}"
    return "?"


# ---------------------------------------------------------------------------
# Representation selection
# ---------------------------------------------------------------------------
def rep_of(t: Optional[Type]) -> str:
    """Return the runtime representation for a value of static type *t*."""

    if t is None:
        return BOX
    t = prune(t)
    if isinstance(t, TCon) and t.name in ("Int", "Bool"):
        return I64
    if isinstance(t, TCon) and t.name == "Float":
        return F64
    return BOX


def ret_and_params(t: Type) -> Tuple[List[Type], Type]:
    """Split a curried function type into (argument types, result type)."""

    args: List[Type] = []
    t = prune(t)
    while isinstance(t, TFun):
        args.append(t.arg)
        t = prune(t.ret)
    return args, t


def arity_of(t: Type) -> int:
    args, _ = ret_and_params(t)
    return len(args)


# ---------------------------------------------------------------------------
# Primitive types
# ---------------------------------------------------------------------------
def _forall(builder) -> TScheme:
    """Build a polymorphic primitive scheme.  Fresh vars per instantiation."""

    a = TVar()
    b = TVar()
    mapping = {"a": a, "b": b}
    t = builder(mapping)
    q = free_type_vars(t)
    return TScheme(t, q)


def _numeric(builder) -> TScheme:
    """Build a scheme whose shared type variable is constrained numeric."""

    n = TNumVar()
    t = builder(n)
    q = free_type_vars(t)
    return TScheme(t, q)


def primitive_scheme(name: str, mapping: Optional[Dict[str, Type]] = None) -> TScheme:
    if name in ("+", "⊕", "-", "⊖", "*", "⊗", "/", "⊘", "%", "⊙"):
        return _numeric(lambda n: tfun([n, n], n))
    if name in ("==", "≡"):
        # Polymorphic structural equality (ints, floats, bools, strings, lists).
        return _forall(lambda m: tfun([m["a"], m["a"]], TBOOL))
    if name in ("<", "≺", "<=", "≼", ">", "≻", ">=", "≽"):
        return _numeric(lambda n: tfun([n, n], TBOOL))
    if name == "not":
        return mono(tfun([TBOOL], TBOOL))
    if name == "cons":
        return _forall(lambda m: tfun([m["a"], TList(m["a"])], TList(m["a"])))
    if name == "head":
        return _forall(lambda m: tfun([TList(m["a"])], m["a"]))
    if name == "tail":
        return _forall(lambda m: tfun([TList(m["a"])], TList(m["a"])))
    if name == "empty?":
        return _forall(lambda m: tfun([TList(m["a"])], TBOOL))
    if name == "nil":
        return _forall(lambda m: TList(m["a"]))
    if name == "str->int":
        return mono(tfun([TSTR], TINT))
    if name == "int->str":
        return mono(tfun([TINT], TSTR))
    if name == "str->float":
        return mono(tfun([TSTR], TFLOAT))
    if name == "float->str":
        return mono(tfun([TFLOAT], TSTR))
    if name == "str->list":
        return mono(tfun([TSTR], TList(TINT)))
    if name == "str-len":
        return mono(tfun([TSTR], TINT))
    if name == "str-append":
        return mono(tfun([TSTR, TSTR], TSTR))
    if name == "read-file":
        return mono(tfun([TSTR], TSTR))
    if name in ("read-line",):
        return mono(tfun([TSTR], TSTR))
    if name == "read-int":
        return mono(tfun([TSTR], TINT))
    if name == "read-float":
        return mono(tfun([TSTR], TFLOAT))
    if name in ("print", "println"):
        return _forall(lambda m: tfun([m["a"]], m["a"]))
    if name in ("fix", "Y"):
        # ((a -> b) -> (a -> b)) -> (a -> b)
        return _forall(lambda m: tfun(
            [tfun([tfun([m["a"]], m["b"])], tfun([m["a"]], m["b"]))],
            tfun([m["a"]], m["b"])))
    # Numeric conversions (compiled to a single LLVM cast).
    if name == "int->float":
        return mono(tfun([TINT], TFLOAT))
    if name == "float->int":
        return mono(tfun([TFLOAT], TINT))
    # Float mathematics.
    if name in ("√", "sqrt", "sin", "cos", "tan", "asin", "acos", "atan",
                "exp", "ln", "log10", "log2", "floor", "ceil", "round", "trunc"):
        return mono(tfun([TFLOAT], TFLOAT))
    if name in ("pow", "**", "^"):
        return mono(tfun([TFLOAT, TFLOAT], TFLOAT))
    if name == "atan2":
        return mono(tfun([TFLOAT, TFLOAT], TFLOAT))
    if name in ("π", "pi", "e"):
        return mono(TFLOAT)
    # Numeric reductions.
    if name in ("∑", "sum", "∏", "product"):
        return _numeric(lambda n: tfun([TList(n)], n))
    if name == "abs":
        return _numeric(lambda n: tfun([n], n))
    # Bit-level integer operations (used by the segmented sieve).
    if name in ("&", "|", "bit-and", "bit-or", "bit-xor", "shl", "shr"):
        return mono(tfun([TINT, TINT], TINT))
    if name in ("!", "bit-not", "popcount"):
        return mono(tfun([TINT], TINT))
    # Lists / parallelism.
    if name in ("map", "pmap"):
        return _forall(lambda m: tfun(
            [tfun([m["a"]], m["b"]), TList(m["a"])], TList(m["b"])))
    if name == "range":
        return mono(tfun([TINT, TINT], TList(TINT)))
    if name in ("immortalize", "freeze"):
        return _forall(lambda m: tfun([m["a"]], m["a"]))
    # Flat arrays: one RC buffer, no per-element wrappers.  `set` consumes the
    # array and returns the same buffer, so callers treat it linearly.
    if name == "arrayf":
        return mono(tfun([TINT, TFLOAT], TARRAYF))
    if name == "arrayf-get":
        return mono(tfun([TARRAYF, TINT], TFLOAT))
    if name == "arrayf-set":
        return mono(tfun([TARRAYF, TINT, TFLOAT], TARRAYF))
    if name == "arrayf-length":
        return mono(tfun([TARRAYF], TINT))
    if name == "arrayf-clone":
        return mono(tfun([TARRAYF], TARRAYF))
    if name == "arrayi":
        return mono(tfun([TINT, TINT], TARRAYI))
    if name == "arrayi-get":
        return mono(tfun([TARRAYI, TINT], TINT))
    if name == "arrayi-set":
        return mono(tfun([TARRAYI, TINT, TINT], TARRAYI))
    if name == "arrayi-length":
        return mono(tfun([TARRAYI], TINT))
    if name == "arrayi-clone":
        return mono(tfun([TARRAYI], TARRAYI))
    if name == "arrayb":
        return mono(tfun([TINT, TINT], TARRAYB))
    if name == "arrayb-get":
        return mono(tfun([TARRAYB, TINT], TINT))
    if name == "arrayb-set":
        return mono(tfun([TARRAYB, TINT, TINT], TARRAYB))
    if name == "arrayb-length":
        return mono(tfun([TARRAYB], TINT))
    if name == "arrayb-clone":
        return mono(tfun([TARRAYB], TARRAYB))
    raise TypeError_(f"unknown primitive '{name}'")


# ---------------------------------------------------------------------------
# Inference
# ---------------------------------------------------------------------------
class Inferencer:
    def __init__(self) -> None:
        self.env: Dict[int, TScheme] = {}
        self.errors: List[str] = []

    def infer_program(self, program: Node) -> Type:
        result = self.infer(program, {})
        # Any numeric constraint that never met an Int/Float use defaults to
        # Int, keeping function ABIs concrete for code generation.
        for n in walk(program):
            if n.type is not None:
                default_numeric(n.type)
            if isinstance(n, Block):
                for d in n.defs:
                    if d.binding is not None:
                        if d.binding.type is not None:
                            default_numeric(d.binding.type)
                        if d.binding.scheme is not None:
                            default_numeric(d.binding.scheme.type)
        default_numeric(result)
        return result

    def infer(self, node: Node, env: Dict[int, TScheme]) -> Type:
        t = self._infer(node, env)
        node.type = t
        return t

    def _infer(self, node: Node, env: Dict[int, TScheme]) -> Type:
        if isinstance(node, Lit):
            if isinstance(node.value, bool):
                return TBOOL
            if isinstance(node.value, int):
                return TINT
            if isinstance(node.value, float):
                return TFLOAT
            return TSTR

        if isinstance(node, Var):
            b: Binding = node.binding
            if b.kind == PRIM:
                return instantiate(primitive_scheme(b.name))
            scheme = env.get(b.uid)
            if scheme is None:
                # A top-level binding whose type has not been inferred yet;
                # this is an internal error rather than a user error.
                raise TypeError_(f"internal: no type for '{b.name}'", node.loc)
            return instantiate(scheme)

        if isinstance(node, Lam):
            env2 = dict(env)
            param_types: List[Type] = []
            for i, b in enumerate(node.param_bindings):
                ann = (node.param_annotations[i]
                       if i < len(node.param_annotations) else None)
                if ann:
                    v = parse_type_annotation(ann, node.loc)
                else:
                    v = TVar()
                b.type = v
                param_types.append(v)
                env2[b.uid] = mono(v)
            body_t = self.infer(node.body, env2)
            return tfun(param_types, body_t)

        if isinstance(node, App):
            func_t = self.infer(node.func, env)
            for arg in node.args:
                arg_t = self.infer(arg, env)
                res_t = TVar()
                try:
                    unify(func_t, TFun(arg_t, res_t))
                except UnifyError as exc:
                    raise TypeError_(exc.message, arg.loc) from None
                func_t = res_t
            return func_t

        if isinstance(node, If):
            cond_t = self.infer(node.cond, env)
            try:
                unify(cond_t, TBOOL)
            except UnifyError as exc:
                raise TypeError_(
                    f"if condition must be Bool, got {show_type(cond_t)}", node.cond.loc
                ) from None
            then_t = self.infer(node.then, env)
            else_t = self.infer(node.else_, env)
            try:
                unify(then_t, else_t)
            except UnifyError as exc:
                raise TypeError_(
                    f"branches of if have different types: "
                    f"{show_type(then_t)} vs {show_type(else_t)}", node.loc
                ) from None
            return then_t

        if isinstance(node, Block):
            return self._infer_block(node, env)

        raise TypeError_(f"internal: cannot infer {type(node).__name__}", node.loc)

    def _infer_block(self, block: Block, outer_env: Dict[int, TScheme]) -> Type:
        env = dict(outer_env)
        def_vars: List[Type] = []
        for d in block.defs:
            v = TVar()
            def_vars.append(v)
            # Recursive definitions are monomorphic while their values are
            # being inferred (standard letrec typing).
            env[d.binding.uid] = mono(v)
            d.binding.type = v

        for d, v in zip(block.defs, def_vars):
            val_t = self.infer(d.value, env)
            d.type = val_t
            try:
                unify(v, val_t)
            except UnifyError as exc:
                raise TypeError_(exc.message, d.value.loc) from None

        # Generalize the whole group before inferring the block body, so the
        # body may use a definition polymorphically.  Numeric constraints
        # without a concrete Int/Float use default to Int, which keeps every
        # function ABI concrete.
        env = dict(outer_env)
        for d, v in zip(block.defs, def_vars):
            t = prune(v)
            default_numeric(t)
            scheme = generalize(t, outer_env)
            d.binding.scheme = scheme
            d.binding.type = t
            env[d.binding.uid] = scheme

        return self.infer(block.body, env)


def infer_program(program: Node) -> Type:
    return Inferencer().infer_program(program)
