import textwrap

import pytest

from scripts.refactor import api_snapshot


@pytest.fixture
def module_pair(tmp_path, monkeypatch):
    """A single-file module and an equivalent package, both named the same."""
    monkeypatch.syspath_prepend(str(tmp_path))
    return tmp_path


def test_snapshot_records_functions_classes_and_values(module_pair, monkeypatch):
    (module_pair / "flatmod.py").write_text(textwrap.dedent("""
        CONST = 7
        _PRIVATE = "p"

        def alpha(a, b=1, *args, **kw):
            return a

        def _helper(x):
            return x

        class Thing:
            def method(self, q):
                return q
    """))
    snap = api_snapshot.snapshot("flatmod")
    assert snap["symbols"]["alpha"]["kind"] == "function"
    assert snap["symbols"]["alpha"]["signature"] == "(a, b=1, *args, **kw)"
    assert snap["symbols"]["Thing"]["kind"] == "class"
    assert snap["symbols"]["Thing"]["methods"]["method"] == "(self, q)"
    assert snap["symbols"]["CONST"]["kind"] == "value"
    # private names are part of the surface: tests monkeypatch them
    assert "_helper" in snap["symbols"]
    assert "_PRIVATE" in snap["symbols"]


def test_diff_is_empty_for_module_converted_to_package(module_pair):
    (module_pair / "flat2.py").write_text(textwrap.dedent("""
        LIMIT = 3

        def alpha(a, b=1):
            return a

        def _helper(x):
            return x
    """))
    before = api_snapshot.snapshot("flat2")

    (module_pair / "flat2.py").unlink()
    pkg = module_pair / "flat2"
    pkg.mkdir()
    (pkg / "consts.py").write_text("LIMIT = 3\n")
    (pkg / "funcs.py").write_text(textwrap.dedent("""
        def alpha(a, b=1):
            return a

        def _helper(x):
            return x
    """))
    (pkg / "__init__.py").write_text(textwrap.dedent("""
        from .consts import LIMIT
        from .funcs import alpha, _helper
    """))

    after = api_snapshot.snapshot("flat2", fresh=True)
    assert api_snapshot.diff(before, after) == []


def test_diff_reports_missing_and_changed_symbols():
    before = {"module": "m", "symbols": {
        "kept": {"kind": "function", "signature": "(a)"},
        "dropped": {"kind": "function", "signature": "()"},
        "retyped": {"kind": "function", "signature": "(a)"},
    }}
    after = {"module": "m", "symbols": {
        "kept": {"kind": "function", "signature": "(a)"},
        "retyped": {"kind": "function", "signature": "(a, b)"},
        "added": {"kind": "value", "signature": None},
    }}
    lines = api_snapshot.diff(before, after)
    assert any("dropped" in l and "missing" in l for l in lines)
    assert any("retyped" in l and "signature" in l for l in lines)
    assert any("added" in l for l in lines)
