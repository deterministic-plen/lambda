// Fair Rust implementations of the same three nth-prime algorithms used by
// Lambda.  Threads are spawned per call, like Lambda's pmap.
use std::hint::black_box;
use std::sync::atomic::{AtomicI64, Ordering};
use std::thread;

const BLOCK: i64 = 65536;
const SIEVE_BLOCK: i64 = 64;

fn bench_threads() -> usize {
    if let Ok(s) = std::env::var("BENCH_THREADS") {
        if let Ok(n) = s.trim().parse::<usize>() {
            if n > 0 {
                return n;
            }
        }
    }
    thread::available_parallelism().map(|n| n.get()).unwrap_or(1)
}

fn upper_bound(n: i64) -> i64 {
    if n < 6 {
        return 15;
    }
    let f = n as f64;
    let est = f * (f.ln() + f.ln().ln());
    (est * 1.05) as i64
}

#[inline(always)]
fn is_prime(n: i64) -> bool {
    if n < 2 {
        return false;
    }
    if n == 2 {
        return true;
    }
    if n & 1 == 0 {
        return false;
    }
    let mut d = 3;
    while d * d <= n {
        if n % d == 0 {
            return false;
        }
        d += 2;
    }
    true
}

// Fixed-iteration Newton for sqrt(2), matching the Lambda/C/C++ kernel.
#[inline(never)]
fn newton_solve(mut x: f64) -> f64 {
    for _ in 0..100 {
        x -= (x * x - 2.0) / (2.0 * x);
    }
    x
}

#[inline(never)]
fn nth_prime_seq(target: i64) -> i64 {
    if target == 1 {
        return 2;
    }
    let mut num = 3i64;
    let mut count = 1i64;
    loop {
        if is_prime(num) {
            count += 1;
            if count == target {
                return num;
            }
        }
        num += 2;
    }
}

fn count_block(b: i64) -> i64 {
    let lo = b * BLOCK;
    let hi = lo + BLOCK;
    let mut count = 0;
    let mut i = if lo < 2 { 2 } else { lo };
    while i < hi {
        if is_prime(i) {
            count += 1;
        }
        i += 1;
    }
    count
}

fn parallel_for<F>(n: i64, threads: usize, f: F)
where
    F: Fn(i64) + Sync,
{
    let next = AtomicI64::new(0);
    let worker = || loop {
        let i = next.fetch_add(1, Ordering::Relaxed);
        if i >= n {
            break;
        }
        f(i);
    };
    if threads <= 1 || n <= 1 {
        worker();
        return;
    }
    let t = (threads as i64).min(n) as usize;
    thread::scope(|scope| {
        for _ in 1..t {
            scope.spawn(&worker);
        }
        worker();
    });
}

#[inline(never)]
fn nth_prime_par(target: i64) -> i64 {
    let limit = upper_bound(target);
    let blocks = (limit + BLOCK - 1) / BLOCK;
    let counts: Vec<AtomicI64> = (0..blocks).map(|_| AtomicI64::new(0)).collect();
    let c = &counts;
    parallel_for(blocks, bench_threads(), |b| {
        c[b as usize].store(count_block(b), Ordering::Relaxed);
    });
    let mut acc = 0i64;
    let mut block = 0i64;
    while block < blocks {
        let count = counts[block as usize].load(Ordering::Relaxed);
        if target <= acc + count {
            break;
        }
        acc += count;
        block += 1;
    }
    let mut k = target - acc;
    let lo = block * BLOCK;
    let hi = lo + BLOCK;
    let mut i = if lo < 2 { 2 } else { lo };
    while i < hi {
        if is_prime(i) {
            k -= 1;
            if k == 0 {
                return i;
            }
        }
        i += 1;
    }
    0
}

fn block_mask(b: i64, base: &[i64]) -> u64 {
    let lo = b * SIEVE_BLOCK;
    let mut mask = u64::MAX;
    if lo == 0 {
        mask &= !3u64;
    }
    for &p in base {
        let p2 = p * p;
        let rem = lo % p;
        let first0 = if rem == 0 { lo } else { lo + (p - rem) };
        let first = first0.max(p2);
        let mut off = first - lo;
        while off < 64 {
            mask &= !(1u64 << off);
            off += p;
        }
    }
    mask
}

#[inline(never)]
fn nth_prime_sieve(target: i64) -> i64 {
    let limit = upper_bound(target);
    let root = (limit as f64).sqrt() as i64;
    let base: Vec<i64> = (2..=root).filter(|&i| is_prime(i)).collect();
    let blocks = (limit + SIEVE_BLOCK - 1) / SIEVE_BLOCK;
    const CHUNK: i64 = 32;
    let chunks = (blocks + CHUNK - 1) / CHUNK;
    let chunk_counts: Vec<AtomicI64> = (0..chunks).map(|_| AtomicI64::new(0)).collect();
    let c = &chunk_counts;
    parallel_for(chunks, bench_threads(), |ch| {
        let start = ch * CHUNK;
        let stop = (start + CHUNK).min(blocks);
        let mut total = 0i64;
        for b in start..stop {
            total += block_mask(b, &base).count_ones() as i64;
        }
        c[ch as usize].store(total, Ordering::Relaxed);
    });
    let mut acc = 0i64;
    let mut chunk = 0i64;
    while chunk < chunks {
        let count = chunk_counts[chunk as usize].load(Ordering::Relaxed);
        if target <= acc + count {
            break;
        }
        acc += count;
        chunk += 1;
    }
    let mut need = target - acc;
    let start = chunk * CHUNK;
    let stop = (start + CHUNK).min(blocks);
    let mut block = start;
    while block < stop {
        let count = block_mask(block, &base).count_ones() as i64;
        if need <= count {
            break;
        }
        need -= count;
        block += 1;
    }
    let mut k = need;
    let mask = block_mask(block, &base);
    for i in 0..64 {
        if mask & (1u64 << i) != 0 {
            k -= 1;
            if k == 0 {
                return block * SIEVE_BLOCK + i;
            }
        }
    }
    0
}

fn main() {
    let args: Vec<String> = std::env::args().collect();
    let mode = args.get(1).map(String::as_str).unwrap_or("seq");
    let reps: usize = args.get(2).and_then(|s| s.parse().ok()).unwrap_or(3);
    if mode == "array" {
        let n = black_box(10_000_000i64);
        let mut total = 0i64;
        for _ in 0..reps {
            let mut a = vec![0i64; n as usize];
            for i in 0..n {
                a[i as usize] = i;
            }
            let mut s = 0i64;
            for &v in &a {
                s += v;
            }
            total += s;
            total = black_box(total);
        }
        println!("{}", total);
        return;
    }
    if mode == "newton" {
        let mut sum = 0.0f64;
        for _ in 0..reps {
            let start = black_box(2.0f64);
            sum += newton_solve(start);
            sum = black_box(sum);
        }
        println!("{:.17}", sum);
        return;
    }
    let mut sum = 0i64;
    for _ in 0..reps {
        let target = black_box(670000i64);
        sum += match mode {
            "seq" => nth_prime_seq(target),
            "par" => nth_prime_par(target),
            "sieve" => nth_prime_sieve(target),
            _ => std::process::exit(2),
        };
        sum = black_box(sum);
    }
    println!("{}", sum);
}
