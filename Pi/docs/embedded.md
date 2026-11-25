# Pi on embedded targets: WCET and the cyclic executive

Pi's coordination semantics are a good match for a drone: a small fixed set of
periodic control tasks, explicit deadlines, structured cancellation, and
fail-safe recovery.  The current **implementation** is not: it is a Python
interpreter on asyncio.  This note separates the language design from the host
runtime and defines the AOT path.

## Why the interpreter is not a WCET platform

1. **No static task table.** `serve(tick) @ rate(5ms)` creates a coroutine and a
   timer at run time.  The set of runnable tasks is dynamic from the
   scheduler's point of view.
2. **Unbounded host operations.** Python object allocation, refcount/finalizer
   work, and the GIL make per-iteration time data-dependent.  Pi itself has no
   GC, but the host does.
3. **No stack bound on the host.** The interpreter's Python stack is not what a
   WCET analyser can bound; Lambda worker calls are separate processes, but the
   coordination loop still allocates.
4. **`@deadline` is a check, not a proof.** It aborts an overrun; it does not
   prove the next iteration fits.

Empirical timing is still useful: `pi run --out DIR` records min/mean/p95/max
per `(worker, op)` in `results.json` and `report.html`.  It is a **measurement
and a bound candidate**, not a WCET certificate.

## AOT cyclic-executive target (the intended fix)

`pi build --profile=cyclic --target=embedded` should emit a static executive:

```
task table:
  id | worker | op | period | deadline | priority | stack | max frame
  0  | drone  | tick | 5 ms  | 4 ms     | 10       | ...   | ...
  1  | logger | write| 100 ms| 90 ms    | 5        | ...   | ...
```

Generated loop, once per tick:

```
for each tick:
    for task in sorted(table, priority):
        if task.next_release <= now:
            dispatch(task)          # no allocation in the loop
            task.next_release += task.period
    sleep until next release
```

Properties this buys:

* **Static task set** — the scheduler is a table, not a dynamic coroutine graph.
* **Bounded dispatch** — fixed-size task records, no interpreter allocations.
* **Deadline semantics** — an overrun enters Pi's existing `on_error` ladder;
  `Deadline` maps to `restart` or `stop` by policy.
* **WCET input** — each task's Lambda worker is already AOT-compiled; WCET is
  per op, measured/analysed once, then summed with dispatch overhead:
  `WCET_tick = Σ WCET_i + overhead`.

This is also the natural split for a drone: Pi is the **static executive**, Lambda
is the **bounded compute kernel**.  No dynamic membership, no runtime routing,
no goroutine growth.

A first native step now exists: [`pi aot`](aot.md) compiles linear programs
(pools, calls, loops, one static `serve @ rate` task) to C and runs without
Python.  It still blocks on worker round-trips and has no task table, deadlines,
or recovery, so it is the base for the cyclic executive rather than the full
target.

## What is required from Pi the language

Already present and tested:

- structured scopes, linear handles, bounded channels;
- `@rate`, `@deadline`, `@priority` (validated; rate/deadline enforced);
- error taxonomy and the failsafe ladder;
- deterministic error/recovery trace.

Still needed for the AOT target:

- a **static task table extraction** pass (periods and priorities are known at
  compile time);
- **dispatch IR/code generation** (C or Rust backend) instead of asyncio;
- **worker ABI with fixed frame sizes** and no per-call dynamic allocation;
- **WCET analysis** of Lambda workers (loop bounds, array bounds, no unbounded
  allocation) and a link step that rejects tasks exceeding their deadline.

The periodic `serve @ rate` shape is still a design target, not a runnable
interpreter feature; use `pi run --out` for empirical timing of the current
sequential/tiled examples.
