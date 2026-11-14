# Pi Specification v2.0

> **Implementation addendum (this repository).**  The reference implementation
> adds an optional **one-GPU** extension for `arrayf-pmap`: a `Pool` may declare
> `{gpus: 1}`, which sets `LAMBDA_GPU=1` for that worker.  This is a superset of
> the v2.0 core, where GPU resources are reserved.  Multi-GPU and explicit VRAM
> budgets remain unsupported (`Unsupported`).  All other normative text below is
> unchanged; the README's status matrix distinguishes implementation from spec.

**A coordination language for Lambda compute, with local-to-global error handling and failsafe recovery**

---

## 0. Preface

Pi coordinates parallel and distributed computation. Lambda performs it. They meet at a narrow typed seam: Pi hands a Lambda worker an opaque handle; the worker computes; Pi routes the result.

This revision (v2.0) makes two things first-class that v1.0 left implicit: **the local-to-global error model** and **the failsafe recovery mechanisms**. v1.0 specified that errors propagate to `on_error`; v2.0 specifies *which* errors a worker handles locally, *which* it surfaces, *how* state survives failure, and *when* the program continues versus halts. It also corrects the backend model (CPU-threaded, no GPU), renames the worker field, and adds sharding as a distinct concern.

The design rests on one observation: **most reliability comes from structure, not algorithms.** Structured lifetimes eliminate orphans. Linear handles eliminate use-after-release. Bounded channels eliminate unbounded growth. A closed error taxonomy eliminates unhandled failures. A **three-tier error model** eliminates the choice between "crash on every numerical hiccup" and "silently absorb everything."

---

## 1. Design goals and paradigms

### 1.1 Goals

**G1. Structural reliability.** Reliability follows from the type system or program structure, not runtime heuristics. Well-typed programs are free of orphaned processes, use-after-release, unbounded growth, cyclic deadlock, and silent failure.

**G2. Local analysis.** Verify one element's contract and one coupling rule; the composition holds independent of mesh size.

**G3. Coarse seam.** Boundary crossings scale with *workers × steps*, not *cells × steps*. The seam overhead is `O(RPC_cost / per_worker_compute)`, negligible when per-worker compute per step is ≥ 100× the RPC cost.

**G4. Visible cost.** Cost is predictable from source: number of messages, allocations, channel operations.

**G5. Minimal primitives.** Every primitive earns its place by eliminating a bug class or generating a widely-used pattern.

**G6. Total error handling.** Every failure is classified, every handler exhaustive, every retry gated on retryability. **Every error has a designated handler — local, aggregate, or fatal — and the layer that handles it is chosen by information and authority, not convention.**

**G7. Failsafe by construction.** A failure in one component has a bounded blast radius. Recovery is a scope operation. The program continues when recovery is possible, halts cleanly when it is not, and always leaves an auditable trace.

**G8. Toolchain closure.** A complete Pi program uses only Pi and Lambda for computation and coordination. External driver and linker are present at every stage (as in Rust, Swift, Zig).

**G9. Backend honesty.** Lambda is CPU-threaded AOT. GPU support is a future extension; Pi's design does not depend on it. Programs declare CPU resources; GPU declarations fail at recruit with `Unsupported`.

**G10. Pluggable scheduling.** Cyclic executive (embedded), M:N (server), priority-based (firm real-time). Same semantics, different profiles.

### 1.2 Paradigms

**Differential element.** Analyze one element (ports, contract) and one assembly rule (coupling). The mesh size drops out.

**Tree of scopes with labeled edges.** Every process lives in a `scope`; exit joins or cancels all children. Every non-tree communication is a linear handle passed as an argument — visible, consumed once. No first-class process handles, no ambient context.

**Coordination is not computation.** Pi never inspects a payload. Values are handles: `Chan`, `Future`, `Worker`, `Pool`, `Tensor`, `Shard`, `Config`, `Err`.

**State ownership determines recovery granularity.** If Pi holds the state (as a `state` field), a failed call consumes it and recovery is limited to restart-from-checkpoint. If the worker holds the state (as in the stateful-worker pattern, §12.3), recovery ops act on the last good state and the program continues. **This is the central design decision for failsafe recovery, and it is expressed in the element template.**

### 1.3 Three-tier error model

Every error is classified by **who has the information and the authority to decide the recovery**:

| Tier | Example | Handled by | Surfaced to Pi? | Mechanism |
|---|---|---|---|---|
| **Local** | One atom divides by zero; one record malformed | Lambda, per policy | No | Policy in `Params`; worker corrects and returns `Ok` |
| **Aggregate** | 30% of atoms diverged; >1% of records malformed | Lambda detects, Pi decides | Yes, as `Ok(stats)` with warning count | Worker handles locally; stats observed by Pi |
| **Fatal** | Invariant violated; resource exhausted; worker cannot proceed | Pi | Yes, as `Err(code)` → `WorkerError(code)` | `on_error` with `resume`/`restart`/`escalate`/`stop` |

The dividing line: **the layer with the information and authority handles the error.** A single atom's numerical issue — the worker knows the atom, the neighbours, the local physics; it handles it. A systematic divergence — the decision (halt, adjust timestep, restart) requires the whole system's state; Pi handles it.

This tiering is not a language feature. It is a **design discipline**, expressed through three mechanisms:
1. `Params.numerical_policy` — a Lambda convention for local handling.
2. `Ok(state, stats)` — a richer `Ok` payload that carries observability.
3. `Err(code)` — the transport surface for fatal failures.

All three use the existing `Result[T]` sum type (§11.1), the existing transport unwrap (§11.3), and the existing `on_error` (§6.5). No new primitives.

---

## 2. Non-goals

Pi is **not** a general-purpose language, a replacement for Rust/C++/Elixir, a config language, a dynamic-topology orchestrator, or a data-processing DSL.

Pi **does not have** arithmetic, payload inspection, string operations, structural data types, dynamic dispatch, reflection, mutable global state, garbage collection, first-class process handles, or implicit I/O.

**Explicit limits from v1.0 retained:** dynamic membership (add/remove workers at runtime), repartitioning the mesh, consensus with dynamic leaders, gossip, unbounded-state joins, hard real-time below Pi. These are outside the safe fragment; they are not bugs.

**New explicit limit:** GPU resources. Lambda is CPU-only; Pi's resource declarations are `{cpus, memory}`. `{gpus, vram}` is reserved and returns `Unsupported`.

---

## 3. Lexical structure

### 3.1 Tokens

