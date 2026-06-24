"""Unit tests for the strategist disposition-digest persistence (A3).

The Sonnet harvest step clusters the open follow-ups and persists its triage
decision via ``--mode digest``. ``write_disposition_digest`` validates +
normalizes that decision and stamps ``generated_at`` in Python (never trusting
an LLM-supplied timestamp); ``total_open``/``reaped`` are derived when the step
omits them. ``read_disposition_digest`` is the defensive dashboard reader.
"""
from __future__ import annotations

import json
import types
from pathlib import Path

import pytest

from hermes_cli import kanban_db as kb
from hermes_cli import strategist
from hermes_cli import strategist_surface as ss


@pytest.fixture
def digest_override(tmp_path, monkeypatch):
    """Isolate the digest path via the explicit override env (no real $HOME)."""
    target = tmp_path / "state" / "strategist" / "disposition_digest.json"
    monkeypatch.setenv("HERMES_STRATEGIST_DIGEST_PATH", str(target))
    return target


@pytest.fixture
def kanban_home(tmp_path, monkeypatch):
    """Isolated HERMES_HOME (mirrors test_strategist_harvest); no digest override
    so the CLI adapter and reader both resolve the default location under tmp."""
    home = tmp_path / ".hermes"
    home.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(home))
    monkeypatch.delenv("HERMES_STRATEGIST_DIGEST_PATH", raising=False)
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    kb.init_db()
    return home


def _payload() -> dict:
    return {
        "clusters": [
            {
                "theme": "test isolation",
                "item_ids": ["di_1", "di_2", "di_3"],
                "kind": "risk",
                "source_severity": "real-risk",
                "triage_severity": "overdue",
                "age_days": 3,
                "recommendation": "planspec",
                "planspec_key": "receipt-t_abc",
            },
            {
                "theme": "stray cleanup",
                "item_ids": ["di_4"],
                "kind": "follow_up",
                "source_severity": "none",
                "triage_severity": "none",
                "recommendation": "drop",
            },
        ],
        "left": [
            {
                "item_id": "di_5",
                "reason": "vague, no concrete action",
                "kind": "follow_up",
                "source_severity": "scope-note",
                "triage_severity": "scope-note",
                "age_days": 1,
                "disposition": "verworfen",
            },
        ],
    }


# ---------------------------------------------------------------------------
# Path resolution
# ---------------------------------------------------------------------------


def test_digest_path_env_override(digest_override):
    assert strategist.disposition_digest_path() == digest_override
    assert ss.disposition_digest_path() == digest_override


def test_digest_path_default_under_state_dir(kanban_home):
    expected = strategist.default_state_dir() / "disposition_digest.json"
    assert strategist.disposition_digest_path() == expected
    assert ss.disposition_digest_path() == expected


# ---------------------------------------------------------------------------
# write_disposition_digest — validation, stamping, derivation
# ---------------------------------------------------------------------------


def test_write_stamps_generated_at_and_persists(digest_override):
    path = strategist.write_disposition_digest(
        digest_override.parent, _payload(), now=1750000000
    )
    assert path == digest_override
    data = json.loads(path.read_text(encoding="utf-8"))
    assert data["generated_at"] == 1750000000
    assert len(data["clusters"]) == 2
    assert data["clusters"][0]["kind"] == "risk"
    assert data["clusters"][0]["source_severity"] == "real-risk"
    assert data["clusters"][0]["triage_severity"] == "overdue"
    assert data["clusters"][0]["severity"] == "overdue"
    assert data["clusters"][0]["age_days"] == 3
    assert data["clusters"][0]["recommendation"] == "planspec"
    assert data["clusters"][0]["planspec_key"] == "receipt-t_abc"
    assert data["left"][0]["item_id"] == "di_5"
    assert data["left"][0]["triage_severity"] == "scope-note"
    assert data["left"][0]["disposition"] == "verworfen"


def test_write_overrides_llm_supplied_generated_at(digest_override):
    payload = _payload()
    payload["generated_at"] = 1  # an LLM must not be able to backdate the digest
    path = strategist.write_disposition_digest(digest_override.parent, payload, now=999)
    assert json.loads(path.read_text())["generated_at"] == 999


def test_write_derives_total_open_and_reaped(digest_override):
    """total_open = distinct triaged items (clusters + left); reaped = planspec clusters."""
    path = strategist.write_disposition_digest(digest_override.parent, _payload(), now=1)
    data = json.loads(path.read_text())
    assert data["total_open"] == 5  # di_1..di_5 distinct
    assert data["reaped"] == 1  # exactly one cluster recommended → planspec


