struct Node { value: i64, left: i32, right: i32 }
fn build(n: i64, nodes: &mut Vec<Node>) -> i32 {
    let id = nodes.len() as i32;
    nodes.push(Node { value: 0, left: -1, right: -1 });
    if n <= 0 { return id; }
    let l = (n - 1) / 2;
    let r = (n - 1) - l;
    nodes[id as usize].value = n;
    let li = build(l, nodes);
    let ri = build(r, nodes);
    nodes[id as usize].left = li;
    nodes[id as usize].right = ri;
    id
}
fn sum(id: i32, nodes: &[Node]) -> i64 {
    let n = &nodes[id as usize];
    if n.left < 0 { return 0; }
    n.value + sum(n.left, nodes) + sum(n.right, nodes)
}
fn main() {
    let mut nodes = Vec::new();
    let root = build(1_048_576, &mut nodes);
    let mut acc = 0i64;
    for _ in 0..10 { acc += sum(root, &nodes); }
    println!("{}", acc);
}
