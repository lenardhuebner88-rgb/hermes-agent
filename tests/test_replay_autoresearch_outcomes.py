"""Cost-evidence contracts for the autoresearch replay script."""

from __future__ import annotations

import importlib.util
from pathlib import Path


_SCRIPT = Path(__file__).resolve().parents[1] / "scripts" / "replay_autoresearch_outcomes.py"


def _load_replay_script():
    spec = importlib.util.spec_from_file_location("replay_outcomes_under_test", _SCRIPT)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_provider_cost_summary_exposes_derived_equivalent_coverage():
    replay = _load_replay_script()

    summary = replay._provider_cost_summary(
        [
            {
                "cost_usd": 0.0,
                "cost_usd_equivalent": 0.42,
                "billing_mode": "subscription_included",
            },
            {
                "cost_usd": 0.15,
                "cost_usd_equivalent": None,
                "billing_mode": "metered",
            },
        ]
    )

    assert summary["provider_cost_usd_equivalent"] == 0.42
    assert summary["provider_cost_usd_equivalent_confidence"] == "derived"
    assert summary["provider_cost_usd_equivalent_coverage"] == 1.0
    assert summary["provider_effective_cost_usd"] == 0.57


def test_provider_cost_summary_keeps_unpriceable_equivalent_unknown():
    replay = _load_replay_script()

    summary = replay._provider_cost_summary(
        [
            {
                "cost_usd": 0.0,
                "cost_usd_equivalent": None,
                "billing_mode": "subscription_included",
            },
        ]
    )

    assert summary["provider_cost_usd_equivalent"] is None
    assert summary["provider_cost_usd_equivalent_confidence"] == "unknown"
    assert summary["provider_cost_usd_equivalent_coverage"] == 0.0
    assert summary["provider_effective_cost_usd"] is None