```
ident       ::= [a-z][a-zA-Z0-9_']*
conid       ::= [A-Z][a-zA-Z0-9_']*
operator    ::= → | := | ≡ | ‖ | + | ; | ⇒ | ≤ | ≥ | == | < | > | / | % | @ | ?
keyword     ::= if | then | else | match | with | type | include | import
              | scope | par | serve | loop | iterate | for | in
              | select | call | await | cancel | send | recv | close | closed
              | after | fail | on_error | restart | escalate | stop | resume
              | slice | dims | drop
              | elem | system | instances | couple | expose | ctrl | state
              | worker | protocol | op | purity | contract
              | program | main | module
attribute   ::= @ rate ( duration ) | @ priority ( int ) | @ deadline ( duration )
              | @ periodic | @ sporadic | @ aperiodic
literal     ::= int | float | duration | bool | ()
duration    ::= int ('ms' | 's' | 'm' | 'h' | 'us' | 'ns')
comment     ::= '--' to end-of-line | '{-' ... '-}' (nested)
```

**Change from v1.0:** `rpc` is renamed to `worker`. The old keyword is accepted with a deprecation warning.

### 3.2 Modules

```
module      ::= import* declaration*
declaration ::= protocol | elem | system | fun | program
import      ::= 'import' string
```

### 3.3 Blocks

```
block      ::= definition* expression
definition ::= ident '→' expression
             | ident ':=' expression
             | statement
```

**Scoping.** Definitions are scoped **sequentially** — each visible only in what follows. Differs from Lambda (mutually recursive); the difference is forced by effects.

**No braces, no semicolons.** Blocks delimited by context.

---

## 4. Types

### 4.1 Type grammar

```
τ ::= Handle κ
    | Chan σ | Future σ | Worker ⟨Protocol⟩ | Pool ⟨Resource⟩
    | Tensor ⟨dt, place, dims⟩ | Shard ⟨dims⟩ | Config ⟨kind⟩ | Err | ()
    | σ × τ | [σ] | Ring[n]

σ ::= Handle κ | σ × τ | [σ] | ()

κ ::= Chan | Future | Worker | Pool | Tensor | Shard | Config | Err

dt    ::= Float | Int | Bool | UInt8 | UInt16 | UInt32 | UInt64
place ::= host | remote              -- `gpu` reserved; returns Unsupported
dims  ::= [n, n, ...]
```

`Handle κ` is **linear** unless marked affine. `Future` is affine. `Chan`, `Worker`, `Pool`, `Tensor`, `Shard`, `Config` are linear. `Ring[n]` is a fixed-size array of `n` linear handles.

### 4.2 Channel endpoints

```
Chan σ = { Tx σ, Rx σ }
```

`chan(cap)` yields both. Sending takes `Tx`; receiving takes `Rx`.

### 4.3 The error type (closed)

```
Err = WorkerDied | Deadline | Transport | Rejected Resource
    | BadFrame SchemaHash | WorkerError ErrorCode
    | Cancelled | NoQuorum | Incomplete | Unsupported

Resource     = Gpu | Memory | Cpu
ErrorCode    = Int                      -- application-defined; see §11.2
```

Retryability:

```
retryable WorkerDied  = true
retryable Deadline    = true
retryable Transport   = true
retryable _           = false
```

`BadFrame` and `WorkerError` are never retryable without an idempotency key.

### 4.4 Protocol types

```
protocol Stencil⟨N⟩ {
  op step  : (Params) → Shard                  -- stateful: worker holds state
  op patch : (HaloRing⟨N⟩, Params) → Shard
  errors   : { 4001: "div_by_zero", 4002: "diverged_step", 9001: "internal" }
  purity   : Pure
}
```

The `errors` field declares the error codes this protocol can produce. Pi's `on_error` can key on these codes. **New in v2.0.**

### 4.5 Schema hashes

128-bit hash negotiated at recruit. `call(w, op, v)` fails `BadFrame(h)` on mismatch.

---

## 5. Core terms

```
p, q ::= 0
       | p ; q
       | p ‖ q
       | p + q
       | scope { p }
       | select { c₁ ⇒ p₁ | … | cₙ ⇒ pₙ }
       | serve(c) { x ⇒ p }
       | serve(c) @ rate(d) { x ⇒ p }
       | loop p
       | iterate(n) { p }
       | recv(c) | send(c, v) | close(c) | closed(c)
       | call(w, op, v)                        -- Future Result
       | await(f) | cancel(f)
       | slice(h, region) | dims(h) | drop(h)
       | after(d) { p } | fail(e)
       | on_error(p) { | e₁ ⇒ h₁ | … | eₙ ⇒ hₙ }
       | serve_worker(w)
       | handle_failure(p, policy)
       | let x = p in q
       | rec(X) { p } | X

select guard ::= recv(c) | closed(c) | after(d)
handler      ::= | e ⇒ p
policy       ::= restart(max = n, backoff = b) | escalate | stop | resume
```

**Change from v1.0:** `resume` is now fully specified (§6.6) and requires the stateful-worker pattern (§12.3) for meaningful use.

### 5.1 Linearity

Every linear handle consumed by exactly one of `send`, `recv`, `close`, `await`, `cancel`, `drop`, or exit from an owning `scope`. Duplication and silent discard rejected. `drop(h)` is the explicit discard.

### 5.2 Scope discipline

`scope { p }` — normal exit joins children; abnormal exit cancels recursively, consuming handles. Spawn outside any scope is ill-typed (except `after` and top-level `program`).

### 5.3 `serve`

`serve(c) { x ⇒ p }`: recv → `x`; if `closed`, exit normally; else run `p` in a fresh scope; repeat. Productivity required.

`serve(c) @ rate(d)` wakes every `d` ms; used for periodic tasks in cyclic-executive profile.

### 5.4 `+` (race)

Start both in fresh sub-scopes. First normal completion wins; loser cancelled. Retryable failure suppressed while other runs. Both fail → first propagates. Completion wins over simultaneous failure.

### 5.5 `on_error`

`on_error(p) { | e₁ ⇒ h₁ | … }` catches errors; match is exhaustive over `Err`. Handlers may be `restart`, `resume`, `escalate`, `stop`, or arbitrary `p`. Semantics of each in §6.6.

### 5.6 `call` and `Future`

`call(w, op, v) → Future Result` — the `Result` is `Ok(return_value)` or `Err(Error)`. The transport unwraps it (§11.3): `Ok` → `REPLY`; `Err` → `ERROR(code, span, sym)`. Pi's `await` on an `Ok` yields the return value; on an `Err`, raises `WorkerError(code)`.

**Change from v1.0:** the transport unwrap is now formalized; `WorkerError` carries the code from the worker's `Err`.

---

## 6. Derived forms and policies

### 6.1–6.4 (unchanged from v1.0)

`par for`, `timeout`, `retry`, `barrier`, `queue`, `pool` — all as before. `retry` over `Impure` requires `idem`.

### 6.5 `on_error` handler semantics

Each handler is one of:

