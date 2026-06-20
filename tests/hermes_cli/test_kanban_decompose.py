"""Tests for the decomposer module + `hermes kanban decompose` CLI surface.

The auxiliary LLM client is mocked — no network calls. Tests exercise the
prompt plumbing, response parsing, DB writes (via the real DB helper),
and the assignee-fallback logic.
"""

from __future__ import annotations

import json as jsonlib
import sqlite3
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from hermes_cli import kanban_db as kb
from hermes_cli import kanban_decompose as decomp


@pytest.fixture
def kanban_home(tmp_path, monkeypatch):
    home = tmp_path / ".hermes"
    home.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(home))
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    kb.init_db()
    return home


def _fake_aux_response(content: str):
    resp = MagicMock()
    resp.choices = [MagicMock()]
    resp.choices[0].message.content = content
    return resp


def _mock_client_returning(content: str):
    client = MagicMock()
    client.chat.completions.create = MagicMock(return_value=_fake_aux_response(content))
    return client


def _patch_aux_client(content: str, *, model: str = "test-model"):
    client = _mock_client_returning(content)
    return patch(
        "agent.auxiliary_client.get_text_auxiliary_client",
        return_value=(client, model),
    )


def _patch_extra_body():
    return patch(
        "agent.auxiliary_client.get_auxiliary_extra_body",
        return_value={},
    )


def _patch_list_profiles(names: list[str]):
    """Pretend the named profiles exist. The decomposer uses
    profiles_mod.list_profiles() to build the roster + valid-set, and
    profiles_mod.profile_exists() to resolve orchestrator/default."""
    from types import SimpleNamespace
    fake_profiles = [
        SimpleNamespace(
            name=n, is_default=(i == 0), description=f"desc for {n}",
            description_auto=False, model="m", provider="p", skill_count=1,
        )
        for i, n in enumerate(names)
    ]
    return [
        patch("hermes_cli.profiles.list_profiles", return_value=fake_profiles),
        patch("hermes_cli.profiles.profile_exists", side_effect=lambda x: x in names),
        patch("hermes_cli.profiles.get_active_profile_name", return_value=names[0] if names else "default"),
    ]


def test_empty_history_keeps_roster_byte_identical(kanban_home):
    patches = _patch_list_profiles(["coder", "researcher"])
    for p in patches:
        p.start()
    try:
        roster, valid = decomp._build_roster()
        with kb.connect() as conn:
            decomp._enrich_roster_with_outcome_stats(conn, roster)
    finally:
        for p in patches:
            p.stop()

    assert valid == {"coder", "researcher"}
    assert decomp._format_roster(roster) == (
        "  - coder: desc for coder\n"
        "  - researcher: desc for researcher"
    )


def test_profile_stats_db_error_keeps_roster_byte_identical(kanban_home):
    patches = _patch_list_profiles(["coder", "researcher"])
    for p in patches:
        p.start()
    try:
        roster, _valid = decomp._build_roster()
        conn = sqlite3.connect(":memory:")
        try:
            assert kb.profile_outcome_stats(conn) == {}
            decomp._enrich_roster_with_outcome_stats(conn, roster)
        finally:
            conn.close()
    finally:
        for p in patches:
            p.stop()

    assert decomp._format_roster(roster) == (
        "  - coder: desc for coder\n"
        "  - researcher: desc for researcher"
    )


def test_roster_with_history_adds_stats_only_for_seen_profile(kanban_home):
    with kb.connect() as conn:
        conn.execute(
            "INSERT INTO task_runs (task_id, profile, status, started_at, ended_at, "
            "outcome, input_tokens, output_tokens, verdict) "
            "VALUES ('t_seen', 'coder', 'done', 1700000000, 1700000290, "
            "'completed', 40000, 1000, 'APPROVED')"
        )
        conn.commit()

    patches = _patch_list_profiles(["coder", "researcher"])
    for p in patches:
        p.start()
    try:
        roster, _valid = decomp._build_roster()
        with kb.connect() as conn:
            decomp._enrich_roster_with_outcome_stats(conn, roster)
    finally:
        for p in patches:
            p.stop()

    rendered = decomp._format_roster(roster)
    assert rendered == (
        "  - coder: desc for coder\n"
        "    stats: done 100% · blocked 0% · timeout 0% · Ø 41k tok · Ø 290s\n"
        "  - researcher: desc for researcher"
    )


def test_profile_stats_suppress_approved_until_min_verdicts():
    rendered = decomp._format_profile_outcome_stats({
        "done_pct": 100.0,
        "blocked_pct": 0.0,
        "timeout_pct": 0.0,
        "avg_tokens": 41000,
        "avg_runtime_s": 290,
        "approved_pct": 100.0,
        "verdict_n": 4,
    })

    assert rendered == "done 100% · blocked 0% · timeout 0% · Ø 41k tok · Ø 290s"


