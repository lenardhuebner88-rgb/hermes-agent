"""Autoresearch nightly budget guard: shared daily ledger, subscription guard,
honest usage accounting and ROI cooldown (plan 2026-07-10 ARB-S2).

Written red-first: every test here pins a budget behaviour the nightlies were
missing (no quota stops, measured-zero token records, env/constant caps).
"""
from __future__ import annotations

import datetime as _dt
import json
from pathlib import Path

import pytest

from agent.account_usage import AccountUsageSnapshot, AccountUsageWindow
from agent.usage_pricing import CanonicalUsage
from hermes_cli import autoresearch_budget as budget


@pytest.fixture(autouse=True)
def _isolate_audit_dir(tmp_path, monkeypatch):
    monkeypatch.setenv("HERMES_AUTORESEARCH_AUDIT_DIR", str(tmp_path / "skill-audit"))
    monkeypatch.setenv("HERMES_HOME", str(tmp_path / ".hermes"))


def _snapshot(session: float | None = None, weekly: float | None = None,
              *, unavailable: str | None = None) -> AccountUsageSnapshot:
    """Realistic snapshot in the exact shape fetch_account_usage returns live."""
    windows = []
    if session is not None:
        windows.append(AccountUsageWindow(
            label="Session", used_percent=session, reset_at=None, window_key="session"))
    if weekly is not None:
        windows.append(AccountUsageWindow(
            label="Weekly", used_percent=weekly, reset_at=None, window_key="weekly"))
    return AccountUsageSnapshot(
        provider="openai-codex", source="usage_api",
        fetched_at=_dt.datetime.now(_dt.timezone.utc), plan="Pro",
        windows=tuple(windows), unavailable_reason=unavailable,
    )


def _clock(iso: str):
    when = _dt.datetime.fromisoformat(iso)
    return lambda: when


# ------------------------------------------------------------- config


def test_budget_config_defaults_match_plan():
    cfg = budget.load_budget_config({})
    assert cfg.timezone == "Europe/Berlin"
    assert cfg.daily_token_limit == 100_000
    assert cfg.daily_model_call_limit == 30
    assert cfg.weekly_expensive_skip_percent == 50
    assert cfg.weekly_all_skip_percent == 70
    assert cfg.session_stop_percent == 60
    assert cfg.unknown_usage_policy == "mini_only"


def test_budget_config_reads_config_yaml_values_not_env(monkeypatch):
    """All non-secret budget values come from config.yaml; no new behaviour env var."""
    monkeypatch.setenv("AR_DAILY_TOKEN_LIMIT", "5")  # must be ignored (no such lever)
    cfg = budget.load_budget_config({
        "autoresearch": {"budget": {"daily_token_limit": 44_000, "daily_model_call_limit": 9}},
    })
    assert cfg.daily_token_limit == 44_000
    assert cfg.daily_model_call_limit == 9


def test_lane_budget_value_reads_validated_config():
    config = {"autoresearch": {"lanes": {"code": {"budget": {"max_files": 3}}}}}
    assert budget.lane_budget_value(config, "code", "max_files", 12) == 3
    assert budget.lane_budget_value({}, "code", "max_files", 12) == 12
    # garbage values fall back instead of crashing the nightly
    bad = {"autoresearch": {"lanes": {"code": {"budget": {"max_files": "many"}}}}}
    assert budget.lane_budget_value(bad, "code", "max_files", 12) == 12


# ------------------------------------------------------------- expensive models


def test_expensive_model_markers():
    assert budget.is_expensive_model("gpt-5.6-luna")
    assert budget.is_expensive_model("gpt-5.6-terra")
    assert budget.is_expensive_model("gpt-5.6-sol-pro")
    assert not budget.is_expensive_model("gpt-5.4-mini")
    assert not budget.is_expensive_model(None)


# ------------------------------------------------------------- subscription guard


def test_weekly_50_blocks_expensive_but_allows_bounded_mini_lane():
    decision = budget.evaluate_quota(_snapshot(session=10, weekly=50), budget.load_budget_config({}))
    assert decision.allow_expensive is False
    assert decision.allow_any is True
    assert budget.quota_block_reason(decision, "gpt-5.6-terra")
    assert budget.quota_block_reason(decision, "gpt-5.6-luna")
    assert budget.quota_block_reason(decision, "gpt-5.4-mini") is None


def test_weekly_70_blocks_all_lanes():
    decision = budget.evaluate_quota(_snapshot(session=10, weekly=70), budget.load_budget_config({}))
    assert decision.allow_any is False
    reason = budget.quota_block_reason(decision, "gpt-5.4-mini")
    assert reason and "quota skip" in reason