| Form | Semantics |
|---|---|
| `restart(max=n, backoff=b)` | Scope exit; fresh worker recruited; handles re-acquired; scope body re-run from the beginning |
| `resume` | Handler returns `Ok(())`; the enclosing `serve` loop continues to the next iteration with the worker's last good state; **only valid with stateful workers** (§12.3) |
| `escalate` | Error propagates to the enclosing scope's `on_error` |
| `stop` | Scope exits cleanly; all children cancelled; handles consumed |
| `\| e ⇒ p` | Arbitrary handler; `p` runs in a fresh scope; on normal completion, the enclosing scope continues as if the failure were handled |

### 6.6 `resume` — the failsafe primitive

`resume` is the mechanism that lets a program continue after a recoverable failure. Its semantics:

1. The failing `call(w, op, v)` raised `WorkerError(code)`.
2. The `on_error` handler matches the code.
3. The handler runs (typically a recovery `call` like `call(w, "renormalize", p)`).
4. `resume` returns `Ok(())` to the enclosing `serve` loop.
5. The `serve` loop continues to the next `tick` (or next `recv`) with the worker's last good state.

**Critical precondition:** `resume` is only valid if the failed operation did not consume Pi's state. In v1.0, the `state` field meant Pi held the state, and a failed call consumed it — `resume` would be unsound. **v2.0 introduces the stateful-worker pattern (§12.3)** where the worker holds the state; `resume` is sound for stateful workers and is rejected by the type checker for stateless ones.

**Type rule:**

```
Γ; Δ ⊢ (element has no `state` field)
────────────────────────────────────────────── (Resume)
handler may use `resume`
```

A `resume` in an element with a `state` field is a type error: "resume requires stateful worker; state consumed by failed call."

### 6.7 The three-tier recovery table

| Tier | Mechanism | Where handled | Program outcome |
|---|---|---|---|
| **Local** | `Params.policy = Renormalize`; worker corrects; returns `Ok` | Lambda | Continues; no Pi involvement |
| **Local (observed)** | `Params.policy = Report`; worker corrects; returns `Ok(state, stats)` with warning count | Lambda + Pi observer | Continues; Pi may act on count |
| **Fatal (recoverable)** | Worker returns `Err(code)`; Pi `on_error` matches; handler calls recovery op; `resume` | Pi | Continues with last good state |
| **Fatal (restart)** | Worker returns `Err(code)`; `restart(n)` | Pi | Re-runs from checkpoint |
| **Fatal (terminal)** | Worker returns `Err(code)`; `stop` or `escalate` | Pi | Halts cleanly or escalates |

**The design rule:** local errors never cross the seam. Aggregate errors cross as `Ok(stats)`. Fatal errors cross as `Err(code)`. Pi's `on_error` handler is exhaustive over `Err`; the aggregate channel is a `Chan[Stats]` served by an observer.

---

## 7. Type system

### 7.1 Structure

Sequent calculus with linear contexts. `Γ; Δ ⊢ p : τ` separates unrestricted `Γ` from linear `Δ`.

### 7.2 Selected rules (unchanged from v1.0)

`Seq`, `Par`, `Scope`, `Serve`, `RetryNoKey`, `RetryWithKey` — as before.

### 7.3 New: `resume` check

```
Γ; Δ ⊢ element has no state field
Γ; Δ ⊢ handler : Result[()]
──────────────────────────────────────  (Resume)
Γ; Δ ⊢ resume valid
```

### 7.4 New: `errors` declaration check

A protocol's `errors` field must declare every code that any `Err(Error(code, _, _))` in the worker's operations can produce. The checker verifies at recruit time: any code observed at runtime that is not declared causes `BadFrame`.

### 7.5 Scope boundary check (unchanged)

Every `call`, `serve`, `par`, `+`, `par for` must be in a `scope` or `program`.

### 7.6 Exhaustiveness (unchanged)

`on_error` handlers cover every `Err` constructor or include a wildcard.

### 7.7 Scheduler attribute checks (unchanged from v1.0 §7.5)

`@ rate`, `@ priority`, `@ deadline` validated as before.

---

## 8. Operational semantics

### 8.1 Configuration (unchanged from v1.0)

```
Conf = ⟨ F, C, W, T, S ⟩
  F : FiberId ⇀ Process
  C : ChanId  ⇀ ChannelState
  W : WorkerId ⇀ WorkerState
  T : FiberId → (SpanID, ParentSpanID, Deadline)
  S : SchedProfile
```

### 8.2 Reduction rules (unchanged plus new)

```
(Call)     call(w, op, v) → enqueue; continue with Future
(AwaitOk)  await(f) with REPLY(v) → consume; continue with v
(AwaitErr) await(f) with ERROR(code, span, sym) → consume; raise WorkerError(code)
(Race)     — unchanged
(Serve)    — unchanged
(ScopeExit), (ScopeFail) — unchanged
(Resume)   on_error raised WorkerError(code); handler returns Ok(());
             consume handler scope; serve loop continues to next iteration
(Stats)    await(f) with REPLY(state, stats) → continue with both;
             if stats.warnings > threshold: emit to observer channel
```

### 8.3 Scheduler profiles (unchanged from v1.0)

Cyclic executive, M:N work-stealing, priority-based preemptive. Semantics identical; timing and WCET guarantees differ.

### 8.4 Schedulability (unchanged)

For cyclic executive: `WCET_tick = Σ_i WCET_i + overhead`.

For priority-based: RTA.

### 8.5 Fairness, deadlines, cancellation (unchanged)

---

## 9. Runtime

### 9.1–9.2 Channels, queues (unchanged from v1.0)

### 9.3 Worker transport (updated)

One socket per worker. Requests multiplexed by correlation ID. Backpressure via socket send buffer. Pipelining: many `call`s in flight. Cancellation via `CANCEL`. Health via `PING`/`PONG`; 3 missed → `WorkerDied`.

**New in v2.0: transport unwrap.** The worker's return value is `Result[T]`. The transport inspects the constructor:

- `Ok(v)` → `REPLY(v)` (or `REPLY(state, stats)` for the observed-local pattern)
- `Err(Error(code, span, sym))` → `ERROR(code, span, sym)`

The `span` and `sym` are structured diagnostic data. Pi's `WorkerError(code)` carries only the code; if the caller wants the span and symbol, the worker sends them over a separate `errors : Chan[Diagnostic]` port. Pi routes the channel but does not inspect it.

### 9.4 Supervisor (updated)

Workers are OS processes (or linked libraries in embedded mode). On failure:

| Error class | Default policy |
|---|---|
| `WorkerDied` | `restart(max=3, backoff=exp)` |
| `Transport` | `restart(max=3)` |
| `Deadline` | `restart(max=1)` if idempotent; else `escalate` |
| `WorkerError(code)` | Per `errors` declaration: `resume` if recoverable, `restart` if transient, `stop` if terminal |
| `BadFrame` | `stop` (schema mismatch is not recoverable) |
| `Rejected` | `escalate` (resource exhaustion needs higher-level decision) |

