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

from ast import (
    App, Binding, Block, Def, If, Lam, Lit, Node, PARAM, PRIM, TOP_DEF, Var, walk,
)
from analysis import IMPURE_PRIMITIVES, PRIM_ARITY, top_level_order
from errors import CompileError
from parser import OP_CANON
from runtime import RUNTIME_IR_DECLS
from typecheck import BOX, F64, I64, TCon, prune, rep_of

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
    "not": "@lambda_prim_not",
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
    "tail": "@lambda_op_tail", "empty": "@lambda_op_empty", "not": "@lambda_op_not",
    "str_to_int": "@lambda_op_str_to_int", "int_to_str": "@lambda_op_int_to_str",
    "str_to_list": "@lambda_op_str_to_list", "str_len": "@lambda_op_str_len",
    "str_append": "@lambda_op_str_append", "read_file": "@lambda_op_read_file",
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
    "arrayf_get": "@lambda_op_arrayf_get", "arrayf_set": "@lambda_op_arrayf_set",
    "arrayf_length": "@lambda_op_arrayf_length", "arrayf_clone": "@lambda_op_arrayf_clone",
    "arrayi_get": "@lambda_op_arrayi_get", "arrayi_set": "@lambda_op_arrayi_set",
    "arrayi_length": "@lambda_op_arrayi_length", "arrayi_clone": "@lambda_op_arrayi_clone",
    "arrayb_get": "@lambda_op_arrayb_get", "arrayb_set": "@lambda_op_arrayb_set",
    "arrayb_length": "@lambda_op_arrayb_length", "arrayb_clone": "@lambda_op_arrayb_clone",
    "arrayf_get_ro": "@lambda_op_arrayf_get", "arrayi_get_ro": "@lambda_op_arrayi_get",
    "arrayb_get_ro": "@lambda_op_arrayb_get",
    "arrayi_pmap": "@lambda_op_arrayi_pmap",
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


class Val:
    __slots__ = ("rep", "name", "owned", "immortal")

    def __init__(self, rep: str, name: str, owned: bool = False,
                 immortal: bool = False) -> None:
        self.rep = rep
        self.name = name
        self.owned = owned
        self.immortal = immortal

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
        "env_dtors", "generated", "generating",
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
        self.direct_name = ""
        self.wrapper_names: List[str] = []
        self.env_type_names: List[str] = []
        self.env_dtors: List[str] = []
        self.generated = False
        self.generating = False


class FnContext:
    __slots__ = ("ret_rep", "ret_type", "nparams")

    def __init__(self, ret_rep: str, ret_type, nparams: int = 0) -> None:
        self.ret_rep = ret_rep
        self.ret_type = ret_type
        self.nparams = nparams


def is_bool_type(t) -> bool:
    if t is None:
        return False
    t = prune(t)
    return isinstance(t, TCon) and t.name == "Bool"


def is_float_type(t) -> bool:
    if t is None:
        return False
    t = prune(t)
    return isinstance(t, TCon) and t.name == "Float"