def test_session_60_stops_next_lane_in_same_window():
    decision = budget.evaluate_quota(_snapshot(session=60, weekly=10), budget.load_budget_config({}))
    assert decision.stop_session is True
    reason = budget.quota_block_reason(decision, "gpt-5.4-mini")
    assert reason and "quota skip" in reason


def test_unknown_usage_api_fails_closed_for_expensive_mini_only_under_ledger():
    cfg = budget.load_budget_config({})
    for snapshot in (None, _snapshot(unavailable="auth expired")):
        decision = budget.evaluate_quota(snapshot, cfg)
        assert decision.allow_expensive is False
        assert decision.allow_any is True  # mini_only policy
        assert decision.source == "unknown"
        assert budget.quota_block_reason(decision, "gpt-5.6-luna")
        assert budget.quota_block_reason(decision, "gpt-5.4-mini") is None


def test_quota_block_reason_is_expected_outcome_not_infra_error():
    from hermes_cli.autoresearch_lane_contracts import classify_lane_outcome, nightly_exit_code

    decision = budget.evaluate_quota(_snapshot(session=5, weekly=75), budget.load_budget_config({}))
    reason = budget.quota_block_reason(decision, "gpt-5.4-mini")
    outcome = classify_lane_outcome("code", scanned=0, errors=0, yielded=0, ok=True, reason=reason)
    assert outcome.outcome == "quota_skipped"
    assert outcome.fatal is False
    assert nightly_exit_code([outcome]) == 0


# ------------------------------------------------------------- daily ledger


def test_ledger_stops_before_call_31(tmp_path):
    led = budget.DailyLedger(tmp_path / "ledger.json", config=budget.load_budget_config({}))
    for _ in range(30):
        led.check_call(10)
        led.record_call(lane="skill", model="gpt-5.4-mini", estimated_tokens=10,
                        usage=CanonicalUsage(input_tokens=5, output_tokens=5))
    assert led.calls_today() == 30
    with pytest.raises(budget.BudgetExhausted):
        led.check_call(10)


def test_ledger_stops_before_exceeding_100k_tokens(tmp_path):
    led = budget.DailyLedger(tmp_path / "ledger.json", config=budget.load_budget_config({}))
    led.record_call(lane="deep-audit", model="gpt-5.6-luna", estimated_tokens=99_500,
                    usage=CanonicalUsage(input_tokens=90_000, output_tokens=9_500))
    led.check_call(400)  # 99_900 ≤ 100_000 still fine
    with pytest.raises(budget.BudgetExhausted):
        led.check_call(600)  # would cross 100_000


def test_ledger_is_shared_via_audit_dir_and_persists_atomically(tmp_path, monkeypatch):
    monkeypatch.setenv("HERMES_AUTORESEARCH_AUDIT_DIR", str(tmp_path))
    first = budget.DailyLedger(config=budget.load_budget_config({}))
    first.record_call(lane="skill", model="gpt-5.4-mini", estimated_tokens=100,
                      usage=CanonicalUsage(input_tokens=60, output_tokens=40))
    # a second nightly process opens the same ledger file and sees the spend
    second = budget.DailyLedger(config=budget.load_budget_config({}))
    assert second.calls_today() == 1
    assert second.tokens_today() == 100
    assert first.path == second.path
    assert Path(first.path).parent == tmp_path


def test_ledger_day_rolls_over_in_europe_berlin(tmp_path):
    cfg = budget.load_budget_config({})
    led = budget.DailyLedger(tmp_path / "ledger.json", config=cfg,
                             clock=_clock("2026-07-10T23:30:00+02:00"))
    led.record_call(lane="skill", model="gpt-5.4-mini", estimated_tokens=50,
                    usage=CanonicalUsage(input_tokens=25, output_tokens=25))
    assert led.tokens_today() == 50
    tomorrow = budget.DailyLedger(tmp_path / "ledger.json", config=cfg,
                                  clock=_clock("2026-07-11T00:30:00+02:00"))
    assert tomorrow.calls_today() == 0
    assert tomorrow.tokens_today() == 0
    # evidence of the previous day is retained, not deleted
    raw = json.loads((tmp_path / "ledger.json").read_text(encoding="utf-8"))
    assert "2026-07-10" in raw.get("days", {})


def test_missing_provider_usage_is_estimated_never_measured_zero(tmp_path):
    led = budget.DailyLedger(tmp_path / "ledger.json", config=budget.load_budget_config({}))
    entry = led.record_call(lane="code", model="gpt-5.4-mini", estimated_tokens=1_200, usage=None)
    assert entry["usage_source"] in ("estimated", "unknown")
    assert entry["usage_source"] != "measured"
    assert entry["total_tokens"] >= 1_200  # the conservative reservation stands
    assert led.tokens_today() >= 1_200


