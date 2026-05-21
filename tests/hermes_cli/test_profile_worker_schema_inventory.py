"""P1.10 profile/worker model-native schema inventory tests.

These tests intentionally avoid dispatcher spawns, MCP discovery, and real
profile/config mutation.  Each scenario builds an isolated HERMES_HOME and
introspects the model-native schema through the same resolver path the CLI uses.
"""
from __future__ import annotations

import json
from pathlib import Path

import pytest

import model_tools
from hermes_cli.config import _LOAD_CONFIG_CACHE, _RAW_CONFIG_CACHE, load_config
from hermes_cli.tools_config import _get_platform_tools
from tools.registry import invalidate_check_fn_cache


KANBAN_TOOLS = {
    "kanban_show",
    "kanban_complete",
    "kanban_completion_template",
    "kanban_block",
    "kanban_heartbeat",
    "kanban_comment",
    "kanban_create",
    "kanban_link",
    "kanban_update_profile_model",
}

WORKER_PROFILE_NAMES = ("dispatcher", "planner", "admin", "coder")

WORKER_ALLOWED_TOOLS = [
    "kanban_show",
    "kanban_complete",
    "kanban_completion_template",
    "kanban_block",
    "kanban_comment",
    "read_file",
    "search_files",
    "todo",
]

WORKER_EXPECTED_SCHEMA = [
    "kanban_block",
    "kanban_comment",
    "kanban_complete",
    "kanban_completion_template",
    "kanban_show",
    "read_file",
    "search_files",
    "todo",
]

WORKER_FORBIDDEN_TOOLS = {
    "terminal",
    "process",
    "write_file",
    "patch",
    "web_search",
    "web_extract",
    "kanban_create",
    "kanban_update_profile_model",
    "memory",
    "skill_manage",
    "delegate_task",
}


def _write_profile_config(
    home: Path,
    *,
    root_toolsets: list[str],
    cli_toolsets: list[str],
    disabled_toolsets: list[str],
) -> None:
    home.mkdir(parents=True, exist_ok=True)
    config = {
        "toolsets": root_toolsets,
        "platform_toolsets": {"cli": cli_toolsets},
        "agent": {"disabled_toolsets": disabled_toolsets},
    }
    (home / "config.yaml").write_text(
        _dump_simple_yaml(config),
        encoding="utf-8",
    )


def _dump_simple_yaml(config: dict) -> str:
    """Dump the tiny deterministic config shape used by these tests."""
    lines: list[str] = []
    lines.append("toolsets:")
    for item in config["toolsets"]:
        lines.append(f"- {item}")
    lines.append("platform_toolsets:")
    lines.append("  cli:")
    for item in config["platform_toolsets"]["cli"]:
        lines.append(f"  - {item}")
    lines.append("agent:")
    lines.append("  disabled_toolsets:")
    for item in config["agent"]["disabled_toolsets"]:
        lines.append(f"  - {item}")
    return "\n".join(lines) + "\n"


@pytest.fixture(autouse=True)
def reset_schema_state(monkeypatch):
    monkeypatch.delenv("HERMES_KANBAN_TASK", raising=False)
    monkeypatch.delenv("HERMES_KANBAN_EFFECTIVE_TOOLSETS", raising=False)
    model_tools._tool_defs_cache.clear()
    invalidate_check_fn_cache()
    _LOAD_CONFIG_CACHE.clear()
    _RAW_CONFIG_CACHE.clear()
    yield
    model_tools._tool_defs_cache.clear()
    invalidate_check_fn_cache()
    _LOAD_CONFIG_CACHE.clear()
    _RAW_CONFIG_CACHE.clear()


def _schema_names_for_home(monkeypatch, home: Path) -> list[str]:
    monkeypatch.setenv("HERMES_HOME", str(home))
    model_tools._tool_defs_cache.clear()
    invalidate_check_fn_cache()
    _LOAD_CONFIG_CACHE.clear()
    _RAW_CONFIG_CACHE.clear()

    config = load_config()
    enabled_toolsets = sorted(_get_platform_tools(config, "cli"))
    disabled_toolsets = config.get("agent", {}).get("disabled_toolsets") or []
    schemas = model_tools.get_tool_definitions(
        enabled_toolsets=enabled_toolsets,
        disabled_toolsets=disabled_toolsets,
        quiet_mode=True,
    )
    return sorted(
        schema["function"]["name"]
        for schema in schemas
        if isinstance(schema, dict) and isinstance(schema.get("function"), dict)
    )


def test_kanban_tool_inventory_exposes_completion_template_in_model_visible_surfaces():
    from toolsets import TOOLSETS, _HERMES_CORE_TOOLS

    assert "kanban_completion_template" in _HERMES_CORE_TOOLS
    assert "kanban_completion_template" in TOOLSETS["kanban"]["tools"]


def _worker_profile_home(tmp_path: Path, profile_name: str) -> Path:
    home = tmp_path / profile_name
    _write_profile_config(
        home,
        root_toolsets=["hermes-cli"],
        cli_toolsets=[
            "clarify",
            "file",
            "kanban",
            "memory",
            "session_search",
            "skills",
            "terminal",
            "todo",
        ],
        disabled_toolsets=[
            "browser",
            "code_execution",
            "cronjob",
            "delegation",
            "homeassistant",
            "image_gen",
            "messaging",
            "tts",
            "web",
        ],
    )
    return home


@pytest.mark.parametrize("profile_name", WORKER_PROFILE_NAMES)
def test_non_worker_profiles_exclude_kanban_without_task_env(tmp_path, monkeypatch, profile_name):
    home = _worker_profile_home(tmp_path, profile_name)

    names = _schema_names_for_home(monkeypatch, home)

    assert not (KANBAN_TOOLS & set(names))


def test_default_profile_includes_kanban_from_root_toolsets(tmp_path, monkeypatch):
    home = tmp_path / "default"
    _write_profile_config(
        home,
        root_toolsets=["hermes-cli", "kanban"],
        cli_toolsets=[
            "clarify",
            "delegation",
            "file",
            "kanban",
            "memory",
            "session_search",
            "skills",
            "terminal",
            "todo",
        ],
        disabled_toolsets=[
            "browser",
            "code_execution",
            "cronjob",
            "homeassistant",
            "image_gen",
            "messaging",
            "tts",
            "web",
        ],
    )

    names = _schema_names_for_home(monkeypatch, home)

    assert KANBAN_TOOLS <= set(names)


def test_kanban_worker_effective_allowed_tools_narrows_schema_to_tool_names(tmp_path, monkeypatch):
    home = _worker_profile_home(tmp_path, "coder")
    monkeypatch.setenv("HERMES_KANBAN_TASK", "t_schema_inventory")
    monkeypatch.setenv("HERMES_KANBAN_EFFECTIVE_TOOLSETS", json.dumps(WORKER_ALLOWED_TOOLS))

    names = _schema_names_for_home(monkeypatch, home)

    assert names == WORKER_EXPECTED_SCHEMA
    assert not (WORKER_FORBIDDEN_TOOLS & set(names))
