# The Lambda–Pi worker protocol

This is the complete wire contract between a Pi interpreter and a Lambda worker.
Pi owns the process, channels, recovery, and deadlines.  Lambda owns one request
at a time and one piece of state.  Nothing else crosses the seam.

```
Pi Worker.call(op, args)                         Lambda worker
  │  corr, op-index, nargs, floats  (stdin)        │
  ├───────────────────────────────────────────────▶│  pi-next-call
  │                                                │  pi-read-floats
  │                                                │  local op
  │  one LMB1 frame  (stdout)                      │  pi-reply-ok / stats / err
  │◀───────────────────────────────────────────────┤
  │  decode Result → Payload / Stats / PiError     │  loop
```

## 1. Startup: HELLO

The worker writes exactly one **i64** frame on stream id `0`:

```
LMB1 | tag=2 | sid=0 | n=24 | [schema_hi, schema_lo, version]
```

- `schema_hi`, `schema_lo`: two signed 64-bit words of the protocol's 128-bit
  schema hash (big-endian bytes of SHA-256(canonical signature), first 16 bytes,
  then `>qq`).
- `version`: currently `2`.

Pi rejects a frame with another stream/tag, or with a hash different from the
protocol it built the worker against:

```
BadFrame: schema hash mismatch (worker 7121…, protocol 48f5…)
```

The schema hash covers the **protocol signature** (op names, argument/return
types, errors, purity), not worker comments, op constants, or implementation.
A comment-only worker change does not invalidate the hash.

`pi_schema_<Protocol>.lambda` contains the hash plus readable constants:

```lambda
schema-hi → <signed i64> ; generated, not hard-coded
schema-lo → <signed i64>
op-init → 0
op-step → 1
error-unknown_op → 10001
```

The constants are generated from declaration order and the protocol's `errors`
map.  Use the names; do not hard-code indices.

## 2. Request

`Worker.call` flattens the typed arguments and writes **one UTF-8 line-oriented
request** to the worker's stdin:

```
<corr>
<op-index>
<nargs>
<float 0>
...
<float nargs-1>
```

- `corr` is a positive Pi-side correlation id, unique per worker generation.
- `op-index` is the declaration index in the protocol, not a name.
- `nargs` is the number of flattened f64 values.

The worker's `pi-next-call` reads the first three lines into an `ArrayI`; EOF
returns `corr = -1`.  `pi-read-floats` reads `nargs` values into an `ArrayF`.

### Argument flattening

Pi values are flattened by the transport, not by the worker:

| Pi value | wire values |
| --- | --- |
| `()` | none |
| `Bool` | `0.0` or `1.0` |
| `Int`, `Float` | one f64 |
| `Payload` | `[length, values…]` |
| `Params` | `[count, values in sorted key order]` |
| policy (`Renormalize`/`Report`/`Halt`) | one code |
| list/tuple | each element flattened in order |

Consequences worth knowing:

- A `HaloRing` of four one-value payloads becomes
  `[1, left, 1, right, 1, up, 1, down]` — eight values.  Existing heat workers
  read the length-prefixed positions directly.
- The new `scalar(payload)` Pi helper extracts a one-value payload so a cell can
  send four ordinary `Float` arguments instead; the worker then reads indices
  `0..3`.
- Strings cannot cross the seam (`BadFrame`).  Pi is not a serialization
  framework.

## 3. Reply

The worker writes exactly one **f64** frame on stream id `corr`:

```
LMB1 | tag=1 | sid=corr | n=8*count | double[count]
```

The first value is the Result tag:

| tag | body | decoded by Pi as |
| --- | --- | --- |
| `0` | `[0, values…]` | `Ok(values)` → `Payload` (or `Unit` if return is `()`) |
| `1` | `[1, k, stats…, values…]` | `Ok(values, Stats)`; return type must be a pair |
| `2` | `[2, code, span_start, span_end, sym]` | `Err(WorkerError(code))` |

`Stats` is `(warnings, max_error, extra…)`; `k` is the number of stats values.
`sym` is `-1` when absent.

