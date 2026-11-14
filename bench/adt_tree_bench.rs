fn main() {
    const N: i64 = 1_048_576;
    const REPS: i64 = 50;
    let mut stack = vec![0i64; (N + 1) as usize];
    let mut total = 0i64;
    for _ in 0..REPS {
        let mut sp = 0usize;
        let mut acc = 0i64;
        stack[sp] = 0; sp += 1;
        while sp > 0 {
            sp -= 1;
            let node = stack[sp];
            acc += node;
            let left = 2 * node + 1;
            let right = left + 1;
            if left < N { stack[sp] = left; sp += 1; }
            if right < N { stack[sp] = right; sp += 1; }
        }
        total += acc;
    }
    println!("{}", total);
}