def test_measured_usage_replaces_reservation_with_breakdown(tmp_path):
    led = budget.DailyLedger(tmp_path / "ledger.json", config=budget.load_budget_config({}))
    usage = CanonicalUsage(input_tokens=700, output_tokens=200,
                           cache_read_tokens=100, reasoning_tokens=50)
    entry = led.record_call(lane="deep-audit", model="gpt-5.6-luna",
                            estimated_tokens=5_000, usage=usage)
    assert entry["usage_source"] == "measured"
    assert entry["input_tokens"] == 700
    assert entry["output_tokens"] == 200
    assert entry["cached_tokens"] == 100
    assert entry["reasoning_tokens"] == 50
    assert entry["total_tokens"] == usage.total_tokens
    assert led.tokens_today() == usage.total_tokens  # estimate reconciled away


def test_ledger_never_persists_prompts_or_credentials(tmp_path):
    led = budget.DailyLedger(tmp_path / "ledger.json", config=budget.load_budget_config({}))
    led.record_call(lane="skill", model="gpt-5.4-mini", estimated_tokens=10,
                    usage=CanonicalUsage(input_tokens=5, output_tokens=5))
    raw = (tmp_path / "ledger.json").read_text(encoding="utf-8")
    payload = json.loads(raw)
    entry = payload["days"][led.day_key()]["calls"][0]
    allowed_keys = {
        "at", "lane", "model", "estimated_tokens", "usage_source",
        "input_tokens", "cached_tokens", "output_tokens", "reasoning_tokens",
        "total_tokens",
    }
    assert set(entry) <= allowed_keys
    for needle in ("api_key", "authorization", "account", "prompt", "messages"):
        assert needle not in raw.lower()


def test_zero_model_calls_still_report_real_zero():
    assert budget.run_usage_summary([]) == {"tokens": 0, "usage_source": "measured"}


def test_run_usage_summary_flags_estimates():
    entries = [
        {"total_tokens": 100, "usage_source": "measured"},
        {"total_tokens": 900, "usage_source": "estimated"},
    ]
    summary = budget.run_usage_summary(entries)
    assert summary["tokens"] == 1_000
    assert summary["usage_source"] == "estimated"


def test_estimate_call_tokens_is_conservative():
    messages = [{"role": "user", "content": "x" * 4_000}]
    est = budget.estimate_call_tokens(messages, max_tokens=900)
    assert est >= 1_000 + 900  # ~chars/4 prompt + full output allowance
    assert budget.estimate_call_tokens([], max_tokens=0) >= 1


# ------------------------------------------------------------- ROI cooldown


def _cooldown_run(path, lane="skill", *, outcome="clean", yielded=0, healthy_calls=5, iso="2026-07-10T03:30:00+02:00"):
    return budget.record_lane_run_for_cooldown(
        lane, outcome=outcome, yielded=yielded, healthy_calls=healthy_calls,
        path=path, clock=_clock(iso),
    )


def test_three_healthy_zero_yield_runs_set_seven_day_cooldown(tmp_path):
    state_path = tmp_path / "cooldowns.json"
    _cooldown_run(state_path)
    _cooldown_run(state_path)
    assert budget.lane_cooldown_until("skill", path=state_path, clock=_clock("2026-07-10T04:00:00+02:00")) is None
    _cooldown_run(state_path)
    until = budget.lane_cooldown_until("skill", path=state_path, clock=_clock("2026-07-10T04:00:00+02:00"))
    assert until is not None
    assert until.startswith("2026-07-17")  # +7 days
    # reversible: expires on its own
    assert budget.lane_cooldown_until("skill", path=state_path, clock=_clock("2026-07-18T04:00:00+02:00")) is None


def test_errors_expected_skips_and_budget_skips_do_not_count(tmp_path):
    state_path = tmp_path / "cooldowns.json"
    _cooldown_run(state_path)
    _cooldown_run(state_path)
    # none of these may complete the streak
    _cooldown_run(state_path, outcome="infra_failed", healthy_calls=0)
    _cooldown_run(state_path, outcome="skipped_expected", healthy_calls=0)
    _cooldown_run(state_path, outcome="budget_exhausted", healthy_calls=2)
    _cooldown_run(state_path, outcome="quota_skipped", healthy_calls=0)
    _cooldown_run(state_path, outcome="clean", healthy_calls=0)  # no model call = not healthy evidence
    assert budget.lane_cooldown_until("skill", path=state_path, clock=_clock("2026-07-10T05:00:00+02:00")) is None
    _cooldown_run(state_path)  # third healthy zero-yield → trigger
    assert budget.lane_cooldown_until("skill", path=state_path, clock=_clock("2026-07-10T05:00:00+02:00"))


def test_yield_resets_the_zero_yield_streak(tmp_path):
    state_path = tmp_path / "cooldowns.json"
    _cooldown_run(state_path)
    _cooldown_run(state_path)
    _cooldown_run(state_path, outcome="yielded", yielded=2)
    _cooldown_run(state_path)
    _cooldown_run(state_path)
    assert budget.lane_cooldown_until("skill", path=state_path, clock=_clock("2026-07-10T05:00:00+02:00")) is None


