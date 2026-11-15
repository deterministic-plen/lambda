/* stencil_bench.cpp -- same algorithm, C++20. */
#include <cstdio>
#include <vector>
constexpr int N = 512, STEPS = 50;
int main() {
    std::vector<double> a(N * N), b(N * N);
    for (int y = 0; y < N; y++)
        for (int x = 0; x < N; x++) {
            int dx = x - N / 2, dy = y - N / 2;
            a[y * N + x] = (dx * dx + dy * dy <= 64 * 64) ? 1.0 : 0.0;
        }
    for (int s = 0; s < STEPS; s++) {
        for (int y = 0; y < N; y++)
            for (int x = 0; x < N; x++) {
                int xm = (x + N - 1) % N, xp = (x + 1) % N;
                int ym = (y + N - 1) % N, yp = (y + 1) % N;
                int p = y * N + x;
                double c = a[p];
                double lap = a[y * N + xm] + a[y * N + xp]
                           + a[ym * N + x] + a[yp * N + x] - 4.0 * c;
                b[p] = c + 0.2 * lap;
            }
        a.swap(b);
    }
    double sum = 0.0;
    for (double v : a) sum += v;
    std::printf("%.17g\n", sum);
}
