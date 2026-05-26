"""Pure Hermes routing-policy selector for test-only hardening.

This module intentionally contains no config reads, provider calls, credential
lookups, gateway interaction, dispatch, MCP startup, or tool execution.  It is a
small deterministic representation of the P1.27/P1.28 routing-policy contract so
future config changes can be tested before they are proposed or applied.
"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class RoutingCandidate:
    provider: str
    model: str
    api_mode: str
    routing_class: str
    source: str
    schema_status: str
    expected_cost_pressure: str
    quality_role: str
    tool_support: str
    recommended_usage: str = ""
    no_go_conditions: tuple[str, ...] = ()


@dataclass(frozen=True)
class RoutingIntent:
    task_type: str = "default"
    cost_mode: str = "normal"
    quality_reason: str | None = None
    user_cost_go: bool = False
    allow_free: bool = False
    exclude_yellow: bool = True
    task_criticality: str = "normal"


@dataclass(frozen=True)
class RoutingDecision:
    primary: RoutingCandidate
    fallbacks: tuple[RoutingCandidate, ...]
    ask_user_for_go: bool = False
    reason: str = ""


DEFAULT_ROUTING_CANDIDATES: tuple[RoutingCandidate, ...] = (
    RoutingCandidate(
        provider="openai-codex",
        model="gpt-5.5",
        api_mode="codex_responses",
        routing_class="default_fast_cheap",
        source="oauth_abo",
        schema_status="green",
        expected_cost_pressure="none",
        quality_role="primary quality default",
        tool_support='proven ["todo"] acceptance',
        recommended_usage="normal Kanban/coding/review/default Hermes",
        no_go_conditions=("OAuth unavailable", "repeated timeout", "profile-local auth missing"),
    ),
    RoutingCandidate(
        provider="minimax",
        model="MiniMax-M2.7",
        api_mode="anthropic_messages",
        routing_class="cheap_fallback",
        source="minimax_budget",
        schema_status="green",
        expected_cost_pressure="medium",
        quality_role="worker/fallback generalist",
        tool_support='proven ["todo"] acceptance',
        recommended_usage="worker profiles and fallback when Codex degraded",
        no_go_conditions=("budget exhaustion", "MiniMax auth unavailable", "schema mode drift"),
    ),
    RoutingCandidate(
        provider="openrouter",
        model="deepseek/deepseek-v4-pro",
        api_mode="chat_completions",
        routing_class="cheap_fallback",
        source="openrouter_paid",
        schema_status="green",
        expected_cost_pressure="low",
        quality_role="cheap coding/fallback",
        tool_support="P1.26 tools/tool_choice accepted",
        recommended_usage="low-cost fallback / OpenRouter relief candidate",
        no_go_conditions=("OpenRouter auth unavailable", "rate/cost limit", "quality insufficient"),
    ),
    RoutingCandidate(
        provider="openrouter",
        model="nvidia/nemotron-3-super-120b-a12b:free",
        api_mode="chat_completions",
        routing_class="free_opportunistic",
        source="openrouter_free",
        schema_status="green",
        expected_cost_pressure="none",
        quality_role="opportunistic free fallback",
        tool_support="P1.26 tools/tool_choice accepted",
        recommended_usage="non-critical tasks with safe fallback",
        no_go_conditions=("endpoint unavailable", "rate-limited", "poor output", "critical task"),
    ),
    RoutingCandidate(
        provider="openrouter",
        model="anthropic/claude-sonnet-4.6",
        api_mode="chat_completions",
        routing_class="quality_escalation",
        source="openrouter_paid",
        schema_status="green",
        expected_cost_pressure="medium",
        quality_role="quality escalation",
        tool_support="P1.26 tools/tool_choice accepted",
        recommended_usage="hard coding/review/reasoning after cheaper paths",
        no_go_conditions=("no quality reason", "rate/cost limit"),
    ),
    RoutingCandidate(
        provider="openrouter",
        model="anthropic/claude-opus-4.7",
        api_mode="chat_completions",
        routing_class="explicit_cost_escalation",
        source="openrouter_paid",
        schema_status="green",
        expected_cost_pressure="high",
        quality_role="premium quality escalation",
        tool_support="P1.26 tools/tool_choice accepted",
        recommended_usage="only after user Go for expensive escalation",
        no_go_conditions=("no explicit user Go", "cost guard exceeded"),
    ),
    RoutingCandidate(
        provider="openrouter",
        model="deepseek/deepseek-v4-flash",
        api_mode="chat_completions",
        routing_class="avoid_or_review",
        source="openrouter_paid",
        schema_status="yellow",
        expected_cost_pressure="low",
        quality_role="cheap candidate only after review",
        tool_support="schema accepted but content empty",
        recommended_usage="no automatic use",
        no_go_conditions=("empty content repeated", "no route-shape fix"),
    ),
    RoutingCandidate(
        provider="openrouter",
        model="google/gemini-2.5-flash-lite",
        api_mode="chat_completions",
        routing_class="avoid_or_review",
        source="openrouter_paid",
        schema_status="yellow",
        expected_cost_pressure="low",
        quality_role="fast cheap candidate only after review",
        tool_support="schema accepted but content empty",
        recommended_usage="no automatic use",
        no_go_conditions=("empty content", "route mismatch unresolved"),
    ),
    RoutingCandidate(
        provider="openrouter",
        model="tencent/hy3-preview:free",
        api_mode="chat_completions",
        routing_class="avoid_or_review",
        source="openrouter_free",
        schema_status="yellow",
        expected_cost_pressure="none",
        quality_role="preview/free only",
        tool_support="schema accepted but content empty",
        recommended_usage="no automatic use",
        no_go_conditions=("empty content", "preview instability"),
    ),
    RoutingCandidate(
        provider="minimax",
        model="MiniMax-M2.7-highspeed",
        api_mode="anthropic_messages",
        routing_class="avoid_or_review",
        source="minimax_budget",
        schema_status="static_only",
        expected_cost_pressure="medium",
        quality_role="low-latency auxiliary",
        tool_support="not separately proven for schema-critical worker paths",
        recommended_usage="auxiliary-only until restricted schema proof",
        no_go_conditions=("worker-critical promotion before dedicated probe",),
    ),
)


def choose_routing(
    intent: RoutingIntent | None = None,
    candidates: tuple[RoutingCandidate, ...] = DEFAULT_ROUTING_CANDIDATES,
) -> RoutingDecision:
    """Return a deterministic policy decision without touching runtime state."""
    intent = intent or RoutingIntent()
    primary = _first(candidates, routing_class="default_fast_cheap")
    fallbacks: list[RoutingCandidate] = []
    ask_user_for_go = False
    reasons: list[str] = []

    def add_if_allowed(candidate: RoutingCandidate) -> None:
        if candidate == primary:
            return
        if intent.exclude_yellow and candidate.schema_status == "yellow":
            return
        if candidate.routing_class == "avoid_or_review":
            return
        if candidate.routing_class == "free_opportunistic" and not _free_allowed(intent):
            return
        if candidate.routing_class == "quality_escalation" and not intent.quality_reason:
            return
        if candidate.routing_class == "explicit_cost_escalation" and not intent.user_cost_go:
            return
        if candidate not in fallbacks:
            fallbacks.append(candidate)

    for candidate in candidates:
        if candidate.routing_class == "cheap_fallback":
            add_if_allowed(candidate)

    if intent.cost_mode == "free_ok" and intent.allow_free:
        for candidate in candidates:
            if candidate.routing_class == "free_opportunistic":
                add_if_allowed(candidate)

    if intent.cost_mode in {"quality", "explicit_cost"}:
        if not intent.quality_reason:
            ask_user_for_go = True
            reasons.append("quality reason required")
        else:
            for candidate in candidates:
                if candidate.routing_class == "quality_escalation":
                    add_if_allowed(candidate)

    if intent.cost_mode == "explicit_cost":
        if not intent.user_cost_go:
            ask_user_for_go = True
            reasons.append("explicit user Go required")
        else:
            for candidate in candidates:
                if candidate.routing_class == "explicit_cost_escalation":
                    add_if_allowed(candidate)

    return RoutingDecision(
        primary=primary,
        fallbacks=tuple(fallbacks),
        ask_user_for_go=ask_user_for_go,
        reason="; ".join(reasons),
    )


def fallback_providers_for(decision: RoutingDecision) -> list[dict[str, str]]:
    """Export a secret-free list compatible with current AIAgent fallback shape."""
    return [
        {"provider": candidate.provider, "model": candidate.model}
        for candidate in decision.fallbacks
    ]


def _first(candidates: tuple[RoutingCandidate, ...], *, routing_class: str) -> RoutingCandidate:
    for candidate in candidates:
        if candidate.routing_class == routing_class:
            return candidate
    raise ValueError(f"missing routing candidate for class {routing_class!r}")


def _free_allowed(intent: RoutingIntent) -> bool:
    if not intent.allow_free:
        return False
    return intent.task_criticality.lower() not in {"critical", "high", "production"}
