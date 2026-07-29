from __future__ import annotations

from datetime import datetime, timezone

from scripts import sync_model_prices


def _feed(price: float = 0.000001) -> dict:
    return {
        "xai/grok-test": {
            "litellm_provider": "xai",
            "mode": "chat",
            "input_cost_per_token": price,
            "output_cost_per_token": price * 2,
        }
    }


def test_filter_includes_direct_zai_models_but_not_hosted_replicas():
    feed = {
        "zai/glm-5.1": {
            "litellm_provider": "zai",
            "mode": "chat",
            "input_cost_per_token": 0.0000014,
            "output_cost_per_token": 0.0000044,
        },
        "cloudflare/@cf/zai-org/glm-5.2": {
            "litellm_provider": "cloudflare",
            "mode": "chat",
            "input_cost_per_token": 0.0000014,
            "output_cost_per_token": 0.0000044,
        },
    }

    selected = sync_model_prices.select_models(feed)

    assert set(selected) == {"zai/glm-5.1"}


def test_sync_prints_price_diff_and_changes_version(tmp_path, monkeypatch, capsys):
    output = tmp_path / "prices.json"
    before = sync_model_prices.build_payload(
        _feed(),
        source_url="https://example.invalid/feed.json",
        fetched_at=datetime(2026, 7, 26, tzinfo=timezone.utc),
    )
    sync_model_prices.write_atomic(output, before)
    monkeypatch.setattr(
        sync_model_prices,
        "download_feed",
        lambda *args, **kwargs: _feed(0.000002),
    )

    result = sync_model_prices.sync(
        source_url="https://example.invalid/feed.json",
        output=output,
    )

    captured = capsys.readouterr()
    after = sync_model_prices.load_existing(output)
    assert result == 0
    assert "Changed LiteLLM prices:" in captured.out
    assert "xai/grok-test" in captured.out
    assert after is not None
    assert after["_meta"]["pricing_version"] != before["_meta"]["pricing_version"]


def test_sync_failure_does_not_touch_existing_file(tmp_path, monkeypatch, capsys):
    output = tmp_path / "prices.json"
    output.write_text("sentinel\n", encoding="utf-8")
    monkeypatch.setattr(
        sync_model_prices,
        "download_feed",
        lambda *args, **kwargs: (_ for _ in ()).throw(OSError("offline")),
    )

    result = sync_model_prices.sync(
        source_url="https://example.invalid/feed.json",
        output=output,
    )

    captured = capsys.readouterr()
    assert result == 1
    assert "vendored file unchanged" in captured.err
    assert output.read_text(encoding="utf-8") == "sentinel\n"


# ---------------------------------------------------------------------------
# Contract pinning
# ---------------------------------------------------------------------------

def test_download_feed_requires_a_complete_map(monkeypatch):
    """The feed must be a dict with >=100 entries — a truncated response
    (list payload or a 100-boundary short map) would silently reprice the
    board from a partial source."""
    import io
    import json as _json
    import urllib.request

    def fake_urlopen_factory(payload):
        class _Resp(io.BytesIO):
            status = 200

            def __enter__(self):
                return self

            def __exit__(self, *exc):
                return False

        def fake_urlopen(request, timeout=None):
            return _Resp(_json.dumps(payload).encode("utf-8"))

        return fake_urlopen

    # exactly 100 entries: complete (boundary is strict)
    full = {f"m{i}": {"litellm_provider": "xai"} for i in range(100)}
    monkeypatch.setattr(urllib.request, "urlopen", fake_urlopen_factory(full))
    assert len(sync_model_prices.download_feed("https://x")) == 100

    # a list payload is never a price map — even a long one (>=100 entries)
    monkeypatch.setattr(
        urllib.request, "urlopen", fake_urlopen_factory([{"i": i} for i in range(150)])
    )
    try:
        sync_model_prices.download_feed("https://x")
    except ValueError:
        pass
    else:
        raise AssertionError("list payload must be rejected")


def test_select_models_defaults_mode_to_chat_and_requires_a_price(monkeypatch):
    """A model without 'mode' defaults to chat and is selectable; a model
    with NO price field at all carries nothing to vendor and is dropped."""
    feed = {
        "xai/no-mode": {
            "litellm_provider": "xai",
            "input_cost_per_token": 0.000001,
        },
        "xai/no-prices": {
            "litellm_provider": "xai",
            "mode": "chat",
        },
    }
    selected = sync_model_prices.select_models(feed)
    assert "xai/no-mode" in selected
    assert "xai/no-prices" not in selected


def test_build_payload_uses_now_when_fetched_at_is_absent():
    """fetched_at=None means 'now' — the payload must still carry a
    parseable ISO timestamp, never crash on None."""
    payload = sync_model_prices.build_payload(
        _feed(), source_url="https://x", fetched_at=None
    )
    assert datetime.fromisoformat(payload["_meta"]["fetched_at"])


def test_pricing_version_is_stable_under_field_insertion_order():
    """pricing_version must be stable under field-insertion order — the
    same prices inserted in a different order (input first vs. cache_read
    first) must hash identically, or a metadata-only feed reshuffle would
    flip the version string with no price change behind it."""
    feed_input_first = {
        "xai/grok-test": {
            "litellm_provider": "xai",
            "mode": "chat",
            "input_cost_per_token": 0.000001,
            "cache_read_input_token_cost": 0.0000001,
        }
    }
    feed_cache_read_first = {
        "xai/grok-test": {
            "litellm_provider": "xai",
            "mode": "chat",
            "cache_read_input_token_cost": 0.0000001,
            "input_cost_per_token": 0.000001,
        }
    }
    models_input_first = sync_model_prices.select_models(feed_input_first)
    models_cache_read_first = sync_model_prices.select_models(feed_cache_read_first)
    assert sync_model_prices.pricing_version(
        models_input_first
    ) == sync_model_prices.pricing_version(models_cache_read_first)
