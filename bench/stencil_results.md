# 5-point periodic heat stencil

512x512, 50 steps, single native thread; median of 3 after build.

| implementation | ms |
| --- | ---: |
| C | 18.6 |
| C++ | 16.3 |
| C functional | 29.0 |
| Lambda inline | 35.3 |

Lambda inline / C (in-place): 1.90x
Lambda inline / C functional: 1.22x