Drain closes, waits, stops. **New in v2.0: recovery call timing.** When `resume` is used, the recovery op must complete within `deadline × 0.5`; otherwise the supervisor escalates to `restart`.

### 9.5 Resources (updated — CPU only)

CPU enumeration at pool creation. Memory accounting per pool; over-budget `call` fails `Rejected(Memory)` at call time.

**Change from v1.0:** `{gpus, vram}` is reserved; `Pool(..., {gpus: N})` fails at recruit with `Unsupported`. `Tensor[_, gpu, _]` fails to construct.

### 9.6 Tracing and replay (unchanged)

Every `scope`, `call`, `send`, `recv`, `close`, error carries a span. Replay reproduces all-`Pure`/`Deterministic` schedules. **New in v2.0:** recovery events (handler invocation, `resume`, `restart`) are traced with a dedicated span type, so the recovery path is auditable.

### 9.7 Implementation strategy (unchanged)

Interpreter v2.0 → AOT v2.1 → embedded mode v2.2.

---

## 10. The Lambda interface

### 10.1 Protocol (IDL) — updated

```
protocol <Name>⟨type-params⟩ {
  op <name> : (<Args>) → <Ret>
  ...
  errors : { <code> : "<name>", ... }
  purity : Pure | Deterministic | Impure
}
```

**`errors` is required** in v2.0. A protocol without it is rejected. The declaration lists every code the worker can produce as `Err(Error(code, _, _))`. Codes 1–9999 are reserved for Pi runtime (`WorkerDied` etc.); application codes are ≥ 10000.

### 10.2 Worker conformance (unchanged)

A Lambda worker is a Lambda module exporting functions matching the protocol's `op` names. Top-level definitions are the exports. Manifest declares protocol, exports, version.

**New in v2.0:** the worker's operations return `Result[Ret]`. The transport unwraps `Ok`/`Err` (§9.3).

### 10.3 Capability frame — updated

```json
{
  "protocol": "Stencil",
  "version": "2.0.0",
  "ops": [
    {"name": "step",  "args": ["Params"], "ret": "Shard", "purity": "Pure"},
    {"name": "patch", "args": ["HaloRing", "Params"], "ret": "Shard", "purity": "Pure"}
  ],
  "errors": { 4001: "div_by_zero", 4002: "diverged_step", 9001: "internal" },
  "resources": {"cpus": 1, "memory_bytes": 67108864, "gpus": 0},
  "limits": {"max_frame_bytes": 4294967296, "max_inflight": 64},
  "arrayf_pmap": false
}
```

**Changes:** `errors` declared; `gpus: 0` explicit; `arrayf_pmap: false` honest about current Lambda.

### 10.4 Transport framing (unchanged)

Length-prefixed binary. Control opcodes `< 0x100`: `HELLO`, `DESCRIBE`, `CALL`, `REPLY`, `ERROR`, `CANCEL`, `DRAIN`, `STOP`, `PING`, `PONG`.

**New:** `ERROR` frame carries `(code, span, sym)` where `span` is two u32s and `sym` is an optional u32 symbol-table index.

### 10.5 `slice` and `dims` (unchanged)

`slice` is Pi-level, computes region from `dims` and constants. Result is a view handle. Transport chooses zero-copy or materialize.

---

## 11. Error handling — the three-tier model

### 11.1 `Result[T]` — a Lambda sum type, not a primitive

```lambda
type Option[T] (Some (value: T) | None)
type Span      (Span (start: Int, end: Int))
type Error     (Error (code: Int, span: Span, sym: Option[Int]))
type Result[T] (Ok (value: T) | Err (err: Error))
```

All four are ordinary sum types with fixed-mode payloads. No string message — diagnostics are structured (code + span + optional symbol index); human-readable text is generated from the code plus context at display time.

**The shared error type is deliberate.** `Err` is a single `Error`, not a generic `E`. This means `?` can propagate across module boundaries without conversions, which is what a compiler needs. Pi's `WorkerError(Int)` also takes an `Int`, so the code space is shared.

### 11.2 `?` propagation — a Lambda syntax addition

```
f : ... → Result[T]
body: x → (g ...)?  ...
```

- `?` only valid in functions returning `Result[_]`.
- `(expr)?`: if `Ok(v)` → yields `v`; if `Err(e)` → early-returns `Err(e)` from the enclosing function.
- No error conversion. Same `Error` type everywhere.

This is the one ergonomic addition that makes `Result` usable. Without it, every function threads `match`.

### 11.3 Transport unwrap — a runtime convention

The worker's return value is `Result[T]`. The transport inspects:

| Worker returns | Transport sends | Pi sees |
|---|---|---|
| `Ok(v)` | `REPLY(v)` | `await` yields `v` |
| `Ok(v, stats)` | `REPLY(v, stats)` | `await` yields `(v, stats)`; stats routed to observer channel |
| `Err(Error(code, span, sym))` | `ERROR(code, span, sym)` | `WorkerError(code)` raised |

The transport is the **only** place that knows the `Result` shape. It is a convention, documented and implemented once.

### 11.4 The three tiers, concretely

**Tier 1 — Local (no Pi involvement).**

```lambda
step → (λp p.
  -- compute new state; if a cell divides by zero:
  -- if p_policy == Renormalize: clamp and continue; return Ok(state)
  -- if p_policy == Report:      clamp, increment warnings, return Ok(state, stats{warnings})
  -- if p_policy == Halt:        return Err(Error(4001, span, None))
  ...)
```

`Params.policy` is a Lambda convention: a sum type `Policy = Renormalize | Report | Halt`. The worker implements it. Pi passes it in `Params`. No Pi involvement for `Renormalize`.

**Tier 2 — Aggregate (Pi observes via stats).**

The worker returns `Ok(state, stats)` where `stats` includes a `warnings : Int` and a `max_error : Float`. Pi can:

```kappa
(_, stats) → await call(w, "step", p)
send(stats_ch, stats)            -- route to observer
if stats.warnings > 1000 {       -- Pi decides based on aggregate
  await call(w, "reduce_timestep")
  send(alert_ch, (w, stats.warnings))
}
```

The decision is Pi's, but the *detection* is the worker's. The worker has the information; Pi has the authority.

**Tier 3 — Fatal (Pi decides via `Err`).**

The worker returns `Err(Error(code, span, sym))`. Pi's `on_error` matches the code:

