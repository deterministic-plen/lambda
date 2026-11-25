# Pi AOT

`pi aot` compiles a **linear Pi program** to a native C executable.  There is no
Python interpreter in the resulting process: the generated program spawns the
Lambda workers, writes requests, reads `LMB1` replies, loops, and writes CSV.

```powershell
pi aot examples/gray_scott.pi -o out/gray_scott --out out/gray_scott-data
out\gray_scott.exe
```

The Lambda workers are built first (with their protocol schema) and their
executable paths are embedded in the AOT binary.

## What is compiled

| construct | AOT |
| --- | --- |
| one program, `scope`, sequential bindings | ✅ |
| `Pool(...).recruit(1)` | ✅ one worker |
| `await call(w, "op", scalars/payloads)` | ✅ direct C call |
| `iterate(n) { ... }` | ✅ C `for` loop |
| `after(d) { ... }` | ✅ `Sleep`/`usleep` |
| one static `serve(tick) @ rate(d)` task | ✅ bounded by `--steps N` |
| `emit`, `emit_text`, `emit_series`, `emit_grid`, `emit_animation_frame` | ✅ text / CSV / final-frame CSV |
| scalar arithmetic, `payload(range(...))`, payload indexing | ✅ |
| program parameters | ❌ specialize the source |
| elements/systems, channels, `serve`, `select`, `par`, `on_error`, recovery | ❌ interpreter only |

`serve` is compiled as a static loop with the declared period; use
`--steps N` to bound it.  Multiple concurrent `serve` tasks are not implemented.
Unsupported constructs fail at `pi aot` time with a location, not at run time.

## Native runtime

`pilang/aot_runtime.c` implements the Lambda–Pi seam in C:

- spawn one worker (Windows `CreateProcess`, POSIX `fork/exec`);
- set `LAMBDA_GPU=1` for a worker whose pool requested `{gpus: 1}`;
- read HELLO, then write `corr / op-index / nargs / floats`;
- read a 16-byte `LMB1` header and f64 body;
- decode `Ok` / `Ok+stats` / `Err` (errors terminate the AOT program);
- write series/grid CSV and the last animation frame.

There is no Python, no asyncio, and no GC in the AOT process.  Worker processes
are still native Lambda executables.

## Generated code

`--keep-c` retains `NAME.aot.c`.  The C runtime is prepended to the generated
`main`, so the output is a single translation unit:

```c
int main(void) {
    pi_set_output("out/data");
    PiVal v_w = pi_spawn("…/gray_scott-….exe", 1);
    (void)pi_call((v_w).worker, 0, 1, (PiVal[]){pi_num(0.0)});
    for (long __i = 0; __i < pi_as_int(pi_num(30.0)); ++__i) {
        PiVal v_v = pi_call((v_w).worker, 1, 0, NULL);
        (void)pi_emit_animation_frame("gray_scott", v_v,
                                      pi_as_int(pi_num(256.0)), pi_as_int(pi_num(256.0)));
    }
    pi_finish();
    return 0;
}
```

## Timing

Reference machine, native AOT, field 200×200 and Gray-Scott 1500 steps:

| example | native AOT | interpreted |
| --- | ---: | ---: |
| `field.pi` | ~0.07 s | ~0.2 s |
| `drone.pi` (200 ticks, sleeps 5 ms) | ~1.6 s | ~1.1 s |
| `gray_scott.pi` (1500 steps, 30 frames) | ~0.3 s | ~0.8 s |

The drone is dominated by its 200 × 5 ms `after` sleeps; the compute loop is
native.  These are wall-clock numbers, not WCET proofs.

## What AOT does not fix

AOT removes the Python interpreter from the loop, but a worker call is still a
blocking process round-trip.  There is no static task table, deadline enforcement,
recovery ladder, or per-task WCET accounting yet.  Those are the next pieces of
the cyclic-executive target; this AOT path is the native base they build on.
See [embedded.md](embedded.md).
