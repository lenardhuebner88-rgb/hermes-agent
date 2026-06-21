"""Tests for the Autoresearch proposal reconciler.

The reconciler is the bridge from passive proposals to the self-improvement
flywheel: skill-doc fixes are applied behind the existing gate, code/test findings
become deduped Kanban work, and risky/no-diff findings become operator decisions.
"""
from __future__ import annotations

import json
from pathlib import Path

import pytest

from hermes_cli import autoresearch_proposals as proposals
from hermes_cli import kanban_db as kb


@pytest.fixture()
def reconcile_env(tmp_path, monkeypatch):
    home = tmp_path / ".hermes"
    audit = home / "skill-audit"
    digest = home / "state" / "strategist" / "autoresearch-digest.json"
    home.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(home))
    monkeypatch.setenv("HERMES_AUTORESEARCH_AUDIT_DIR", str(audit))
    monkeypatch.setenv("HERMES_AUTORESEARCH_DIGEST_PATH", str(digest))
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    kb.init_db()
    return {"home": home, "audit": audit, "digest": digest}


def _proposal(pid: str, **overrides):
    data = {
        "id": pid,
        "schema": proposals.PROPOSAL_SCHEMA,
        "mode": "skill",
        "target": "demo",
        "target_path": "/tmp/demo/SKILL.md",
        "section": "Output",
        "eval_label": "Output / Ergebnis",
        "category": "missing_section",
        "severity": "high",
        "finding_id": pid,
        "subsystem": "skills",
        "theme": "missing-section",
        "title": f"Proposal {pid}",
        "rationale_plain": "test rationale",
        "before_text": "before",
        "after_text": "after",
        "new_text": "new",
        "diff_before_after": "--- a/demo\n+++ b/demo\n@@ -1 +1 @@\n-before\n+after",
        "status": "proposed",
        "created_at": "2026-06-21T00:00:00Z",
        "applied_at": None,
        "result": None,
    }
    data.update(overrides)
    proposals.save_proposal(data)
    return data


def _load(pid: str) -> dict:
    loaded = proposals.load_proposal(pid)
    assert loaded is not None
    return loaded


def test_skill_doc_with_diff_is_sent_through_apply_proposal(reconcile_env, monkeypatch):
    from hermes_cli import autoresearch_reconcile as reconcile

    _proposal("skill-fix")
    applied: list[str] = []

    def fake_apply(pid: str, *, confirm: bool = True, judged: bool = False):
        applied.append(pid)
        prop = _load(pid)
        prop["status"] = "applied"
        prop["last_outcome"] = "applied"
        proposals.save_proposal(prop)
        return {"ok": True, "status": "applied", "id": pid}

    monkeypatch.setattr(reconcile.proposals, "apply_proposal", fake_apply)

    with kb.connect() as conn:
        summary = reconcile.reconcile_proposals(conn=conn)

    assert applied == ["skill-fix"]
    assert summary["applied"] == 1
    assert _load("skill-fix")["status"] == "applied"


def test_reverted_skill_doc_is_escalated_with_signal_key(reconcile_env, monkeypatch):
    from hermes_cli import autoresearch_reconcile as reconcile

    _proposal("skill-reverts", category="silent_except", theme="silent-except")

    def fake_apply(pid: str, *, confirm: bool = True, judged: bool = False):
        prop = _load(pid)
        prop["status"] = "proposed"
        prop["last_outcome"] = "reverted_no_improvement"
        proposals.save_proposal(prop)
        return {"ok": False, "status": "proposed", "id": pid, "reverted": True, "detail": "gate red"}

    monkeypatch.setattr(reconcile.proposals, "apply_proposal", fake_apply)

    with kb.connect() as conn:
        summary = reconcile.reconcile_proposals(conn=conn)
        queue = kb.decision_queue(conn)

    assert summary["escalated"] == 1
    routed = _load("skill-reverts")
    assert routed["status"] == "escalated"
    assert routed["escalation_task_id"]
    assert queue["count"] == 1
    event = queue["decisions"][0]["operator_escalation"]
    assert event["source"] == "autoresearch"
    assert event["signal_key"] == "silent-except"


def test_high_severity_code_findings_create_one_deduped_kanban_task(reconcile_env):
    from hermes_cli import autoresearch_reconcile as reconcile

    _proposal(
        "code-a",
        mode="code",
        finding_id="F-123",
        target="hermes_cli/auth.py",
        target_path="hermes_cli/auth.py",
        title="Auth silent exception",
        category="bug_risk",
        theme="silent-except",
        subsystem="auth",
    )
    _proposal(
        "code-b",
        mode="code",
        finding_id="F-123",
        target="hermes_cli/auth.py",
        target_path="hermes_cli/auth.py",
        title="Duplicate Auth silent exception",
        category="bug_risk",
        theme="silent-except",
        subsystem="auth",
    )

    with kb.connect() as conn:
        summary = reconcile.reconcile_proposals(conn=conn)
        rows = conn.execute("SELECT id, title, assignee, idempotency_key FROM tasks").fetchall()

    assert summary["routed_to_kanban"] == 2
    assert summary["new_tasks"] == 1
    assert len(rows) == 1
    assert rows[0]["assignee"] == "coder"
    assert rows[0]["idempotency_key"] == "autoresearch:F-123"
    assert _load("code-a")["kanban_task_id"] == rows[0]["id"]
    assert _load("code-b")["kanban_task_id"] == rows[0]["id"]