def float_const(x: float) -> str:
    """A decimal LLVM double literal that round-trips to *x*."""

    text = repr(float(x))
    if text in ("inf", "-inf", "nan"):
        # Not reachable from source literals; be defensive.
        return "0.0"
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
                 allow_input: bool = False) -> None:
        self.dynamic = dynamic
        self.use_musttail = use_musttail
        self.debug = debug
        self.lambda_purity = lambda_purity or {}
        self.uses_parallel = False
        self.allow_input = allow_input

        self.type_defs: List[str] = []
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
        self.top_defs: List[Def] = []

        self.lam_infos: Dict[int, FunctionInfo] = {}
        self.pending_infos: List[FunctionInfo] = []
        self.next_lam_id = 0

        self.builder: Optional[IRBuilder] = None
        self.ctx: Optional[FnContext] = None
        self.scope_stack: List[Dict[int, Storage]] = []
        self.cleanup_stack: List[List[Tuple[str, bool]]] = []
        self.terminated = False
        self.moved_names: Set[str] = set()
        self.captured_uids: Set[int] = set()
        self.moves_allowed: bool = True

        self.string_counter = 0

    # ------------------------------------------------------------------
    # top-level setup
    # ------------------------------------------------------------------
    def compile_program(self, program: Node) -> str:
        if not isinstance(program, Block):
            program = Block([], program, program.loc)
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
        parts.append("\n; ---- top-level globals ----")
        parts.extend(self.top_globals)
        parts.append("\n; ---- functions ----")
        parts.extend(self.functions)
        parts.append("\n; ---- program entry ----")
        parts.append(self.main_text)
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
                if rep == I64:
                    self.top_globals.append(f"{name} = internal global i64 0")
                elif rep == F64:
                    self.top_globals.append(f"{name} = internal global double 0.0")
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
        b.raw(f"define void {name}(i8* %raw) {{")
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
            b.emit(f"call void @lambda_release(%Value* {v})")
        for idx in arg_idx:
            p = b.new_reg()
            b.emit(f"{p} = getelementptr {ty}, {ty}* {env}, i32 0, i32 {idx}")
            v = b.new_reg()
            b.emit(f"{v} = load %Value*, %Value** {p}")
            b.emit(f"call void @lambda_release(%Value* {v})")
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
    def rep_type(rep: str) -> str:
        if rep == I64:
            return "i64"
        if rep == F64:
            return "double"
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
        b.raw(f"define %Value* {name}(i8* %env_raw, %Value* %arg) {{")
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
                    b.emit(f"call void @lambda_retain(%Value* {v})")
                    self._store_env_field(b, nty, nenv, new_idx, "%Value*", v)
                elif rep == F64:
                    v = self._load_env_field(b, ty, env, old_idx, "double")
                    self._store_env_field(b, nty, nenv, new_idx, "double", v)
                else:
                    v = self._load_env_field(b, ty, env, old_idx, "i64")
                    self._store_env_field(b, nty, nenv, new_idx, "i64", v)
            for old_idx, new_idx in zip(arg_idx, narg_idx):
                v = self._load_env_field(b, ty, env, old_idx, "%Value*")
                b.emit(f"call void @lambda_retain(%Value* {v})")
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
                b.emit(f"call void @lambda_retain(%Value* {v})")
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
                b.emit(f"call void @lambda_retain(%Value* {v})")
                call_args.append(f"%Value* {v}")
        # The argument that triggered this final stage.
        rep = info.param_reps[info.arity - 1]
        ptype = info.param_types[info.arity - 1]
        if rep == I64:
            getter = "@lambda_get_bool" if (not self.dynamic and is_bool_type(ptype)) else "@lambda_get_int"
            raw = b.new_reg()
            b.emit(f"{raw} = call i64 {getter}(%Value* %arg)")
            b.emit("call void @lambda_release(%Value* %arg)")
            call_args.append(f"i64 {raw}")
        elif rep == F64:
            raw = b.new_reg()
            b.emit(f"{raw} = call double @lambda_get_float(%Value* %arg)")
            b.emit("call void @lambda_release(%Value* %arg)")
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

    def _gen_function_body(self, info: FunctionInfo) -> None:
        if info.generated or info.generating:
            return
        info.generating = True
        self.builder = IRBuilder()
        self.ctx = FnContext(info.ret_rep, info.ret_type,
                             len(info.captures) + info.arity)
        self.scope_stack = []
        self.cleanup_stack = []
        self.terminated = False
        self.moved_names = set()
        self.moves_allowed = True

        ret_ty = self.rep_type(info.ret_rep)
        sig: List[str] = []
        for i, rep in enumerate(info.capture_reps):
            sig.append(f"{self.rep_type(rep)} %cap{i}")
        for i, rep in enumerate(info.param_reps):
            sig.append(f"{self.rep_type(rep)} %par{i}")
        self.builder.raw(f"define {ret_ty} {info.direct_name}({', '.join(sig)}) {{")
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
            if rep == BOX:
                cleanup.append((op, False))

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
        raise CompileError(f"cannot coerce {val.rep} to {target}")

    def box(self, val: Val, type_hint) -> Val:
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
            self.emit_inst(f"call void @lambda_retain(%Value* {val.name})")
            val.owned = True
        return val

    def ensure_owned(self, val: Val) -> Val:
        if val.rep == BOX and not val.owned and not val.immortal:
            return self.retain(val)
        return val

    def release(self, val: Optional[Val]) -> None:
        if val is None:
            return
        if val.rep == BOX and val.owned and not val.immortal:
            self.emit_inst(f"call void @lambda_release(%Value* {val.name})")
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
                    self.emit_inst(f"call void @lambda_release(%Value* {name})")
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
                    self.emit_inst(f"call void @lambda_release(%Value* {name})")

    def emit_return(self, val: Optional[Val], type_hint) -> None:
        if self.ctx is None:
            raise CompileError("internal: return outside function")
        if val is None:
            return
        val = self.coerce(val, self.ctx.ret_rep, type_hint)
        self.emit_cleanup_all()
        if self.ctx.ret_rep == I64:
            self.emit_inst(f"ret i64 {val.name}")
        elif self.ctx.ret_rep == F64:
            self.emit_inst(f"ret double {val.name}")
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
        if isinstance(node, Lit):
            return self.compile_lit(node, is_tail)
        if isinstance(node, Var):
            return self.compile_var(node)
        if isinstance(node, Lam):
            return self.emit_make_closure(node)
        if isinstance(node, App):
            return self.compile_app(node, is_tail)
        if isinstance(node, If):
            return self.compile_if(node, is_tail)
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

    def compile_var(self, node: Var) -> Val:
        b = node.binding
        if b is None:
            raise CompileError(f"unresolved variable '{node.name}'", node.loc)
        if b.kind == PRIM:
            return self.primitive_global(b)
        if b.is_function:
            return self.materialize_function(b)
        st = self.find_storage(b.uid)
        if st is None:
            raise CompileError(
                f"definition '{b.name}' is used before it is initialised", node.loc)
        if st.rep == BOX and not st.is_global and st.name in self.moved_names:
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
        if st.is_global:
            res = self.new_reg()
            self.emit_inst(f"{res} = load %Value*, %Value** {st.name}")
            val = Val(BOX, res, owned=True)
            self.retain(val)
            return val
        val = Val(BOX, st.name, owned=True)
        self.retain(val)
        return val

    def _capture_value(self, b: Binding, loc) -> Val:
        if b.kind == PRIM:
            return self.primitive_global(b)
        if b.is_function:
            return self.materialize_function(b)
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

        saved_moved = set(self.moved_names)
        saved_cleanup = [list(c) for c in self.cleanup_stack]
        saved_allowed = self.moves_allowed

        def restore_branch_state() -> None:
            self.moved_names = set(saved_moved)
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
            self.moved_names = set(saved_moved)
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

        if not self.moves_allowed:
            return None
        if not isinstance(node, Var) or node.binding is None:
            return None
        b = node.binding
        if b.kind != PARAM or b.uid in self.captured_uids:
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
        if st.name in self.moved_names:
            raise CompileError(
                f"'{b.name}' was already consumed and cannot be moved again",
                node.loc)
        self.moved_names.add(st.name)
        for cleanup in self.cleanup_stack:
            cleanup[:] = [(name, immortal) for name, immortal in cleanup
                          if name != st.name]
        return Val(BOX, st.name, owned=True)

    def emit_direct_call(self, info: FunctionInfo, args: List[Node],
                         result_type, is_tail: bool) -> Optional[Val]:
        call_args: List[str] = []

        for cap, rep, ctype in zip(info.captures, info.capture_reps,
                                   info.capture_types):
            val = self._capture_value(cap, info.lam.loc if info.lam else None)
            val = self.coerce(val, rep, ctype)
            if rep == I64:
                call_args.append(f"i64 {val.name}")
            elif rep == F64:
                call_args.append(f"double {val.name}")
            else:
                val = self.ensure_owned(val)
                call_args.append(f"%Value* {val.name}")

        for pos, (arg, rep, ptype) in enumerate(zip(args, info.param_reps,
                                                    info.param_types)):
            val = None
            if rep == BOX and is_tail:
                val = self._move_tail_arg(arg, args, info, pos)
            if val is None:
                val = self.compile(arg, is_tail=False)
                if val is None:
                    return None
                val = self.coerce(val, rep, arg.type)
            if rep == I64:
                call_args.append(f"i64 {val.name}")
            elif rep == F64:
                call_args.append(f"double {val.name}")
            else:
                val = self.ensure_owned(val)
                call_args.append(f"%Value* {val.name}")

        ret_ty = self.rep_type(info.ret_rep)
        joined = ", ".join(call_args)

        if (is_tail and self.use_musttail
                and info.ret_rep == self.ctx.ret_rep):
            self.emit_cleanup_all()
            # LLVM's ``musttail`` requires matching argument counts, which our
            # explicit-capture ABI does not always have (e.g. a closure calling
            # a neighbouring loop with more captures).  ``tail`` is still
            # enough for LLVM to emit a jump; use the stronger marker when the
            # signatures line up so self-recursion is guaranteed.
            nparams = len(info.captures) + info.arity
            marker = "musttail" if nparams == self.ctx.nparams else "tail"
            res = self.new_reg("mt")
            self.emit_inst(f"{res} = {marker} call {ret_ty} {info.direct_name}({joined})")
            self.emit_inst(f"ret {ret_ty} {res}")
            self.terminated = True
            return None

        res = self.new_reg("res")
        self.emit_inst(f"{res} = call {ret_ty} {info.direct_name}({joined})")
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
        if op == "pmap":
            if len(args) < 2:
                raise CompileError(
                    "pmap must be applied to a provably pure function and a list",
                    node.loc)
            self.uses_parallel = True
            self.check_pmap_function(args[0], node.loc)
        if op == "arrayi_pmap":
            if len(args) < 2:
                raise CompileError(
                    "arrayi-pmap must be applied to a provably pure Int -> Int "
                    "function and a length", node.loc)
            self.uses_parallel = True
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
                    if not st.is_global and st.name in self.moved_names:
                        raise CompileError(
                            f"'{b.name}' was consumed by a destructive array "
                            f"update and cannot be used again", node.loc)
                    if st.is_global:
                        res = self.new_reg()
                        self.emit_inst(
                            f"{res} = load %Value*, %Value** {st.name}")
                        return Val(BOX, res, owned=False)
                    return Val(BOX, st.name, owned=False)
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
        if b.kind != PARAM or b.uid in self.captured_uids:
            return None
        st = self.find_storage(b.uid)
        if st is None or st.rep != BOX or st.is_global:
            return None
        if not st.name.startswith("%par"):
            return None
        if st.name in self.moved_names:
            raise CompileError(
                f"'{b.name}' was already consumed by a destructive array update",
                node.loc)
        self.moved_names.add(st.name)
        for cleanup in self.cleanup_stack:
            cleanup[:] = [(name, immortal) for name, immortal in cleanup
                          if name != st.name]
        return Val(BOX, st.name, owned=True)

    def _array_bounds(self, arr: Val, idx: Val) -> None:
        """Emit a checked bounds branch; types already guarantee the tag."""
        lenp = self.new_reg()
        self.emit_inst(
            f"{lenp} = getelementptr %Value, %Value* {arr.name}, i32 0, i32 2")
        length = self.new_reg()
        self.emit_inst(f"{length} = load i64, i64* {lenp}")
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
        pp = self.new_reg()
        self.emit_inst(
            f"{pp} = getelementptr %Value, %Value* {arr.name}, i32 0, i32 3")
        raw = self.new_reg()
        self.emit_inst(f"{raw} = load i8*, i8** {pp}")
        data = self.new_reg()
        self.emit_inst(f"{data} = bitcast i8* {raw} to {llty}*")
        ep = self.new_reg()
        self.emit_inst(
            f"{ep} = getelementptr {llty}, {llty}* {data}, i64 {idx.name}")
        return ep

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
            inst = "sdiv" if op == "div" else "srem"
            self.emit_inst(f"{res} = {inst} i64 {a.name}, {b.name}")
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
            self.emit_inst(f"{res} = fptosi double {a.name} to i64")
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
        if op in ("arrayf_set", "arrayi_set", "arrayb_set"):
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
        if op in ("arrayf_length", "arrayi_length", "arrayb_length"):
            a = self._array_borrow(args[0])
            base = op.replace("_length", "")
            lenp = self.new_reg()
            self.emit_inst(
                f"{lenp} = getelementptr %Value, %Value* {a.name}, i32 0, i32 2")
            res = self.new_reg()
            self.emit_inst(f"{res} = load i64, i64* {lenp}")
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
        self.moved_names = set()
        self.moves_allowed = True
        self.builder.raw("define i32 @main(i32 %argc, i8** %argv) {")
        self.builder.label("entry")
        self.emit_inst("call void @lambda_runtime_init()")

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
            clo = self.emit_make_closure(lam)
            gname = self.top_function_globals[d.binding.uid]
            self.emit_inst(f"store %Value* {clo.name}, %Value** {gname}")

        for d in top_level_order(program):
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
            else:
                val = self.ensure_owned(val)
                self.emit_inst(f"store %Value* {val.name}, %Value** {gname}")

        result = self.compile(program.body, is_tail=False)
        if result is None:
            raise CompileError("program produced no value", program.body.loc)
        rep = self.node_rep(program.body)
        result = self.coerce(result, rep, program.body.type)
        if rep == I64:
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
                gname = self.top_function_globals.get(uid)
                if gname:
                    tmp = self.new_reg()
                    self.emit_inst(f"{tmp} = load %Value*, %Value** {gname}")
                    self.emit_inst(f"call void @lambda_release(%Value* {tmp})")
            else:
                gname = self.top_value_globals.get(uid)
                if gname and self.binding_rep(d.binding) == BOX:
                    tmp = self.new_reg()
                    self.emit_inst(f"{tmp} = load %Value*, %Value** {gname}")
                    self.emit_inst(f"call void @lambda_release(%Value* {tmp})")

        self.emit_inst("call void @lambda_flush()")
        self.emit_inst("call void @lambda_stats()")
        self.emit_inst("ret i32 0")
        self.builder.raw("}")
        self.main_text = self.builder.render()


