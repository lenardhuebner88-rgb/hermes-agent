"""P2 run-history + P3 nightly rotation."""
from __future__ import annotations

import datetime as _dt
import importlib.util
from pathlib import Path

import pytest

from hermes_cli import autoresearch_runs

_ROOT = Path(__file__).resolve().parents[1]


@pytest.fixture(autouse=True)
def _isolate_live_state(tmp_path, monkeypatch):
    """Keep EVERY test off the live proposal store / kanban DB / strategist state.
    ``_proposals_dir()`` resolves CWD-based, so HERMES_HOME alone does NOT isolate
    the backlog — a test running the real reconciler (``main()`` with
    ``_run_reconciler`` unmocked, e.g. test_nightly_main_routes_by_lane) would
    mutate the live 77-proposal store. Regression fence for the 2026-06-22 incident."""
    iso = tmp_path / "_iso"
    (iso / "skill-audit").mkdir(parents=True)
    monkeypatch.setenv("HERMES_HOME", str(iso / ".hermes"))
    monkeypatch.setenv("HERMES_AUTORESEARCH_AUDIT_DIR", str(iso / "skill-audit"))
    monkeypatch.setenv("HERMES_AUTORESEARCH_DIGEST_PATH", str(iso / "digest.json"))
    monkeypatch.setenv("HERMES_AUTORESEARCH_RECONCILE_SUMMARY_PATH", str(iso / "last-reconcile.json"))
    monkeypatch.setenv("HERMES_STRATEGIST_VETOED_PATH", str(iso / "vetoed_levers.json"))


@pytest.fixture()
def audit(tmp_path, monkeypatch):
    monkeypatch.setenv("HERMES_AUTORESEARCH_AUDIT_DIR", str(tmp_path))
    return tmp_path


def test_append_and_read_newest_first(audit):
    autoresearch_runs.append_run(lane="skill", request_id="r1", tokens=100, proposed=2, errors=0, scanned=3)
    autoresearch_runs.append_run(
        lane="code", request_id="r2", tokens=50, proposed=1, errors=1,
        scanned=4, vetoed=2, model="test-model",
    )
    runs = autoresearch_runs.read_runs()
    assert [r["request_id"] for r in runs] == ["r2", "r1"]  # newest first
    assert runs[0]["lane"] == "code" and runs[0]["tokens"] == 50 and runs[0]["errors"] == 1
    assert runs[0]["vetoed"] == 2 and runs[0]["model"] == "test-model"


def test_history_capped_to_30(audit):
    for i in range(35):
        autoresearch_runs.append_run(lane="skill", request_id=f"r{i}", tokens=i)
    runs = autoresearch_runs.read_runs(100)
    assert len(runs) == 30
    assert runs[0]["request_id"] == "r34"  # newest kept
    assert runs[-1]["request_id"] == "r5"  # oldest within the cap


def test_append_run_records_usage_source(audit):
    """Run records carry how the token figure was obtained — estimated
    figures must never masquerade as measured zeros."""
    autoresearch_runs.append_run(lane="skill", request_id="r1", tokens=1200,
                                 usage_source="estimated")
    autoresearch_runs.append_run(lane="code", request_id="r2", tokens=0)
    runs = autoresearch_runs.read_runs()
    assert runs[1]["usage_source"] == "estimated"
    assert runs[0]["usage_source"] == "measured"  # default: real zero stays real


def test_read_tolerates_garbage(audit):
    (audit / "autoresearch-runs.json").write_text("{ not json", encoding="utf-8")
    assert autoresearch_runs.read_runs() == []


def test_invalid_lane_defaults_to_skill(audit):
    autoresearch_runs.append_run(lane="bogus", tokens=1)
    assert autoresearch_runs.read_runs()[0]["lane"] == "skill"


