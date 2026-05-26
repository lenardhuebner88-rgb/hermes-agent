from __future__ import annotations

from pathlib import Path

import pytest

from hermes_cli import kanban_db as kb
from hermes_cli.cli.kanban_scope_lint import TaskSpec, validate_task_spec
from hermes_cli.kanban_swarm import SwarmWorkerSpec, create_swarm
from hermes_cli.templates.task_template_builder import (
    DEFAULT_ALLOWED_TOOLS,
    DEFAULT_FORBIDDEN_SYSTEMS,
    build_task_template,
)
from tools import kanban_tools


@pytest.fixture
def kanban_home(tmp_path, monkeypatch):
    home = tmp_path / ".hermes"
    home.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(home))
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    for profile in ["coordinator", "coder", "reviewer", "research", "default"]:
        target = home if profile == "default" else home / "profiles" / profile
        target.mkdir(parents=True, exist_ok=True)
        (target / "skills").mkdir(parents=True, exist_ok=True)
    for profile, skill in [
        ("coordinator", "kanban-orchestrator"),
        ("reviewer", "requesting-code-review"),
        ("research", "avoid-ai-writing"),
    ]:
        skill_dir = home / "profiles" / profile / "skills" / skill
        skill_dir.mkdir(parents=True, exist_ok=True)
        (skill_dir / "SKILL.md").write_text(f"# {skill}\n", encoding="utf-8")
    kb.init_db()
    return home


def _validate_body(body: str, assignee: str) -> dict:
    return validate_task_spec(TaskSpec("file", None, None, body, assignee, []))


def test_builder_snapshot_default_body():
    template = build_task_template("coder", {"version": 2}, body="Implement the patch.")

    assert template.body_template == (
        "---\n"
        "assignee: \"coder\"\n"
        "report_contract_version: 1\n"
        "scope_contract:\n"
        "  version: 2\n"
        "  allowed_tools:\n"
        "    - \"kanban_show\"\n"
        "    - \"kanban_complete\"\n"
        "    - \"kanban_block\"\n"
        "    - \"kanban_comment\"\n"
        "    - \"kanban_heartbeat\"\n"
        "  forbidden_systems:\n"
        "    - \"OpenClaw\"\n"
        "    - \"Atlas\"\n"
        "    - \"Mission-Control\"\n"
        "    - \"Telegram\"\n"
        "---\n"
        "\n"
        "Implement the patch.\n"
    )


def test_builder_snapshot_override_body():
    template = build_task_template(
        "reviewer",
        {"version": 2},
        report_contract_version=2,
        allowed_tools=["kanban_show", "kanban_complete", "kanban_block", "skill_view"],
        forbidden_systems=["OpenClaw", "Atlas", "Mission-Control", "Telegram", "Discord"],
        body="Review evidence.",
    )

    assert template.body_template == (
        "---\n"
        "assignee: \"reviewer\"\n"
        "report_contract_version: 2\n"
        "scope_contract:\n"
        "  version: 2\n"
        "  allowed_tools:\n"
        "    - \"kanban_show\"\n"
        "    - \"kanban_complete\"\n"
        "    - \"kanban_block\"\n"
        "    - \"skill_view\"\n"
        "  forbidden_systems:\n"
        "    - \"OpenClaw\"\n"
        "    - \"Atlas\"\n"
        "    - \"Mission-Control\"\n"
        "    - \"Telegram\"\n"
        "    - \"Discord\"\n"
        "---\n"
        "\n"
        "Review evidence.\n"
    )


@pytest.mark.parametrize("profile", ["coordinator", "coder", "reviewer", "research", "default"])
def test_template_for_each_profile_validates(kanban_home, profile):
    template = build_task_template(profile, {"version": 2}, body=f"Work for {profile}.")
    payload = _validate_body(template.body_template, profile)

    assert payload["ok"] is True


def test_override_allowed_tools_preserved():
    allowed = ["kanban_show", "kanban_complete", "kanban_block", "skill_view"]
    template = build_task_template("coder", {"version": 2}, allowed_tools=allowed)

    assert template.allowed_tools == allowed
    assert template.scope_contract["allowed_tools"] == allowed


def test_forbidden_systems_default_from_profile_policy_floor():
    template = build_task_template("coder", {"version": 2})

    assert template.forbidden_systems == DEFAULT_FORBIDDEN_SYSTEMS


def test_skills_are_structured_but_not_rendered_into_scope_contract():
    template = build_task_template("coder", {"version": 2}, skills=["dogfood"])

    assert template.skills == ["dogfood"]
    assert "skills:" not in template.body_template


