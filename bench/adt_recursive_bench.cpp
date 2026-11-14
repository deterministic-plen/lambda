#include <cstdio>
#include <cstdint>
#include <vector>
struct Node { int64_t value; int left, right; };
static std::vector<Node> nodes;
static int build(int64_t n) {
    int id = (int)nodes.size(); nodes.push_back({0, -1, -1});
    if (n <= 0) return id;
    int64_t l = (n - 1) / 2, r = (n - 1) - l;
    nodes[id].value = n;
    nodes[id].left = build(l);
    nodes[id].right = build(r);
    return id;
}
static int64_t sum(int id) {
    if (nodes[id].left < 0) return 0;
    return nodes[id].value + sum(nodes[id].left) + sum(nodes[id].right);
}
int main() {
    int root = build(1048576); int64_t acc = 0;
    for (int k = 0; k < 10; k++) acc += sum(root);
    std::printf("%lld\n", (long long)acc);
}
