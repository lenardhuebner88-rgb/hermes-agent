"""Regression tests for Autoresearch lane model routing.

The live smoke was verified against NeuralWatt on 2026-06-21; these tests keep the
contract hermetic by mocking the OpenAI-compatible chat.completions client.
"""
from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

import yaml

from hermes_cli import autoresearch_lane_models as lanes
from hermes_cli.config import DEFAULT_CONFIG

NEURALWATT_KEY_REF = "$" + "{NEURALWATT_API_KEY}"


def test_default_autoresearch_aux_profiles_stay_secret_neutral():
    aux = DEFAULT_CONFIG["auxiliary"]

    for lane, timeout in (("code_audit", 120), ("test_hardening", 120), ("skills_hub", 30)):
        assert aux[lane]["provider"] == "auto"
        assert aux[lane]["base_url"] == ""
        assert aux[lane]["api_key"] == ""
        assert aux[lane]["model"] == ""
        assert aux[lane]["timeout"] == timeout

    assert lanes.default_lane_profile("code_audit")["model"] == "kimi-k2.7-code"
    assert lanes.default_lane_profile("test_hardening")["model"] == "kimi-k2.7-code"
    assert lanes.default_lane_profile("skills_hub")["model"] == "kimi-k2.6-fast"


def test_tool_call_smoke_uses_chat_completions_tools_and_reads_usage_metadata():
    calls: list[dict] = []

    class FakeCompletions:
        def create(self, **kwargs):
            calls.append(kwargs)
            return SimpleNamespace(
                model=kwargs["model"],
                choices=[
                    SimpleNamespace(
                        message=SimpleNamespace(
                            content=None,
                            tool_calls=[SimpleNamespace(function=SimpleNamespace(name="record_signal"))],
                        )
                    )
                ],
                energy={"energy_kwh": 0.00042, "avg_power_watts": 38.0, "carbon_g_co2eq": 0.0147},
                cost={"request_cost_usd": 0.0019},
            )

    class FakeClient:
        def __init__(self, **kwargs):
            self.kwargs = kwargs
            self.chat = SimpleNamespace(completions=FakeCompletions())

    result = lanes.neuralwatt_tool_call_smoke(
        models=["kimi-k2.7-code", "glm-5.2"],
        api_key="test-key",
        client_factory=FakeClient,
    )

    assert result["ok"] is True
    assert [call["model"] for call in calls] == ["kimi-k2.7-code", "glm-5.2"]
    assert all(call["tools"][0]["function"]["name"] == "record_signal" for call in calls)
    assert all(call["tool_choice"] == "auto" for call in calls)
    assert result["models"][0]["tool_call"] == "record_signal"
    assert result["models"][0]["energy"]["energy_kwh"] == 0.00042
    assert result["models"][0]["cost"]["request_cost_usd"] == 0.0019


def test_response_usage_metadata_reads_cost_from_model_extra():
    resp = SimpleNamespace(
        model_extra={
            "energy": {"energy_kwh": 0.03},
            "cost": {"request_cost_usd": 0.42},
        }
    )

    assert lanes.response_usage_metadata(resp) == {
        "energy": {"energy_kwh": 0.03},
        "cost": {"request_cost_usd": 0.42},
    }


def test_response_usage_metadata_reads_energy_from_model_extra_on_dict():
    # Regression: _energy_from_response must mirror _cost_from_response and
    # fall back to resp["model_extra"]["energy"] for dict-shaped responses.
    resp = {"model_extra": {"energy": {"energy_kwh": 0.03}}}

    assert lanes.response_usage_metadata(resp) == {
        "energy": {"energy_kwh": 0.03},
    }


def test_tool_call_smoke_fails_closed_without_tool_call():
    class FakeCompletions:
        def create(self, **_kwargs):
            return SimpleNamespace(
                model="kimi-k2.7-code",
                choices=[SimpleNamespace(message=SimpleNamespace(content="plain text", tool_calls=[]))],
            )

    class FakeClient:
        def __init__(self, **_kwargs):
            self.chat = SimpleNamespace(completions=FakeCompletions())

    result = lanes.neuralwatt_tool_call_smoke(
        models=["kimi-k2.7-code"],
        api_key="test-key",
        client_factory=FakeClient,
    )

    assert result["ok"] is False
    assert result["models"][0]["ok"] is False
    assert "tool_call" in result["models"][0]["error"]


def test_apply_lane_model_writes_only_requested_aux_slots(tmp_path: Path):
    cfg_path = tmp_path / "config.yaml"
    cfg_path.write_text(
        yaml.safe_dump({"auxiliary": {"code_audit": {"model": "old"}, "vision": {"model": "keep"}}}),
        encoding="utf-8",
    )

    result = lanes.apply_lane_model_config(cfg_path, lane="code_audit", model_key="glm-5.2")

    saved = yaml.safe_load(cfg_path.read_text(encoding="utf-8"))
    assert result["ok"] is True
    assert saved["auxiliary"]["code_audit"] == {
        "provider": "auto",
        "base_url": "https://api.neuralwatt.com/v1",
        "api_key": NEURALWATT_KEY_REF,
        "model": "glm-5.2",
        "timeout": 120,
    }
    assert saved["auxiliary"]["vision"] == {"model": "keep"}
