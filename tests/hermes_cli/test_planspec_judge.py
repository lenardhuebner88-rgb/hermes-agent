"""Subjective Sonnet quality judge (run_spec_quality_judge) in the ingest path.

The judge is layered ON TOP of the deterministic S1 rubric: it runs
synchronously inside ``ingest_planspec`` *after* ``validate_spec_rubric``
passes and *before* any DB write. It REUSES the in-repo auxiliary-client
call path (``agent.auxiliary_client.get_text_auxiliary_client``) — no new
HTTP client / SDK. A fail verdict raises ``PlanSpecBlocked`` with the
judge's reasons (gate + teacher); any LLM infra/network/auth/timeout error
logs a WARNING and falls back to deterministic-only ingest (never a hard
fail). The model call is mocked here — no network, no real provider.
"""

from __future__ import annotations

import logging
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from hermes_cli import kanban_db as kb
from hermes_cli import planspecs

# These tests exercise the judge directly with a mocked aux client — opt in to
# it (the conftest autouse fixture disables the judge for unmarked tests).
pytestmark = pytest.mark.spec_judge


@pytest.fixture
def kanban_home(tmp_path, monkeypatch, all_assignees_spawnable):
    home = tmp_path / ".hermes"
    home.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(home))
    monkeypatch.delenv("HERMES_KANBAN_BOARD", raising=False)
    monkeypatch.delenv("HERMES_PLANSPEC_JUDGE", raising=False)
    monkeypatch.delenv("HERMES_PLANSPEC_JUDGE_MODEL", raising=False)
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    kb.init_db()
    return home


def _write(plans_root: Path, body: str, name: str = "2026-06-18-judge.md") -> Path:
    path = plans_root / "Hermes" / "plans" / name
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(body, encoding="utf-8")
    return path


def _task_count() -> int:
    with kb.connect_closing() as conn:
        return conn.execute("SELECT COUNT(*) AS n FROM tasks").fetchone()["n"]


# A clean, deterministically-passing PlanSpec (mirrors the S1 rubric fixture).
CLEAN = """---
status: freigegeben-komplett
owner: Hermes
slice: J1
topic: "Judge clean"
freigabe: complete
live_test_depth: smoke
taskgraph_hints:
  binding: true
  subtasks:
    - id: J1-S1
      title: "Short verbatim task title"
      lane: coder
      deps: []
      acceptance_criteria:
        - "Verbatim AC statement that must hold for this subtask"
      body: "Optional verbatim worker body"
    - id: J1-S2
      title: "Final verdict on the slice"
      lane: reviewer
      deps: [J1-S1]
      acceptance_criteria:
        - "Review verdict recorded with evidence"
---
# J1
"""


# A deterministically-broken PlanSpec (AC-less subtask) — the S1 rubric must
# block it before the judge is ever consulted.
BROKEN_DETERMINISTIC = """---
status: freigegeben-komplett
owner: Hermes
slice: J2
topic: "Judge broken-det"
freigabe: complete
live_test_depth: smoke
taskgraph_hints:
  binding: true
  subtasks:
    - id: J2-S1
      title: "Subtask with no acceptance criterion at all"
      lane: coder
      deps: []
---
# J2
"""


def _fake_aux_response(content: str, *, usage: bool = True):
    """Minimal object shaped like an OpenAI chat.completions result.

    The judge reads ``resp.choices[0].message.content`` and (best-effort)
    ``resp.usage`` — built with MagicMock to avoid importing the SDK.
    """
    resp = MagicMock()
    resp.choices = [MagicMock()]
    resp.choices[0].message.content = content
    if usage:
        resp.usage.prompt_tokens = 1234
        resp.usage.completion_tokens = 56
        resp.usage.total_tokens = 1290
    else:
        resp.usage = None
    return resp


def _mock_client_returning(content: str, *, usage: bool = True):
    client = MagicMock()
    client.chat.completions.create = MagicMock(
        return_value=_fake_aux_response(content, usage=usage)
    )
    return client


def _patch_judge_client(client, *, model: str = "claude-sonnet-4-6"):
    """Patch the aux client factory at its source. The judge imports it
    lazily inside the function body (mirrors kanban_specify), so patching
    the source module attribute is sufficient."""
    return patch(
        "agent.auxiliary_client.get_text_auxiliary_client",
        return_value=(client, model),
    )


PASS_JSON = '{"verdict": "pass", "reasons": []}'
FAIL_JSON = (
    '{"verdict": "fail", "reasons": '
    '["AC J1-S1 is vague — \\"works well\\" is not observable", '
    '"done is not sharp: no measurable completion condition"]}'
)


# ---------------------------------------------------------------------------
# Verdict parsing
# ---------------------------------------------------------------------------