def test_profile_stats_show_approved_with_sample_size_after_min_verdicts():
    rendered = decomp._format_profile_outcome_stats({
        "done_pct": 80.0,
        "blocked_pct": 10.0,
        "timeout_pct": 10.0,
        "approved_pct": 92.0,
        "verdict_n": 12,
    })

    assert rendered == "done 80% · blocked 10% · timeout 10% · approved 92% (n=12)"


def test_decompose_with_fanout_creates_children(kanban_home):
    with kb.connect() as conn:
        tid = kb.create_task(conn, title="ship a feature", triage=True)

    llm_payload = jsonlib.dumps({
        "fanout": True,
        "rationale": "test split",
        "tasks": [
            {"title": "research", "body": "look it up", "assignee": "researcher", "parents": []},
            {"title": "build", "body": "code it", "assignee": "engineer", "parents": [0]},
        ],
    })

    patches = _patch_list_profiles(["orchestrator", "researcher", "engineer"])
    for p in patches:
        p.start()
    try:
        with _patch_aux_client(llm_payload), _patch_extra_body():
            outcome = decomp.decompose_task(tid, author="me")
    finally:
        for p in patches:
            p.stop()

    assert outcome.ok, outcome.reason
    assert outcome.fanout is True
    assert outcome.child_ids and len(outcome.child_ids) == 2

    with kb.connect() as conn:
        root = kb.get_task(conn, tid)
        c0 = kb.get_task(conn, outcome.child_ids[0])
        c1 = kb.get_task(conn, outcome.child_ids[1])
    assert root.status == "todo"
    assert c0.status == "ready"
    assert c1.status == "todo"
    assert c0.assignee == "researcher"
    assert c1.assignee == "engineer"


def test_decompose_fanout_false_assigns_default_when_unassigned(kanban_home):
    with kb.connect() as conn:
        tid = kb.create_task(conn, title="just one thing", triage=True)

    llm_payload = jsonlib.dumps({
        "fanout": False,
        "rationale": "single unit",
        "title": "Tightened title",
        "body": "**Goal**\nDo the thing.",
    })

    patches = _patch_list_profiles(["orchestrator", "fallback"])
    for p in patches:
        p.start()
    try:
        with _patch_aux_client(llm_payload), _patch_extra_body(), patch(
            "hermes_cli.kanban_decompose._load_config",
            return_value={"kanban": {"default_assignee": "fallback"}},
        ):
            outcome = decomp.decompose_task(tid, author="me")
    finally:
        for p in patches:
            p.stop()

    assert outcome.ok, outcome.reason
    assert outcome.fanout is False
    assert outcome.new_title == "Tightened title"
    with kb.connect() as conn:
        task = kb.get_task(conn, tid)
    assert task is not None
    # specify path with no parents -> recompute_ready flips to 'ready'
    assert task.status == "ready"
    assert task.title == "Tightened title"
    assert task.assignee == "fallback"


def test_decompose_fanout_false_preserves_existing_assignee(kanban_home):
    with kb.connect() as conn:
        tid = kb.create_task(
            conn,
            title="already routed",
            assignee="engineer",
            triage=True,
        )

    llm_payload = jsonlib.dumps({
        "fanout": False,
        "rationale": "single unit",
        "title": "Tightened title",
        "body": "Keep existing lane.",
        "assignee": "fallback",
    })

    patches = _patch_list_profiles(["orchestrator", "engineer", "fallback"])
    for p in patches:
        p.start()
    try:
        with _patch_aux_client(llm_payload), _patch_extra_body(), patch(
            "hermes_cli.kanban_decompose._load_config",
            return_value={"kanban": {"default_assignee": "fallback"}},
        ):
            outcome = decomp.decompose_task(tid, author="me")
    finally:
        for p in patches:
            p.stop()

    assert outcome.ok, outcome.reason
    with kb.connect() as conn:
        task = kb.get_task(conn, tid)
    assert task is not None
    assert task.assignee == "engineer"
    assert task.title == "Tightened title"


def test_decompose_fanout_false_uses_valid_llm_assignee(kanban_home):
    with kb.connect() as conn:
        tid = kb.create_task(conn, title="route me", triage=True)

    llm_payload = jsonlib.dumps({
        "fanout": False,
        "rationale": "single unit",
        "title": "Tightened title",
        "body": "Route to specialist.",
        "assignee": "engineer",
    })

    patches = _patch_list_profiles(["orchestrator", "engineer", "fallback"])
    for p in patches:
        p.start()
    try:
        with _patch_aux_client(llm_payload), _patch_extra_body(), patch(
            "hermes_cli.kanban_decompose._load_config",
            return_value={"kanban": {"default_assignee": "fallback"}},
        ):
            outcome = decomp.decompose_task(tid, author="me")
    finally:
        for p in patches:
            p.stop()

    assert outcome.ok, outcome.reason
    with kb.connect() as conn:
        task = kb.get_task(conn, tid)
    assert task is not None
    assert task.assignee == "engineer"