```kappa
on_error(loop_step) {
  | WorkerError(4001) => { await call(gs, "renormalize", p); resume }
  | WorkerError(4002) => { await call(gs, "reduce_timestep"); resume }
  | WorkerError(9001) => restart(max = 3, backoff = exp(0.2))
  | WorkerDied        => restart(max = 3)
  | _                 => escalate
}
```

`resume` requires the stateful-worker pattern (§12.3). `restart` re-runs from the last checkpoint. `escalate` propagates.

### 11.5 The failsafe ladder

For any failure, the supervisor tries, in order:

1. **Local handling** — worker's policy; no Pi involvement.
2. **Observed aggregate** — stats routed; Pi may act on thresholds.
3. **Recovery op** — `call(w, "recover", ...)` with the worker's last good state.
4. **Resume** — continue the serve loop.
5. **Restart** — re-run the scope from the last checkpoint.
6. **Escalate** — propagate to the parent scope.
7. **Stop** — clean halt with dead-letter record.

Each rung has a bounded time budget. If a rung fails or times out, the supervisor drops to the next. This is the **failsafe implementation**: the program never hangs on a failure; it either recovers, restarts, escalates, or halts cleanly, and always leaves an auditable trace.

---

## 12. Modules: elements and systems

### 12.1 `elem` — updated syntax

```
elem <Name>⟨type-params⟩(value-params) {
  in  <port> : <ChanType>
  out <port> : <ChanType>
  worker <name> : Worker⟨Protocol⟩         -- renamed from `rpc`
  state <name> : <HandleType>              -- optional; see §12.3
  ctrl <name> = <block>
  contract { <meta-notation> }
  on_failure <policy>                       -- optional default handler
}
```

**Changes from v1.0:** `rpc` → `worker`; `on_failure` added as a default recovery policy for the element's `ctrl`; `state` becomes optional (stateful workers omit it).

### 12.2 Stateless element (v1.0 pattern)

Pi holds the state; a failed call consumes it; recovery is restart-only.

```kappa
elem StencilWorkerStateless⟨N⟩(p : Params) {
  in  tick      : Chan[()]
  in  halos_in  : Ring[N]
  out halos_out : Ring[N]
  worker gs     : Worker⟨Stencil⟩
  state s       : Shard[4098, 4098]

  ctrl loop_step =
    serve(tick) { _ =>
      s' → await call(gs, step, s, p)
      for i in 0..N { send(halos_out[i], slice(s', halo_region(i, dims(s)))) }
      rings → for i in 0..N { recv(halos_in[i]) }
      s := await call(gs, patch, s', rings, p)
    }

  contract {
    live_handles  ≤ 2
    per_step      = N sends + N recvs + 2 calls
    recovery      = restart-only (state consumed on failure)
  }
}
```

`on_error` for this element can only use `restart`, `escalate`, `stop`. A `resume` handler is a type error.

### 12.3 Stateful element — the failsafe pattern (new in v2.0)

The worker holds state internally. Operations take only `Params`, not state. A failed call leaves the worker's last good state intact. Recovery ops act on that state.

```kappa
elem StencilWorker⟨N⟩(p : Params) {
  in  tick      : Chan[()]
  in  halos_in  : Ring[N]
  out halos_out : Ring[N]
  out stats     : Chan[Stats]              -- observed-local channel
  worker gs     : Worker⟨StencilStateful⟩
  -- no `state` field: the worker holds it

  ctrl loop_step =
    serve(tick) { _ =>
      (b_out, st) → await call(gs, "step", p)
      send(stats, st)                       -- route observability
      for i in 0..N { send(halos_out[i], b_out[i]) }
      rings → for i in 0..N { recv(halos_in[i]) }
      await call(gs, "patch", rings, p)
    }

  on_failure {
    | WorkerError(4001) => { await call(gs, "renormalize", p); resume }
    | WorkerError(4002) => { await call(gs, "reduce_timestep"); resume }
    | WorkerError(9001) => restart(max = 3, backoff = exp(0.2))
    | WorkerDied        => restart(max = 3)
    | _                 => escalate
  }

  contract {
    live_handles  ≤ 2                         -- no state; one pending Future at most
    per_step      = N sends + N recvs + 2 calls + 1 stats-send
    recovery      = resume-capable (worker holds last good state)
    deadlock_free = phase-separated
    terminates_on = close(tick)
  }
}
```

**Why this works.** The worker's `step` op:

```lambda
step → (λp p.
  -- Read current internal state (borrow)
  -- Compute into scratch; on success: commit scratch, return Ok(boundary, stats)
  -- On recoverable issue: return Err(Error(code, span, None)) WITHOUT committing
  -- On fatal issue: return Err(Error(9001, span, None))
  ...)
```

The internal state is only committed on `Ok`. On `Err`, the scratch is discarded, the last good state remains. The recovery op `renormalize` operates on that state and returns `Ok(())`. `resume` then continues the loop.

**The contract is stronger:** `live_handles ≤ 2` (no state field), `resume-capable`. The differential-element property is unchanged — one element, one coupling, parametric in `N` and `(nx, ny)`.

### 12.4 `system` — updated

```
system <Name>(value-params) {
  ports    { <port> : <ChanType> ... }
  instances { <name> : <Elem>⟨args⟩(params) for <bindings> }
  couple    { <port equations or combinator calls> }
  expose    { <op> = <Pi term> }
}
```

Unchanged except: `couple` may use library combinators (`couple_grid2d_block`, etc.) that fuse partition and topology (§14).

### 12.5 Coupling rules and combinators

Hand-written coupling for `N ≤ 8`:

```
<instance>.<port>[k] ≡ <instance>.<port>[k'] [if <guard>]
```

For `N > 8`, use a combinator. Combinators fuse partition and topology; see §14.1.

### 12.6 Nesting

A `system` is an element. Its `expose`d ops are callable. Purity degrades one step per nesting. Structural guarantees preserved; recovery semantics degrade — a nested system's `resume` is only meaningful if its inner workers are stateful.

### 12.7 Local analysis property (unchanged)

One element per `elem`, one coupling rule per `system`. Mesh size is a parameter.

**New in v2.0:** the contract must include `recovery = resume-capable | restart-only`. A `system` whose element contracts are `restart-only` inherits that; a system with `resume-capable` elements can use `resume` at the system level.

---

## 13. Sharding

### 13.1 Three modes

| Mode | Partition decided | Partition function | Use case |
|---|---|---|---|
| **Static Pi-side** | Compile time | Pi coupling rule | Grids, blocks, uniform domains |
| **Dynamic Pi-driven** | Runtime, Pi routes | Partitioner worker | Consistent hash, key-range |
| **Worker-driven** | Worker decides | Lambda worker reads `Config` | Files, Kafka, S3 |

### 13.2 Static Pi-side

The partition function is type-level:

