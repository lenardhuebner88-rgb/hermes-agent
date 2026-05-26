"""F-2026-05-17-02 contract pin: ``effective_toolsets`` aligns with the
profile-runtime surface, not the raw contract.

The dispatcher's kanban-effective filter (model_tools.py) intentionally
overrides profile-level ``disabled_toolsets`` for spawned workers so they can
use dispatcher-approved coordination tools. But the metadata that gets
recorded into ``task_runs`` is consumed by ``capability_drift_detector``,
which flags ``actually_used_but_disabled`` per-profile. Recording the
unfiltered list re-surfaces dispatcher-overridden tools as false-positive
drift every cron tick.

These tests pin the post-F-02 behavior: ``kanban_completion_template`` and
``_validate_required_scope_attestation`` both apply
``_filter_tool_names_by_profile_disabled`` to align the metadata-record with
the profile's declared surface.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from hermes_cli import kanban_db as kb


@pytest.fixture
def kanban_home(tmp_path, monkeypatch):
    home = tmp_path / ".hermes"
    home.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(home))
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    kb.init_db()
    return home


def _write_profile_config(home: Path, profile: str, disabled_toolsets: list[str]) -> None:
    """Drop a profile config with the given ``agent.disabled_toolsets`` list."""
    pdir = home / "profiles" / profile
    pdir.mkdir(parents=True, exist_ok=True)
    import yaml as _yaml
    cfg = {
        "model": {"default": "gpt-test", "provider": "openai-codex"},
        "agent": {"disabled_toolsets": list(disabled_toolsets)},
    }
    (pdir / "config.yaml").write_text(_yaml.safe_dump(cfg), encoding="utf-8")


# ---------------------------------------------------------------------------
# _filter_tool_names_by_profile_disabled (unit)
# ---------------------------------------------------------------------------


def test_filter_with_no_profile_is_passthrough(kanban_home):
    result = kb._filter_tool_names_by_profile_disabled(
        ["kanban_show", "kanban_complete", "memory"], profile=None
    )
    assert result == ["kanban_show", "kanban_complete", "memory"]


def test_filter_with_empty_list_returns_empty(kanban_home):
    assert kb._filter_tool_names_by_profile_disabled([], profile="reviewer") == []


def test_filter_removes_tools_owned_by_disabled_toolsets(kanban_home):
    # Build a profile that disables the entire ``kanban`` toolset.
    _write_profile_config(kanban_home, "minimal-no-kanban", ["kanban"])
    candidate = ["kanban_show", "kanban_complete", "memory"]
    result = kb._filter_tool_names_by_profile_disabled(
        candidate, profile="minimal-no-kanban"
    )
    # kanban_show + kanban_complete belong to the kanban toolset → removed.
    # memory is not in the kanban toolset → kept.
    assert "kanban_show" not in result
    assert "kanban_complete" not in result
    assert "memory" in result


def test_filter_nonexistent_profile_is_passthrough(kanban_home):
    """Unknown profile names should never raise; they pass through unchanged."""
    result = kb._filter_tool_names_by_profile_disabled(
        ["kanban_show", "memory"], profile="no-such-profile"
    )
    assert result == ["kanban_show", "memory"]


# ---------------------------------------------------------------------------
# kanban_completion_template fallback / preflight filtering
# ---------------------------------------------------------------------------


def test_template_preflight_unchanged_when_profile_does_not_disable_any(
    kanban_home, monkeypatch
):
    """When the assignee profile disables nothing relevant, the preflight list
    flows through unchanged and source remains ``dispatch_preflight_passed``."""
    body = """
scope_contract:
  version: 2
  allowed_tools: [kanban_show, kanban_complete]
completion_policy:
  require_scope_attestation: true
