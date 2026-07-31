"""Machine-readable evidence contracts for every usage-fact origin.

The contract describes what a source can directly observe.  It is intentionally
separate from current coverage: a missing value from an eligible source is a
coverage gap, while a value the source never emits is ``unavailable_source``.
"""

from __future__ import annotations

from typing import Any

CONTRACT_VERSION = "telemetry-capabilities.v1"

MEASURED = "measured"
PARTIAL = "partial"
UNAVAILABLE_SOURCE = "unavailable_source"
UNKNOWN = "unknown"

_ORIGIN_CONTRACTS: dict[str, dict[str, Any]] = {
    "hermes_agent": {
        "label": "Hermes worker",
        "tokens": MEASURED,
        "model": MEASURED,
        "duration": MEASURED,
        "ttft": MEASURED,
        "task_attribution": "exact_worker_run",
        "langfuse_trace": "hermes_trace_and_generation",
        "billing": "runtime_route",
        "notes": [],
    },
    "hermes_aux": {
        "label": "Hermes auxiliary call",
        "tokens": PARTIAL,
        "model": PARTIAL,
        "duration": PARTIAL,
        "ttft": PARTIAL,
        "task_attribution": "normally_unavailable",
        "langfuse_trace": "hermes_trace_when_in_agent_context",
        "billing": "runtime_route_or_unknown",
        "notes": [
            "Auxiliary hooks vary; eligible coverage therefore remains visible."
        ],
    },
    "claude_code": {
        "label": "Claude CLI transcript",
        "tokens": MEASURED,
        "model": MEASURED,
        "duration": UNAVAILABLE_SOURCE,
        "ttft": UNAVAILABLE_SOURCE,
        "task_attribution": "exact_claude_session_metadata_only",
        "langfuse_trace": "no_hermes_trace",
        "billing": "subscription_or_access_configuration",
        "notes": [
            "Transcript timestamps do not expose request duration or first token.",
            "Subscription marginal cost and API-equivalent cost are separate facts.",
        ],
    },
    "grok_cli": {
        "label": "Grok CLI log",
        "tokens": MEASURED,
        "model": UNAVAILABLE_SOURCE,
        "duration": MEASURED,
        "ttft": PARTIAL,
        "task_attribution": "unavailable_source",
        "langfuse_trace": "no_hermes_trace",
        "billing": "foreign_cli",
        "notes": [
            "TTFT is exact only when the inference_done record emits ttft_ms."
        ],
    },
    "qwen_cli": {
        "label": "Qwen CLI log",
        "tokens": MEASURED,
        "model": MEASURED,
        "duration": MEASURED,
        "ttft": UNAVAILABLE_SOURCE,
        "task_attribution": "unavailable_source",
        "langfuse_trace": "no_hermes_trace",
        "billing": "foreign_cli",
        "notes": [],
    },
    "codex_cli": {
        "label": "Codex CLI transcript",
        "tokens": MEASURED,
        "model": MEASURED,
        "duration": UNAVAILABLE_SOURCE,
        "ttft": UNAVAILABLE_SOURCE,
        "task_attribution": "unavailable_source",
        "langfuse_trace": "no_hermes_trace",
        "billing": "foreign_cli",
        "notes": [],
    },
    "kimi_cli": {
        "label": "Kimi CLI transcript",
        "tokens": MEASURED,
        "model": MEASURED,
        "duration": UNAVAILABLE_SOURCE,
        "ttft": UNAVAILABLE_SOURCE,
        "task_attribution": "unavailable_source",
        "langfuse_trace": "no_hermes_trace",
        "billing": "foreign_cli",
        "notes": [],
    },
}

_UNKNOWN_CONTRACT = {
    "label": "Unknown origin",
    "tokens": UNKNOWN,
    "model": UNKNOWN,
    "duration": UNKNOWN,
    "ttft": UNKNOWN,
    "task_attribution": "unknown",
    "langfuse_trace": "unknown",
    "billing": "unknown",
    "notes": ["No source contract is registered; values must not be inferred."],
}


def telemetry_contract(origin: str) -> dict[str, Any]:
    """Return one copy so callers cannot mutate the canonical contract."""
    return dict(_ORIGIN_CONTRACTS.get(origin, _UNKNOWN_CONTRACT))


def telemetry_contracts() -> dict[str, Any]:
    """Return the complete contract table for API payloads."""
    return {
        "contract_version": CONTRACT_VERSION,
        "origins": {
            origin: telemetry_contract(origin)
            for origin in sorted(_ORIGIN_CONTRACTS)
        },
        "unknown_origin": dict(_UNKNOWN_CONTRACT),
    }


def metric_is_eligible(origin: str, metric: str) -> bool:
    """Whether absence of ``metric`` counts as a source coverage gap."""
    state = telemetry_contract(origin).get(metric, UNKNOWN)
    return state not in {UNAVAILABLE_SOURCE, UNKNOWN}


__all__ = (
    "CONTRACT_VERSION",
    "MEASURED",
    "PARTIAL",
    "UNAVAILABLE_SOURCE",
    "UNKNOWN",
    "metric_is_eligible",
    "telemetry_contract",
    "telemetry_contracts",
)