```kappa
grid_partition_block(W, H, nx, ny) : (i, j) → Region
  = region(i * W / nx, (i+1) * W / nx, j * H / ny, (j+1) * H / ny)
```

Three sub-modes for getting the shard to the worker: **pre-split** (driver hands pieces), **worker-loads** (worker reads its own `Config`), **Pi-decomposes** (Pi slices an in-memory tensor).

The coupling follows from the partition. Library combinators fuse them (§14.1).

### 13.3 Dynamic Pi-driven

A partitioner worker implements `assign : Key → Shard`. Pi routes keys through the partitioner:

```kappa
system ShardedMap(n_workers, keys, fn) = scope {
  pool     → Pool("fn.lambda", n_workers).recruit(n_workers)
  router   → Pool("consistent_hash.lambda", 1).recruit(1)
  (routed_tx, routed_rx) → chan(cap = 64)
  await call(router, "init", n_workers, seed)
  par {
    serve(in_rx) { k => send(routed_tx, await call(router, "assign", k)) }
    ‖ serve(routed_rx) { (k, w) => send(worker_chs[w].tx, k) }
    ‖ par for (i, w) in zip(0..n_workers, pool) {
        serve(worker_chs[i].rx) { k => ... }
      }
  }
}
```

The topology (router ↔ workers) is fixed. The assignment is dynamic. Migration on failure reroutes; the router is a fixed worker.

### 13.4 Worker-driven

Each worker reads its own shard from a `Config`:

```kappa
protocol ShardedSource⟨T⟩ {
  op open  : Config → ()
  op start : Tx⟨T⟩ → ()
  op stop  : () → ()
  purity : Impure
}
```

Pi passes the `Config`; the worker reads its shard. No load balancing by Pi; use mode 2 on top for heterogeneous sources.

### 13.5 The sharding contract

| Mode | Partition | Topology | Load balance | Recovery |
|---|---|---|---|---|
| Static Pi-side | Compile time | Compile time | Static/weighted | Restart, reload shard |
| Dynamic Pi-driven | Runtime | Compile time (star) | Router policy | Router re-assigns |
| Worker-driven | Worker | Compile time (star/merge) | Source-dependent | Worker restarts, re-reads |

**Invariant across all modes:** the *topology* is compile-time. This preserves the safe fragment.

### 13.6 What sharding cannot do

Repartition the mesh at runtime; add/remove workers dynamically; partition based on data content (unless a partitioner worker is used, mode 2). These are outside the safe fragment; they require `Unsafe` or are a different problem (service mesh, not compute orchestration).

---

## 14. Standard library

### 14.1 Topology combinators (`lib/topology/`)

Fuse partition and coupling:

```
couple_grid2d_block(nx, ny, periodic)
couple_grid2d_cyclic(nx, ny, periodic)
couple_grid2d_block_cyclic(rows, cols, block_size)
couple_grid3d_block(nx, ny, nz, periodic)
couple_ring_block(n)
couple_hypercube_block(d)
couple_tree_block(arity, depth)
couple_hash_range(n_buckets, n_workers)
couple_star(k), couple_line(n), couple_dag(edges)
```

Each exports `dir_idx`, `opposite`, `neighbour`, and a partition function.

### 14.2 Collective combinators (`lib/collective/`)

`allreduce` (tree/ring/recursive-doubling), `allgather`, `reduce_scatter`, `broadcast`, `scatter`, `gather`, `scan`, `permute`, `all_to_all`.

### 14.3 Load balancing (`lib/balance/`)

`work_queue`, `work_stealing`, `work_sharing`, `block`, `cyclic`, `block_cyclic`, `guided`, `consistent_hash`, `master_worker`.

### 14.4 Synchronization (`lib/sync/`)

`barrier`, `sense_reversing_barrier`, `phase_barrier`, `rendezvous`, `token_ring`, `reader_writer`, `heartbeat`, `snapshot`.

### 14.5 Resilience (`lib/resilience/`)

`supervisor`, `circuit_breaker`, `bulkhead`, `saga`, `two_phase_commit`, `leader_election`, `quorum`, `dead_letter`, `checkpoint`, `replay`.

### 14.6 I/O (`lib/io/`)

`file_source`, `file_sink`, `stdio_source`, `stdio_sink`, `socket_source`, `socket_sink`, `array_source`, `array_sink`, `tensor_source`, `tensor_sink`, `kafka_source`, `kafka_sink`, `http_source`, `http_sink`, `sharded_file`, `sharded_kafka`, `sharded_s3`.

### 14.7 Data and text (`lib/data/`, `lib/text/`)

Structured parsers: `json`, `csv`, `msgpack`, `cbor`, `toml`. FSM-only regex: `regex_compile`, `regex_match`, `regex_find_all`, `regex_replace`. No backreferences, no PCRE lookaround — by design.

### 14.8 Diagnostics (`lib/diag/`)

Error code registry: `message_for(code, context) → String`. Span rendering: `render_span(span, source) → String`. Symbol resolution: `resolve(sym, symtab) → String`. Used by compiler workers and any worker that returns structured `Err`.

### 14.9 New: `write_file` and `array*-push` in Lambda

The distribution ships with runtime additions:

- `write_file : String × ArrayB → Result[Int]`
- `arrayb-push`, `arrayi-push`, `arrayf-push` (consume-on-write, doubling)

These are required for bootstrap and for compiler workers. They are runtime primitives, not Pi features.

---

## 15. Distribution

### 15.1 What ships

- `pi` binary (compiler + runtime + bootstrapping).
- `lambda` compiler.
- Standard protocols (IDL).
- Standard Lambda workers.
- Standard Pi combinators.
- Trace / dead-letter / telemetry infrastructure.

### 15.2 What the user writes

`.pi` files, `.lambda` files, config files (TOML or flags).

### 15.3 Build and run

```
pi build my_program.pi
pi build --profile=cyclic --target=embedded --output=firmware.bin my_program.pi
pi run my_program --src in.bin --snk out.bin
pi test my_program.pi
pi analyze my_program.pi --wcet=wcet.json
```

One build, one run, one test, one analyze.

### 15.4 Toolchain closure

A complete Pi program uses only Pi and Lambda. External driver and linker are present (as in Rust, Swift, Zig) and carry no computation logic. `spawn` would enable self-contained binaries; it is not required for bootstrap.

---

## 16. Conformance

### 16.1 Structural (unchanged from v1.0)

Scope closure, linearity, boundedness, nondeterminism localization.

### 16.2 Algebraic (unchanged)

`‖` commutative up to observation; `+` symmetric; `0` unit of `‖`; timeout distributes over scopes; `par for` order-preserving.

### 16.3 Failure laws — updated

