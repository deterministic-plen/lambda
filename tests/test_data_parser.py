"""Source-level data declaration parsing/layout integration checks."""
import os
import sys
import tempfile

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
from loader import load_program
from lambda_ast import DataDecl
from type_algebra import layout, normalize, parse_data


def test_data_declarations_are_retained():
    src = """data Tree<X> = Leaf | Node (value: X, left: Tree<X>, right: Tree<X>)
data List<X> = Nil | Cons (head: X, tail: List<X>)
0
"""
    fd, path = tempfile.mkstemp(suffix=".lambda")
    os.close(fd)
    try:
        with open(path, "w", encoding="utf-8") as f:
            f.write(src)
        prog = load_program(path)
        assert len(prog.data_decls) == 2
        names = [d.name for d in prog.data_decls]
        assert names == ["Tree", "List"]
        tree = prog.data_decls[0]
        assert isinstance(tree, DataDecl)
        assert tree.params == ["X"]
        ctor_names = [d.name for d in prog.defs]
        assert ctor_names == ["Leaf", "Node", "Nil", "Cons"]
    finally:
        os.unlink(path)


def test_source_data_elaborates_to_layouts():
    tree = parse_data("data Tree<X> = Leaf | Node (value: X, left: Tree<X>, right: Tree<X>)")
    assert layout(normalize(tree.body())).kind == "arena"
    lst = parse_data("data List<X> = Nil | Cons (head: X, tail: List<X>)")
    assert layout(normalize(lst.instantiate(__import__("type_algebra").Base("Int")))).kind == "list"


def main():
    for name, fn in sorted(globals().items()):
        if name.startswith("test_") and callable(fn):
            fn(); print("PASS", name)
    print("data parser checks passed")


if __name__ == "__main__":
    main()
