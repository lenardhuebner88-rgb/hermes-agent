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

_SCRIPTS = Path(__file__).resolve().parents[1] / "scripts"
if str(_SCRIPTS) not in sys.path:
    sys.path.insert(0, str(_SCRIPTS))

import autoresearch_v2_nightly as nightly  # noqa: E402


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
    return {"subsystem": subsystem, "ok": findings > 0, "findings": findings,
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


def test_main_runs_reconciler_after_prune_before_summary(monkeypatch):
    order = []

    monkeypatch.setattr(nightly, "run_deep_audit_lane", lambda *_a, **_k: order.append("deep-audit") or _da())
    monkeypatch.setattr(nightly, "run_test_foundry_lane", lambda *_a, **_k: order.append("test-foundry") or [_tf("x.py")])
    monkeypatch.setattr(nightly, "_run_reconciler", lambda: order.append("reconcile") or {"ok": True}, raising=False)

    def fake_prune():
        order.append("prune")
        return {"auto_skipped": 0, "archived": 0}

    def fake_build_summary(*args, **kwargs):
        order.append("summary")
        return "summary"

    monkeypatch.setattr(nightly._proposals, "prune_proposals", fake_prune, raising=False)
    monkeypatch.setattr(nightly, "build_summary", fake_build_summary)

    assert nightly.main(["--no-send", "--date", "2026-06-04"]) == 0
    assert order == ["deep-audit", "test-foundry", "prune", "reconcile", "summary"]



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
    ]) == 0
    assert order == ["deep-audit", "prune", "reconcile", "summary"]