1. **Exhaustive `on_error`.** Every `Err` constructor handled.
2. **Restart isolation.** A crashed worker under `restart` does not affect siblings.
3. **Cancel idempotent.** `cancel` then `await` yields `Cancelled`; reverse is a type error.
4. **Deadline propagation.** Fails within `deadline + ε`.
5. **Local error invisibility.** A worker with `policy = Renormalize` that recovers locally produces no `WorkerError` at Pi.
6. **Aggregate observability.** A worker returning `Ok(_, stats)` with `warnings > 0` delivers the stats to the observer channel.
7. **`resume` soundness.** A `resume` handler on a stateful worker leaves the worker's last good state intact; the serve loop continues from the next tick.
8. **`resume` rejection.** A `resume` handler on a stateless element (has `state` field) is rejected at compile time.
9. **Failsafe ladder.** For every failure, either a handler recovers, the supervisor restarts, escalation reaches a stopping scope, or the program halts with a dead-letter record. No hang.

### 16.4 Replay laws

Deterministic replay for all-`Pure` with recorded schedule; divergence marking for `Impure`.

**New:** recovery events (handler invocation, `resume`, `restart`) are recorded and reproduced in replay. A replay of a run that used `resume` reproduces the same recovery sequence.

### 16.5 Parametricity (unchanged)

Mesh size is a parameter; the analysis is invariant.

### 16.6 Scheduler profiles (unchanged)

The language semantics are identical across profiles; timing and WCET guarantees differ.

---

## 17. Worked examples

### 17.1 Gray–Scott stencil with failsafe recovery

```kappa
protocol StencilStateful⟨N⟩ {
  op step  : (Params) → (Shard, Stats)
  op patch : (HaloRing⟨N⟩, Params) → Shard
  op renormalize : (Params) → ()
  op reduce_timestep : (Params) → ()
  errors : { 4001: "div_by_zero", 4002: "diverged_step", 9001: "internal" }
  purity : Pure
}

elem StencilWorker⟨N⟩(p : Params) {
  in  tick      : Chan[()]
  in  halos_in  : Ring[N]
  out halos_out : Ring[N]
  out stats     : Chan[Stats]
  worker gs     : Worker⟨StencilStateful⟩

  ctrl loop_step =
    serve(tick) { _ =>
      (b_out, st) → await call(gs, "step", p)
      send(stats, st)
      for i in 0..N { send(halos_out[i], b_out[i]) }
      rings → for i in 0..N { recv(halos_in[i]) }
      await call(gs, "patch", rings, p)
    }

  on_failure {
    | WorkerError(4001) => { await call(gs, "renormalize", p); resume }
    | WorkerError(4002) => { await call(gs, "reduce_timestep"); resume }
    | WorkerError(9001) => restart(max = 3, backoff = exp(0.2))
    | WorkerDied        => restart(max = 3)
    | _                 => escalate
  }

  contract {
    live_handles  ≤ 2
    per_step      = N sends + N recvs + 2 calls + 1 stats-send
    recovery      = resume-capable
    deadlock_free = phase-separated
    terminates_on = close(tick)
  }
}

system GrayScott2D(rows, cols, p : Params) {
  instances { w[r,c] : StencilWorker⟨4⟩(p) for r in 0..rows, c in 0..cols }
  couple    { couple_grid2d_block(rows, cols, periodic = false) }
  expose step = <push () into every w's tick channel>
}
```

### 17.2 Embedded control loop with local-to-global error handling

```kappa
program Controller() = scope {
  sensor   → Pool("sensor.lambda",   1, profile = cyclic).recruit(1)
  filter   → Pool("filter.lambda",   1, profile = cyclic).recruit(1)
  control  → Pool("control.lambda",  1, profile = cyclic).recruit(1)
  actuator → Pool("actuator.lambda", 1, profile = cyclic).recruit(1)
  logger   → Pool("log.lambda",      1, profile = cyclic).recruit(1)

  (raw_tx, raw_rx) → chan(cap = 1)
  (filt_tx, filt_rx) → chan(cap = 1)
  (cmd_tx, cmd_rx) → chan(cap = 1)
  (log_tx, log_rx) → chan(cap = 16)
  (stats_tx, stats_rx) → chan(cap = 16)

  par {
    serve(tick_1k) @ rate(1ms) @ priority(1) @ deadline(800us) { _ =>
      v → await call(sensor, "read", ())
      send(raw_tx, v)
    }
    ‖ serve(raw_rx) @ priority(2) { raw =>
        (f, st) → await call(filter, "apply", raw)   -- Ok(v, stats)
        send(filt_tx, f)
        send(stats_tx, st)
      }
    ‖ serve(filt_rx) @ priority(3) { f =>
        c → await call(control, "compute", f)
        send(cmd_tx, c)
        send(log_tx, (f, c))
      }
    ‖ serve(cmd_rx) @ priority(4) { c =>
        await call(actuator, "apply", c)
      }
    ‖ serve(log_rx) @ rate(100ms) @ priority(5) { pair =>
        await call(logger, "write", pair)
      }
    ‖ serve(stats_rx) @ priority(6) { st =>
        if st.warnings > 100 { send(alert_tx, st) }
      }
  }
}
```

