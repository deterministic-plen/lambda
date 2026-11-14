# Pi — a coordination language for Lambda compute

**Pi is a small, statically structured coordination language for orchestrating
Lambda workers. It does not compute payloads: it names resources, wires
channels, sequences phases, and decides what happens when a worker fails.**
Lambda performs the numerics; Pi routes handles, schedules tasks, and records
results.

This repository is the reference implementation for [Pi Specification
v2.0](../pispec.md). It includes a native **AOT compiler for linear programs**
(`pi aot`) and a C++ AST runtime for the full language (elements, systems,
scheduling, recovery).

```
             ┌──────────── Pi (coordination) ─────────────┐
  inputs ───▶│ scopes · channels · workers · schedules    │───▶ results
             │ errors · recovery · artifacts              │
             └───────────────────┬────────────────────────┘
                                 │ typed seam: call(op, args) / Result
             ┌───────────────────▼────────────────────────┐
             │ Lambda workers (AOT native processes)       │
             │ Kalman filters, stencils, bigint, arrays …  │
             └────────────────────────────────────────────┘
```

## Design goals

1. **Structural reliability.** Scopes own children; linear handles are consumed
   once; channels are bounded; failures cannot be silently dropped.
2. **A coarse seam.** Crossings scale with `workers × steps`, not `cells ×
   steps`. A worker owns a slice; Pi never sees individual elements.
3. **Local analysis.** Verify one `elem` contract and one coupling rule; the
   mesh size is a parameter.
4. **Total error handling.** Every error is classified local, aggregate, or
   fatal, and the layer with information and authority handles it.
5. **Failsafe ladder.** Local correction → aggregate warning → recovery op →
   `resume` → `restart` → `escalate` → clean `stop`. No hangs.
6. **Visible cost.** Message, handle, and call counts are readable from source;
   allocations are explicit.
7. **Backend honesty.** Lambda is CPU-native. Pi's optional GPU path is one
   device and `arrayf-pmap` only. Hard WCET requires an AOT build.
8. **Small language.** No arithmetic on payloads, no reflection, no dynamic
   topology, no first-class process handles, no ambient I/O.

## Quick start

Requirements: Python ≥ 3.11, `clang`, and a Lambda checkout.

```powershell
cd pi
$env:LAMBDA_HOME = "..\..\lambda"       # or your checkout
python -m pilang check examples/heat.pi
python -m pilang build examples/heat.pi
python -m pilang run examples/heat.pi
python -m pilang run examples/heat.pi --out out/heat
```

On Linux/macOS use `./pi` (a thin `python -m pilang` wrapper).

| command | purpose |
| --- | --- |
| `pi check FILE` | parse and static checks |
| `pi build FILE` | compile the Lambda workers a program names |
| `pi run FILE [--out DIR]` | execute through the C++ AST runtime |
| `pi test PATHS` | run `.pi` test programs |
| `pi analyze FILE` | schemas, recovery capability, static ops, scheduler table |
| `pi aot FILE -o EXE` | compile the linear subset to a native C executable |
| `pi cpp FILE` | alias for the C++ runtime |

## Language

### Modules and declarations

A module has imports, protocols, elements, systems, functions, and programs.
Protocols require an `errors` map and a purity class:

```kappa
protocol Drone {
  op init : (Float) → ()
  op tick : () → Shard
  errors : { 10001: "unknown_op" }
  purity : Pure
}
```

### Processes

```kappa
program Main() = scope {
  w → Pool("workers/drone.lambda", 1, {cpus: 1}).recruit(1)
  await call(w, "init", 0.0)
  iterate(200) {
    await call(w, "tick")
    after(5ms) { () }
  }
  xs → await call(w, "history_x")
  emit_series("position", xs)
}
```

Core forms: `scope { p }`, `p ; q`, `p ‖ q`, `p + q` (race), `select`,
`serve`, `loop`, `iterate`, `after`, `call`/`await`, `send`/`recv`/`close`,
`on_error`, `fail`, and `for`/`par for`.

### Scopes and linear handles

