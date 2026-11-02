def fib(n):
    if n == 0:
        return (0, 1)
    a, b = fib(n >> 1)
    c = a * ((b << 1) - a)
    d = a * a + b * b
    if n & 1:
        return (d, c + d)
    return (c, d)
print(fib(1_000_000)[0])