def test_decompose_fanout_false_invalid_llm_assignee_uses_default(kanban_home):
    with kb.connect() as conn:
        tid = kb.create_task(conn, title="route me safely", triage=True)

    llm_payload = jsonlib.dumps({
        "fanout": False,
        "rationale": "single unit",
        "title": "Tightened title",
        "body": "Route to fallback.",
        "assignee": "made_up",
    })

    patches = _patch_list_profiles(["orchestrator", "fallback"])
    for p in patches:
        p.start()
    try:
        with _patch_aux_client(llm_payload), _patch_extra_body(), patch(
            "hermes_cli.kanban_decompose._load_config",
            return_value={"kanban": {"default_assignee": "fallback"}},
        ):
            outcome = decomp.decompose_task(tid, author="me")
    finally:
        for p in patches:
            p.stop()

    assert outcome.ok, outcome.reason
    with kb.connect() as conn:
        task = kb.get_task(conn, tid)
    assert task is not None
    assert task.assignee == "fallback"


def test_decompose_unknown_assignee_falls_back_to_default(kanban_home):
    with kb.connect() as conn:
        tid = kb.create_task(conn, title="x", triage=True)

    # Roster only has 'orchestrator' and 'fallback'; LLM picks 'made_up'.
    llm_payload = jsonlib.dumps({
        "fanout": True,
        "rationale": "test",
        "tasks": [
            {"title": "do X", "body": "", "assignee": "made_up", "parents": []},
        ],
    })

    patches = _patch_list_profiles(["orchestrator", "fallback"])
    for p in patches:
        p.start()
    try:
        with patch.dict(
            "os.environ", {}, clear=False,
        ), _patch_aux_client(llm_payload), _patch_extra_body(), \
            patch(
                "hermes_cli.kanban_decompose._load_config",
                return_value={
                    "kanban": {
                        "orchestrator_profile": "orchestrator",
                        "default_assignee": "fallback",
                    }
                },
            ):
            outcome = decomp.decompose_task(tid, author="me")
    finally:
        for p in patches:
            p.stop()

    assert outcome.ok, outcome.reason
    assert outcome.child_ids and len(outcome.child_ids) == 1
    with kb.connect() as conn:
        child = kb.get_task(conn, outcome.child_ids[0])
    # 'made_up' wasn't in roster, so assignee rewritten to 'fallback'
    assert child.assignee == "fallback"


@pytest.mark.parametrize(
    "raw,expected",
    [
        ("code", "code"),
        (" CODE ", "code"),
        ("research", "research"),
        ("made-up", None),
        ("", None),
        (None, None),
        (123, None),
    ],
)
def test_normalize_kind_choice(raw, expected):
    assert decomp._normalize_kind_choice(
        raw,
        valid_kinds=decomp._VALID_TASK_KINDS,
    ) == expected


def test_children_from_parsed_normalizes_optional_kind(kanban_home):
    with kb.connect() as conn:
        tid = kb.create_task(conn, title="classify", triage=True)
        task = kb.get_task(conn, tid)

    parsed = {
        "fanout": True,
        "tasks": [
            {"title": "valid", "assignee": "writer", "kind": "CODE"},
            {"title": "invalid", "assignee": "writer", "kind": "garbage"},
            {"title": "missing", "assignee": "writer"},
        ],
    }

    children, err = decomp._children_from_parsed(
        parsed,
        task,
        valid_names={"writer"},
        default_assignee="writer",
    )

    assert err is None
    assert children is not None
    assert [child["kind"] for child in children] == ["code", None, None]


def test_decompose_handles_malformed_llm_json(kanban_home):
    with kb.connect() as conn:
        tid = kb.create_task(conn, title="x", triage=True)

    patches = _patch_list_profiles(["orchestrator"])
    for p in patches:
        p.start()
    try:
        with _patch_aux_client("not json at all, sorry"), _patch_extra_body():
            outcome = decomp.decompose_task(tid, author="me")
    finally:
        for p in patches:
            p.stop()

    assert outcome.ok is False
    assert "malformed JSON" in outcome.reason


def test_decompose_returns_false_when_task_not_triage(kanban_home):
    with kb.connect() as conn:
        tid = kb.create_task(conn, title="x")  # ready, not triage

    patches = _patch_list_profiles(["orchestrator"])
    for p in patches:
        p.start()
    try:
        outcome = decomp.decompose_task(tid, author="me")
    finally:
        for p in patches:
            p.stop()
    assert outcome.ok is False
    assert "not in triage" in outcome.reason


def test_decompose_no_aux_client_configured(kanban_home):
    with kb.connect() as conn:
        tid = kb.create_task(conn, title="x", triage=True)

    patches = _patch_list_profiles(["orchestrator"])
    for p in patches:
        p.start()
    try:
        with patch(
            "agent.auxiliary_client.get_text_auxiliary_client",
            return_value=(None, ""),
        ):
            outcome = decomp.decompose_task(tid, author="me")
    finally:
        for p in patches:
            p.stop()

    assert outcome.ok is False
    assert "no auxiliary client" in outcome.reason


