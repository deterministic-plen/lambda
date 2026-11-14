#include <stdio.h>
#include <stdint.h>

static int64_t stack[1048577];

int main(void) {
    const int64_t N = 1048576, REPS = 50;
    int64_t total = 0;
    for (int64_t rep = 0; rep < REPS; rep++) {
        int64_t sp = 0, acc = 0;
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
    printf("%lld\n", (long long)total);
    return 0;
}
