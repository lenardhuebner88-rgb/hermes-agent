"""Unit tests for the OpenClaw (Mission-Control) cross-system dispatch slice.

Covers the LENS-only vertical:
  * ``_parse_openclaw_assignee`` routing (lens -> operation, plain/unknown -> None).
  * The dispatch branch in ``dispatch_once`` (openclaw:lens claims + signs +
    submits; normal assignees still take the local-spawn path).
  * ``poll_openclaw_results`` (a done MC task closes the kanban task; idempotent).

The MC signer module is stubbed via ``sys.modules`` so no real HMAC secret /
network call is exercised. httpx is monkeypatched for the poll-back read.
"""
from __future__ import annotations

import sys
import tempfile
import types

import pytest


@pytest.fixture()
def isolated_kanban_home(monkeypatch):
    """Fresh HERMES_HOME with a clean kanban DB, kanban_db re-imported."""
    test_home = tempfile.mkdtemp(prefix="openclaw_dispatch_test_")
    monkeypatch.setenv("HERMES_HOME", test_home)
    for mod in list(sys.modules.keys()):
        if (
            mod.startswith("hermes_cli")
            or mod.startswith("hermes_state")
            or mod == "hermes_constants"
        ):
            del sys.modules[mod]
    from hermes_cli import kanban_db
    yield kanban_db, test_home


def _fake_spawn(*args, **kwargs):
    """Stand-in for the real worker spawn — records that it ran, returns a PID."""
    _fake_spawn.calls.append((args, kwargs))
    return 4242


def _install_fake_signer(monkeypatch, *, submit_result):
    """Inject a fake mc_mutation_triage_server into sys.modules.

    ``compute_signature`` is a no-op (returns a fixed hex string) and
    ``submit_to_mission_control`` returns ``submit_result`` so the dispatch
    path never touches the real secret or the network.
    """
    fake = types.ModuleType("mc_mutation_triage_server")

    def compute_signature(payload, workflow_id, timestamp):
        return "deadbeef" * 8

    def submit_to_mission_control(envelope):
        # Record the envelope for assertions.
        submit_to_mission_control.last_envelope = envelope
        return submit_result

    submit_to_mission_control.last_envelope = None
    fake.compute_signature = compute_signature
    fake.submit_to_mission_control = submit_to_mission_control
    monkeypatch.setitem(sys.modules, "mc_mutation_triage_server", fake)
    return fake


# ---------------------------------------------------------------------------
# _parse_openclaw_assignee
# ---------------------------------------------------------------------------


def test_parse_openclaw_assignee_lens(isolated_kanban_home):
    kb, _ = isolated_kanban_home
    assert kb._parse_openclaw_assignee("openclaw:lens") == "request_lens_audit"


def test_parse_openclaw_assignee_plain_profile_is_none(isolated_kanban_home):
    kb, _ = isolated_kanban_home
    assert kb._parse_openclaw_assignee("coder") is None


def test_parse_openclaw_assignee_unknown_agent_is_none(isolated_kanban_home):
    kb, _ = isolated_kanban_home
    assert kb._parse_openclaw_assignee("openclaw:bogus") is None


def test_parse_openclaw_assignee_none_and_empty(isolated_kanban_home):
    kb, _ = isolated_kanban_home
    assert kb._parse_openclaw_assignee(None) is None
    assert kb._parse_openclaw_assignee("") is None


# ---------------------------------------------------------------------------
# dispatch branch
# ---------------------------------------------------------------------------


def test_dispatch_openclaw_lens_claims_and_submits(isolated_kanban_home, monkeypatch):
    """openclaw:lens task is claimed, signed, submitted; MC task id persisted
    on the run metadata; task stays running."""
    kb, _home = isolated_kanban_home
    _install_fake_signer(
        monkeypatch,
        submit_result={
            "status": "ok",
            "stage": "mc-accepted",
            "mc_response": {"taskId": "mc-task-123", "workflowId": "wf-xyz"},
        },
    )
    _fake_spawn.calls = []

    with kb.connect_closing() as conn:
        kb.create_board(slug="default", name="Test")
        task_id = kb.create_task(conn, title="audit the memory pipeline", assignee="openclaw:lens")
    with kb.connect_closing() as conn:
        res = kb.dispatch_once(conn, spawn_fn=_fake_spawn, dry_run=False)

    # Routed to the openclaw bucket, NOT a local spawn.
    assert (task_id, "request_lens_audit") in res.openclaw_dispatched
    assert _fake_spawn.calls == []  # the local spawn_fn was never called

    with kb.connect_closing() as conn:
        trow = conn.execute(
            "SELECT status, current_run_id FROM tasks WHERE id = ?", (task_id,)
        ).fetchone()
        assert trow["status"] == "running"
        run_id = trow["current_run_id"]
        assert run_id is not None
        rrow = conn.execute(
            "SELECT metadata FROM task_runs WHERE id = ?", (run_id,)
        ).fetchone()
    import json
    meta = json.loads(rrow["metadata"])
    assert meta["openclaw"]["mc_task_id"] == "mc-task-123"
    assert meta["openclaw"]["operation"] == "request_lens_audit"
    assert meta["openclaw"]["poll_state"] == "submitted"


