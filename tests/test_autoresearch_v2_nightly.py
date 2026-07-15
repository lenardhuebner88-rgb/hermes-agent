"""Unit tests for the Autoresearch-v2 nightly sweep entrypoint.

The two lanes themselves are already verified end-to-end against a real model; here
we lock down the *orchestration* glue: deterministic day-of-year rotation, honest
summary formatting (zero-yield / skips / per-lane errors), the Discord send contract,
the dry-run (``apply=False``) invariant, and lane-crash isolation.
"""
from __future__ import annotations

import datetime as _dt
import sys
from pathlib import Path

import pytest

_SCRIPTS = Path(__file__).resolve().parents[1] / "scripts"
if str(_SCRIPTS) not in sys.path:
    sys.path.insert(0, str(_SCRIPTS))

import autoresearch_v2_nightly as nightly  # noqa: E402


@pytest.fixture(autouse=True)
def _isolate_live_state(monkeypatch, tmp_path):
    """Keep EVERY nightly test off the live proposal store, kanban DB, and
    strategist state. ``_proposals_dir()`` resolves CWD-based, so HERMES_HOME alone
    does NOT isolate the backlog — a test running the real reconciler (e.g.
    ``nightly.main()`` with ``_run_reconciler`` unmocked) would mutate the live
    77-proposal store. Regression fence for the 2026-06-22 incident."""
    audit = tmp_path / "skill-audit"
    audit.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(tmp_path / ".hermes"))
    monkeypatch.setenv("HERMES_AUTORESEARCH_AUDIT_DIR", str(audit))
    monkeypatch.setenv("HERMES_AUTORESEARCH_DIGEST_PATH", str(tmp_path / "digest.json"))
    monkeypatch.setenv("HERMES_AUTORESEARCH_RECONCILE_SUMMARY_PATH", str(tmp_path / "last-reconcile.json"))
    monkeypatch.setenv("HERMES_STRATEGIST_VETOED_PATH", str(tmp_path / "vetoed_levers.json"))
    # Keep every test off the live subscription usage API: default to a
    # healthy quota; quota-specific tests override this stub themselves.
    import hermes_cli.autoresearch_budget as _arb
    monkeypatch.setattr(
        _arb, "fetch_quota_snapshot",
        lambda provider="openai-codex": _usage_snapshot(session=5, weekly=5),
    )


# ---------------------------------------------------------------- rotation

def test_select_subsystem_wraps_over_all():
    subs = ["a", "b", "c", "d", "e", "f", "g"]
    seen = {nightly.select_subsystem(subs, day) for day in range(1, 8)}
    assert seen == set(subs)  # 7 consecutive days cover every subsystem
    assert nightly.select_subsystem(subs, 7) == nightly.select_subsystem(subs, 0)  # wrap


def test_select_targets_returns_two_unique_rotating():
    targets = ["t0", "t1", "t2", "t3", "t4"]
    for day in range(0, 12):
        picked = nightly.select_targets(targets, day, 2)
        assert len(picked) == 2
        assert len(set(picked)) == 2  # never duplicates within a night
        assert all(t in targets for t in picked)


def test_select_targets_wraps_at_end_of_list():
    targets = ["t0", "t1", "t2", "t3", "t4"]
    # day=4 → start=(4*2)%5=3 → [t3, t4]
    picked = nightly.select_targets(targets, day=4, count=2)
    assert picked == ["t3", "t4"]
    # day=2 → start=(2*2)%5=4 → [t4, t0] (wraps past end)
    picked2 = nightly.select_targets(targets, day=2, count=2)
    assert picked2 == ["t4", "t0"]


def test_select_targets_count_capped_to_list_size():
    targets = ["t0", "t1"]
    picked = nightly.select_targets(targets, day=5, count=9)
    assert sorted(picked) == ["t0", "t1"]  # never more than exist, no dupes


# ---------------------------------------------------------------- summary

def _da(findings=3, tokens=14000, model="MiniMax-M2.7", reason="", subsystem="credentials"):
    return {"subsystem": subsystem, "ok": True, "findings": findings,
            "tokens": tokens, "model": model, "reason": reason}


def _tf(target, tests_kept=0, tokens=0, reason="", survivors=0, model="MiniMax-M2.7"):
    return {"target": target, "ok": tests_kept > 0, "tests_kept": tests_kept,
            "survivors": survivors, "tokens": tokens, "model": model, "reason": reason}