def test_append_is_atomic_no_temp_leftover(audit):
    """append_run writes via tempfile + os.replace → no half-written temp lingers
    and the on-disk file is always complete, valid JSON."""
    import json as _json
    for i in range(5):
        autoresearch_runs.append_run(lane="code", request_id=f"c{i}", tokens=i)
    assert list(audit.glob("autoresearch-runs.json.tmp.*")) == []  # no torn temp left behind
    data = _json.loads((audit / "autoresearch-runs.json").read_text(encoding="utf-8"))
    assert len(data["runs"]) == 5 and data["runs"][0]["request_id"] == "c4"


def _load_nightly():
    spec = importlib.util.spec_from_file_location(
        "autoresearch_nightly_under_test", _ROOT / "scripts" / "autoresearch_nightly.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def test_nightly_lane_parity(monkeypatch):
    mod = _load_nightly()

    class _Stub:
        fixed = None

        @classmethod
        def now(cls, tz=None):
            return cls.fixed

    _Stub.fixed = _dt.datetime(2026, 1, 1, tzinfo=_dt.timezone.utc)  # yday 1 (odd → code)
    monkeypatch.setattr(mod, "datetime", _Stub)
    assert mod._is_code_night() is True
    _Stub.fixed = _dt.datetime(2026, 1, 2, tzinfo=_dt.timezone.utc)  # yday 2 (even → skill)
    assert mod._is_code_night() is False


def test_nightly_code_night_reads_caps_from_validated_config(monkeypatch):
    """AR3 code caps come from the validated lane contract (config.yaml
    overridable), not from constants drifting apart in the script."""
    mod = _load_nightly()
    captured = {}
    import hermes_cli.autoresearch_proposals as arp
    monkeypatch.setattr(arp, "generate_code_weakness_proposals",
                        lambda **k: captured.update(k) or {"created_count": 0, "files_seen": 0})
    assert mod._run_code_night() == 0
    assert captured["max_files"] == 12 and captured["limit"] == 4


def test_nightly_code_night_honors_config_override(monkeypatch):
    mod = _load_nightly()
    captured = {}
    import hermes_cli.autoresearch_proposals as arp
    from hermes_cli.autoresearch_lane_contracts import load_lane_specs
    override = {"autoresearch": {"lanes": {"code": {"budget": {"max_files": 5, "max_proposals": 2}}}}}
    monkeypatch.setattr(mod, "_lane_specs", lambda: load_lane_specs(config=override))
    monkeypatch.setattr(arp, "generate_code_weakness_proposals",
                        lambda **k: captured.update(k) or {"created_count": 0, "files_seen": 0})
    assert mod._run_code_night() == 0
    assert captured["max_files"] == 5 and captured["limit"] == 2


def test_nightly_skill_night_cap_comes_from_config_not_unit_env(monkeypatch):
    """Without the legacy env override the skill lane runs the reduced
    config cap (12), not the historical 50."""
    mod = _load_nightly()
    monkeypatch.delenv("AR_NIGHTLY_ITERATIONS", raising=False)
    captured = {}

    def _fake_create_request(**kwargs):
        captured.update(kwargs)
        return "req.json"

    import autoresearch_request as arr
    monkeypatch.setattr(mod.arr, "create_request", _fake_create_request, raising=False)
    monkeypatch.setattr(mod.runner, "main", lambda argv: 0, raising=False)
    assert mod._run_skill_night() == 0
    assert captured["max_iterations"] == 12
    # legacy env override stays honored (backwards-compatible, unit-removable)
    monkeypatch.setenv("AR_NIGHTLY_ITERATIONS", "7")
    assert mod._run_skill_night() == 0
    assert captured["max_iterations"] == 7


def test_nightly_quota_gate_skips_lane_as_expected_outcome(monkeypatch, capsys):
    """Weekly >= 70%: no lane call is made and the night reports
    quota_skipped as an expected, zero-exit outcome."""
    mod = _load_nightly()
    import hermes_cli.autoresearch_budget as arb
    import hermes_cli.autoresearch_proposals as arp

    def _boom(**_k):
        raise AssertionError("lane must not run when the weekly quota gate is closed")

    monkeypatch.setattr(arp, "generate_code_weakness_proposals", _boom)
    monkeypatch.setattr(mod, "_is_code_night", lambda: True)
    monkeypatch.setattr(
        arb, "fetch_quota_snapshot",
        lambda provider="openai-codex": _usage_snapshot(session=10, weekly=75),
    )
    assert mod.main([]) == 0
    out = capsys.readouterr().out
    assert "quota_skipped" in out


def test_nightly_cooldown_skips_lane_until_operator_override(monkeypatch, capsys, tmp_path):
    """Three healthy zero-yield runs park the lane for 7 days; the operator
    can override explicitly with --ignore-cooldown."""
    mod = _load_nightly()
    import hermes_cli.autoresearch_budget as arb
    import hermes_cli.autoresearch_proposals as arp

    monkeypatch.setattr(mod, "_is_code_night", lambda: True)
    monkeypatch.setattr(
        arb, "fetch_quota_snapshot",
        lambda provider="openai-codex": _usage_snapshot(session=5, weekly=5),
    )
    for _ in range(3):
        arb.record_lane_run_for_cooldown("code", outcome="clean", yielded=0, healthy_calls=4)

    ran = {"n": 0}
    monkeypatch.setattr(
        arp, "generate_code_weakness_proposals",
        lambda **_k: ran.__setitem__("n", ran["n"] + 1) or {"created_count": 0, "files_seen": 1},
    )
    assert mod.main([]) == 0
    assert ran["n"] == 0
    assert "cooldown" in capsys.readouterr().out.lower()

    assert mod.main(["--ignore-cooldown"]) == 0
    assert ran["n"] == 1


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


def test_nightly_code_night_returns_nonzero_on_total_provider_failure(monkeypatch):
    mod = _load_nightly()
    import hermes_cli.autoresearch_proposals as arp

    monkeypatch.setattr(
        arp,
        "generate_code_weakness_proposals",
        lambda **_k: {
            "ok": False,
            "created_count": 0,
            "files_seen": 2,
            "findings_seen": 0,
            "errors": [
                {"target": "a.py", "reason": "AuthenticationError: invalid API key"},
                {"target": "b.py", "reason": "AuthenticationError: invalid API key"},
            ],
        },
    )

    assert mod._run_code_night() == 2


def _open_quota(monkeypatch):
    """Keep main()-flow tests off the live usage API: healthy quota, no gate."""
    import hermes_cli.autoresearch_budget as arb
    monkeypatch.setattr(
        arb, "fetch_quota_snapshot",
        lambda provider="openai-codex": _usage_snapshot(session=5, weekly=5),
    )


def test_nightly_main_routes_by_lane(monkeypatch):
    mod = _load_nightly()
    _open_quota(monkeypatch)
    called = {}
    monkeypatch.setattr(mod, "_run_code_night", lambda *a, **k: (called.__setitem__("lane", "code"), 0)[1])
    monkeypatch.setattr(mod, "_run_skill_night", lambda *a, **k: (called.__setitem__("lane", "skill"), 0)[1])
    monkeypatch.setattr(mod, "_is_code_night", lambda: True)
    assert mod.main() == 0 and called["lane"] == "code"
    monkeypatch.setattr(mod, "_is_code_night", lambda: False)
    assert mod.main() == 0 and called["lane"] == "skill"


def test_nightly_main_runs_reconciler_after_lane(monkeypatch):
    mod = _load_nightly()
    _open_quota(monkeypatch)
    order = []
    monkeypatch.setattr(mod, "_is_code_night", lambda: True)
    monkeypatch.setattr(mod, "_run_code_night", lambda *a, **k: order.append("code") or 0)
    monkeypatch.setattr(mod, "_run_reconciler", lambda: order.append("reconcile") or {"ok": True}, raising=False)

    assert mod.main() == 0
    assert order == ["code", "reconcile"]
