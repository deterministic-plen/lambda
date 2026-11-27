#!/usr/bin/env bash
# Compile a representative subset of the regression suite and run each program
# under Valgrind memcheck.
#
# The main suite (run_tests.py) already checks stdout and `live=0`; this job is
# the CI memory-safety floor proposed by the PR review (docs item "CI
# coverage").  It catches invalid reads/writes in the emitted C runtime on a
# cross-section of tests without paying for a full-suite Valgrind run on every
# push.
#
# Rules for entries: flag-free single-file cases that do not need stdin, and
# whose stdout is irrelevant to this check (stdout is discarded).
set -euo pipefail

root="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
python_bin="${PYTHON:-}"
if [ -z "$python_bin" ]; then
  if command -v python >/dev/null 2>&1; then python_bin=python; else python_bin=python3; fi
fi
cd "$root"

cases=(
  tests/core/hello.lambda
  tests/arrays/arrayf_basic.lambda
  tests/arrays/array_clone.lambda
  tests/arrays/arrayf_pmap.lambda
  tests/lists/listchurn.lambda
  tests/lists/member_in.lambda
  tests/closures/fix.lambda
  tests/strings/str_bytes_codepoints.lambda
  tests/records/record_complex.lambda
  tests/enums_sums/sum_shape.lambda
  tests/data/adt_tree.lambda
  tests/borrows/borrow_read.lambda
  tests/parallel/pmap_basic.lambda
)

workdir="$(mktemp -d)"
trap 'rm -rf "$workdir"' EXIT

fail=0
for src in "${cases[@]}"; do
  name="${src##*/}"
  name="${name%.lambda}"
  exe="$workdir/$name"
  echo "== compile $src"
  "$python_bin" lambda.py "$src" -o "$exe"

  log="$workdir/$name.valgrind.log"
  err="$workdir/$name.stderr"
  if ! LAMBDA_STATS=1 valgrind --quiet --error-exitcode=99 --leak-check=no \
      --log-file="$log" "$exe" >"$workdir/$name.stdout" 2>"$err"; then
    echo "valgrind reported errors for $src:" >&2
    cat "$log" >&2 || true
    fail=1
    continue
  fi

  # The runtime prints allocation statistics to stderr; live=0 means no
  # reference-count leak (the same invariant run_tests.py enforces).
  if ! grep -q 'live=0' "$err"; then
    echo "missing live=0 allocation statistics for $src:" >&2
    cat "$err" >&2
    fail=1
  fi
done

if [ "$fail" -ne 0 ]; then
  echo "valgrind smoke subset: FAILED" >&2
  exit 1
fi
echo "valgrind smoke subset: PASS (${#cases[@]} programs)"
