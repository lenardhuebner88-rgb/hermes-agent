from __future__ import annotations

import sqlite3

from scripts import check_usage_facts_pricing_coverage as coverage


def test_buzz_agent_pricing_uses_source_native_origin(tmp_path, monkeypatch) -> None:
    database = tmp_path / "usage.db"
    with sqlite3.connect(database) as connection:
        connection.execute(
            """
            CREATE TABLE run_usage_facts (
                run_id TEXT PRIMARY KEY,
                origin TEXT NOT NULL,
                model TEXT,
                provider TEXT,
                input_tokens INTEGER,
                output_tokens INTEGER,
                cache_read_tokens INTEGER,
                cache_write_tokens INTEGER,
                captured_at TEXT
            )
            """
        )
        connection.execute(
            """
            INSERT INTO run_usage_facts VALUES (
                'claude_code:buzz-real-shape', 'buzz_agent', 'qwen3-coder-plus',
                'alibaba', 100, 10, 900, 25, '2026-08-03T20:00:00+00:00'
            )
            """
        )

    observed_origins: list[str] = []

    def fake_priceability(model, provider, origin):
        observed_origins.append(origin)
        return {
            "priceable": True,
            "reason": None,
            "resolved_provider": provider,
            "resolved_model": model,
            "alias_of": None,
            "classification": "priced",
        }

    monkeypatch.setattr(coverage, "priceability", fake_priceability)

    report = coverage.coverage_report(database, days=30)

    assert observed_origins == ["claude_code"]
    assert report["priced"][0]["origin"] == "buzz_agent"
    assert report["priced"][0]["source_origin"] == "claude_code"
