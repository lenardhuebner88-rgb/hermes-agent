"""Persistente Bibliothek-Suchen und Themen-Follows."""

from __future__ import annotations

from pathlib import Path

import pytest

from hermes_cli import library_state as ls


@pytest.fixture
def hermes_home(tmp_path, monkeypatch):
    home = tmp_path / ".hermes"
    home.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(home))
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    return home


def test_saved_search_create_list_update_delete_roundtrip(hermes_home):
    created = ls.create_saved_search(
        name="KI Modelle täglich",
        query="frontier model releases",
        topic_tags=["KI-Modelle"],
        person_tags=["Piet"],
    )

    assert created["id"]
    assert created["name"] == "KI Modelle täglich"
    assert created["query"] == "frontier model releases"
    assert created["topic_tags"] == ["KI-Modelle"]
    assert created["person_tags"] == ["Piet"]
    assert created["created_at"] <= created["updated_at"]

    listing = ls.list_saved_searches()
    assert [s["name"] for s in listing] == ["KI Modelle täglich"]

    updated = ls.update_saved_search(created["id"], name="KI Modelle Woche", query="open weights")
    assert updated is not None
    assert updated["name"] == "KI Modelle Woche"
    assert updated["query"] == "open weights"
    assert updated["created_at"] == created["created_at"]
    assert updated["updated_at"] >= created["updated_at"]

    assert ls.delete_saved_search(created["id"]) is True
    assert ls.list_saved_searches() == []
    assert ls.delete_saved_search(created["id"]) is False


def test_topic_follow_unfollow_roundtrip_and_demo_seeds(hermes_home):
    topics = ls.list_topics()
    by_title = {topic["title"]: topic for topic in topics}

    for title in (
        "KI-Modelle",
        "WM 2026 Deutschland",
        "Hermes Dashboard",
        "Langfuse/LangSmith",
    ):
        assert title in by_title
        assert by_title[title]["followed"] is False
        assert by_title[title]["seeded"] is True

    topic_id = by_title["KI-Modelle"]["id"]
    followed = ls.set_topic_follow(topic_id, True)
    assert followed is not None
    assert followed["id"] == topic_id
    assert followed["followed"] is True
    assert followed["subscribed"] is True

    unfollowed = ls.set_topic_follow(topic_id, False)
    assert unfollowed is not None
    assert unfollowed["followed"] is False
    assert unfollowed["subscribed"] is False


def test_saved_search_validation_rejects_empty_name_or_query(hermes_home):
    with pytest.raises(ValueError):
        ls.create_saved_search(name="", query="frontier models")
    with pytest.raises(ValueError):
        ls.create_saved_search(name="KI", query="")


def test_topic_follow_unknown_topic_returns_none(hermes_home):
    assert ls.set_topic_follow("unknown-topic", True) is None


def test_unfollow_demo_topic_does_not_persist_when_never_followed(hermes_home):
    topics = {t["title"]: t for t in ls.list_topics()}
    topic_id = topics["KI-Modelle"]["id"]
    result = ls.set_topic_follow(topic_id, False)
    assert result is not None
    assert result["followed"] is False
    assert result["subscribed"] is False
    # Virtual demo topics stay virtual; only an actual follow change persists.
    assert ls._read_state()["topics"] == []