def test_dispatch_normal_assignee_still_local_spawns(isolated_kanban_home, monkeypatch):
    """A normal Hermes-profile assignee must flow through the local-spawn path
    unchanged (spawn_fn called), never touching the openclaw branch."""
    kb, _home = isolated_kanban_home

    # Force profile_exists -> True for our test assignee so the normal path
    # reaches spawn_fn (otherwise it'd be bucketed skipped_nonspawnable).
    import hermes_cli.profiles as profiles
    monkeypatch.setattr(profiles, "profile_exists", lambda name: name == "coder")

    _fake_spawn.calls = []
    with kb.connect_closing() as conn:
        kb.create_board(slug="default", name="Test")
        task_id = kb.create_task(conn, title="normal task", assignee="coder")
    with kb.connect_closing() as conn:
        res = kb.dispatch_once(conn, spawn_fn=_fake_spawn, dry_run=False)

    assert not res.openclaw_dispatched
    assert any(s[0] == task_id and s[1] == "coder" for s in res.spawned)
    assert len(_fake_spawn.calls) == 1  # local spawn path taken


def test_dispatch_openclaw_rejection_routes_to_spawn_failure(isolated_kanban_home, monkeypatch):
    """An MC rejection must degrade through the spawn-failure path: the task
    is released (not stranded running) and not in openclaw_dispatched."""
    kb, _home = isolated_kanban_home
    _install_fake_signer(
        monkeypatch,
        submit_result={"status": "rejected", "stage": "mc-rejected", "http_status": 422},
    )
    with kb.connect_closing() as conn:
        kb.create_board(slug="default", name="Test")
        task_id = kb.create_task(conn, title="audit", assignee="openclaw:lens")
    with kb.connect_closing() as conn:
        res = kb.dispatch_once(conn, spawn_fn=_fake_spawn, dry_run=False)

    assert (task_id, "request_lens_audit") not in res.openclaw_dispatched
    with kb.connect_closing() as conn:
        trow = conn.execute(
            "SELECT status FROM tasks WHERE id = ?", (task_id,)
        ).fetchone()
    # _record_spawn_failure releases the claim — task is back to ready (or
    # blocked once the failure limit trips). It must NOT be left running.
    assert trow["status"] != "running"


# ---------------------------------------------------------------------------
# per-agent dispatch (atlas / forge / pixel) — same metadata.openclaw block
# ---------------------------------------------------------------------------


def _dispatch_agent_and_get_meta(kb, monkeypatch, *, agent, operation, title, body=None):
    """Create an ``openclaw:<agent>`` task, run dispatch_once with a stubbed
    signer, and return (task_id, run_metadata_dict, last_envelope)."""
    import json

    _install_fake_signer(
        monkeypatch,
        submit_result={
            "status": "ok",
            "stage": "mc-accepted",
            "mc_response": {"taskId": f"mc-{agent}-1", "workflowId": f"wf-{agent}"},
        },
    )
    _fake_spawn.calls = []
    with kb.connect_closing() as conn:
        kb.create_board(slug="default", name="Test")
        task_id = kb.create_task(conn, title=title, body=body, assignee=f"openclaw:{agent}")
    with kb.connect_closing() as conn:
        res = kb.dispatch_once(conn, spawn_fn=_fake_spawn, dry_run=False)

    assert (task_id, operation) in res.openclaw_dispatched
    assert _fake_spawn.calls == []

    with kb.connect_closing() as conn:
        trow = conn.execute(
            "SELECT status, current_run_id FROM tasks WHERE id = ?", (task_id,)
        ).fetchone()
        assert trow["status"] == "running"
        rrow = conn.execute(
            "SELECT metadata FROM task_runs WHERE id = ?", (trow["current_run_id"],)
        ).fetchone()
    meta = json.loads(rrow["metadata"])
    last_env = sys.modules["mc_mutation_triage_server"].submit_to_mission_control.last_envelope
    return task_id, meta, last_env


