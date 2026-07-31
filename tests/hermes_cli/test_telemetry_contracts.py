from __future__ import annotations

from hermes_cli.telemetry_contracts import (
    UNAVAILABLE_SOURCE,
    metric_is_eligible,
    telemetry_contract,
    telemetry_contracts,
)


def test_timing_eligibility_distinguishes_gaps_from_source_limits() -> None:
    contracts = telemetry_contracts()

    assert contracts["contract_version"] == "telemetry-capabilities.v1"
    assert telemetry_contract("claude_code")["ttft"] == UNAVAILABLE_SOURCE
    assert metric_is_eligible("claude_code", "ttft") is False
    assert metric_is_eligible("hermes_agent", "ttft") is True
    assert metric_is_eligible("grok_cli", "ttft") is True
    assert metric_is_eligible("future_origin", "ttft") is False


def test_returned_contract_cannot_mutate_canonical_origin_state() -> None:
    first = telemetry_contract("hermes_agent")
    first["ttft"] = "broken"

    assert telemetry_contract("hermes_agent")["ttft"] == "measured"