def test_build_summary_happy_path_sums_tokens():
    when = _dt.date(2026, 6, 4)
    tf = [_tf("hermes_cli/kanban.py", tests_kept=1, tokens=88000)]
    msg = nightly.build_summary(when, _da(findings=3, tokens=14000), tf)
    assert "🌙 Autoresearch-v2 Nightly · 2026-06-04" in msg
    assert "Deep-Audit · credentials · 3 Funde · 14k tok · MiniMax-M2.7" in msg
    assert "kanban.py(+1)" in msg
    assert "Σ 102k tok" in msg  # 14k + 88k


def test_build_summary_zero_yield_and_skip_are_honest():
    when = _dt.date(2026, 6, 4)
    da = _da(findings=0, tokens=9000, reason="no files resolved")
    tf = [_tf("hermes_cli/kanban.py", tests_kept=0, tokens=50000,
              reason="target file is not clean in the main checkout")]
    msg = nightly.build_summary(when, da, tf)
    assert "0 Funde" in msg and "(skip:no-files)" in msg
    assert "kanban.py(0, skip:dirty)" in msg


def test_build_summary_renders_lane_errors_without_crashing():
    when = _dt.date(2026, 6, 4)
    da_err = {"subsystem": "kanban", "error": "RuntimeError: boom"}
    msg = nightly.build_summary(when, da_err, None, tf_error="ValueError: nope")
    assert "Deep-Audit · kanban · FEHLER: RuntimeError: boom" in msg
    assert "Test-Foundry · FEHLER: ValueError: nope" in msg
    assert "Σ 0k tok" not in msg  # 0 tokens renders as "0"
    assert "Σ 0 tok" in msg


def test_deep_audit_generic_whole_lane_failure_is_infra_failed():
    outcome = nightly._classify_deep_audit({
        "subsystem": "kanban",
        "ok": False,
        "findings": 0,
        "scanned": 12,
        "errors": 12,
        "reason": "audit protocol ended without a usable result",
    })

    assert outcome.outcome == "infra_failed"


# ---------------------------------------------------------------- discord contract

def test_post_summary_uses_send_message_contract():
    captured = {}

    def fake_sender(payload):
        captured.update(payload)
        return "{}"

    nightly.post_summary("hello world", channel_id="999", sender=fake_sender)
    assert captured["action"] == "send"
    assert captured["target"] == "discord:999"
    assert captured["message"] == "hello world"


# ---------------------------------------------------------------- dry-run invariant + isolation

def test_test_foundry_lane_always_dry_run(monkeypatch):
    seen = []

    def fake_write_request(*, target, max_mutants, apply):
        seen.append({"target": target, "max_mutants": max_mutants, "apply": apply})
        return {"request_path": f"/tmp/{target}.json"}

    def fake_run_request_file(_path):
        return {"ok": True, "tests_kept": 1, "survivors": [1], "tokens": 100, "model": "X", "reason": ""}

    monkeypatch.setattr(nightly.test_foundry, "write_request", fake_write_request)
    monkeypatch.setattr(nightly.test_foundry, "run_request_file", fake_run_request_file)

    nightly.run_test_foundry_lane(["a", "b"], max_mutants=15)
    assert seen and all(item["apply"] is False for item in seen)  # never writes a branch
    assert all(item["max_mutants"] == 15 for item in seen)


def test_test_foundry_lane_skips_remaining_targets_when_budget_exhausted(monkeypatch):
    """A heavy night must degrade gracefully: once the wall-clock budget is
    spent, remaining targets are marked skipped (not run) so the nightly posts a
    partial report instead of overrunning the systemd timeout."""
    seen = []
    clock = {"t": 1000.0}

    def fake_write_request(*, target, max_mutants, apply):
        seen.append(target)
        return {"request_path": f"/tmp/{target}.json"}

    def fake_run_request_file(_path):
        clock["t"] += 100.0  # each target burns 100s of wall-clock
        return {"ok": True, "tests_kept": 1, "survivors": [], "tokens": 10, "model": "X", "reason": ""}

    monkeypatch.setattr(nightly.test_foundry, "write_request", fake_write_request)
    monkeypatch.setattr(nightly.test_foundry, "run_request_file", fake_run_request_file)
    monkeypatch.setattr(nightly.time, "monotonic", lambda: clock["t"])

    out = nightly.run_test_foundry_lane(
        ["a", "b", "c"], max_mutants=15, started=1000.0, budget_seconds=150.0
    )
    # a: elapsed 0 -> run (t->1100); b: elapsed 100 -> run (t->1200); c: elapsed 200 >= 150 -> skip
    assert seen == ["a", "b"]
    assert out[2]["reason"].startswith("skipped")
    assert out[2]["ok"] is False
    assert out[0]["ok"] is True


