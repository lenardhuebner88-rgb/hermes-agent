"""Tests for the decomposer module + `hermes kanban decompose` CLI surface.

The auxiliary LLM client is mocked — no network calls. Tests exercise the
prompt plumbing, response parsing, DB writes (via the real DB helper),
and the assignee-fallback logic.
"""

from __future__ import annotations

import json as jsonlib
import os
import sqlite3
import subprocess
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from hermes_cli import kanban_db as kb
from hermes_cli import kanban_decompose as decomp
from hermes_cli import kanban_worktrees as kwt


@pytest.fixture
def kanban_home(tmp_path, monkeypatch):
    home = tmp_path / ".hermes"
    home.mkdir()
    for key in list(os.environ):
        if key.startswith("HERMES_KANBAN_"):
            monkeypatch.delenv(key, raising=False)
    monkeypatch.setenv("HERMES_HOME", str(home))
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    db_path = kb.kanban_db_path(board="default")
    live_db = Path("/home/piet/.hermes/kanban.db").resolve()
    assert db_path.resolve() != live_db
    assert home.resolve() in db_path.resolve().parents
    kb._INITIALIZED_PATHS.discard(str(db_path.resolve()))
    kb.init_db()
    return home


def _git(repo, *args) -> str:
    proc = subprocess.run(
        ["git", "-C", str(repo), *args],
        check=True,
        capture_output=True,
        text=True,
    )
    return proc.stdout.strip()


@pytest.fixture
def repo(tmp_path):
    r = tmp_path / "repo"
    r.mkdir()
    _git(r, "init", "-b", "main")
    _git(r, "config", "user.email", "t@example.com")
    _git(r, "config", "user.name", "tester")
    (r / "base.txt").write_text("base\n")
    _git(r, "add", "-A")
    _git(r, "commit", "-m", "base")
    return r


def _commit_in(repo_or_wt, relpath, content, msg="change"):
    p = Path(repo_or_wt) / relpath
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(content)
    _git(repo_or_wt, "add", "-A")
    _git(repo_or_wt, "commit", "-m", msg)


def _ok_gate(_repo, _files):
    return True, "stub gate"


def _events(conn, task_id, kind):
    rows = conn.execute(
        "SELECT payload FROM task_events WHERE task_id = ? AND kind = ? "
        "ORDER BY id",
        (task_id, kind),
    ).fetchall()
    return [jsonlib.loads(r["payload"]) if r["payload"] else {} for r in rows]


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
    # decompose_task now routes through call_llm (see #35566) — mock it at
    # the source module so task config, extra_body, and retries stay out of
    # unit-test scope.
    return patch(
        "agent.auxiliary_client.call_llm",
        return_value=_fake_aux_response(content),
    )


def _patch_extra_body():
    # No-op shim retained for call-site compatibility: extra_body plumbing
    # now lives inside call_llm, which _patch_aux_client already mocks.
    return patch("agent.auxiliary_client.get_auxiliary_extra_body", return_value={})


def _task_stub(task_id: str = "t_parent", body: str = ""):
    return type("TaskStub", (), {"id": task_id, "body": body})()


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


def test_decompose_children_auto_integrate_root_finalizer(
    kanban_home, repo, monkeypatch
):
    """All green children of a decomposed repo root finalize the root branch."""
    monkeypatch.setattr(kwt, "default_quick_gate", _ok_gate)
    with kb.connect() as conn:
        root = kb.create_task(
            conn,
            title="ship decomposed feature",
            assignee="orchestrator",
            triage=True,
            workspace_kind="dir",
            workspace_path=str(repo),
        )
        child_ids = kb.decompose_triage_task(
            conn,
            root,
            root_assignee="orchestrator",
            children=[
                {
                    "title": "build part A",
                    "body": "A",
                    "assignee": "coder",
                    "parents": [],
                },
                {
                    "title": "build part B",
                    "body": "B",
                    "assignee": "coder",
                    "parents": [],
                },
            ],
            author="decomposer",
        )
        assert child_ids is not None
        child_a, child_b = child_ids

        task_a = kb.claim_task(conn, child_a)
        ws_a = kwt.provision_for_task(conn, task_a, str(repo))
        assert ws_a.name == root
        assert _git(ws_a, "symbolic-ref", "--short", "HEAD") == f"kanban/{root}"
        _commit_in(ws_a, "child_a.py", "VALUE_A = 1\n", msg="child A")
        assert kb.complete_task(conn, child_a, result="child A done")
        assert not (repo / "child_a.py").exists()
        assert ws_a.exists()

        task_b = kb.claim_task(conn, child_b)
        ws_b = kwt.provision_for_task(conn, task_b, str(repo))
        assert ws_b == ws_a
        _commit_in(ws_b, "child_b.py", "VALUE_B = 2\n", msg="child B")
        assert kb.complete_task(conn, child_b, result="child B done")

        root_task = kb.get_task(conn, root)
        child_b_task = kb.get_task(conn, child_b)
        merged = _events(conn, child_b, "integration_merged")
        auto_done = _events(conn, root, "decompose_root_auto_completed")

    assert child_b_task.status == "done"
    assert root_task.status == "done"
    assert len(merged) == 1
    assert merged[0]["branch"] == f"kanban/{root}"
    assert (repo / "child_a.py").read_text() == "VALUE_A = 1\n"
    assert (repo / "child_b.py").read_text() == "VALUE_B = 2\n"
    assert len(_git(repo, "log", "--merges", "--oneline").splitlines()) == 1
    assert not ws_a.exists()
    assert auto_done and auto_done[-1]["completed_by"] == child_b


