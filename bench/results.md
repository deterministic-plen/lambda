# Lambda benchmark results

CPU: AMD Ryzen 7 9700X 8-Core Processor (16 logical)

| workload | threads | Lambda ms | C ms | C++ ms | Rust ms | Lambda/C | Lambda/C++ | Lambda/Rust |
|---|---:|---:|---:|---:|---:|---:|---:|---:|
| seq | 1 | 1026.891 | 1013.433 | 1020.614 | 1028.851 | 1.01x | 1.01x | 1.00x |
| par | 1 | 1198.745 | 1196.187 | 1242.860 | 1202.168 | 1.00x | 0.96x | 1.00x |
| par | 16 | 172.057 | 169.157 | 174.323 | 170.246 | 1.02x | 0.99x | 1.01x |
| sieve | 1 | 845.503 | 164.995 | 166.900 | 174.848 | 5.12x | 5.07x | 4.84x |
| sieve | 16 | 175.310 | 20.013 | 20.719 | 20.253 | 8.76x | 8.46x | 8.66x |

| Newton (100 steps) | Lambda µs | C µs | C++ µs | Rust µs | Lambda/C | Lambda/C++ | Lambda/Rust |
|---|---:|---:|---:|---:|---:|---:|---:|
| per solve | 0.3648 | 0.3639 | 0.3630 | 0.3651 | 1.00x | 1.00x | 1.00x |
