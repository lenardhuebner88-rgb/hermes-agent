"""Subsystem C — autonomous release + rollback (Plan→Board pipeline, 2026-07-05).

Unit tests ALWAYS mock deploy/rollback/HTTP — never touch the live service.
"""

from __future__ import annotations

import pytest

from hermes_cli import auto_release


# ---------------------------------------------------------------------------
# C2: run_live_test — depth executor
# ---------------------------------------------------------------------------


def test_smoke_depth_calls_health():
    """smoke = health/status payload check (truth = API payload, not screenshot)."""
    calls = []

    def fake_fetch(path, timeout=8.0):
        calls.append(path)
        return {"version": "0.18.0", "gateway_running": True, "config_version": 3}

    res = auto_release.run_live_test("smoke", fetch=fake_fetch)
    assert res.passed is True
    assert res.held is False
    assert "/api/status" in calls


def test_smoke_depth_fails_on_bad_payload():
    def fake_fetch(path, timeout=8.0):
        return {"not": "a status payload"}

    res = auto_release.run_live_test("smoke", fetch=fake_fetch)
    assert res.passed is False
    assert res.held is False


def test_smoke_depth_fails_on_fetch_error():
    def fake_fetch(path, timeout=8.0):
        raise OSError("connection refused")

    res = auto_release.run_live_test("smoke", fetch=fake_fetch)
    assert res.passed is False


def test_contract_depth_asserts_expected_payload():
    def fake_fetch(path, timeout=8.0):
        return {"version": "0.18.0", "gateway_running": False, "config_version": 3}

    res = auto_release.run_live_test(
        "contract",
        fetch=fake_fetch,
        contract={"path": "/api/status", "expect": {"gateway_running": True}},
    )
    assert res.passed is False
    assert "gateway_running" in res.detail


def test_contract_depth_passes_on_match():
    def fake_fetch(path, timeout=8.0):
        return {"version": "0.18.0", "gateway_running": True}

    res = auto_release.run_live_test(
        "contract",
        fetch=fake_fetch,
        contract={"path": "/api/status", "expect": {"gateway_running": True}},
    )
    assert res.passed is True


def test_ui_real_depth_always_held():
    """ui-real stays operator-gated — never autonomous."""

    def fake_fetch(path, timeout=8.0):  # pragma: no cover — must not be called
        raise AssertionError("ui-real must not probe anything")

    res = auto_release.run_live_test("ui-real", fetch=fake_fetch)
    assert res.held is True
    assert res.passed is False


def test_empty_depth_trivially_passes():
    res = auto_release.run_live_test("", fetch=lambda p, timeout=8.0: {})
    assert res.passed is True
    assert res.detail == "no live test configured"


# ---------------------------------------------------------------------------
# C3: release orchestrator + kill-switch + chain-tip hook
# ---------------------------------------------------------------------------

from pathlib import Path  # noqa: E402

import hermes_cli.profiles as profiles_mod  # noqa: E402
from hermes_cli import kanban_db as kb  # noqa: E402
from hermes_cli import kanban_closeout as closeout  # noqa: E402
from hermes_cli import vision_metrics as vm  # noqa: E402


def _write_profile(home: Path, name: str) -> None:
    d = home / "profiles" / name
    d.mkdir(parents=True, exist_ok=True)
    (d / "config.yaml").write_text("model: {}\n")


@pytest.fixture
def kanban_home(tmp_path, monkeypatch):
    home = tmp_path / ".hermes"
    home.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(home))
    monkeypatch.setenv("HERMES_AUTO_RECEIPT_DIR", str(tmp_path / "receipts"))
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    for name in ["coder", "verifier"]:
        _write_profile(home, name)
    kb.init_db()
    return home


def _mk_chain(conn, n=2, tier=None, freigabe="complete", depth="smoke"):
    """PlanSpec-shaped chain: root carries freigabe/live_test_depth; children
    carry planspec_source and link to the root via the sink convention
    (task_links parent=chain task, child=root)."""
    root = kb.create_task(
        conn,
        title="PlanSpec T: auto-release chain",
        freigabe=freigabe,
        live_test_depth=depth,
    )
    kids = []
    for i in range(n):
        kid = kb.create_task(
            conn, title=f"slice {i + 1}", assignee="coder", review_tier=tier
        )
        conn.execute(
            "UPDATE tasks SET planspec_source = ? WHERE id = ?", ("/tmp/ar.md", kid)
        )
        kb.link_tasks(conn, kid, root)
        kids.append(kid)
    conn.commit()
    return root, kids


