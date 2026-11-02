# Lambda benchmark results

CPU: AMD Ryzen 7 9700X 8-Core Processor (16 logical)

| workload | threads | Lambda ms | C ms | C++ ms | Rust ms | Lambda/C | Lambda/C++ | Lambda/Rust |
|---|---:|---:|---:|---:|---:|---:|---:|---:|
| seq | 1 | 1022.093 | 1016.444 | 1014.733 | 1034.298 | 1.01x | 1.01x | 0.99x |
| par | 1 | 1204.113 | 1193.461 | 1249.067 | 1219.795 | 1.01x | 0.96x | 0.99x |
| par | 16 | 170.994 | 165.140 | 173.880 | 176.084 | 1.04x | 0.98x | 0.97x |
| sieve | 1 | 230.340 | 167.667 | 164.992 | 165.115 | 1.37x | 1.40x | 1.40x |
| sieve | 16 | 33.894 | 21.107 | 21.613 | 20.596 | 1.61x | 1.57x | 1.65x |

| Newton (100 steps) | Lambda µs | C µs | C++ µs | Rust µs | Lambda/C | Lambda/C++ | Lambda/Rust |
|---|---:|---:|---:|---:|---:|---:|---:|
| per solve | 0.3746 | 0.3656 | 0.3655 | 0.3681 | 1.02x | 1.02x | 1.02x |

| large array (10M i64 fill+sum) | Lambda ms | C ms | C++ ms | Rust ms | Lambda/C | Lambda/C++ | Lambda/Rust |
|---|---:|---:|---:|---:|---:|---:|---:|
| per pass | 16.919 | 14.976 | 13.264 | 16.127 | 1.13x | 1.28x | 1.05x |

| bigint | Lambda | Python (C bigint) | Lambda / Python |
|---|---:|---:|---:|
| F(1,000,000), 208988 digits | 760.2 ms | 527.8 ms | 1.44x |
