/* vec2_bench_func.c -- functional rotation: new component buffers per step. */
#include <stdio.h>
#include <stdlib.h>
#define N 1000000
int main(void) {
    double *x = (double *)malloc(sizeof(double) * N);
    double *y = (double *)malloc(sizeof(double) * N);
    for (int i = 0; i < N; i++) { x[i] = (double)(i % 97) + 1.0; y[i] = (double)(i % 89) + 2.0; }
    for (int rep = 0; rep < 3; rep++) {
        double *nx = (double *)malloc(sizeof(double) * N);
        double *ny = (double *)malloc(sizeof(double) * N);
        for (int i = 0; i < N; i++) { nx[i] = -y[i]; ny[i] = x[i]; }
        free(x); free(y); x = nx; y = ny;
    }
    double sx = 0.0, sy = 0.0;
    for (int i = 0; i < N; i++) { sx += x[i]; sy += y[i]; }
    printf("%.17g\n", sx + sy);
    free(x); free(y);
    return 0;
}