def test_parse_verdict_pass():
    resp = _fake_aux_response(PASS_JSON)
    verdict = planspecs._parse_spec_judge_verdict(resp)
    assert verdict is not None
    assert verdict.passed is True


def test_parse_verdict_fail_carries_reasons():
    resp = _fake_aux_response(FAIL_JSON)
    verdict = planspecs._parse_spec_judge_verdict(resp)
    assert verdict is not None
    assert verdict.passed is False
    assert len(verdict.reasons) == 2
    assert any("not observable" in r for r in verdict.reasons)


def test_parse_verdict_handles_fenced_json():
    resp = _fake_aux_response('```json\n{"verdict": "pass"}\n```')
    verdict = planspecs._parse_spec_judge_verdict(resp)
    assert verdict is not None and verdict.passed is True


def test_parse_verdict_accepts_boolean_passed_field():
    resp = _fake_aux_response('{"passed": false, "reasons": ["bad"]}')
    verdict = planspecs._parse_spec_judge_verdict(resp)
    assert verdict is not None and verdict.passed is False


def test_parse_verdict_unparseable_returns_none():
    resp = _fake_aux_response("I think this looks fine to me, ship it!")
    assert planspecs._parse_spec_judge_verdict(resp) is None


def test_parse_verdict_empty_choices_returns_none():
    resp = MagicMock()
    resp.choices = []
    assert planspecs._parse_spec_judge_verdict(resp) is None


def test_parse_verdict_ignores_trailing_prose_with_braces():
    """Regression: trailing prose (especially with braces) must not corrupt the
    JSON extraction. The parser should recover the first complete JSON object."""
    resp = _fake_aux_response(
        '{"verdict": "fail", "reasons": ["bad"]} Here is extra context: {foo: bar}.'
    )
    verdict = planspecs._parse_spec_judge_verdict(resp)
    assert verdict is not None
    assert verdict.passed is False
    assert verdict.reasons == ["bad"]


def test_parse_verdict_handles_braces_inside_reason_string():
    """Braces inside a JSON string value must not confuse object-boundary detection."""
    resp = _fake_aux_response(
        '{"verdict": "fail", "reasons": ["Use {metric} instead"]}'
    )
    verdict = planspecs._parse_spec_judge_verdict(resp)
    assert verdict is not None
    assert verdict.passed is False
    assert "Use {metric} instead" in verdict.reasons


# ---------------------------------------------------------------------------
# run_spec_quality_judge — unit (no DB)
# ---------------------------------------------------------------------------

def test_judge_pass_returns_none(tmp_path, monkeypatch):
    monkeypatch.delenv("HERMES_PLANSPEC_JUDGE", raising=False)
    monkeypatch.delenv("HERMES_PLANSPEC_JUDGE_MODEL", raising=False)
    plans_root = tmp_path / "03-Agents"
    path = _write(plans_root, CLEAN)
    spec = planspecs.parse_binding_planspec(path, plans_root=plans_root)
    client = _mock_client_returning(PASS_JSON)
    with _patch_judge_client(client):
        assert planspecs.run_spec_quality_judge(spec) is None
    assert client.chat.completions.create.call_count == 1


def test_judge_fail_raises_planspecblocked_with_reasons(tmp_path, monkeypatch):
    monkeypatch.delenv("HERMES_PLANSPEC_JUDGE", raising=False)
    plans_root = tmp_path / "03-Agents"
    path = _write(plans_root, CLEAN)
    spec = planspecs.parse_binding_planspec(path, plans_root=plans_root)
    client = _mock_client_returning(FAIL_JSON)
    with _patch_judge_client(client):
        with pytest.raises(planspecs.PlanSpecBlocked) as excinfo:
            planspecs.run_spec_quality_judge(spec)
    findings = excinfo.value.findings
    assert any("quality judge" in f for f in findings)
    assert any("not observable" in f for f in findings)


def test_judge_requests_sonnet_model_via_aux_path(tmp_path, monkeypatch):
    """The judge must request claude-sonnet-4-6 through the reused aux client."""
    monkeypatch.delenv("HERMES_PLANSPEC_JUDGE_MODEL", raising=False)
    plans_root = tmp_path / "03-Agents"
    path = _write(plans_root, CLEAN)
    spec = planspecs.parse_binding_planspec(path, plans_root=plans_root)
    client = _mock_client_returning(PASS_JSON)
    # aux factory returns a non-Sonnet default model; the judge must still
    # request Sonnet (the documented quality tier), not the generic default.
    with _patch_judge_client(client, model="some-generic-aux-model"):
        planspecs.run_spec_quality_judge(spec)
    _, kwargs = client.chat.completions.create.call_args
    assert kwargs["model"] == planspecs.SPEC_JUDGE_MODEL == "claude-sonnet-4-6"