def test_dispatch_openclaw_atlas(isolated_kanban_home, monkeypatch):
    kb, _ = isolated_kanban_home
    task_id, meta, env = _dispatch_agent_and_get_meta(
        kb, monkeypatch,
        agent="atlas", operation="trigger_atlas_sprint",
        title="run an audit sprint", body="check the cron panel\ncheck the health strip",
    )
    assert meta["openclaw"]["operation"] == "trigger_atlas_sprint"
    assert meta["openclaw"]["mc_task_id"] == "mc-atlas-1"
    assert meta["openclaw"]["poll_state"] == "submitted"
    # gated-mutation envelope built to spec.
    assert env["operation"] == "trigger_atlas_sprint"
    assert env["risk_class"] == "gated-mutation"
    assert env["capability_id"] == "openclaw.atlas.trigger_sprint"
    sc = env["payload"]["scope_contract_v2"]
    assert sc["in_scope"] == ["check the cron panel", "check the health strip"]
    assert sc["out_of_scope"] and sc["termination_conditions"] and sc["evidence_requirements"]


def test_dispatch_openclaw_forge(isolated_kanban_home, monkeypatch):
    kb, _ = isolated_kanban_home
    task_id, meta, env = _dispatch_agent_and_get_meta(
        kb, monkeypatch,
        agent="forge", operation="request_forge_review",
        title="review the dispatch module", body="hermes_cli/kanban_db.py\nhermes_cli/openclaw_dispatch.py",
    )
    assert meta["openclaw"]["operation"] == "request_forge_review"
    assert meta["openclaw"]["mc_task_id"] == "mc-forge-1"
    assert env["operation"] == "request_forge_review"
    assert env["risk_class"] == "safe-read-only"
    assert env["payload"]["review_kind"] == "code-quality"
    assert env["payload"]["target_paths"] == [
        "hermes_cli/kanban_db.py", "hermes_cli/openclaw_dispatch.py",
    ]


def test_dispatch_openclaw_forge_defaults_target_paths(isolated_kanban_home, monkeypatch):
    kb, _ = isolated_kanban_home
    _task_id, _meta, env = _dispatch_agent_and_get_meta(
        kb, monkeypatch,
        agent="forge", operation="request_forge_review",
        title="review everything", body=None,
    )
    assert env["payload"]["target_paths"] == ["."]


def test_dispatch_openclaw_pixel(isolated_kanban_home, monkeypatch):
    kb, _ = isolated_kanban_home
    task_id, meta, env = _dispatch_agent_and_get_meta(
        kb, monkeypatch,
        agent="pixel", operation="request_pixel_ui_qa",
        title="qa the control dashboard", body="please check http://127.0.0.1:9119/control",
    )
    assert meta["openclaw"]["operation"] == "request_pixel_ui_qa"
    assert meta["openclaw"]["mc_task_id"] == "mc-pixel-1"
    assert env["operation"] == "request_pixel_ui_qa"
    assert env["risk_class"] == "operator-lock"
    # operator-lock risk class REQUIRES this literal true in the payload.
    assert env["payload"]["operator_lock_acknowledged"] is True
    assert env["payload"]["target_url"] == "http://127.0.0.1:9119/control"
    assert env["payload"]["qa_kind"] == "layout-check"


# ---------------------------------------------------------------------------
# endpoint validation: pixel without operator_lock_acknowledged -> 400
# ---------------------------------------------------------------------------


def test_endpoint_pixel_requires_operator_lock_ack(isolated_kanban_home):
    """POST /api/openclaw/dispatch with agent=pixel and no operator-lock ack
    must be rejected with HTTP 400 before any task is created."""
    from fastapi import HTTPException
    from hermes_cli.openclaw_dispatch import (
        OpenClawDispatchBody,
        create_openclaw_dispatch,
    )

    body = OpenClawDispatchBody(
        title="qa the dashboard", agent="pixel",
        operator_lock_acknowledged=False,
    )
    with pytest.raises(HTTPException) as exc:
        create_openclaw_dispatch(body)
    assert exc.value.status_code == 400
    assert "operator_lock_acknowledged" in str(exc.value.detail)