`scope` is the unit of structure. Normal exit joins children; abnormal exit
cancels them. `Worker`, `Pool`, `Chan`, `Future`, `Tensor`, and `Shard` are **linear handles**, consumed exactly once by `call`, `send`,
`recv`, `await`, `cancel`, `close`, `drop`, or scope exit. `Future` is affine.
Duplicating or silently discarding a handle is rejected.

### Errors and recovery

| tier | example | handled by | crosses seam? |
| --- | --- | --- | --- |
| local | one cell divides by zero; policy clamps it | Lambda | no |
| aggregate | 30% of cells diverged; stats warning | Lambda detects, Pi observes | `Ok(value, stats)` |
| fatal | invariant broken; worker cannot proceed | Pi `on_error` | `Err(code)` |

```kappa
on_error(await call(w, "step", p)) {
  | WorkerError(4001) ⇒ { await call(w, "renormalize", p); resume }
  | WorkerError(9001) ⇒ restart(max = 3, backoff = exp(0.01))
  | WorkerDied        ⇒ restart(max = 3)
  | _                 ⇒ escalate
}
```

`resume` is only sound for a **stateful worker** (element has no `state` field):
the worker commits on success and keeps its last good state on failure. A
stateless element (Pi holds `state`) is `restart`-only; `resume` is a compile
error. Recovery ops get half the deadline budget.

### Elements, systems, coupling

```kappa
elem HeatWorker⟨N⟩(p : Params) {
  in  tick : Chan[()]
  in  halos_in : Ring[N]
  out halos_out : Ring[N]
  worker gs : Worker⟨HeatStateful⟩
  ctrl loop = serve(tick) { _ ⇒ … }
  on_failure { | WorkerDied ⇒ restart(max = 3) | _ ⇒ escalate }
  contract { recovery = resume-capable }
}

system HeatChain(n, p : Params) {
  instances { h[i] : HeatWorker⟨2⟩(p) for i in 0..n using { gs: "workers/heat1d.lambda" } }
  couple { couple_line(n) }
  expose step = <push () into every h's tick channel>
}
```

Implemented combinators: `couple_line`, `couple_ring_block`,
`couple_grid2d_block`, `couple_grid3d_block` (von Neumann/Moore, periodic).
`allreduce(sum|max)`, `split`, and `zip` are available; the rest of the §14
catalog is planned, not shipped.

## The Lambda seam

A worker is an ordinary Lambda program compiled with `--input --data`. Pi starts
it as an OS process:

* Startup `HELLO`: i64 frame `[schema_hi, schema_lo, version]`. A worker built
  against another protocol hash is rejected with `BadFrame`.
* Request (stdin, line oriented): `corr`, op index, argument count, floats.
* Reply (stdout, one `LMB1` f64 frame per request):
  * `Ok`: `[0, values…]`
  * `Ok + stats`: `[1, k, stats…, values…]`
  * `Err`: `[2, code, span_start, span_end, sym]`

The transport unwraps the result: `Ok` yields values; `Err` raises
`WorkerError(code)`. Full wire format, frame layout, argument flattening and timeout semantics: [`docs/protocol.md`](docs/protocol.md). `pilang/lib/pi_worker.lambda` is the Lambda-side shim;
workers include it and a generated `pi_schema_<Protocol>.lambda`.  The schema
file also defines readable `op-<name>` and `error-<name>` constants, so worker
dispatch says `(== op op-step)` and `error-unknown_op` instead of magic
integers.

`Pool` resources are `{cpus, memory}` plus optional `{gpus: 1}`. A GPU resource
sets `LAMBDA_GPU=1` for that worker; only `arrayf-pmap` uses it. Multiple GPUs
and explicit VRAM budgets are rejected as `Unsupported` (a one-GPU extension to
the v2.0 core, which reserved GPU).

* [AOT](docs/aot.md) — native subset, generated code, limits.
* [Wire protocol](docs/protocol.md) — HELLO, requests, frames, Result/error encoding, timeouts.
* [Writing a worker](docs/worker.md) — the minimal `.lambda` protocol shape.

## Results and plotting