def _assert_worker_scope_contract(body: str):
    assert "scope_contract:" in body
    assert "version: 2" in body
    assert "allowed_tools:" in body
    assert "kanban_show" in body
    assert "kanban_complete" in body
    assert "kanban_block" in body
    assert "kanban_comment" in body
    assert "completion_policy:" in body
    assert "require_scope_attestation: true" in body


def test_decompose_injects_scope_contract_for_worker_children(kanban_home):
    with kb.connect() as conn:
        tid = kb.create_task(conn, title="harden worker routing", triage=True)

    llm_payload = jsonlib.dumps({
        "fanout": True,
        "rationale": "worker graph",
        "tasks": [
            {"title": "admin check", "body": "Inspect operator state.", "assignee": "admin", "parents": []},
            {"title": "code change", "body": "Modify the decomposer.", "assignee": "coder", "parents": [0]},
            {"title": "review result", "body": "Review the diff.", "assignee": "reviewer", "parents": [1]},
        ],
    })

    patches = _patch_list_profiles(["orchestrator", "admin", "coder", "reviewer"])
    for p in patches:
        p.start()
    try:
        with _patch_aux_client(llm_payload), _patch_extra_body():
            outcome = decomp.decompose_task(tid, author="me")
    finally:
        for p in patches:
            p.stop()

    assert outcome.ok, outcome.reason
    assert outcome.child_ids and len(outcome.child_ids) == 3
    with kb.connect() as conn:
        children = [kb.get_task(conn, cid) for cid in outcome.child_ids]

    for child in children:
        assert child is not None
        _assert_worker_scope_contract(child.body or "")


def test_decompose_prompt_mentions_scope_contract_allowed_tools():
    assert "scope_contract" in decomp._SYSTEM_PROMPT
    assert "allowed_tools" in decomp._SYSTEM_PROMPT
    assert "completion_policy" in decomp._SYSTEM_PROMPT
    assert "require_scope_attestation" in decomp._SYSTEM_PROMPT


def test_decompose_prompt_requires_verifiable_acceptance_criteria():
    prompt = decomp._SYSTEM_PROMPT
    assert "at least two acceptance criteria" in prompt.lower()
    assert "AC-" in prompt
    assert "verification" in prompt.lower()
    assert "done_signal" in prompt


def test_decompose_prompt_documents_optional_kind():
    prompt = decomp._SYSTEM_PROMPT
    assert '"kind"' in prompt


def test_decompose_prompt_documents_3a_lane_policy():
    prompt = decomp._SYSTEM_PROMPT
    assert "Lane routing table" in prompt
    assert "coder-claude" in prompt
    assert "reviewer and critic: verdict-only lanes" in prompt
    assert "research: research lane" in prompt
    assert 'Do not invent "researcher" as an alias' in prompt
    assert "code|research|review|ops|text" in prompt
    assert "null if unsure" in prompt


def test_decompose_prompt_separates_coder_and_claude_coder():
    """Phase A: two clean coder families in the lane table —
    coder = Codex/GPT default; premium = the Claude coder; coder-claude =
    deprecated alias of premium; no invented opus-coder lane."""
    prompt = decomp._SYSTEM_PROMPT
    assert "OpenAI-Codex/GPT" in prompt
    assert "premium: the Claude code lane" in prompt
    assert "DEPRECATED alias of premium" in prompt
    assert "opus-coder" not in prompt


def test_decompose_prompt_frames_roster_stats_as_background():
    prompt = decomp._SYSTEM_PROMPT
    assert "stats:" in prompt
    assert "background information about past" in prompt
    assert "not routing instructions" in prompt
    assert "structural review gate" in prompt


def test_worker_scope_contract_validator_rejects_broad_allowed_tools():
    children = [{
        "title": "unsafe worker",
        "body": (
            "scope_contract:\n"
            "  version: 2\n"
            "  allowed_tools:\n"
            "    - all\n"
            "completion_policy:\n"
            "  require_scope_attestation: true\n"
        ),
        "assignee": "coder",
    }]

    report = decomp.validate_worker_scope_contracts(children)

    assert report.ok is False
    assert report.issues
    assert "broad" in report.issues[0].reason


def test_decompose_blocks_before_db_insert_when_worker_contract_invalid(kanban_home, monkeypatch):
    with kb.connect() as conn:
        tid = kb.create_task(conn, title="block invalid worker", triage=True)

    llm_payload = jsonlib.dumps({
        "fanout": True,
        "rationale": "worker graph",
        "tasks": [
            {"title": "code change", "body": "Modify the decomposer.", "assignee": "coder", "parents": []},
        ],
    })

    monkeypatch.setattr(
        decomp,
        "_ensure_worker_scope_contract",
        lambda child, **kwargs: {**child, "body": "scope_contract:\n  version: 2\n  allowed_tools:\n    - all\n"},
    )
    patches = _patch_list_profiles(["orchestrator", "coder"])
    for p in patches:
        p.start()
    try:
        with _patch_aux_client(llm_payload), _patch_extra_body():
            outcome = decomp.decompose_task(tid, author="me")
    finally:
        for p in patches:
            p.stop()

    assert outcome.ok is False
    assert "worker scope contract" in outcome.reason
    with kb.connect() as conn:
        root = kb.get_task(conn, tid)
        tasks = kb.list_tasks(conn, limit=10)
    assert root is not None
    assert root.status == "triage"
    assert [task.id for task in tasks] == [tid]