def test_test_foundry_lane_no_budget_runs_all(monkeypatch):
    """Default (no budget) preserves prior behavior: every target runs."""
    seen = []
    monkeypatch.setattr(
        nightly.test_foundry, "write_request",
        lambda *, target, max_mutants, apply: seen.append(target) or {"request_path": f"/tmp/{target}.json"},
    )
    monkeypatch.setattr(
        nightly.test_foundry, "run_request_file",
        lambda _p: {"ok": True, "tests_kept": 0, "survivors": [], "tokens": 0, "model": "X", "reason": ""},
    )
    nightly.run_test_foundry_lane(["a", "b"], max_mutants=15)
    assert seen == ["a", "b"]


def test_nightly_tests_isolate_the_live_proposal_store():
    """Regression guard for the 2026-06-22 incident: a nightly test ran
    ``nightly.main()`` with the REAL reconciler (``_run_reconciler`` unmocked).
    ``_proposals_dir()`` resolves CWD-based — HERMES_HOME only isolates the kanban
    DB — so the real reconcile mutated the LIVE 77-proposal backlog into a
    half-processed state with dangling task refs. The module-level autouse fixture
    must keep the proposal store pointed at a tmp dir so no test can leak again."""
    import tempfile

    from hermes_cli import autoresearch_proposals as proposals

    pdir = str(proposals._proposals_dir())
    assert pdir.startswith(tempfile.gettempdir()), (
        f"nightly tests resolve to a non-tmp proposal store ({pdir}) — the live "
        "backlog is reachable; the isolation fixture is missing or broken."
    )


def test_main_isolates_a_lane_crash(monkeypatch, capsys):
    def boom_da(*_a, **_k):
        raise RuntimeError("deep-audit exploded")

    def ok_tf(targets, **_k):
        return [_tf(t, tests_kept=1, tokens=1000) for t in targets]

    monkeypatch.setattr(nightly, "run_deep_audit_lane", boom_da)
    monkeypatch.setattr(nightly, "run_test_foundry_lane", ok_tf)

    rc = nightly.main(["--no-send", "--date", "2026-06-04"])
    out = capsys.readouterr().out
    assert rc == 0  # report still produced despite the crash
    assert "Deep-Audit" in out and "FEHLER" in out  # crash surfaced, not swallowed
    assert "Test-Foundry" in out and "(+1)" in out  # other lane still ran


def test_main_returns_nonzero_when_every_selected_lane_is_infra_failed(monkeypatch, capsys):
    monkeypatch.setattr(
        nightly,
        "run_deep_audit_lane",
        lambda *_a, **_k: {
            "subsystem": "credentials", "ok": False, "findings": 0, "tokens": 0,
            "model": None, "reason": "AuthenticationError: invalid API key",
            "scanned": 0, "errors": 1,
        },
    )
    monkeypatch.setattr(
        nightly,
        "run_test_foundry_lane",
        lambda *_a, **_k: [{
            "target": "x.py", "ok": False, "tests_kept": 0, "survivors": 3,
            "tokens": 0, "model": None, "reason": "no validated mutation tests kept",
            "scanned": 3, "errors": 3,
        }],
    )

    rc = nightly.main(["--no-send", "--date", "2026-06-04"])
    out = capsys.readouterr().out
    assert rc == 2
    assert "infra_failed" in out


def test_main_treats_authenticated_zero_yield_as_success(monkeypatch, capsys):
    monkeypatch.setattr(nightly, "run_deep_audit_lane", lambda *_a, **_k: _da(findings=0))
    monkeypatch.setattr(
        nightly,
        "run_test_foundry_lane",
        lambda *_a, **_k: [_tf("x.py", tests_kept=0, survivors=3,
                               reason="no validated mutation tests kept")],
    )

    rc = nightly.main(["--no-send", "--date", "2026-06-04"])
    out = capsys.readouterr().out
    assert rc == 0
    assert "clean" in out


