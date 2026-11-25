# Lambda data model and Pi authoring

Status: design note, 2026-11-15.  The domain-specific
`arrayf-gray-scott` / `arrayf-gray-stripe` primitives have been removed.
Gray-Scott is now ordinary Lambda code.

## Current constraints

- `ArrayF` is a flat `Float` array.  `Array<Float>`, `Array<Int>`, and
  `Array<Bool>` are now accepted as aliases for `ArrayF`/`ArrayI`/`ArrayB`.
  `Array<Vec2>` is accepted as the planar SoA `ArrayF` convention; general
  user-type `Array<T>` with compiler-known element strides is still absent.
  `arrayf-get-unchecked` exists as an explicitly unsafe hot-loop escape hatch.
- `arrayf-pmap` and `arrayi-pmap` are the data-parallel array primitives.  A
  pure one-parameter lambda is now **inlined into a raw `i64` loop** for speed; this
  is serial within one array and relies on Pi tiles/processes for scale.  Other
  provably pure functions use the runtime thread pool; unknown function values
  use the safe serial fallback.  Function parameters declared `pure` are
  trusted only after a call-site check.
- Arrays are fixed-length.  `lib/vector.lambda` provides a flat growable vector
  by doubling and copying; it is not yet a bump-pointer arena.
- Pi has `partition`, `snapshot_grid`, `collect`, and `stitch_grid` for 2D
  tiles.  `tile_shim.lambda` now owns the worker request loop and op dispatch;
  tile workers (heat and Gray-Scott) supply only `init` / `step` / `payload` / `patch` / `snapshot`
  hooks.

## Layouts

A vector field should stay structure-of-arrays:

```text
PlanarArrayF(n, 2):
  [x0 .. x(n-1), y0 .. y(n-1)]
```

This is contiguous, cache-friendly, and works with the current `halo2` /
`stitch_grid` helpers.  It is not a new primitive; it is a convention plus
libraries.

The next step is a compiler-known element layout:

```text
ScalarLayout { size, align, load, store, add, mul, ... }
Array<T>
par-map : (T -> T) -> Array<T> -> Array<T>
```

The element operation must be monomorphized and inlined.  A dynamic dispatch
per element would erase the benefit.

## Parallel map and purity

`arrayf-pmap` now accepts calls through unknown function values, but only via
the serial fallback:

```text
v2-map(v, n, fx, fy)        -- unknown fx/fy: serial
arrayf-pmap (λ i. ...)      -- parallel when the lambda is proved pure
v2-map with fx: pure        -- parallel; call site must pass a pure function
```

A parallel range-loop lowering was prototyped: one compiled body loop per
worker range, no per-element callback.  Its generated loop was slower than the
inline serial loop on the 512x512 stencil, so it is not selected.  The next
attempt should keep the body loop fully inlined in the caller and split only
the outer range, rather than moving the body into a separately compiled loop
function.

Function parameters can now be declared:

```lambda
v2-map → (λ v: ArrayF n: Int fx: pure fy: pure. ...)
```

A call to a pure parameter must receive a provably pure function (a literal
pure lambda or a `pure`-declared function); otherwise compilation fails.
With that, `arrayf-pmap` inside `v2-map` is lowered to the parallel primitive,
not the serial fallback.  `lib/vec2.lambda` uses this to give a generic,
parallel vector map.

## Growable flat vectors

`lib/arena.lambda` now provides a flat bump allocator with an explicit caller
cursor: `arena-alloc(cap, used, n)` returns `[offset, used']`, and the storage
remains one contiguous `ArrayF`.  It is a library, not a global mutable heap.
`lib/vector.lambda` implements:

```text
[length, capacity, v0, v1, ...]
vec-new, vec-length, vec-get, vec-set, vec-push, vec-to-array
```

Growth doubles capacity and copies.  This is already contiguous and avoids a
node per element.  A true bump-pointer version needs an arena:

```text
Arena { base: ArrayB, used: Int, capacity: Int }
alloc(arena, n) -> (offset, arena')
```

with lifetime/ownership rules.  Do not add a mutable global pointer; keep the
arena linear and thread it through the computation.

## Pi authoring

`partition`, `collect`, and `stitch_grid` remove the coordinate and snapshot
boilerplate.  The next ergonomic step is a worker shim:

```lambda
include "grid_worker.lambda"

grid-step → (λu halos. ...)
grid-snapshot → (λu. u)
```

The shim owns the protocol loop (`init`, `step`, `patch`, `snapshot`) and the
wire encoding.  The author writes only the matrix operation.  This is a
library/generator concern; it should not add another Pi primitive.

## Custom scalar types

Double-double, complex, and interval values should be **layouts**, not arena
nodes:

```text
DD = (hi: f64, lo: f64)
Array<DD> = { hi: ArrayF, lo: ArrayF }
```

The same generic `par-map` pipeline then handles them.  Halo and stitch helpers
must take an element width (or a layout descriptor) rather than assuming one
f64 per cell.
