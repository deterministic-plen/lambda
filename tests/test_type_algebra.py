"""Type-algebra/layout regression checks (plain Python, no pytest dependency)."""
import os
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
from type_algebra import (Base, ListTy, Mu, Prod, Sum, Var, layout, normalize,
                          parse_data, parse_type, pretty, substitute, validate_decl)


def test_list_normalizes_and_uses_dense_array():
    t = normalize(parse_type("mu L. 1 + Int * L"))
    assert isinstance(t, ListTy)
    assert layout(t).kind == "list"


def test_binary_tree_uses_arena():
    t = normalize(parse_type("mu T. 1 + Int * T * T"))
    assert isinstance(t, Mu)
    assert layout(t).kind == "arena"


def test_rose_tree_uses_arena():
    t = normalize(parse_type("Int * 1/(1 - R)"))
    assert layout(t).kind == "arena"


def test_fixed_product_is_inline():
    t = normalize(parse_type("Float * Float * Float"))
    lay = layout(t)
    assert lay.kind == "inline" and lay.size == 24


def test_identities():
    assert normalize(parse_type("0 + Int")) == Base("Int")
    assert normalize(parse_type("1 * Int")) == Base("Int")


def test_substitution():
    t = parse_type("List<X>")
    got = substitute(t, "X", Base("Float"))
    assert isinstance(got, ListTy) and got.element == Base("Float")


def test_data_list_elaborates_to_dense_list():
    d = parse_data("data List<X> = Nil | Cons (head: X, tail: List<X>)")
    assert d.name == "List" and d.params == ("X",)
    t = normalize(d.instantiate(Base("Int")))
    assert isinstance(t, ListTy) and layout(t).kind == "list"


def test_data_binary_tree_uses_arena():
    d = parse_data("data Tree<X> = Leaf | Node (value: X, left: Tree<X>, right: Tree<X>)")
    t = normalize(d.body())
    assert isinstance(t, Mu)
    assert layout(t).kind == "arena"


def test_data_rose_tree_uses_arena():
    d = parse_data("data Rose<X> = Rose (value: X, children: [Rose<X>])")
    t = normalize(d.instantiate(Base("Float")))
    assert layout(t).kind == "arena"


def test_data_validation_rejects_duplicate_variant():
    d = parse_data("data X = A | A")
    try:
        validate_decl(d)
    except ValueError as e:
        assert "duplicate variant" in str(e)
    else:
        raise AssertionError("duplicate variant should be rejected")


def main():
    tests = [v for k, v in sorted(globals().items()) if k.startswith("test_")]
    for fn in tests:
        fn()
        print("PASS", fn.__name__)
    print(f"{len(tests)} type-algebra checks passed")


if __name__ == "__main__":
    main()
