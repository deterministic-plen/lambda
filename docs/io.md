# IO as a state machine (`lib/io.lambda`)

Lambda's IO surface follows the phase-space discipline:

```lambda
io-step : (IOState, IOEvent) -> IOOutcome
```

`IOState` is nominal and threaded through steps; `IOEvent` is consumed by each
step; the result is a tagged union so success and failure cannot be confused:

```lambda
data IOResult = Ok (value: String) | Err (code: Int)

type IOState = (reads: Int)
type IOEvent = (path: String)
type IOOutcome = (state: IOState, result: IOResult)
```

```lambda
include "lib/io.lambda"

s0 → (io-state 0)
event → (io-event "data.csv")
out → (io-step s0 event)
match (IOOutcome-result out)
  Ok text → (str-len text)
| Err code → -1
```

`IOOutcome-state` is the new state for the next cycle.  `read-file-io` (the
primitive behind `io-step`) reads regular files and returns `Err 1` for
directories, unseekable files, missing files and read errors; an empty regular
file is a valid `Ok ""`.

`read-file`, `read-file-bytes` and `write-file` remain as legacy convenience
primitives for bootstrap-style code and existing tests.  New code should use
`io-step`; the legacy `read-file` still returns `""` on error and therefore
cannot distinguish an empty file from a failure.