# ---------------------------------------------------------------------------
# CRITICAL: / MANDATORY: / absolute-path / task-id preservation
# (Hardening sprint 2026-05-27 TASK 4)
#
# The decomposer's LLM pass has historically watered down strict
# constraints in the parent body. The system-prompt now mandates verbatim
# preservation and a post-validator warns when the LLM still drops them.
# These tests pin both the prompt anchor and the validator behaviour.
# ---------------------------------------------------------------------------


def test_decompose_prompt_mentions_critical_preservation():
    """Anchor test: the decomposer's system prompt must instruct the
    LLM to preserve CRITICAL / MANDATORY / MUST / NEVER lines and
    absolute paths verbatim. If this assertion breaks, double-check
    that the rewrite still carries the constraint-preservation rule —
    the rule is load-bearing for hardening-20260527 TASK 4.
    """
    assert "CRITICAL:" in decomp._SYSTEM_PROMPT
    assert "MANDATORY:" in decomp._SYSTEM_PROMPT
    assert "VERBATIM" in decomp._SYSTEM_PROMPT.upper()
    assert "absolute filesystem path" in decomp._SYSTEM_PROMPT.lower()


def test_validator_passes_when_critical_line_in_one_kid():
    parent_body = (
        "Build the thing.\n"
        "CRITICAL: copy all artifacts to /home/piet/.hermes/reports/sprint/ "
        "BEFORE kanban_complete\n"
    )
    kids = [
        {"title": "research", "body": "look it up"},
        {
            "title": "build",
            "body": (
                "Code the thing.\n"
                "CRITICAL: copy all artifacts to "
                "/home/piet/.hermes/reports/sprint/ BEFORE kanban_complete"
            ),
        },
    ]
    report = decomp.validate_constraint_preservation(parent_body, kids)
    assert report.ok is True
    assert report.missing_constraints == []
    assert report.missing_paths == []


def test_validator_flags_missing_critical_line():
    """The 2026-05-27 Discord-Report-Sprint failure case: parent had a
    CRITICAL: artifact-copy line, kids paraphrased it away. The
    validator must report the original line as missing.
    """
    parent_body = (
        "CRITICAL: copy all artifacts to ~/.hermes/reports/discord-report-sprint/ "
        "BEFORE kanban_complete\n"
    )
    kids = [
        {
            "title": "build",
            "body": "Preserve or copy artifacts if implementation requires.",
        },
        {
            "title": "review",
            "body": "Verify completeness.",
        },
    ]
    report = decomp.validate_constraint_preservation(parent_body, kids)
    assert report.ok is False
    assert len(report.missing_constraints) == 1
    assert (
        "discord-report-sprint" in report.missing_constraints[0]
    )
    # Path is also missing (paraphrased).
    assert any(
        "discord-report-sprint" in p
        for p in report.missing_paths
    )


def test_validator_flags_missing_absolute_path():
    parent_body = (
        "Write the receipt to /tmp/test-sprint/foo.txt before completing.\n"
    )
    kids = [
        {"title": "kid", "body": "Write the receipt to the sprint dir."},
    ]
    report = decomp.validate_constraint_preservation(parent_body, kids)
    assert report.ok is False
    assert report.missing_paths == ["/tmp/test-sprint/foo.txt"]


def test_validator_flags_missing_task_id():
    parent_body = (
        "Continue work on t_d8b446b9 — fix the reviewer block.\n"
    )
    kids = [
        {"title": "kid", "body": "Continue work on the parent sprint task."},
    ]
    report = decomp.validate_constraint_preservation(parent_body, kids)
    assert report.ok is False
    assert report.missing_task_ids == ["t_d8b446b9"]


def test_validator_ok_when_one_kid_has_path_and_id():
    """A single kid carrying the path + id is sufficient. The rule is
    "at least one kid", not "every kid".
    """
    parent_body = (
        "Continue on t_d8b446b9. Output goes to /tmp/test-sprint/foo.txt.\n"
    )
    kids = [
        {"title": "research", "body": "scout the codebase"},
        {
            "title": "implement",
            "body": "Continue on t_d8b446b9 and write /tmp/test-sprint/foo.txt.",
        },
    ]
    report = decomp.validate_constraint_preservation(parent_body, kids)
    assert report.ok is True


def test_validator_handles_empty_parent_body():
    report = decomp.validate_constraint_preservation(
        "",
        [{"title": "k", "body": "noop"}],
    )
    assert report.ok is True


