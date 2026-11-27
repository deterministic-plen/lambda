#!/usr/bin/env python3
"""Minimal proof assistant for the Lambda type checker.

This is deliberately small: a corpus of positive and negative programs that
pin down the type-system invariants the compiler relies on.  It is not a
general theorem prover; it is a soundness regression gate.  Each case is a
self-contained Lambda program plus either

* ``stdout``: the program must compile, run, and print exactly this value; or
* ``error``: the program must be rejected with this substring in the compiler
  diagnostic.

The corpus covers the invariants most likely to regress:

* exhaustive tagged-union matching, duplicate and missing arms;
* literal matching (strings and character-indexed strings);
* Hindley--Milner generalization across block SCCs;
* borrow inference: reads are accepted, returning/consuming a borrow is not;
* explicit ``clone`` as the only way to keep two independent array values;
* sum payloads in ``Array<T>`` and nominal data-backed products.

Run from the repository root:

    python tools/proofcheck.py [-v]
"""

from __future__ import annotations

import argparse
import os
import subprocess
import sys
import tempfile
from dataclasses import dataclass


ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


@dataclass
class Case:
    name: str
    source: str
    stdout: str | None = None
    error: str | None = None


CASES = [
    Case(
        "tagged_union_exhaustive",
        """
        type Opt (Some (x: Int) | None)
        f → (λo: Opt. match o with Some x → x | None → 0)
        [(f (Some 7)), (f None)]
        """,
        stdout="[7, 0]\n",
    ),
    Case(
        "tagged_union_default_arm",
        """
        enum S (A, B, C)
        f → (λs: S. match s _ → 9 | B → 2)
        [(f A), (f B), (f C)]
        """,
        stdout="[9, 2, 9]\n",
    ),
    Case(
        "tagged_union_missing_arm",
        """
        type Opt (Some (x: Int) | None)
        f → (λo: Opt. match o with Some x → x)
        (f (Some 1))
        """,
        error="non-exhaustive match",
    ),
    Case(
        "tagged_union_duplicate_arm",
        """
        enum S (A, B)
        f → (λs: S. match s with A → 1 | A → 2 | B → 3)
        (f A)
        """,
        error="duplicate match arm",
    ),
    Case(
        "literal_match_string_index",
        """
        f → (λs: String. match s[0] 'a' → 1 | 'b' → 2 | _ → 0)
        [(f "abc"), (f "bcd"), (f "xyz")]
        """,
        stdout="[1, 2, 0]\n",
    ),
    Case(
        "literal_match_needs_default",
        """
        f → (λs: String. match s "a" → 1 | "b" → 2)
        (f "a")
        """,
        error="default arm",
    ),
    Case(
        "scc_generalization",
        """
        ident → (λx. x)
        a → (ident 1)
        b → (ident "s")
        b
        """,
        stdout="s\n",
    ),
    Case(
        "borrow_string_read",
        """
        size → (λs: String. (str-len s))
        size (str-append "ab" "cd")
        """,
        stdout="4\n",
    ),
    Case(
        "borrow_list_read",
        """
        total → (λxs. if (empty? xs) then 0 else (+ (head xs) (total (tail xs))))
        total [1, 2, 3, 4]
        """,
        stdout="10\n",
    ),
    Case(
        "borrow_propagation",
        """
        first → (λxs. (head xs))
        outer → (λxs. (first xs))
        outer [7, 8, 9]
        """,
        stdout="7\n",
    ),
    Case(
        "borrow_return_rejected",
        """
        id → (λ&a: ArrayI. a)
        (id (arrayi 1 0))
        """,
        error="borrowed parameter cannot be returned",
    ),
    Case(
        "borrow_consume_rejected",
        """
        bad → (λ&a: ArrayI. (arrayi-set a 0 1))
        (bad (arrayi 1 0))
        """,
        error="cannot mutate a borrowed array",
    ),
    Case(
        "clone_is_the_retention_path",
        """
        a → (arrayi-of [1, 2, 3])
        b → (arrayi-clone a)
        c → (arrayi-set a 0 9)
        [(arrayi-get b 0), (arrayi-get c 0)]
        """,
        stdout="[1, 9]\n",
    ),
    Case(
        "array_of_sum_payload",
        """
        type Opt (Some (x: Int) | None)
        a → #[Some 3, None]
        (match (array-get a 0) with Some x → x | None → 0)
        """,
        stdout="3\n",
    ),
    Case(
        "token_nominal_product",
        """
        enum Kind (Num, Op)
        type Token = (kind: Kind, value: [Int])
        t → (Token Num [1, 2, 3])
        (sum (Token-value t))
        """,
        stdout="6\n",
    ),
]


def run_case(case: Case, workdir: str, verbose: bool) -> tuple[bool, str]:
    src = os.path.join(workdir, case.name + ".lambda")
    exe = os.path.join(workdir, case.name + ".exe")
    with open(src, "w", encoding="utf-8") as f:
        f.write(case.source)
    env = dict(os.environ)
    proc = subprocess.run(
        [sys.executable, os.path.join(ROOT, "lambda.py"), src, "-o", exe],
        capture_output=True, text=True, encoding="utf-8", errors="replace",
        timeout=180)
    if case.error is not None:
        if proc.returncode == 0:
            return False, f"expected rejection, but {case.name} compiled"
        if case.error not in proc.stderr:
            return False, (f"expected '{case.error}' in compiler stderr, got:\n"
                           f"{proc.stderr}")
        return True, "rejected as expected"
    if proc.returncode != 0:
        return False, f"compilation failed:\n{proc.stderr}"
    env["LAMBDA_STATS"] = "1"
    run = subprocess.run([exe], capture_output=True, text=True,
                         encoding="utf-8", errors="replace", env=env,
                         timeout=180)
    if run.returncode != 0:
        return False, f"runtime exited {run.returncode}:\n{run.stderr}"
    if run.stdout != case.stdout:
        return False, (f"stdout mismatch: expected {case.stdout!r}, "
                       f"got {run.stdout!r}")
    if "live=0" not in run.stderr:
        return False, f"missing live=0 allocation report:\n{run.stderr}"
    return True, f"printed {run.stdout!r}, live=0"


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("-v", "--verbose", action="store_true")
    args = ap.parse_args(argv)
    failures = []
    with tempfile.TemporaryDirectory(prefix="lambda-proofs-") as tmp:
        for i, case in enumerate(CASES):
            try:
                ok, message = run_case(case, tmp, args.verbose)
            except subprocess.TimeoutExpired:
                ok, message = False, "timed out"
            if ok and args.verbose:
                print(f"PASS {case.name}: {message}")
            elif not ok:
                failures.append((case.name, message))
    print(f"{len(CASES) - len(failures)}/{len(CASES)} proof checks passed")
    for name, message in failures:
        print(f"FAIL {name}: {message}", file=sys.stderr)
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(main())
