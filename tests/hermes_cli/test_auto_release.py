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
        assert deploys == []


# ---------------------------------------------------------------------------
# A2 (S3 chronic-red-refinement): fail-open hook must never break completion,
# and must leave a forensic task_event behind.
# ---------------------------------------------------------------------------

def test_hook_crash_completion_still_succeeds_and_records_forensic_event(
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
            # completion must succeed despite the hook crashing
            assert kb.complete_task(conn, kid, summary="s")
        ev = conn.execute(
            "SELECT payload FROM task_events WHERE kind = 'auto_release_hook_crashed' "
            "ORDER BY id DESC LIMIT 1"
        ).fetchone()
        assert ev is not None
        payload = json.loads(ev["payload"])
        assert "simulated hook crash" in payload["error"]
        assert "chain_root" in payload
        # no auto_release event at all this run — the outcome dict was never
        # produced (the hook crashed before returning one)
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
        assert events == ["deploy"]


import json  # noqa: E402
