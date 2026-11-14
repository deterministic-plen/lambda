"""LLVM IR backend.

Design summary
--------------
* **Type-directed representation.**  Values whose static type is ``Int`` or
  ``Bool`` are unboxed ``i64`` values.  Everything else is a reference-counted
  ``%Value*``.  Arithmetic therefore never allocates in typed code.
* **Direct calls.**  A reference to a known function with the right number of
  arguments compiles to a direct call of its native entry point (all arguments
  unboxed).  Curried/partial applications go through a boxed closure wrapper.
* **Flat closures.**  A lambda's free variables are passed explicitly: at a
  direct call as extra leading parameters, and at closure creation as fields of
  a small environment struct.  There is no linked environment chain, so a loop
  body that only uses integers allocates nothing.
* **Ownership.**  Boxed function parameters, captures and local definitions are
  owned by the callee and released at every exit.  Every boxed expression
  yields a fresh owned reference.  Tail calls release the caller's locals and
  transfer argument ownership.
* **Tail calls.**  A direct call in tail position with a matching return
  representation is emitted as ``musttail``, so numeric loops run in constant
  stack space.
* **Dynamic fallback.**  If inference fails, every value is boxed and every
  call goes through the same boxed ABI.  This is what makes Z/Y-style
  fixed-point combinators work.
"""

from __future__ import annotations

import math
from typing import Dict, List, Optional, Set, Tuple

from lambda_ast import (
    App, Binding, Block, Borrow, Def, EnumLit, If, Lam, Lit, Match, Node, PARAM, PRIM,
    DataCtor, RecordField, RecordLit, SumLit, TOP_DEF, Var, walk,
)
from analysis import IMPURE_PRIMITIVES, PRIM_ARITY, top_level_order
from errors import CompileError
from parser import OP_CANON
from runtime import RUNTIME_IR_DECLS
from typecheck import BOX, F64, I64, TAdt, TCon, TList, TRecord, TSum, prune, rep_of

PRIM_GLOBALS = {
    "add": "@lambda_prim_add",
    "sub": "@lambda_prim_sub",
    "mul": "@lambda_prim_mul",
    "div": "@lambda_prim_div",
    "mod": "@lambda_prim_mod",
    "eq": "@lambda_prim_eq",
    "lt": "@lambda_prim_lt",
    "le": "@lambda_prim_le",
    "gt": "@lambda_prim_gt",
    "ge": "@lambda_prim_ge",
    "cons": "@lambda_prim_cons",
    "head": "@lambda_prim_head",
    "tail": "@lambda_prim_tail",
    "empty": "@lambda_prim_empty",
    "not": "@lambda_prim_not", "and": "@lambda_prim_and", "or": "@lambda_prim_or",
    "str_to_int": "@lambda_prim_str_to_int",
    "int_to_str": "@lambda_prim_int_to_str",
    "str_to_list": "@lambda_prim_str_to_list",
    "str_len": "@lambda_prim_str_len",
    "str_append": "@lambda_prim_str_append",
    "read_file": "@lambda_prim_read_file",
    "print": "@lambda_prim_print",
    "println": "@lambda_prim_println",
    "fix": "@lambda_prim_fix",
    "int_to_float": "@lambda_prim_int_to_float",
    "float_to_int": "@lambda_prim_float_to_int",
    "str_to_float": "@lambda_prim_str_to_float",
    "float_to_str": "@lambda_prim_float_to_str",
    "sqrt": "@lambda_prim_sqrt", "sin": "@lambda_prim_sin",
    "cos": "@lambda_prim_cos", "tan": "@lambda_prim_tan",
    "asin": "@lambda_prim_asin", "acos": "@lambda_prim_acos",
    "atan": "@lambda_prim_atan", "atan2": "@lambda_prim_atan2",
    "exp": "@lambda_prim_exp", "ln": "@lambda_prim_ln",
    "log10": "@lambda_prim_log10", "log2": "@lambda_prim_log2",
    "floor": "@lambda_prim_floor", "ceil": "@lambda_prim_ceil",
    "round": "@lambda_prim_round", "trunc": "@lambda_prim_trunc",
    "pow": "@lambda_prim_pow", "abs": "@lambda_prim_abs",
    "sum": "@lambda_prim_sum", "product": "@lambda_prim_product",
    "bit_and": "@lambda_prim_bit_and", "bit_or": "@lambda_prim_bit_or",
    "bit_xor": "@lambda_prim_bit_xor", "bit_not": "@lambda_prim_bit_not",
    "shl": "@lambda_prim_shl", "shr": "@lambda_prim_shr",
    "popcount": "@lambda_prim_popcount",
    "range": "@lambda_prim_range", "map": "@lambda_prim_map",
    "pmap": "@lambda_prim_pmap",
    "immortalize": "@lambda_prim_immortalize",
    "read_line": "@lambda_prim_read_line",
    "read_int": "@lambda_prim_read_int",
    "read_float": "@lambda_prim_read_float",
}

OP_FN = {
    "add": "@lambda_op_add", "sub": "@lambda_op_sub", "mul": "@lambda_op_mul",
    "div": "@lambda_op_div", "mod": "@lambda_op_mod", "eq": "@lambda_op_eq",
    "lt": "@lambda_op_lt", "le": "@lambda_op_le", "gt": "@lambda_op_gt",
    "ge": "@lambda_op_ge", "cons": "@lambda_op_cons", "head": "@lambda_op_head",
    "tail": "@lambda_op_tail", "empty": "@lambda_op_empty", "not": "@lambda_op_not", "and": "@lambda_op_and", "or": "@lambda_op_or",
    "str_to_int": "@lambda_op_str_to_int", "int_to_str": "@lambda_op_int_to_str",
    "str_to_list": "@lambda_op_str_to_list", "str_len": "@lambda_op_str_len",
    "str_append": "@lambda_op_str_append", "read_file": "@lambda_op_read_file",
    "write_file": "@lambda_op_write_file",
    "print": "@lambda_op_print", "println": "@lambda_op_println",
    "fix": "@lambda_fix",
    "int_to_float": "@lambda_op_int_to_float",
    "float_to_int": "@lambda_op_float_to_int",
    "str_to_float": "@lambda_op_str_to_float",
    "float_to_str": "@lambda_op_float_to_str",
    "sqrt": "@lambda_op_sqrt", "sin": "@lambda_op_sin",
    "cos": "@lambda_op_cos", "tan": "@lambda_op_tan",
    "asin": "@lambda_op_asin", "acos": "@lambda_op_acos",
    "atan": "@lambda_op_atan", "atan2": "@lambda_op_atan2",
    "exp": "@lambda_op_exp", "ln": "@lambda_op_ln",
    "log10": "@lambda_op_log10", "log2": "@lambda_op_log2",
    "floor": "@lambda_op_floor", "ceil": "@lambda_op_ceil",
    "round": "@lambda_op_round", "trunc": "@lambda_op_trunc",
    "pow": "@lambda_op_pow", "abs": "@lambda_op_abs",
    "sum": "@lambda_op_sum", "product": "@lambda_op_product",
    "bit_and": "@lambda_op_bit_and", "bit_or": "@lambda_op_bit_or",
    "bit_xor": "@lambda_op_bit_xor", "bit_not": "@lambda_op_bit_not",
    "shl": "@lambda_op_shl", "shr": "@lambda_op_shr",
    "popcount": "@lambda_op_popcount",
    "range": "@lambda_op_range", "map": "@lambda_op_map",
    "pmap": "@lambda_op_pmap",
    "immortalize": "@lambda_op_immortalize",
    "arrayf": "@lambda_op_arrayf_new", "arrayi": "@lambda_op_arrayi_new",
    "arrayb": "@lambda_op_arrayb_new",
    "arrayf_of": "@lambda_op_arrayf_of", "arrayi_of": "@lambda_op_arrayi_of",
    "arrayb_of": "@lambda_op_arrayb_of",
    "arrayf_get": "@lambda_op_arrayf_get", "arrayf_set": "@lambda_op_arrayf_set",
    "arrayf_length": "@lambda_op_arrayf_length", "arrayf_clone": "@lambda_op_arrayf_clone",
    "arrayi_get": "@lambda_op_arrayi_get", "arrayi_set": "@lambda_op_arrayi_set",
    "arrayi_length": "@lambda_op_arrayi_length", "arrayi_clone": "@lambda_op_arrayi_clone",
    "arrayb_get": "@lambda_op_arrayb_get", "arrayb_set": "@lambda_op_arrayb_set",
    "arrayb_length": "@lambda_op_arrayb_length", "arrayb_clone": "@lambda_op_arrayb_clone",
    "arrayf_get_ro": "@lambda_op_arrayf_get", "arrayi_get_ro": "@lambda_op_arrayi_get",
    "arrayb_get_ro": "@lambda_op_arrayb_get",
    "arrayi_pmap": "@lambda_op_arrayi_pmap", "arrayf_pmap": "@lambda_op_arrayf_pmap",
    "arrayi_pmap_chunk": "@lambda_op_arrayi_pmap_chunk",
    "read_line": "@lambda_op_read_line",
    "read_int": "@lambda_op_read_int",
    "read_float": "@lambda_op_read_float",
}

CMP_OP = {"eq": "eq", "lt": "slt", "le": "sle", "gt": "sgt", "ge": "sge"}


class IRBuilder:
    __slots__ = ("lines", "reg", "label_count", "allocas")

    def __init__(self) -> None:
        self.lines: List[str] = []
        self.reg = 0
        self.label_count = 0
        self.allocas: List[str] = []

    def new_reg(self, prefix: str = "r") -> str:
        name = f"%{prefix}{self.reg}"
        self.reg += 1
        return name

    def new_label(self, prefix: str = "bb") -> str:
        name = f"{prefix}{self.label_count}"
        self.label_count += 1
        return name

    def emit(self, text: str) -> None:
        self.lines.append("  " + text)

    def raw(self, text: str) -> None:
        self.lines.append(text)

    def label(self, name: str) -> None:
        self.lines.append(f"{name}:")

    def new_alloca(self, ty: str) -> str:
        name = self.new_reg("a")
        self.allocas.append(f"  {name} = alloca {ty}")
        return name

    def render(self) -> str:
        lines = list(self.lines)
        if self.allocas:
            idx = 0
            for i, line in enumerate(lines):
                if line.strip() == "entry:":
                    idx = i + 1
                    break
            lines[idx:idx] = self.allocas
        return "\n".join(lines)


def emit_retain_ir(b: IRBuilder, value: str, atomic: bool = False) -> None:
    """Inline the common reference-count increment.

    Compiler-generated boxed values are never null (``nil`` is a real immortal
    value), so the fast path needs only a load/select/store.  Parallel programs
    use the runtime's atomic functions instead."""
    if atomic:
        b.emit(f"call void @lambda_retain(%Value* {value})")
        return
    rcp = b.new_reg()
    b.emit(f"{rcp} = getelementptr %Value, %Value* {value}, i32 0, i32 1")
    rc = b.new_reg()
    b.emit(f"{rc} = load i32, i32* {rcp}")
    pos = b.new_reg()
    b.emit(f"{pos} = icmp sgt i32 {rc}, 0")
    inc = b.new_reg()
    b.emit(f"{inc} = add i32 {rc}, 1")
    new = b.new_reg()
    b.emit(f"{new} = select i1 {pos}, i32 {inc}, i32 {rc}")
    b.emit(f"store i32 {new}, i32* {rcp}")


def emit_release_ir(b: IRBuilder, value: str, atomic: bool = False) -> None:
    """Inline the reference-count decrement; call the runtime only when the
    last reference goes away.  Parallel programs use the atomic runtime."""
    if atomic:
        b.emit(f"call void @lambda_release(%Value* {value})")
        return
    free = b.new_label("rc_free")
    done = b.new_label("rc_done")
    rcp = b.new_reg()
    b.emit(f"{rcp} = getelementptr %Value, %Value* {value}, i32 0, i32 1")
    rc = b.new_reg()
    b.emit(f"{rc} = load i32, i32* {rcp}")
    pos = b.new_reg()
    b.emit(f"{pos} = icmp sgt i32 {rc}, 0")
    dec = b.new_reg()
    b.emit(f"{dec} = sub i32 {rc}, 1")
    new = b.new_reg()
    b.emit(f"{new} = select i1 {pos}, i32 {dec}, i32 {rc}")
    b.emit(f"store i32 {new}, i32* {rcp}")
    last = b.new_reg()
    b.emit(f"{last} = icmp eq i32 {new}, 0")
    drop = b.new_reg()
    b.emit(f"{drop} = and i1 {pos}, {last}")
    b.emit(f"br i1 {drop}, label %{free}, label %{done}")
    b.label(free)
    b.emit(f"call void @lambda_release_zero(%Value* {value})")
    b.emit(f"br label %{done}")
    b.label(done)


class Val:
    __slots__ = ("rep", "name", "owned", "immortal", "borrowed")
    def __init__(self, rep: str, name: str, owned: bool = False,
                 immortal: bool = False, borrowed: bool = False) -> None:
        self.rep = rep
        self.name = name
        self.owned = owned
        self.immortal = immortal
        self.borrowed = borrowed

    def __repr__(self) -> str:  # pragma: no cover
        return f"Val({self.rep},{self.name},owned={self.owned})"


class Storage:
    __slots__ = ("rep", "name", "is_global")

    def __init__(self, rep: str, name: str, is_global: bool = False) -> None:
        self.rep = rep
        self.name = name
        self.is_global = is_global


class FunctionInfo:
    __slots__ = (
        "lam", "id", "captures", "capture_reps", "capture_types",
        "params", "param_reps", "param_types", "ret_rep", "ret_type",
        "arity", "direct_name", "wrapper_names", "env_type_names",
        "env_dtors", "generated", "generating", "param_borrows",
    )

    def __init__(self) -> None:
        self.lam: Optional[Lam] = None
        self.id = 0
        self.captures: List[Binding] = []
        self.capture_reps: List[str] = []
        self.capture_types: List[object] = []
        self.params: List[Binding] = []
        self.param_reps: List[str] = []
        self.param_types: List[object] = []
        self.ret_rep = BOX
        self.ret_type = None
        self.arity = 0
        self.param_borrows = []
        self.direct_name = ""
        self.wrapper_names: List[str] = []
        self.env_type_names: List[str] = []
        self.env_dtors: List[str] = []
        self.generated = False
        self.generating = False


class FnContext:
    __slots__ = ("ret_rep", "ret_type", "nparams", "param_reps")

    def __init__(self, ret_rep: str, ret_type, nparams: int = 0,
                 param_reps: Optional[List[str]] = None) -> None:
        self.ret_rep = ret_rep
        self.ret_type = ret_type
        self.nparams = nparams
        self.param_reps = list(param_reps or [])


def is_bool_type(t) -> bool:
    if t is None:
        return False
    t = prune(t)
    return isinstance(t, TCon) and t.name == "Bool"


def is_array_type(t) -> bool:
    if t is None:
        return False
    t = prune(t)
    return isinstance(t, TCon) and t.name in ("ArrayI", "ArrayF", "ArrayB")


def float_const(x: float) -> str:
    """A decimal LLVM double literal that round-trips to *x*."""

    text = repr(float(x))
    if text in ("inf", "-inf", "nan"):
        raise CompileError("float literal must be finite")
    lower = text.lower()
    if "e" in lower:
        if "." not in text:
            idx = lower.index("e")
            text = text[:idx] + ".0" + text[idx:]
    elif "." not in text:
        text += ".0"
    return text