"""
    _write_profile_config(kanban_home, "p-clean", [])  # nothing disabled
    monkeypatch.setattr(kb, "_KNOWN_VALID_ASSIGNEES", set(), raising=False)
    with kb.connect() as conn:
        t = kb.create_task(conn, title="clean", assignee="p-clean", body=body)
        conn.execute(
            "INSERT INTO task_events(task_id, kind, payload, created_at) VALUES (?, ?, ?, 1)",
            (
                t,
                "dispatch_preflight_passed",
                json.dumps({"effective_toolsets": ["kanban_show", "kanban_complete"]}),
            ),
        )
        conn.commit()
        template = kb.kanban_completion_template(conn, t)
    assert template["metadata"]["effective_toolsets"] == ["kanban_show", "kanban_complete"]
    # No filter happened → no F-02 marker stamped.
    assert "effective_toolsets_source" not in template["metadata"]
    assert template["effective_toolsets_source"] == "dispatch_preflight_passed"


def test_template_preflight_filtered_when_profile_disables_overlap(
    kanban_home, monkeypatch
):
    body = """
scope_contract:
  version: 2
  allowed_tools: [kanban_show, kanban_complete]
completion_policy:
  require_scope_attestation: true
"""
    # Profile that disables the kanban toolset entirely.
    _write_profile_config(kanban_home, "p-no-kanban", ["kanban"])
    with kb.connect() as conn:
        t = kb.create_task(conn, title="filtered", assignee="p-no-kanban", body=body)
        conn.execute(
            "INSERT INTO task_events(task_id, kind, payload, created_at) VALUES (?, ?, ?, 1)",
            (
                t,
                "dispatch_preflight_passed",
                json.dumps({"effective_toolsets": ["kanban_show", "kanban_complete"]}),
            ),
        )
        conn.commit()
        template = kb.kanban_completion_template(conn, t)
    # Both tools are kanban → all filtered → fallback marker, raw list preserved
    # so the validator can still pin against the event payload.
    assert template["effective_toolsets_source"] == "fallback_preflight_all_profile_disabled"
    assert template["metadata"]["effective_toolsets_source"] == "fallback_preflight_all_profile_disabled"
    assert template["metadata"]["effective_toolsets"] == ["kanban_show", "kanban_complete"]


def test_template_contract_fallback_filtered_when_profile_disables_overlap(
    kanban_home,
):
    """When no preflight event exists AND filter trims something, the contract
    falls back through the profile filter with the F-02 marker."""
    body = """
scope_contract:
  version: 2
  allowed_tools: [kanban_show, kanban_complete, memory]
completion_policy:
  require_scope_attestation: true
"""
    _write_profile_config(kanban_home, "p-no-kanban-2", ["kanban"])
    with kb.connect() as conn:
        t = kb.create_task(
            conn, title="contract-fallback", assignee="p-no-kanban-2", body=body
        )
        template = kb.kanban_completion_template(conn, t)
    # kanban_* removed; memory kept.
    assert template["metadata"]["effective_toolsets"] == ["memory"]
    assert template["metadata"]["effective_toolsets_source"] == "filtered_contract_by_profile"
    assert template["effective_toolsets_source"] == "filtered_contract_by_profile"
    # And the diagnostic still surfaces the missing-preflight context.
    assert template["diagnostic"]["code"] == "dispatch_preflight_missing"


def test_template_contract_fallback_unchanged_when_filter_is_noop(kanban_home):
    """No profile / no overlap → raw contract, legacy source label preserved."""
    body = """
scope_contract:
  version: 2
  allowed_tools: [kanban_show, kanban_complete]
completion_policy:
  require_scope_attestation: true
"""
    with kb.connect() as conn:
        t = kb.create_task(conn, title="legacy", assignee="alice", body=body)
        template = kb.kanban_completion_template(conn, t)
    assert template["metadata"]["effective_toolsets"] == ["kanban_show", "kanban_complete"]
    # Backwards-compat: no F-02 marker, legacy source string preserved.
    assert "effective_toolsets_source" not in template["metadata"]
    assert template["effective_toolsets_source"] == "scope_contract.allowed_tools"
