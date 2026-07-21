"""Tests for the config-driven /control Stats field loader (hermes_cli.stats_config)."""
import textwrap

import pytest

import hermes_cli.stats_config as stats_config
from hermes_cli.stats_config import DEFAULT_STATS_CONFIG, load_stats_config


@pytest.fixture
def config_file(tmp_path, monkeypatch):
    """Point the loader at a temp YAML path and clear the module cache around the test."""
    path = tmp_path / "stats_fields.yaml"
    monkeypatch.setattr(stats_config, "stats_config_path", lambda: path)
    stats_config._reset_cache_for_tests()
    yield path
    stats_config._reset_cache_for_tests()


def test_missing_file_falls_back_to_defaults(config_file):
    assert not config_file.exists()
    cfg = load_stats_config(force=True)
    assert cfg == DEFAULT_STATS_CONFIG
    # Returned object is a copy — mutating it must not poison the module default.
    cfg["providers"].clear()
    assert len(DEFAULT_STATS_CONFIG["providers"]) == 5


def test_valid_yaml_is_parsed_and_normalized(config_file):
    config_file.write_text(
        textwrap.dedent(
            """
            version: 2
            providers:
              - id: anthropic
                label: Claude Custom
                lane: claude
                visible: false
              - id: openrouter
                label: OpenRouter
                # lane omitted -> API-billed (null)
            windows:
              - key: session
                label: 5h
                kind: session
            subscription_lanes:
              - key: claude
                label: Claude Abo
            """
        ),
        encoding="utf-8",
    )
    cfg = load_stats_config(force=True)
    assert cfg["version"] == 2
    assert cfg["providers"][0] == {
        "id": "anthropic",
        "label": "Claude Custom",
        "lane": "claude",
        "usage_role": "subscription",
        "visible": False,
    }
    # Omitted lane normalizes to None; visible defaults to True.
    assert cfg["providers"][1] == {
            "id": "openrouter",
            "label": "OpenRouter",
            "lane": None,
            "usage_role": "spend",
            "visible": True,
    }
    assert cfg["windows"] == [{"key": "session", "label": "5h", "kind": "session"}]
    assert cfg["subscription_lanes"] == [{"key": "claude", "label": "Claude Abo", "visible": True}]


def test_malformed_yaml_falls_back_to_defaults(config_file):
    config_file.write_text("providers: [this is: not valid: yaml", encoding="utf-8")
    assert load_stats_config(force=True) == DEFAULT_STATS_CONFIG


def test_empty_sections_backfill_from_defaults(config_file):
    # An empty/whitespace edit must never blank the tab — sections backfill.
    config_file.write_text("version: 1\nproviders: []\nwindows: []\nsubscription_lanes: []\n", encoding="utf-8")
    cfg = load_stats_config(force=True)
    assert cfg["providers"] == DEFAULT_STATS_CONFIG["providers"]
    assert cfg["windows"] == DEFAULT_STATS_CONFIG["windows"]
    assert cfg["subscription_lanes"] == DEFAULT_STATS_CONFIG["subscription_lanes"]


def test_malformed_entries_are_dropped(config_file):
    config_file.write_text(
        textwrap.dedent(
            """
            providers:
              - id: anthropic
                label: Claude
                lane: claude
              - label: "no id -> dropped"
              - "not a mapping -> dropped"
            windows:
              - key: weekly
                label: Woche
                kind: nonsense   # invalid kind -> coerced to "other"
            """
        ),
        encoding="utf-8",
    )
    cfg = load_stats_config(force=True)
    assert [p["id"] for p in cfg["providers"]] == ["anthropic"]
    assert cfg["windows"][0]["kind"] == "other"


def test_invalid_usage_role_uses_same_provider_heuristic_as_missing_role(config_file):
    config_file.write_text(
        textwrap.dedent(
            """
            providers:
              - {id: anthropic, label: Claude, lane: claude, usage_role: typo}
              - {id: openrouter, label: OpenRouter, lane: null, usage_role: typo}
            """
        ),
        encoding="utf-8",
    )
    cfg = load_stats_config(force=True)
    assert [provider["usage_role"] for provider in cfg["providers"]] == ["subscription", "spend"]


def test_mtime_change_reflects_without_force(config_file):
    # AC-2: editing the file changes /stats output without a code edit. The loader
    # re-reads as soon as the mtime changes, even inside the TTL window.
    config_file.write_text(
        "providers:\n  - {id: anthropic, label: First, lane: claude}\n", encoding="utf-8"
    )
    first = load_stats_config()  # cached (no force)
    assert first["providers"][0]["label"] == "First"

    import os

    config_file.write_text(
        "providers:\n  - {id: anthropic, label: Second, lane: claude}\n", encoding="utf-8"
    )
    # Bump mtime explicitly so the change is observable regardless of fs resolution.
    stat = config_file.stat()
    os.utime(config_file, (stat.st_atime, stat.st_mtime + 5))

    second = load_stats_config()  # still no force — relies on mtime invalidation
    assert second["providers"][0]["label"] == "Second"