Pi does not compute with payloads, but it can name them:

```kappa
emit_series("temperature", xs, ys)
emit_grid("field", values, 200, 200)
emit_text("status", "converged")
```

With `--out DIR`:

```
DIR/results.json     artifact manifest (names and kinds)
DIR/<name>.csv       series: x,y; grid: row,col,value
DIR/<name>.svg       line plot or heatmap (standalone, no dependencies)
DIR/report.html      one self-contained page
```

`emit` still prints small values; payloads above 32 values are abbreviated.
Reporting is off unless `--out` is given, so the coordination path pays nothing
for it.

![Gray-Scott, 4 Pi workers](examples/gray_scott_multi_512.gif)

The four-worker Gray-Scott example runs 3000 Euler steps at 512×512 and emits
one frame every 50 steps.  The GIF above was produced by `gray_scott_multi.pi`
through the C++ runtime; `gray_scott.pi` is the single-worker GPU variant.

## Examples

| example | shows |
| --- | --- |
| `examples/heat.pi` | 1D heat chain, stateful workers, resume/restart, snapshots |
| `examples/reduce.pi` | pool + `par for` + allreduce over real Lambda workers |
| `examples/field.pi` | `arrayf-pmap` field, heatmap report |
| `examples/drone.pi` | simulated drone, scalar Kalman filter, controller |
| `examples/drone_scheduled.pi` | controller as a periodic `serve @ rate` task |
| `examples/report_demo.pi` | smallest artifact-producing program |
| `examples/heat2d_cells.pi` | 4-neighbour halo grid; one local update per cell |
| `examples/gray_scott.pi` | GPU-backed Gray-Scott, single worker, 512×512 |
| `examples/gray_scott_multi.pi` | Gray-Scott on four stripe workers, 512×512, 60-frame animation |

## C++ runtime

`pi cpp FILE` parses/checks Pi and serializes a plan, then runs it in the C++
AST interpreter at [`csrc/pi_runtime.cpp`](csrc/pi_runtime.cpp).  The Python process is only
the frontend; worker calls, loops, and emit artifacts execute in C++.

```powershell
pi cpp examples\gray_scott.pi --out out\gray-scott
python tools\check_cpp_examples.py      # C++ regression gate: all examples
```

Current C++ coverage is the linear core plus channels, `par`, `par for`, `select`,
`serve`, basic `on_error` and element `on_failure` with worker restart/replay/resume, 2D/ring
`elem`/`system` coupling with type/value parameters, `split`/`zip`/`allreduce`
(`reduce.pi`), stateful tuple returns, and multi-worker Gray-Scott with CSV/SVG/HTML
reports.  All repository examples now run through `pi cpp`.  Full recovery budgets,
scope cancellation, and the complete standard library are implemented in the C++
runtime.  The 512×512/60-frame GIF above is produced by
`gray_scott_multi.pi` through this C++ runtime, not by a separate C++ simulation.

## AOT (native)

`pi aot FILE -o EXE [--out DIR]` compiles the linear subset — one program,
`scope`, bindings, one-worker pools, `await call`, `iterate`, `after`, one static
`serve @ rate` loop, and the emit/report calls — to a native C executable.  The generated program speaks the
worker protocol directly; no Python runs in the loop.  Worker paths are built
first and embedded, and `{gpus: 1}` is honored.

See [AOT details](docs/aot.md) for the supported subset, generated code, and timings.
Elements/systems, channels, `serve`, `select`, `par`, recovery, and program
parameters are still interpreter-only.

## Scheduling and WCET

`@rate`, `@deadline`, and `@priority` are validated: non-positive values and
`deadline > rate` are rejected; priority range is checked; `@sporadic` with
`@rate` and `@periodic` without `@rate` are warned. `pi analyze` prints the
static task table.

The runtime is **soft real-time only**. It allocates and has no bounded
dispatch. `@rate` is a drift-compensated timer, `@deadline` is a per-iteration
budget, and priorities are recorded, not enforced by an RTOS. Hard WCET requires an AOT cyclic executive with a static
task table and generated dispatch. See
[`docs/embedded.md`](docs/embedded.md); `pi run --out` timing is empirical input,
not a proof.

