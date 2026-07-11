from __future__ import annotations

import sqlite3
from pathlib import Path
from types import SimpleNamespace

import pytest

from hermes_state import SessionDB
from hermes_cli import deep_audit


def test_state_db_persists_openrouter_generation_id(tmp_path: Path):
    db = SessionDB(tmp_path / "state.db")
    sid = "sess-openrouter"
    db.create_session(sid, "test", model="openrouter/unit")
    db.update_token_counts(
        sid,
        input_tokens=10,
        output_tokens=2,
        model="openrouter/unit",
        billing_provider="openrouter",
        openrouter_generation_id="gen-or-123",
    )
    row = sqlite3.connect(tmp_path / "state.db").execute(
        "SELECT openrouter_generation_id FROM sessions WHERE id=?", (sid,)
    ).fetchone()
    assert row[0] == "gen-or-123"


def test_deep_audit_accumulates_cost_for_all_turns(monkeypatch):
    monkeypatch.setitem(deep_audit.SUBSYSTEM_GLOBS, "unit-cost", ("hermes_cli/deep_audit.py",))
    # run_deep_audit routes every turn through the shared autoresearch budget
    # guard (added after this test). Turn one here bills 1M tokens — far past the
    # 100k daily default — so the guard would refuse turn two and only the first
    # turn's cost would accrue. This test exercises cost ACCUMULATION, not the
    # budget guard (that lives in test_autoresearch_budget), so give it headroom.
    from hermes_cli import autoresearch_budget

    monkeypatch.setattr(
        autoresearch_budget,
        "load_budget_config",
        lambda config=None: autoresearch_budget.BudgetConfig(
            daily_token_limit=1_000_000_000,
            daily_model_call_limit=1_000_000,
        ),
    )
    calls = []

    def resp(tool_calls, *, prompt_tokens: int, completion_tokens: int):
        usage = SimpleNamespace(
            total_tokens=prompt_tokens + completion_tokens,
            prompt_tokens=prompt_tokens,
            completion_tokens=completion_tokens,
        )
        return SimpleNamespace(
            model="glm-5.2",
            usage=usage,
            choices=[SimpleNamespace(message=SimpleNamespace(content="", tool_calls=tool_calls))],
        )

    def tc(name: str, args: dict, call_id: str):
        import json
        return SimpleNamespace(
            id=call_id,
            function=SimpleNamespace(name=name, arguments=json.dumps(args)),
        )

    def fake_llm(**_kwargs):
        calls.append(1)
        if len(calls) == 1:
            return resp([tc("report_finding", {
                "fileline": "hermes_cli/deep_audit.py:426",
                "severity": "low",
                "category": "bug_risk",
                "title": "cost path",
                "problem": "x",
                "evidence": "def _per_turn_cost",
                "fix_hint": "y",
            }, "c1")], prompt_tokens=1_000_000, completion_tokens=0)
        return resp([tc("finish_audit", {"summary": "done"}, "c2")], prompt_tokens=0, completion_tokens=1_000_000)

    result = deep_audit.run_deep_audit(subsystem="unit-cost", llm_call=fake_llm)
    assert result["ok"] is True
    assert result["cost"]["request_cost_usd"] == pytest.approx(2.80)
