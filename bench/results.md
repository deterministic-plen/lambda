# Lambda benchmark results

CPU: AMD Ryzen 7 9700X 8-Core Processor (16 logical)

| workload | threads | Lambda ms | C ms | C++ ms | Rust ms | Lambda/C | Lambda/C++ | Lambda/Rust |
|---|---:|---:|---:|---:|---:|---:|---:|---:|
| seq | 1 | 1026.028 | 1014.895 | 1014.278 | 1036.528 | 1.01x | 1.01x | 0.99x |
| par | 1 | 1209.604 | 1199.620 | 1241.366 | 1206.489 | 1.01x | 0.97x | 1.00x |
| par | 16 | 171.817 | 171.801 | 179.253 | 174.272 | 1.00x | 0.96x | 0.99x |
| sieve | 1 | 241.282 | 175.651 | 167.302 | 171.371 | 1.37x | 1.44x | 1.41x |
| sieve | 16 | 36.619 | 21.224 | 20.708 | 20.726 | 1.73x | 1.77x | 1.77x |

| Newton (100 steps) | Lambda µs | C µs | C++ µs | Rust µs | Lambda/C | Lambda/C++ | Lambda/Rust |
|---|---:|---:|---:|---:|---:|---:|---:|
| per solve | 0.3674 | 0.3626 | 0.3673 | 0.3648 | 1.01x | 1.00x | 1.01x |

| large array (10M i64 fill+sum) | Lambda ms | C ms | C++ ms | Rust ms | Lambda/C | Lambda/C++ | Lambda/Rust |
|---|---:|---:|---:|---:|---:|---:|---:|
| per pass | 17.706 | 14.158 | 13.946 | 14.074 | 1.25x | 1.27x | 1.26x |