def _green_config():
    return {"autonomous": True, "max_tier_autonomous": "review"}


def _drain_closeouts(conn):
    return closeout.closeout_sweep(conn, limit=100)


def test_completion_atomically_enqueues_closeout_without_inline_release(
    kanban_home, monkeypatch
):
    release_calls = []
    monkeypatch.setattr(
        auto_release,
        "maybe_auto_release",
        lambda *_args, **_kwargs: release_calls.append("release"),
    )
    with kb.connect() as conn:
        task_id = kb.create_task(conn, title="atomic closeout", assignee="coder")
        kb.claim_task(conn, task_id)
        assert kb.complete_task(conn, task_id, summary="done")
        events = kb.list_events(conn, task_id)
        kinds = [event.kind for event in events]
        assert kinds.index(closeout.CLOSEOUT_PENDING) < kinds.index("completed")
        assert release_calls == []


def test_closeout_enqueue_failure_rolls_back_terminal_transition(
    kanban_home, monkeypatch
):
    with kb.connect() as conn:
        task_id = kb.create_task(conn, title="rollback closeout", assignee="coder")
        kb.claim_task(conn, task_id)

        def fail_enqueue(*_args, **_kwargs):
            raise OSError("outbox unavailable")

        monkeypatch.setattr(
            closeout, "enqueue_closeout_pending_in_txn", fail_enqueue
        )
        with pytest.raises(OSError, match="outbox unavailable"):
            kb.complete_task(conn, task_id, summary="must roll back")

        assert kb.get_task(conn, task_id).status == "running"
        kinds = [event.kind for event in kb.list_events(conn, task_id)]
        assert "completed" not in kinds
        assert closeout.CLOSEOUT_PENDING not in kinds


def test_autonomous_off_by_default(kanban_home, monkeypatch):
    """Kill-switch default false → chain-tip completion does NOT deploy."""
    deploys = []
    monkeypatch.setattr(
        auto_release, "_default_deploy", lambda: deploys.append(1) or (True, "")
    )
    monkeypatch.setattr(profiles_mod, "profile_exists", lambda name: True)
    with kb.connect() as conn:
        _root, kids = _mk_chain(conn)
        for kid in kids:
            kb.claim_task(conn, kid)
            assert kb.complete_task(conn, kid, summary="s")
        _drain_closeouts(conn)
        assert deploys == []


def test_green_chain_deploys_and_verifies(kanban_home, monkeypatch):
    """Autonomous on, review tier, smoke green → deploy runs, health re-checked."""
    events = []
    monkeypatch.setattr(auto_release, "_release_config", _green_config)
    monkeypatch.setattr(
        auto_release, "_default_deploy", lambda: events.append("deploy") or (True, "ok")
    )
    monkeypatch.setattr(
        auto_release,
        "_default_rollback",
        lambda: events.append("rollback") or (True, ""),
    )
    monkeypatch.setattr(auto_release, "_default_notify", lambda msg: None)
    fetches = []

    def fake_fetch(path, timeout=8.0):
        fetches.append(path)
        return {"version": "0.18.0", "gateway_running": True}

    monkeypatch.setattr(auto_release, "_default_fetch", fake_fetch)
    with kb.connect() as conn:
        _root, kids = _mk_chain(conn, tier="review")
        for kid in kids:
            kb.claim_task(conn, kid)
            assert kb.complete_task(conn, kid, summary="s")
        _drain_closeouts(conn)
        assert events == ["deploy"]
        # pre-deploy live test + post-deploy re-check both hit the payload
        assert fetches.count("/api/status") >= 2
        ev = conn.execute(
            "SELECT payload FROM task_events WHERE kind = 'auto_release' "
            "ORDER BY id DESC LIMIT 1"
        ).fetchone()
        assert ev is not None
        assert json.loads(ev["payload"])["outcome"] == "deployed"


