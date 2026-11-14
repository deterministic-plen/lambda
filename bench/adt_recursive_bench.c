#include <stdio.h>
#include <stdint.h>

static int64_t value_[2097154], left_[2097154], right_[2097154];

static int64_t build(int64_t n) {
    static int64_t used = 0;
    int64_t id = used++;
    if (n <= 0) { value_[id] = 0; left_[id] = -1; right_[id] = -1; return id; }
    int64_t l = (n - 1) / 2, r = (n - 1) - l;
    value_[id] = n;
    left_[id] = build(l);
    right_[id] = build(r);
    return id;
}

static int64_t sum(int64_t id) {
    if (left_[id] < 0) return 0;
    return value_[id] + sum(left_[id]) + sum(right_[id]);
}

int main(void) {
    int64_t root = build(1048576), acc = 0;
    for (int k = 0; k < 10; k++) acc += sum(root);
    printf("%lld\n", (long long)acc);
    return 0;
}
