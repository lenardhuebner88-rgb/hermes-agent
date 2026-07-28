from __future__ import annotations

import json
from datetime import datetime, timezone

import pytest

from scripts.daily_research_post import (
    DEFAULT_CHANNEL_ID,
    ResearchItem,
    format_daily_post,
    post_to_discord,
    select_research_items,
)


NOW = datetime(2026, 5, 28, 7, 0, tzinfo=timezone.utc)


def item(
    title: str,
    *,
    source: str = "Source",
    url: str | None = None,
    priority: str = "P2",
    published: datetime | None = None,
    summary: str = "",
) -> ResearchItem:
    return ResearchItem(
        title=title,
        source=source,
        url=url or f"https://example.test/{title.lower().replace(' ', '-')}",
        priority=priority,
        published=published or NOW,
        summary=summary,
    )


def test_select_research_items_dedupes_scores_and_limits_to_top_signals():
    older = datetime(2026, 5, 24, 7, 0, tzinfo=timezone.utc)
    candidates = [
        item(
            "OpenAI releases agent workflow evals",
            source="OpenAI",
            priority="P1",
            summary="agent eval benchmark with tool-calling",
        ),
        item(
            "OpenAI releases agent workflow evals",  # duplicate title should lose
            source="Mirror",
            priority="P3",
            summary="copied announcement",
        ),
        item(
            "LangGraph adds checkpointing for production agents",
            source="LangChain",
            priority="P1",
            summary="workflow reliability and orchestration",
        ),
        item(
            "GPU vendor quarterly results",
            source="Finance Blog",
            priority="P3",
            summary="market recap",
        ),
        item(
            "New MCP security guidance for tool boundaries",
            source="GitHub",
            priority="P2",
            summary="security hardening for mcp and tools",
        ),
        item(
            "Old but relevant agent benchmark",
            source="Archive",
            priority="P1",
            published=older,
            summary="agents benchmark",
        ),
        item(
            "Lightweight local model routing update",
            source="Hugging Face",
            priority="P2",
            summary="model routing and inference",
        ),
    ]

    selected = select_research_items(candidates, max_items=5, now=NOW)

    assert 3 <= len(selected) <= 5
    assert [entry.title for entry in selected].count("OpenAI releases agent workflow evals") == 1
    assert selected[0].title == "OpenAI releases agent workflow evals"
    assert "GPU vendor quarterly results" not in {entry.title for entry in selected}
    assert all(entry.system_impact for entry in selected)


def test_format_daily_post_uses_specified_german_structure_and_low_signal_fallback():
    selected = [
        item(
            "New MCP security guidance for tool boundaries",
            source="GitHub",
            priority="P2",
            summary="security hardening for MCP and tools",
        )
    ]
    selected = select_research_items(selected, max_items=5, now=NOW)

    message = format_daily_post(selected, generated_at=NOW, channel_id=DEFAULT_CHANNEL_ID)

    assert "🧭 Daily Research Radar" in message
    assert "1491150772224659649" in message
    assert "1. **New MCP security guidance for tool boundaries**" in message
    assert "Was bringt uns das im System?" in message
    assert "Quelle: GitHub" in message

    fallback = format_daily_post([], generated_at=NOW, channel_id=DEFAULT_CHANNEL_ID)
    assert "Heute kein belastbares Signal" in fallback
    assert "[SILENT]" not in fallback


def test_post_to_discord_uses_existing_send_message_contract_and_surfaces_errors():
    calls = []

    def fake_sender(payload):
        calls.append(payload)
        return json.dumps({"success": True, "platform": "discord"})

    result = post_to_discord("hello", channel_id="123", sender=fake_sender)

    assert result["success"] is True
    assert calls == [{"action": "send", "target": "discord:123", "message": "hello"}]

    def failing_sender(payload):
        return json.dumps({"error": "boom"})

    with pytest.raises(RuntimeError, match="boom"):
        post_to_discord("hello", channel_id="123", sender=failing_sender)


# ---------------------------------------------------------------------------
# Parsing helpers — None-safe and frozen
# ---------------------------------------------------------------------------

def test_source_config_is_frozen():
    """Source defaults are an immutable contract — a runtime mutation would
    silently change which feeds the next post reads."""
    from scripts.daily_research_post import SourceConfig

    source = SourceConfig("n", "u")
    with pytest.raises(AttributeError):
        source.name = "mutated"


def test_clean_text_and_parse_datetime_tolerate_none():
    """Feed fields are routinely absent; both helpers must answer the empty
    default instead of crashing the whole collection run."""
    from scripts.daily_research_post import _clean_text, _parse_datetime

    assert _clean_text(None) == ""
    assert _parse_datetime(None) is None


def test_xml_text_skips_missing_tags_and_uses_the_first_present():
    """A missing tag must be skipped, not crash on .text — RSS items omit
    optional fields all the time."""
    import xml.etree.ElementTree as ET

    from scripts.daily_research_post import _xml_text

    parent = ET.fromstring("<item><title>Hello &amp; more</title></item>")
    assert _xml_text(parent, ["missing", "title"]) == "Hello & more"
    assert _xml_text(parent, ["missing", "absent_too"]) == ""


def test_xml_link_falls_back_to_element_text_without_href():
    """RSS <link> carries the URL as TEXT (no href attribute); only Atom
    uses href. Requiring href would silently drop every RSS link."""
    import xml.etree.ElementTree as ET

    from scripts.daily_research_post import _xml_link

    rss = ET.fromstring("<item><link>https://example.com/a</link></item>")
    assert _xml_link(rss) == "https://example.com/a"
