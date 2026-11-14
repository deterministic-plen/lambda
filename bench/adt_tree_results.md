# Recursive tree / flat-arena benchmark

1048576-node implicit complete binary tree, 50 traversals; median of 5 measured runs.

| implementation | time | vs C |
| --- | ---: | ---: |
| Lambda | 244.7 ms | 6.08x |
| C | 40.2 ms | 1.00x |
| C++ | 41.2 ms | 1.02x |
| Rust | 39.2 ms | 0.98x |