def test_judge_model_env_override(tmp_path, monkeypatch):
    monkeypatch.setenv("HERMES_PLANSPEC_JUDGE_MODEL", "claude-sonnet-4-6-custom")
    plans_root = tmp_path / "03-Agents"
    path = _write(plans_root, CLEAN)
    spec = planspecs.parse_binding_planspec(path, plans_root=plans_root)
    client = _mock_client_returning(PASS_JSON)
    with _patch_judge_client(client):
        planspecs.run_spec_quality_judge(spec)
    _, kwargs = client.chat.completions.create.call_args
    assert kwargs["model"] == "claude-sonnet-4-6-custom"


def test_judge_disabled_via_env_skips_call(tmp_path, monkeypatch):
    monkeypatch.setenv("HERMES_PLANSPEC_JUDGE", "0")
    plans_root = tmp_path / "03-Agents"
    path = _write(plans_root, CLEAN)
    spec = planspecs.parse_binding_planspec(path, plans_root=plans_root)
    client = _mock_client_returning(FAIL_JSON)  # would block if consulted
    with _patch_judge_client(client):
        assert planspecs.run_spec_quality_judge(spec) is None
    assert client.chat.completions.create.call_count == 0


# ---------------------------------------------------------------------------
# Graceful fallback (infra / net / auth / timeout)
# ---------------------------------------------------------------------------

def test_judge_infra_error_falls_back_with_warning(tmp_path, monkeypatch, caplog):
    monkeypatch.delenv("HERMES_PLANSPEC_JUDGE", raising=False)
    plans_root = tmp_path / "03-Agents"
    path = _write(plans_root, CLEAN)
    spec = planspecs.parse_binding_planspec(path, plans_root=plans_root)
    client = MagicMock()
    client.chat.completions.create = MagicMock(
        side_effect=RuntimeError("503 upstream timeout")
    )
    with _patch_judge_client(client):
        with caplog.at_level(logging.WARNING, logger="hermes_cli.planspecs"):
            # Must NOT raise — infra trouble degrades to deterministic-only.
            assert planspecs.run_spec_quality_judge(spec) is None
    assert any("judge" in r.message.lower() for r in caplog.records)
    assert any("fall" in r.message.lower() for r in caplog.records)


def test_judge_no_client_configured_falls_back(tmp_path, monkeypatch):
    monkeypatch.delenv("HERMES_PLANSPEC_JUDGE", raising=False)
    plans_root = tmp_path / "03-Agents"
    path = _write(plans_root, CLEAN)
    spec = planspecs.parse_binding_planspec(path, plans_root=plans_root)
    with patch(
        "agent.auxiliary_client.get_text_auxiliary_client",
        return_value=(None, None),
    ):
        assert planspecs.run_spec_quality_judge(spec) is None


def test_judge_unparseable_verdict_falls_back_with_warning(
    tmp_path, monkeypatch, caplog
):
    monkeypatch.delenv("HERMES_PLANSPEC_JUDGE", raising=False)
    plans_root = tmp_path / "03-Agents"
    path = _write(plans_root, CLEAN)
    spec = planspecs.parse_binding_planspec(path, plans_root=plans_root)
    client = _mock_client_returning("no json here, just prose")
    with _patch_judge_client(client):
        with caplog.at_level(logging.WARNING, logger="hermes_cli.planspecs"):
            assert planspecs.run_spec_quality_judge(spec) is None
    assert any("judge" in r.message.lower() for r in caplog.records)


def test_judge_import_failure_falls_back(tmp_path, monkeypatch):
    monkeypatch.delenv("HERMES_PLANSPEC_JUDGE", raising=False)
    plans_root = tmp_path / "03-Agents"
    path = _write(plans_root, CLEAN)
    spec = planspecs.parse_binding_planspec(path, plans_root=plans_root)
    with patch(
        "agent.auxiliary_client.get_text_auxiliary_client",
        side_effect=ImportError("auxiliary client unavailable"),
    ):
        assert planspecs.run_spec_quality_judge(spec) is None


# ---------------------------------------------------------------------------
# Cost / usage observability
# ---------------------------------------------------------------------------

def test_judge_logs_token_usage(tmp_path, monkeypatch, caplog):
    monkeypatch.delenv("HERMES_PLANSPEC_JUDGE", raising=False)
    plans_root = tmp_path / "03-Agents"
    path = _write(plans_root, CLEAN)
    spec = planspecs.parse_binding_planspec(path, plans_root=plans_root)
    client = _mock_client_returning(PASS_JSON, usage=True)
    with _patch_judge_client(client):
        with caplog.at_level(logging.INFO, logger="hermes_cli.planspecs"):
            planspecs.run_spec_quality_judge(spec)
    assert any("token usage" in r.message.lower() for r in caplog.records)


