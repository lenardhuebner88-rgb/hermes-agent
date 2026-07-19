from __future__ import annotations

import json
import subprocess
from pathlib import Path
from typing import Any

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from hermes_cli import agent_questions as aq
from hermes_cli import pa_actions
from hermes_cli import pa_chat
from hermes_cli import pa_planspec as pp


PLAN = """---
title: "PA Minimalplan"
status: draft
freigabe: operator
live_test_depth: smoke
taskgraph_hints:
  binding: true
  subtasks:
    - id: S1
      title: "PlanSpec-Flow implementieren"
      lane: coder
      deps: []
      acceptance_criteria:
        - "Draft und Validate liefern strukturierte Evidenz"
      scope_files:
        - "hermes_cli/pa_planspec.py"
---
# PA Minimalplan

Ziel ist ein validierter, operator-gehaltener Slice.
"""


@pytest.fixture
def isolated_planspec_home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    home = tmp_path / "home"
    hermes_home = home / ".hermes"
    hermes_home.mkdir(parents=True)
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("HERMES_HOME", str(hermes_home))
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: home))
    return hermes_home


def _validation_result(
    disposition: str = "clean", findings: list[str] | None = None
) -> subprocess.CompletedProcess[str]:
    ok = disposition in {"clean", "warn"}
    payload = {
        "ok": ok,
        "disposition": disposition,
        "path": "/profile/pa/planspecs/draft.md",
        "signed": False,
        "approved_by": "",
        "freigabe": "operator",
        "board": "health-track",
        "findings": findings or [],
        "would_block": not ok,
    }
    return subprocess.CompletedProcess(
        args=["hermes", "plan", "validate"],
        returncode=0 if ok else 2,
        stdout=json.dumps(payload),
        stderr="",
    )


def _register() -> TestClient:
    app = FastAPI()
    pp.register_pa_planspec_routes(app)
    return TestClient(app)


@pytest.mark.parametrize("raw", [PLAN, f"```markdown\n{PLAN}```"])
def test_extract_planspec_accepts_whole_document_and_fence(raw: str) -> None:
    text, frontmatter = pp.extract_planspec_text(raw)

    assert text == PLAN
    assert frontmatter["freigabe"] == "operator"
    assert pp.extract_slices(frontmatter) == [
        {
            "id": "S1",
            "title": "PlanSpec-Flow implementieren",
            "lane": "coder",
            "deps": [],
        }
    ]