def test_main_runs_reconciler_after_prune_before_summary(monkeypatch):
    order = []

    monkeypatch.setattr(nightly, "run_deep_audit_lane", lambda *_a, **_k: order.append("deep-audit") or _da())
    monkeypatch.setattr(nightly, "run_test_foundry_lane", lambda *_a, **_k: order.append("test-foundry") or [_tf("x.py")])
    monkeypatch.setattr(nightly, "_run_reconciler", lambda: order.append("reconcile") or {"ok": True}, raising=False)
    monkeypatch.setattr(nightly, "_run_shadow_verifier", lambda: order.append("shadow") or {"ok": True}, raising=False)

    def fake_prune():
        order.append("prune")
        return {"auto_skipped": 0, "archived": 0}

    def fake_build_summary(*args, **kwargs):
        order.append("summary")
        return "summary"

    monkeypatch.setattr(nightly._proposals, "prune_proposals", fake_prune, raising=False)
    monkeypatch.setattr(nightly, "build_summary", fake_build_summary)

    assert nightly.main(["--no-send", "--date", "2026-06-04"]) == 0
    assert order == ["deep-audit", "test-foundry", "prune", "reconcile", "shadow", "summary"]



def test_main_wall_clock_budget_skips_remaining_lanes_but_keeps_report(monkeypatch):
    order = []
    ticks = iter([0.0, 0.0, 2.0])

    monkeypatch.setattr(nightly.time, "monotonic", lambda: next(ticks, 2.0))
    monkeypatch.setattr(nightly, "run_deep_audit_lane", lambda *_a, **_k: order.append("deep-audit") or _da())

    def unexpected_tf(*_a, **_k):
        raise AssertionError("test-foundry must not run after the wall-clock budget is exhausted")

    monkeypatch.setattr(nightly, "run_test_foundry_lane", unexpected_tf)

    def fake_prune():
        order.append("prune")
        return {"auto_skipped": 0, "archived": 0}

    monkeypatch.setattr(nightly._proposals, "prune_proposals", fake_prune, raising=False)
    monkeypatch.setattr(nightly, "_run_reconciler", lambda: order.append("reconcile") or {"ok": True}, raising=False)

    def fake_build_summary(*args, **kwargs):
        order.append("summary")
        assert kwargs["tf_error"].startswith("Wall-clock budget exhausted")
        return "summary"

    monkeypatch.setattr(nightly, "build_summary", fake_build_summary)

    assert nightly.main([
        "--no-send",
        "--date", "2026-06-04",
        "--wall-clock-budget-seconds", "1",
    ]) == 0
    assert order == ["deep-audit", "prune", "reconcile", "summary"]


def test_main_circuit_breaker_skips_remaining_lanes_but_keeps_hygiene(monkeypatch):
    order = []

    def boom_da(*_a, **_k):
        order.append("deep-audit")
        raise RuntimeError("deep-audit exploded")

    def unexpected_tf(*_a, **_k):
        raise AssertionError("test-foundry must not run while the circuit breaker is open")

    monkeypatch.setattr(nightly, "run_deep_audit_lane", boom_da)
    monkeypatch.setattr(nightly, "run_test_foundry_lane", unexpected_tf)
    monkeypatch.setattr(nightly._proposals, "prune_proposals", lambda: order.append("prune") or {"auto_skipped": 0, "archived": 0}, raising=False)
    monkeypatch.setattr(nightly, "_run_reconciler", lambda: order.append("reconcile") or {"ok": True}, raising=False)

    def fake_build_summary(_when, da_summary, _tf_summary, *, tf_error=None):
        order.append("summary")
        assert da_summary["error"].startswith("RuntimeError")
        assert tf_error == "Circuit breaker open before Test-Foundry"
        return "summary"

    monkeypatch.setattr(nightly, "build_summary", fake_build_summary)

    assert nightly.main([
        "--no-send",
        "--date", "2026-06-04",
        "--circuit-breaker-threshold", "1",
    ]) == 2  # no lane completed: the nightly must surface a red/nonzero result
    assert order == ["deep-audit", "prune", "reconcile", "summary"]


# ---------------------------------------------------- budget guard wiring


def _usage_snapshot(*, session: float, weekly: float):
    from agent.account_usage import AccountUsageSnapshot, AccountUsageWindow
    return AccountUsageSnapshot(
        provider="openai-codex", source="usage_api",
        fetched_at=_dt.datetime.now(_dt.timezone.utc), plan="Pro",
        windows=(
            AccountUsageWindow(label="Session", used_percent=session, reset_at=None, window_key="session"),
            AccountUsageWindow(label="Weekly", used_percent=weekly, reset_at=None, window_key="weekly"),
        ),
    )


def _healthy_quota(monkeypatch):
    import hermes_cli.autoresearch_budget as arb
    monkeypatch.setattr(
        arb, "fetch_quota_snapshot",
        lambda provider="openai-codex": _usage_snapshot(session=5, weekly=5),
    )