def test_vetoed_autoresearch_signal_is_suppressed(reconcile_env):
    from hermes_cli import autoresearch_reconcile as reconcile

    veto_path = reconcile_env["home"] / "state" / "strategist" / "vetoed_levers.json"
    veto_path.parent.mkdir(parents=True, exist_ok=True)
    veto_path.write_text(json.dumps(["autoresearch:silent-except"]), encoding="utf-8")
    _proposal(
        "code-suppressed",
        mode="code",
        finding_id="F-SUP",
        target="hermes_cli/auth.py",
        target_path="hermes_cli/auth.py",
        title="Suppressed silent exception",
        category="bug_risk",
        theme="silent-except",
        subsystem="auth",
    )

    with kb.connect() as conn:
        summary = reconcile.reconcile_proposals(conn=conn)
        task_count = conn.execute("SELECT COUNT(*) AS n FROM tasks").fetchone()["n"]

    assert summary["suppressed"] == 1
    assert task_count == 0
    routed = _load("code-suppressed")
    assert routed["status"] == "skipped"
    assert "silent-except" in routed["result"]


def test_flood_guard_caps_new_tasks_and_pools_the_rest(reconcile_env):
    from hermes_cli import autoresearch_reconcile as reconcile

    for i in range(60):
        _proposal(
            f"code-{i:02d}",
            mode="code",
            finding_id=f"F-{i:02d}",
            target="hermes_cli/example.py",
            target_path="hermes_cli/example.py",
            title=f"Finding {i}",
            category="bug_risk",
            theme="silent-except",
            subsystem="auth",
        )

    with kb.connect() as conn:
        summary = reconcile.reconcile_proposals(conn=conn, max_new_tasks=5)
        task_count = conn.execute("SELECT COUNT(*) AS n FROM tasks").fetchone()["n"]

    assert task_count == 5
    assert summary["new_tasks"] == 5
    assert summary["pooled"] == 55
    assert len([p for p in proposals.list_proposals() if p.get("status") == "routed_to_kanban"]) == 5
    assert len([p for p in proposals.list_proposals() if p.get("status") == "pooled"]) == 55


def test_diff_less_finding_escalates_and_digest_groups_theme(reconcile_env):
    from hermes_cli import autoresearch_reconcile as reconcile

    _proposal(
        "diff-less",
        mode="skill",
        diff_before_after="",
        new_text="",
        category="silent_except",
        theme="silent-except",
        subsystem="auth",
        severity="medium",
        finding_id="F-diff-less",
    )

    with kb.connect() as conn:
        summary = reconcile.reconcile_proposals(conn=conn)
        queue = kb.decision_queue(conn)

    assert summary["escalated"] == 1
    assert _load("diff-less")["status"] == "escalated"
    assert queue["count"] == 1

    digest = json.loads(reconcile_env["digest"].read_text(encoding="utf-8"))
    assert digest["themes"] == [
        {
            "subsystem": "auth",
            "theme": "silent-except",
            "count": 1,
            "severity_max": "medium",
            "example_finding_ids": ["F-diff-less"],
            "atomic_tasks_filed": 0,
            "escalated": 1,
        }
    ]


def test_once_mode_uses_same_reconciler_without_running_drain(reconcile_env, monkeypatch):
    from hermes_cli import autoresearch_reconcile as reconcile

    calls: list[dict] = []
    monkeypatch.setattr(reconcile, "reconcile_proposals", lambda **kw: calls.append(kw) or {"ok": True})

    rc = reconcile.main(["--once", "--max-new-tasks", "3"])

    assert rc == 0
    assert calls and calls[0]["once"] is True
    assert calls[0]["max_new_tasks"] == 3


def test_veto_operator_escalation_archives_and_records_via_real_path(reconcile_env):
    """The sanctioned veto path archives an Autoresearch escalation AND writes
    the ``freigabe_vetoed`` event that the strategist's reflect joins on —
    closing Naht 3 without a raw event injection."""
    from hermes_cli import autoresearch_reconcile as reconcile

    _proposal(
        "diff-less-veto", mode="skill", diff_before_after="", new_text="",
        category="silent_except", theme="silent-except", subsystem="auth",
        severity="high", finding_id="F-veto",
    )

    with kb.connect() as conn:
        reconcile.reconcile_proposals(conn=conn)
        task_id = kb.decision_queue(conn)["decisions"][0]["task_id"]

        vetoed = kb.veto_operator_escalation(conn, task_id, author="operator")

        assert vetoed is True
        status = conn.execute(
            "SELECT status FROM tasks WHERE id = ?", (task_id,)
        ).fetchone()["status"]
        assert status == "archived"
        veto_events = conn.execute(
            "SELECT COUNT(*) AS n FROM task_events "
            "WHERE task_id = ? AND kind = 'freigabe_vetoed'",
            (task_id,),
        ).fetchone()["n"]
        assert veto_events == 1


def test_veto_operator_escalation_rejects_non_autoresearch_task(reconcile_env):
    """A plain blocked task (no Autoresearch escalation event) must NOT be
    dismissible via this path — a stalled-worker block is not a vetoable signal."""
    with kb.connect() as conn:
        task_id = kb.create_task(
            conn, title="plain blocked", assignee=None,
            created_by="test", initial_status="blocked", kind="ops",
        )

        vetoed = kb.veto_operator_escalation(conn, task_id)

        assert vetoed is False
        status = conn.execute(
            "SELECT status FROM tasks WHERE id = ?", (task_id,)
        ).fetchone()["status"]
        assert status == "blocked"
        n = conn.execute(
            "SELECT COUNT(*) AS n FROM task_events "
            "WHERE task_id = ? AND kind = 'freigabe_vetoed'",
            (task_id,),
        ).fetchone()["n"]
        assert n == 0
