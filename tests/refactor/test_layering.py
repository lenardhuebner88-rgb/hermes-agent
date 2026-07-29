import ast
import textwrap
from pathlib import Path

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


def annotation_fixture(*, postponed):
    future = "from __future__ import annotations\n" if postponed else ""
    return f"""{future}
POSONLY = object()
POSITIONAL = object()
VARARG = object()
KWONLY = object()
KWARG = object()
RETURN = object()
ANNASSIGN = object()
META = object()
METHOD_POSONLY = object()
METHOD_POSITIONAL = object()
METHOD_VARARG = object()
METHOD_KWONLY = object()
METHOD_KWARG = object()
METHOD_RETURN = object()

def annotated(
    posonly: POSONLY,
    /,
    positional: POSITIONAL,
    *vararg: VARARG,
    kwonly: KWONLY,
    **kwarg: KWARG,
) -> RETURN:
    pass

typed: ANNASSIGN = 1

class WithMeta(metaclass=META):
    def method(
        self,
        posonly: METHOD_POSONLY,
        /,
        positional: METHOD_POSITIONAL,
        *vararg: METHOD_VARARG,
        kwonly: METHOD_KWONLY,
        **kwarg: METHOD_KWARG,
    ) -> METHOD_RETURN:
        pass
"""


def test_evaluated_annotations_and_class_keywords_are_import_time():
    tree, _ = parse(annotation_fixture(postponed=False))
    top = layering.top_level_symbols(tree)

    assert layering.annotations_are_postponed(tree) is False
    assert layering.import_time_names(top["annotated"], top) == {
        "POSONLY",
        "POSITIONAL",
        "VARARG",
        "KWONLY",
        "KWARG",
        "RETURN",
    }
    assert layering.import_time_names(top["typed"], top) == {"ANNASSIGN"}
    assert layering.import_time_names(top["WithMeta"], top) == {
        "META",
        "METHOD_POSONLY",
        "METHOD_POSITIONAL",
        "METHOD_VARARG",
        "METHOD_KWONLY",
        "METHOD_KWARG",
        "METHOD_RETURN",
    }


def test_postponed_annotations_are_excluded_but_class_keywords_are_not():
    tree, _ = parse(annotation_fixture(postponed=True))
    top = layering.top_level_symbols(tree)
    postponed = layering.annotations_are_postponed(tree)

    assert postponed is True
    assert layering.import_time_names(
        top["annotated"], top, annotations_postponed=postponed
    ) == set()
    assert layering.import_time_names(
        top["typed"], top, annotations_postponed=postponed
    ) == set()
    assert layering.import_time_names(
        top["WithMeta"], top, annotations_postponed=postponed
    ) == {"META"}


def test_kanban_schedule_task_uses_default_but_postpones_annotation():
    path = Path(__file__).resolve().parents[2] / "hermes_cli" / "kanban_db.py"
    tree = ast.parse(path.read_text(encoding="utf-8"))
    top = layering.top_level_symbols(tree)
    postponed = layering.annotations_are_postponed(tree)

    assert postponed is True
    names = layering.import_time_names(
        top["schedule_task"],
        top,
        annotations_postponed=postponed,
    )
    assert "_SCHEDULE_DUE_UNSPECIFIED" in names
    assert "_ScheduleDueUnspecified" not in names


def test_banner_sections_banner_on_last_line_is_ignored():
    """A divider on the very last line has no title line after it — it
    must be ignored, not crash with an out-of-range read."""
    assert layering.banner_sections(["x = 1", "# ---"]) == []


def test_import_time_names_includes_annotated_assignment_value():
    """An annotated assignment's VALUE is an import-time reference —
    dropping it would let a cross-module 'X: int = OTHER' move without
    flagging the order constraint."""
    tree, _ = parse("""
        OTHER = 1
        TYPED: int = OTHER
    """)
    top = layering.top_level_symbols(tree)
    assert layering.import_time_names(top["TYPED"], top) == {"OTHER"}
