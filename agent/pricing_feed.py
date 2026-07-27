"""Load the vendored LiteLLM model-price feed into Hermes pricing entries.

The vendored file deliberately keeps only priced chat models for the provider
families Hermes runs directly.  ``scripts/sync_model_prices.py`` owns that
filter and records the upstream URL, fetch timestamp, and a content-derived
pricing version in the file metadata.
"""

from __future__ import annotations

import json
from datetime import datetime
from decimal import Decimal
from pathlib import Path
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from agent.usage_pricing import PricingEntry

_ONE_MILLION = Decimal("1000000")
DEFAULT_VENDORED_PRICES_PATH = (
    Path(__file__).resolve().parent / "data" / "model_prices_vendored.json"
)

_PRICE_PROVIDER_BY_LITELLM_PROVIDER = {
    "anthropic": "anthropic",
    "openai": "openai",
    "moonshot": "moonshot",
    "xai": "xai",
    "dashscope": "alibaba",
}

_MODEL_PREFIX_BY_PRICE_PROVIDER = {
    "moonshot": "moonshot/",
    "xai": "xai/",
    "alibaba": "dashscope/",
}


def _per_token_to_per_million(value: Any) -> Decimal | None:
    if value is None:
        return None
    try:
        return Decimal(str(value)) * _ONE_MILLION
    except Exception:
        return None


def _parse_fetched_at(value: Any) -> datetime:
    if not isinstance(value, str) or not value.strip():
        raise ValueError("vendored pricing feed is missing _meta.fetched_at")
    parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if parsed.tzinfo is None:
        raise ValueError("vendored pricing feed fetched_at must include a timezone")
    return parsed


def _canonical_model_id(
    raw_model_id: str,
    *,
    price_provider: str,
) -> str | None:
    prefix = _MODEL_PREFIX_BY_PRICE_PROVIDER.get(price_provider)
    if prefix:
        if not raw_model_id.startswith(prefix):
            return None
        return raw_model_id[len(prefix) :].lower()
    if "/" in raw_model_id:
        return None
    return raw_model_id.lower()


def load_vendored_pricing(
    path: str | Path | None = None,
) -> dict[tuple[str, str], "PricingEntry"]:
    """Return canonical ``(provider, model)`` LiteLLM pricing entries.

    Every returned entry is traceable to the vendored feed version and carries
    the fetch timestamp recorded by the sync script.  Unknown or incomplete
    prices are omitted rather than converted to zero.
    """

    from agent.usage_pricing import PricingEntry

    feed_path = Path(path) if path is not None else DEFAULT_VENDORED_PRICES_PATH
    payload = json.loads(feed_path.read_text(encoding="utf-8"))
    if not isinstance(payload, dict):
        raise ValueError("vendored pricing feed must be a JSON object")
    metadata = payload.get("_meta")
    models = payload.get("models")
    if not isinstance(metadata, dict) or not isinstance(models, dict):
        raise ValueError("vendored pricing feed requires _meta and models objects")

    fetched_at = _parse_fetched_at(metadata.get("fetched_at"))
    pricing_version = metadata.get("pricing_version")
    source_url = metadata.get("source_url")
    if not isinstance(pricing_version, str) or not pricing_version:
        raise ValueError("vendored pricing feed is missing _meta.pricing_version")
    if not isinstance(source_url, str) or not source_url:
        raise ValueError("vendored pricing feed is missing _meta.source_url")

    entries: dict[tuple[str, str], PricingEntry] = {}
    for raw_model_id, raw in models.items():
        if not isinstance(raw_model_id, str) or not isinstance(raw, dict):
            continue
        litellm_provider = str(raw.get("litellm_provider") or "").lower()
        price_provider = _PRICE_PROVIDER_BY_LITELLM_PROVIDER.get(litellm_provider)
        if price_provider is None:
            continue
        model_id = _canonical_model_id(
            raw_model_id,
            price_provider=price_provider,
        )
        if model_id is None:
            continue

        input_rate = _per_token_to_per_million(raw.get("input_cost_per_token"))
        output_rate = _per_token_to_per_million(raw.get("output_cost_per_token"))
        if input_rate is None and output_rate is None:
            continue
        reasoning_rate = _per_token_to_per_million(
            raw.get("output_cost_per_reasoning_token")
        )
        reasoning_billing = (
            "separate_rate" if reasoning_rate is not None else "included_in_output"
        )
        entries[(price_provider, model_id)] = PricingEntry(
            input_cost_per_million=input_rate,
            output_cost_per_million=output_rate,
            cache_read_cost_per_million=_per_token_to_per_million(
                raw.get("cache_read_input_token_cost")
            ),
            cache_write_cost_per_million=_per_token_to_per_million(
                raw.get("cache_creation_input_token_cost")
            ),
            reasoning_billing=reasoning_billing,
            reasoning_cost_per_million=reasoning_rate,
            source="litellm_feed",
            source_url=str(raw.get("source") or source_url),
            pricing_version=pricing_version,
            fetched_at=fetched_at,
        )
    return entries

