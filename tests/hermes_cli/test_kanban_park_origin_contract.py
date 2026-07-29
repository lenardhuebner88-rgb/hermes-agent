"""Static contract for parks that hand work to the conflict fixer.

This inventories current callers of the shared system-park helper.  It does
NOT prove that a future park producer which bypasses that helper will be
caught; such a producer is intentionally invisible to this inventory.
"""

from __future__ import annotations

import ast
from pathlib import Path


_REPO_ROOT = Path(__file__).resolve().parents[2]
_HERMES_CLI_ROOT = _REPO_ROOT / "hermes_cli"
_PARK_HELPER = "_system_park_set_blocked"
_FIXER_ROUTER = "_maybe_route_conflict_park_fixer"
_RESUME_GUARD = "_resume_parent_for_completed_conflict_fixer"
_INTEGRATION_PARK_GUARD = "is_integration_park"


def _top_level_functions(tree: ast.Module) -> list[ast.FunctionDef | ast.AsyncFunctionDef]:
    return [
        node
        for node in tree.body
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
    ]


def _calls_named(
    function: ast.FunctionDef | ast.AsyncFunctionDef, name: str
) -> list[ast.Call]:
    return [
        node
        for node in ast.walk(function)
        if isinstance(node, ast.Call)
        and isinstance(node.func, ast.Name)
        and node.func.id == name
    ]


def _has_string_keyword(call: ast.Call, keyword: str, value: str) -> bool:
    return any(
        argument.arg == keyword
        and isinstance(argument.value, ast.Constant)
        and argument.value.value == value
        for argument in call.keywords
    )


def _has_name_keyword(call: ast.Call, keyword: str, value: str) -> bool:
    return any(
        argument.arg == keyword
        and isinstance(argument.value, ast.Name)
        and argument.value.id == value
        for argument in call.keywords
    )


def test_fixer_routed_system_parks_are_recognized_by_resume_guard():
    """Pin the measured fixer-routed park origins, not a hand-maintained list.

    A future park producer that does not call ``_system_park_set_blocked`` is
    not detected by this inventory.  This test deliberately does not claim to
    be a future-producer breaker.
    """
    functions: list[ast.FunctionDef | ast.AsyncFunctionDef] = []
    for source_path in sorted(_HERMES_CLI_ROOT.rglob("*.py")):
        tree = ast.parse(
            source_path.read_text(encoding="utf-8-sig"), filename=str(source_path)
        )
        functions.extend(_top_level_functions(tree))

    fixer_routed_parkers = [
        function
        for function in functions
        if _calls_named(function, _PARK_HELPER)
        and _calls_named(function, _FIXER_ROUTER)
    ]

    assert fixer_routed_parkers, "the shared-park inventory found no fixer route"
    for function in fixer_routed_parkers:
        park_calls = _calls_named(function, _PARK_HELPER)
        assert any(
            _has_string_keyword(call, "kind", "integration") for call in park_calls
        ), f"{function.name} routes a fixer without an integration park"

    resume_guards = [function for function in functions if function.name == _RESUME_GUARD]
    assert len(resume_guards) == 1, "the fixer resume guard must have one definition"
    resume_guard = resume_guards[0]
    integration_guards = _calls_named(resume_guard, _INTEGRATION_PARK_GUARD)
    assert integration_guards, "the fixer resume guard does not recognize integration parks"
    assert any(
        _has_name_keyword(call, "reason", "current_reason")
        and any(
            argument.arg == "block_kind"
            and isinstance(argument.value, ast.Call)
            and isinstance(argument.value.func, ast.Name)
            and argument.value.func.id == "blocked_event_kind"
            for argument in call.keywords
        )
        for call in integration_guards
    ), "the resume guard must classify the blocked event's typed park origin"