def test_as_task_kwargs_returns_create_task_shape():
    template = build_task_template("coder", {"version": 2}, body="Body")

    assert template.as_task_kwargs() == {
        "assignee": "coder",
        "body": template.body_template,
        "skills": None,
    }


def test_round_trip_scope_contract_identical(kanban_home):
    template = build_task_template("coder", {"version": 2}, body="Body")
    payload = _validate_body(template.body_template, "coder")

    assert payload["scope_contract_version"] == 2
    assert payload["effective_toolsets"] == DEFAULT_ALLOWED_TOOLS


def test_cli_create_can_use_template_builder_flags(kanban_home):
    from hermes_cli import kanban as kc
    import argparse
    import json

    parser = argparse.ArgumentParser(prog="hermes")
    sub = parser.add_subparsers(dest="command")
    kc.build_parser(sub)
    args = parser.parse_args(
        [
            "kanban",
            "create",
            "templated",
            "--assignee",
            "coder",
            "--body",
            "Body",
            "--scope-contract-json",
            json.dumps({"version": 2}),
            "--json",
        ]
    )
    assert kc.kanban_command(args) == 0
    with kb.connect() as conn:
        task = kb.list_tasks(conn)[0]
    assert "scope_contract:" in task.body


def test_tool_create_uses_template_builder_when_scope_contract_present(kanban_home):
    result = kanban_tools._handle_create(
        {
            "title": "tool templated",
            "assignee": "coder",
            "body": "Body",
            "scope_contract": {"version": 2},
        }
    )

    assert '"ok": true' in result
    with kb.connect() as conn:
        task = kb.list_tasks(conn)[0]
    assert "scope_contract:" in task.body


def test_swarm_worker_body_snapshot_uses_template_builder(kanban_home):
    with kb.connect() as conn:
        created = create_swarm(
            conn,
            goal="Goal.",
            workers=[SwarmWorkerSpec(profile="coder", title="Worker", body="Body")],
            verifier_assignee="reviewer",
            synthesizer_assignee="research",
            created_by="coordinator",
        )
        worker = kb.get_task(conn, created.worker_ids[0])

    assert worker.body.startswith("---\nassignee: \"coder\"\n")
    assert "scope_contract:\n  version: 2\n" in worker.body
    assert "Swarm root / shared blackboard" in worker.body


def test_swarm_verifier_and_synthesizer_bodies_are_templated(kanban_home):
    with kb.connect() as conn:
        created = create_swarm(
            conn,
            goal="Goal.",
            workers=[SwarmWorkerSpec(profile="coder", title="Worker", body="Body")],
            verifier_assignee="reviewer",
            synthesizer_assignee="research",
            created_by="coordinator",
        )
        verifier = kb.get_task(conn, created.verifier_id)
        synthesizer = kb.get_task(conn, created.synthesizer_id)

    assert verifier.body.startswith("---\nassignee: \"reviewer\"\n")
    assert synthesizer.body.startswith("---\nassignee: \"research\"\n")


def test_swarm_root_body_is_templated(kanban_home):
    with kb.connect() as conn:
        created = create_swarm(
            conn,
            goal="Goal.",
            workers=[SwarmWorkerSpec(profile="coder", title="Worker", body="Body")],
            verifier_assignee="reviewer",
            synthesizer_assignee="research",
            created_by="coordinator",
        )
        root = kb.get_task(conn, created.root_id)

    assert root.body.startswith("---\nassignee: \"coordinator\"\n")


def test_build_task_template_requires_profile():
    with pytest.raises(ValueError):
        build_task_template("", {"version": 2})


def test_synthetic_five_template_tasks_validate_and_emit_no_attestation_events(kanban_home):
    variants = [
        ("coordinator", "Coordinate work."),
        ("coder", "Implement work."),
        ("reviewer", "Review work."),
        ("research", "Research work."),
        ("default", "Default profile work."),
    ]
    with kb.connect() as conn:
        for profile, body in variants:
            template = build_task_template(profile, {"version": 2}, body=body)
            payload = _validate_body(template.body_template, profile)
            assert payload["ok"] is True
            kb.create_task(
                conn,
                title=f"synthetic {profile}",
                body=template.body_template,
                assignee=profile,
                internal_test_bypass_control_plane_gate=(profile == "coordinator"),
            )
        blocked = conn.execute(
            "SELECT COUNT(*) FROM task_events WHERE kind = 'completion_blocked_scope_attestation'"
        ).fetchone()[0]

    assert blocked == 0