def test_decompose_string_false_auto_promote_holds_children(kanban_home):
    with kb.connect() as conn:
        tid = kb.create_task(conn, title="ship deliberately", triage=True)

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
        with _patch_aux_client(llm_payload), _patch_extra_body(), patch(
            "hermes_cli.kanban_decompose._load_config",
            return_value={"kanban": {"auto_promote_children": "false"}},
        ):
            outcome = decomp.decompose_task(tid, author="me")
    finally:
        for p in patches:
            p.stop()

    assert outcome.ok, outcome.reason
    with kb.connect() as conn:
        statuses = [kb.get_task(conn, c).status for c in outcome.child_ids]
    assert statuses == ["todo", "todo"], statuses


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
            {"title": "check X", "body": "", "assignee": "fallback", "parents": [0]},
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
    assert outcome.child_ids and len(outcome.child_ids) == 2
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
            {"title": "invalid", "assignee": "writer", "kind": "garbage", "parents": [0]},
            {"title": "missing", "assignee": "writer", "parents": [1]},
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


def test_decompose_demotes_single_task_fanout_to_single_owner(kanban_home):
    with kb.connect() as conn:
        tid = kb.create_task(
            conn,
            title="single parent",
            body="Original single-owner spec.",
            triage=True,
        )

    llm_payload = jsonlib.dumps({
        "fanout": True,
        "rationale": "accidental single fanout",
        "tasks": [
            {"title": "single", "body": "do it", "assignee": "writer", "parents": []},
        ],
    })

    patches = _patch_list_profiles(["orchestrator", "writer"])
    for p in patches:
        p.start()
    try:
        with _patch_aux_client(llm_payload), _patch_extra_body(), patch(
            "hermes_cli.kanban_decompose._load_config",
            return_value={"kanban": {"default_assignee": "writer"}},
        ):
            outcome = decomp.decompose_task(tid, author="me")
    finally:
        for p in patches:
            p.stop()

    assert outcome.ok, outcome.reason
    assert outcome.fanout is False
    assert outcome.child_ids is None
    assert "demoted from fanout" in outcome.reason
    with kb.connect() as conn:
        task = kb.get_task(conn, tid)
        tasks = kb.list_tasks(conn, limit=10)
    assert task is not None
    assert task.status == "ready"
    assert task.title == "single parent"
    assert task.body == "Original single-owner spec."
    assert task.assignee == "writer"
    assert [task.id for task in tasks] == [tid]


def test_children_from_parsed_caps_at_six():
    children, err = decomp._children_from_parsed(
        {
            "fanout": True,
            "tasks": [
                {"title": f"task {idx}", "body": "", "assignee": "writer", "parents": []}
                for idx in range(7)
            ],
        },
        _task_stub(),
        valid_names={"writer"},
        default_assignee="writer",
    )

    assert err is None
    assert children is not None
    assert len(children) == 6