def test_validator_handles_mandatory_must_never_prefixes():
    parent_body = (
        "MANDATORY: keep things in sandbox mode\n"
        "MUST: run tests\n"
        "NEVER: touch production\n"
    )
    kids_drop_all = [
        {"title": "k", "body": "do the work carefully"},
    ]
    report = decomp.validate_constraint_preservation(parent_body, kids_drop_all)
    assert {line.split(":", 1)[0] for line in report.missing_constraints} == {
        "MANDATORY", "MUST", "NEVER",
    }


def test_validator_critical_line_with_leading_bullet():
    """Bulleted constraint lines must still be recognised — the
    decomposer sometimes sees the body as a bulleted list and the
    preservation rule applies regardless of bullet style.
    """
    parent_body = (
        "- CRITICAL: do not rename module foo.py\n"
    )
    kids = [
        {"title": "k", "body": "do not rename module foo.py"},
    ]
    # Verbatim CRITICAL line is not in the kid — must report missing.
    report = decomp.validate_constraint_preservation(parent_body, kids)
    assert report.ok is False
    assert any(
        line.endswith("CRITICAL: do not rename module foo.py")
        for line in report.missing_constraints
    )


def test_decompose_logs_warning_when_critical_not_preserved(kanban_home, caplog):
    """End-to-end: a parent task with CRITICAL: + absolute path; the
    LLM response strips both. The validator must log a warning during
    decompose_task that mentions "MANDATORY comments".
    """
    parent_body = (
        "Plan the sprint.\n"
        "CRITICAL: copy artifacts to /tmp/hsv1/out.md BEFORE kanban_complete\n"
    )
    with kb.connect() as conn:
        tid = kb.create_task(
            conn,
            title="sprint plan",
            body=parent_body,
            triage=True,
        )

    llm_payload = jsonlib.dumps({
        "fanout": True,
        "rationale": "split it",
        "tasks": [
            {
                "title": "research",
                "body": "Scout the area.",
                "assignee": "researcher",
                "parents": [],
            },
            {
                "title": "implement",
                "body": "Write the code and verify outputs.",
                "assignee": "engineer",
                "parents": [0],
            },
        ],
    })

    patches = _patch_list_profiles(["orchestrator", "researcher", "engineer"])
    for p in patches:
        p.start()
    try:
        with caplog.at_level("WARNING", logger="hermes_cli.kanban_decompose"):
            with _patch_aux_client(llm_payload), _patch_extra_body():
                outcome = decomp.decompose_task(tid, author="me")
    finally:
        for p in patches:
            p.stop()

    assert outcome.ok is True
    assert outcome.fanout is True

    # The validator must have logged both warnings.
    messages = [r.getMessage() for r in caplog.records]
    assert any("MANDATORY" in m and "CRITICAL" in m.upper() for m in messages), (
        "expected validator to warn about missing CRITICAL line; got: %r" % messages
    )
    assert any("/tmp/hsv1/out.md" in m for m in messages), (
        "expected validator to warn about missing absolute path; got: %r" % messages
    )


# ---------------------------------------------------------------------------
# plan_and_document — Flow capture Phase B (documented/lean + gate + spec)
# ---------------------------------------------------------------------------

def _park_in_scheduled(tid: str) -> None:
    """Mimic the flow-capture endpoint: triage -> todo -> scheduled."""
    with kb.connect() as conn:
        with kb.write_txn(conn):
            conn.execute("UPDATE tasks SET status='todo' WHERE id=?", (tid,))
        kb.schedule_task(conn, tid, reason="parked for flow-plan")
        assert kb.get_task(conn, tid).status == "scheduled"


_DOC_PAYLOAD = jsonlib.dumps({
    "fanout": True,
    "rationale": "split into independent + one dependent step",
    "narrative": "We break the request into three parts: two parallel builds and a review that depends on both.",
    "tasks": [
        {"title": "part one", "body": "do part one", "assignee": "engineer", "parents": [], "summary": "delivers part one"},
        {"title": "part two", "body": "do part two", "assignee": "engineer", "parents": [], "summary": "delivers part two"},
        {"title": "review both", "body": "review 1+2", "assignee": "reviewer", "parents": [0, 1], "summary": "reviews the two parts"},
    ],
})


