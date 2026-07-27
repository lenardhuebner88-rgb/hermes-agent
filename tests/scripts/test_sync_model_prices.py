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
