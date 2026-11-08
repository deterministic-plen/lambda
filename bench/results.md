# Lambda benchmark results

CPU: AMD Ryzen 7 9700X 8-Core Processor (16 logical)

| workload | threads | Lambda ms | C ms | C++ ms | Rust ms | Lambda/C | Lambda/C++ | Lambda/Rust |
|---|---:|---:|---:|---:|---:|---:|---:|---:|
| seq | 1 | 1018.627 | 1004.947 | 1003.476 | 1010.571 | 1.01x | 1.02x | 1.01x |
| par | 1 | 1178.045 | 1175.279 | 1225.127 | 1189.475 | 1.00x | 0.96x | 0.99x |
| par | 16 | 171.927 | 170.194 | 176.097 | 178.156 | 1.01x | 0.98x | 0.97x |
| sieve | 1 | 136.041 | 165.756 | 160.300 | 163.428 | 0.82x | 0.85x | 0.83x |
| sieve | 16 | 20.466 | 22.496 | 23.216 | 21.450 | 0.91x | 0.88x | 0.95x |

| Newton (100 steps) | Lambda µs | C µs | C++ µs | Rust µs | Lambda/C | Lambda/C++ | Lambda/Rust |
|---|---:|---:|---:|---:|---:|---:|---:|
| per solve | 0.3670 | 0.3702 | 0.3635 | 0.3665 | 0.99x | 1.01x | 1.00x |

| large array (10M i64 fill+sum) | Lambda ms | C ms | C++ ms | Rust ms | Lambda/C | Lambda/C++ | Lambda/Rust |
|---|---:|---:|---:|---:|---:|---:|---:|
| per pass | 16.323 | 13.852 | 13.232 | 14.154 | 1.18x | 1.23x | 1.15x |

| bigint | Lambda | Python (C bigint) | Lambda / Python |
|---|---:|---:|---:|
| F(1,000,000), 208988 digits (compute + output) | 96.7 ms | 522.7 ms | 0.19x |

| bigint compute | Lambda | Python int | gmpy2 (GMP) | Lambda / Python | Lambda / gmpy2 |
|---|---:|---:|---:|---:|---:|
| F(1,000,000) compute only | 89.9 ms | 35.6 ms | 2.74 ms | 2.53x | 32.8x |