def test_plan_and_document_gate_writes_spec_and_holds_children(kanban_home, tmp_path, monkeypatch):
    spec_dir = tmp_path / "flow-plans"
    monkeypatch.setenv("HERMES_FLOW_PLANS_DIR", str(spec_dir))
    with kb.connect() as conn:
        tid = kb.create_task(conn, title="Build a thing in 3 parts", body="part1; part2; part3", triage=True, tenant="flow-capture")
    _park_in_scheduled(tid)

    patches = _patch_list_profiles(["orchestrator", "engineer", "reviewer"])
    for p in patches:
        p.start()
    try:
        with _patch_aux_client(_DOC_PAYLOAD), _patch_extra_body():
            outcome = decomp.plan_and_document(tid, gate=True, document=True, author="user")
    finally:
        for p in patches:
            p.stop()

    assert outcome.ok, outcome.reason
    assert outcome.fanout is True
    assert outcome.gated is True
    assert outcome.spec_relpath == f"{tid}.md"
    assert outcome.child_ids and len(outcome.child_ids) == 3

    # Children are HELD in scheduled (gate), root flips to todo.
    with kb.connect() as conn:
        assert kb.get_task(conn, tid).status == "todo"
        statuses = [kb.get_task(conn, c).status for c in outcome.child_ids]
        # The dispatcher tick (recompute_ready) must not leak held children.
        kb.recompute_ready(conn)
        statuses_after = [kb.get_task(conn, c).status for c in outcome.child_ids]
    assert statuses == ["scheduled", "scheduled", "scheduled"], statuses
    assert statuses_after == ["scheduled", "scheduled", "scheduled"], "GATE LEAK"

    # Spec written: narrative on top, structured subtask table with child ids.
    spec = (spec_dir / f"{tid}.md").read_text(encoding="utf-8")
    assert "## Narrativ" in spec
    assert "three parts" in spec
    assert "## Subtasks (3)" in spec
    assert "Gate aktiv" in spec
    for c in outcome.child_ids:
        assert c in spec, "every child id must appear in the spec table (spec == subtasks)"

    # flow_plan event recorded on the root for the dashboard rail link.
    with kb.connect() as conn:
        kinds = [e.kind for e in kb.list_events(conn, tid)]
    assert "flow_plan" in kinds


def test_plan_and_document_auto_promotes_and_writes_spec(kanban_home, tmp_path, monkeypatch):
    spec_dir = tmp_path / "flow-plans"
    monkeypatch.setenv("HERMES_FLOW_PLANS_DIR", str(spec_dir))
    with kb.connect() as conn:
        tid = kb.create_task(conn, title="Auto build", triage=True, tenant="flow-capture")
    _park_in_scheduled(tid)

    patches = _patch_list_profiles(["orchestrator", "engineer", "reviewer"])
    for p in patches:
        p.start()
    try:
        with _patch_aux_client(_DOC_PAYLOAD), _patch_extra_body():
            outcome = decomp.plan_and_document(tid, gate=False, document=True, author="user")
    finally:
        for p in patches:
            p.stop()

    assert outcome.ok, outcome.reason
    assert outcome.gated is False
    with kb.connect() as conn:
        statuses = sorted(kb.get_task(conn, c).status for c in outcome.child_ids)
    # Parent-free children -> ready, dependent child waits in todo.
    assert statuses == ["ready", "ready", "todo"], statuses
    assert (spec_dir / f"{tid}.md").is_file()


def test_plan_and_document_lean_gate_holds_without_spec(kanban_home, tmp_path, monkeypatch):
    spec_dir = tmp_path / "flow-plans"
    monkeypatch.setenv("HERMES_FLOW_PLANS_DIR", str(spec_dir))
    with kb.connect() as conn:
        tid = kb.create_task(conn, title="Lean gated", triage=True, tenant="flow-capture")
    _park_in_scheduled(tid)

    patches = _patch_list_profiles(["orchestrator", "engineer", "reviewer"])
    for p in patches:
        p.start()
    try:
        with _patch_aux_client(_DOC_PAYLOAD), _patch_extra_body():
            outcome = decomp.plan_and_document(tid, gate=True, document=False, author="user")
    finally:
        for p in patches:
            p.stop()

    assert outcome.ok, outcome.reason
    assert outcome.gated is True
    assert outcome.spec_relpath is None, "lean method writes no spec"
    assert not (spec_dir / f"{tid}.md").exists()
    with kb.connect() as conn:
        statuses = [kb.get_task(conn, c).status for c in outcome.child_ids]
        kinds = [e.kind for e in kb.list_events(conn, tid)]
    assert all(s == "scheduled" for s in statuses), statuses
    assert "flow_plan" not in kinds, "lean method emits no flow_plan event"


def test_plan_and_document_rejects_non_scheduled_root(kanban_home):
    with kb.connect() as conn:
        tid = kb.create_task(conn, title="still in triage", triage=True)
    patches = _patch_list_profiles(["orchestrator", "engineer"])
    for p in patches:
        p.start()
    try:
        with _patch_aux_client(_DOC_PAYLOAD), _patch_extra_body():
            outcome = decomp.plan_and_document(tid, gate=True, document=True, author="user")
    finally:
        for p in patches:
            p.stop()
    assert not outcome.ok
    assert "scheduled" in outcome.reason


# ---------------------------------------------------------------------------
# N-Epics P5: konservative Auto-Zuordnung beim Zerlegen
# ---------------------------------------------------------------------------

_EPIC_FANOUT = {
    "fanout": True,
    "rationale": "split",
    "tasks": [
        {"title": "research", "body": "look it up", "assignee": None, "parents": []},
        {"title": "build", "body": "code it", "assignee": None, "parents": [0]},
    ],
}


