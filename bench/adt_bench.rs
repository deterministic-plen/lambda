#[derive(Clone, Copy)]
struct Vec2 {
    x: f64,
    y: f64,
}

enum Shape {
    Circle(f64),
    Rect(f64, f64),
    Dot,
}

fn area(s: &Shape) -> f64 {
    match s {
        Shape::Circle(r) => 3.0 * r * r,
        Shape::Rect(w, h) => w * h,
        Shape::Dot => 0.0,
    }
}

fn step(n: usize) -> f64 {
    let mut v = Vec2 { x: 0.0, y: 0.0 };
    let mut acc = 0.0;
    for i in 0..n {
        let x = v.x;
        let y = v.y;
        v.x = x + 1.0;
        v.y = y + 0.5;
        let s = match i % 3 {
            0 => Shape::Circle(x),
            1 => Shape::Rect(x, y),
            _ => Shape::Dot,
        };
        acc += area(&s) + v.x + v.y;
    }
    acc
}

fn main() {
    let mut acc = 0.0;
    for _ in 0..3 {
        acc += step(20_000_000);
    }
    println!("{:.17}", acc);
}