def test_draft_without_yaml_is_clean_422_with_bounded_engine_text(
    isolated_planspec_home: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    raw = "Ich kann dazu leider nur Prosa liefern."
    monkeypatch.setattr(pa_chat, "run_engine", lambda *args, **kwargs: raw)

    with _register() as client:
        response = client.post("/api/pa/planspec/draft", json={"idea": "Baue etwas"})

    assert response.status_code == 422
    assert response.json()["detail"] == {
        "error": "Engine-Ausgabe enthält keine PlanSpec-YAML-Frontmatter",
        "engine_output": raw,
    }
    assert list(pp.planspecs_dir().glob("*.md")) == []


@pytest.mark.parametrize(
    ("disposition", "returncode", "expected"),
    [
        ("clean", 0, "CLEAN"),
        ("warn", 0, "WARN"),
        ("block", 2, "BLOCK"),
        ("invalid", 2, "BLOCK"),
    ],
)
def test_parse_validation_output_normalizes_clean_warn_block(
    disposition: str,
    returncode: int,
    expected: str,
) -> None:
    payload = {"disposition": disposition, "findings": ["one finding"]}

    result = pp.parse_validation_output(
        returncode=returncode,
        stdout=json.dumps(payload),
        stderr="",
    )

    assert result == {
        "status": expected,
        "findings": ["one finding"],
        "exit": returncode,
    }


def test_draft_response_and_propose_card_include_slices_gates_and_validate(
    isolated_planspec_home: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(pa_chat, "run_engine", lambda *args, **kwargs: f"```md\n{PLAN}```")
    monkeypatch.setattr(pp.subprocess, "run", lambda *args, **kwargs: _validation_result())

    with _register() as client:
        draft_response = client.post(
            "/api/pa/planspec/draft",
            json={"idea": "Baue den PlanSpec-Flow", "project": "hermes"},
        )
        assert draft_response.status_code == 200
        draft = draft_response.json()
        propose = client.post(
            "/api/pa/planspec/propose", json={"draft_id": draft["draft_id"]}
        )

    assert set(draft) == {"draft_id", "planspec_text", "validation", "slices"}
    assert pp.DRAFT_ID_RE.fullmatch(draft["draft_id"])
    assert draft["planspec_text"] == PLAN
    assert draft["validation"] == {"status": "CLEAN", "findings": []}
    assert draft["slices"][0]["id"] == "S1"
    assert propose.status_code == 200
    assert set(propose.json()) == {"question_id"}

    events = aq.list_question_events(status="open")
    assert len(events) == 1
    event = events[0]
    assert event["id"] == propose.json()["question_id"]
    assert event["action_payload"]["category"] == "planspec.ingest"
    assert event["action_payload"]["payload"] == {"draft_id": draft["draft_id"]}
    assert "Validate: CLEAN (0 Findings)" in event["question_text"]
    assert "freigabe=operator · live_test_depth=smoke" in event["question_text"]
    assert "`S1` [coder] PlanSpec-Flow implementieren · deps: —" in event["question_text"]


def test_blocked_draft_cannot_be_proposed(
    isolated_planspec_home: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(pa_chat, "run_engine", lambda *args, **kwargs: PLAN)
    monkeypatch.setattr(
        pp.subprocess,
        "run",
        lambda *args, **kwargs: _validation_result(
            "block", ["placeholder residue in S1: ['TODO']"]
        ),
    )

    with _register() as client:
        draft = client.post(
            "/api/pa/planspec/draft", json={"idea": "Draft mit Finding"}
        ).json()
        response = client.post(
            "/api/pa/planspec/propose", json={"draft_id": draft["draft_id"]}
        )

    assert draft["validation"]["status"] == "BLOCK"
    assert response.status_code == 400
    assert "BLOCK" in response.json()["detail"]
    assert aq.list_question_events(status="open") == []


def _create_draft(
    monkeypatch: pytest.MonkeyPatch,
    *,
    initial_disposition: str = "clean",
) -> str:
    monkeypatch.setattr(pa_chat, "run_engine", lambda *args, **kwargs: PLAN)
    monkeypatch.setattr(
        pp.subprocess,
        "run",
        lambda *args, **kwargs: _validation_result(
            initial_disposition,
            ["operator-visible warning"] if initial_disposition == "warn" else [],
        ),
    )
    return pp.draft_planspec(pp.DraftIn(idea="Executor prüfen"))["draft_id"]


def test_executor_recheck_block_prevents_ingest_call(
    isolated_planspec_home: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    draft_id = _create_draft(monkeypatch)
    calls: list[list[str]] = []

    def blocked(argv: list[str], **kwargs: Any) -> subprocess.CompletedProcess[str]:
        calls.append(argv)
        return _validation_result("block", ["draft changed into a blocker"])

    monkeypatch.setattr(pp.subprocess, "run", blocked)

    result = pa_actions.execute_action("planspec.ingest", {"draft_id": draft_id})

    assert result["ok"] is False
    assert result["validation"] == {
        "status": "BLOCK",
        "findings": ["draft changed into a blocker"],
    }
    assert "Re-Check" in result["error"]
    assert len(calls) == 1
    assert calls[0][1:3] == ["plan", "validate"]


def test_executor_ingest_success_returns_chain_and_task_evidence(
    isolated_planspec_home: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    draft_id = _create_draft(monkeypatch)
    calls: list[tuple[list[str], dict[str, Any]]] = []

    def successful(argv: list[str], **kwargs: Any) -> subprocess.CompletedProcess[str]:
        calls.append((argv, kwargs))
        if argv[2] == "validate":
            return _validation_result()
        assert argv[2] == "ingest"
        return subprocess.CompletedProcess(
            args=argv,
            returncode=0,
            stdout=json.dumps(
                {
                    "path": str(pp.resolve_draft(draft_id)),
                    "root_task_id": "t_a1b2c3d4",
                    "child_ids": ["t_11111111", "t_22222222"],
                }
            ),
            stderr="",
        )

    monkeypatch.setattr(pp.subprocess, "run", successful)

    result = pa_actions.execute_action("planspec.ingest", {"draft_id": draft_id})

    assert result["ok"] is True
    assert result["exit"] == 0
    assert result["chain_id"] == "t_a1b2c3d4"
    assert result["task_ids"] == ["t_11111111", "t_22222222"]
    assert '"root_task_id": "t_a1b2c3d4"' in result["stdout_tail"]
    assert [argv[2] for argv, _kwargs in calls] == ["validate", "ingest"]
    assert all(kwargs["shell"] is False for _argv, kwargs in calls)
    assert all(kwargs["env"]["HERMES_PA_PLANS_ROOT"] == str(pp.planspecs_dir()) for _argv, kwargs in calls)


def test_warn_requires_warning_to_have_been_visible_on_approval_card(
    isolated_planspec_home: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    draft_id = _create_draft(monkeypatch, initial_disposition="clean")
    calls: list[list[str]] = []

    def newly_warned(argv: list[str], **kwargs: Any) -> subprocess.CompletedProcess[str]:
        calls.append(argv)
        return _validation_result("warn", ["new warning"])

    monkeypatch.setattr(pp.subprocess, "run", newly_warned)

    result = pa_actions.execute_action("planspec.ingest", {"draft_id": draft_id})

    assert result["ok"] is False
    assert "nicht sichtbar" in result["error"]
    assert len(calls) == 1


def test_warn_visible_on_card_can_ingest_when_recheck_is_still_warn(
    isolated_planspec_home: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    draft_id = _create_draft(monkeypatch, initial_disposition="warn")
    calls: list[list[str]] = []

    def still_warned(argv: list[str], **kwargs: Any) -> subprocess.CompletedProcess[str]:
        calls.append(argv)
        if argv[2] == "validate":
            return _validation_result("warn", ["operator-visible warning"])
        return subprocess.CompletedProcess(
            args=argv,
            returncode=0,
            stdout=json.dumps(
                {
                    "root_task_id": "t_feedbeef",
                    "child_ids": ["t_cafebabe"],
                }
            ),
            stderr="",
        )

    monkeypatch.setattr(pp.subprocess, "run", still_warned)

    result = pa_actions.execute_action("planspec.ingest", {"draft_id": draft_id})

    assert result["ok"] is True
    assert result["validation"]["status"] == "WARN"
    assert [argv[2] for argv in calls] == ["validate", "ingest"]


def test_changed_warn_findings_block_even_when_card_already_showed_warn(
    isolated_planspec_home: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    draft_id = _create_draft(monkeypatch, initial_disposition="warn")
    calls: list[list[str]] = []

    def changed_warning(argv: list[str], **kwargs: Any) -> subprocess.CompletedProcess[str]:
        calls.append(argv)
        return _validation_result("warn", ["different warning"])

    monkeypatch.setattr(pp.subprocess, "run", changed_warning)

    result = pa_actions.execute_action("planspec.ingest", {"draft_id": draft_id})

    assert result["ok"] is False
    assert "Geänderte WARN-Findings" in result["error"]
    assert len(calls) == 1


def test_process_evidence_is_a_real_bounded_tail() -> None:
    assert pp._bounded_tail("abcdef", 4) == "…def"


def test_planspec_action_payload_schema_is_closed() -> None:
    assert aq.normalize_pa_action_payload(
        "planspec.ingest", {"draft_id": "draft_" + "a" * 24, "reason": "ship"}
    ) == {"draft_id": "draft_" + "a" * 24, "reason": "ship"}
    with pytest.raises(ValueError, match="unbekannte Felder"):
        aq.normalize_pa_action_payload(
            "planspec.ingest",
            {"draft_id": "draft_" + "a" * 24, "force": "true"},
        )
