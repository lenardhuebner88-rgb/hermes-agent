"""Autoresearch auxiliary lane model routing and smoke checks."""
from __future__ import annotations

from pathlib import Path
from typing import Any, Callable, Iterable

import yaml

from hermes_cli.config import get_hermes_home

NEURALWATT_BASE_URL = "https://api.neuralwatt.com/v1"
NEURALWATT_API_KEY_REF = "$" + "{NEURALWATT_API_KEY}"
AUTORESEARCH_LANES = ("code_audit", "test_hardening", "skills_hub")

LANE_DEFAULT_MODELS = {
    "code_audit": "kimi-k2.7-code",
    "test_hardening": "kimi-k2.7-code",
    "skills_hub": "kimi-k2.6-fast",
}

MODEL_PROFILES: dict[str, dict[str, Any]] = {
    "kimi-k2.7-code": {
        "provider": "auto",
        "base_url": NEURALWATT_BASE_URL,
        "api_key": NEURALWATT_API_KEY_REF,
        "model": "kimi-k2.7-code",
        "timeout": 120,
    },
    "kimi-k2.6-fast": {
        "provider": "auto",
        "base_url": NEURALWATT_BASE_URL,
        "api_key": NEURALWATT_API_KEY_REF,
        "model": "kimi-k2.6-fast",
        "timeout": 30,
    },
    "glm-5.2": {
        "provider": "auto",
        "base_url": NEURALWATT_BASE_URL,
        "api_key": NEURALWATT_API_KEY_REF,
        "model": "glm-5.2",
        "timeout": 120,
    },
    "minimax": {
        "provider": "minimax",
        "base_url": "",
        "api_key": "",
        "model": "MiniMax-M2.7",
    },
}


def default_config_path() -> Path:
    return get_hermes_home() / "config.yaml"


def default_lane_profile(lane: str) -> dict[str, Any]:
    model = LANE_DEFAULT_MODELS[lane]
    return dict(MODEL_PROFILES[model])


def apply_lane_model_config(
    config_path: str | Path | None = None,
    *,
    lane: str,
    model_key: str,
) -> dict[str, Any]:
    """Persist one Autoresearch auxiliary lane model profile in config.yaml."""
    if lane not in AUTORESEARCH_LANES:
        return {"ok": False, "detail": f"unknown autoresearch lane: {lane}"}
    if model_key not in MODEL_PROFILES:
        return {"ok": False, "detail": f"unknown autoresearch model: {model_key}"}

    path = Path(config_path) if config_path is not None else default_config_path()
    try:
        data = yaml.safe_load(path.read_text(encoding="utf-8")) if path.exists() else {}
    except (OSError, ValueError, yaml.YAMLError) as exc:
        return {"ok": False, "detail": f"could not read config: {exc}"}
    if not isinstance(data, dict):
        data = {}
    aux = data.setdefault("auxiliary", {})
    if not isinstance(aux, dict):
        aux = {}
        data["auxiliary"] = aux

    before = dict(aux.get(lane) or {}) if isinstance(aux.get(lane), dict) else {}
    profile = dict(MODEL_PROFILES[model_key])
    aux[lane] = profile
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(yaml.safe_dump(data, sort_keys=False, allow_unicode=True), encoding="utf-8")
    return {"ok": True, "lane": lane, "model_key": model_key, "before": before, "after": profile}


def _tool_spec() -> list[dict[str, Any]]:
    return [
        {
            "type": "function",
            "function": {
                "name": "record_signal",
                "description": "Record the synthetic smoke-test signal.",
                "parameters": {
                    "type": "object",
                    "properties": {"signal": {"type": "string"}},
                    "required": ["signal"],
                    "additionalProperties": False,
                },
            },
        }
    ]


def _energy_from_response(resp: Any) -> Any:
    if hasattr(resp, "energy"):
        return getattr(resp, "energy")
    if isinstance(resp, dict):
        energy = resp.get("energy")
        if energy is not None:
            return energy
        extra = resp.get("model_extra")
        if isinstance(extra, dict):
            return extra.get("energy")
        return None
    extra = getattr(resp, "model_extra", None)
    if isinstance(extra, dict):
        return extra.get("energy")
    return None


def _cost_from_response(resp: Any) -> dict[str, Any] | None:
    cost = getattr(resp, "cost", None)
    if cost is None and isinstance(resp, dict):
        cost = resp.get("cost")
    if cost is None:
        extra = getattr(resp, "model_extra", None)
        if isinstance(extra, dict):
            cost = extra.get("cost")
    return cost if isinstance(cost, dict) else None


def response_usage_metadata(resp: Any) -> dict[str, Any]:
    """Extract persisted NeuralWatt usage metadata from an API response."""
    metadata: dict[str, Any] = {}
    energy = _energy_from_response(resp)
    if energy is not None:
        metadata["energy"] = energy
    cost = _cost_from_response(resp)
    if cost is not None:
        metadata["cost"] = cost
    return metadata


def _first_tool_call_name(resp: Any) -> str | None:
    choices = getattr(resp, "choices", None)
    if not choices and isinstance(resp, dict):
        choices = resp.get("choices")
    if not choices:
        return None
    message = getattr(choices[0], "message", None)
    if message is None and isinstance(choices[0], dict):
        message = choices[0].get("message")
    calls = getattr(message, "tool_calls", None) if message is not None else None
    if calls is None and isinstance(message, dict):
        calls = message.get("tool_calls")
    if not calls:
        return None
    fn = getattr(calls[0], "function", None)
    if fn is None and isinstance(calls[0], dict):
        fn = calls[0].get("function")
    name = getattr(fn, "name", None) if fn is not None else None
    if name is None and isinstance(fn, dict):
        name = fn.get("name")
    return str(name) if name else None


def neuralwatt_tool_call_smoke(
    *,
    models: Iterable[str],
    api_key: str,
    client_factory: Callable[..., Any] | None = None,
    timeout: float = 30.0,
) -> dict[str, Any]:
    """Run an OpenAI-compatible chat.completions tool-call smoke."""
    if client_factory is None:
        from openai import OpenAI

        client_factory = OpenAI
    client = client_factory(api_key=api_key, base_url=NEURALWATT_BASE_URL)
    results: list[dict[str, Any]] = []
    for model in models:
        item: dict[str, Any] = {"model": model, "ok": False}
        try:
            resp = client.chat.completions.create(
                model=model,
                messages=[{"role": "user", "content": "Call record_signal with signal='ok'."}],
                tools=_tool_spec(),
                tool_choice="auto",
                timeout=timeout,
            )
            tool_name = _first_tool_call_name(resp)
            if not tool_name:
                item["error"] = "response did not include a tool_call"
            else:
                item.update({"ok": True, "tool_call": tool_name, **response_usage_metadata(resp)})
        except Exception as exc:  # pragma: no cover - live smoke failure surface
            item["error"] = f"{type(exc).__name__}: {exc}"
        results.append(item)
    return {"ok": all(item.get("ok") for item in results), "models": results}
