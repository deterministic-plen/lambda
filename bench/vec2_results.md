# SoA Vec2 rotation

1,000,000 vectors, 3 rotations, plus reduction.

| implementation | ms |
| --- | ---: |
| C | 13.0 |
| C functional | 16.8 |
| Lambda | 18.1 |

Lambda / C (in-place): 1.39x
Lambda / C functional: 1.07x
