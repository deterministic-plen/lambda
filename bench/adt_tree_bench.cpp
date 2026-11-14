#include <cstdio>
#include <cstdint>
#include <vector>

int main() {
    constexpr int64_t N = 1048576, REPS = 50;
    std::vector<int64_t> stack((size_t)N + 1);
    int64_t total = 0;
    for (int64_t rep = 0; rep < REPS; ++rep) {
        size_t sp = 0;
        int64_t acc = 0;
        stack[sp++] = 0;
        while (sp) {
            int64_t node = stack[--sp];
            acc += node;
            int64_t left = 2 * node + 1, right = left + 1;
            if (left < N) stack[sp++] = left;
            if (right < N) stack[sp++] = right;
        }
        total += acc;
    }
    std::printf("%lld\n", (long long)total);
    return 0;
}