def test_write_honors_explicit_counts(digest_override):
    payload = _payload()
    payload["total_open"] = 42
    payload["reaped"] = 7
    path = strategist.write_disposition_digest(digest_override.parent, payload, now=1)
    data = json.loads(path.read_text())
    assert data["total_open"] == 42
    assert data["reaped"] == 7


def test_write_rejects_non_dict_payload(digest_override):
    with pytest.raises(ValueError):
        strategist.write_disposition_digest(digest_override.parent, ["not", "a", "dict"], now=1)


def test_write_rejects_unknown_recommendation(digest_override):
    payload = _payload()
    payload["clusters"][0]["recommendation"] = "escalate"  # not drop|collect|planspec
    with pytest.raises(ValueError):
        strategist.write_disposition_digest(digest_override.parent, payload, now=1)


def test_write_rejects_llm_reinterpreted_triage_severity(digest_override):
    payload = _payload()
    payload["clusters"][0]["triage_severity"] = "critical"
    with pytest.raises(ValueError):
        strategist.write_disposition_digest(digest_override.parent, payload, now=1)


def test_write_rejects_empty_theme(digest_override):
    payload = _payload()
    payload["clusters"][0]["theme"] = "  "
    with pytest.raises(ValueError):
        strategist.write_disposition_digest(digest_override.parent, payload, now=1)


def test_write_rejects_left_entry_without_item_id(digest_override):
    payload = _payload()
    payload["left"] = [{"reason": "no id"}]
    with pytest.raises(ValueError):
        strategist.write_disposition_digest(digest_override.parent, payload, now=1)


def test_write_accepts_empty_clusters_and_left(digest_override):
    """A run that reaped nothing still writes a transparent (empty) digest."""
    path = strategist.write_disposition_digest(
        digest_override.parent, {"clusters": [], "left": []}, now=1
    )
    data = json.loads(path.read_text())
    assert data["clusters"] == []
    assert data["left"] == []
    assert data["total_open"] == 0
    assert data["reaped"] == 0


def test_write_coerces_item_ids_to_strings(digest_override):
    payload = {
        "clusters": [
            {"theme": "x", "item_ids": [1, 2], "severity": "none", "recommendation": "collect"}
        ],
        "left": [],
    }
    path = strategist.write_disposition_digest(digest_override.parent, payload, now=1)
    data = json.loads(path.read_text())
    assert data["clusters"][0]["item_ids"] == ["1", "2"]


# ---------------------------------------------------------------------------
# read_disposition_digest — defensive reader
# ---------------------------------------------------------------------------


def test_read_returns_none_when_absent(digest_override):
    assert ss.read_disposition_digest() is None


def test_read_roundtrips_written_digest(digest_override):
    strategist.write_disposition_digest(digest_override.parent, _payload(), now=123)
    data = ss.read_disposition_digest()
    assert data is not None
    assert data["generated_at"] == 123
    assert data["total_open"] == 5


def test_read_bad_json_returns_none(digest_override):
    digest_override.parent.mkdir(parents=True, exist_ok=True)
    digest_override.write_text("{not json", encoding="utf-8")
    assert ss.read_disposition_digest() is None


# ---------------------------------------------------------------------------
# run_digest — CLI adapter
# ---------------------------------------------------------------------------


def test_run_digest_cli_adapter_persists_and_logs(kanban_home, tmp_path):
    drafts = tmp_path / "digest-in.json"
    drafts.write_text(json.dumps(_payload()), encoding="utf-8")
    args = types.SimpleNamespace(digest_file=str(drafts))
    result = strategist.run_digest(args)
    assert result["mode"] == "digest"
    assert result["clusters"] == 2
    assert result["reaped"] == 1
    assert result["total_open"] == 5
    # Persisted where the reader looks
    data = ss.read_disposition_digest()
    assert data is not None and data["total_open"] == 5
    # run-history line appended
    runs = strategist.read_last_runs(strategist.default_state_dir())
    assert runs.get("digest") is not None
    assert runs["digest"]["clusters"] == 2


def test_run_digest_requires_digest_file(kanban_home):
    args = types.SimpleNamespace(digest_file=None)
    with pytest.raises(FileNotFoundError):
        strategist.run_digest(args)