def test_live_failure_triggers_rollback():
    """Post-deploy smoke fails → rollback invoked + operator notified."""
    events = []
    payloads = iter(
        [
            {"version": "0.18.0"},  # pre-deploy smoke: green
            OSError("dead after deploy"),  # post-deploy: red
        ]
    )

    def fake_fetch(path, timeout=8.0):
        item = next(payloads)
        if isinstance(item, Exception):
            raise item
        return item

    outcome = auto_release.release_chain(
        depth="smoke",
        config=_green_config(),
        deploy=lambda: events.append("deploy") or (True, "ok"),
        rollback=lambda: events.append("rollback") or (True, "rolled back"),
        notify=lambda msg: events.append(f"notify:{msg[:20]}"),
        fetch=fake_fetch,
    )
    assert outcome["outcome"] == "rolled_back"
    assert "deploy" in events and "rollback" in events
    assert any(e.startswith("notify:") for e in events)


def test_critical_tier_never_autonomous(kanban_home, monkeypatch):
    """critical chain never auto-deploys regardless of kill-switch."""
    deploys = []
    monkeypatch.setattr(auto_release, "_release_config", _green_config)
    monkeypatch.setattr(
        auto_release, "_default_deploy", lambda: deploys.append(1) or (True, "")
    )
    monkeypatch.setattr(
        auto_release, "_default_fetch", lambda p, timeout=8.0: {"version": "1"}
    )
    with kb.connect() as conn:
        _root, kids = _mk_chain(conn, tier="critical")
        for kid in kids:
            kb.claim_task(conn, kid)
            assert kb.complete_task(conn, kid, summary="s")
        _drain_closeouts(conn)
        assert deploys == []
        ev = conn.execute(
            "SELECT payload FROM task_events WHERE kind = 'auto_release' "
            "ORDER BY id DESC LIMIT 1"
        ).fetchone()
        assert ev is not None
        assert json.loads(ev["payload"])["outcome"] == "held_critical"


def test_ui_real_chain_held(kanban_home, monkeypatch):
    deploys = []
    monkeypatch.setattr(auto_release, "_release_config", _green_config)
    monkeypatch.setattr(
        auto_release, "_default_deploy", lambda: deploys.append(1) or (True, "")
    )
    with kb.connect() as conn:
        _root, kids = _mk_chain(conn, tier="review", depth="ui-real")
        for kid in kids:
            kb.claim_task(conn, kid)
            assert kb.complete_task(conn, kid, summary="s")
        _drain_closeouts(conn)
        assert deploys == []


def test_incomplete_chain_does_not_deploy(kanban_home, monkeypatch):
    """Hook fires per completion but only the LAST open slice releases."""
    deploys = []
    monkeypatch.setattr(auto_release, "_release_config", _green_config)
    monkeypatch.setattr(
        auto_release, "_default_deploy", lambda: deploys.append(1) or (True, "")
    )
    monkeypatch.setattr(
        auto_release, "_default_fetch", lambda p, timeout=8.0: {"version": "1"}
    )
    with kb.connect() as conn:
        _root, kids = _mk_chain(conn, tier="review", n=3)
        kb.claim_task(conn, kids[0])
        assert kb.complete_task(conn, kids[0], summary="s")
        _drain_closeouts(conn)
        assert deploys == []


# ---------------------------------------------------------------------------
# A2: a release-runner crash must never undo completion and must become a
# durable, non-redeployable ambiguous closeout state.
# ---------------------------------------------------------------------------

