from __future__ import annotations

import importlib.util
from pathlib import Path
from types import ModuleType

import pytest

from hermes_cli.projects_overview import ProjectsRegistry, _coordination_agents


VAULT_PARSER_PATH = Path(
    "/home/piet/vault/_agents/_shared/scripts/coordination-open-sessions.py"
)
FIXTURE_DIR = Path(__file__).parent / "fixtures" / "coordination_parser"

pytestmark = pytest.mark.skipif(
    not VAULT_PARSER_PATH.exists(),
    reason="canonical Vault coordination parser is unavailable outside Piet's host",
)


def _load_vault_parser() -> ModuleType:
    spec = importlib.util.spec_from_file_location(
        "canonical_coordination_open_sessions", VAULT_PARSER_PATH
    )
    if spec is None or spec.loader is None:
        pytest.fail(f"could not load canonical parser from {VAULT_PARSER_PATH}")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_dashboard_and_vault_parser_detect_identical_open_note_set() -> None:
    vault_parser = _load_vault_parser()
    vault_sessions, _stats = vault_parser.iter_open_sessions(FIXTURE_DIR)
    vault_open = {Path(session["path"]).name for session in vault_sessions}

    dashboard_agents, errors = _coordination_agents(
        FIXTURE_DIR, registry=ProjectsRegistry()
    )
    dashboard_open = {f"{agent['label']}.md" for agent in dashboard_agents}

    only_vault = sorted(vault_open - dashboard_open)
    only_dashboard = sorted(dashboard_open - vault_open)
    assert errors == []
    assert dashboard_open == vault_open, (
        "coordination parser drift: "
        f"only canonical Vault parser considered open={only_vault}; "
        f"only Dashboard parser considered open={only_dashboard}"
    )