def _run_decompose(tid: str, payload: dict) -> "decomp.DecomposeOutcome":
    patches = _patch_list_profiles(["orchestrator", "fallback"])
    for p in patches:
        p.start()
    try:
        with _patch_aux_client(jsonlib.dumps(payload)), _patch_extra_body():
            return decomp.decompose_task(tid, author="me")
    finally:
        for p in patches:
            p.stop()


def test_p5_decompose_assigns_matching_open_epic_and_children_inherit(kanban_home):
    with kb.connect() as conn:
        eid = kb.create_epic(conn, title="Dashboard reliability")
        tid = kb.create_task(conn, title="harden the dashboard", triage=True)
    outcome = _run_decompose(tid, {**_EPIC_FANOUT, "epic": eid})
    assert outcome.ok, outcome.reason
    with kb.connect() as conn:
        assert kb.get_task(conn, tid).epic_id == eid
        for cid in outcome.child_ids:
            assert kb.get_task(conn, cid).epic_id == eid


def test_p5_decompose_ignores_hallucinated_epic_id(kanban_home):
    with kb.connect() as conn:
        kb.create_epic(conn, title="real epic")
        tid = kb.create_task(conn, title="unrelated", triage=True)
    outcome = _run_decompose(tid, {**_EPIC_FANOUT, "epic": "e_deadbeef"})
    assert outcome.ok, outcome.reason
    with kb.connect() as conn:
        assert kb.get_task(conn, tid).epic_id is None


def test_p5_decompose_ignores_closed_epic(kanban_home):
    with kb.connect() as conn:
        eid = kb.create_epic(conn, title="finished initiative")
        kb.close_epic(conn, eid)
        tid = kb.create_task(conn, title="late arrival", triage=True)
    outcome = _run_decompose(tid, {**_EPIC_FANOUT, "epic": eid})
    assert outcome.ok, outcome.reason
    with kb.connect() as conn:
        assert kb.get_task(conn, tid).epic_id is None


def test_p5_decompose_null_epic_means_no_epic(kanban_home):
    with kb.connect() as conn:
        kb.create_epic(conn, title="open but unrelated")
        tid = kb.create_task(conn, title="standalone", triage=True)
    outcome = _run_decompose(tid, {**_EPIC_FANOUT, "epic": None})
    assert outcome.ok, outcome.reason
    with kb.connect() as conn:
        assert kb.get_task(conn, tid).epic_id is None


def test_p5_decompose_operator_assignment_wins(kanban_home):
    """Ein vom Operator gesetztes Epic wird nie vom Decomposer umgehängt."""
    with kb.connect() as conn:
        manual = kb.create_epic(conn, title="operator pick")
        other = kb.create_epic(conn, title="llm pick")
        tid = kb.create_task(conn, title="pre-assigned", triage=True, epic_id=manual)
    outcome = _run_decompose(tid, {**_EPIC_FANOUT, "epic": other})
    assert outcome.ok, outcome.reason
    with kb.connect() as conn:
        assert kb.get_task(conn, tid).epic_id == manual


def test_p5_prompt_lists_open_epics_only(kanban_home):
    with kb.connect() as conn:
        open_eid = kb.create_epic(conn, title="open initiative")
        closed_eid = kb.create_epic(conn, title="closed initiative")
        kb.close_epic(conn, closed_eid)
        tid = kb.create_task(conn, title="anything", triage=True)

    client = _mock_client_returning(jsonlib.dumps({**_EPIC_FANOUT, "epic": None}))
    patches = _patch_list_profiles(["orchestrator", "fallback"])
    for p in patches:
        p.start()
    try:
        with patch(
            "agent.auxiliary_client.get_text_auxiliary_client",
            return_value=(client, "test-model"),
        ), _patch_extra_body():
            outcome = decomp.decompose_task(tid, author="me")
    finally:
        for p in patches:
            p.stop()

    assert outcome.ok, outcome.reason
    user_msg = client.chat.completions.create.call_args.kwargs["messages"][1]["content"]
    assert "Open epics" in user_msg
    assert open_eid in user_msg
    assert closed_eid not in user_msg


def test_p5_prompt_says_none_without_open_epics(kanban_home):
    with kb.connect() as conn:
        tid = kb.create_task(conn, title="anything", triage=True)
    client = _mock_client_returning(jsonlib.dumps(_EPIC_FANOUT))
    patches = _patch_list_profiles(["orchestrator", "fallback"])
    for p in patches:
        p.start()
    try:
        with patch(
            "agent.auxiliary_client.get_text_auxiliary_client",
            return_value=(client, "test-model"),
        ), _patch_extra_body():
            outcome = decomp.decompose_task(tid, author="me")
    finally:
        for p in patches:
            p.stop()
    assert outcome.ok, outcome.reason
    user_msg = client.chat.completions.create.call_args.kwargs["messages"][1]["content"]
    assert "(none)" in user_msg
