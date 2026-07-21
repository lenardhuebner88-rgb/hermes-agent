"""Characterization tests for persisted-state integrity guards (TEST-ONLY).

Three readers that must degrade to a SAFE EMPTY value when their on-disk state
is missing or corrupt, instead of throwing or returning garbage:

* ``active_sessions._read_entries`` — the active-session registry is the
  concurrency guard. A corrupt registry that raises (or returns non-dicts)
  would either crash session acquisition or double-book sessions.
* ``active_sessions._pid_alive`` — decides whether a leased PID still owns its
  lease (with a create_time check so a recycled PID doesn't keep a stale lease).
  A silent wrong answer blocks new sessions or lets two sessions run at once.
* ``autoresearch_reconcile._suppressed_autoresearch_signals`` — the operator's
  veto set. Silent loss means the reconciler re-files tasks the operator killed.

These are side-effecting/concurrency-adjacent reads → tests only, never refactored.
"""
from __future__ import annotations

import json

from hermes_cli import active_sessions
from hermes_cli.autoresearch_reconcile import _suppressed_autoresearch_signals

# ─── active_sessions._read_entries ───────────────────────────────────────────


def test_read_entries_missing_file_returns_empty(tmp_path):
    assert active_sessions._read_entries(tmp_path / "nope.json") == []


def test_read_entries_corrupt_json_returns_empty(tmp_path):
    p = tmp_path / "registry.json"
    p.write_text("{not valid json", encoding="utf-8")
    assert active_sessions._read_entries(p) == []


def test_read_entries_wrapped_dict_returns_entries(tmp_path):
    p = tmp_path / "registry.json"
    p.write_text(json.dumps({"entries": [{"pid": 1}, {"pid": 2}]}), encoding="utf-8")
    assert active_sessions._read_entries(p) == [{"pid": 1}, {"pid": 2}]


def test_read_entries_top_level_list_is_accepted(tmp_path):
    p = tmp_path / "registry.json"
    p.write_text(json.dumps([{"pid": 1}]), encoding="utf-8")
    assert active_sessions._read_entries(p) == [{"pid": 1}]


def test_read_entries_non_list_payload_returns_empty(tmp_path):
    p = tmp_path / "registry.json"
    p.write_text(json.dumps({"entries": "oops"}), encoding="utf-8")
    assert active_sessions._read_entries(p) == []
    p.write_text(json.dumps("just a string"), encoding="utf-8")
    assert active_sessions._read_entries(p) == []


def test_read_entries_filters_out_non_dict_entries(tmp_path):
    p = tmp_path / "registry.json"
    p.write_text(json.dumps({"entries": [{"pid": 1}, "junk", 5, None]}), encoding="utf-8")
    assert active_sessions._read_entries(p) == [{"pid": 1}]


# ─── active_sessions._pid_alive ──────────────────────────────────────────────


def test_pid_alive_rejects_non_numeric_and_non_positive():
    assert active_sessions._pid_alive("abc") is False
    assert active_sessions._pid_alive(None) is False
    assert active_sessions._pid_alive(0) is False
    assert active_sessions._pid_alive(-3) is False


def test_pid_alive_false_when_pid_does_not_exist(monkeypatch):
    monkeypatch.setattr("gateway.status._pid_exists", lambda pid: False)
    assert active_sessions._pid_alive(12345) is False


def test_pid_alive_false_when_pid_exists_lookup_raises(monkeypatch):
    def _boom(pid):
        raise RuntimeError("cannot probe")

    monkeypatch.setattr("gateway.status._pid_exists", _boom)
    assert active_sessions._pid_alive(12345) is False


def test_pid_alive_true_when_exists_and_no_start_time_given(monkeypatch):
    monkeypatch.setattr("gateway.status._pid_exists", lambda pid: True)
    assert active_sessions._pid_alive(12345) is True


def test_pid_alive_true_when_exists_and_start_time_unreadable(monkeypatch):
    # If psutil can't read create_time, don't evict the lease (fail-open).
    monkeypatch.setattr("gateway.status._pid_exists", lambda pid: True)
    monkeypatch.setattr(active_sessions, "_process_start_time", lambda pid: None)
    assert active_sessions._pid_alive(12345, process_start_time=1000.0) is True


def test_pid_alive_compares_create_time_for_recycled_pid(monkeypatch):
    monkeypatch.setattr("gateway.status._pid_exists", lambda pid: True)
    # Matching create_time → same process → alive.
    monkeypatch.setattr(active_sessions, "_process_start_time", lambda pid: 1000.0)
    assert active_sessions._pid_alive(12345, process_start_time=1000.0) is True
    # Different create_time → recycled PID → the stale lease is NOT alive.
    monkeypatch.setattr(active_sessions, "_process_start_time", lambda pid: 2000.0)
    assert active_sessions._pid_alive(12345, process_start_time=1000.0) is False


# ─── autoresearch_reconcile._suppressed_autoresearch_signals ─────────────────


def _write_vetoed(tmp_path, content: str):
    p = tmp_path / "vetoed_levers.json"
    p.write_text(content, encoding="utf-8")
    return str(p)


def test_vetoed_missing_file_returns_empty_set(tmp_path, monkeypatch):
    monkeypatch.setenv("HERMES_STRATEGIST_VETOED_PATH", str(tmp_path / "absent.json"))
    assert _suppressed_autoresearch_signals() == set()


def test_vetoed_corrupt_json_returns_empty_set(tmp_path, monkeypatch):
    monkeypatch.setenv("HERMES_STRATEGIST_VETOED_PATH", _write_vetoed(tmp_path, "{broken"))
    assert _suppressed_autoresearch_signals() == set()


def test_vetoed_non_list_payload_returns_empty_set(tmp_path, monkeypatch):
    monkeypatch.setenv("HERMES_STRATEGIST_VETOED_PATH", _write_vetoed(tmp_path, '{"a": 1}'))
    assert _suppressed_autoresearch_signals() == set()


def test_vetoed_extracts_prefixed_signals_normalized(tmp_path, monkeypatch):
    payload = json.dumps(
        [
            "autoresearch:lever_one",      # kept
            "  AUTORESEARCH: Lever_Two  ",  # normalized: lower + strip
            "autoresearch:",                # empty after prefix → dropped
            "unrelated:thing",              # wrong prefix → dropped
            None,                            # falsy → dropped
        ]
    )
    monkeypatch.setenv("HERMES_STRATEGIST_VETOED_PATH", _write_vetoed(tmp_path, payload))
    assert _suppressed_autoresearch_signals() == {"lever_one", "lever_two"}
