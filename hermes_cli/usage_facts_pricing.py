"""Fork-owned pricing seam for the usage-facts layer.

Every fork read path that prices usage calls this module — never the
upstream estimators in ``agent/usage_pricing.py`` directly.  Upstream is
still the source for route resolution and base rates; this module layers
the fork decisions on top (Canon ``2026-07-27-kosten-ssot-im-lesepfad``,
``2026-07-31-metrik-ssot-register``):

- a component vocabulary that knows the cache-write TTL split
  (``cache_write_1h_tokens`` / ``cache_write_5m_tokens``);
- Anthropic cache-write rates split by TTL: 5m write = 1.25x input,
  1h write = 2x input (P1 research, platform.claude.com pricing docs,
  fetched 2026-08-03);
- list prices instead of temporal discounts (Canon 7.2): claude-sonnet-5
  is priced at $3/$15, not the introductory $2/$10 window;
- Qwen list prices incl. the explicit cache-read rate (10% of input);
  qwen3.8-max-preview and qwen3.8-max carry no sourced PAYG rate and
  stay unknown (fail-closed, Canon rule 3);
- local aliases resolve onto their priced target model instead of
  growing their own price row;
- one ``pricing_version`` stamp so a rate change is a visible diff.

Costs remain a read-path derivation: nothing here writes a price into a
database column.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from decimal import Decimal
from typing import Any, Mapping, Optional, Union

from agent.usage_pricing import (
    BillingRoute,
    CanonicalUsage,
    CostResult,
    PricingEntry,
    get_pricing_entry,
    resolve_billing_route,
)

__all__ = [
    "USAGE_FACTS_PRICING_VERSION",
    "PRICE_COMPONENTS",
    "UsageFactsUsage",
    "estimate_usage_cost",
    "estimate_equivalent_cost",
    "priceability",
    "resolve_model_alias",
    # Re-exported upstream types so fork read paths do not import
    # agent.usage_pricing themselves.
    "BillingRoute",
    "CanonicalUsage",
    "CostResult",
    "PricingEntry",
]

# Bump on every rate change: a price change must be a visible diff
# (Canon kosten-ssot-im-lesepfad, rule 2).
USAGE_FACTS_PRICING_VERSION = "usage-facts-pricing-2026-08-03"

# Own component vocabulary. The TTL-split components exist because
# upstream CanonicalUsage/PricingEntry cannot represent them (Fable D2).
PRICE_COMPONENTS = (
    "input_tokens",
    "output_tokens",
    "cache_read_tokens",
    "cache_write_tokens",
    "cache_write_1h_tokens",
    "cache_write_5m_tokens",
    "reasoning_tokens",
)

_ONE_MILLION = Decimal("1000000")
_ZERO = Decimal("0")

# Local aliases observed in the facts layer. They resolve onto a priced
# target model and never get a price row of their own (P1, 2026-08-03).
# ``qwen-route`` is deliberately absent: it has never been observed in
# usage_facts and no sourced target model exists — it stays unpriced
# (fail-closed) instead of being guessed onto a Qwen model.
_MODEL_ALIASES: dict[str, str] = {
    "kimi-for-coding": "k3",
    "kimi-k3": "k3",
    # Dated Anthropic snapshot ids onto their priced base entries.
    "claude-haiku-4-5-20251001": "claude-haiku-4-5",
    "claude-sonnet-4-5-20250929": "claude-sonnet-4-5",
}

# Anthropic cache-write multipliers on the input rate (P1, 2026-08-03,
# platform.claude.com about-claude/pricing + prompt-caching docs).
_ANTHROPIC_CACHE_WRITE_5M_FACTOR = Decimal("1.25")
_ANTHROPIC_CACHE_WRITE_1H_FACTOR = Decimal("2")


@dataclass(frozen=True)
class _RateTable:
    """Per-component $/1M rates; None means "no sourced rate"."""

    rates: Mapping[str, Optional[Decimal]]
    request_rate: Optional[Decimal]
    status: str  # "priced" | "unknown"
    source: str
    pricing_version: Optional[str]
    notes: tuple[str, ...] = ()
    # Mirrors upstream PricingEntry.reasoning_billing: with
    # "included_in_output" reasoning tokens need no rate of their own
    # (they are already paid through output_tokens); only
    # "separate_rate" models require a sourced reasoning rate.
    reasoning_billing: str = "included_in_output"


@dataclass(frozen=True)
class UsageFactsUsage:
    """Usage shaped for fork pricing, including the cache-write TTL split.

    ``cache_write_1h_tokens`` / ``cache_write_5m_tokens`` are None when the
    split was not observed; pricing then falls back to the
    ``cache_write_tokens`` total (register Leseregel-Nachtrag: the split is
    the more complete observation where it exists, otherwise the total).
    """

    input_tokens: int = 0
    output_tokens: int = 0
    cache_read_tokens: int = 0
    cache_write_tokens: int = 0
    cache_write_1h_tokens: Optional[int] = None
    cache_write_5m_tokens: Optional[int] = None
    reasoning_tokens: int = 0
    request_count: int = 1

    @property
    def total_tokens(self) -> int:
        """Mirror upstream CanonicalUsage: stored buckets, no split double
        counting (the TTL split re-prices cache writes, it does not add
        tokens)."""
        return (
            int(self.input_tokens)
            + int(self.cache_read_tokens)
            + int(self.cache_write_tokens)
            + int(self.output_tokens)
        )

    @classmethod
    def from_canonical(cls, usage: CanonicalUsage) -> "UsageFactsUsage":
        """Adapt upstream usage (no TTL split) without inventing data."""
        return cls(
            input_tokens=int(usage.input_tokens),
            output_tokens=int(usage.output_tokens),
            cache_read_tokens=int(usage.cache_read_tokens),
            cache_write_tokens=int(usage.cache_write_tokens),
            cache_write_1h_tokens=None,
            cache_write_5m_tokens=None,
            reasoning_tokens=int(usage.reasoning_tokens),
            request_count=int(usage.request_count),
        )


UsageArgument = Union[UsageFactsUsage, CanonicalUsage]


def resolve_model_alias(model: str) -> str:
    """Resolve local aliases onto their priced target model.

    Applied both before route resolution (bare aliases such as
    ``kimi-k3``) and after it (``kimi-code/kimi-for-coding`` — upstream
    route resolution strips the vendor prefix for subscription aliases,
    the alias then matches the stripped name).
    """
    normalized = str(model or "").strip()
    resolved = _MODEL_ALIASES.get(normalized.lower())
    if resolved is not None:
        return resolved
    basename = normalized.rsplit("/", 1)[-1]
    return _MODEL_ALIASES.get(basename.lower(), normalized)


# Alibaba context-cache (alibabacloud.com model-studio context-cache docs,
# Grok round 2, 2026-08-03): explicit caching (``cache_control: ephemeral``)
# bills writes at 125% of input and hits at 10%; implicit caching activates
# automatically without any header, cannot be disabled, and bills hits at
# 20% — explicit and implicit are mutually exclusive.
_ALIBABA_EXPLICIT_CACHE_READ_FACTOR = Decimal("0.10")
_ALIBABA_IMPLICIT_CACHE_READ_FACTOR = Decimal("0.20")
_ALIBABA_EXPLICIT_CACHE_WRITE_FACTOR = Decimal("1.25")

# Which clients send ``cache_control``?  Verified 2026-08-03 on the
# installed clients: Claude Code (claude-qwen wrapper) speaks the
# Anthropic protocol to the Token-Plan Anthropic-compatible endpoint and
# sends cache_control markers — the qwen transcripts bill
# cache_creation_input_tokens, so explicit caching is active there.
# Qwen Code (@qwen-code/qwen-code) sets cache_control only in its
# anthropicContentGenerator, never in the OpenAI-compatible generator the
# ``authType: openai`` lane uses; the hermes runtime sets it nowhere
# outside agent/anthropic_adapter.py.  Hence: claude_code origin =
# explicit rates, everything else = implicit read rate.
_EXPLICIT_CACHE_ORIGINS = frozenset({"claude_code"})


def _fork_override(
    route: BillingRoute, origin: Optional[str] = None
) -> Optional[_RateTable]:
    """Fork rate decisions that upstream must not (and does not) carry."""
    key = (route.provider, route.model.lower())
    version = USAGE_FACTS_PRICING_VERSION
    docs_source = "official_docs_snapshot"
    anthropic_docs = "https://platform.claude.com/docs/en/about-claude/pricing"

    def _anthropic_entry(
        input_rate: Decimal,
        output_rate: Decimal,
        cache_read_rate: Decimal,
        cache_write_5m: Decimal,
        cache_write_1h: Decimal,
        notes: tuple[str, ...] = (),
    ) -> _RateTable:
        return _RateTable(
            rates={
                "input_tokens": input_rate,
                "output_tokens": output_rate,
                "cache_read_tokens": cache_read_rate,
                "cache_write_tokens": cache_write_5m,
                "cache_write_1h_tokens": cache_write_1h,
                "cache_write_5m_tokens": cache_write_5m,
                "reasoning_tokens": None,
            },
            request_rate=None,
            status="priced",
            source=docs_source,
            pricing_version=version,
            notes=notes,
        )

    if key == ("anthropic", "claude-opus-5"):
        # Absent from the upstream catalog; rates fetched from the source
        # below on 2026-08-03 (P1 provided only the input rate).
        return _anthropic_entry(
            Decimal("5.00"),
            Decimal("25.00"),
            Decimal("0.50"),
            Decimal("6.25"),
            Decimal("10.00"),
            notes=(f"sourced {anthropic_docs} 2026-08-03",),
        )

    if key == ("anthropic", "claude-fable-5"):
        # Absent from the upstream catalog; sourced as above.
        return _anthropic_entry(
            Decimal("10.00"),
            Decimal("50.00"),
            Decimal("1.00"),
            Decimal("12.50"),
            Decimal("20.00"),
            notes=(f"sourced {anthropic_docs} 2026-08-03",),
        )

    if key == ("anthropic", "claude-sonnet-5"):
        # Canon 7.2: list price, no temporal discounts. The source itself
        # lists the introductory window (through 2026-08-31) separately;
        # the fork prices the standing list rates.
        return _anthropic_entry(
            Decimal("3.00"),
            Decimal("15.00"),
            Decimal("0.30"),
            Decimal("3.75"),
            Decimal("6.00"),
            notes=(
                f"sourced {anthropic_docs} 2026-08-03; "
                "list price per Canon 7.2, introductory rate ignored"
            ),
        )

    qwen_list_rates = {
        # Singapore list prices, <=256K context tier for plus/flash
        # (alibabacloud.com model-studio model-pricing, Grok round 2,
        # 2026-08-03). The temporal 50% campaign is not used (Canon 7.2).
        "qwen3.7-max": (Decimal("2.50"), Decimal("7.50")),
        "qwen3.7-plus": (Decimal("0.40"), Decimal("1.60")),
        "qwen3.6-flash": (Decimal("0.25"), Decimal("1.50")),
    }
    if route.provider == "alibaba" and key[1] in qwen_list_rates:
        input_rate, output_rate = qwen_list_rates[key[1]]
        if origin in _EXPLICIT_CACHE_ORIGINS:
            # Claude Code sends cache_control against the Anthropic-
            # compatible Token-Plan endpoint: explicit cache pricing
            # (read 10% of input, write 125% of input). Alibaba bills no
            # TTL split, so all write components share the write rate.
            cache_read = input_rate * _ALIBABA_EXPLICIT_CACHE_READ_FACTOR
            cache_write = input_rate * _ALIBABA_EXPLICIT_CACHE_WRITE_FACTOR
            notes = ("explicit cache (cache_control via Anthropic protocol)",)
        else:
            # OpenAI-compatible paths (qwen_cli, hermes runtime) send no
            # cache_control, so the automatic implicit cache applies:
            # reads at 20% of input; writes are not billed at all — the
            # write rate does not exist (documented, fail-closed).
            cache_read = input_rate * _ALIBABA_IMPLICIT_CACHE_READ_FACTOR
            cache_write = None
            notes = (
                "implicit cache (no cache_control on this path); "
                "write rate does not exist — only hits are billed",
            )
        return _RateTable(
            rates={
                "input_tokens": input_rate,
                "output_tokens": output_rate,
                "cache_read_tokens": cache_read,
                "cache_write_tokens": cache_write,
                "cache_write_1h_tokens": cache_write,
                "cache_write_5m_tokens": cache_write,
                "reasoning_tokens": None,
            },
            request_rate=None,
            status="priced",
            source=docs_source,
            pricing_version=version,
            notes=notes,
        )

    if key == ("alibaba", "qwen3.8-max-preview") or key == (
        "alibaba",
        "qwen3.8-max",
    ):
        # No PAYG list rate exists for these models — Token-Plan access
        # only, credits without a $/MTok rate. Negative proof (Grok round
        # 2, 2026-08-03): full scrape of the model-pricing page, 0 hits
        # for qwen3.8 (positive control qwen3.7-max: 27 hits). Upstream
        # carries a proxy entry for qwen3.8-max-preview; the fork
        # deliberately overrides it with unknown instead of pricing
        # against another model's rates.
        return _RateTable(
            rates={name: None for name in PRICE_COMPONENTS},
            request_rate=None,
            status="unknown",
            source="none",
            pricing_version=version,
            notes=(
                "PAYG rate does not exist — Token-Plan credits only "
                "(model-pricing full scrape 0 hits, Grok 2026-08-03)",
            ),
        )

    if key == ("openai", "gpt-5.5"):
        # developers.openai.com/api/docs/pricing (Grok round 2,
        # 2026-08-03), <272K context. The cache-writes column is empty
        # for this model: no write line item exists, rows with
        # cache-write tokens stay unknown (fail-closed).
        return _RateTable(
            rates={
                "input_tokens": Decimal("5.00"),
                "output_tokens": Decimal("30.00"),
                "cache_read_tokens": Decimal("0.50"),
                "cache_write_tokens": None,
                "cache_write_1h_tokens": None,
                "cache_write_5m_tokens": None,
                "reasoning_tokens": None,
            },
            request_rate=None,
            status="priced",
            source=docs_source,
            pricing_version=version,
            notes=("cache-write line item does not exist",),
        )

    if key == ("xai", "grok-4.5"):
        # docs.x.ai/developers/pricing, <200K prompt tier (Grok round 2,
        # 2026-08-03). No separate cache-write line item exists.
        return _RateTable(
            rates={
                "input_tokens": Decimal("2.00"),
                "output_tokens": Decimal("6.00"),
                "cache_read_tokens": Decimal("0.30"),
                "cache_write_tokens": None,
                "cache_write_1h_tokens": None,
                "cache_write_5m_tokens": None,
                "reasoning_tokens": None,
            },
            request_rate=None,
            status="priced",
            source=docs_source,
            pricing_version=version,
            notes=(
                "<200K prompt tier; cache-write line item does not exist",
            ),
        )

    if key in {("openai", "gpt-5.6-terra"), ("openai", "gpt-5.6-luna")}:
        # Current OpenAI pricing page (Grok round 2, 2026-08-03). The
        # upstream catalog still carries the outdated launch-blog rates
        # for these two; sol matches upstream and is left alone.
        terra_luna = {
            "gpt-5.6-terra": (
                Decimal("2.00"),
                Decimal("0.20"),
                Decimal("2.50"),
                Decimal("12.00"),
            ),
            "gpt-5.6-luna": (
                Decimal("0.20"),
                Decimal("0.02"),
                Decimal("0.25"),
                Decimal("1.20"),
            ),
        }
        input_rate, cached, write, output_rate = terra_luna[key[1]]
        return _RateTable(
            rates={
                "input_tokens": input_rate,
                "output_tokens": output_rate,
                "cache_read_tokens": cached,
                "cache_write_tokens": write,
                "cache_write_1h_tokens": None,
                "cache_write_5m_tokens": None,
                "reasoning_tokens": None,
            },
            request_rate=None,
            status="priced",
            source=docs_source,
            pricing_version=version,
            notes=(
                "developers.openai.com/api/docs/pricing 2026-08-03; "
                "supersedes the upstream launch-blog entry",
            ),
        )

    return None


def _rate_table_for_route(
    route: BillingRoute, origin: Optional[str] = None
) -> _RateTable:
    override = _fork_override(route, origin)
    if override is not None:
        return override

    entry = get_pricing_entry(
        route.model,
        provider=route.provider,
        base_url=route.base_url or None,
    )
    if entry is None:
        return _RateTable(
            rates={name: None for name in PRICE_COMPONENTS},
            request_rate=None,
            status="unknown",
            source="none",
            pricing_version=None,
            notes=("no pricing entry for route",),
        )

    rates: dict[str, Optional[Decimal]] = {
        "input_tokens": entry.input_cost_per_million,
        "output_tokens": entry.output_cost_per_million,
        "cache_read_tokens": entry.cache_read_cost_per_million,
        "cache_write_tokens": entry.cache_write_cost_per_million,
        "cache_write_1h_tokens": None,
        "cache_write_5m_tokens": None,
        "reasoning_tokens": (
            entry.reasoning_cost_per_million
            if entry.reasoning_billing == "separate_rate"
            else None
        ),
    }
    notes: list[str] = []
    if route.provider == "anthropic" and entry.input_cost_per_million is not None:
        # TTL split for Anthropic cache writes (P1 multipliers). The 5m
        # rate keeps the sourced entry value; 1h is derived from input.
        rates["cache_write_1h_tokens"] = (
            entry.input_cost_per_million * _ANTHROPIC_CACHE_WRITE_1H_FACTOR
        )
        rates["cache_write_5m_tokens"] = (
            entry.cache_write_cost_per_million
            if entry.cache_write_cost_per_million is not None
            else entry.input_cost_per_million
            * _ANTHROPIC_CACHE_WRITE_5M_FACTOR
        )
        notes.append("cache-write TTL split derived (P1 2026-08-03)")
    return _RateTable(
        rates=rates,
        request_rate=entry.request_cost,
        status="priced",
        source=entry.source,
        pricing_version=(
            f"{entry.pricing_version}+{USAGE_FACTS_PRICING_VERSION}"
            if entry.pricing_version
            else USAGE_FACTS_PRICING_VERSION
        ),
        notes=tuple(notes),
        reasoning_billing=entry.reasoning_billing,
    )


def _billable_components(usage: UsageFactsUsage) -> dict[str, int]:
    """Token amounts per component after the cache-write split rule.

    Input counting follows the row-wise, self-detecting rule of Canon
    register §5f: providers disagree on whether ``input_tokens`` already
    contains cached reads (OpenAI convention: yes; Anthropic: no;
    ``hermes_agent`` mixes both).  Without the correction the same cached
    tokens are billed twice — measured 2026-07-31 as a 6.3x cost error on
    the foreign lanes.  The cached share is subtracted only when it
    provably fits inside the reported input; Anthropic rows (cache reads
    far above uncached input) are untouched, as are cache-free runs.
    """
    input_tokens = int(usage.input_tokens)
    cache_read = int(usage.cache_read_tokens)
    billable_input = (
        input_tokens - cache_read
        if 0 < cache_read <= input_tokens
        else input_tokens
    )
    components = {
        "input_tokens": billable_input,
        "output_tokens": int(usage.output_tokens),
        "cache_read_tokens": cache_read,
        "reasoning_tokens": int(usage.reasoning_tokens),
    }
    split_present = (
        usage.cache_write_1h_tokens is not None
        or usage.cache_write_5m_tokens is not None
    )
    if split_present:
        components["cache_write_1h_tokens"] = int(
            usage.cache_write_1h_tokens or 0
        )
        components["cache_write_5m_tokens"] = int(
            usage.cache_write_5m_tokens or 0
        )
    else:
        components["cache_write_tokens"] = int(usage.cache_write_tokens)
    return components


def _estimate(
    model_name: str,
    usage: UsageArgument,
    *,
    provider: Optional[str],
    base_url: Optional[str],
    result_status: str,
    origin: Optional[str] = None,
) -> CostResult:
    resolved_model = resolve_model_alias(model_name)
    route = resolve_billing_route(
        resolved_model,
        provider=provider,
        base_url=base_url,
    )
    if result_status == "estimated" and route.billing_mode == "subscription_included":
        return CostResult(
            amount_usd=_ZERO,
            status="included",
            source="none",
            label="included",
            pricing_version="included-route",
        )

    table = _rate_table_for_route(route, origin)
    normalized = (
        usage
        if isinstance(usage, UsageFactsUsage)
        else UsageFactsUsage.from_canonical(usage)
    )
    components = _billable_components(normalized)

    missing: list[str] = []
    amount = _ZERO
    reasoning_included = table.reasoning_billing != "separate_rate"
    for component, tokens in components.items():
        rate = table.rates.get(component)
        if component == "reasoning_tokens" and reasoning_included:
            # Reasoning is already billed through output_tokens (upstream
            # PricingEntry semantics); an extra rate is optional here and a
            # missing one must not fail-closed the whole row.
            if rate is not None:
                amount += Decimal(tokens) * rate / _ONE_MILLION
            continue
        if tokens and rate is None:
            missing.append(component)
        elif rate is not None:
            amount += Decimal(tokens) * rate / _ONE_MILLION
    if normalized.request_count and table.request_rate is not None:
        amount += Decimal(normalized.request_count) * table.request_rate

    if table.status == "unknown" or missing:
        notes = list(table.notes)
        if missing:
            notes.append(
                "unpriced components: " + ", ".join(sorted(missing))
            )
        return CostResult(
            amount_usd=None,
            status="unknown",
            source=table.source,
            label="n/a",
            pricing_version=table.pricing_version,
            notes=tuple(notes),
        )

    label = (
        f"~${amount:.2f} list equivalent"
        if result_status == "equivalent"
        else f"~${amount:.2f}"
    )
    return CostResult(
        amount_usd=amount,
        status=result_status,
        source=table.source,
        label=label,
        pricing_version=table.pricing_version,
        notes=table.notes,
    )


def estimate_usage_cost(
    model_name: str,
    usage: UsageArgument,
    *,
    provider: Optional[str] = None,
    base_url: Optional[str] = None,
    origin: Optional[str] = None,
) -> CostResult:
    """Metered pricing: subscription routes stay $0 'included'."""
    return _estimate(
        model_name,
        usage,
        provider=provider,
        base_url=base_url,
        result_status="estimated",
        origin=origin,
    )


def estimate_equivalent_cost(
    model_name: str,
    usage: UsageArgument,
    *,
    provider: Optional[str] = None,
    base_url: Optional[str] = None,
    origin: Optional[str] = None,
) -> CostResult:
    """List-price equivalent for any route (Canon 7.2 comparison currency)."""
    return _estimate(
        model_name,
        usage,
        provider=provider,
        base_url=base_url,
        result_status="equivalent",
        origin=origin,
    )


# Rates whose non-existence is sourced, not merely an unpriced gap.
# Fail-closed stays: rows needing these components remain unknown — but
# the coverage report marks them "documented absent" instead of "open
# gap" (Canon register 7.5 classes; Grok round 2, 2026-08-03).
_DOCUMENTED_ABSENT_ROUTES: dict[tuple[str, str], str] = {
    ("openai", "codex-auto-review"): (
        "vendor price does not exist — Codex product feature, no public "
        "API model id (OpenAI pricing page; openai/codex#25395; "
        "Grok 2026-08-03)"
    ),
}
_DOCUMENTED_ABSENT_COMPONENTS: dict[tuple[str, str, str], str] = {
    ("moonshot", "k3", "cache_write_tokens"): (
        "separate write rate does not exist — fully automatic caching "
        "bills miss 3.00 / hit 0.30 only "
        "(platform.kimi.ai/docs/pricing/chat-k3; Grok 2026-08-03)"
    ),
    ("openai", "gpt-5.5", "cache_write_tokens"): (
        "cache-write line item does not exist "
        "(developers.openai.com/api/docs/pricing; Grok 2026-08-03)"
    ),
    ("xai", "grok-4.5", "cache_write_tokens"): (
        "cache-write line item does not exist "
        "(docs.x.ai/developers/pricing; Grok 2026-08-03)"
    ),
    ("alibaba", "qwen3.7-max", "cache_write_tokens"): (
        "implicit cache bills hits only; write rate does not exist "
        "(alibabacloud.com model-studio context-cache; Grok 2026-08-03)"
    ),
    ("alibaba", "qwen3.7-plus", "cache_write_tokens"): (
        "implicit cache bills hits only; write rate does not exist "
        "(alibabacloud.com model-studio context-cache; Grok 2026-08-03)"
    ),
    ("alibaba", "qwen3.6-flash", "cache_write_tokens"): (
        "implicit cache bills hits only; write rate does not exist "
        "(alibabacloud.com model-studio context-cache; Grok 2026-08-03)"
    ),
}


def _documented_absent_component(
    route: BillingRoute, component: str, origin: Optional[str]
) -> Optional[str]:
    reason = _DOCUMENTED_ABSENT_COMPONENTS.get(
        (route.provider, route.model.lower(), component)
    )
    if reason is None:
        return None
    # The qwen write-rate absence holds only on the implicit path; the
    # Anthropic-protocol origin sends cache_control and has write rates.
    if (
        route.provider == "alibaba"
        and component == "cache_write_tokens"
        and origin in _EXPLICIT_CACHE_ORIGINS
    ):
        return None
    return reason


def priceability(
    model_name: Optional[str],
    provider: Optional[str] = None,
    origin: Optional[str] = None,
) -> dict[str, Any]:
    """Report whether a (model, provider) pair is fully priceable.

    'Fully' means every component that can carry tokens has a sourced
    rate (Canon 7.4 mechanical gate). Where a rate is sourcedly absent,
    the pair is classified ``documented_absent`` (fail-closed unknown
    with negative proof) rather than ``open_gap``. Subscription
    eligibility is not a priceability question — the equivalent path
    prices every route.
    """
    base = {
        "resolved_provider": None,
        "resolved_model": None,
        "alias_of": None,
        "origin": origin,
    }
    if not model_name or not str(model_name).strip():
        return {
            **base,
            "priceable": False,
            "classification": "model_missing",
            "reason": "model not captured (harvest gap)",
        }
    if not provider or not str(provider).strip():
        resolved_model = resolve_model_alias(str(model_name))
        return {
            **base,
            "resolved_model": resolved_model,
            "priceable": False,
            "classification": "provider_missing",
            "reason": "provider not captured (harvest gap)",
        }
    resolved_model = resolve_model_alias(str(model_name))
    route = resolve_billing_route(resolved_model, provider=provider)
    base["resolved_provider"] = route.provider
    base["resolved_model"] = route.model
    base["alias_of"] = resolved_model if resolved_model != model_name else None
    table = _rate_table_for_route(route, origin)
    if table.status != "priced":
        documented = _DOCUMENTED_ABSENT_ROUTES.get(
            (route.provider, route.model.lower())
        )
        reason = table.notes[0] if table.notes else "no pricing entry"
        if documented is not None:
            reason = documented
        classification = (
            "documented_absent"
            if documented is not None or "does not exist" in reason
            else "open_gap"
        )
        return {**base, "priceable": False, "classification": classification, "reason": reason}
    required = [
        "input_tokens",
        "output_tokens",
        "cache_read_tokens",
        "cache_write_tokens",
    ]
    if route.provider == "anthropic":
        # Rows without an observed split fall back to the total column,
        # so the total rate must exist alongside the split rates.
        required.extend(["cache_write_1h_tokens", "cache_write_5m_tokens"])
    if table.reasoning_billing == "separate_rate":
        required.append("reasoning_tokens")
    missing = [name for name in required if table.rates.get(name) is None]
    if missing:
        documented_reasons = [
            _documented_absent_component(route, name, origin)
            for name in missing
        ]
        if all(documented_reasons):
            return {
                **base,
                "priceable": False,
                "classification": "documented_absent",
                "reason": "; ".join(
                    f"{name}: {reason}"
                    for name, reason in zip(missing, documented_reasons)
                ),
            }
        return {
            **base,
            "priceable": False,
            "classification": "open_gap",
            "reason": "missing rates: " + ", ".join(missing),
        }
    return {**base, "priceable": True, "classification": "priced", "reason": None}
