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


def _qwen_cache_read_rate(input_rate: Decimal) -> Decimal:
    # Alibaba explicit context-cache read: 10% of the input rate
    # (P1, alibabacloud.com model-studio context-cache docs, 2026-08-03).
    return input_rate / Decimal("10")


def _fork_override(route: BillingRoute) -> Optional[_RateTable]:
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

    if key == ("alibaba", "qwen3.7-max"):
        # P1 list prices; the temporal 50% campaign is not used (Canon 7.2).
        input_rate = Decimal("2.50")
        return _RateTable(
            rates={
                "input_tokens": input_rate,
                "output_tokens": Decimal("7.50"),
                "cache_read_tokens": _qwen_cache_read_rate(input_rate),
                # No sourced cache-write rate — rows with cache-write
                # tokens stay unknown (fail-closed, Canon rule 3).
                "cache_write_tokens": None,
                "cache_write_1h_tokens": None,
                "cache_write_5m_tokens": None,
                "reasoning_tokens": None,
            },
            request_rate=None,
            status="priced",
            source=docs_source,
            pricing_version=version,
            notes=("cache-write rate unsourced; unknown where observed",),
        )

    if key == ("alibaba", "qwen3.6-flash"):
        # <=256K context tier; the 256K-1M tier is not derivable from the
        # persisted facts and the base tier is the sourced default.
        input_rate = Decimal("0.25")
        return _RateTable(
            rates={
                "input_tokens": input_rate,
                "output_tokens": Decimal("1.50"),
                "cache_read_tokens": _qwen_cache_read_rate(input_rate),
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
                "<=256K context tier; cache-write rate unsourced",
            ),
        )

    if key == ("alibaba", "qwen3.7-plus"):
        input_rate = Decimal("0.40")
        return _RateTable(
            rates={
                "input_tokens": input_rate,
                "output_tokens": Decimal("1.60"),
                "cache_read_tokens": _qwen_cache_read_rate(input_rate),
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
                "<=256K context tier; cache-write rate unsourced",
            ),
        )

    if key in {("alibaba", "qwen3.8-max-preview"), ("alibaba", "qwen3.8-max")}:
        # P1 (2026-08-03): no PAYG list rate exists for these models —
        # Token-Plan access only. Upstream carries a proxy entry for
        # qwen3.8-max-preview; the fork deliberately overrides it with
        # unknown instead of pricing against another model's rates.
        return _RateTable(
            rates={name: None for name in PRICE_COMPONENTS},
            request_rate=None,
            status="unknown",
            source="none",
            pricing_version=version,
            notes=(
                "no PAYG list rate (P1 2026-08-03); Token-Plan-exclusive",
            ),
        )

    return None


def _rate_table_for_route(route: BillingRoute) -> _RateTable:
    override = _fork_override(route)
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
    )


def _billable_components(usage: UsageFactsUsage) -> dict[str, int]:
    """Token amounts per component after the cache-write split rule."""
    components = {
        "input_tokens": int(usage.input_tokens),
        "output_tokens": int(usage.output_tokens),
        "cache_read_tokens": int(usage.cache_read_tokens),
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

    table = _rate_table_for_route(route)
    normalized = (
        usage
        if isinstance(usage, UsageFactsUsage)
        else UsageFactsUsage.from_canonical(usage)
    )
    components = _billable_components(normalized)

    missing: list[str] = []
    amount = _ZERO
    for component, tokens in components.items():
        rate = table.rates.get(component)
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
) -> CostResult:
    """Metered pricing: subscription routes stay $0 'included'."""
    return _estimate(
        model_name,
        usage,
        provider=provider,
        base_url=base_url,
        result_status="estimated",
    )


def estimate_equivalent_cost(
    model_name: str,
    usage: UsageArgument,
    *,
    provider: Optional[str] = None,
    base_url: Optional[str] = None,
) -> CostResult:
    """List-price equivalent for any route (Canon 7.2 comparison currency)."""
    return _estimate(
        model_name,
        usage,
        provider=provider,
        base_url=base_url,
        result_status="equivalent",
    )


def priceability(
    model_name: Optional[str],
    provider: Optional[str] = None,
) -> dict[str, Any]:
    """Report whether a (model, provider) pair is fully priceable.

    'Fully' means every component that can carry tokens has a sourced
    rate (Canon 7.4 mechanical gate). Subscription eligibility is not a
    priceability question — the equivalent path prices every route.
    """
    if not model_name or not str(model_name).strip():
        return {"priceable": False, "reason": "model_missing"}
    resolved_model = resolve_model_alias(str(model_name))
    route = resolve_billing_route(resolved_model, provider=provider)
    table = _rate_table_for_route(route)
    if table.status != "priced":
        reason = table.notes[0] if table.notes else "no pricing entry"
        return {
            "priceable": False,
            "reason": reason,
            "resolved_provider": route.provider,
            "resolved_model": route.model,
            "alias_of": resolved_model if resolved_model != model_name else None,
        }
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
    missing = [name for name in required if table.rates.get(name) is None]
    if missing:
        return {
            "priceable": False,
            "reason": "missing rates: " + ", ".join(missing),
            "resolved_provider": route.provider,
            "resolved_model": route.model,
            "alias_of": resolved_model if resolved_model != model_name else None,
        }
    return {
        "priceable": True,
        "reason": None,
        "resolved_provider": route.provider,
        "resolved_model": route.model,
        "alias_of": resolved_model if resolved_model != model_name else None,
    }
