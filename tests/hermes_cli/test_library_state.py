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


def test_corrupt_saved_search_timestamps_are_failsoft(hermes_home):
    """Ein korruptes library_state.json mit nicht-numerischen Zeitstempeln
    darf list_saved_searches nicht crashen lassen."""
    ls.create_saved_search(name="Keep", query="foo")
    state = ls._read_state()
    state["saved_searches"][0]["created_at"] = "not-a-number"
    state["saved_searches"][0]["updated_at"] = "also-bad"
    ls._write_state(state)

    listing = ls.list_saved_searches()
    assert len(listing) == 1
    assert listing[0]["name"] == "Keep"
    assert listing[0]["created_at"] == 0
    assert listing[0]["updated_at"] == 0


def test_corrupt_topic_timestamps_are_failsoft(hermes_home):
    """Ein korruptes library_state.json mit nicht-numerischen Topic-Zeitstempeln
    darf list_topics nicht crashen lassen."""
    ls._write_state({
        "version": ls._STATE_VERSION,
        "saved_searches": [],
        "topics": [{"id": "ki-modelle", "title": "KI-Modelle", "followed": True,
                    "created_at": "bad", "updated_at": "worse"}],
    })

    topics = {t["id"]: t for t in ls.list_topics()}
    topic = topics["ki-modelle"]
    assert topic["followed"] is True
    assert topic["created_at"] == 0
    assert topic["updated_at"] == 0


# --- mutation-hardening tests (night-run 2026-07-29) ---


def test_saved_search_updated_at_falls_back_to_created_at(hermes_home):
    """Kill bool_op_swap L154: or -> and would lose the created_at fallback."""
    ls.create_saved_search(name="Fallback", query="q")
    state = ls._read_state()
    state["saved_searches"][0]["updated_at"] = 0
    state["saved_searches"][0]["created_at"] = 12345
    ls._write_state(state)

    listing = ls.list_saved_searches()
    assert listing[0]["updated_at"] == 12345


def test_saved_search_empty_name_filtered_out(hermes_home):
    """Kill bool_op_swap L161: and -> or would keep items with empty name."""
    ls.create_saved_search(name="Good", query="q")
    state = ls._read_state()
    state["saved_searches"].append({
        "id": "bad-search", "name": "", "query": "q",
        "created_at": 1, "updated_at": 1,
    })
    ls._write_state(state)

    listing = ls.list_saved_searches()
    assert len(listing) == 1
    assert listing[0]["name"] == "Good"


def test_topic_updated_at_falls_back_to_created_at(hermes_home):
    """Kill bool_op_swap L255: or -> and would lose the created_at fallback."""
    ls._write_state({
        "version": ls._STATE_VERSION,
        "saved_searches": [],
        "topics": [{"id": "ki-modelle", "title": "KI-Modelle", "followed": True,
                    "created_at": 99999, "updated_at": 0}],
    })

    topics = {t["id"]: t for t in ls.list_topics()}
    assert topics["ki-modelle"]["updated_at"] == 99999


def test_topic_empty_title_filtered_out(hermes_home):
    """Kill bool_op_swap L278: and -> or would keep topics with empty title."""
    ls._write_state({
        "version": ls._STATE_VERSION,
        "saved_searches": [],
        "topics": [
            {"id": "ki-modelle", "title": "KI-Modelle", "followed": True},
            {"id": "no-title", "title": "", "followed": True},
        ],
    })

    topics = {t["id"]: t for t in ls.list_topics()}
    assert "no-title" not in topics
    assert "ki-modelle" in topics


def test_was_persisted_checks_id_not_just_dict_type(hermes_home):
    """Kill bool_op_swap L300 (and->or): mutant treats any dict as persisted."""
    # State has a dict entry for a DIFFERENT topic; "ki-modelle" is not persisted.
    ls._write_state({
        "version": ls._STATE_VERSION,
        "saved_searches": [],
        "topics": [{"id": "agent-systems", "title": "Agent-Systeme", "followed": True}],
    })

    # Unfollowing a non-persisted demo topic must early-return (no persist).
    result = ls.set_topic_follow("ki-modelle", False)
    assert result is not None
    assert result["followed"] is False
    # State must NOT gain a "ki-modelle" entry.
    state = ls._read_state()
    persisted_ids = {t["id"] for t in state["topics"] if isinstance(t, dict)}
    assert "ki-modelle" not in persisted_ids


def test_unfollow_persisted_topic_updates_state(hermes_home):
    """Kill bool_op_swap L300 (or->and): was_persisted would be False, skipping persist."""
    ls._write_state({
        "version": ls._STATE_VERSION,
        "saved_searches": [],
        "topics": [{"id": "ki-modelle", "title": "KI-Modelle", "followed": True}],
    })

    result = ls.set_topic_follow("ki-modelle", False)
    assert result is not None
    assert result["followed"] is False
    # Must actually persist the unfollow, not early-return.
    state = ls._read_state()
    persisted_ids = [t["id"] for t in state["topics"] if isinstance(t, dict)]
    assert "ki-modelle" in persisted_ids
    matching = [t for t in state["topics"] if isinstance(t, dict) and t["id"] == "ki-modelle"]
    assert matching[0]["followed"] is False


def test_follow_new_demo_topic_persists(hermes_home):
    """Kill bool_op_swap L303: and -> or would early-return on new follows."""
    topics = {t["title"]: t for t in ls.list_topics()}
    topic_id = topics["KI-Modelle"]["id"]

    result = ls.set_topic_follow(topic_id, True)
    assert result is not None
    assert result["followed"] is True
    # Must persist, not early-return.
    state = ls._read_state()
    assert any(
        isinstance(t, dict) and t.get("id") == topic_id and t.get("followed")
        for t in state["topics"]
    )


def test_set_topic_follow_preserves_other_topics(hermes_home):
    """Kill bool_op_swap L311: or -> and would drop sibling topics."""
    ls._write_state({
        "version": ls._STATE_VERSION,
        "saved_searches": [],
        "topics": [
            {"id": "ki-modelle", "title": "KI-Modelle", "followed": True},
            {"id": "agent-systems", "title": "Agent-Systeme", "followed": True},
        ],
    })

    ls.set_topic_follow("ki-modelle", False)
    state = ls._read_state()
    persisted_ids = {t["id"] for t in state["topics"] if isinstance(t, dict)}
    assert "agent-systems" in persisted_ids
