#include <cstdio>

struct Vec2 { double x, y; };

enum class Tag { Circle, Rect, Dot };
struct Shape {
    Tag tag;
    double a, b;
};

static double area(const Shape &s) {
    switch (s.tag) {
    case Tag::Circle: return 3.0 * s.a * s.a;
    case Tag::Rect: return s.a * s.b;
    case Tag::Dot: return 0.0;
    }
    return 0.0;
}

static double step(int n) {
    Vec2 v{0.0, 0.0};
    double acc = 0.0;
    for (int i = 0; i < n; i++) {
        double x = v.x, y = v.y;
        v.x = x + 1.0;
        v.y = y + 0.5;
        Shape s;
        int m = i % 3;
        if (m == 0) s = {Tag::Circle, x, 0.0};
        else if (m == 1) s = {Tag::Rect, x, y};
        else s = {Tag::Dot, 0.0, 0.0};
        acc += area(s) + v.x + v.y;
    }
    return acc;
}

int main() {
    double acc = 0.0;
    for (int r = 0; r < 3; r++) acc += step(20000000);
    std::printf("%.17g\n", acc);
    return 0;
}
