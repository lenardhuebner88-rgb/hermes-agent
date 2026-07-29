import textwrap

import pytest

from scripts.refactor import split_module


def test_analyze_flags_import_time_backward_reference(tmp_path):
    src = tmp_path / "m.py"
    src.write_text(textwrap.dedent("""
        # ---------------------------------------------------------------------------
        # Alpha
        # ---------------------------------------------------------------------------
        FIRST = SECOND + 1

        # ---------------------------------------------------------------------------
        # Beta
        # ---------------------------------------------------------------------------
        SECOND = 2
    """))
    report = split_module.analyze(str(src))
    assert report["import_time_backward"] == [("FIRST", "SECOND")]


def test_analyze_reports_runtime_backward_without_flagging_it_fatal(tmp_path):
    src = tmp_path / "m2.py"
    src.write_text(textwrap.dedent("""
        # ---------------------------------------------------------------------------
        # Alpha
        # ---------------------------------------------------------------------------
        def early():
            return late()

        # ---------------------------------------------------------------------------
        # Beta
        # ---------------------------------------------------------------------------
        def late():
            return 1
    """))
    report = split_module.analyze(str(src))
    assert report["import_time_backward"] == []
    assert report["runtime_backward"] == [("early", "late")]
    assert report["fatal"] is False


def test_analyze_marks_import_time_backward_as_fatal(tmp_path):
    src = tmp_path / "m3.py"
    src.write_text(textwrap.dedent("""
        # ---------------------------------------------------------------------------
        # Alpha
        # ---------------------------------------------------------------------------
        FIRST = SECOND

        # ---------------------------------------------------------------------------
        # Beta
        # ---------------------------------------------------------------------------
        SECOND = 2
    """))
    assert split_module.analyze(str(src))["fatal"] is True


def test_ownership_separates_the_three_buckets(tmp_path, monkeypatch):
    import subprocess

    repo = tmp_path / "r"
    repo.mkdir()
    monkeypatch.chdir(repo)
    subprocess.run(["git", "init", "-q"], check=True)
    subprocess.run(["git", "config", "user.email", "t@t"], check=True)
    subprocess.run(["git", "config", "user.name", "t"], check=True)

    (repo / "m.py").write_text(
        "SHARED_SAME = 1\n\n\ndef shared_changed():\n    return 1\n")
    subprocess.run(["git", "add", "m.py"], check=True)
    subprocess.run(["git", "commit", "-qm", "up"], check=True)
    subprocess.run(["git", "branch", "-M", "upstream"], check=True)

    (repo / "m.py").write_text(
        "SHARED_SAME = 1\n\n\ndef shared_changed():\n    return 2\n\n\n"
        "def fork_only():\n    return 3\n")

    rep = split_module.ownership("m.py", upstream_ref="upstream")
    assert rep["fork_only"] == ["fork_only"]
    assert rep["upstream_identical"] == ["SHARED_SAME"]
    assert rep["upstream_diverged"] == ["shared_changed"]


EXTRACT_SOURCE = '''\
"""Origin module docstring."""
import os

DEFAULT_TTL = 300


def connect():
    return "conn:" + os.sep


def upstream_uses_fork():
    return fork_helper() + "|" + str(DEFAULT_TTL)


def fork_helper():
    return "fork(" + connect() + "," + str(DEFAULT_TTL) + ")"
'''


def test_extract_leaves_origin_a_file_and_moves_only_mapped_symbols(
    tmp_path, monkeypatch
):
    monkeypatch.syspath_prepend(str(tmp_path))
    src = tmp_path / "origin.py"
    src.write_text(EXTRACT_SOURCE)
    split_module.extract_to_package(
        str(src),
        {"modules": [{"name": "helpers", "symbols": ["fork_helper"]}]},
        str(tmp_path / "origin_ext"),
    )

    assert src.is_file()
    assert (tmp_path / "origin_ext" / "helpers.py").exists()
    text = src.read_text()
    assert "def connect()" in text
    assert "def fork_helper()" not in text
    assert text.rstrip().endswith(")")
    assert "from origin_ext import (" in text


def test_extract_imports_stayed_symbols_without_rewriting_body(tmp_path):
    src = tmp_path / "origin_imports.py"
    src.write_text(EXTRACT_SOURCE)
    split_module.extract_to_package(
        str(src),
        {"modules": [{"name": "helpers", "symbols": ["fork_helper"]}]},
        str(tmp_path / "origin_imports_ext"),
    )

    helpers = (tmp_path / "origin_imports_ext" / "helpers.py").read_text()
    assert "from origin_imports import DEFAULT_TTL, connect" in helpers
    assert (
        'def fork_helper():\n'
        '    return "fork(" + connect() + "," + str(DEFAULT_TTL) + ")"'
    ) in helpers
    assert "origin_imports.connect()" not in helpers
    assert "return fork_helper() + " in src.read_text()