def test_release_crash_completion_stays_done_and_records_ambiguous_closeout(
    kanban_home, monkeypatch
):
    monkeypatch.setattr(auto_release, "_release_config", _green_config)

    def _boom(conn, task_id):
        raise RuntimeError("simulated hook crash")

    monkeypatch.setattr(auto_release, "maybe_auto_release", _boom)
    with kb.connect() as conn:
        _root, kids = _mk_chain(conn, tier="review")
        for kid in kids:
            kb.claim_task(conn, kid)
            assert kb.complete_task(conn, kid, summary="s")
        results = _drain_closeouts(conn)
        assert any(result.state == "ambiguous" for result in results)
        ev = conn.execute(
            "SELECT payload FROM task_events WHERE kind = 'closeout_release_ambiguous' "
            "ORDER BY id DESC LIMIT 1"
        ).fetchone()
        assert ev is not None
        payload = json.loads(ev["payload"])
        assert "simulated hook crash" in payload["error"]
        no_ar = conn.execute(
            "SELECT COUNT(*) AS n FROM task_events WHERE kind = 'auto_release'"
        ).fetchone()
        assert no_ar["n"] == 0


# ---------------------------------------------------------------------------
# release.pause_on_red_streak (S3)
# ---------------------------------------------------------------------------

def _config_with_pause(n):
    def _cfg():
        cfg = dict(_green_config())
        cfg["pause_on_red_streak"] = n
        return cfg

    return _cfg


def test_pause_on_red_streak_default_off_is_unchanged(kanban_home, monkeypatch):
    """Regression: no pause_on_red_streak key at all -> deploy runs exactly
    as before this slice (default 0 = off)."""
    events = []
    monkeypatch.setattr(auto_release, "_release_config", _green_config)
    monkeypatch.setattr(
        auto_release, "_default_deploy", lambda: events.append("deploy") or (True, "ok")
    )
    monkeypatch.setattr(
        auto_release, "_default_rollback", lambda: events.append("rollback") or (True, "")
    )
    monkeypatch.setattr(auto_release, "_default_notify", lambda msg: None)
    monkeypatch.setattr(
        auto_release,
        "_default_fetch",
        lambda p, timeout=8.0: {"version": "1", "gateway_running": True},
    )
    with kb.connect() as conn:
        _root, kids = _mk_chain(conn, tier="review")
        for kid in kids:
            kb.claim_task(conn, kid)
            assert kb.complete_task(conn, kid, summary="s")
        _drain_closeouts(conn)
        assert events == ["deploy"]


def test_pause_on_red_streak_holds_when_last_n_nights_all_red(kanban_home, monkeypatch):
    monkeypatch.setattr(auto_release, "_release_config", _config_with_pause(3))
    deploys = []
    monkeypatch.setattr(
        auto_release, "_default_deploy", lambda: deploys.append(1) or (True, "ok")
    )
    monkeypatch.setattr(
        vm,
        "read_gate_records",
        lambda: [
            {"date": "2026-07-03", "result": "fail"},
            {"date": "2026-07-04", "result": "fail"},
            {"date": "2026-07-05", "result": "fail"},
        ],
    )
    with kb.connect() as conn:
        _root, kids = _mk_chain(conn, tier="review")
        for kid in kids:
            kb.claim_task(conn, kid)
            assert kb.complete_task(conn, kid, summary="s")
        _drain_closeouts(conn)
        assert deploys == []
        ev = conn.execute(
            "SELECT payload FROM task_events WHERE kind = 'auto_release' "
            "ORDER BY id DESC LIMIT 1"
        ).fetchone()
        assert ev is not None
        assert json.loads(ev["payload"])["outcome"] == "held_red_gate"


def test_pause_on_red_streak_releases_when_fewer_than_n_red(kanban_home, monkeypatch):
    """N=3 but only 2 consecutive red nights at the head -> release runs."""
    monkeypatch.setattr(auto_release, "_release_config", _config_with_pause(3))
    events = []
    monkeypatch.setattr(
        auto_release, "_default_deploy", lambda: events.append("deploy") or (True, "ok")
    )
    monkeypatch.setattr(
        auto_release, "_default_rollback", lambda: events.append("rollback") or (True, "")
    )
    monkeypatch.setattr(auto_release, "_default_notify", lambda msg: None)
    monkeypatch.setattr(
        auto_release,
        "_default_fetch",
        lambda p, timeout=8.0: {"version": "1", "gateway_running": True},
    )
    monkeypatch.setattr(
        vm,
        "read_gate_records",
        lambda: [
            {"date": "2026-07-03", "result": "pass"},
            {"date": "2026-07-04", "result": "fail"},
            {"date": "2026-07-05", "result": "fail"},
        ],
    )
    with kb.connect() as conn:
        _root, kids = _mk_chain(conn, tier="review")
        for kid in kids:
            kb.claim_task(conn, kid)
            assert kb.complete_task(conn, kid, summary="s")
        _drain_closeouts(conn)
        assert events == ["deploy"]


