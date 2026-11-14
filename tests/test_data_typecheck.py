"""Type checking for recursive data declarations (no codegen yet)."""
import os
import sys
import tempfile

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
from loader import load_program
from resolve import Resolver
from typecheck import infer_program, show_type
from errors import TypeError_


def check(src):
    fd, path = tempfile.mkstemp(suffix=".lambda")
    os.close(fd)
    try:
        with open(path, "w", encoding="utf-8") as f:
            f.write(src)
        prog = Resolver().resolve_program(load_program(path))
        return show_type(infer_program(prog))
    finally:
        os.unlink(path)


def test_recursive_tree_typechecks():
    t = check("""data Tree = Leaf | Node (value: Int, left: Tree, right: Tree)
t → (Node 1 Leaf Leaf)
match t with
  Leaf: 0,
  Node v l r: v
""")
    assert t == "Int"


def test_nonexhaustive_data_match_rejected():
    try:
        check("""data Tree = Leaf | Node (value: Int, left: Tree, right: Tree)
t → Leaf
match t with
  Leaf: 0
""")
    except TypeError_ as e:
        assert "non-exhaustive" in e.message
    else:
        raise AssertionError("non-exhaustive data match should fail")


def main():
    for name, fn in sorted(globals().items()):
        if name.startswith("test_") and callable(fn):
            fn(); print("PASS", name)
    print("data typecheck checks passed")


if __name__ == "__main__":
    main()