def test_extract_rewrites_only_intra_package_backward_edges(tmp_path):
    src = tmp_path / "edges.py"
    src.write_text(textwrap.dedent('''\
        """Edge fixture."""
        import os

        LIMIT = 3


        def early(n):
            return late(n) + LIMIT


        def late(n):
            return n * 2


        def stays():
            return os.sep
    '''))
    split_module.extract_to_package(
        str(src),
        {"modules": [
            {"name": "consts", "symbols": ["LIMIT"]},
            {"name": "front", "symbols": ["early"]},
            {"name": "back", "symbols": ["late"]},
        ]},
        str(tmp_path / "edges_ext"),
    )

    front = (tmp_path / "edges_ext" / "front.py").read_text()
    assert "from .consts import LIMIT" in front
    assert "from . import back" in front
    assert "return back.late(n) + LIMIT" in front


def test_extract_copies_docstring_source_span_and_header(tmp_path):
    source = """r'''Origin says \"\"\"quoted\"\"\".'''
from __future__ import annotations
import os


def moved():
    return os.sep
"""
    src = tmp_path / "rawdoc.py"
    src.write_text(source)
    split_module.extract_to_package(
        str(src),
        {"modules": [{"name": "part", "symbols": ["moved"]}]},
        str(tmp_path / "rawdoc_ext"),
    )

    init = (tmp_path / "rawdoc_ext" / "__init__.py").read_text()
    part = (tmp_path / "rawdoc_ext" / "part.py").read_text()
    assert init.startswith("r'''Origin says \"\"\"quoted\"\"\".'''")
    assert "from __future__ import annotations" in part
    assert "import os" in part


def test_extract_preserves_behaviour_and_api(tmp_path, monkeypatch):
    from scripts.refactor import api_snapshot

    monkeypatch.syspath_prepend(str(tmp_path))
    src = tmp_path / "origin2.py"
    src.write_text(EXTRACT_SOURCE)
    before = api_snapshot.snapshot("origin2", fresh=True)

    split_module.extract_to_package(
        str(src),
        {"modules": [{"name": "helpers", "symbols": ["fork_helper"]}]},
        str(tmp_path / "origin2_ext"),
    )

    after = api_snapshot.snapshot("origin2", fresh=True)
    assert api_snapshot.diff(before, after) == []

    import origin2
    assert origin2.upstream_uses_fork() == "fork(conn:/,300)|300"


def test_extract_refuses_import_time_reference_into_the_extracted_set(tmp_path):
    src = tmp_path / "origin3.py"
    src.write_text(
        "FORK_CONST = 'fc'\n\n\ndef stays(_x=FORK_CONST):\n    return _x\n"
    )
    with pytest.raises(SystemExit):
        split_module.extract_to_package(
            str(src),
            {"modules": [{"name": "c", "symbols": ["FORK_CONST"]}]},
            str(tmp_path / "origin3_ext"),
        )
    assert src.exists()
    assert not (tmp_path / "origin3_ext").exists()


def test_local_binding_shadowing_a_backward_target_is_not_rewritten(
    tmp_path, capsys
):
    """Emission rule 8: a local `late` must not become `back.late`."""
    src = tmp_path / "shadow.py"
    src.write_text(textwrap.dedent('''\
        def early(n):
            calls = [late(n)]
            late = n + 100
            return calls, late


        def shadow_param(late):
            return late * 2


        def late(n):
            return n * 2
    '''))
    split_module.extract_to_package(
        str(src),
        {"modules": [
            {"name": "front", "symbols": ["early", "shadow_param"]},
            {"name": "back", "symbols": ["late"]},
        ]},
        str(tmp_path / "shadow_ext"),
    )

    front = (tmp_path / "shadow_ext" / "front.py").read_text()
    assert "back.late" not in front
    assert "return late * 2" in front
    assert "REVIEW REQUIRED" in capsys.readouterr().out


def test_extract_refuses_split_binding_across_modules(tmp_path):
    """Emission rule 9."""
    src = tmp_path / "binding.py"
    src.write_text("A = B = 1\n")
    bmap = {"modules": [
        {"name": "one", "symbols": ["A"]},
        {"name": "two", "symbols": ["B"]},
    ]}
    with pytest.raises(SystemExit):
        split_module.extract_to_package(
            str(src), bmap, str(tmp_path / "binding_ext")
        )
    assert src.exists()
    assert not (tmp_path / "binding_ext").exists()


