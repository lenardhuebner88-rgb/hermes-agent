from __future__ import annotations

import argparse
import json
from pathlib import Path

import pytest

from hermes_cli import kanban as kc
from hermes_cli import kanban_db as kb
from hermes_cli.cli.kanban_scope_lint import TaskSpec, load_task_spec, validate_task_spec
from hermes_cli.templates.task_template_builder import build_task_template


def _run_kanban_command(argv: list[str]) -> tuple[int, dict]:
    parser = argparse.ArgumentParser(prog="hermes")
    sub = parser.add_subparsers(dest="command")
    kc.build_parser(sub)
    args = parser.parse_args(["kanban", *argv])
    code = kc.kanban_command(args)
    return code, {}


@pytest.fixture
def kanban_home(tmp_path, monkeypatch):
    home = tmp_path / ".hermes"
    home.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(home))
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    (home / "profiles" / "coder" / "skills" / "dogfood").mkdir(parents=True)
    (home / "profiles" / "coder" / "skills" / "dogfood" / "SKILL.md").write_text(
        "# dogfood\n", encoding="utf-8"
    )
    (home / "profiles" / "reviewer").mkdir(parents=True)
    kb.init_db()
    return home


def _valid_body(profile: str = "coder", **overrides) -> str:
    return build_task_template(
        profile,
        {
            "version": 2,
            "allowed_tools": [
                "kanban_show",
                "kanban_complete",
                "kanban_block",
                "kanban_comment",
                "kanban_heartbeat",
            ],
        },
        body="Do the scoped work.",
        **overrides,
    ).body_template


def _codes(payload: dict) -> set[str]:
    return {e["code"] for e in payload["errors"]}


def test_valid_task_file_exits_zero(kanban_home, tmp_path, capsys):
    task_file = tmp_path / "task.md"
    task_file.write_text(_valid_body(), encoding="utf-8")

    parser = argparse.ArgumentParser(prog="hermes")
    sub = parser.add_subparsers(dest="command")
    kc.build_parser(sub)
    args = parser.parse_args(["kanban", "validate-spec", str(task_file)])
    code = kc.kanban_command(args)
    payload = json.loads(capsys.readouterr().out)

    assert code == 0
    assert payload["ok"] is True
    assert payload["errors"] == []


@pytest.mark.parametrize(
    "body,expected",
    [
        (_valid_body(profile="missing-profile"), "invalid_assignee"),
        (
            _valid_body(
                allowed_tools=["*"],
            ),
            "invalid_allowed_tools",
        ),
        (
            _valid_body(
                allowed_tools=["not_a_tool"],
            ),
            "invalid_allowed_tools",
        ),
        (
            """---
assignee: "coder"
report_contract_version: 1
scope_contract:
  version: 2
  allowed_tools:
    - "kanban_show"
    - "kanban_complete"
    - "kanban_block"
completion_policy:
  require_scope_attestation: true
---
body
""",
            "missing_forbidden_systems",
        ),
        (
            """---
assignee: "coder"
report_contract_version: 1
completion_policy:
  require_scope_attestation: true
---
body
""",
            "missing_scope_contract",
        ),
    ],
)
def test_rca_preflight_block_reasons_are_error_codes(kanban_home, body, expected):
    payload = validate_task_spec(TaskSpec("file", None, None, body, None, []))

    assert payload["ok"] is False
    assert expected in _codes(payload)


def test_unknown_skills_matches_preflight_reason(kanban_home):
    payload = validate_task_spec(
        TaskSpec("file", None, None, _valid_body(), "coder", ["missing-skill"])
    )

    assert "unknown_skills" in _codes(payload)


def test_known_skill_passes_skill_validation(kanban_home):
    payload = validate_task_spec(
        TaskSpec("file", None, None, _valid_body(), "coder", ["dogfood"])
    )

    assert "unknown_skills" not in _codes(payload)


def test_duplicate_scope_contract_is_specific_error(kanban_home):
    body = _valid_body() + "\n```yaml\nscope_contract:\n  version: 2\n```\n"
    payload = validate_task_spec(TaskSpec("file", None, None, body, None, []))

    assert "duplicate_scope_contract" in _codes(payload)


def test_invalid_frontmatter_is_specific_error(kanban_home):
    body = "---\nassignee: [broken\n---\nbody\n"
    payload = validate_task_spec(TaskSpec("file", None, None, body, None, []))

    assert "invalid_frontmatter" in _codes(payload)


def test_missing_report_contract_version_is_warning_only(kanban_home):
    body = _valid_body().replace("report_contract_version: 1\n", "")
    payload = validate_task_spec(TaskSpec("file", None, None, body, None, []))

    assert payload["ok"] is True
    assert payload["report_contract_version"] == 1
    assert {w["code"] for w in payload["warnings"]} == {"missing_report_contract_version"}


def test_file_mode_loads_assignee_from_frontmatter(kanban_home, tmp_path):
    task_file = tmp_path / "task.md"
    task_file.write_text(_valid_body(), encoding="utf-8")
    spec = load_task_spec(str(task_file))

    assert spec.source == "file"
    assert spec.assignee == "coder"


def test_db_task_id_mode_is_read_only(kanban_home):
    with kb.connect() as conn:
        tid = kb.create_task(conn, title="scoped", body=_valid_body(), assignee="coder")
        before = conn.execute("SELECT COUNT(*) FROM task_events").fetchone()[0]

    spec = load_task_spec(tid, task_id_mode=True)
    payload = validate_task_spec(spec)

    with kb.connect() as conn:
        after = conn.execute("SELECT COUNT(*) FROM task_events").fetchone()[0]
    assert payload["ok"] is True
    assert before == after


@pytest.mark.parametrize(
    "allowed",
    [
        ["all"],
        ["terminal"],
        ["secrets"],
        ["openclaw"],
    ],
)
def test_allowed_tools_edge_cases(kanban_home, allowed):
    body = _valid_body(allowed_tools=allowed)
    payload = validate_task_spec(TaskSpec("file", None, None, body, None, []))

    assert "invalid_allowed_tools" in _codes(payload)


@pytest.mark.parametrize("profile", ["coder", "reviewer", "default"])
def test_known_profiles_validate(kanban_home, profile):
    body = _valid_body(profile=profile)
    payload = validate_task_spec(TaskSpec("file", None, None, body, None, []))

    assert payload["ok"] is True


def test_valid_task_lints_under_200ms_after_imports(kanban_home):
    payload = validate_task_spec(TaskSpec("file", None, None, _valid_body(), None, []))

    assert payload["ok"] is True
    assert payload["diagnostics"]["elapsed_ms"] < 200