The filter worker returns `Ok(f, stats)`. Pi routes stats to the observer, which may alert. Local numerical issues are handled by the filter (never seen by Pi). Fatal issues surface as `WorkerError` and are handled by `on_failure` (not shown, but present in each worker's element).

### 17.3 MD with boundary conditions and recovery

Same `MDPartWorker` element applies to five boundary types via different coupling rules; each boundary type may declare a different `Params.policy`.

| Boundary | Coupling | Recovery |
|---|---|---|
| Free | `couple_grid2d_block` with guards | `restart` on `WorkerDied` |
| Periodic | `couple_grid2d_block(periodic=true)` | `resume` on div-by-zero |
| Smooth wall | Grid + wall `Params` | `resume` with wall force recompute |
| Discrete wall | Grid + `WallLayer` elements | `restart` wall element |
| Open | Grid + `Source`/`Sink` | `stop` on sink failure, `resume` on source |

### 17.4 3D stencil with combinator

```kappa
system GrayScott3D(nx, ny, nz, p : Params) {
  instances { w[i,j,k] : StencilWorker⟨26⟩(p) for i in 0..nx, j in 0..ny, k in 0..nz }
  couple    { couple_grid3d_block(nx, ny, nz, periodic = true) }
  expose step = <push () into every w's tick channel>
}
```

One line replaces 26 equations.

### 17.5 DDP training with gradient clipping recovery

```kappa
program DDP(model, data, steps) = scope {
  ws → Pool("trainer.lambda", 8, {cpus: 1, memory: "4GiB"}).recruit(8)
  shards → split(data, 8)
  iterate(steps) {
    grads → par for (w, shard) in zip(ws, shards) {
              on_error(await call(w, "backward", shard)) {
                | WorkerError(7001) => {          -- gradient explosion
                    await call(w, "clip_gradients", 1.0)
                    resume
                  }
                | WorkerDied => restart(max = 3)
                | _         => escalate
              }
            }
    allreduce(sum, grads) @ tree
    par for (w, _) in zip(ws, [()]*8) { await call(w, "apply", grads) }
  }
}
```

`clip_gradients` is the recovery op. `resume` continues the backward pass with clipped gradients.

---

## 18. Properties

**P1. Linearity.** Every linear handle consumed exactly once.

**P2. Scope closure.** Every process terminates at enclosing scope exit.

**P3. Boundedness.** Every channel has finite capacity.

**P4. Nondeterminism localization.** Only `+` and `select` choose.

**P5. Total error handling.** Every `Err` handled; retries gated on retryability.

**P6. Cascade termination.** `close` cascades deterministically to `serve` consumers.

**P7. Local analysis.** Correctness from one element per kind and one coupling rule.

**P8. Coarse seam.** Crossings `O(workers × steps)`; overhead `O(RPC / per_worker_compute)`.

**P9. Visible cost.** Cost determined by syntax.

**P10. Toolchain closure.** Pi + Lambda only; external driver and linker carry no computation logic.

**P11. Scheduler profiles.** Semantics identical across cyclic, M:N, priority-based.

**P12. Catalog coverage.** Standard library combinators and protocols cover §19 patterns without new primitives.

**P13. Three-tier error model.** Every error has a designated handler: local (Lambda), aggregate (Lambda detects, Pi observes), or fatal (Lambda returns `Err`, Pi decides).

**P14. Failsafe ladder.** For every failure: recover, restart, escalate, or halt cleanly with dead-letter. No hang.

**P15. State ownership determines recovery.** Stateful workers support `resume`; stateless elements support only `restart`.

**P16. Backend honesty.** CPU-threaded; GPU declarations fail with `Unsupported`.

---

## 19. Coverage

### 19.1 Patterns covered

| Category | Patterns |
|---|---|
| Topological coupling | Grid (2D/3D, Moore/von Neumann, periodic), ring, torus, hypercube, tree, bipartite, graph, line, star, DAG |
| Sharding | Static (block, cyclic, block-cyclic), dynamic (hash, consistent hash), worker-driven |
| Collectives | Allreduce (tree/ring/recursive-doubling), allgather, reduce-scatter, broadcast, scatter, gather, scan, permute, all-to-all |
| Load balancing | Work queue, work-stealing, work-sharing, block, cyclic, block-cyclic, guided, consistent hash, master-worker |
| Synchronization | Barrier, sense-reversing, phase, rendezvous, token ring, reader-writer, heartbeat, snapshot |
| Resilience | Supervisor, circuit breaker, bulkhead, saga, two-phase commit, leader election (static peers), quorum, dead-letter, checkpoint, replay |
| Numerical/ML (CPU) | Stencil, multigrid, domain decomposition, Schwarz, FFT, sparse MV, dense linear algebra, PIC, N-body, MoE, DDP, pipeline parallelism, tensor parallelism, RL rollouts, federated averaging |
| I/O | File, stdio, socket, array, tensor, Kafka, HTTP, sharded sources/sinks |

### 19.2 Patterns out of scope

Dynamic membership, first-class process handles, consensus with dynamic leaders, gossip, unbounded-state joins, hard real-time below Pi, interactive/UI, web services, GPU compute (reserved), general text/data parsing beyond FSM regex.

### 19.3 Coverage claim

Pi + Lambda covers a **strong majority (70–90%)** of CPU-parallel compute orchestration workloads — scientific simulation, ML training and inference infrastructure on CPU, ensemble methods, batch processing, streaming ETL with compute stages, fault-tolerant work queues. It covers a **minority (10–20%)** of all software. It does not cover the domains it was never designed for. GPU workloads are a future Lambda feature; the design does not depend on them.

---

## 20. Versioning and future work

### 20.1 Version policy

Syntax: minor. Semantics: major. New protocols/combinators: minor. Conformance preserved across minors.

### 20.2 Open items for v2.1+

**`arrayf-pmap` in Lambda.** Blocks GPU lowering for `Float` kernels; when added, GPU becomes a valid `place` and `res = {gpus, vram}` becomes meaningful. Pi's design is unchanged.

**`spawn` in Lambda.** Self-contained compiler binary; not required for bootstrap.

**Full `resume` semantics for nested systems.** A `system` with mixed stateful/stateless elements needs a composite recovery contract.

**`contract` checking.** Machine-verified `live_handles ≤ k` and `resume-capable` from linear typing plus `ctrl` structure.

**Adapter contract for non-Lambda backends.** Structural tier (linear handles, typed ops, closed errors) vs purity tier (Pure/Deterministic/Impure). v2.0 targets Lambda only.

**WCET analyzer.** Static analysis tool for Lambda kernels.

**Scheduler profile composition.** Mixing profiles at `Source`/`Sink` boundaries; inter-profile timing guarantees.

**Recovery replay determinism.** Current: records recovery events. Target: full replay of recovery path.

---

## 21. Glossary

| Term | Meaning |
|---|---|
| **Element** | One instance of an `elem` template |
| **Coupling** | The parametric assembly rule that wires ports |
| **Coupling combinator** | Library function generating coupling equations from a direction set |
| **Sharding** | The partition of a global domain into per-worker pieces |
| **Static sharding** | Partition fixed at compile time; coupling follows |
| **Dynamic sharding** | Assignment decided at runtime by a partitioner worker |
| **Worker-driven sharding** | Worker reads its own shard from a `Config` |
| **Mesh** | An instantiation of elements at a given size |
| **Linear handle** | A value consumed exactly once |
| **Scope** | A structured region; children joined or cancelled at exit |
| **Cascade** | Deterministic termination following `close` |
| **Purity** | Worker's effect class: `Pure`, `Deterministic`, `Impure` |
| **Protocol** | IDL declaration of a worker's operations and error codes |
| **Error code** | Integer in `WorkerError(Int)`; declared in protocol `errors` |
| **Three-tier error model** | Local (Lambda), aggregate (Lambda detects, Pi observes), fatal (Lambda returns `Err`, Pi decides) |
| **Stateful worker** | Element without `state` field; worker holds state internally; supports `resume` |
| **Stateless element** | Element with `state` field; Pi holds state; supports only `restart` |
| **`resume`** | Recovery primitive: handler runs, then serve loop continues with last good state |
| **Failsafe ladder** | Local → aggregate → recovery op → resume → restart → escalate → stop |
| **Profile** | Scheduler configuration: cyclic, M:N, priority-based |
| **WCET** | Worst-case execution time |
| **RTA** | Response-time analysis |

---

*End of specification v2.0.*