def test_decompose_demotes_three_fully_independent_tasks_to_single_owner(kanban_home):
    with kb.connect() as conn:
        tid = kb.create_task(
            conn,
            title="three independent chores",
            body="Keep this as one owner-visible task.",
            triage=True,
        )

    llm_payload = jsonlib.dumps({
        "fanout": True,
        "rationale": "accidental over-split",
        "tasks": [
            {"title": "first", "body": "", "assignee": "writer", "parents": []},
            {"title": "second", "body": "", "assignee": "writer", "parents": []},
            {"title": "third", "body": "", "assignee": "writer", "parents": []},
        ],
    })

    patches = _patch_list_profiles(["orchestrator", "writer"])
    for p in patches:
        p.start()
    try:
        with _patch_aux_client(llm_payload), _patch_extra_body(), patch(
            "hermes_cli.kanban_db.record_decompose_failure",
        ) as record_failure, patch(
            "hermes_cli.kanban_decompose._load_config",
            return_value={"kanban": {"default_assignee": "writer"}},
        ):
            outcome = decomp.decompose_task(tid, author="me")
    finally:
        for p in patches:
            p.stop()

    record_failure.assert_not_called()
    assert outcome.ok, outcome.reason
    assert outcome.fanout is False
    assert outcome.child_ids is None
    assert "demoted from fanout" in outcome.reason
    with kb.connect() as conn:
        task = kb.get_task(conn, tid)
        tasks = kb.list_tasks(conn, limit=10)
        row = conn.execute(
            "SELECT decompose_failed, last_failure_error FROM tasks WHERE id = ?",
            (tid,),
        ).fetchone()
    assert task is not None
    assert task.status == "ready"
    assert task.title == "three independent chores"
    assert task.body == "Keep this as one owner-visible task."
    assert task.assignee == "writer"
    assert [task.id for task in tasks] == [tid]
    assert row["decompose_failed"] == 0
    assert row["last_failure_error"] is None


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
        # call_llm raises RuntimeError when no provider is configured; the
        # decomposer must convert that into a failed outcome, not a crash.
        with patch(
            "agent.auxiliary_client.call_llm",
            side_effect=RuntimeError("No LLM provider configured"),
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


def test_decompose_prompt_offers_scout_prep_lane():
    """Slice c: the decomposer may propose a read-only scout recon predecessor."""
    prompt = decomp._SYSTEM_PROMPT
    assert "scout:" in prompt          # listed in the lane routing table
    assert "recon" in prompt.lower()
    assert "read-only" in prompt.lower()


def test_scout_is_a_worker_scope_lane_and_gets_a_contract():
    """Slice c: scout is a real gateway worker — treated as a worker lane and
    given the kanban-lifecycle scope contract like research."""
    assert "scout" in decomp._WORKER_SCOPE_LANES
    assert decomp._is_worker_lane("scout")
    child = {"title": "recon the area", "body": "Read the affected code.", "assignee": "scout", "parents": []}
    out = decomp._ensure_worker_scope_contract(child)
    body = out["body"] or ""
    _assert_worker_scope_contract(body)
    # Befund 7 / Fix 2: scout is read-only — no write tools in the attestation.
    assert "write_file" not in body
    assert "patch" not in body


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


def test_worker_scope_contract_validator_accepts_valid_contract():
    """Positive-path coverage for WorkerScopeContractReport.ok.

    The existing rejection test only exercises ``ok is False`` for non-empty
    issue lists.  A valid worker-lane child with a well-formed scope_contract
    and only known allowed_tools must yield ``issues == []`` so ``ok is True``.
    Without this assertion, mutation-testing survivors on ``return not
    self.issues`` (line 655) go undetected because the True branch is never
    observed.
    """
    children = [{
        "title": "safe worker",
        "body": (
            "scope_contract:\n"
            "  version: 2\n"
            "  allowed_tools:\n"
            "    - kanban_show\n"
            "    - kanban_complete\n"
            "    - kanban_block\n"
            "    - kanban_comment\n"
            "    - read_file\n"
            "    - terminal\n"
            "completion_policy:\n"
            "  require_scope_attestation: true\n"
        ),
        "assignee": "coder",
    }]

    report = decomp.validate_worker_scope_contracts(children)

    assert report.issues == []
    assert report.ok is True


def test_worker_scope_contract_validator_rejects_duplicate_blocks():
    """Exactly one structured scope_contract block is accepted. A second
    (decoy) block is a spoofing vector — the first-match extractor attests
    against block #1 while a reader could act on a broader block #2 — so the
    validator must fail closed (S1b finding, autoscout-context-integrity)."""
    legit = (
        "scope_contract:\n"
        "  version: 2\n"
        "  allowed_tools:\n"
        "    - read_file\n"
        "    - terminal\n"
        "completion_policy:\n"
        "  require_scope_attestation: true\n"
    )
    decoy = (
        "scope_contract:\n"
        "  version: 2\n"
        "  allowed_tools:\n"
        "    - all\n"
    )
    children = [{
        "title": "spoofed worker",
        "body": legit + "\n" + decoy,
        "assignee": "coder",
    }]

    report = decomp.validate_worker_scope_contracts(children)

    assert report.ok is False
    assert "multiple scope_contract blocks" in report.issues[0].reason


def test_count_scope_contract_blocks_ignores_prose_mentions():
    """Prose references like 'scope_contract/allowed paths' are NOT block
    headers, so a single real block still counts as one (no false reject on
    bodies that merely mention the contract in prose)."""
    body = (
        "Inherit scope_contract/allowed paths where present.\n"
        "scope_contract:\n"
        "  version: 2\n"
        "  allowed_tools:\n"
        "    - terminal\n"
    )
    assert decomp._count_scope_contract_blocks(body) == 1


def test_decompose_blocks_before_db_insert_when_worker_contract_invalid(kanban_home, monkeypatch):
    with kb.connect() as conn:
        tid = kb.create_task(conn, title="block invalid worker", triage=True)

    llm_payload = jsonlib.dumps({
        "fanout": True,
        "rationale": "worker graph",
        "tasks": [
            {"title": "code change", "body": "Modify the decomposer.", "assignee": "coder", "parents": []},
            {"title": "review change", "body": "Review the decomposer.", "assignee": "coder", "parents": [0]},
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


def test_plan_and_document_demotes_independent_lt4_to_single_owner(kanban_home):
    with kb.connect() as conn:
        tid = kb.create_task(
            conn,
            title="Flow single-owner",
            body="Keep the flow root as one task.",
            triage=True,
            tenant="flow-capture",
        )
    _park_in_scheduled(tid)

    payload = jsonlib.dumps({
        "fanout": True,
        "rationale": "accidental over-split",
        "tasks": [
            {"title": "first", "body": "", "assignee": "writer", "parents": []},
            {"title": "second", "body": "", "assignee": "writer", "parents": []},
            {"title": "third", "body": "", "assignee": "writer", "parents": []},
        ],
    })

    patches = _patch_list_profiles(["orchestrator", "writer"])
    for p in patches:
        p.start()
    try:
        with _patch_aux_client(payload), _patch_extra_body(), patch(
            "hermes_cli.kanban_decompose._load_config",
            return_value={"kanban": {"default_assignee": "writer"}},
        ):
            outcome = decomp.plan_and_document(
                tid, gate=False, document=False, author="user",
            )
    finally:
        for p in patches:
            p.stop()

    assert outcome.ok, outcome.reason
    assert outcome.fanout is False
    assert outcome.child_ids is None
    assert "demoted from fanout" in outcome.reason
    with kb.connect() as conn:
        task = kb.get_task(conn, tid)
        tasks = kb.list_tasks(conn, limit=10)
    assert task is not None
    assert task.status == "ready"
    assert task.title == "Flow single-owner"
    assert task.body == "Keep the flow root as one task."
    assert task.assignee == "writer"
    assert [task.id for task in tasks] == [tid]


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

    response = _fake_aux_response(jsonlib.dumps({**_EPIC_FANOUT, "epic": None}))
    patches = _patch_list_profiles(["orchestrator", "fallback"])
    for p in patches:
        p.start()
    try:
        with patch("agent.auxiliary_client.call_llm", return_value=response) as call_llm:
            outcome = decomp.decompose_task(tid, author="me")
    finally:
        for p in patches:
            p.stop()

    assert outcome.ok, outcome.reason
    user_msg = call_llm.call_args.kwargs["messages"][1]["content"]
    assert "Open epics" in user_msg
    assert open_eid in user_msg
    assert closed_eid not in user_msg


def test_p5_prompt_says_none_without_open_epics(kanban_home):
    with kb.connect() as conn:
        tid = kb.create_task(conn, title="anything", triage=True)
    response = _fake_aux_response(jsonlib.dumps(_EPIC_FANOUT))
    patches = _patch_list_profiles(["orchestrator", "fallback"])
    for p in patches:
        p.start()
    try:
        with patch("agent.auxiliary_client.call_llm", return_value=response) as call_llm:
            outcome = decomp.decompose_task(tid, author="me")
    finally:
        for p in patches:
            p.stop()
    assert outcome.ok, outcome.reason
    user_msg = call_llm.call_args.kwargs["messages"][1]["content"]
    assert "(none)" in user_msg


# ---------------------------------------------------------------------------
# Befund 7 — scope-contract inference hardening: path extraction parity,
# role-scoped tool grants, anti_scope
# ---------------------------------------------------------------------------

def test_collect_absolute_paths_drops_prosa_slash_token():
    """B2.1 hardening (kanban_decompose._collect_absolute_paths delegates to
    kanban_db._absolute_paths_from_text): a single-segment slash token
    scooped out of prose — e.g. the dispatcher's own action check
    'action=="merged"/integration_merged' — is never a real allowed path."""
    body = 'Guard the action=="merged"/integration_merged branch before merging.'
    paths = decomp._collect_absolute_paths(body)
    assert "/integration_merged" not in paths
    assert not any(p.endswith("integration_merged") for p in paths)


def test_collect_absolute_paths_strips_trailing_sentence_punctuation():
    body = "Read /home/piet/vault/00-Canon/vision.md. Then act on it."
    paths = decomp._collect_absolute_paths(body)
    assert "/home/piet/vault/00-Canon/vision.md" in paths
    assert "/home/piet/vault/00-Canon/vision.md." not in paths


def test_collect_absolute_paths_still_handles_tilde_paths():
    """Regression: kb._absolute_paths_from_text only matches a leading '/',
    so '~/' paths must still be recognised by this module's wrapper."""
    body = "CRITICAL: copy artifacts to ~/.hermes/reports/sprint/. Then finish."
    paths = decomp._collect_absolute_paths(body)
    assert "~/.hermes/reports/sprint/" in paths


def test_default_scope_contract_scout_child_has_no_write_tools():
    parent = _task_stub("t_parent", "Recon the area.")
    child = {"title": "recon", "body": "", "assignee": "scout"}
    out = decomp._ensure_worker_scope_contract(child, parent_task=parent)
    body = out["body"]
    _assert_worker_scope_contract(body)
    assert "write_file" not in body
    assert "patch" not in body
    assert "read_file" in body
    assert "search_files" in body
    assert "terminal" in body
    report = decomp.validate_worker_scope_contracts([{**child, "body": body}])
    assert report.ok is True, report.issues


def test_default_scope_contract_coder_child_keeps_write_tools():
    """Regression: the read-only carve-out must not touch code lanes."""
    parent = _task_stub("t_parent", "Write the code.")
    child = {"title": "implement", "body": "", "assignee": "coder"}
    out = decomp._ensure_worker_scope_contract(child, parent_task=parent)
    body = out["body"]
    _assert_worker_scope_contract(body)
    assert "write_file" in body
    assert "patch" in body


def test_validator_rejects_scout_contract_with_write_file():
    children = [{
        "title": "unsafe scout",
        "assignee": "scout",
        "body": (
            "scope_contract:\n"
            "  version: 2\n"
            "  allowed_tools:\n"
            "    - kanban_show\n"
            "    - kanban_complete\n"
            "    - kanban_block\n"
            "    - kanban_comment\n"
            "    - read_file\n"
            "    - write_file\n"
            "completion_policy:\n"
            "  require_scope_attestation: true\n"
        ),
    }]
    report = decomp.validate_worker_scope_contracts(children)
    assert report.ok is False
    assert any("read-only lane" in issue.reason for issue in report.issues)


def test_default_scope_contract_anti_scope_has_static_defaults():
    parent = _task_stub("t_parent", "Build the thing.")
    child = {"title": "build", "body": "", "assignee": "coder"}
    contract = decomp._default_worker_scope_contract(child, parent_task=parent)
    anti_scope = contract["scope_contract"]["anti_scope"]
    for expected in (
        "no unrelated cleanup",
        "no git push",
        "no deploy or runtime restart",
        "no DB schema migration",
    ):
        assert expected in anti_scope


def test_default_scope_contract_anti_scope_includes_parent_negation_line():
    parent = _task_stub(
        "t_parent",
        "NEVER: touch /home/piet/.hermes/config.yaml — read only.",
    )
    child = {"title": "build", "body": "", "assignee": "coder"}
    contract = decomp._default_worker_scope_contract(child, parent_task=parent)
    anti_scope = contract["scope_contract"]["anti_scope"]
    assert any(
        "NEVER: touch /home/piet/.hermes/config.yaml" in line for line in anti_scope
    )
    rendered = decomp._render_scope_contract_yaml(contract)
    assert "anti_scope:" in rendered
    assert "no unrelated cleanup" in rendered


# ---------------------------------------------------------------------------
# Codex review of ad9cbe8f1 — negation-only paths must not leak into
# allowed_paths; LLM-authored contracts must get anti_scope backfilled.
# ---------------------------------------------------------------------------

def test_allowed_paths_excludes_negation_only_path():
    """T1: a path that ONLY appears in a NEVER: line must not be granted in
    allowed_paths, even though the same negation line surfaces in anti_scope."""
    parent = _task_stub(
        "t_parent",
        "NEVER: touch /home/piet/.hermes/config.yaml — read only.",
    )
    child = {"title": "build", "body": "", "assignee": "coder"}
    contract = decomp._default_worker_scope_contract(child, parent_task=parent)
    scope = contract["scope_contract"]
    assert "/home/piet/.hermes/config.yaml" not in scope["allowed_paths"]
    assert any(
        "NEVER: touch /home/piet/.hermes/config.yaml" in line
        for line in scope["anti_scope"]
    )


def test_allowed_paths_keeps_path_mentioned_both_positively_and_negated():
    """T2: a path mentioned BOTH positively and in a NEVER: line stays in
    allowed_paths — the prohibition remains visible via anti_scope."""
    parent = _task_stub(
        "t_parent",
        "Read /home/piet/.hermes/config.yaml for context.\n"
        "NEVER: touch /home/piet/.hermes/config.yaml — read only.",
    )
    child = {"title": "build", "body": "", "assignee": "coder"}
    contract = decomp._default_worker_scope_contract(child, parent_task=parent)
    scope = contract["scope_contract"]
    assert "/home/piet/.hermes/config.yaml" in scope["allowed_paths"]
    assert any(
        "NEVER: touch /home/piet/.hermes/config.yaml" in line
        for line in scope["anti_scope"]
    )


def test_allowed_paths_excludes_negation_only_tilde_path():
    """T3: same rule for ~/-style paths."""
    parent = _task_stub(
        "t_parent",
        "NEVER: touch ~/.hermes/config.yaml — read only.",
    )
    child = {"title": "build", "body": "", "assignee": "coder"}
    contract = decomp._default_worker_scope_contract(child, parent_task=parent)
    scope = contract["scope_contract"]
    assert "~/.hermes/config.yaml" not in scope["allowed_paths"]
    assert any(
        "NEVER: touch ~/.hermes/config.yaml" in line for line in scope["anti_scope"]
    )


def test_normalize_backfills_missing_anti_scope_on_llm_contract():
    """T4a: an LLM-authored scope_contract block without anti_scope gets the
    static defaults + parent negation lines backfilled on normalization."""
    parent = _task_stub(
        "t_parent",
        "NEVER: touch /home/piet/.hermes/config.yaml — read only.",
    )
    child = {
        "title": "build",
        "assignee": "coder",
        "body": (
            "scope_contract:\n"
            "  version: 2\n"
            "  allowed_tools:\n"
            "    - kanban_show\n"
            "    - kanban_complete\n"
            "    - kanban_block\n"
            "    - kanban_comment\n"
            "completion_policy:\n"
            "  require_scope_attestation: true\n"
        ),
    }
    out = decomp._ensure_worker_scope_contract(child, parent_task=parent)
    body = out["body"]
    assert "anti_scope:" in body
    assert "no unrelated cleanup" in body
    assert "NEVER: touch /home/piet/.hermes/config.yaml" in body
    report = decomp.validate_worker_scope_contracts([{**child, "body": body}])
    assert report.ok is True, report.issues


def test_normalize_leaves_existing_anti_scope_untouched():
    """T4b: an LLM-authored contract that already carries anti_scope is left
    exactly as the model wrote it."""
    child = {
        "title": "build",
        "assignee": "coder",
        "body": (
            "scope_contract:\n"
            "  version: 2\n"
            "  allowed_tools:\n"
            "    - kanban_show\n"
            "    - kanban_complete\n"
            "    - kanban_block\n"
            "    - kanban_comment\n"
            "  anti_scope:\n"
            "    - only touch the payment module\n"
            "completion_policy:\n"
            "  require_scope_attestation: true\n"
        ),
    }
    out = decomp._ensure_worker_scope_contract(child)
    body = out["body"]
    assert body.count("anti_scope:") == 1
    assert "only touch the payment module" in body
    assert "no unrelated cleanup" not in body
