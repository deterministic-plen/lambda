# Parallel execution: design directions

The current model is **SPMD over an immutable snapshot**: a `pmap`/`λp` splits
one index range across N forked workers, each worker owns its slice of the
output array, and all workers read the same input arrays.  That covers
embarrassingly parallel array kernels and nothing else -- a worker cannot
spawn a task, wait on one, or talk to a sibling.

Below: the candidate paradigms, then the recommended one.

## Candidate 1 -- fork/join with continuations (a "suspendable computation")

Idea: every worker runs a function `f`; when it needs help it *suspends*, hands
the suspended continuation to the runtime, a free worker resumes it.  The
recursive-call rung of the agenda is exactly the "spawn a sub-computation"
primitive.

- Safety: excellent -- a suspended computation is a first-class value, so
  ownership is simply "the continuation owns its captured values".
- Efficiency: poor with `musttail` recursion and reference counting: capturing
  a continuation means copying/retaining a frame, and the current compiler is
  built around direct calls with no frame copying.
- Effort: a stack-free CPS backend or stack copying.  Large.

## Candidate 2 -- channel / mailbox actors (Erlang, Go)

Idea: workers are actors with mailboxes; `emit` becomes "send to mailbox $k".

- Safety: good, but message passing either copies (safe, costly) or moves
  ownership (needs linear types on messages, which Lambda does not have).
- Efficiency: the atomic RC cost and allocation per message dominate kernels
  like the sieve, where the work does not cover the message overhead.
- Fit: Lambda has no subtyping, no process supervision, no error model; building
  supervision trees is a whole second language.

## Candidate 3 -- transactional memory / lock-and-sync

Idea: shared arrays with atomics and locks; `emit` becomes a shared append.

- Safety: worst -- deadlock and data races become possible, exactly what the
  current design excludes by construction (atomic RC is only switched on for
  `pmap`).
- Rejected.

## Candidate 4 -- streaming / dataflow graph (reactive pipelines)

Idea: a program is a directed graph of pure nodes; `emit` puts a frame on an
edge; the runtime schedules nodes over N workers.

- Safety: perfect -- nodes are pure, edges are typed, regions are explicit.
- Efficiency: excellent for the current workloads (emit already *is* a frame on
  an edge); trivial to make deterministic, order-independent, replayable.
- Fit: matches `--data`/`emit` framing exactly, and `pmap` becomes one node
  type.  Weakness: awkward when the graph shape depends on data, i.e. genuine
  search/recursion.

## Recommended: regions with an immutable snapshot, work-stealing scheduling, and a
## linear intra-region channel ("the pool of worker ants with a shared queen")

**Model.** The program is a *federation of regions*.  A region is created
statically (a `λp`, a `pmap`, or an explicit `pmap-region`), has:

```
region = (f, N_workers, read-set, write-slices, channel)
```

- **read-set**: arrays that every worker may read.  Enforced by the existing
  `&` borrow checker: a region captures only borrowed views of data that
  outlives the region.  This is the "each worker reads the same source data"
  requirement, and it is free because the borrow checker already rejects
  capturing a borrow in a closure that outlives it.
- **write-slices**: each worker owns a disjoint output slice, decided before the
  region starts.  No locks, no atomics on the fast path -- the same
  invariant that makes the current `arrayi-pmap-chunk` correct.
- **work stealing** inside the region: if one slice finishes early it *steals*
  a unit of work from a sibling's slice.  Units are fixed-size chunks, so
  stealing needs only an atomic counter per slice -- one atomic per chunk, not
  per element.  `musttail` is preserved: a worker loops over chunks until none
  are left, so no stack growth and no closures.
- **recruitment**: spawning a *new* region from inside a region is allowed only
  in a **tail position** (`pmap` as a region's last expression).  Then no
  worker ever waits on a child: the child's region becomes a parent of the
  runtime's region stack and the current region is retired.  That is
  **asynchrony without continuations** -- it is exactly `musttail`, lifted to
  the region level.  Recursion stays flat, memory stays flat.
- **intra-region channel**: `emit` generalized from "stdout frames" to "frames
  to a region channel".  Because only a region's own workers write it, the
  channel needs no locks: a per-worker ring buffer, merged deterministically
  (by worker id) when the region completes.  This gives ordered, replayable
  output -- a monoid fold, not a message-passing protocol.
- **determinism**: a region's result is a pure function of its inputs.  Stealing
  and worker count affect *time*, never *values*.  `emit` frames sort by
  (region, worker, index), so the stream is reproducible across N.

**Why this fits Lambda's goals.**

- *Safety*: no data races are expressible.  Read-only sharing by borrow; writes
  by disjoint ownership; no locks, no poisoning, no deadlock, no torn state.
  Region nesting is tail-only, so there is no unbounded waiting graph.
- *Efficiency*: the fast path is pointer arithmetic into disjoint slices, the
  same code already benchmarked at `1.01x` C for 16-thread trial division and
  `0.91x` for the sieve.  Stealing costs one atomic per chunk; the channel
  costs one buffer per worker.  No per-element refcount atomics.
- *Mathematical properties*: this is the **parallel map over a disjoint
  decomposition of an index set** -- formally, a fold over a free monoid of
  chunks.  Associativity gives the determinism theorem: `pmap f (a ++ b) =
  pmap f a ++ pmap f b` for the right chunking, so results are independent of
  worker count and schedule.  The region is the runtime's **tensor product of
  independent components**: components touch disjoint factors (write slices),
  and share one common factor (read-set).  No communication is needed because
  the shared factor is only ever *observed*.

**The physical analogy.**  It is a **shared-memory single-particle propagation**,
not a message-passing gas: cells (workers) evolve from the same initial state
(the borrowed snapshot), write into disjoint regions of phase space (their
slices), and exchange no information during evolution (no channels mid-run).
The channel is a *measurement record* accumulated in one place and read only
after the evolution finishes -- a delayed choice, not an interaction.  This is
also why it is safe: independent components of a tensor product commute, so the
outcome cannot depend on the order in which they are evaluated.  Work stealing
is a change of *time parametrization* only.

**What is deliberately not offered.**

- No mid-run message passing between siblings.  That would require either
  copying (safe, slow) or linear ownership transfer (a new type system
  feature).  If it becomes essential, the natural extension is a **linear
  message**: a `Queue a` whose send is consuming and whose `&`-receive is
  borrowed, matching the existing `&` discipline.
- No general async/await on arbitrary expressions.  Only region-tail
  spawning, i.e. structured, scoped asynchrony with a static parent/child
  relation -- the same shape as `structured concurrency` in Python/Kotlin/Haskell,
  and the only one that keeps `musttail` and non-atomic RC sound.

**Minimal first steps** (each independently useful):

1. `pmap-chunk` with stealing: split a range into K chunks, workers pop the
   next chunk index from one atomic counter.  Same output array, same
   determinism, 2-4 lines of runtime change.
2. Region channel: replace the global stdout frame stream with a per-worker
   buffer flushed in worker order, so `emit` becomes deterministic under
   parallelism.
3. Region nesting: allow `pmap` in tail position of a `λp` body, compiling to a
   fork that reuses the calling worker instead of blocking on it.
