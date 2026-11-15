/* vec2_bench.c -- SoA 2D vector rotation, 1M elements. */
#include <stdio.h>
#include <stdlib.h>
#define N 1000000
int main(void) {
    double *x = (double *)malloc(sizeof(double) * N);
    double *y = (double *)malloc(sizeof(double) * N);
    for (int i = 0; i < N; i++) { x[i] = (double)(i % 97) + 1.0; y[i] = (double)(i % 89) + 2.0; }
    for (int rep = 0; rep < 3; rep++) {
        for (int i = 0; i < N; i++) {
            double a = x[i], b = y[i];
            x[i] = -b;
            y[i] = a;
        }
    }
    double sx = 0.0, sy = 0.0;
    for (int i = 0; i < N; i++) { sx += x[i]; sy += y[i]; }
    printf("%.17g\n", sx + sy);
    free(x); free(y);
    return 0;
}
