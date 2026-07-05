import os
import pytest
from hermes_cli import design_board_store as store


@pytest.fixture
def board(tmp_path, monkeypatch):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    # get_hermes_home reads HERMES_HOME fresh each call
    return store


def test_create_and_get_card(board):
    cid = board.create_card(kind="bug", title="Header overlaps", target={"view": "FleetView"})
    assert cid.startswith("c_")
    card = board.get_card(cid)
    assert card["kind"] == "bug"
    assert card["title"] == "Header overlaps"
    assert card["target"] == {"view": "FleetView"}
    assert card["status"] == "open"
    assert card["linked_tasks"] == []
    assert card["entries"] == []


def test_add_entry_with_pins(board):
    cid = board.create_card(kind="bug", title="x")
    eid = board.add_entry(
        cid, author="piet", kind="screenshot", note="misaligned",
        pins=[{"id": "p1", "x": 0.42, "y": 0.61, "note": "here"}],
        asset_name="e1-shot.png",
    )
    card = board.get_card(cid)
    assert len(card["entries"]) == 1
    entry = card["entries"][0]
    assert entry["id"] == eid
    assert entry["author"] == "piet"
    assert entry["asset"] == "assets/e1-shot.png"
    assert entry["pins"][0]["x"] == 0.42


def test_list_cards_returns_created(board):
    board.create_card(kind="wish", title="a")
    board.create_card(kind="bug", title="b")
    titles = {c["title"] for c in board.list_cards()}
    assert titles == {"a", "b"}


def test_set_status(board):
    cid = board.create_card(kind="bug", title="x")
    board.set_status(cid, "addressed")
    assert board.get_card(cid)["status"] == "addressed"


def test_get_missing_card_returns_none(board):
    assert board.get_card("c_deadbeef") is None
