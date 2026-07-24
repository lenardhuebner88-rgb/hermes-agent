import ast
import textwrap

from scripts.refactor import layering


def parse(src):
    src = textwrap.dedent(src)
    tree = ast.parse(src)
    return tree, src.splitlines()


def test_top_level_symbols_collects_defs_classes_and_assignments():
    tree, _ = parse("""
        LIMIT = 5
        TYPED: int = 6

        def fn():
            inner = 1
            return inner

        class Cls:
            attr = 2
    """)
    top = layering.top_level_symbols(tree)
    assert set(top) == {"LIMIT", "TYPED", "fn", "Cls"}
    assert "inner" not in top   # local, not top-level
    assert "attr" not in top    # class attribute, not top-level


def test_banner_sections_finds_divider_title_pairs():
    _, lines = parse("""
        x = 1
        # ---------------------------------------------------------------------------
        # Schema
        # ---------------------------------------------------------------------------
        y = 2
    """)
    # the reported lineno is the TITLE line, not the divider above it
    assert layering.banner_sections(lines) == [(4, "Schema")]


def test_import_time_names_covers_decorators_defaults_and_bases():
    tree, _ = parse("""
        BASE_LIMIT = 1

        def deco(f):
            return f

        class Base:
            pass

        DERIVED = BASE_LIMIT + 1

        @deco
        def uses_default(x=BASE_LIMIT):
            return helper()

        def helper():
            return BASE_LIMIT

        class Child(Base):
            pass
    """)
    top = layering.top_level_symbols(tree)
    by_name = {}
    for node in tree.body:
        if isinstance(node, (ast.FunctionDef, ast.ClassDef)):
            by_name[node.name] = node
        elif isinstance(node, ast.Assign) and isinstance(node.targets[0], ast.Name):
            by_name[node.targets[0].id] = node

    assert layering.import_time_names(by_name["DERIVED"], top) == {"BASE_LIMIT"}
    # decorator and default arg are import-time; the call in the body is not
    assert layering.import_time_names(by_name["uses_default"], top) == {"deco", "BASE_LIMIT"}
    assert layering.import_time_names(by_name["Child"], top) == {"Base"}


def test_classify_references_splits_forward_from_backward():
    tree, _ = parse("""
        def early():
            return late()

        def late():
            return 1

        def also_early():
            return early()
    """)
    top = layering.top_level_symbols(tree)
    owner = {"early": "a", "late": "b", "also_early": "a"}
    refs = layering.classify_references(top, owner, module_order=["a", "b"])
    assert ("early", "late") in refs.runtime_backward
    assert refs.runtime_forward == []      # also_early -> early is same-module
    assert refs.import_time_backward == []


def test_import_time_backward_reference_is_detected():
    tree, _ = parse("""
        FIRST = SECOND + 1
        SECOND = 2
    """)
    top = layering.top_level_symbols(tree)
    owner = {"FIRST": "a", "SECOND": "b"}
    refs = layering.classify_references(top, owner, module_order=["a", "b"])
    assert ("FIRST", "SECOND") in refs.import_time_backward