def test_judge_missing_usage_does_not_crash(tmp_path, monkeypatch):
    monkeypatch.delenv("HERMES_PLANSPEC_JUDGE", raising=False)
    plans_root = tmp_path / "03-Agents"
    path = _write(plans_root, CLEAN)
    spec = planspecs.parse_binding_planspec(path, plans_root=plans_root)
    client = _mock_client_returning(PASS_JSON, usage=False)
    with _patch_judge_client(client):
        assert planspecs.run_spec_quality_judge(spec) is None


# ---------------------------------------------------------------------------
# Integration with ingest_planspec
# ---------------------------------------------------------------------------

def test_ingest_pass_verdict_creates_chain(kanban_home, tmp_path):
    plans_root = tmp_path / "03-Agents"
    path = _write(plans_root, CLEAN)
    client = _mock_client_returning(PASS_JSON)
    with _patch_judge_client(client):
        result = planspecs.ingest_planspec(path, plans_root=plans_root)
    assert result["ok"] is True
    assert len(result["child_ids"]) == 2
    assert client.chat.completions.create.call_count == 1


def test_ingest_fail_verdict_blocks_and_leaves_board_untouched(kanban_home, tmp_path):
    plans_root = tmp_path / "03-Agents"
    path = _write(plans_root, CLEAN)
    before = _task_count()
    client = _mock_client_returning(FAIL_JSON)
    with _patch_judge_client(client):
        with pytest.raises(planspecs.PlanSpecBlocked) as excinfo:
            planspecs.ingest_planspec(path, plans_root=plans_root)
    assert any("quality judge" in f for f in excinfo.value.findings)
    assert _task_count() == before  # no DB write past the gate


def test_ingest_infra_error_still_ingests(kanban_home, tmp_path, caplog):
    plans_root = tmp_path / "03-Agents"
    path = _write(plans_root, CLEAN)
    client = MagicMock()
    client.chat.completions.create = MagicMock(
        side_effect=RuntimeError("auth: 401 invalid api key")
    )
    with _patch_judge_client(client):
        with caplog.at_level(logging.WARNING, logger="hermes_cli.planspecs"):
            result = planspecs.ingest_planspec(path, plans_root=plans_root)
    assert result["ok"] is True
    assert len(result["child_ids"]) == 2
    assert any("judge" in r.message.lower() for r in caplog.records)


def test_ingest_deterministic_block_preempts_judge(kanban_home, tmp_path):
    """The judge must not be consulted when the deterministic rubric fails —
    the cheap gate fires first and the expensive LLM call never happens."""
    plans_root = tmp_path / "03-Agents"
    path = _write(plans_root, BROKEN_DETERMINISTIC)
    client = _mock_client_returning(PASS_JSON)
    with _patch_judge_client(client):
        with pytest.raises(planspecs.PlanSpecBlocked) as excinfo:
            planspecs.ingest_planspec(path, plans_root=plans_root)
    assert any("AC-less subtask" in f for f in excinfo.value.findings)
    assert client.chat.completions.create.call_count == 0


def test_ingest_force_bypasses_judge(kanban_home, tmp_path):
    plans_root = tmp_path / "03-Agents"
    path = _write(plans_root, CLEAN)
    client = _mock_client_returning(FAIL_JSON)  # would block if consulted
    with _patch_judge_client(client):
        result = planspecs.ingest_planspec(path, plans_root=plans_root, force=True)
    assert result["ok"] is True
    assert client.chat.completions.create.call_count == 0


# ---------------------------------------------------------------------------
# No new network client (PFLICHT-WIEDERVERWENDUNG)
# ---------------------------------------------------------------------------

def test_no_new_network_client_imported():
    """planspecs.py must reuse the aux call path, not introduce a new HTTP
    client / SDK. Scan the module source for forbidden network constructs."""
    src = Path(planspecs.__file__).read_text(encoding="utf-8")
    forbidden = [
        "import requests",
        "import httpx",
        "import openai",
        "from openai",
        "import anthropic",
        "from anthropic",
        "OpenAI(",
        "AsyncOpenAI(",
        "httpx.Client",
        "httpx.AsyncClient",
        "requests.get",
        "requests.post",
    ]
    hits = [tok for tok in forbidden if tok in src]
    assert hits == [], f"planspecs.py introduced a network client: {hits}"
    # Positive proof the reused path is referenced.
    assert "get_text_auxiliary_client" in src
