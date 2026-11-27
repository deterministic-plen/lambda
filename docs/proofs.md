# Type-system proof corpus (`tools/proofcheck.py`)

The project does not ship a general theorem prover.  `tools/proofcheck.py` is a
small **soundness gate**: a corpus of complete Lambda programs that either must
type-check and produce a specific value (with `live=0`) or must be rejected with
a specific diagnostic.  It is intentionally cheap enough for CI and targets the
rules most likely to become unsound:

| property | corpus case |
| --- | --- |
| exhaustive tagged-union matching | `tagged_union_exhaustive` |
| explicit default/catch-all on a tagged union | `tagged_union_default_arm` |
| missing/duplicate arms are errors | `tagged_union_missing_arm`, `tagged_union_duplicate_arm` |
| literal matching, including `s[0]` character subjects | `literal_match_string_index` |
| a literal match needs a default | `literal_match_needs_default` |
| SCC generalization across definitions | `scc_generalization` |
| string/list borrow reads | `borrow_string_read`, `borrow_list_read` |
| borrows cannot be returned/consumed | `borrow_return_rejected`, `borrow_consume_rejected` |
| `clone` is the only retention path | `clone_is_the_retention_path` |
| sum payloads in `Array<T>` | `array_of_sum_payload` |
| nominal data product (`Token`) | `token_nominal_product` |

Run it from the repository root:

```sh
python tools/proofcheck.py -v
```

CI runs it after the regression suite.  The corpus is deliberately explicit
rather than generated: adding a typing rule should add a positive case, its
negative counterpart, and (where relevant) an output-equality check.