## Implementation status

| area | status |
| --- | --- |
| lexer/parser, Unicode + ASCII operators | ✅ |
| scopes, channels, linearity (dynamic + straight-line) | ✅ |
| `call/await`, pipelining, cancellation, deadlines | ✅ |
| error taxonomy, `errors`, schema hashes | ✅ |
| `on_error`, `restart`/`resume`/`escalate`/`stop`, ladder | ✅ |
| `elem`, `system`, `instances`, `couple`, `expose` | ✅ |
| stateful/stateless worker patterns | ✅ |
| static block grids, 4 topology combinators | ✅ |
| `allreduce`, `split`, `zip` | ✅ minimal |
| result artifacts and HTML/SVG reports | ✅ |
| animation artifacts (`emit_animation_frame`) | ✅ |
| GPU-backed Gray-Scott primitive (one device) | ✅ experimental |
| scheduler checks + task table | ✅ static `pi analyze` |
| AOT linear subset (native C, no Python) | ✅ `pi aot` |
| C++ AST interpreter (channels/par/serve/par-for/select, systems, collectives, reports) | ✅ `pi cpp` |
| multi-worker Gray-Scott via Pi system | ✅ 4 stripe workers |
| full C++ Pi interpreter | ✅ static suite + all C++ examples pass |
| AOT full Pi / cyclic executive | ❌ linear subset only |
| WCET proof | ❌ design only |
| full §14 catalog | ❌ partial |

## Limitations

* **AOT is a linear subset.** Native C covers one program, pools, calls, `iterate`, `after`, and reports; elements/systems, channels, scheduling, recovery, and WCET remain interpreter work.
* **Static topology.** No dynamic membership, repartitioning, or first-class
  process handles.
* **No payload computation.** Pi routes `Payload`; payload arithmetic is a
  runtime fault. Coordination metadata arithmetic is allowed.
* **One-GPU, one primitive.** GPU is an extension for `arrayf-pmap`, not a GPU
  language; multi-GPU is not implemented.
* **No PING/PONG.** Liveness is process-level; a running call cannot be
  interrupted, only the worker can be killed at scope exit.
* Python 3.10 is best-effort; target is ≥3.11 (frontend only).

## Layout and tests

```
pi                    launcher
pilang/               lexer parser checker plan worker cpp aot cli
pilang/lib/           pi_worker.lambda (Lambda side of the seam)
examples/             heat, reduce, field, drone, gray_scott, heat2d
tests/                static checker tests
docs/protocol.md      Lambda–Pi wire format
docs/worker.md        minimal worker guide
docs/aot.md           native AOT subset
docs/embedded.md      WCET/cyclic-executive plan
../pispec.md          Pi Specification v2.0
```

```sh
python3 -m pytest tests        # 17 static checker tests
python tools/check_cpp_examples.py   # C++ regression gate: all examples
```

The static suite covers syntax, linearity, error/recovery laws, scope rules,
and scheduling validation. The C++ gate covers real Lambda workers, schema
checks, systems, channels, recovery, reports, and Gray-Scott.

## Editor support

The shared extension at [`editors/vscode`](../../editors/vscode)
contributes `.pi` (`source.pi`) alongside `.lambda`: comments `--` / `{- -}`,
keywords, handle types, declarations, scheduler attributes, error constructors,
and completion for Pi builtins and local definitions.  Install it from the
Lambda checkout:

```powershell
powershell -ExecutionPolicy Bypass -File ../../editors/install_vscode.ps1
```

```bash
sh ../../editors/install_vscode.sh
```

Fully restart VS Code, then open any `.pi` file.  No build step or language
server is needed.  Pygments users can import `pi_lexer.PiLexer`; validate with
`python ../../editors/test_pi_highlight.py`.

## Documentation

* [Pi Specification v2.0](../pispec.md) — normative design.
* [Embedded/WCET plan](docs/embedded.md) — interpreter limits and AOT target.
* [Lambda README](../../README.md) — the compute language.
