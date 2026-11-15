// stencil_bench.rs -- same algorithm.
const N: usize = 512;
const STEPS: usize = 50;
fn main() {
    let mut a = vec![0.0f64; N * N];
    let mut b = vec![0.0f64; N * N];
    for y in 0..N {
        for x in 0..N {
            let dx = x as isize - (N / 2) as isize;
            let dy = y as isize - (N / 2) as isize;
            a[y * N + x] = if dx * dx + dy * dy <= 64 * 64 { 1.0 } else { 0.0 };
        }
    }
    for _ in 0..STEPS {
        for y in 0..N {
            for x in 0..N {
                let xm = (x + N - 1) % N;
                let xp = (x + 1) % N;
                let ym = (y + N - 1) % N;
                let yp = (y + 1) % N;
                let p = y * N + x;
                let c = a[p];
                let lap = a[y * N + xm] + a[y * N + xp]
                    + a[ym * N + x] + a[yp * N + x] - 4.0 * c;
                b[p] = c + 0.2 * lap;
            }
        }
        std::mem::swap(&mut a, &mut b);
    }
    let sum: f64 = a.iter().sum();
    println!("{:.17}", sum);
}