def test_v2_caps_default_from_validated_lane_contracts(monkeypatch):
    """Without CLI overrides the V2 lanes run the reduced config caps
    (deep audit 6 files, foundry 1 target x 6 mutants, 600s wall-clock each),
    not the historical 12/2/15/env constants."""
    _healthy_quota(monkeypatch)
    captured = {}

    def fake_da(subsystem, *, max_files, **kwargs):
        captured["da_max_files"] = max_files
        captured["da_kwargs"] = kwargs
        return _da(findings=0, tokens=100, reason="")

    def fake_tf(targets, *, max_mutants, **kwargs):
        captured["tf_targets"] = list(targets)
        captured["tf_max_mutants"] = max_mutants
        captured["tf_kwargs"] = kwargs
        return [_tf(t, tests_kept=0, tokens=100) for t in targets]

    monkeypatch.setattr(nightly, "run_deep_audit_lane", fake_da)
    monkeypatch.setattr(nightly, "run_test_foundry_lane", fake_tf)

    assert nightly.main(["--no-send", "--date", "2026-06-04"]) == 0
    assert captured["da_max_files"] == 6
    assert len(captured["tf_targets"]) == 1
    assert captured["tf_max_mutants"] == 6


def test_v2_wall_clock_budget_comes_from_config_without_env(monkeypatch):
    """The 600s per-lane wall-clock ceiling lives in the validated config,
    with the legacy env var only as a backwards-compatible override."""
    monkeypatch.delenv("AR_V2_WALL_CLOCK_BUDGET_SECONDS", raising=False)
    _healthy_quota(monkeypatch)
    captured = {}

    monkeypatch.setattr(nightly, "run_deep_audit_lane",
                        lambda subsystem, **k: _da(findings=0, tokens=1, reason=""))

    def fake_tf(targets, *, max_mutants, budget_seconds=0.0, **kwargs):
        captured["tf_budget_seconds"] = budget_seconds
        return [_tf(t) for t in targets]

    monkeypatch.setattr(nightly, "run_test_foundry_lane", fake_tf)
    assert nightly.main(["--no-send", "--date", "2026-06-04"]) == 0
    assert captured["tf_budget_seconds"] == 600


def test_v2_weekly_quota_gate_skips_expensive_lanes(monkeypatch, capsys):
    """Weekly >= 50%: Luna/Terra lanes are quota-skipped (expected outcome,
    exit 0) and make no model call."""
    import hermes_cli.autoresearch_budget as arb
    monkeypatch.setattr(
        arb, "fetch_quota_snapshot",
        lambda provider="openai-codex": _usage_snapshot(session=5, weekly=55),
    )
    monkeypatch.setattr(
        nightly, "_lane_model",
        lambda lane: "gpt-5.6-luna" if lane == "deep-audit" else "gpt-5.4-mini",
        raising=False,
    )

    def boom(*_a, **_k):
        raise AssertionError("expensive lane must not run at weekly >= 50%")

    ran = {"tf": 0}
    monkeypatch.setattr(nightly, "run_deep_audit_lane", boom)
    monkeypatch.setattr(
        nightly, "run_test_foundry_lane",
        lambda targets, **_k: ran.__setitem__("tf", ran["tf"] + 1) or [_tf(t) for t in targets],
    )
    assert nightly.main(["--no-send", "--date", "2026-06-04"]) == 0
    assert ran["tf"] == 1  # mini lane may still run, bounded by the ledger
    assert "quota" in capsys.readouterr().out.lower()


def test_v2_session_stop_blocks_next_lane_in_same_window(monkeypatch, capsys):
    """Session >= 60% after the first lane: the second lane of the same night
    window is stopped (quota_skipped), not started."""
    import hermes_cli.autoresearch_budget as arb
    snapshots = iter([
        _usage_snapshot(session=30, weekly=10),   # before deep-audit
        _usage_snapshot(session=65, weekly=12),   # refreshed before test-foundry
    ])
    monkeypatch.setattr(
        arb, "fetch_quota_snapshot",
        lambda provider="openai-codex": next(snapshots),
    )
    monkeypatch.setattr(nightly, "run_deep_audit_lane",
                        lambda subsystem, **k: _da(findings=0, tokens=500, reason=""))

    def boom_tf(*_a, **_k):
        raise AssertionError("session >= 60% must stop the next lane")

    monkeypatch.setattr(nightly, "run_test_foundry_lane", boom_tf)
    assert nightly.main(["--no-send", "--date", "2026-06-04"]) == 0
    assert "quota" in capsys.readouterr().out.lower()
