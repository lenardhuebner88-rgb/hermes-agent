"""Every subcommand parser builder must actually be wired into the CLI.

Fork-owned guard against a silent upstream-merge failure mode.

When an upstream merge resolves ``hermes_cli/main.py`` as *ours*, we keep our
implementation of that file but still take upstream's new ``subcommands/``
modules, their tests and their docs.  The registration lines that upstream
added to *its* ``main.py`` are dropped on the floor.  The feature is then
unreachable from the CLI while upstream's own tests stay green, because those
tests call ``build_<x>_parser`` directly instead of going through the real
parser tree.

That is exactly what happened to ``import-agent`` after the T1 merge
(2026-08-05): ``hermes_cli/agent_import.py``, ``subcommands/import_agent.py``,
52 passing tests and a user-guide page all landed, but ``hermes import-agent``
answered ``invalid choice``.  42 of 43 builders were registered; that one was
not, and nothing in the suite noticed.

The check is deliberately structural (AST, not grep): a builder that is only
mentioned in a comment or a docstring does not count as registered.
"""

from __future__ import annotations

import ast
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]
MAIN_PY = REPO_ROOT / "hermes_cli" / "main.py"
SUBCOMMANDS_DIR = REPO_ROOT / "hermes_cli" / "subcommands"

# Builders that are intentionally NOT wired into the CLI.  Add an entry only
# together with the reason — an empty allowlist is the healthy state.
DELIBERATELY_UNREGISTERED: dict[str, str] = {}


def _parse(path: Path) -> ast.Module:
    # utf-8-sig: a single BOM'd file would otherwise raise inside ast.parse and
    # surface as "no symbols found" rather than as an error.
    return ast.parse(path.read_text(encoding="utf-8-sig"), filename=str(path))


def _builders() -> dict[str, Path]:
    """Top-level ``build_*_parser`` functions defined under subcommands/."""
    found: dict[str, Path] = {}
    for path in sorted(SUBCOMMANDS_DIR.glob("*.py")):
        for node in _parse(path).body:
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                if node.name.startswith("build_") and node.name.endswith("_parser"):
                    found[node.name] = path
    return found


def _called_in_main() -> set[str]:
    """Names invoked as a plain call anywhere in main.py."""
    called: set[str] = set()
    for node in ast.walk(_parse(MAIN_PY)):
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Name):
            called.add(node.func.id)
    return called


def test_subcommands_dir_is_readable():
    """Control probe: without this, every assertion below passes vacuously."""
    builders = _builders()
    assert len(builders) >= 30, (
        f"only {len(builders)} parser builders found under {SUBCOMMANDS_DIR} — "
        "the discovery itself is broken, so the registration check below proves nothing"
    )
    assert "build_kanban_parser" in builders or "build_config_parser" in builders, (
        f"neither of two known builders was discovered; found: {sorted(builders)[:10]}"
    )


def test_main_py_call_extraction_works():
    """Control probe for the other half: main.py must yield real call names."""
    called = _called_in_main()
    assert "build_config_parser" in called, (
        "build_config_parser is registered in main.py but the AST walk did not "
        "see it — the extraction is broken, not the wiring"
    )


@pytest.mark.parametrize("builder", sorted(_builders()))
def test_builder_is_registered_in_main(builder: str):
    if builder in DELIBERATELY_UNREGISTERED:
        pytest.skip(f"allowlisted: {DELIBERATELY_UNREGISTERED[builder]}")
    defining_file = _builders()[builder].relative_to(REPO_ROOT)
    assert builder in _called_in_main(), (
        f"{builder} is defined in {defining_file} but never called in "
        f"hermes_cli/main.py — the subcommand is unreachable from the CLI. "
        f"Either register it in main.py or add it to DELIBERATELY_UNREGISTERED "
        f"with a reason."
    )