`pi-reply-ok`, `pi-reply-stats`, and `pi-reply-err` build these bodies.  A
protocol return type of `()` decodes to `Unit`; a pair return requires tag `1`.
Returning an undeclared error code is a protocol violation: Pi raises
`BadFrame`, not `WorkerError`.

## 4. Frame format

All frames are 16-byte little-endian headers:

| offset | bytes | meaning |
| --- | --- | --- |
| 0 | 4 | ASCII `LMB1` |
| 4 | 1 | payload tag: `1` = f64, `2` = i64 |
| 5 | 3 | padding |
| 8 | 4 | stream id / correlation id (`<I`) |
| 12 | 4 | payload length in bytes (`<I`) |

The body is `n/8` doubles (tag 1) or int64s (tag 2).  Pi reads exactly `n` bytes;
a short read is `WorkerDied`.

HELLO is the only i64 frame in normal operation.  Requests are text on stdin;
replies are binary frames on stdout.  Stderr is reserved for worker diagnostics
and is discarded by the supervisor by default.

## 5. Errors, timeouts, cancellation

| situation | Pi-side result | worker |
| --- | --- | --- |
| worker returns `Err(code)` | `WorkerError(code, span, sym)` | may continue; state must be last-good |
| declared op missing | `BadFrame` before sending | untouched |
| schema mismatch | `BadFrame` at startup, process killed | killed |
| malformed/absent frame | `BadFrame` / `WorkerDied` | killed on death |
| call exceeds deadline | `Deadline` | killed if the timed-out branch was blocked in the call, then restarted by policy |
| task explicitly cancelled | `Cancelled` | not killed; call abandoned |
| worker exit | `WorkerDied` | supervised restart/replay if policy allows |
| memory budget exceeded | `Rejected(Memory)` before write | untouched |

A timeout cancels the timed-out branch before the `Deadline` error is delivered.
If that branch was blocked inside a worker call, its worker process is killed
and recorded on the `Deadline` error, so `| Deadline ⇒ restart(max = 1)` starts
a fresh worker; a late reply can therefore never be read by an unrelated call.
Pi cannot interrupt a running native call any other way.  At scope exit, the
worker is stopped; the recovery ladder decides whether a `WorkerDied` is
restarted.

`Err` is the only way a worker reports a protocol-level failure.  Local
numerical correction should stay local (clamp/renormalize) and, if the contract
requires visibility, be returned as `Ok+stats`; it should not become an `Err`
that triggers a restart.

## 6. A worked exchange

Protocol:

```kappa
protocol HeatCell {
  op init : (Float) → ()
  op step : (Float, Float, Float, Float) → Shard
  op snapshot : () → Shard
  errors : { 10001: "unknown_op" }
}
```

Worker starts:

```
HELLO: sid=0 tag=2 [schema_hi, schema_lo, 2]
```

Pi calls `init(0.5)` with corr `7`:

```
7
0
1
0.5
```

Worker replies `Ok(())`:

```
LMB1 tag=1 sid=7 [0.0]
```

Pi calls `step(0.5, 0.5, 0.5, 0.5)` with corr `8`:

```
8
1
4
0.5
0.5
0.5
0.5
```

Worker replies `Ok([0.5])`:

```
LMB1 tag=1 sid=8 [0.0, 0.5]
```

Pi decodes the body as `Payload([0.5])`; `u[0]` is the scalar.

## 7. Worker checklist

1. Include `pi_worker.lambda` and the generated schema.
2. Emit HELLO once, then loop on `pi-next-call`.
3. Read/drain all arguments before an early return.
4. Keep the last-good state; commit only after success.
5. Use `op-*` and `error-*` constants.
6. Return one reply per request, on the request's `corr`.
7. Never write non-frame diagnostics to stdout; stderr is for humans.

See [`worker.md`](worker.md) for the minimal Lambda skeleton and
[`examples/workers/heat_cell.lambda`](../examples/workers/heat_cell.lambda) for a
complete 45-line worker.
