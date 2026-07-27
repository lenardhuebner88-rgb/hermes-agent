#!/usr/bin/env python3
"""Refresh Hermes' filtered vendored copy of the LiteLLM pricing feed.

Filter contract:

* only priced chat/text models are retained;
* only the direct Hermes price-provider families Anthropic, OpenAI, Moonshot,
  xAI, Alibaba/DashScope, and Z.AI are retained;
* provider-specific duplicates (Azure, Bedrock, Vertex, hosted replicas) are
  excluded because Hermes prices those routes separately;
* official-doc snapshots in ``agent.usage_pricing`` are never read or written,
  so they remain the higher-priority override layer.

The script downloads into memory and writes atomically only after a complete,
valid response.  Network or validation failure leaves the vendored file
untouched.  The pricing version is derived from the selected price fields, so
any changed historical price necessarily creates a new version.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import sys
import tempfile
import urllib.request
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

DEFAULT_SOURCE_URL = (
    "https://raw.githubusercontent.com/BerriAI/litellm/main/"
    "model_prices_and_context_window.json"
)
DEFAULT_OUTPUT = (
    Path(__file__).resolve().parents[1]
    / "agent"
    / "data"
    / "model_prices_vendored.json"
)

_ALLOWED_MODEL_PREFIXES = {
    "moonshot": "moonshot/",
    "xai": "xai/",
    "dashscope": "dashscope/",
    "zai": "zai/",
}
_ALLOWED_BARE_PROVIDERS = {"anthropic", "openai"}
_PRICE_FIELDS = (
    "input_cost_per_token",
    "output_cost_per_token",
    "cache_read_input_token_cost",
    "cache_creation_input_token_cost",
    "output_cost_per_reasoning_token",
)


def download_feed(url: str, *, timeout: float = 30.0) -> dict[str, Any]:
    request = urllib.request.Request(
        url,
        headers={"User-Agent": "Hermes-LiteLLM-price-sync/1"},
    )
    with urllib.request.urlopen(request, timeout=timeout) as response:
        raw = response.read()
    payload = json.loads(raw)
    if not isinstance(payload, dict) or len(payload) < 100:
        raise ValueError("LiteLLM feed response is not a complete model-price map")
    return payload


def select_models(feed: dict[str, Any]) -> dict[str, dict[str, Any]]:
    selected: dict[str, dict[str, Any]] = {}
    for model_id, raw in feed.items():
        if not isinstance(model_id, str) or not isinstance(raw, dict):
            continue
        provider = str(raw.get("litellm_provider") or "").lower()
        if provider in _ALLOWED_BARE_PROVIDERS:
            if "/" in model_id:
                continue
        else:
            prefix = _ALLOWED_MODEL_PREFIXES.get(provider)
            if prefix is None or not model_id.startswith(prefix):
                continue
        if not any(raw.get(field) is not None for field in _PRICE_FIELDS):
            continue
        mode = str(raw.get("mode") or "chat").lower()
        if mode not in {"chat", "text-completion", "responses"}:
            continue
        selected[model_id] = raw
    if not selected:
        raise ValueError("LiteLLM provider filter selected no priced models")
    return dict(sorted(selected.items()))


def price_snapshot(models: dict[str, dict[str, Any]]) -> dict[str, dict[str, Any]]:
    return {
        model_id: {
            field: raw.get(field)
            for field in _PRICE_FIELDS
            if raw.get(field) is not None
        }
        for model_id, raw in sorted(models.items())
    }


def pricing_version(models: dict[str, dict[str, Any]]) -> str:
    encoded = json.dumps(
        price_snapshot(models),
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    return f"litellm-{hashlib.sha256(encoded).hexdigest()[:16]}"


def build_payload(
    feed: dict[str, Any],
    *,
    source_url: str,
    fetched_at: datetime | None = None,
) -> dict[str, Any]:
    models = select_models(feed)
    timestamp = fetched_at or datetime.now(timezone.utc)
    return {
        "_meta": {
            "source": "BerriAI/litellm model_prices_and_context_window.json",
            "source_url": source_url,
            "fetched_at": timestamp.isoformat(),
            "pricing_version": pricing_version(models),
            "filter": (
                "priced chat/text models for anthropic, openai, moonshot, "
                "xai, dashscope, and zai direct-provider routes"
            ),
        },
        "models": models,
    }


def load_existing(path: Path) -> dict[str, Any] | None:
    if not path.exists():
        return None
    payload = json.loads(path.read_text(encoding="utf-8"))
    return payload if isinstance(payload, dict) else None


def iter_price_diff(
    old_payload: dict[str, Any] | None,
    new_payload: dict[str, Any],
) -> list[str]:
    old_models = (
        old_payload.get("models", {})
        if isinstance(old_payload, dict)
        else {}
    )
    new_models = new_payload["models"]
    old_prices = price_snapshot(old_models) if isinstance(old_models, dict) else {}
    new_prices = price_snapshot(new_models)
    lines: list[str] = []
    for model_id in sorted(set(old_prices) | set(new_prices)):
        before = old_prices.get(model_id)
        after = new_prices.get(model_id)
        if before != after:
            lines.append(
                f"{model_id}: "
                f"{json.dumps(before, sort_keys=True)} -> "
                f"{json.dumps(after, sort_keys=True)}"
            )
    return lines


def write_atomic(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    rendered = json.dumps(payload, indent=2, sort_keys=True) + "\n"
    with tempfile.NamedTemporaryFile(
        "w",
        encoding="utf-8",
        dir=path.parent,
        prefix=f".{path.name}.",
        delete=False,
    ) as handle:
        handle.write(rendered)
        temp_path = Path(handle.name)
    os.replace(temp_path, path)


def sync(*, source_url: str, output: Path, timeout: float = 30.0) -> int:
    try:
        feed = download_feed(source_url, timeout=timeout)
        new_payload = build_payload(feed, source_url=source_url)
        old_payload = load_existing(output)
    except Exception as exc:
        print(f"pricing sync failed; vendored file unchanged: {exc}", file=sys.stderr)
        return 1

    changed = iter_price_diff(old_payload, new_payload)
    if not changed:
        print("No price changes; vendored file unchanged.")
        return 0
    print("Changed LiteLLM prices:")
    for line in changed:
        print(f"  {line}")
    write_atomic(output, new_payload)
    print(
        f"Wrote {len(new_payload['models'])} models to {output} "
        f"as {new_payload['_meta']['pricing_version']}."
    )
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source-url", default=DEFAULT_SOURCE_URL)
    parser.add_argument("--output", type=Path, default=DEFAULT_OUTPUT)
    parser.add_argument("--timeout", type=float, default=30.0)
    args = parser.parse_args()
    return sync(
        source_url=args.source_url,
        output=args.output,
        timeout=args.timeout,
    )


if __name__ == "__main__":
    raise SystemExit(main())