def test_endpoint_pixel_with_ack_creates_task(isolated_kanban_home):
    """With the ack, the pixel task IS created (openclaw:pixel assignee)."""
    from hermes_cli.openclaw_dispatch import (
        OpenClawDispatchBody,
        create_openclaw_dispatch,
    )

    kb, _ = isolated_kanban_home
    with kb.connect_closing() as conn:
        kb.create_board(slug="default", name="Test")

    body = OpenClawDispatchBody(
        title="qa the dashboard", agent="pixel",
        operator_lock_acknowledged=True,
    )
    out = create_openclaw_dispatch(body)
    assert out["ok"] is True
    task_id = out["taskId"]
    with kb.connect_closing() as conn:
        row = conn.execute(
            "SELECT assignee FROM tasks WHERE id = ?", (task_id,)
        ).fetchone()
    assert row["assignee"] == "openclaw:pixel"


# ---------------------------------------------------------------------------
# poll_openclaw_results
# ---------------------------------------------------------------------------


class _FakeResp:
    def __init__(self, payload):
        self._payload = payload

    def raise_for_status(self):
        return None

    def json(self):
        return self._payload


def _seed_submitted_openclaw_task(kb):
    """Create an openclaw:lens task already dispatched (running + run metadata
    with poll_state=submitted), returning (task_id, run_id)."""
    import json
    with kb.connect_closing() as conn:
        kb.create_board(slug="default", name="Test")
        task_id = kb.create_task(conn, title="audit", assignee="openclaw:lens")
    with kb.connect_closing() as conn:
        claimed = kb.claim_task(conn, task_id)
        assert claimed is not None
        run_id = claimed.current_run_id
        meta = {
            "openclaw": {
                "mc_task_id": "mc-task-999",
                "workflow_id": "wf-abc",
                "operation": "request_lens_audit",
                "poll_state": "submitted",
                "submitted_at": 1,
            }
        }
        conn.execute(
            "UPDATE task_runs SET metadata = ? WHERE id = ?",
            (json.dumps(meta), run_id),
        )
    return task_id, run_id


def test_poll_openclaw_results_completes_and_is_idempotent(isolated_kanban_home, monkeypatch):
    kb, _home = isolated_kanban_home
    task_id, run_id = _seed_submitted_openclaw_task(kb)

    calls = {"n": 0}

    def fake_get(url, headers=None, timeout=None):
        calls["n"] += 1
        assert "mc-task-999" in url
        return _FakeResp({"status": "done", "resultSummary": "audit clean, no findings"})

    import httpx
    monkeypatch.setattr(httpx, "get", fake_get)

    with kb.connect_closing() as conn:
        kb.poll_openclaw_results(conn, board="default")

    with kb.connect_closing() as conn:
        trow = conn.execute(
            "SELECT status, result FROM tasks WHERE id = ?", (task_id,)
        ).fetchone()
    assert trow["status"] == "done"
    assert trow["result"] == "audit clean, no findings"
    assert calls["n"] == 1

    # Second poll is a no-op: the run is closed (ended_at set), so the
    # ended_at filter excludes it — fake_get must not be hit again.
    with kb.connect_closing() as conn:
        kb.poll_openclaw_results(conn, board="default")
    assert calls["n"] == 1


def test_poll_openclaw_results_blocks_on_failure(isolated_kanban_home, monkeypatch):
    kb, _home = isolated_kanban_home
    task_id, run_id = _seed_submitted_openclaw_task(kb)

    def fake_get(url, headers=None, timeout=None):
        return _FakeResp({"status": "failed", "resultSummary": "lens crashed"})

    import httpx
    monkeypatch.setattr(httpx, "get", fake_get)

    with kb.connect_closing() as conn:
        kb.poll_openclaw_results(conn, board="default")

    with kb.connect_closing() as conn:
        trow = conn.execute(
            "SELECT status FROM tasks WHERE id = ?", (task_id,)
        ).fetchone()
    assert trow["status"] == "blocked"


def test_poll_openclaw_results_inflight_is_noop(isolated_kanban_home, monkeypatch):
    """MC still queued/running -> kanban task stays running, retried next tick."""
    kb, _home = isolated_kanban_home
    task_id, run_id = _seed_submitted_openclaw_task(kb)

    def fake_get(url, headers=None, timeout=None):
        return _FakeResp({"status": "running"})

    import httpx
    monkeypatch.setattr(httpx, "get", fake_get)

    with kb.connect_closing() as conn:
        kb.poll_openclaw_results(conn, board="default")

    with kb.connect_closing() as conn:
        trow = conn.execute(
            "SELECT status FROM tasks WHERE id = ?", (task_id,)
        ).fetchone()
    assert trow["status"] == "running"