def test_docstring_span_is_none_for_empty_module():
    """An empty module has no body at all — the docstring probe must
    answer None, not crash on body[0]."""
    import ast

    assert split_module._docstring_span(ast.parse("")) is None
    assert split_module._docstring_span(ast.parse('"""Doc."""\nx = 1\n')) == (1, 1)


def test_locally_bound_anywhere_counts_plain_import_names():
    """`import os` binds 'os' (asname absent) — missing it would let the
    backward-ref rewrite clobber a locally-imported name."""
    import ast

    tree = ast.parse(textwrap.dedent("""
        def f():
            import os
            return os.name
    """))
    bound = split_module.locally_bound_anywhere(tree.body[0])
    assert "os" in bound


def test_section_owner_counts_banner_on_symbol_line_as_that_section(
    tmp_path, monkeypatch
):
    """A banner AT the symbol's own line (<=, not <) owns the symbol —
    off-by-one here silently re-homes the symbol to __head__."""
    src = tmp_path / "edge.py"
    src.write_text("def f():\n    return 1\n")
    monkeypatch.setattr(
        split_module.layering, "banner_sections", lambda lines: [(1, "Same")]
    )
    report = split_module.analyze(str(src))
    assert report["modules"] == ["Same"]


def test_analyze_oversized_boundary_is_strictly_above_4000(tmp_path):
    """A section of EXACTLY 4000 lines is not oversized — the threshold is
    strict; flagging the boundary would cry wolf on the limit case."""
    src = tmp_path / "big.py"
    src.write_text("def f():\n" + "    pass\n" * 3999)
    report = split_module.analyze(str(src))
    assert report["module_lines"]["__head__"] == 4000
    assert report["oversized"] == []


def test_import_name_for_path_returns_dotted_relative(tmp_path, monkeypatch):
    """Inside the cwd the import name is the dotted relative path, never
    the bare basename — basename imports would collide across packages."""
    monkeypatch.chdir(tmp_path)
    assert split_module._import_name_for_path("pkg/mod.py") == "pkg.mod"


# ---------------------------------------------------------------------------
# Third pass: rewrite offsets + extract edge cases
# ---------------------------------------------------------------------------

def test_rewrite_backward_refs_applies_right_to_left_on_one_line():
    """Two references on ONE line are rewritten right-to-left so column
    offsets stay valid — left-to-right would shift the second hit and
    abort with an offset mismatch."""
    import ast as _ast

    node = _ast.parse("def f():\n    return A + A\n").body[0]
    body = ["    return A + A"]
    out = split_module._rewrite_backward_refs(body, 2, node, {"A": "mod"}, [])
    assert out == ["    return mod.A + mod.A"]


def test_rewrite_backward_refs_bounds_checks():
    """Edits whose mapped index falls outside the body window are
    skipped — never applied at a negative or past-the-end index."""
    import ast as _ast

    # idx < 0: node line above first_lineno -> skipped
    node_before = _ast.parse("A\n").body[0]
    assert split_module._rewrite_backward_refs(
        ["zzz"], 3, node_before, {"A": "m"}, []
    ) == ["zzz"]

    # idx == 0: in bounds -> rewritten
    node_zero = _ast.parse("A\n").body[0]
    assert split_module._rewrite_backward_refs(
        ["A"], 1, node_zero, {"A": "m"}, []
    ) == ["m.A"]

    # idx == len(body): past the end -> skipped
    node_past = _ast.parse("x\nA\n").body[1]
    assert split_module._rewrite_backward_refs(
        ["y"], 1, node_past, {"A": "m"}, []
    ) == ["y"]


def test_extract_tolerates_symbol_listed_twice_in_same_module(tmp_path):
    """Listing a symbol twice in the SAME target module is a harmless
    duplicate — only placement in two DIFFERENT modules is refused."""
    src = tmp_path / "origin.py"
    src.write_text("A = 1\n")
    pkg = tmp_path / "origin_ext"
    split_module.extract_to_package(
        str(src),
        {"modules": [{"name": "consts", "symbols": ["A", "A"]}]},
        str(pkg),
    )
    assert (pkg / "consts.py").exists()


def test_extract_origin_keeps_single_blank_separator(tmp_path):
    """Trailing blank lines of the remaining origin survive as exactly
    one blank separator before the re-export block — the second
    endswith guard must not add a stacked newline."""
    src = tmp_path / "origin.py"
    src.write_text("A = 1\nB = 2\n\n")
    split_module.extract_to_package(
        str(src),
        {"modules": [{"name": "consts", "symbols": ["A"]}]},
        str(tmp_path / "origin_ext"),
    )
    text = src.read_text()
    assert "B = 2\n\nfrom origin_ext import (" in text
