#include <stdio.h>

typedef struct { double x, y; } Vec2;
typedef enum { CIRCLE, RECT, DOT } Tag;
typedef struct { Tag tag; double a, b; } Shape;

static double area(Shape s) {
    switch (s.tag) {
    case CIRCLE: return 3.0 * s.a * s.a;
    case RECT: return s.a * s.b;
    case DOT: return 0.0;
    }
    return 0.0;
}

static double step(int n) {
    Vec2 v = {0.0, 0.0};
    double acc = 0.0;
    for (int i = 0; i < n; i++) {
        double x = v.x, y = v.y;
        v.x = x + 1.0;
        v.y = y + 0.5;
        Shape s;
        int m = i % 3;
        if (m == 0) { s.tag = CIRCLE; s.a = x; s.b = 0.0; }
        else if (m == 1) { s.tag = RECT; s.a = x; s.b = y; }
        else { s.tag = DOT; s.a = 0.0; s.b = 0.0; }
        acc += area(s) + v.x + v.y;
    }
    return acc;
}

int main(void) {
    double acc = 0.0;
    for (int r = 0; r < 3; r++) acc += step(20000000);
    printf("%.17g\n", acc);
    return 0;
}