class Compiler:
    def __init__(self, dynamic: bool = False, use_musttail: bool = True,
                 debug: bool = False,
                 lambda_purity: Optional[Dict[int, bool]] = None,
                 allow_input: bool = False, allow_data: bool = False,
                 exit_code: bool = False) -> None:
        self.dynamic = dynamic
        self.use_musttail = use_musttail
        self.lambda_purity = lambda_purity or {}
        self.allow_input = allow_input
        self.allow_data = allow_data
        self.exit_code = exit_code
        self.variant_tags = {}
        self.record_schemas = {}
        self.adt_schemas = {}
        self.adt_arenas = {}
        self.adt_widths = {}
        self.adt_words = {}
        self.value_functions: Set[int] = set()

        self.type_defs: List[str] = ["%AdtArena = type { i64, i64, i64, i64*, i64, i64, i64* }"]
        self.gpu_defs: List[str] = []
        self.gpu_ids: Dict[str, str] = {}
        self.globals: List[str] = []
        self.functions: List[str] = []
        self.main_text = ""
        self.string_globals: Dict[str, str] = {}
        self.string_defs: List[str] = []
        self.nil_global_declared = False
        self.top_globals: List[str] = []
        self.prim_decls: Set[str] = set()

        self.top_storage: Dict[int, Storage] = {}
        self.top_function_globals: Dict[int, str] = {}
        self.top_value_globals: Dict[int, str] = {}
        self.top_constants: Set[int] = set()
        self.top_defs: List[Def] = []

        self.lam_infos: Dict[int, FunctionInfo] = {}
        self.pending_infos: List[FunctionInfo] = []
        self.next_lam_id = 0

        self.builder: Optional[IRBuilder] = None
        self.ctx: Optional[FnContext] = None
        self.scope_stack: List[Dict[int, Storage]] = []
        self.cleanup_stack: List[List[Tuple[str, bool]]] = []
        self.terminated = False
        self.moved_uids: Set[int] = set()
        self.captured_uids: Set[int] = set()
        self.array_meta: Dict[str, Tuple[str, str]] = {}
        self.atomic_rc = False
        self.moves_allowed: bool = True

        self.string_counter = 0

    # ------------------------------------------------------------------
    # top-level setup
    # ------------------------------------------------------------------
    def _mark_function_values(self, program: Node) -> None:
        """Find function bindings that actually escape as first-class values.

        A function referenced only as the head of a fully applied call can stay
        closure-free; top-level closures are allocated only for this set.
        """

        def visit(n: Node, direct_head: bool = False) -> None:
            if isinstance(n, Var):
                b = n.binding
                if b is not None and b.is_function and not direct_head:
                    self.value_functions.add(b.uid)
                return
            if isinstance(n, App):
                head, args = self.flatten_app(n)
                if isinstance(head, Var) and head.binding is not None \
                        and head.binding.is_function:
                    arity = len(head.binding.lam.param_bindings) \
                        if head.binding.lam is not None else 0
                    if len(args) < arity:
                        self.value_functions.add(head.binding.uid)
                    for a in args:
                        visit(a)
                    return
                visit(head)
                for a in args:
                    visit(a)
                return
            if isinstance(n, Lam):
                visit(n.body)
                return
            if isinstance(n, Block):
                for d in n.defs:
                    if isinstance(d.value, Lam):
                        visit(d.value.body)
                    else:
                        visit(d.value)
                visit(n.body)
                return
            if isinstance(n, If):
                visit(n.cond)
                visit(n.then)
                visit(n.else_)
                return
            if isinstance(n, Match):
                visit(n.subject)
                for _, arm in n.arms:
                    visit(arm)
                return
            if isinstance(n, (RecordLit, SumLit)):
                for a in n.args:
                    visit(a)
                return
            if isinstance(n, RecordField):
                visit(n.value)

        visit(program)

    def _uses_parallel_primitives(self, program: Node) -> bool:
        for n in walk(program):
            if (isinstance(n, App) and isinstance(n.func, Var)
                    and n.func.binding is not None):
                b = n.func.binding
                if (b.kind == PRIM
                        and OP_CANON.get(b.name)
                        in ("pmap", "arrayi_pmap", "arrayi_pmap_chunk", "arrayf_pmap")):
                    return True
        return False

    def compile_program(self, program: Node) -> str:
        if not isinstance(program, Block):
            program = Block([], program, program.loc)
        self.atomic_rc = self._uses_parallel_primitives(program)
        self.variant_tags = {}
        for block in walk(program):
            if not isinstance(block, Block):
                continue
            for d in block.defs:
                if isinstance(d.value, EnumLit):
                    self.variant_tags[d.name] = (
                        d.value.enum_name, d.value.tag)
        self.value_functions = set()
        self._mark_function_values(program)
        self.record_schemas = {}
        for node in walk(program):
            if isinstance(node, RecordLit) and isinstance(node.type, TRecord):
                self.record_schemas[node.record_name] = node.type
            if isinstance(node, SumLit) and isinstance(node.type, TRecord):
                self.record_schemas[node.sum_name] = node.type
        for node in walk(program):
            if isinstance(node, DataCtor) and isinstance(node.type, TAdt):
                self.adt_schemas[node.data_name] = node.type
        for name, adt in sorted(self.adt_schemas.items()):
            if adt.layout != "arena":
                continue
            width = max((len(fields) for _, _, fields in adt.variants), default=0)
            g = f"@arena_{name}"
            self.adt_arenas[name] = g
            self.adt_widths[name] = width
            self.top_globals.append(f"{g} = internal global %AdtArena* null")
            wg = f"@arena_words_{name}"
            self.adt_words[name] = wg
            self.top_globals.append(f"{wg} = internal global i64* null")
        for name, rec in sorted(self.record_schemas.items()):
            field_types = []
            for _, ftype in rec.fields:
                rep = rep_of(ftype)
                if rep not in (I64, F64):
                    raise CompileError(
                        f"product field '{name}' is not unboxed",
                        program.loc)
                field_types.append(self.rep_type(rep))
            self.type_defs.append(
                f"{self.record_llvm_name(name)} = type {{ "
                f"{', '.join(field_types)} }}")
        self.top_defs = list(program.defs)
        self.captured_uids = {
            b.uid
            for n in walk(program)
            if isinstance(n, Lam)
            for b in getattr(n, "captures", [])
        }
        self._setup_top_level(program)
        self._gen_main(program)
        self._drain_functions()

        parts: List[str] = [RUNTIME_IR_DECLS]
        parts.append("\n; ---- LLVM types ----")
        parts.extend(self.type_defs)
        parts.append("\n; ---- primitive globals ----")
        for op in sorted(PRIM_GLOBALS):
            parts.append(f"@lambda_prim_{op} = external global %Value*")
        parts.append("\n; ---- string / nil constants ----")
        parts.extend(self.string_defs)
        if self.gpu_defs:
            parts.append("\n; ---- GPU kernel sources ----")
            parts.extend(self.gpu_defs)
        parts.append("\n; ---- top-level globals ----")
        parts.extend(self.top_globals)
        parts.append("\n; ---- functions ----")
        parts.extend(self.functions)
        parts.append("\n; ---- program entry ----")
        parts.append(self.main_text)
        parts.append("\n!0 = !{}")
        return "\n".join(parts) + "\n"

    def _setup_top_level(self, program: Block) -> None:
        for d in self.top_defs:
            b = d.binding
            if b is None:
                continue
            if d.is_function:
                name = f"@gclo_{b.uid}"
                self.top_globals.append(f"{name} = internal global %Value* null")
                self.top_storage[b.uid] = Storage(BOX, name, is_global=True)
                self.top_function_globals[b.uid] = name
            else:
                rep = self.binding_rep(b)
                name = f"@gval_{b.uid}"
                if isinstance(d.value, Lit) and rep in (I64, F64):
                    # Top-level bindings are immutable.  Emitting literal scalars
                    # as LLVM constants lets the optimizer fold loads and, in
                    # particular, strength-reduce division/modulo by a named
                    # constant such as the bigint base.
                    v = d.value.value
                    if rep == I64:
                        text = "1" if v is True else "0" if v is False else str(v)
                        self.top_globals.append(
                            f"{name} = internal constant i64 {text}")
                    else:
                        self.top_globals.append(
                            f"{name} = internal constant double {float_const(float(v))}")
                    self.top_constants.add(b.uid)
                    self.top_storage[b.uid] = Storage(rep, name, is_global=True)
                    self.top_value_globals[b.uid] = name
                    continue
                if rep == I64:
                    self.top_globals.append(f"{name} = internal global i64 0")
                elif rep == F64:
                    self.top_globals.append(f"{name} = internal global double 0.0")
                elif rep.startswith("REC:"):
                    self.top_globals.append(
                        f"{name} = internal global {self.rep_type(rep)} zeroinitializer")
                else:
                    self.top_globals.append(f"{name} = internal global %Value* null")
                self.top_storage[b.uid] = Storage(rep, name, is_global=True)
                self.top_value_globals[b.uid] = name

    # ------------------------------------------------------------------
    # function metadata / wrappers
    # ------------------------------------------------------------------
    def ensure_function(self, lam: Lam) -> FunctionInfo:
        cached = self.lam_infos.get(id(lam))
        if cached is not None:
            return cached

        info = FunctionInfo()
        info.lam = lam
        self.next_lam_id += 1
        info.id = self.next_lam_id
        info.captures = list(lam.captures)
        info.params = list(lam.param_bindings)
        info.arity = len(info.params)

        if self.dynamic:
            info.capture_reps = [BOX] * len(info.captures)
            info.capture_types = [None] * len(info.captures)
            info.param_reps = [BOX] * info.arity
            info.param_types = [None] * info.arity
            info.ret_rep = BOX
            info.ret_type = None
        else:
            info.capture_reps = [self.binding_rep(b) for b in info.captures]
            info.capture_types = [b.type for b in info.captures]
            info.param_reps = [self.binding_rep(b) for b in info.params]
            info.param_types = [b.type for b in info.params]
            # The immediate result is the lambda body's type.  Do not flatten
            # through it: a lambda may itself return a function (currying), and
            # that function must be returned as a boxed closure.
            info.ret_type = lam.body.type
            info.ret_rep = rep_of(lam.body.type)

        info.param_borrows = [b.borrowed for b in info.params]
        info.direct_name = f"@fn_{info.id}"
        info.wrapper_names = [f"@wrap_{info.id}_{k}" for k in range(1, info.arity + 1)]
        info.env_type_names = [f"%env_{info.id}_{k}" for k in range(1, info.arity + 1)]
        info.env_dtors = [f"@env_dtor_{info.id}_{k}" for k in range(1, info.arity + 1)]

        self.lam_infos[id(lam)] = info
        self.pending_infos.append(info)
        self._gen_wrappers(info)
        return info

    def _env_layout(self, info: FunctionInfo, stage: int):
        """Return (type name, field types, capture indexes, argument indexes)."""

        fields: List[str] = ["void (i8*)*"]
        cap_idx: List[int] = []
        for rep in info.capture_reps:
            cap_idx.append(len(fields))
            fields.append(self.rep_type(rep))
        arg_idx: List[int] = []
        for _ in range(stage - 1):
            arg_idx.append(len(fields))
            fields.append("%Value*")
        return info.env_type_names[stage - 1], fields, cap_idx, arg_idx

    def _ensure_env_type(self, info: FunctionInfo, stage: int) -> None:
        ty, fields, _, _ = self._env_layout(info, stage)
        key = f"{ty} = type"
        if any(line.startswith(key) for line in self.type_defs):
            return
        self.type_defs.append(f"{ty} = type {{ {', '.join(fields)} }}")

    def _gen_wrappers(self, info: FunctionInfo) -> None:
        # Product values are unboxed aggregates.  Wrappers would need a boxed
        # representation, so record functions are direct-call-only for now.
        reps = info.capture_reps + info.param_reps + [info.ret_rep]
        if any(rep.startswith("REC:") for rep in reps) or any(info.param_borrows):
            return
        # All stage types must exist before any wrapper is generated, because a
        # wrapper for stage k allocates an environment for stage k+1.
        for stage in range(1, info.arity + 1):
            self._ensure_env_type(info, stage)
        for stage in range(1, info.arity + 1):
            self._gen_env_dtor(info, stage)
            self._gen_wrapper(info, stage)

    def _gen_env_dtor(self, info: FunctionInfo, stage: int) -> None:
        ty, fields, cap_idx, arg_idx = self._env_layout(info, stage)
        b = IRBuilder()
        name = info.env_dtors[stage - 1]
        b.raw(f"define internal void {name}(i8* %raw) {{")
        b.label("entry")
        env = b.new_reg("env")
        b.emit(f"{env} = bitcast i8* %raw to {ty}*")
        for idx in cap_idx:
            if fields[idx] != "%Value*":
                continue
            p = b.new_reg()
            b.emit(f"{p} = getelementptr {ty}, {ty}* {env}, i32 0, i32 {idx}")
            v = b.new_reg()
            b.emit(f"{v} = load %Value*, %Value** {p}")
            emit_release_ir(b, v, self.atomic_rc)
        for idx in arg_idx:
            p = b.new_reg()
            b.emit(f"{p} = getelementptr {ty}, {ty}* {env}, i32 0, i32 {idx}")
            v = b.new_reg()
            b.emit(f"{v} = load %Value*, %Value** {p}")
            emit_release_ir(b, v, self.atomic_rc)
        b.emit("call void @lambda_free_env(i8* %raw)")
        b.emit("ret void")
        b.raw("}")
        self.functions.append(b.render())

    def _load_env_field(self, b: IRBuilder, ty: str, env: str, idx: int,
                        field_ty: str) -> str:
        p = b.new_reg()
        b.emit(f"{p} = getelementptr {ty}, {ty}* {env}, i32 0, i32 {idx}")
        v = b.new_reg()
        b.emit(f"{v} = load {field_ty}, {field_ty}* {p}")
        return v

    def _store_env_field(self, b: IRBuilder, ty: str, env: str, idx: int,
                         field_ty: str, value: str) -> None:
        p = b.new_reg()
        b.emit(f"{p} = getelementptr {ty}, {ty}* {env}, i32 0, i32 {idx}")
        b.emit(f"store {field_ty} {value}, {field_ty}* {p}")

    def _env_size(self, fields: List[str]) -> int:
        # Every field (dtor pointer, capture, applied argument) is 8 bytes on
        # the 64-bit targets this compiler supports.
        return 8 * len(fields)

    @staticmethod
    def record_llvm_name(name: str) -> str:
        return "%rec_" + "".join(
            c if c.isalnum() or c == "_" else "_" for c in name)

    def rep_type(self, rep: str) -> str:
        if rep == I64:
            return "i64"
        if rep == F64:
            return "double"
        if rep.startswith("REC:"):
            return self.record_llvm_name(rep[4:])
        return "%Value*"

    def _alloc_env(self, b: IRBuilder, ty: str, fields: List[str],
                   dtor_name: str) -> str:
        raw = b.new_reg("raw")
        b.emit(f"{raw} = call i8* @lambda_alloc_env(i64 {self._env_size(fields)})")
        env = b.new_reg("env")
        b.emit(f"{env} = bitcast i8* {raw} to {ty}*")
        p = b.new_reg()
        b.emit(f"{p} = getelementptr {ty}, {ty}* {env}, i32 0, i32 0")
        b.emit(f"store void (i8*)* {dtor_name}, void (i8*)** {p}")
        return env

    def _gen_wrapper(self, info: FunctionInfo, stage: int) -> None:
        ty, fields, cap_idx, arg_idx = self._env_layout(info, stage)
        b = IRBuilder()
        name = info.wrapper_names[stage - 1]
        b.raw(f"define internal %Value* {name}(i8* %env_raw, %Value* %arg) {{")
        b.label("entry")
        env = b.new_reg("env")
        b.emit(f"{env} = bitcast i8* %env_raw to {ty}*")

        if stage < info.arity:
            nxt = stage + 1
            nty, nfields, ncap_idx, narg_idx = self._env_layout(info, nxt)
            nenv = self._alloc_env(b, nty, nfields, info.env_dtors[nxt - 1])
            for old_idx, new_idx, rep in zip(
                    cap_idx, ncap_idx, info.capture_reps):
                if rep == BOX:
                    v = self._load_env_field(b, ty, env, old_idx, "%Value*")
                    emit_retain_ir(b, v, self.atomic_rc)
                    self._store_env_field(b, nty, nenv, new_idx, "%Value*", v)
                elif rep == F64:
                    v = self._load_env_field(b, ty, env, old_idx, "double")
                    self._store_env_field(b, nty, nenv, new_idx, "double", v)
                else:
                    v = self._load_env_field(b, ty, env, old_idx, "i64")
                    self._store_env_field(b, nty, nenv, new_idx, "i64", v)
            for old_idx, new_idx in zip(arg_idx, narg_idx):
                v = self._load_env_field(b, ty, env, old_idx, "%Value*")
                emit_retain_ir(b, v, self.atomic_rc)
                self._store_env_field(b, nty, nenv, new_idx, "%Value*", v)
            # The newly supplied argument is the last field of the new env.
            new_arg_idx = len(nfields) - 1
            self._store_env_field(b, nty, nenv, new_arg_idx, "%Value*", "%arg")
            env_raw = b.new_reg()
            b.emit(f"{env_raw} = bitcast {nty}* {nenv} to i8*")
            code = b.new_reg()
            b.emit(f"{code} = bitcast ptr {info.wrapper_names[nxt - 1]} to i8*")
            res = b.new_reg()
            b.emit(f"{res} = call %Value* @lambda_make_closure(i8* {code}, i8* {env_raw})")
            b.emit(f"ret %Value* {res}")
            b.raw("}")
            self.functions.append(b.render())
            return

        # Final stage: invoke the unboxed direct entry.
        call_args: List[str] = []
        for idx, rep, ctype in zip(cap_idx, info.capture_reps, info.capture_types):
            if rep == I64:
                v = self._load_env_field(b, ty, env, idx, "i64")
                call_args.append(f"i64 {v}")
            elif rep == F64:
                v = self._load_env_field(b, ty, env, idx, "double")
                call_args.append(f"double {v}")
            else:
                v = self._load_env_field(b, ty, env, idx, "%Value*")
                emit_retain_ir(b, v, self.atomic_rc)
                call_args.append(f"%Value* {v}")
        for j, idx in enumerate(arg_idx):
            rep = info.param_reps[j]
            ptype = info.param_types[j]
            v = self._load_env_field(b, ty, env, idx, "%Value*")
            if rep == I64:
                getter = "@lambda_get_bool" if (not self.dynamic and is_bool_type(ptype)) else "@lambda_get_int"
                raw = b.new_reg()
                b.emit(f"{raw} = call i64 {getter}(%Value* {v})")
                call_args.append(f"i64 {raw}")
            elif rep == F64:
                raw = b.new_reg()
                b.emit(f"{raw} = call double @lambda_get_float(%Value* {v})")
                call_args.append(f"double {raw}")
            else:
                emit_retain_ir(b, v, self.atomic_rc)
                call_args.append(f"%Value* {v}")
        # The argument that triggered this final stage.
        rep = info.param_reps[info.arity - 1]
        ptype = info.param_types[info.arity - 1]
        if rep == I64:
            getter = "@lambda_get_bool" if (not self.dynamic and is_bool_type(ptype)) else "@lambda_get_int"
            raw = b.new_reg()
            b.emit(f"{raw} = call i64 {getter}(%Value* %arg)")
            emit_release_ir(b, "%arg", self.atomic_rc)
            call_args.append(f"i64 {raw}")
        elif rep == F64:
            raw = b.new_reg()
            b.emit(f"{raw} = call double @lambda_get_float(%Value* %arg)")
            emit_release_ir(b, "%arg", self.atomic_rc)
            call_args.append(f"double {raw}")
        else:
            call_args.append("%Value* %arg")

        ret_ty = self.rep_type(info.ret_rep)
        res = b.new_reg("res")
        b.emit(f"{res} = call {ret_ty} {info.direct_name}({', '.join(call_args)})")
        if info.ret_rep == I64:
            box = "@lambda_box_bool" if (not self.dynamic and is_bool_type(info.ret_type)) else "@lambda_box_int"
            boxed = b.new_reg()
            b.emit(f"{boxed} = call %Value* {box}(i64 {res})")
            b.emit(f"ret %Value* {boxed}")
        elif info.ret_rep == F64:
            boxed = b.new_reg()
            b.emit(f"{boxed} = call %Value* @lambda_box_float(double {res})")
            b.emit(f"ret %Value* {boxed}")
        elif info.ret_rep.startswith("REC:"):
            b.emit(f"ret {ret_ty} {res}")
        else:
            b.emit(f"ret %Value* {res}")
        b.raw("}")
        self.functions.append(b.render())

    # ------------------------------------------------------------------
    # function bodies
    # ------------------------------------------------------------------
    def _drain_functions(self) -> None:
        idx = 0
        while idx < len(self.pending_infos):
            info = self.pending_infos[idx]
            idx += 1
            if info.generated:
                continue
            self._gen_function_body(info)

    def _cache_array_meta(self, value: str) -> None:
        """Load an array's immutable length and data pointer once per frame."""
        lenp = self.new_reg()
        self.emit_inst(f"{lenp} = getelementptr %Value, %Value* {value}, i32 0, i32 2")
        length = self.new_reg()
        self.emit_inst(f"{length} = load i64, i64* {lenp}, !invariant.load !0")
        datap = self.new_reg()
        self.emit_inst(f"{datap} = getelementptr %Value, %Value* {value}, i32 0, i32 3")
        data = self.new_reg()
        self.emit_inst(f"{data} = load i8*, i8** {datap}, !invariant.load !0")
        self.array_meta[value] = (length, data)

    def _gen_function_body(self, info: FunctionInfo) -> None:
        if info.generated or info.generating:
            return
        info.generating = True
        self.builder = IRBuilder()
        self.ctx = FnContext(info.ret_rep, info.ret_type,
                             len(info.captures) + info.arity,
                             info.capture_reps + info.param_reps)
        self.scope_stack = []
        self.cleanup_stack = []
        self.terminated = False
        self.moved_uids = set()
        self.moves_allowed = True

        ret_ty = self.rep_type(info.ret_rep)
        sig: List[str] = []
        for i, rep in enumerate(info.capture_reps):
            sig.append(f"{self.rep_type(rep)} %cap{i}")
        for i, rep in enumerate(info.param_reps):
            attr = ""
            if i < len(info.param_borrows) and info.param_borrows[i]:
                attr = " nocapture readonly"
            sig.append(f"{self.rep_type(rep)}{attr} %par{i}")
        self.builder.raw(f"define internal {ret_ty} {info.direct_name}({', '.join(sig)}) inlinehint {{")
        self.builder.label("entry")

        scope: Dict[int, Storage] = {}
        cleanup: List[Tuple[str, bool]] = []
        self.scope_stack.append(scope)
        self.cleanup_stack.append(cleanup)
        for i, b in enumerate(info.captures):
            rep = info.capture_reps[i]
            op = f"%cap{i}"
            scope[b.uid] = Storage(rep, op)
            if rep == BOX:
                cleanup.append((op, False))
        for i, b in enumerate(info.params):
            rep = info.param_reps[i]
            op = f"%par{i}"
            scope[b.uid] = Storage(rep, op)
            if rep == BOX and not b.borrowed:
                cleanup.append((op, False))

        self.array_meta = {}
        for i, ctype in enumerate(info.capture_types):
            if info.capture_reps[i] == BOX and is_array_type(ctype):
                self._cache_array_meta(f"%cap{i}")
        for i, ptype in enumerate(info.param_types):
            if info.param_reps[i] == BOX and is_array_type(ptype):
                self._cache_array_meta(f"%par{i}")

        body = self.compile(info.lam.body, is_tail=True)
        if not self.terminated:
            if body is None:
                raise CompileError("internal: function body produced no value",
                                   info.lam.loc)
            self.emit_return(body, info.ret_type)

        info.generating = False
        info.generated = True
        self.builder.raw("}")
        self.functions.append(self.builder.render())

    # ------------------------------------------------------------------
    # representation helpers
    # ------------------------------------------------------------------
    def binding_rep(self, b: Binding) -> str:
        if self.dynamic:
            return BOX
        return rep_of(b.type)

    def node_rep(self, n: Node) -> str:
        if self.dynamic:
            return BOX
        return rep_of(n.type)

    def coerce(self, val: Val, target: str, type_hint) -> Val:
        if val.rep == target:
            return val
        if target == BOX:
            return self.box(val, type_hint)
        if target == F64:
            return self.unbox_float(val)
        if target == I64:
            if val.rep == F64:
                raise CompileError(
                    "cannot implicitly convert Float to Int; use float->int",
                    getattr(type_hint, "loc", None))
            return self.unbox(val, type_hint)
        if target.startswith("REC:"):
            raise CompileError(
                "cannot implicitly convert to a product type",
                getattr(type_hint, "loc", None))
        raise CompileError(f"cannot coerce {val.rep} to {target}")

    def box(self, val: Val, type_hint) -> Val:
        if val.rep.startswith("REC:"):
            raise CompileError(
                "product values cannot be boxed/first-class yet",
                getattr(type_hint, "loc", None))
        if val.rep == BOX:
            return val
        if val.rep == F64:
            res = self.new_reg()
            self.emit_inst(f"{res} = call %Value* @lambda_box_float(double {val.name})")
            return Val(BOX, res, owned=True)
        fn = "@lambda_box_bool" if (not self.dynamic and is_bool_type(type_hint)) else "@lambda_box_int"
        res = self.new_reg()
        self.emit_inst(f"{res} = call %Value* {fn}(i64 {val.name})")
        return Val(BOX, res, owned=True)

    def unbox(self, val: Val, type_hint) -> Val:
        if val.rep == I64:
            return val
        if val.rep.startswith("REC:"):
            raise CompileError(
                "product values have no implicit scalar conversion",
                getattr(type_hint, "loc", None))
        if val.rep == F64:
            raise CompileError(
                "cannot implicitly convert Float to Int; use float->int",
                getattr(type_hint, "loc", None))
        fn = "@lambda_get_bool" if (not self.dynamic and is_bool_type(type_hint)) else "@lambda_get_int"
        res = self.new_reg()
        self.emit_inst(f"{res} = call i64 {fn}(%Value* {val.name})")
        self.release(val)
        return Val(I64, res)

    def unbox_float(self, val: Val) -> Val:
        if val.rep == F64:
            return val
        if val.rep != BOX:
            raise CompileError("cannot implicitly convert Int to Float; use int->float")
        res = self.new_reg()
        self.emit_inst(f"{res} = call double @lambda_get_float(%Value* {val.name})")
        self.release(val)
        return Val(F64, res)

    def retain(self, val: Val) -> Val:
        if val.rep == BOX and not val.immortal:
            emit_retain_ir(self.builder, val.name, self.atomic_rc)
            val.owned = True
        return val

    def ensure_owned(self, val: Val) -> Val:
        if val.rep == BOX and val.borrowed:
            raise CompileError(
                "a borrowed inspection cannot be consumed, stored, or returned")
        if val.rep == BOX and not val.owned and not val.immortal:
            return self.retain(val)
        return val

    def release(self, val: Optional[Val]) -> None:
        if val is None:
            return
        if val.rep == BOX and val.owned and not val.immortal:
            emit_release_ir(self.builder, val.name, self.atomic_rc)
            val.owned = False

    def emit_inst(self, text: str) -> None:
        assert self.builder is not None
        self.builder.emit(text)

    def new_reg(self, prefix: str = "r") -> str:
        assert self.builder is not None
        return self.builder.new_reg(prefix)

    def new_label(self, prefix: str = "bb") -> str:
        assert self.builder is not None
        return self.builder.new_label(prefix)

    # ------------------------------------------------------------------
    # scope / cleanup
    # ------------------------------------------------------------------
    def push_scope(self) -> None:
        self.scope_stack.append({})
        self.cleanup_stack.append([])

    def pop_scope(self) -> None:
        if not self.terminated:
            for name, immortal in reversed(self.cleanup_stack[-1]):
                if not immortal:
                    emit_release_ir(self.builder, name, self.atomic_rc)
        self.cleanup_stack.pop()
        self.scope_stack.pop()

    def find_storage(self, uid: int) -> Optional[Storage]:
        for scope in reversed(self.scope_stack):
            st = scope.get(uid)
            if st is not None:
                return st
        return self.top_storage.get(uid)

    def emit_cleanup_all(self) -> None:
        for cleanup in self.cleanup_stack:
            for name, immortal in reversed(cleanup):
                if not immortal:
                    emit_release_ir(self.builder, name, self.atomic_rc)

    def emit_return(self, val: Optional[Val], type_hint) -> None:
        if self.ctx is None:
            raise CompileError("internal: return outside function")
        if val is None:
            return
        if val.borrowed:
            raise CompileError(
                "a borrowed parameter cannot be returned", type_hint.loc
                if hasattr(type_hint, "loc") else None)
        val = self.coerce(val, self.ctx.ret_rep, type_hint)
        self.emit_cleanup_all()
        if self.ctx.ret_rep == I64:
            self.emit_inst(f"ret i64 {val.name}")
        elif self.ctx.ret_rep == F64:
            self.emit_inst(f"ret double {val.name}")
        elif self.ctx.ret_rep.startswith("REC:"):
            self.emit_inst(f"ret {self.rep_type(self.ctx.ret_rep)} {val.name}")
        else:
            self.emit_inst(f"ret %Value* {val.name}")
        self.terminated = True

    # ------------------------------------------------------------------
    # constants
    # ------------------------------------------------------------------
    def nil_value(self) -> Val:
        if not self.nil_global_declared:
            self.nil_global_declared = True
            self.string_defs.append(
                "@lambda_nil_value = internal global %Value "
                "{ i32 5, i32 -1, i64 0, i8* null }")
        return Val(BOX, "@lambda_nil_value", immortal=True)

    def string_value(self, text: str) -> Val:
        existing = self.string_globals.get(text)
        if existing is not None:
            return Val(BOX, existing, immortal=True)
        data = text.encode("utf-8")
        self.string_counter += 1
        idx = self.string_counter
        bytes_name = f"@lambda_str_bytes_{idx}"
        val_name = f"@lambda_str_{idx}"
        escaped = "".join(f"\\{b:02X}" for b in data) + "\\00"
        self.string_defs.append(
            f"{bytes_name} = private constant [{len(data) + 1} x i8] c\"{escaped}\"")
        self.string_defs.append(
            f"{val_name} = internal global %Value {{ i32 3, i32 -1, i64 {len(data)}, "
            f"i8* getelementptr inbounds ([{len(data) + 1} x i8], "
            f"[{len(data) + 1} x i8]* {bytes_name}, i32 0, i32 0) }}")
        self.string_globals[text] = val_name
        return Val(BOX, val_name, immortal=True)

    def primitive_global(self, b: Binding) -> Val:
        op = OP_CANON.get(b.name)
        if op is None:
            raise CompileError(f"unknown primitive '{b.name}'", b.loc)
        if op == "nil":
            return self.nil_value()
        if op == "pmap":
            raise CompileError(
                "pmap must be applied directly to a provably pure function "
                "and a list", b.loc)
        if op in ("read_line", "read_int", "read_float") and not self.allow_input:
            raise CompileError(
                "terminal input is disabled; recompile with --input", b.loc)
        if op in ("write_bytes", "write_f64s", "write_i64s"):
            if not self.allow_data:
                raise CompileError(
                    "binary output is disabled; recompile with --data", b.loc)
            raise CompileError(
                "stream write primitives must be fully applied; first-class "
                "writers are not supported", b.loc)
        if op.startswith("array"):
            raise CompileError(
                "array primitives must be fully applied; first-class arrays "
                "are not supported yet", b.loc)
        if op in ("pi", "e"):
            value = math.pi if op == "pi" else math.e
            if self.dynamic:
                res = self.new_reg()
                self.emit_inst(f"{res} = call %Value* @lambda_box_float(double {float_const(value)})")
                return Val(BOX, res, owned=True)
            return Val(F64, float_const(value))
        name = PRIM_GLOBALS.get(op)
        if name is None:
            raise CompileError(
                f"primitive '{b.name}' cannot be used as a first-class value "
                f"without a direct application", b.loc)
        self.prim_decls.add(name)
        res = self.new_reg()
        self.emit_inst(f"{res} = load %Value*, %Value** {name}")
        return Val(BOX, res, immortal=True)

    # ------------------------------------------------------------------
    # expressions
    # ------------------------------------------------------------------
    def compile(self, node: Node, is_tail: bool = False) -> Optional[Val]:
        if isinstance(node, RecordLit):
            return self.compile_record(node)
        if isinstance(node, DataCtor):
            return self.compile_data_ctor(node)
        if isinstance(node, SumLit):
            return self.compile_sum(node)
        if isinstance(node, RecordField):
            return self.compile_record_field(node)
        if isinstance(node, EnumLit):
            return Val(I64, str(node.tag))
        if isinstance(node, Lit):
            return self.compile_lit(node, is_tail)
        if isinstance(node, Borrow):
            return self.compile_borrow(node)
        if isinstance(node, Var):
            return self.compile_var(node)
        if isinstance(node, Lam):
            return self.emit_make_closure(node)
        if isinstance(node, App):
            return self.compile_app(node, is_tail)
        if isinstance(node, If):
            return self.compile_if(node, is_tail)
        if isinstance(node, Match):
            return self.compile_match(node, is_tail)
        if isinstance(node, Block):
            return self.compile_block(node, is_tail)
        raise CompileError(f"cannot compile {type(node).__name__}", node.loc)

    def compile_lit(self, node: Lit, is_tail: bool) -> Optional[Val]:
        v = node.value
        if self.dynamic:
            if isinstance(v, bool):
                res = self.new_reg()
                self.emit_inst(f"{res} = call %Value* @lambda_box_bool(i64 {1 if v else 0})")
                return Val(BOX, res, owned=True)
            if isinstance(v, int):
                res = self.new_reg()
                self.emit_inst(f"{res} = call %Value* @lambda_box_int(i64 {v})")
                return Val(BOX, res, owned=True)
            if isinstance(v, float):
                res = self.new_reg()
                self.emit_inst(f"{res} = call %Value* @lambda_box_float(double {float_const(v)})")
                return Val(BOX, res, owned=True)
            return self.string_value(v)
        if isinstance(v, bool):
            return Val(I64, "1" if v else "0")
        if isinstance(v, int):
            return Val(I64, str(v))
        if isinstance(v, float):
            return Val(F64, float_const(v))
        return self.string_value(v)

    def compile_data_ctor(self, node: DataCtor) -> Val:
        adt = self.adt_schemas.get(node.data_name)
        if adt is not None and adt.layout == "arena":
            arena = self.adt_arenas.get(node.data_name)
            if arena is None:
                raise CompileError(f"no arena for data {node.data_name}", node.loc)
            info = adt.variant(node.variant)
            if info is None:
                raise CompileError(f"unknown data variant '{node.variant}'", node.loc)
            _, fields = info
            areg = self.new_reg()
            self.emit_inst(f"{areg} = load %AdtArena*, %AdtArena** {arena}")
            raws = []
            for arg, (_, ftype) in zip(node.args, fields):
                v = self.compile(arg, is_tail=False)
                if v is None:
                    raise CompileError("data field produced no value", arg.loc)
                if (isinstance(ftype, TList)
                        and isinstance(ftype.elem, TAdt)
                        and ftype.elem.layout == "arena"):
                    v = self.ensure_owned(v)
                    raw = self.new_reg()
                    self.emit_inst(
                        f"{raw} = call i64 @lambda_arena_attach_list(%AdtArena* {areg}, %Value* {v.name})")
                    raws.append(raw)
                    continue
                frep = rep_of(ftype)
                v = self.coerce(v, frep, ftype)
                if frep == I64:
                    raws.append(v.name)
                elif frep == F64:
                    raw = self.new_reg()
                    self.emit_inst(f"{raw} = bitcast double {v.name} to i64")
                    raws.append(raw)
                else:
                    raise CompileError(
                        "arena ADT field is not scalar/ADT", arg.loc)
            if 0 < len(raws) <= 4:
                fn = f"@lambda_arena_alloc{len(raws)}"
                args = ", ".join(["%AdtArena* " + areg, "i64 " + str(node.tag)]
                                  + ["i64 " + r for r in raws])
                idreg = self.new_reg()
                self.emit_inst(f"{idreg} = call i64 {fn}({args})")
                wp = self.new_reg()
                wslot = self.new_reg()
                self.emit_inst(f"{wslot} = getelementptr %AdtArena, %AdtArena* {areg}, i32 0, i32 3")
                self.emit_inst(f"{wp} = load i64*, i64** {wslot}")
                self.emit_inst(f"store i64* {wp}, i64** {self.adt_words[node.data_name]}")
                return Val(I64, idreg)
            idreg = self.new_reg()
            self.emit_inst(f"{idreg} = call i64 @lambda_arena_alloc(%AdtArena* {areg}, i64 {node.tag})")
            words_ptr = self.new_reg()
            wp_slot = self.new_reg()
            self.emit_inst(f"{wp_slot} = getelementptr %AdtArena, %AdtArena* {areg}, i32 0, i32 3")
            self.emit_inst(f"{words_ptr} = load i64*, i64** {wp_slot}")
            off = self.new_reg()
            self.emit_inst(f"{off} = mul i64 {idreg}, {self.adt_widths.get(node.data_name, 0) + 1}")
            row = self.new_reg()
            self.emit_inst(f"{row} = getelementptr i64, i64* {words_ptr}, i64 {off}")
            self.emit_inst(f"store i64* {words_ptr}, i64** {self.adt_words[node.data_name]}")
            for j, raw in enumerate(raws):
                slot = self.new_reg()
                self.emit_inst(f"{slot} = getelementptr i64, i64* {row}, i64 {j + 1}")
                self.emit_inst(f"store i64 {raw}, i64* {slot}")
            return Val(I64, idreg)
        vals = []
        for a in node.args:
            v = self.compile(a, is_tail=False)
            if v is None:
                raise CompileError("data field produced no value", a.loc)
            v = self.coerce(v, BOX, a.type)
            v = self.ensure_owned(v)
            vals.append(v)
        lst = self.nil_value()
        for v in reversed(vals):
            res = self.new_reg()
            self.emit_inst(f"{res} = call %Value* @lambda_cons(%Value* {v.name}, %Value* {lst.name})")
            lst = Val(BOX, res, owned=True)
        tag = self.new_reg()
        self.emit_inst(f"{tag} = call %Value* @lambda_box_int(i64 {node.tag})")
        res = self.new_reg()
        self.emit_inst(f"{res} = call %Value* @lambda_cons(%Value* {tag}, %Value* {lst.name})")
        return Val(BOX, res, owned=True)

    def compile_sum(self, node: SumLit) -> Val:
        schema = self.record_schemas.get(node.sum_name)
        if not isinstance(schema, TSum):
            raise CompileError(f"unknown sum type '{node.sum_name}'", node.loc)
        info = schema.variant(node.variant)
        if info is None:
            raise CompileError(f"unknown variant '{node.variant}'", node.loc)
        _, fields = info
        if len(fields) != len(node.args):
            raise CompileError(
                f"'{node.variant}' expects {len(fields)} payload fields",
                node.loc)
        rep = "REC:" + node.sum_name
        ty = self.rep_type(rep)
        all_float = schema.word_rep == F64
        cur = "undef"
        tag_reg = self.new_reg()
        self.emit_inst(f"{tag_reg} = insertvalue {ty} {cur}, i64 {node.tag}, 0")
        cur = tag_reg
        for i in range(schema.max_words):
            if all_float:
                word = "0.0"
                if i < len(fields):
                    arg = node.args[i]
                    ftype = fields[i][1]
                    fval = self.compile(arg, is_tail=False)
                    if fval is None:
                        raise CompileError("sum payload produced no value", arg.loc)
                    fval = self.coerce(fval, F64, ftype)
                    word = fval.name
                nxt = self.new_reg()
                self.emit_inst(
                    f"{nxt} = insertvalue {ty} {cur}, double {word}, {i + 1}")
            else:
                word = "0"
                if i < len(fields):
                    arg = node.args[i]
                    ftype = fields[i][1]
                    fval = self.compile(arg, is_tail=False)
                    if fval is None:
                        raise CompileError("sum payload produced no value", arg.loc)
                    fval = self.coerce(fval, rep_of(ftype), ftype)
                    if rep_of(ftype) == F64:
                        word_reg = self.new_reg()
                        self.emit_inst(
                            f"{word_reg} = bitcast double {fval.name} to i64")
                        word = word_reg
                    else:
                        word = fval.name
                nxt = self.new_reg()
                self.emit_inst(
                    f"{nxt} = insertvalue {ty} {cur}, i64 {word}, {i + 1}")
            cur = nxt
        return Val(rep, cur)

    def compile_record(self, node: RecordLit) -> Val:
        rec = self.record_schemas.get(node.record_name)
        if rec is None:
            raise CompileError(
                f"unknown product type '{node.record_name}'", node.loc)
        rep = "REC:" + node.record_name
        ty = self.rep_type(rep)
        cur = "undef"
        for idx, (arg, (_, ftype)) in enumerate(zip(node.args, rec.fields)):
            fval = self.compile(arg, is_tail=False)
            if fval is None:
                raise CompileError("record field produced no value", arg.loc)
            frep = rep_of(ftype)
            fval = self.coerce(fval, frep, ftype)
            nxt = self.new_reg()
            self.emit_inst(
                f"{nxt} = insertvalue {ty} {cur}, "
                f"{self.rep_type(frep)} {fval.name}, {idx}")
            cur = nxt
        return Val(rep, cur)

    def compile_record_field(self, node: RecordField) -> Val:
        value = self.compile(node.value, is_tail=False)
        if value is None:
            raise CompileError("record field base produced no value", node.loc)
        rep = "REC:" + node.record_name
        value = self.coerce(value, rep, node.value.type)
        rec = self.record_schemas.get(node.record_name)
        if rec is None:
            raise CompileError(
                f"unknown product type '{node.record_name}'", node.loc)
        idx = None
        ftype = None
        for i, (fname, ft) in enumerate(rec.fields):
            if fname == node.field_name:
                idx, ftype = i, ft
                break
        if idx is None:
            raise CompileError(
                f"'{node.record_name}' has no field '{node.field_name}'",
                node.loc)
        res = self.new_reg()
        self.emit_inst(
            f"{res} = extractvalue {self.rep_type(rep)} {value.name}, {idx}")
        return Val(rep_of(ftype), res)

    def compile_borrow(self, node: Borrow) -> Val:
        """Compile ``&x`` into a non-owning view of x's storage.

        The view is read-only and may not be consumed, returned, stored or
        captured.  For scalars it is just the value; the borrow marker mainly
        gives the type checker and the RC fast paths a precise lifetime.
        """
        inner = node.value
        if not isinstance(inner, Var) or inner.binding is None:
            raise CompileError("'&' expects a variable name", node.loc)
        b = inner.binding
        if b.is_function or b.kind == PRIM:
            raise CompileError("'&' cannot inspect a function value", node.loc)
        if b.uid in self.moved_uids:
            raise CompileError(
                f"'{b.name}' was consumed and cannot be inspected", node.loc)
        st = self.find_storage(b.uid)
        if st is None:
            raise CompileError(
                f"definition '{b.name}' is used before it is initialised",
                node.loc)
        if st.rep in (I64, F64) or st.rep.startswith("REC:"):
            return Val(st.rep, st.name, borrowed=True)
        if st.is_global:
            res = self.new_reg()
            self.emit_inst(f"{res} = load %Value*, %Value** {st.name}")
            return Val(BOX, res, owned=False, borrowed=True)
        return Val(BOX, st.name, owned=False, borrowed=True)

    def compile_var(self, node: Var) -> Val:
        b = node.binding
        if b is None:
            raise CompileError(f"unresolved variable '{node.name}'", node.loc)
        if node.field_name is not None:
            rt = prune(b.type)
            if not isinstance(rt, TRecord):
                raise CompileError(
                    f"'{b.name}' is not a product value", node.loc)
            base = Var(b.name, node.loc)
            base.binding = b
            base.type = rt
            return self.compile_record_field(
                RecordField(base, rt.name, node.field_name, "", node.loc))
        if b.kind == PRIM:
            return self.primitive_global(b)
        if b.is_function:
            return self.materialize_function(b)
        st = self.find_storage(b.uid)
        if st is None:
            raise CompileError(
                f"definition '{b.name}' is used before it is initialised", node.loc)
        if st.rep == BOX and not st.is_global and b.uid in self.moved_uids:
            raise CompileError(
                f"'{b.name}' was consumed by a destructive array update and "
                f"cannot be used again; use *-clone for a persistent copy",
                node.loc)
        if st.rep == I64:
            if st.is_global:
                res = self.new_reg()
                self.emit_inst(f"{res} = load i64, i64* {st.name}")
                return Val(I64, res)
            return Val(I64, st.name)
        if st.rep == F64:
            if st.is_global:
                res = self.new_reg()
                self.emit_inst(f"{res} = load double, double* {st.name}")
                return Val(F64, res)
            return Val(F64, st.name)
        if st.rep.startswith("REC:"):
            if st.is_global:
                res = self.new_reg()
                ty = self.rep_type(st.rep)
                self.emit_inst(f"{res} = load {ty}, {ty}* {st.name}")
                return Val(st.rep, res)
            return Val(st.rep, st.name)
        if st.is_global:
            res = self.new_reg()
            self.emit_inst(f"{res} = load %Value*, %Value** {st.name}")
            val = Val(BOX, res, owned=True)
            self.retain(val)
            return val
        if b.borrowed:
            return Val(BOX, st.name, owned=False, borrowed=True)
        val = Val(BOX, st.name, owned=True)
        self.retain(val)
        return val

    def _capture_value(self, b: Binding, loc) -> Val:
        if b.kind == PRIM:
            return self.primitive_global(b)
        if b.is_function:
            return self.materialize_function(b)
        if b.borrowed:
            raise CompileError(
                "a borrowed parameter cannot be captured by a closure", loc)
        st = self.find_storage(b.uid)
        if st is None:
            raise CompileError(
                f"captured value '{b.name}' is not available here "
                f"(definitions must be initialised in order)", loc)
        if st.rep == I64:
            if st.is_global:
                res = self.new_reg()
                self.emit_inst(f"{res} = load i64, i64* {st.name}")
                return Val(I64, res)
            return Val(I64, st.name)
        if st.rep == F64:
            if st.is_global:
                res = self.new_reg()
                self.emit_inst(f"{res} = load double, double* {st.name}")
                return Val(F64, res)
            return Val(F64, st.name)
        if st.rep.startswith("REC:"):
            if st.is_global:
                res = self.new_reg()
                ty = self.rep_type(st.rep)
                self.emit_inst(f"{res} = load {ty}, {ty}* {st.name}")
                return Val(st.rep, res)
            return Val(st.rep, st.name)
        if st.is_global:
            res = self.new_reg()
            self.emit_inst(f"{res} = load %Value*, %Value** {st.name}")
            val = Val(BOX, res, owned=True)
            self.retain(val)
            return val
        val = Val(BOX, st.name, owned=True)
        self.retain(val)
        return val

    def materialize_function(self, b: Binding) -> Val:
        if b.lam is not None:
            info = self.ensure_function(b.lam)
            reps = info.capture_reps + info.param_reps + [info.ret_rep]
            if any(rep.startswith("REC:") for rep in reps):
                raise CompileError(
                    "product-typed functions cannot be used as first-class "
                    "values yet", b.loc)
            if any(info.param_borrows):
                raise CompileError(
                    "functions with borrowed parameters cannot be first-class "
                    "values yet", b.loc)
        if b.kind == TOP_DEF:
            gname = self.top_function_globals.get(b.uid)
            if gname is None:
                raise CompileError(f"no closure for top-level function '{b.name}'", b.loc)
            res = self.new_reg()
            self.emit_inst(f"{res} = load %Value*, %Value** {gname}")
            val = Val(BOX, res, owned=True)
            self.retain(val)
            return val
        if b.lam is None:
            raise CompileError(f"'{b.name}' is not a function", b.loc)
        return self.emit_make_closure(b.lam)

    def emit_make_closure(self, lam: Lam) -> Val:
        info = self.ensure_function(lam)
        reps = info.capture_reps + info.param_reps + [info.ret_rep]
        if any(rep.startswith("REC:") for rep in reps):
            raise CompileError(
                "product-typed functions cannot be captured as closures yet",
                lam.loc)
        if any(info.param_borrows):
            raise CompileError(
                "functions with borrowed parameters cannot be captured as "
                "closures yet", lam.loc)
        ty, fields, cap_idx, _ = self._env_layout(info, 1)
        env = self._alloc_env(self.builder, ty, fields, info.env_dtors[0])
        for idx, cap, rep, ctype in zip(cap_idx, info.captures,
                                        info.capture_reps, info.capture_types):
            val = self._capture_value(cap, lam.loc)
            val = self.coerce(val, rep, ctype)
            if rep == I64:
                self._store_env_field(self.builder, ty, env, idx, "i64", val.name)
            elif rep == F64:
                self._store_env_field(self.builder, ty, env, idx, "double", val.name)
            else:
                val = self.ensure_owned(val)
                self._store_env_field(self.builder, ty, env, idx, "%Value*", val.name)
        env_raw = self.new_reg()
        self.emit_inst(f"{env_raw} = bitcast {ty}* {env} to i8*")
        code = self.new_reg()
        self.emit_inst(f"{code} = bitcast ptr {info.wrapper_names[0]} to i8*")
        res = self.new_reg()
        self.emit_inst(f"{res} = call %Value* @lambda_make_closure(i8* {code}, i8* {env_raw})")
        return Val(BOX, res, owned=True)

    def compile_block(self, block: Block, is_tail: bool) -> Optional[Val]:
        self.push_scope()
        for d in block.defs:
            if d.is_function or (d.binding is not None and d.binding.is_function):
                continue
            val = self.compile(d.value, is_tail=False)
            if val is None:
                raise CompileError("definition value did not produce a value", d.loc)
            rep = self.binding_rep(d.binding) if d.binding is not None else self.node_rep(d.value)
            val = self.coerce(val, rep, d.value.type)
            self.scope_stack[-1][d.binding.uid] = Storage(rep, val.name)
            if rep == BOX and not val.immortal and val.owned:
                self.cleanup_stack[-1].append((val.name, False))
        result = self.compile(block.body, is_tail=is_tail)
        if not self.terminated:
            self.pop_scope()
        else:
            self.cleanup_stack.pop()
            self.scope_stack.pop()
        return result

    def compile_match(self, node: Match, is_tail: bool) -> Optional[Val]:
        subject_type = prune(node.subject.type)
        if isinstance(subject_type, TAdt):
            if subject_type.layout == "arena":
                return self.compile_adt_arena_match(node, subject_type, is_tail)
            return self.compile_adt_match(node, subject_type, is_tail)
        if isinstance(subject_type, TSum):
            return self.compile_sum_match(node, subject_type, is_tail)
        subject = self.compile(node.subject, is_tail=False)
        if subject is None:
            raise CompileError("match subject produced no value", node.subject.loc)
        if self.dynamic or subject.rep == BOX:
            tag = self.new_reg()
            self.emit_inst(f"{tag} = call i64 @lambda_get_int(%Value* {subject.name})")
            self.release(subject)
        elif subject.rep == I64:
            tag = subject.name
        else:
            raise CompileError("match subject must be an enum", node.subject.loc)

        arms = []
        enum_name = None
        for pattern, arm in node.arms:
            info = self.variant_tags.get(pattern)
            if info is None:
                raise CompileError(f"unknown enum variant '{pattern}'", arm.loc)
            name, arm_tag = info
            if enum_name is None:
                enum_name = name
            elif enum_name != name:
                raise CompileError(
                    f"match mixes variants of {enum_name} and {name}", arm.loc)
            arms.append((arm_tag, arm))

        saved_moved = set(self.moved_uids)
        saved_cleanup = [list(c) for c in self.cleanup_stack]
        saved_allowed = self.moves_allowed

        def restore_branch_state() -> None:
            self.moved_uids = set(saved_moved)
            self.cleanup_stack = [list(c) for c in saved_cleanup]
            self.moves_allowed = saved_allowed and is_tail

        default_bb = self.new_label("match_default")
        arm_bbs = [self.new_label("match_arm") for _ in arms]
        cases = " ".join(
            f"i64 {arm_tag}, label %{bb}"
            for (arm_tag, _), bb in zip(arms, arm_bbs)
        )
        self.emit_inst(
            f"switch i64 {tag}, label %{default_bb} [ {cases} ]")

        if is_tail:
            for bb, (_, arm) in zip(arm_bbs, arms):
                self.builder.label(bb)
                self.terminated = False
                restore_branch_state()
                value = self.compile(arm, is_tail=True)
                if not self.terminated:
                    self.emit_return(value, arm.type)
            self.builder.label(default_bb)
            self.emit_inst("call void @lambda_bad_enum()")
            self.emit_inst("unreachable")
            self.terminated = True
            self.moved_uids = set(saved_moved)
            self.cleanup_stack = [list(c) for c in saved_cleanup]
            self.moves_allowed = saved_allowed
            return None

        common = self.node_rep(node)
        slot = self.builder.new_alloca(self.rep_type(common))
        merge_bb = self.new_label("match_merge")
        for bb, (_, arm) in zip(arm_bbs, arms):
            self.builder.label(bb)
            self.terminated = False
            restore_branch_state()
            value = self.compile(arm, is_tail=False)
            if value is None:
                raise CompileError("match arm produced no value", arm.loc)
            value = self.coerce(value, common, arm.type)
            if common == I64:
                self.emit_inst(f"store i64 {value.name}, i64* {slot}")
            elif common == F64:
                self.emit_inst(f"store double {value.name}, double* {slot}")
            else:
                self.emit_inst(f"store %Value* {value.name}, %Value** {slot}")
            self.emit_inst(f"br label %{merge_bb}")

        self.builder.label(default_bb)
        self.emit_inst("call void @lambda_bad_enum()")
        self.emit_inst("unreachable")

        self.builder.label(merge_bb)
        self.terminated = False
        self.moved_uids = set(saved_moved)
        self.cleanup_stack = [list(c) for c in saved_cleanup]
        self.moves_allowed = saved_allowed
        res = self.new_reg()
        if common == I64:
            self.emit_inst(f"{res} = load i64, i64* {slot}")
            return Val(I64, res)
        if common == F64:
            self.emit_inst(f"{res} = load double, double* {slot}")
            return Val(F64, res)
        if common.startswith("REC:"):
            ty = self.rep_type(common)
            self.emit_inst(f"{res} = load {ty}, {ty}* {slot}")
            return Val(common, res)
        self.emit_inst(f"{res} = load %Value*, %Value** {slot}")
        return Val(BOX, res, owned=True)

    def compile_adt_arena_match(self, node: Match, rec: TAdt,
                                is_tail: bool) -> Optional[Val]:
        arena = self.adt_arenas.get(rec.name)
        if arena is None:
            raise CompileError(f"no arena for data {rec.name}", node.loc)
        subject = self.compile(node.subject, is_tail=False)
        if subject is None or subject.rep != I64:
            raise CompileError("arena data subject must be an i64 node id", node.subject.loc)
        areg = self.new_reg()  # retained for diagnostics only; rows use the cache
        self.emit_inst(f"{areg} = load %AdtArena*, %AdtArena** {arena}")
        words_ptr = self.new_reg()
        self.emit_inst(f"{words_ptr} = load i64*, i64** {self.adt_words[rec.name]}")
        subj_off = self.new_reg()
        self.emit_inst(f"{subj_off} = mul i64 {subject.name}, {self.adt_widths.get(rec.name, 0) + 1}")
        subject_row = self.new_reg()
        self.emit_inst(f"{subject_row} = getelementptr i64, i64* {words_ptr}, i64 {subj_off}")
        tag = self.new_reg()
        self.emit_inst(f"{tag} = load i64, i64* {subject_row}")

        arms = []
        for pattern, arm in node.arms:
            info = rec.variant(pattern)
            if info is None:
                raise CompileError(f"unknown data variant '{pattern}'", arm.loc)
            arm_tag, fields = info
            arms.append((arm_tag, fields, arm))

        saved_moved = set(self.moved_uids)
        saved_cleanup = [list(c) for c in self.cleanup_stack]
        saved_allowed = self.moves_allowed

        def restore() -> None:
            self.moved_uids = set(saved_moved)
            self.cleanup_stack = [list(c) for c in saved_cleanup]
            self.moves_allowed = saved_allowed and is_tail

        default_bb = self.new_label("arena_default")
        arm_bbs = [self.new_label("arena_arm") for _ in arms]
        cases = " ".join(
            f"i64 {arm_tag}, label %{bb}"
            for (arm_tag, _, _), bb in zip(arms, arm_bbs))
        self.emit_inst(f"switch i64 {tag}, label %{default_bb} [ {cases} ]")

        def bind_fields(idx: int, fields) -> None:
            self.push_scope()
            bindings = node.arm_bindings[idx] if idx < len(node.arm_bindings) else []
            row = subject_row
            for j, ((_, ftype), bnd) in enumerate(zip(fields, bindings)):
                slot = self.new_reg()
                self.emit_inst(f"{slot} = getelementptr i64, i64* {row}, i64 {j + 1}")
                raw = self.new_reg()
                self.emit_inst(f"{raw} = load i64, i64* {slot}")
                if (isinstance(ftype, TList)
                        and isinstance(ftype.elem, TAdt)
                        and ftype.elem.layout == "arena"):
                    lst = self.new_reg()
                    self.emit_inst(
                        f"{lst} = call %Value* @lambda_arena_get_list(%AdtArena* {areg}, i64 {raw})")
                    self.scope_stack[-1][bnd.uid] = Storage(BOX, lst)
                    self.cleanup_stack[-1].append((lst, False))
                    continue
                frep = rep_of(ftype)
                if frep == F64:
                    val = self.new_reg()
                    self.emit_inst(f"{val} = bitcast i64 {raw} to double")
                    self.scope_stack[-1][bnd.uid] = Storage(F64, val)
                else:
                    self.scope_stack[-1][bnd.uid] = Storage(I64, raw)

        if is_tail:
            for idx, (bb, (_, fields, arm)) in enumerate(zip(arm_bbs, arms)):
                self.builder.label(bb)
                self.terminated = False
                restore()
                bind_fields(idx, fields)
                value = self.compile(arm, is_tail=True)
                if not self.terminated:
                    self.emit_return(value, arm.type)
                self.pop_scope()
            self.builder.label(default_bb)
            self.emit_inst("call void @lambda_bad_enum()")
            self.emit_inst("unreachable")
            self.terminated = True
            self.moved_uids = set(saved_moved)
            self.cleanup_stack = [list(c) for c in saved_cleanup]
            self.moves_allowed = saved_allowed
            return None

        common = self.node_rep(node)
        slot = self.builder.new_alloca(self.rep_type(common))
        merge_bb = self.new_label("arena_merge")
        for idx, (bb, (_, fields, arm)) in enumerate(zip(arm_bbs, arms)):
            self.builder.label(bb)
            self.terminated = False
            restore()
            bind_fields(idx, fields)
            value = self.compile(arm, is_tail=False)
            if value is None:
                raise CompileError("data match arm produced no value", arm.loc)
            value = self.coerce(value, common, arm.type)
            self.pop_scope()
            if common == I64:
                self.emit_inst(f"store i64 {value.name}, i64* {slot}")
            elif common == F64:
                self.emit_inst(f"store double {value.name}, double* {slot}")
            else:
                self.emit_inst(f"store %Value* {value.name}, %Value** {slot}")
            self.emit_inst(f"br label %{merge_bb}")

        self.builder.label(default_bb)
        self.emit_inst("call void @lambda_bad_enum()")
        self.emit_inst("unreachable")
        self.builder.label(merge_bb)
        self.terminated = False
        self.moved_uids = set(saved_moved)
        self.cleanup_stack = [list(c) for c in saved_cleanup]
        self.moves_allowed = saved_allowed
        res = self.new_reg()
        if common == I64:
            self.emit_inst(f"{res} = load i64, i64* {slot}")
            return Val(I64, res)
        if common == F64:
            self.emit_inst(f"{res} = load double, double* {slot}")
            return Val(F64, res)
        self.emit_inst(f"{res} = load %Value*, %Value** {slot}")
        return Val(BOX, res, owned=True)

    def compile_adt_match(self, node: Match, rec: TAdt,
                          is_tail: bool) -> Optional[Val]:
        subject = self.compile(node.subject, is_tail=False)
        if subject is None or subject.rep != BOX:
            raise CompileError("data match subject must be boxed", node.subject.loc)
        tag = self.new_reg()
        self.emit_inst(f"{tag} = call i64 @lambda_adt_tag(%Value* {subject.name})")

        arms = []
        for pattern, arm in node.arms:
            info = rec.variant(pattern)
            if info is None:
                raise CompileError(f"unknown data variant '{pattern}'", arm.loc)
            arm_tag, fields = info
            arms.append((arm_tag, fields, arm))

        # Each arm needs the subject for field extraction; take one retained
        # copy per arm and release the original before switching.
        # One retained copy is enough: only the selected switch arm executes
        # and releases it after extracting its fields.
        copy = self.retain(subject)
        copy_slot = self.builder.new_alloca("%Value*")
        self.emit_inst(f"store %Value* {copy.name}, %Value** {copy_slot}")
        self.release(subject)

        saved_moved = set(self.moved_uids)
        saved_cleanup = [list(c) for c in self.cleanup_stack]
        saved_allowed = self.moves_allowed

        def restore() -> None:
            self.moved_uids = set(saved_moved)
            self.cleanup_stack = [list(c) for c in saved_cleanup]
            self.moves_allowed = saved_allowed and is_tail

        default_bb = self.new_label("adt_default")
        arm_bbs = [self.new_label("adt_arm") for _ in arms]
        cases = " ".join(
            f"i64 {arm_tag}, label %{bb}"
            for (arm_tag, _, _), bb in zip(arms, arm_bbs))
        self.emit_inst(f"switch i64 {tag}, label %{default_bb} [ {cases} ]")

        def bind_fields(idx: int, fields) -> None:
            self.push_scope()
            copy = self.new_reg()
            self.emit_inst(f"{copy} = load %Value*, %Value** {copy_slot}")
            bindings = node.arm_bindings[idx] if idx < len(node.arm_bindings) else []
            for j, bnd in enumerate(bindings):
                ftype = fields[j][1]
                frep = rep_of(ftype)
                raw = self.new_reg()
                self.emit_inst(f"{raw} = call %Value* @lambda_adt_field(%Value* {copy}, i64 {j})")
                val = self.coerce(Val(BOX, raw, owned=True), frep, ftype)
                self.scope_stack[-1][bnd.uid] = Storage(frep, val.name)
                if frep == BOX:
                    self.cleanup_stack[-1].append((val.name, False))
            self.emit_inst(f"call void @lambda_release(%Value* {copy})")

        if is_tail:
            for idx, (bb, (_, fields, arm)) in enumerate(zip(arm_bbs, arms)):
                self.builder.label(bb)
                self.terminated = False
                restore()
                bind_fields(idx, fields)
                value = self.compile(arm, is_tail=True)
                if not self.terminated:
                    # Fields bound from the arena/list are owned.  A tail
                    # return bypasses pop_scope's cleanup; release them before
                    # returning unless the returned value is the field itself.
                    for nm, immortal in reversed(self.cleanup_stack[-1]):
                        if not immortal and (value is None or nm != value.name):
                            emit_release_ir(self.builder, nm, self.atomic_rc)
                    self.cleanup_stack[-1].clear()
                    self.emit_return(value, arm.type)
                self.pop_scope()
            self.builder.label(default_bb)
            self.emit_inst("call void @lambda_bad_enum()")
            self.emit_inst("unreachable")
            self.terminated = True
            self.moved_uids = set(saved_moved)
            self.cleanup_stack = [list(c) for c in saved_cleanup]
            self.moves_allowed = saved_allowed
            return None

        common = self.node_rep(node)
        slot = self.builder.new_alloca(self.rep_type(common))
        merge_bb = self.new_label("adt_merge")
        for idx, (bb, (_, fields, arm)) in enumerate(zip(arm_bbs, arms)):
            self.builder.label(bb)
            self.terminated = False
            restore()
            bind_fields(idx, fields)
            value = self.compile(arm, is_tail=False)
            if value is None:
                raise CompileError("data match arm produced no value", arm.loc)
            value = self.coerce(value, common, arm.type)
            self.pop_scope()
            if common == I64:
                self.emit_inst(f"store i64 {value.name}, i64* {slot}")
            elif common == F64:
                self.emit_inst(f"store double {value.name}, double* {slot}")
            else:
                self.emit_inst(f"store %Value* {value.name}, %Value** {slot}")
            self.emit_inst(f"br label %{merge_bb}")

        self.builder.label(default_bb)
        self.emit_inst("call void @lambda_bad_enum()")
        self.emit_inst("unreachable")
        self.builder.label(merge_bb)
        self.terminated = False
        self.moved_uids = set(saved_moved)
        self.cleanup_stack = [list(c) for c in saved_cleanup]
        self.moves_allowed = saved_allowed
        res = self.new_reg()
        if common == I64:
            self.emit_inst(f"{res} = load i64, i64* {slot}")
            return Val(I64, res)
        if common == F64:
            self.emit_inst(f"{res} = load double, double* {slot}")
            return Val(F64, res)
        self.emit_inst(f"{res} = load %Value*, %Value** {slot}")
        return Val(BOX, res, owned=True)

    def compile_sum_match(self, node: Match, rec: TSum,
                          is_tail: bool) -> Optional[Val]:
        subject = self.compile(node.subject, is_tail=False)
        if subject is None:
            raise CompileError("match subject produced no value", node.subject.loc)
        rep = "REC:" + rec.name
        if subject.rep != rep:
            raise CompileError("sum match subject is not unboxed", node.subject.loc)
        ty = self.rep_type(rep)
        tag = self.new_reg()
        self.emit_inst(f"{tag} = extractvalue {ty} {subject.name}, 0")

        arms = []
        for pattern, arm in node.arms:
            info = rec.variant(pattern)
            if info is None:
                raise CompileError(f"unknown sum variant '{pattern}'", arm.loc)
            arm_tag, fields = info
            arms.append((arm_tag, fields, arm))

        saved_moved = set(self.moved_uids)
        saved_cleanup = [list(c) for c in self.cleanup_stack]
        saved_allowed = self.moves_allowed

        def restore() -> None:
            self.moved_uids = set(saved_moved)
            self.cleanup_stack = [list(c) for c in saved_cleanup]
            self.moves_allowed = saved_allowed and is_tail

        default_bb = self.new_label("sum_default")
        arm_bbs = [self.new_label("sum_arm") for _ in arms]
        cases = " ".join(
            f"i64 {arm_tag}, label %{bb}"
            for (arm_tag, _, _), bb in zip(arms, arm_bbs))
        self.emit_inst(f"switch i64 {tag}, label %{default_bb} [ {cases} ]")

        def bind_payload(arm_idx: int, fields) -> None:
            self.push_scope()
            bindings = (node.arm_bindings[arm_idx]
                        if arm_idx < len(node.arm_bindings) else [])
            for j, bnd in enumerate(bindings):
                ftype = fields[j][1]
                frep = rep_of(ftype)
                if rec.word_rep == F64:
                    word = self.new_reg()
                    self.emit_inst(
                        f"{word} = extractvalue {ty} {subject.name}, {j + 1}")
                    self.scope_stack[-1][bnd.uid] = Storage(F64, word)
                else:
                    word = self.new_reg()
                    self.emit_inst(
                        f"{word} = extractvalue {ty} {subject.name}, {j + 1}")
                    if frep == F64:
                        val = self.new_reg()
                        self.emit_inst(f"{val} = bitcast i64 {word} to double")
                        self.scope_stack[-1][bnd.uid] = Storage(F64, val)
                    else:
                        self.scope_stack[-1][bnd.uid] = Storage(I64, word)

        if is_tail:
            for idx, (bb, (_, fields, arm)) in enumerate(zip(arm_bbs, arms)):
                self.builder.label(bb)
                self.terminated = False
                restore()
                bind_payload(idx, fields)
                value = self.compile(arm, is_tail=True)
                if not self.terminated:
                    self.emit_return(value, arm.type)
                self.pop_scope()
            self.builder.label(default_bb)
            self.emit_inst("call void @lambda_bad_enum()")
            self.emit_inst("unreachable")
            self.terminated = True
            self.moved_uids = set(saved_moved)
            self.cleanup_stack = [list(c) for c in saved_cleanup]
            self.moves_allowed = saved_allowed
            return None

        common = self.node_rep(node)
        slot = self.builder.new_alloca(self.rep_type(common))
        merge_bb = self.new_label("sum_merge")
        for idx, (bb, (_, fields, arm)) in enumerate(zip(arm_bbs, arms)):
            self.builder.label(bb)
            self.terminated = False
            restore()
            bind_payload(idx, fields)
            value = self.compile(arm, is_tail=False)
            if value is None:
                raise CompileError("sum match arm produced no value", arm.loc)
            value = self.coerce(value, common, arm.type)
            self.pop_scope()
            if common == I64:
                self.emit_inst(f"store i64 {value.name}, i64* {slot}")
            elif common == F64:
                self.emit_inst(f"store double {value.name}, double* {slot}")
            elif common.startswith("REC:"):
                sty = self.rep_type(common)
                self.emit_inst(f"store {sty} {value.name}, {sty}* {slot}")
            else:
                self.emit_inst(f"store %Value* {value.name}, %Value** {slot}")
            self.emit_inst(f"br label %{merge_bb}")

        self.builder.label(default_bb)
        self.emit_inst("call void @lambda_bad_enum()")
        self.emit_inst("unreachable")
        self.builder.label(merge_bb)
        self.terminated = False
        self.moved_uids = set(saved_moved)
        self.cleanup_stack = [list(c) for c in saved_cleanup]
        self.moves_allowed = saved_allowed
        res = self.new_reg()
        if common == I64:
            self.emit_inst(f"{res} = load i64, i64* {slot}")
            return Val(I64, res)
        if common == F64:
            self.emit_inst(f"{res} = load double, double* {slot}")
            return Val(F64, res)
        if common.startswith("REC:"):
            sty = self.rep_type(common)
            self.emit_inst(f"{res} = load {sty}, {sty}* {slot}")
            return Val(common, res)
        self.emit_inst(f"{res} = load %Value*, %Value** {slot}")
        return Val(BOX, res, owned=True)

    def compile_if(self, node: If, is_tail: bool) -> Optional[Val]:
        cond = self.compile(node.cond, is_tail=False)
        if cond is None:
            return None
        if self.dynamic or cond.rep == BOX:
            raw = self.new_reg()
            self.emit_inst(f"{raw} = call i64 @lambda_truthy(%Value* {cond.name})")
            self.release(cond)
            cbool = self.new_reg()
            self.emit_inst(f"{cbool} = icmp ne i64 {raw}, 0")
        else:
            cbool = self.new_reg()
            self.emit_inst(f"{cbool} = icmp ne i64 {cond.name}, 0")

        then_bb = self.new_label("then")
        else_bb = self.new_label("else")
        self.emit_inst(f"br i1 {cbool}, label %{then_bb}, label %{else_bb}")

        saved_moved = set(self.moved_uids)
        saved_cleanup = [list(c) for c in self.cleanup_stack]
        saved_allowed = self.moves_allowed

        def restore_branch_state() -> None:
            self.moved_uids = set(saved_moved)
            self.cleanup_stack = [list(c) for c in saved_cleanup]
            # Moves in a branch are only safe when that branch returns.
            self.moves_allowed = saved_allowed and is_tail

        if is_tail:
            self.builder.label(then_bb)
            self.terminated = False
            restore_branch_state()
            tv = self.compile(node.then, is_tail=True)
            if not self.terminated:
                self.emit_return(tv, node.then.type)
            self.builder.label(else_bb)
            self.terminated = False
            restore_branch_state()
            ev = self.compile(node.else_, is_tail=True)
            if not self.terminated:
                self.emit_return(ev, node.else_.type)
            self.terminated = True
            self.moved_uids = set(saved_moved)
            self.cleanup_stack = [list(c) for c in saved_cleanup]
            self.moves_allowed = saved_allowed
            return None

        common = self.node_rep(node)
        slot_ty = self.rep_type(common)
        slot = self.builder.new_alloca(slot_ty)
        merge_bb = self.new_label("merge")

        self.builder.label(then_bb)
        self.terminated = False
        restore_branch_state()
        tv = self.compile(node.then, is_tail=False)
        if tv is None:
            raise CompileError("if branch produced no value", node.then.loc)
        tv = self.coerce(tv, common, node.then.type)
        if common == I64:
            self.emit_inst(f"store i64 {tv.name}, i64* {slot}")
        elif common == F64:
            self.emit_inst(f"store double {tv.name}, double* {slot}")
        elif common.startswith("REC:"):
            ty = self.rep_type(common)
            self.emit_inst(f"store {ty} {tv.name}, {ty}* {slot}")
        else:
            self.emit_inst(f"store %Value* {tv.name}, %Value** {slot}")
        self.emit_inst(f"br label %{merge_bb}")

        self.builder.label(else_bb)
        self.terminated = False
        restore_branch_state()
        ev = self.compile(node.else_, is_tail=False)
        if ev is None:
            raise CompileError("if branch produced no value", node.else_.loc)
        ev = self.coerce(ev, common, node.else_.type)
        if common == I64:
            self.emit_inst(f"store i64 {ev.name}, i64* {slot}")
        elif common == F64:
            self.emit_inst(f"store double {ev.name}, double* {slot}")
        elif common.startswith("REC:"):
            ty = self.rep_type(common)
            self.emit_inst(f"store {ty} {ev.name}, {ty}* {slot}")
        else:
            self.emit_inst(f"store %Value* {ev.name}, %Value** {slot}")
        self.emit_inst(f"br label %{merge_bb}")

        self.builder.label(merge_bb)
        self.terminated = False
        res = self.new_reg()
        if common == I64:
            self.emit_inst(f"{res} = load i64, i64* {slot}")
            return Val(I64, res)
        if common == F64:
            self.emit_inst(f"{res} = load double, double* {slot}")
            return Val(F64, res)
        if common.startswith("REC:"):
            ty = self.rep_type(common)
            self.emit_inst(f"{res} = load {ty}, {ty}* {slot}")
            return Val(common, res)
        self.emit_inst(f"{res} = load %Value*, %Value** {slot}")
        return Val(BOX, res, owned=True)

    # ------------------------------------------------------------------
    # applications
    # ------------------------------------------------------------------
    def flatten_app(self, node: Node) -> Tuple[Node, List[Node]]:
        args: List[Node] = []
        cur = node
        while isinstance(cur, App):
            args = list(cur.args) + args
            cur = cur.func
        return cur, args

    def compile_app(self, node: App, is_tail: bool) -> Optional[Val]:
        head, args = self.flatten_app(node)
        if isinstance(head, Var) and head.binding is not None:
            b = head.binding
            if b.kind == PRIM:
                return self.compile_prim_app(b, args, node, is_tail)
            if b.is_function and b.lam is not None:
                return self.compile_named_call(b.lam, args, node, is_tail)
        if isinstance(head, Lam):
            return self.compile_named_call(head, args, node, is_tail)
        fn = self.compile(head, is_tail=False)
        if fn is None:
            return None
        fn = self.coerce(fn, BOX, head.type)
        return self.apply_closure_chain(fn, args, node.type, is_tail)

    def compile_named_call(self, lam: Lam, args: List[Node], node: Node,
                           is_tail: bool) -> Optional[Val]:
        info = self.ensure_function(lam)
        n = info.arity
        if len(args) == n:
            return self.emit_direct_call(info, args, node.type, is_tail)
        if len(args) < n:
            clo = self.emit_make_closure(lam)
            return self.apply_closure_chain(clo, args, node.type, is_tail)
        first, rest = args[:n], args[n:]
        res = self.emit_direct_call(info, first, info.ret_type, is_tail=False)
        if res is None:
            return None
        res = self.coerce(res, BOX, info.ret_type)
        return self.apply_closure_chain(res, rest, node.type, is_tail)

    def _move_tail_arg(self, node: Node, args: List[Node],
                       info: FunctionInfo, index: int) -> Optional[Val]:
        """Move a boxed parameter into a tail call.

        After a tail call there is no later use in this frame, so the callee
        can take over the reference.  Skip captured parameters and duplicated
        arguments; this keeps the move local and linear.
        """

        if not isinstance(node, Var) or node.binding is None:
            return None
        b = node.binding
        if b.kind != PARAM or b.uid in self.captured_uids or b.borrowed:
            return None
        if any(c.uid == b.uid for c in info.captures):
            return None
        # A later argument could still read the parameter.  Earlier arguments
        # have already been evaluated, so moving after them is safe.
        for other in args[index + 1:]:
            if any(isinstance(n, Var) and n.binding is b for n in walk(other)):
                return None
        st = self.find_storage(b.uid)
        if st is None or st.rep != BOX or st.is_global:
            return None
        if not st.name.startswith("%par"):
            return None
        if b.uid in self.moved_uids:
            raise CompileError(
                f"'{b.name}' was already consumed and cannot be moved again",
                node.loc)
        self.moved_uids.add(b.uid)
        for cleanup in self.cleanup_stack:
            cleanup[:] = [(name, immortal) for name, immortal in cleanup
                          if name != st.name]
        return Val(BOX, st.name, owned=True)

    def emit_direct_call(self, info: FunctionInfo, args: List[Node],
                         result_type, is_tail: bool) -> Optional[Val]:
        call_args: List[str] = []
        borrow_temps: List[Val] = []

        for cap, rep, ctype in zip(info.captures, info.capture_reps,
                                   info.capture_types):
            val = None
            if rep == BOX and is_tail:
                val = self._move_tail_capture(cap)
            if val is None:
                val = self._capture_value(cap, info.lam.loc if info.lam else None)
                val = self.coerce(val, rep, ctype)
            if rep == I64:
                call_args.append(f"i64 {val.name}")
            elif rep == F64:
                call_args.append(f"double {val.name}")
            elif rep.startswith("REC:"):
                call_args.append(f"{self.rep_type(rep)} {val.name}")
            else:
                val = self.ensure_owned(val)
                call_args.append(f"%Value* {val.name}")

        for pos, (arg, rep, ptype) in enumerate(zip(args, info.param_reps,
                                                    info.param_types)):
            callee_borrow = (pos < len(info.param_borrows)
                             and info.param_borrows[pos])
            if (isinstance(arg, Var) and arg.binding is not None
                    and arg.binding.borrowed and not callee_borrow):
                raise CompileError(
                    "a borrowed value cannot be passed to a consuming "
                    "parameter", arg.loc)
            val = None
            if rep == BOX and callee_borrow:
                val = self._borrow_value_for_call(arg)
            if rep == BOX and is_tail and not callee_borrow:
                val = self._move_tail_arg(arg, args, info, pos)
            if val is None:
                val = self.compile(arg, is_tail=False)
                if val is None:
                    return None
                val = self.coerce(val, rep, arg.type)
            if not callee_borrow and val.borrowed:
                raise CompileError(
                    "a borrowed inspection cannot be passed to a consuming "
                    "parameter", arg.loc)
            if rep == I64:
                call_args.append(f"i64 {val.name}")
            elif rep == F64:
                call_args.append(f"double {val.name}")
            elif rep.startswith("REC:"):
                call_args.append(f"{self.rep_type(rep)} {val.name}")
            elif callee_borrow:
                if val.owned:
                    borrow_temps.append(val)
                call_args.append(f"%Value* {val.name}")
            else:
                val = self.ensure_owned(val)
                call_args.append(f"%Value* {val.name}")

        ret_ty = self.rep_type(info.ret_rep)
        joined = ", ".join(call_args)

        if (is_tail and self.use_musttail and not borrow_temps
                and info.ret_rep == self.ctx.ret_rep):
            self.emit_cleanup_all()
            # LLVM's ``musttail`` requires matching argument counts, which our
            # explicit-capture ABI does not always have (e.g. a closure calling
            # a neighbouring loop with more captures).  ``tail`` is still
            # enough for LLVM to emit a jump; use the stronger marker when the
            # signatures line up so self-recursion is guaranteed.
            nparams = len(info.captures) + info.arity
            callee_reps = info.capture_reps + info.param_reps
            same_sig = (nparams == self.ctx.nparams
                        and callee_reps == self.ctx.param_reps)
            marker = "musttail" if same_sig else "tail"
            res = self.new_reg("mt")
            self.emit_inst(f"{res} = {marker} call {ret_ty} {info.direct_name}({joined})")
            self.emit_inst(f"ret {ret_ty} {res}")
            self.terminated = True
            return None

        res = self.new_reg("res")
        self.emit_inst(f"{res} = call {ret_ty} {info.direct_name}({joined})")
        for tmp in borrow_temps:
            self.release(tmp)
        val = Val(info.ret_rep, res, owned=(info.ret_rep == BOX))
        target = BOX if self.dynamic else rep_of(result_type)
        val = self.coerce(val, target, result_type)
        if is_tail:
            self.emit_return(val, result_type)
            return None
        return val

    def apply_closure_chain(self, fn: Val, args: List[Node], result_type,
                            is_tail: bool) -> Optional[Val]:
        cur = fn
        for arg in args:
            if cur.rep != BOX:
                cur = self.coerce(cur, BOX, None)
            argval = self.compile(arg, is_tail=False)
            if argval is None:
                return None
            argval = self.coerce(argval, BOX, arg.type)
            argval = self.ensure_owned(argval)
            self.emit_inst(f"call void @lambda_check_function(%Value* {cur.name})")
            code_p = self.new_reg()
            self.emit_inst(f"{code_p} = getelementptr %Value, %Value* {cur.name}, i32 0, i32 3")
            code = self.new_reg()
            self.emit_inst(f"{code} = load i8*, i8** {code_p}")
            env_p = self.new_reg()
            self.emit_inst(f"{env_p} = getelementptr %Value, %Value* {cur.name}, i32 0, i32 2")
            env_i = self.new_reg()
            self.emit_inst(f"{env_i} = load i64, i64* {env_p}")
            env = self.new_reg()
            self.emit_inst(f"{env} = inttoptr i64 {env_i} to i8*")
            res = self.new_reg("res")
            self.emit_inst(f"{res} = call %Value* {code}(i8* {env}, %Value* {argval.name})")
            self.release(cur)
            cur = Val(BOX, res, owned=True)
        target = BOX if self.dynamic else rep_of(result_type)
        cur = self.coerce(cur, target, result_type)
        if is_tail:
            self.emit_return(cur, result_type)
            return None
        return cur

    # ------------------------------------------------------------------
    # primitives
    # ------------------------------------------------------------------
    def compile_prim_app(self, b: Binding, args: List[Node], node: App,
                         is_tail: bool) -> Optional[Val]:
        op = OP_CANON.get(b.name)
        if op is None:
            raise CompileError(f"unknown primitive '{b.name}'", b.loc)
        arity = PRIM_ARITY.get(op, 0)
        if op == "nil" and args:
            raise CompileError("nil is not a function", node.loc)
        if op in ("read_line", "read_int", "read_float") and not self.allow_input:
            raise CompileError(
                "terminal input is disabled; recompile with --input", node.loc)
        if op in ("write_bytes", "write_f64s", "write_i64s"):
            if not self.allow_data:
                raise CompileError(
                    "binary output is disabled; recompile with --data", node.loc)
            if len(args) != 1:
                raise CompileError(
                    f"'{op}' takes exactly one array argument", node.loc)
            a = self._array_borrow(args[0])
            fn = {
                "write_bytes": "@lambda_op_write_bytes",
                "write_f64s": "@lambda_op_write_f64s",
                "write_i64s": "@lambda_op_write_i64s",
            }[op]
            res = self.new_reg()
            self.emit_inst(f"{res} = call i64 {fn}(%Value* {a.name})")
            self.release(a)
            return Val(I64, res)
        if op == "pmap":
            if len(args) < 2:
                raise CompileError(
                    "pmap must be applied to a provably pure function and a list",
                    node.loc)
            self.check_pmap_function(args[0], node.loc)
        if op == "arrayf_pmap":
            if len(args) < 2:
                raise CompileError(
                    "arrayf-pmap must be applied to a provably pure Int -> Float "
                    "function and a length", node.loc)
            self.check_pmap_function(args[0], node.loc)
        if op == "arrayi_pmap":
            if len(args) < 2:
                raise CompileError(
                    "arrayi-pmap must be applied to a provably pure Int -> Int "
                    "function and a length", node.loc)
            self.check_pmap_function(args[0], node.loc)
        if op == "arrayi_pmap_chunk":
            if len(args) < 3:
                raise CompileError(
                    "arrayi-pmap-chunk must be applied to a provably pure "
                    "Int -> Int function, a length and a chunk size", node.loc)
            self.check_pmap_function(args[0], node.loc)
        if self.dynamic:
            if len(args) == arity and arity > 0:
                val = self.emit_boxed_primitive(op, args, node)
                if is_tail and val is not None:
                    self.emit_return(val, node.type)
                    return None
                return val
            clo = self.primitive_global(b)
            return self.apply_closure_chain(clo, args, node.type, is_tail)

        if len(args) == arity:
            val = self.emit_typed_primitive(op, args, node)
            if val is None:
                return None
            if is_tail:
                self.emit_return(val, node.type)
                return None
            return val
        clo = self.primitive_global(b)
        return self.apply_closure_chain(clo, args, node.type, is_tail)

    def check_pmap_function(self, arg: Node, loc) -> None:
        ok = False
        if isinstance(arg, Lam):
            ok = self.lambda_purity.get(id(arg), False)
        elif isinstance(arg, Var) and arg.binding is not None:
            b = arg.binding
            if b.is_function and b.lam is not None:
                ok = self.lambda_purity.get(id(b.lam), False)
            elif b.kind == PRIM:
                op = OP_CANON.get(b.name)
                ok = (op is not None and op not in IMPURE_PRIMITIVES
                      and op not in ("map", "pmap"))
        if not ok:
            raise CompileError(
                "pmap requires a provably pure function: no printing, terminal "
                "input, or calls through unknown function values", loc)

    def _prim_arg(self, node: Node, rep: str, hint) -> Val:
        val = self.compile(node, is_tail=False)
        if val is None:
            raise CompileError("primitive argument did not produce a value", node.loc)
        val = self.coerce(val, rep, hint if hint is not None else node.type)
        return self.ensure_owned(val) if rep == BOX else val

    def _move_tail_capture(self, b: Binding) -> Optional[Val]:
        """Transfer a boxed capture into a tail call instead of retaining it."""
        st = self.find_storage(b.uid)
        if st is None or st.rep != BOX or st.is_global:
            return None
        if not (st.name.startswith("%cap") or st.name.startswith("%par")):
            return None
        if b.uid in self.moved_uids:
            return None
        self.moved_uids.add(b.uid)
        for cleanup in self.cleanup_stack:
            cleanup[:] = [(name, immortal) for name, immortal in cleanup
                          if name != st.name]
        return Val(BOX, st.name, owned=True)

    def _borrow_value_for_call(self, node: Node) -> Optional[Val]:
        """Pass a plain local/global variable to a borrowed parameter without
        a retain/release round trip.  The callee cannot store or return the
        borrow, so the storage outlives the call."""
        if not isinstance(node, Var) or node.binding is None:
            return None
        b = node.binding
        if b.is_function or b.kind == PRIM:
            return None
        st = self.find_storage(b.uid)
        if st is None or st.rep != BOX:
            return None
        if not st.is_global and b.uid in self.moved_uids:
            raise CompileError(
                f"'{b.name}' was consumed by a destructive array update and "
                f"cannot be used again", node.loc)
        if st.is_global:
            res = self.new_reg()
            self.emit_inst(f"{res} = load %Value*, %Value** {st.name}")
            return Val(BOX, res, owned=False)
        return Val(BOX, st.name, owned=False, borrowed=True)

    def _array_borrow(self, node: Node) -> Val:
        """Read an array argument without a retain when it is a simple value.

        The primitive returns immediately, so a local/global pointer remains
        valid for the duration of the call.  Non-variable expressions still
        produce an owned value and are released by the caller.
        """

        if isinstance(node, Var) and node.binding is not None:
            b = node.binding
            if not b.is_function and b.kind != PRIM:
                st = self.find_storage(b.uid)
                if st is not None and st.rep == BOX:
                    if not st.is_global and b.uid in self.moved_uids:
                        raise CompileError(
                            f"'{b.name}' was consumed by a destructive array "
                            f"update and cannot be used again", node.loc)
                    if st.is_global:
                        res = self.new_reg()
                        self.emit_inst(
                            f"{res} = load %Value*, %Value** {st.name}")
                        return Val(BOX, res, owned=False)
                    return Val(BOX, st.name, owned=False)
        if isinstance(node, Borrow):
            return self.compile_borrow(node)
        val = self.compile(node, is_tail=False)
        if val is None:
            raise CompileError("array argument did not produce a value", node.loc)
        val = self.coerce(val, BOX, node.type)
        return self.ensure_owned(val)

    def _move_array_param(self, node: Node) -> Optional[Val]:
        """Move a non-captured array parameter into a consuming update.

        This removes the retain/release pair around the update and suppresses
        the parameter's cleanup.  Any later use is rejected by compile_var, so
        the linear convention is enforced within the function.
        """

        if not self.moves_allowed:
            return None
        if not isinstance(node, Var) or node.binding is None:
            return None
        b = node.binding
        if b.kind != PARAM or b.uid in self.captured_uids or b.borrowed:
            return None
        st = self.find_storage(b.uid)
        if st is None or st.rep != BOX or st.is_global:
            return None
        if not st.name.startswith("%par"):
            return None
        if b.uid in self.moved_uids:
            raise CompileError(
                f"'{b.name}' was already consumed by a destructive array update",
                node.loc)
        self.moved_uids.add(b.uid)
        for cleanup in self.cleanup_stack:
            cleanup[:] = [(name, immortal) for name, immortal in cleanup
                          if name != st.name]
        return Val(BOX, st.name, owned=True)

    def _array_bounds(self, arr: Val, idx: Val) -> None:
        """Emit a checked bounds branch; types already guarantee the tag."""
        meta = self.array_meta.get(arr.name)
        if meta is not None:
            length = meta[0]
        else:
            lenp = self.new_reg()
            self.emit_inst(
                f"{lenp} = getelementptr %Value, %Value* {arr.name}, i32 0, i32 2")
            length = self.new_reg()
            self.emit_inst(f"{length} = load i64, i64* {lenp}, !invariant.load !0")
        ok = self.new_reg()
        self.emit_inst(f"{ok} = icmp ult i64 {idx.name}, {length}")
        good = self.new_label("arr_ok")
        bad = self.new_label("arr_bad")
        self.emit_inst(f"br i1 {ok}, label %{good}, label %{bad}")
        self.builder.label(bad)
        self.emit_inst("call void @lambda_array_bounds_fail()")
        self.emit_inst("unreachable")
        self.builder.label(good)

    def _array_element_ptr(self, arr: Val, idx: Val, llty: str) -> str:
        meta = self.array_meta.get(arr.name)
        if meta is not None:
            raw = meta[1]
        else:
            pp = self.new_reg()
            self.emit_inst(
                f"{pp} = getelementptr %Value, %Value* {arr.name}, i32 0, i32 3")
            raw = self.new_reg()
            self.emit_inst(f"{raw} = load i8*, i8** {pp}, !invariant.load !0")
        data = self.new_reg()
        self.emit_inst(f"{data} = bitcast i8* {raw} to {llty}*")
        ep = self.new_reg()
        self.emit_inst(
            f"{ep} = getelementptr {llty}, {llty}* {data}, i64 {idx.name}")
        return ep

    # ------------------------------------------------------------------
    # GPU source generation for arrayf-pmap
    # ------------------------------------------------------------------
    def _gpu_lambda(self, node: Node) -> Optional[Lam]:
        if isinstance(node, Lam):
            return node
        if isinstance(node, Var) and node.binding is not None:
            b = node.binding
            if b.lam is not None and len(b.lam.params) == 1:
                return b.lam
        return None

    def _gpu_lit(self, v) -> Optional[str]:
        if isinstance(v, bool):
            return "1.0" if v else "0.0"
        if isinstance(v, int):
            return repr(float(v))
        if isinstance(v, float):
            return repr(v)
        return None

    def _gpu_index(self, node: Node, depth: int = 0) -> Optional[str]:
        """Integer expression for an ArrayF index in a GPU kernel."""
        if depth > 12:
            return None
        if isinstance(node, Lit) and isinstance(node.value, int):
            return str(node.value)
        if isinstance(node, Var):
            if node.name == getattr(self, "_gpu_i_name", None):
                return "((long long)i)"
            if node.name == getattr(self, "_gpu_n_name", None):
                return "n"
            b = node.binding
            if b is not None and b.def_node is not None and isinstance(b.def_node.value, Lit):
                return str(int(b.def_node.value.value))
            return None
        if isinstance(node, App):
            head, args = self.flatten_app(node)
            if not isinstance(head, Var) or head.binding is None:
                return None
            op = OP_CANON.get(head.binding.name)
            if op in ("add", "sub", "mul", "div", "mod") and len(args) == 2:
                a = self._gpu_index(args[0], depth + 1)
                b = self._gpu_index(args[1], depth + 1)
                if a is None or b is None:
                    return None
                return f"(({a}) { {'add': '+', 'sub': '-', 'mul': '*', 'div': '/', 'mod': '%'}[op] } ({b}))"
            if op == "int_to_float" and len(args) == 1:
                return self._gpu_index(args[0], depth + 1)
            return None
        if isinstance(node, If):
            c = self._gpu_expr(node.cond, {}, depth + 1)
            a = self._gpu_index(node.then, depth + 1)
            b = self._gpu_index(node.else_, depth + 1)
            if c is None or a is None or b is None:
                return None
            return f"(({c}) ? ({a}) : ({b}))"
        return None

    def _gpu_expr(self, node: Node, env: Dict[str, str], depth: int = 0) -> Optional[str]:
        if depth > 12:
            return None
        if isinstance(node, Lit):
            return self._gpu_lit(node.value)
        if isinstance(node, Var):
            if node.name in env:
                return env[node.name]
            if node.name == getattr(self, "_gpu_n_name", None):
                return "((double)n)"
            b = node.binding
            if b is None:
                return None
            if b.kind == PRIM:
                if b.name == "pi":
                    return "3.14159265358979323846"
                if b.name == "e":
                    return "2.71828182845904523536"
                return None
            if b.def_node is not None and isinstance(b.def_node.value, Lit):
                return self._gpu_lit(b.def_node.value.value)
            return None
        if isinstance(node, Block):
            local = dict(env)
            for d in node.defs:
                e = self._gpu_expr(d.value, local, depth + 1)
                if e is None:
                    return None
                local[d.name] = e
            return self._gpu_expr(node.body, local, depth + 1)
        if isinstance(node, If):
            c = self._gpu_expr(node.cond, env, depth + 1)
            a = self._gpu_expr(node.then, env, depth + 1)
            b = self._gpu_expr(node.else_, env, depth + 1)
            if c is None or a is None or b is None:
                return None
            return f"(({c}) ? ({a}) : ({b}))"
        if isinstance(node, App):
            head, args = self.flatten_app(node)
            if not isinstance(head, Var) or head.binding is None:
                return None
            b = head.binding
            op = OP_CANON.get(b.name)
            arr = getattr(self, "_gpu_arr_binding", None)
            if op == "arrayf_get" and arr is not None and len(args) == 2:
                if isinstance(args[0], Var) and args[0].binding is arr:
                    idx = self._gpu_index(args[1])
                    return f"(((long long)({idx}) >= 0 && (long long)({idx}) < n) ? in0[(long long)({idx})] : 0.0)" if idx is not None else None
            if op == "arrayf_length" and arr is not None and len(args) == 1:
                if isinstance(args[0], Var) and args[0].binding is arr:
                    return "((double)n)"
            unary = {
                "sin": "sin", "cos": "cos", "tan": "tan", "asin": "asin",
                "acos": "acos", "atan": "atan", "exp": "exp", "ln": "log",
                "log10": "log10", "log2": "log2", "sqrt": "sqrt",
                "floor": "floor", "ceil": "ceil", "round": "round",
                "trunc": "trunc", "abs": "fabs",
            }
            binary = {
                "add": "+", "sub": "-", "mul": "*", "div": "/",
                "pow": "pow", "atan2": "atan2", "mod": "fmod",
            }
            if op in unary and len(args) == 1:
                a = self._gpu_expr(args[0], env, depth + 1)
                return f"{unary[op]}({a})" if a is not None else None
            if op in binary and len(args) == 2:
                a = self._gpu_expr(args[0], env, depth + 1)
                c = self._gpu_expr(args[1], env, depth + 1)
                if a is None or c is None:
                    return None
                return f"({a} {binary[op]} {c})"
            if op in ("lt", "le", "gt", "ge", "eq") and len(args) == 2:
                a = self._gpu_expr(args[0], env, depth + 1)
                c = self._gpu_expr(args[1], env, depth + 1)
                if a is None or c is None:
                    return None
                sym = {"lt": "<", "le": "<=", "gt": ">", "ge": ">=", "eq": "=="}[op]
                return f"(({a}) {sym} ({c}))"
            if op == "int_to_float" and len(args) == 1:
                a = self._gpu_expr(args[0], env, depth + 1)
                return f"((double)({a}))" if a is not None else None
            if b.lam is not None and len(args) == len(b.lam.params):
                vals = [self._gpu_expr(a, env, depth + 1) for a in args]
                if any(v is None for v in vals):
                    return None
                inner = dict(zip(b.lam.params, vals))
                return self._gpu_expr(b.lam.body, inner, depth + 1)
            return None
        return None

    def _gpu_pmap_source(self, node: Node):
        lam = self._gpu_lambda(node)
        if lam is None or len(lam.params) != 1:
            return None
        arr_cap = None
        n_cap = None
        bindings = list(getattr(lam, "captures", []) or [])
        bindings += [n.binding for n in walk(lam.body)
                     if isinstance(n, Var) and n.binding is not None]
        for b in bindings:
            t = prune(b.type) if b.type is not None else None
            if isinstance(t, TCon) and t.name == "ArrayF":
                if arr_cap is not None and arr_cap.uid != b.uid:
                    return None
                arr_cap = b
            elif isinstance(t, TCon) and t.name == "Int" and b.name == "n":
                n_cap = b
        self._gpu_i_name = lam.params[0]
        self._gpu_n_name = n_cap.name if n_cap is not None else None
        self._gpu_arr_binding = arr_cap
        try:
            body = self._gpu_expr(lam.body, {lam.params[0]: "((double)i)"})
        finally:
            self._gpu_arr_binding = None
        if body is None:
            return None
        if arr_cap is not None:
            return ("extern \"C\" __global__ void pmap(double* out, const double* in0, long long n) {"
                    " long long i = (long long)blockIdx.x * blockDim.x + threadIdx.x;"
                    f" if (i < n) out[i] = (double)({body}); }}", arr_cap)
        return ("extern \"C\" __global__ void pmap(double* out, long long n) {"
                " long long i = (long long)blockIdx.x * blockDim.x + threadIdx.x;"
                f" if (i < n) out[i] = (double)({body}); }}", None)

    def _gpu_source_global(self, source: str) -> str:
        gid = self.gpu_ids.get(source)
        if gid is not None:
            return gid
        idx = len(self.gpu_ids)
        gid = f"@gpu_src_{idx}"
        data = source.encode("utf-8") + b"\0"
        esc = "".join(f"\\{b:02x}" for b in data)
        self.gpu_defs.append(
            f"{gid} = private unnamed_addr constant [{len(data)} x i8] c\"{esc}\"")
        self.gpu_ids[source] = gid
        return gid

    def emit_typed_primitive(self, op: str, args: List[Node], node: App) -> Optional[Val]:
        if op in ("add", "sub", "mul", "div", "mod"):
            rep = self.node_rep(args[0])
            a = self._prim_arg(args[0], rep, args[0].type)
            b = self._prim_arg(args[1], rep, args[1].type)
            res = self.new_reg()
            if rep == F64:
                inst = {"add": "fadd", "sub": "fsub", "mul": "fmul",
                        "div": "fdiv", "mod": "frem"}[op]
                self.emit_inst(f"{res} = {inst} double {a.name}, {b.name}")
                return Val(F64, res)
            if op in ("add", "sub", "mul"):
                self.emit_inst(f"{res} = {op} i64 {a.name}, {b.name}")
                return Val(I64, res)
            # A literal non-zero divisor (other than the INT64_MIN/-1 edge)
            # can use the native instruction; dynamic divisors keep the
            # checked helper so division by zero remains a clean error.
            literal = args[1]
            lit_value = getattr(literal, "value", None)
            if (isinstance(literal, Lit) and isinstance(lit_value, int)
                    and lit_value != 0 and lit_value != -1):
                inst = "sdiv" if op == "div" else "srem"
                self.emit_inst(f"{res} = {inst} i64 {a.name}, {b.name}")
                return Val(I64, res)
            fn = "lambda_div_i64" if op == "div" else "lambda_mod_i64"
            self.emit_inst(f"{res} = call i64 @{fn}(i64 {a.name}, i64 {b.name})")
            return Val(I64, res)
        if op in ("lt", "le", "gt", "ge"):
            rep = self.node_rep(args[0])
            a = self._prim_arg(args[0], rep, args[0].type)
            b = self._prim_arg(args[1], rep, args[1].type)
            t = self.new_reg()
            if rep == F64:
                cmp = {"lt": "olt", "le": "ole", "gt": "ogt", "ge": "oge"}[op]
                self.emit_inst(f"{t} = fcmp {cmp} double {a.name}, {b.name}")
            else:
                cmp = CMP_OP[op]
                self.emit_inst(f"{t} = icmp {cmp} i64 {a.name}, {b.name}")
            res = self.new_reg()
            self.emit_inst(f"{res} = zext i1 {t} to i64")
            return Val(I64, res)
        if op == "eq":
            rep = self.node_rep(args[0])
            a = self._prim_arg(args[0], rep, args[0].type)
            b = self._prim_arg(args[1], rep, args[1].type)
            if a.rep == I64 and b.rep == I64:
                t = self.new_reg()
                self.emit_inst(f"{t} = icmp eq i64 {a.name}, {b.name}")
                res = self.new_reg()
                self.emit_inst(f"{res} = zext i1 {t} to i64")
                return Val(I64, res)
            if a.rep == F64 and b.rep == F64:
                t = self.new_reg()
                self.emit_inst(f"{t} = fcmp oeq double {a.name}, {b.name}")
                res = self.new_reg()
                self.emit_inst(f"{res} = zext i1 {t} to i64")
                return Val(I64, res)
            a = self.coerce(a, BOX, args[0].type)
            b = self.coerce(b, BOX, args[1].type)
            res = self.new_reg()
            self.emit_inst(f"{res} = call %Value* @lambda_op_eq(%Value* {a.name}, %Value* {b.name})")
            return self.coerce(Val(BOX, res, owned=True), I64, node.type)
        if op == "not":
            a = self._prim_arg(args[0], I64, args[0].type)
            res = self.new_reg()
            self.emit_inst(f"{res} = xor i64 {a.name}, 1")
            return Val(I64, res)
        if op in ("and", "or"):
            # Evaluate both operands; with short-circuit operands that already
            # contain `if`, the 0/1 of the first is exactly the identity for
            # the second, so this equals the short-circuit result.
            a = self._prim_arg(args[0], I64, args[0].type)
            b = self._prim_arg(args[1], I64, args[1].type)
            res = self.new_reg()
            inst = "and" if op == "and" else "or"
            self.emit_inst(f"{res} = {inst} i64 {a.name}, {b.name}")
            return Val(I64, res)
        if op == "cons":
            head = self._prim_arg(args[0], self.node_rep(args[0]), args[0].type)
            head = self.coerce(head, BOX, args[0].type)
            tail = self._prim_arg(args[1], BOX, args[1].type)
            res = self.new_reg()
            self.emit_inst(f"{res} = call %Value* @lambda_cons(%Value* {head.name}, %Value* {tail.name})")
            return Val(BOX, res, owned=True)
        if op == "head":
            lst = self._prim_arg(args[0], BOX, args[0].type)
            res = self.new_reg()
            self.emit_inst(f"{res} = call %Value* @lambda_op_head(%Value* {lst.name})")
            return self.coerce(Val(BOX, res, owned=True), self.node_rep(node), node.type)
        if op == "tail":
            lst = self._prim_arg(args[0], BOX, args[0].type)
            res = self.new_reg()
            self.emit_inst(f"{res} = call %Value* @lambda_op_tail(%Value* {lst.name})")
            return Val(BOX, res, owned=True)
        if op == "empty":
            lst = self._prim_arg(args[0], BOX, args[0].type)
            tag_p = self.new_reg()
            self.emit_inst(f"{tag_p} = getelementptr %Value, %Value* {lst.name}, i32 0, i32 0")
            tag = self.new_reg()
            self.emit_inst(f"{tag} = load i32, i32* {tag_p}")
            is_nil = self.new_reg()
            self.emit_inst(f"{is_nil} = icmp eq i32 {tag}, 5")
            res = self.new_reg()
            self.emit_inst(f"{res} = zext i1 {is_nil} to i64")
            self.release(lst)
            return Val(I64, res)
        if op == "nil":
            return self.nil_value()
        if op == "int_to_str":
            a = self._prim_arg(args[0], I64, args[0].type)
            boxed = self.box(a, args[0].type)
            res = self.new_reg()
            self.emit_inst(f"{res} = call %Value* @lambda_op_int_to_str(%Value* {boxed.name})")
            return Val(BOX, res, owned=True)
        if op == "str_to_int":
            a = self._prim_arg(args[0], BOX, args[0].type)
            res = self.new_reg()
            self.emit_inst(f"{res} = call %Value* @lambda_op_str_to_int(%Value* {a.name})")
            return self.coerce(Val(BOX, res, owned=True), I64, node.type)
        if op == "str_to_list":
            a = self._prim_arg(args[0], BOX, args[0].type)
            res = self.new_reg()
            self.emit_inst(f"{res} = call %Value* @lambda_op_str_to_list(%Value* {a.name})")
            return Val(BOX, res, owned=True)
        if op == "str_len":
            a = self._prim_arg(args[0], BOX, args[0].type)
            res = self.new_reg()
            self.emit_inst(f"{res} = call %Value* @lambda_op_str_len(%Value* {a.name})")
            return self.coerce(Val(BOX, res, owned=True), I64, node.type)
        if op == "str_append":
            a = self._prim_arg(args[0], BOX, args[0].type)
            b = self._prim_arg(args[1], BOX, args[1].type)
            res = self.new_reg()
            self.emit_inst(f"{res} = call %Value* @lambda_op_str_append(%Value* {a.name}, %Value* {b.name})")
            return Val(BOX, res, owned=True)
        if op == "read_file":
            a = self._prim_arg(args[0], BOX, args[0].type)
            res = self.new_reg()
            self.emit_inst(f"{res} = call %Value* @lambda_op_read_file(%Value* {a.name})")
            return Val(BOX, res, owned=True)
        if op == "write_file":
            path = self._prim_arg(args[0], BOX, args[0].type)
            data = self._prim_arg(args[1], BOX, args[1].type)
            res = self.new_reg()
            self.emit_inst(
                f"{res} = call i64 @lambda_op_write_file(%Value* {path.name}, %Value* {data.name})")
            return Val(I64, res)
        if op == "read_line":
            a = self._prim_arg(args[0], BOX, args[0].type)
            res = self.new_reg()
            self.emit_inst(f"{res} = call %Value* @lambda_op_read_line(%Value* {a.name})")
            return Val(BOX, res, owned=True)
        if op == "read_int":
            a = self._prim_arg(args[0], BOX, args[0].type)
            res = self.new_reg()
            self.emit_inst(f"{res} = call %Value* @lambda_op_read_int(%Value* {a.name})")
            return self.coerce(Val(BOX, res, owned=True), I64, node.type)
        if op == "read_float":
            a = self._prim_arg(args[0], BOX, args[0].type)
            res = self.new_reg()
            self.emit_inst(f"{res} = call %Value* @lambda_op_read_float(%Value* {a.name})")
            return self.coerce(Val(BOX, res, owned=True), F64, node.type)
        if op in ("print", "println"):
            a = self.compile(args[0], is_tail=False)
            if a is None:
                return None
            a = self.coerce(a, self.node_rep(args[0]), args[0].type)
            boxed = self.coerce(a, BOX, args[0].type)
            fn = "@lambda_op_print" if op == "print" else "@lambda_op_println"
            res = self.new_reg()
            self.emit_inst(f"{res} = call %Value* {fn}(%Value* {boxed.name})")
            return self.coerce(Val(BOX, res, owned=True), self.node_rep(node), node.type)
        if op == "fix":
            a = self._prim_arg(args[0], BOX, args[0].type)
            res = self.new_reg()
            self.emit_inst(f"{res} = call %Value* @lambda_fix(%Value* {a.name})")
            return Val(BOX, res, owned=True)
        if op == "abs":
            rep = self.node_rep(args[0])
            a = self._prim_arg(args[0], rep, args[0].type)
            res = self.new_reg()
            if rep == F64:
                self.emit_inst(f"{res} = call double @llvm.fabs.f64(double {a.name})")
                return Val(F64, res)
            is_neg = self.new_reg()
            self.emit_inst(f"{is_neg} = icmp slt i64 {a.name}, 0")
            neg = self.new_reg()
            self.emit_inst(f"{neg} = sub i64 0, {a.name}")
            self.emit_inst(f"{res} = select i1 {is_neg}, i64 {neg}, i64 {a.name}")
            return Val(I64, res)
        if op == "int_to_float":
            a = self._prim_arg(args[0], I64, args[0].type)
            res = self.new_reg()
            self.emit_inst(f"{res} = sitofp i64 {a.name} to double")
            return Val(F64, res)
        if op == "float_to_int":
            a = self._prim_arg(args[0], F64, args[0].type)
            res = self.new_reg()
            self.emit_inst(f"{res} = call i64 @lambda_ftoi(double {a.name})")
            return Val(I64, res)
        if op == "str_to_float":
            a = self._prim_arg(args[0], BOX, args[0].type)
            res = self.new_reg()
            self.emit_inst(f"{res} = call %Value* @lambda_op_str_to_float(%Value* {a.name})")
            return self.coerce(Val(BOX, res, owned=True), F64, node.type)
        if op == "float_to_str":
            a = self._prim_arg(args[0], F64, args[0].type)
            boxed = self.box(a, args[0].type)
            res = self.new_reg()
            self.emit_inst(f"{res} = call %Value* @lambda_op_float_to_str(%Value* {boxed.name})")
            return Val(BOX, res, owned=True)
        if op in ("sqrt", "sin", "cos", "tan", "asin", "acos", "atan",
                  "exp", "ln", "log10", "log2", "floor", "ceil", "round", "trunc"):
            a = self._prim_arg(args[0], F64, args[0].type)
            intrinsic = {
                "sqrt": "@llvm.sqrt.f64", "floor": "@llvm.floor.f64",
                "ceil": "@llvm.ceil.f64", "round": "@llvm.round.f64",
                "trunc": "@llvm.trunc.f64",
            }.get(op)
            if intrinsic:
                res = self.new_reg()
                self.emit_inst(f"{res} = call double {intrinsic}(double {a.name})")
            else:
                cname = {"ln": "log", "log10": "log10", "log2": "log2"}.get(op, op)
                res = self.new_reg()
                self.emit_inst(f"{res} = call double @{cname}(double {a.name})")
            return Val(F64, res)
        if op in ("pow", "atan2"):
            a = self._prim_arg(args[0], F64, args[0].type)
            b = self._prim_arg(args[1], F64, args[1].type)
            fn = "@llvm.pow.f64" if op == "pow" else "@atan2"
            res = self.new_reg()
            self.emit_inst(f"{res} = call double {fn}(double {a.name}, double {b.name})")
            return Val(F64, res)
        if op == "pi":
            return Val(F64, float_const(math.pi))
        if op == "e":
            return Val(F64, float_const(math.e))
        if op in ("bit_and", "bit_or", "bit_xor", "shl", "shr"):
            a = self._prim_arg(args[0], I64, args[0].type)
            b = self._prim_arg(args[1], I64, args[1].type)
            inst = {"bit_and": "and", "bit_or": "or", "bit_xor": "xor",
                    "shl": "shl", "shr": "lshr"}[op]
            res = self.new_reg()
            if op in ("shl", "shr"):
                shift = self.new_reg()
                self.emit_inst(f"{shift} = and i64 {b.name}, 63")
                self.emit_inst(f"{res} = {inst} i64 {a.name}, {shift}")
            else:
                self.emit_inst(f"{res} = {inst} i64 {a.name}, {b.name}")
            return Val(I64, res)
        if op == "bit_not":
            a = self._prim_arg(args[0], I64, args[0].type)
            res = self.new_reg()
            self.emit_inst(f"{res} = xor i64 {a.name}, -1")
            return Val(I64, res)
        if op == "popcount":
            a = self._prim_arg(args[0], I64, args[0].type)
            res = self.new_reg()
            self.emit_inst(f"{res} = call i64 @llvm.ctpop.i64(i64 {a.name})")
            return Val(I64, res)
        if op in ("sum", "product"):
            lst = self._prim_arg(args[0], BOX, args[0].type)
            target = self.node_rep(node)
            raw = self.new_reg()
            if target == F64:
                fn = "@lambda_sum_f64" if op == "sum" else "@lambda_product_f64"
                self.emit_inst(f"{raw} = call double {fn}(%Value* {lst.name})")
                return Val(F64, raw)
            fn = "@lambda_sum_i64" if op == "sum" else "@lambda_product_i64"
            self.emit_inst(f"{raw} = call i64 {fn}(%Value* {lst.name})")
            return Val(I64, raw)
        if op == "range":
            lo = self._prim_arg(args[0], I64, args[0].type)
            hi = self._prim_arg(args[1], I64, args[1].type)
            res = self.new_reg()
            self.emit_inst(f"{res} = call %Value* @lambda_range(i64 {lo.name}, i64 {hi.name})")
            return Val(BOX, res, owned=True)
        if op in ("map", "pmap"):
            f = self._prim_arg(args[0], BOX, args[0].type)
            lst = self._prim_arg(args[1], BOX, args[1].type)
            fn = "@lambda_op_map" if op == "map" else "@lambda_op_pmap"
            res = self.new_reg()
            self.emit_inst(f"{res} = call %Value* {fn}(%Value* {f.name}, %Value* {lst.name})")
            return Val(BOX, res, owned=True)
        if op == "immortalize":
            a = self.compile(args[0], is_tail=False)
            if a is None:
                return None
            a = self.coerce(a, BOX, args[0].type)
            res = self.new_reg()
            self.emit_inst(f"{res} = call %Value* @lambda_op_immortalize(%Value* {a.name})")
            return self.coerce(Val(BOX, res, owned=True), self.node_rep(node), node.type)
        if op == "arrayi_pmap":
            f = self._prim_arg(args[0], BOX, args[0].type)
            n = self._prim_arg(args[1], I64, args[1].type)
            res = self.new_reg()
            self.emit_inst(
                f"{res} = call %Value* @lambda_arrayi_pmap(%Value* {f.name}, i64 {n.name})")
            return Val(BOX, res, owned=True)
        if op == "arrayf_pmap":
            f = self._prim_arg(args[0], BOX, args[0].type)
            n = self._prim_arg(args[1], I64, args[1].type)
            gpu = self._gpu_pmap_source(args[0])
            res = self.new_reg()
            if gpu is not None:
                source, cap = gpu
                gid = self._gpu_source_global(source)
                if cap is not None:
                    capvar = Var(cap.name, node.loc)
                    capvar.binding = cap
                    capvar.type = cap.type
                    av = self._array_borrow(capvar)
                    self.emit_inst(
                        f"{res} = call %Value* @lambda_gpu_arrayf_pmap1("
                        f"%Value* {f.name}, i64 {n.name}, i8* {gid}, %Value* {av.name})")
                else:
                    self.emit_inst(
                        f"{res} = call %Value* @lambda_gpu_arrayf_pmap("
                        f"%Value* {f.name}, i64 {n.name}, i8* {gid})")
            else:
                self.emit_inst(
                    f"{res} = call %Value* @lambda_arrayf_pmap(%Value* {f.name}, i64 {n.name})")
            return Val(BOX, res, owned=True)
        if op == "arrayi_pmap_chunk":
            f = self._prim_arg(args[0], BOX, args[0].type)
            n = self._prim_arg(args[1], I64, args[1].type)
            chunk = self._prim_arg(args[2], I64, args[2].type)
            res = self.new_reg()
            self.emit_inst(
                f"{res} = call %Value* @lambda_arrayi_pmap_chunk("
                f"%Value* {f.name}, i64 {n.name}, i64 {chunk.name})")
            return Val(BOX, res, owned=True)
        if op in ("arrayf_of", "arrayi_of", "arrayb_of"):
            lst = self._prim_arg(args[0], BOX, args[0].type)
            fn = {"arrayf_of": "@lambda_arrayf_of",
                  "arrayi_of": "@lambda_arrayi_of",
                  "arrayb_of": "@lambda_arrayb_of"}[op]
            res = self.new_reg()
            self.emit_inst(f"{res} = call %Value* {fn}(%Value* {lst.name})")
            return Val(BOX, res, owned=True)
        if op in ("arrayf", "arrayi", "arrayb"):
            n = self._prim_arg(args[0], I64, args[0].type)
            if op == "arrayf":
                init = self._prim_arg(args[1], F64, args[1].type)
                fn = "@lambda_arrayf_new"
                sig = f"i64 {n.name}, double {init.name}"
            else:
                init = self._prim_arg(args[1], I64, args[1].type)
                fn = "@lambda_arrayi_new" if op == "arrayi" else "@lambda_arrayb_new"
                sig = f"i64 {n.name}, i64 {init.name}"
            res = self.new_reg()
            self.emit_inst(f"{res} = call %Value* {fn}({sig})")
            return Val(BOX, res, owned=True)
        if op in ("arrayf_get", "arrayi_get", "arrayb_get",
                  "arrayf_get_ro", "arrayi_get_ro", "arrayb_get_ro"):
            a = self._array_borrow(args[0])
            idx = self._prim_arg(args[1], I64, args[1].type)
            if op.startswith("arrayf"):
                llty = "double"
            elif op.startswith("arrayi"):
                llty = "i64"
            else:
                llty = "i8"
            self._array_bounds(a, idx)
            ptr = self._array_element_ptr(a, idx, llty)
            raw = self.new_reg()
            if llty == "i8":
                byte = self.new_reg()
                self.emit_inst(f"{byte} = load i8, i8* {ptr}")
                self.emit_inst(f"{raw} = zext i8 {byte} to i64")
            else:
                self.emit_inst(f"{raw} = load {llty}, {llty}* {ptr}")
            self.release(a)
            return Val(F64 if llty == "double" else I64, raw)
        if op == "gray_stripe":
            vals = [self._prim_arg(args[i], BOX, args[i].type) for i in (0, 1, 2)]
            w = self._prim_arg(args[3], I64, args[3].type)
            r = self._prim_arg(args[4], I64, args[4].type)
            fl = [self._prim_arg(args[i], F64, args[i].type) for i in range(5, 10)]
            res = self.new_reg()
            self.emit_inst(
                f"{res} = call %Value* @lambda_gray_stripe(%Value* {vals[0].name}, "
                f"%Value* {vals[1].name}, %Value* {vals[2].name}, i64 {w.name}, i64 {r.name}, "
                f"double {fl[0].name}, double {fl[1].name}, double {fl[2].name}, "
                f"double {fl[3].name}, double {fl[4].name})")
            return Val(BOX, res, owned=True)
        if op == "gray_scott":
            a = self._move_array_param(args[0])
            if a is None:
                a = self._prim_arg(args[0], BOX, args[0].type)
            n = self._prim_arg(args[1], I64, args[1].type)
            vals = [self._prim_arg(x, F64, x.type) for x in args[2:7]]
            steps = self._prim_arg(args[7], I64, args[7].type)
            res = self.new_reg()
            self.emit_inst(
                f"{res} = call %Value* @lambda_gray_scott(%Value* {a.name}, i64 {n.name}, "
                f"double {vals[0].name}, double {vals[1].name}, double {vals[2].name}, "
                f"double {vals[3].name}, double {vals[4].name}, i64 {steps.name})")
            return Val(BOX, res, owned=True)
        if op in ("arrayf_set", "arrayi_set", "arrayb_set"):
            if (isinstance(args[0], Var) and args[0].binding is not None
                    and args[0].binding.borrowed):
                raise CompileError(
                    "cannot mutate a borrowed array parameter", args[0].loc)
            a = self._move_array_param(args[0])
            if a is None:
                a = self._prim_arg(args[0], BOX, args[0].type)
            idx = self._prim_arg(args[1], I64, args[1].type)
            base = op.replace("_set", "")
            vrep = F64 if base == "arrayf" else I64
            v = self._prim_arg(args[2], vrep, args[2].type)
            if base == "arrayb":
                # Keep the C call for the 0..255 range check.
                self.emit_inst(
                    f"call void @lambda_{base}_set(%Value* {a.name}, i64 {idx.name}, "
                    f"i64 {v.name})")
            else:
                llty = "double" if base == "arrayf" else "i64"
                self._array_bounds(a, idx)
                ptr = self._array_element_ptr(a, idx, llty)
                self.emit_inst(f"store {llty} {v.name}, {llty}* {ptr}")
            return Val(BOX, a.name, owned=True)
        if op in ("arrayi_sum", "arrayf_sum"):
            a = self._array_borrow(args[0])
            res = self.new_reg()
            if op == "arrayi_sum":
                self.emit_inst(f"{res} = call i64 @lambda_arrayi_sum(%Value* {a.name})")
                self.release(a)
                return Val(I64, res)
            self.emit_inst(f"{res} = call double @lambda_arrayf_sum(%Value* {a.name})")
            self.release(a)
            return Val(F64, res)
        if op in ("arrayf_length", "arrayi_length", "arrayb_length"):
            a = self._array_borrow(args[0])
            meta = self.array_meta.get(a.name)
            if meta is not None:
                res = meta[0]
            else:
                lenp = self.new_reg()
                self.emit_inst(
                    f"{lenp} = getelementptr %Value, %Value* {a.name}, i32 0, i32 2")
                res = self.new_reg()
                self.emit_inst(
                    f"{res} = load i64, i64* {lenp}, !invariant.load !0")
            self.release(a)
            return Val(I64, res)
        if op in ("arrayf_clone", "arrayi_clone", "arrayb_clone"):
            a = self._array_borrow(args[0])
            base = op.replace("_clone", "")
            res = self.new_reg()
            self.emit_inst(f"{res} = call %Value* @lambda_{base}_clone(%Value* {a.name})")
            self.release(a)
            return Val(BOX, res, owned=True)
        raise CompileError(f"primitive '{op}' is not implemented", node.loc)

    def emit_boxed_primitive(self, op: str, args: List[Node], node: App) -> Optional[Val]:
        if op == "nil":
            return self.nil_value()
        vals: List[Val] = []
        for a in args:
            v = self.compile(a, is_tail=False)
            if v is None:
                return None
            v = self.coerce(v, BOX, a.type)
            vals.append(self.ensure_owned(v))
        if op == "write_file":
            if len(vals) != 2:
                raise CompileError("write_file needs two arguments", node.loc)
            raw = self.new_reg()
            self.emit_inst(f"{raw} = call i64 @lambda_op_write_file(%Value* {vals[0].name}, %Value* {vals[1].name})")
            res = self.new_reg()
            self.emit_inst(f"{res} = call %Value* @lambda_box_int(i64 {raw})")
            return Val(BOX, res, owned=True)
        if op in OP_FN:
            fn = OP_FN[op]
            if len(vals) == 1:
                res = self.new_reg()
                self.emit_inst(f"{res} = call %Value* {fn}(%Value* {vals[0].name})")
            elif len(vals) == 2:
                res = self.new_reg()
                self.emit_inst(f"{res} = call %Value* {fn}(%Value* {vals[0].name}, "
                               f"%Value* {vals[1].name})")
            elif len(vals) == 3:
                res = self.new_reg()
                self.emit_inst(f"{res} = call %Value* {fn}(%Value* {vals[0].name}, "
                               f"%Value* {vals[1].name}, %Value* {vals[2].name})")
            else:
                raise CompileError(f"bad primitive arity for '{op}'", node.loc)
            return Val(BOX, res, owned=True)
        raise CompileError(f"primitive '{op}' is not implemented", node.loc)

    # ------------------------------------------------------------------
    # program entry point
    # ------------------------------------------------------------------
    def _gen_main(self, program: Block) -> None:
        self.builder = IRBuilder()
        self.ctx = FnContext(BOX, None)
        self.scope_stack = []
        self.cleanup_stack = []
        self.terminated = False
        self.moved_uids = set()
        self.moves_allowed = True
        self.array_meta = {}
        self.builder.raw("define i32 @main(i32 %argc, i8** %argv) {")
        self.builder.label("entry")
        self.emit_inst("call void @lambda_runtime_init()")
        for name, adt in sorted(self.adt_schemas.items()):
            if adt.layout != "arena" or name not in self.adt_arenas:
                continue
            width = max((len(fields) for _, _, fields in adt.variants), default=0)
            reg = self.new_reg()
            self.emit_inst(f"{reg} = call %AdtArena* @lambda_arena_new(i64 {width})")
            self.emit_inst(f"store %AdtArena* {reg}, %AdtArena** {self.adt_arenas[name]}")
            wp = self.new_reg()
            wslot = self.new_reg()
            self.emit_inst(f"{wslot} = getelementptr %AdtArena, %AdtArena* {reg}, i32 0, i32 3")
            self.emit_inst(f"{wp} = load i64*, i64** {wslot}")
            self.emit_inst(f"store i64* {wp}, i64** {self.adt_words[name]}")
        if self.allow_data:
            self.emit_inst("call void @lambda_enable_binary_stdout()")

        # Top-level function closures first, so functions can be used as values
        # from anywhere (including top-level value initialisers).
        for d in self.top_defs:
            if not d.is_function or d.binding is None:
                continue
            lam = d.value
            if not isinstance(lam, Lam):
                continue
            if lam.captures:
                raise CompileError(
                    f"top-level function '{d.name}' unexpectedly captures variables",
                    d.loc)
            if d.binding.uid not in self.value_functions:
                continue
            info = self.ensure_function(lam)
            reps = info.capture_reps + info.param_reps + [info.ret_rep]
            if (any(rep.startswith("REC:") for rep in reps)
                    or any(info.param_borrows)):
                continue
            clo = self.emit_make_closure(lam)
            gname = self.top_function_globals[d.binding.uid]
            self.emit_inst(f"store %Value* {clo.name}, %Value** {gname}")

        for d in top_level_order(program):
            if d.binding.uid in self.top_constants:
                continue
            val = self.compile(d.value, is_tail=False)
            if val is None:
                raise CompileError("top-level value did not produce a value", d.loc)
            rep = self.binding_rep(d.binding)
            val = self.coerce(val, rep, d.value.type)
            gname = self.top_value_globals[d.binding.uid]
            if rep == I64:
                self.emit_inst(f"store i64 {val.name}, i64* {gname}")
            elif rep == F64:
                self.emit_inst(f"store double {val.name}, double* {gname}")
            elif rep.startswith("REC:"):
                ty = self.rep_type(rep)
                self.emit_inst(f"store {ty} {val.name}, {ty}* {gname}")
            else:
                val = self.ensure_owned(val)
                self.emit_inst(f"store %Value* {val.name}, %Value** {gname}")

        result = self.compile(program.body, is_tail=False)
        if result is None:
            raise CompileError("program produced no value", program.body.loc)
        rep = self.node_rep(program.body)
        result = self.coerce(result, rep, program.body.type)
        if self.exit_code:
            if rep != I64:
                raise CompileError("--exit-code requires the program result to be Int", program.body.loc)
        elif self.allow_data:
            if rep == BOX:
                self.release(result)
        elif rep == I64:
            if not self.dynamic and is_bool_type(program.body.type):
                self.emit_inst(f"call void @lambda_print_bool(i64 {result.name})")
            else:
                self.emit_inst(f"call void @lambda_print_i64(i64 {result.name})")
        elif rep == F64:
            self.emit_inst(f"call void @lambda_print_float(double {result.name})")
        else:
            self.emit_inst(f"call void @lambda_print_ln(%Value* {result.name})")
            self.release(result)

        # Release program-lifetime globals.
        for d in self.top_defs:
            if d.binding is None:
                continue
            uid = d.binding.uid
            if d.is_function:
                if uid not in self.value_functions:
                    continue
                gname = self.top_function_globals.get(uid)
                if gname:
                    tmp = self.new_reg()
                    self.emit_inst(f"{tmp} = load %Value*, %Value** {gname}")
                    emit_release_ir(self.builder, tmp, self.atomic_rc)
            else:
                gname = self.top_value_globals.get(uid)
                if gname and self.binding_rep(d.binding) == BOX:
                    tmp = self.new_reg()
                    self.emit_inst(f"{tmp} = load %Value*, %Value** {gname}")
                    emit_release_ir(self.builder, tmp, self.atomic_rc)

        for name, g in sorted(self.adt_arenas.items()):
            reg = self.new_reg()
            self.emit_inst(f"{reg} = load %AdtArena*, %AdtArena** {g}")
            self.emit_inst(f"call void @lambda_arena_free(%AdtArena* {reg})")
        self.emit_inst("call void @lambda_flush()")
        self.emit_inst("call void @lambda_stats()")
        if self.exit_code:
            status = self.new_reg()
            self.emit_inst(f"{status} = trunc i64 {result.name} to i32")
            self.emit_inst(f"ret i32 {status}")
        else:
            self.emit_inst("ret i32 0")
        self.builder.raw("}")
        self.main_text = self.builder.render()