def test_cooldown_never_mutates_config_yaml(tmp_path, monkeypatch):
    """The cooldown is state-only: config.yaml stays byte-identical."""
    fake_home = tmp_path / ".hermes"
    fake_home.mkdir()
    cfg_file = fake_home / "config.yaml"
    cfg_file.write_text("model: test\n", encoding="utf-8")
    monkeypatch.setenv("HERMES_HOME", str(fake_home))
    state_path = tmp_path / "cooldowns.json"
    for _ in range(3):
        _cooldown_run(state_path)
    assert cfg_file.read_text(encoding="utf-8") == "model: test\n"


# ------------------------------------------------- per-call enforcement in lanes


def _exhaust_shared_ledger():
    led = budget.DailyLedger(config=budget.load_budget_config({}))
    for _ in range(30):
        led.record_call(lane="skill", model="gpt-5.4-mini", estimated_tokens=1,
                        usage=CanonicalUsage(input_tokens=1))
    return led


def _never_call(**_kwargs):
    raise AssertionError("model must not be called once the daily ledger is exhausted")


def test_skill_lane_respects_shared_ledger():
    from hermes_cli import capability_researcher

    _exhaust_shared_ledger()
    result = capability_researcher.research_skill("probe", "# skill text", call_llm=_never_call)
    assert result["ok"] is False
    assert "budget exhausted" in (result["reason"] or "").lower()


def test_code_lane_respects_shared_ledger(monkeypatch, tmp_path):
    import hermes_cli.autoresearch_proposals as proposals

    _exhaust_shared_ledger()
    monkeypatch.setattr(proposals, "_writer_call_llm", _never_call)
    res = proposals._call_code_weakness_finder(tmp_path / "p.py", "x = 1\n", timeout=5)
    assert res["ok"] is False
    assert "budget exhausted" in (res["reason"] or "").lower()


def test_deep_audit_lane_respects_shared_ledger(monkeypatch):
    from hermes_cli import deep_audit

    monkeypatch.setitem(deep_audit.SUBSYSTEM_GLOBS, "unit", ("hermes_cli/autoresearch_runs.py",))
    _exhaust_shared_ledger()
    result = deep_audit.run_deep_audit(subsystem="unit", llm_call=_never_call)
    assert "budget exhausted" in (result.get("reason") or "").lower()

    from hermes_cli.autoresearch_lane_contracts import classify_lane_outcome
    outcome = classify_lane_outcome(
        "deep-audit", scanned=0, errors=0, yielded=0, ok=False,
        reason=result.get("reason") or "")
    assert outcome.outcome == "budget_exhausted"
    assert outcome.fatal is False


def test_foundry_lane_respects_shared_ledger():
    from hermes_cli import test_foundry
    from hermes_cli._ast_mutator import generate_mutants

    source = "def f(a, b):\n    return a + b\n"
    mutant = generate_mutants(source, max_mutants=1)[0]
    _exhaust_shared_ledger()
    with pytest.raises(budget.BudgetExhausted):
        test_foundry._call_hardening_llm(
            _never_call,
            target_module="hermes_cli/x.py",
            source=source,
            mutant=mutant,
            diff="",
            affected_tests=["tests/test_x.py"],
        )


def test_guarded_llm_call_records_measured_usage(tmp_path):
    led = budget.DailyLedger(tmp_path / "ledger.json", config=budget.load_budget_config({}))

    class _Resp:
        model = "gpt-5.4-mini"

        class usage:  # noqa: N801 — provider SDK shape
            input_tokens = 40
            output_tokens = 10
            total_tokens = 50

        choices = []

    resp, entry = budget.guarded_llm_call(
        lane="skill",
        call=lambda **_k: _Resp(),
        task="skills_hub",
        messages=[{"role": "user", "content": "hi"}],
        max_tokens=100,
        ledger=led,
    )
    assert entry["usage_source"] == "measured"
    assert entry["total_tokens"] == 50
    assert led.calls_today() == 1


def test_guarded_llm_call_estimates_when_usage_missing(tmp_path):
    led = budget.DailyLedger(tmp_path / "ledger.json", config=budget.load_budget_config({}))

    class _Resp:
        model = "gpt-5.4-mini"
        usage = None
        choices = []

    _resp, entry = budget.guarded_llm_call(
        lane="code",
        call=lambda **_k: _Resp(),
        task="skills_hub",
        messages=[{"role": "user", "content": "x" * 400}],
        max_tokens=200,
        ledger=led,
    )
    assert entry["usage_source"] == "estimated"
    assert entry["total_tokens"] > 0
    assert led.tokens_today() == entry["total_tokens"]
