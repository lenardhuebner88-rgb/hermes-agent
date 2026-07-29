import json
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


# ---------------------------------------------------------------------------
# Import-semantics + CLI pinning
# ---------------------------------------------------------------------------

def test_snapshot_default_reads_cached_module_and_describes_classes(module_pair):
    """The default (fresh=False) reads the CACHED module object — later
    mutations are visible — and classes are described with their methods,
    not flattened to 'value'."""
    (module_pair / "cached_mod.py").write_text(
        "X = 1\n"
        "class K:\n"
        "    def m(self):\n"
        "        pass\n",
        encoding="utf-8",
    )
    first = api_snapshot.snapshot("cached_mod")
    assert first["symbols"]["K"]["kind"] == "class"
    assert "m" in first["symbols"]["K"]["methods"]

    import cached_mod

    cached_mod.injected_later = 42
    again = api_snapshot.snapshot("cached_mod")
    assert "injected_later" in again["symbols"]


def test_snapshot_fresh_purges_and_records_private_non_dunder_names(module_pair):
    """fresh=True reimports (dropping cached mutations); underscore-private
    and '__'-prefixed-but-not-suffixed names are recorded — only full
    dunders are skipped."""
    (module_pair / "fresh_mod.py").write_text("X = 1\n", encoding="utf-8")
    api_snapshot.snapshot("fresh_mod")

    import fresh_mod

    setattr(fresh_mod, "injected", 1)
    setattr(fresh_mod, "__custom", 7)  # not a full dunder

    stale = api_snapshot.snapshot("fresh_mod")
    assert "injected" in stale["symbols"]
    assert "__custom" in stale["symbols"]

    clean = api_snapshot.snapshot("fresh_mod", fresh=True)
    assert "injected" not in clean["symbols"]


def test_main_writes_sorted_indented_json_of_a_fresh_snapshot(
    module_pair, tmp_path
):
    """main() snapshots FRESH (cached pollution must not leak into the
    artifact) and writes the exact indent=2, sort_keys serialization."""
    (module_pair / "main_mod.py").write_text(
        "def f(a):\n    return a\nY = 2\n", encoding="utf-8"
    )
    import main_mod

    main_mod.polluted = 1  # cache pollution — fresh snapshot must drop it

    out = tmp_path / "snap.json"
    assert api_snapshot.main(["main_mod", "--out", str(out)]) == 0
    text = out.read_text(encoding="utf-8")
    payload = json.loads(text)
    assert "polluted" not in payload["symbols"]
    assert text == json.dumps(payload, indent=2, sort_keys=True)


def test_main_compare_exit_codes(module_pair, tmp_path, capsys):
    """Identical surface -> exit 0 with 'API IDENTICAL'; any difference
    -> exit 1. Neither path may be inverted."""
    (module_pair / "cmp_mod.py").write_text("A = 1\n", encoding="utf-8")
    snap_path = tmp_path / "before.json"
    assert api_snapshot.main(["cmp_mod", "--out", str(snap_path)]) == 0

    assert api_snapshot.main(["cmp_mod", "--compare", str(snap_path)]) == 0
    assert "API IDENTICAL" in capsys.readouterr().out

    before = json.loads(snap_path.read_text(encoding="utf-8"))
    before["symbols"]["ghost"] = {"kind": "value", "signature": None}
    divergent = tmp_path / "divergent.json"
    divergent.write_text(json.dumps(before), encoding="utf-8")
    assert api_snapshot.main(["cmp_mod", "--compare", str(divergent)]) == 1


def test_main_without_out_or_compare_just_snapshots(module_pair):
    """No --out/--compare is a valid 'just validate it imports' run —
    exit 0, no crash on absent paths."""
    (module_pair / "bare_mod.py").write_text("B = 3\n", encoding="utf-8")
    assert api_snapshot.main(["bare_mod"]) == 0
