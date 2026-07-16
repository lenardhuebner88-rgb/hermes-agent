"""Regression tests for _subscription_for_spawn_identity mapping.

Covers AC-1..AC-4 of t_2023cb7b.
"""

from __future__ import annotations

import json
import pytest
from hermes_cli import kanban_db as kb


def _fake_lane_xai_oauth_grok(profile: str):
    return {
        "worker_runtime": "hermes",
        "provider": "xai-oauth",
        "model": "grok-4.5",
    }


def _fake_lane_kimi_coding(profile: str):
    return {
        "worker_runtime": "hermes",
        "provider": "kimi-coding",
        "model": "kimi-k2.7-code",
    }


def _fake_lane_unknown(profile: str):
    return {
        "worker_runtime": "hermes",
        "provider": "openrouter",
        "model": "openai/gpt-5-mini",
    }


@pytest.mark.parametrize("alias", ["xai-oauth", "xai", "grok"])
def test_spawn_identity_subscription_xai_family_maps_to_grok(kanban_home, monkeypatch, alias):
    """AC-1: xai-oauth (and aliases) stamps as grok subscription via real spawn path."""
    monkeypatch.setattr(
        kb, "_active_lane_entry_for_profile_from_conn",
        lambda conn, profile: {
            "worker_runtime": "hermes",
            "provider": alias,
            "model": "grok-4.5",
        },
    )
    # Disable profile fallback so the mapping is exercised.
    monkeypatch.setattr(kb, "_profile_subscription", lambda profile: None)

    with kb.connect_closing() as conn:
        tid = kb.create_task(conn, title=f"xai-{alias}-claim", assignee="research")
        assert kb.claim_task(conn, tid)
        row = conn.execute(
            "SELECT metadata FROM task_runs WHERE task_id = ?",
            (tid,),
        ).fetchone()
        meta = json.loads(row["metadata"])
        assert meta["provider"] == alias
        assert meta["model"] == "grok-4.5"
        assert meta["billing_mode"] == "subscription_included"
        assert meta["cost_source"] == "dispatch_subscription_stamp"
        assert meta["subscription"] == "grok"


def test_spawn_identity_subscription_kimi_coding_maps_to_kimi_without_profile_fallback(
    kanban_home, monkeypatch
):
    """AC-2: kimi-coding maps to kimi subscription even when profile has no subscription."""
    monkeypatch.setattr(
        kb, "_active_lane_entry_for_profile_from_conn",
        lambda conn, profile: {
            "worker_runtime": "hermes",
            "provider": "kimi-coding",
            "model": "kimi-k2.7-code",
        },
    )
    monkeypatch.setattr(kb, "_profile_subscription", lambda profile: None)

    with kb.connect_closing() as conn:
        tid = kb.create_task(conn, title="kimi-coding-claim", assignee="coder")
        assert kb.claim_task(conn, tid)
        row = conn.execute(
            "SELECT metadata FROM task_runs WHERE task_id = ?",
            (tid,),
        ).fetchone()
        meta = json.loads(row["metadata"])
        assert meta["provider"] == "kimi-coding"
        assert meta["model"] == "kimi-k2.7-code"
        assert meta["billing_mode"] == "subscription_included"
        assert meta["cost_source"] == "dispatch_subscription_stamp"
        assert meta["subscription"] == "kimi"


@pytest.mark.parametrize(
    "provider,model,expected_subscription",
    [
        ("openai-codex", "gpt-5.5", "chatgpt"),
        ("codex", "gpt-5.5", "chatgpt"),
        ("chatgpt", "gpt-5.5", "chatgpt"),
        ("openai-chatgpt", "gpt-5.5", "chatgpt"),
        ("claude", "claude-fable-5", "claude"),
        ("anthropic", "claude-fable-5", "claude"),
        ("kimi", "kimi-k2.7", "kimi"),
        ("moonshot", "kimi-k2.7", "kimi"),
    ],
)
def test_spawn_identity_existing_mappings_unchanged(
    kanban_home, monkeypatch, provider, model, expected_subscription
):
    """AC-3: existing mappings remain byte-identical in behavior."""
    monkeypatch.setattr(
        kb, "_active_lane_entry_for_profile_from_conn",
        lambda conn, profile: {
            "worker_runtime": "hermes",
            "provider": provider,
            "model": model,
        },
    )
    monkeypatch.setattr(kb, "_profile_subscription", lambda profile: None)

    with kb.connect_closing() as conn:
        tid = kb.create_task(conn, title=f"reg-{provider}-claim", assignee="coder")
        assert kb.claim_task(conn, tid)
        row = conn.execute(
            "SELECT metadata FROM task_runs WHERE task_id = ?",
            (tid,),
        ).fetchone()
        meta = json.loads(row["metadata"])
        assert meta["billing_mode"] == "subscription_included"
        assert meta["cost_source"] == "dispatch_subscription_stamp"
        assert meta["subscription"] == expected_subscription


def test_spawn_identity_unknown_provider_remains_metered(kanban_home, monkeypatch):
    """AC-4: an unknown provider without profile subscription stays metered."""
    monkeypatch.setattr(
        kb, "_active_lane_entry_for_profile_from_conn",
        lambda conn, profile: {
            "worker_runtime": "hermes",
            "provider": "openrouter",
            "model": "openai/gpt-5-mini",
        },
    )
    monkeypatch.setattr(kb, "_profile_subscription", lambda profile: None)

    with kb.connect_closing() as conn:
        tid = kb.create_task(conn, title="unknown-claim", assignee="verifier")
        assert kb.claim_task(conn, tid)
        row = conn.execute(
            "SELECT metadata FROM task_runs WHERE task_id = ?",
            (tid,),
        ).fetchone()
        meta = json.loads(row["metadata"])
        assert meta["billing_mode"] == "metered"
        assert meta["cost_source"] == "dispatch_metered_stamp"
        assert "subscription" not in meta


def test_spawn_identity_xai_oauth_prefers_mapping_over_profile_fallback(
    kanban_home, monkeypatch
):
    """xai-oauth mapping wins even if a profile would supply a different subscription."""
    monkeypatch.setattr(
        kb, "_active_lane_entry_for_profile_from_conn",
        lambda conn, profile: {
            "worker_runtime": "hermes",
            "provider": "xai-oauth",
            "model": "grok-4.5",
        },
    )
    # Profile fallback says something else; mapping must still win.
    monkeypatch.setattr(kb, "_profile_subscription", lambda profile: "claude")

    with kb.connect_closing() as conn:
        tid = kb.create_task(conn, title="xai-priority-claim", assignee="research")
        assert kb.claim_task(conn, tid)
        row = conn.execute(
            "SELECT metadata FROM task_runs WHERE task_id = ?",
            (tid,),
        ).fetchone()
        meta = json.loads(row["metadata"])
        assert meta["subscription"] == "grok"
        assert meta["billing_mode"] == "subscription_included"