# ---------------------------------------------------------------------------
# AD-S2: evaluate_ad_hoc_release_guards — the guard ceiling reused by the
# release-gate auto-execution hook. Same guards as maybe_auto_release, exercised
# against real task rows / task_events, PLUS the AD-S1 effective_ui_impact gate.
# ---------------------------------------------------------------------------

def _chain_ids(root, kids):
    return set(kids) | {root}


def test_ad_hoc_guards_green_chain_auto_executes(kanban_home, monkeypatch):
    monkeypatch.setattr(auto_release, "_release_config", _green_config)
    with kb.connect() as conn:
        root, kids = _mk_chain(conn, tier="review")
        decision = auto_release.evaluate_ad_hoc_release_guards(
            conn, root_id=root, chain_ids=_chain_ids(root, kids),
        )
    assert decision == {"outcome": "auto_execute"}


def test_ad_hoc_guards_kill_switch_off_holds(kanban_home, monkeypatch):
    """release.autonomous false (the default) → held_kill_switch, never spawns."""
    # no _release_config monkeypatch: the tmp home has no config.yaml, so the
    # real resolver returns autonomous=False (byte-exact today's off state).
    with kb.connect() as conn:
        root, kids = _mk_chain(conn, tier="review")
        decision = auto_release.evaluate_ad_hoc_release_guards(
            conn, root_id=root, chain_ids=_chain_ids(root, kids),
        )
    assert decision == {"outcome": "held_kill_switch"}


def test_ad_hoc_guards_critical_tier_holds(kanban_home, monkeypatch):
    monkeypatch.setattr(auto_release, "_release_config", _green_config)
    with kb.connect() as conn:
        root, kids = _mk_chain(conn, tier="critical")
        decision = auto_release.evaluate_ad_hoc_release_guards(
            conn, root_id=root, chain_ids=_chain_ids(root, kids),
        )
    assert decision["outcome"] == "held_critical"
    assert "critical" in decision["detail"]


def test_ad_hoc_guards_operator_freigabe_holds(kanban_home, monkeypatch):
    monkeypatch.setattr(auto_release, "_release_config", _green_config)
    with kb.connect() as conn:
        root, kids = _mk_chain(conn, tier="review", freigabe="operator")
        decision = auto_release.evaluate_ad_hoc_release_guards(
            conn, root_id=root, chain_ids=_chain_ids(root, kids),
        )
    assert decision["outcome"] == "held_no_freigabe"
    assert "operator" in decision["detail"]


def test_ad_hoc_guards_ui_redesign_member_holds(kanban_home, monkeypatch):
    """A single redesign slice in the chain pins the whole chain (AD-S1 gate)."""
    monkeypatch.setattr(auto_release, "_release_config", _green_config)
    with kb.connect() as conn:
        root, kids = _mk_chain(conn, tier="review")
        assert kb.set_task_ui_impact(conn, kids[0], "redesign")
        decision = auto_release.evaluate_ad_hoc_release_guards(
            conn, root_id=root, chain_ids=_chain_ids(root, kids),
        )
    assert decision["outcome"] == "held_ui_redesign"
    assert kids[0] in decision["detail"]


def test_ad_hoc_guards_minor_ui_impact_still_auto_executes(kanban_home, monkeypatch):
    """none/minor stay autonom-capable — only redesign gates."""
    monkeypatch.setattr(auto_release, "_release_config", _green_config)
    with kb.connect() as conn:
        root, kids = _mk_chain(conn, tier="review")
        assert kb.set_task_ui_impact(conn, kids[0], "minor")
        decision = auto_release.evaluate_ad_hoc_release_guards(
            conn, root_id=root, chain_ids=_chain_ids(root, kids),
        )
    assert decision == {"outcome": "auto_execute"}


import json  # noqa: E402
