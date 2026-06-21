"""Tests for ``hermes memory digest`` — the weekly decision extract.

Covers the two halves the digest rests on:

* ``normalize_completion_metadata`` — the canonical completion-metadata
  schema (``decisions[]``, ``operator_followup``, ``supersedes[]``) that
  workers report and the digest reads. Lenient on input (string shorthand,
  lone values), strict on output (lists of canonical records), preserves
  every unrelated key.
* ``build_digest`` / ``render_digest`` — receipt-first extraction over a
  window of completed runs into decisions, open operator follow-ups, and
  superseded items.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

# Ensure the worktree (not the stale global clone) is first on sys.path.
_WORKTREE = Path(__file__).resolve().parents[2]
if str(_WORKTREE) not in sys.path:
    sys.path.insert(0, str(_WORKTREE))

from hermes_cli import memory_digest as md  # noqa: E402


DAY = 86400


# --------------------------------------------------------------------------- #
# normalize_completion_metadata                                               #
# --------------------------------------------------------------------------- #


def test_string_decisions_become_canonical_records():
    out = md.normalize_completion_metadata({"decisions": ["Pin pymilvus", "Cap workers at 5"]})
    assert out["decisions"] == [
        {"text": "Pin pymilvus", "scope": None, "supersedes": []},
        {"text": "Cap workers at 5", "scope": None, "supersedes": []},
    ]


def test_lone_string_decision_is_wrapped_in_a_list():
    out = md.normalize_completion_metadata({"decisions": "Single decision"})
    assert out["decisions"] == [{"text": "Single decision", "scope": None, "supersedes": []}]


def test_dict_decision_keys_are_canonicalized():
    out = md.normalize_completion_metadata(
        {"decisions": [{"decision": "Use SQLite", "scope": "global", "supersedes": "t_old"}]}
    )
    assert out["decisions"] == [
        {"text": "Use SQLite", "scope": "global", "supersedes": ["t_old"]}
    ]


def test_decision_text_read_from_summary_or_text_alias():
    out = md.normalize_completion_metadata(
        {"decisions": [{"summary": "From summary key"}, {"text": "From text key"}]}
    )
    assert [d["text"] for d in out["decisions"]] == ["From summary key", "From text key"]


def test_empty_or_textless_decisions_are_dropped():
    out = md.normalize_completion_metadata(
        {"decisions": ["", "  ", None, {"scope": "global"}, "Real one"]}
    )
    assert out["decisions"] == [{"text": "Real one", "scope": None, "supersedes": []}]


def test_operator_followup_string_becomes_list():
    out = md.normalize_completion_metadata({"operator_followup": "Restart the gateway"})
    assert out["operator_followup"] == ["Restart the gateway"]


def test_operator_followup_list_filters_blanks():
    out = md.normalize_completion_metadata({"operator_followup": ["do x", "", "  ", "do y"]})
    assert out["operator_followup"] == ["do x", "do y"]


def test_top_level_supersedes_string_becomes_list():
    out = md.normalize_completion_metadata({"supersedes": "t_1234"})
    assert out["supersedes"] == ["t_1234"]


def test_unrelated_keys_are_preserved_untouched():
    meta = {
        "decisions": ["a"],
        "changed_files": ["foo.py"],
        "tests_run": 5,
        "residual_risk": "none",
        "commit": "abc123",
        "usage": {"input_tokens": 10},
    }
    out = md.normalize_completion_metadata(meta)
    assert out["changed_files"] == ["foo.py"]
    assert out["tests_run"] == 5
    assert out["residual_risk"] == "none"
    assert out["commit"] == "abc123"
    assert out["usage"] == {"input_tokens": 10}


def test_none_metadata_yields_empty_dict():
    assert md.normalize_completion_metadata(None) == {}
    assert md.normalize_completion_metadata("not a dict") == {}


def test_normalization_is_idempotent():
    once = md.normalize_completion_metadata(
        {"decisions": ["a", {"decision": "b", "supersedes": "t_1"}], "operator_followup": "x"}
    )
    twice = md.normalize_completion_metadata(once)
    assert once == twice


def test_absent_canonical_keys_are_not_injected_by_default():
    out = md.normalize_completion_metadata({"changed_files": ["a"]})
    assert "decisions" not in out
    assert "operator_followup" not in out
    assert "supersedes" not in out


def test_ensure_keys_injects_empty_canonical_defaults():
    out = md.normalize_completion_metadata({"changed_files": ["a"]}, ensure_keys=True)
    assert out["decisions"] == []
    assert out["operator_followup"] == []
    assert out["supersedes"] == []


# --------------------------------------------------------------------------- #
# parse_since                                                                  #
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize(
    "spec,expected",
    [
        ("7d", 7 * DAY),
        ("24h", 24 * 3600),
        ("2w", 14 * DAY),
        ("30m", 30 * 60),
        ("5", 5 * DAY),  # bare number defaults to days
        ("1D", DAY),  # case-insensitive
    ],
)
def test_parse_since_units(spec, expected):
    assert md.parse_since(spec) == expected


@pytest.mark.parametrize("spec", ["", "abc", "7x", "-3d", "d"])
def test_parse_since_rejects_garbage(spec):
    with pytest.raises(ValueError):
        md.parse_since(spec)


# --------------------------------------------------------------------------- #
# build_digest                                                                 #
# --------------------------------------------------------------------------- #


def _run(task_id, ended_at, *, decisions=None, followup=None, supersedes=None,
         title="T", profile="coder", tenant=None, summary=None):
    meta = {}
    if decisions is not None:
        meta["decisions"] = decisions
    if followup is not None:
        meta["operator_followup"] = followup
    if supersedes is not None:
        meta["supersedes"] = supersedes
    return {
        "task_id": task_id,
        "title": title,
        "profile": profile,
        "tenant": tenant,
        "ended_at": ended_at,
        "summary": summary,
        "metadata": meta,
    }


def test_runs_outside_window_are_excluded():
    now = 100 * DAY
    runs = [
        _run("t_new", now - 1 * DAY, decisions=["fresh"]),
        _run("t_old", now - 30 * DAY, decisions=["stale"]),
    ]
    digest = md.build_digest(runs, now=now, since_seconds=7 * DAY)
    texts = [d["text"] for d in digest["decisions"]]
    assert texts == ["fresh"]


def test_decisions_are_newest_first():
    now = 100 * DAY
    runs = [
        _run("t_a", now - 5 * DAY, decisions=["older"]),
        _run("t_b", now - 1 * DAY, decisions=["newer"]),
    ]
    digest = md.build_digest(runs, now=now, since_seconds=7 * DAY)
    assert [d["text"] for d in digest["decisions"]] == ["newer", "older"]


def test_each_decision_carries_its_source_task():
    now = 100 * DAY
    runs = [_run("t_src", now - 1 * DAY, decisions=["x"], title="My task")]
    digest = md.build_digest(runs, now=now, since_seconds=7 * DAY)
    d = digest["decisions"][0]
    assert d["task_id"] == "t_src"
    assert d["title"] == "My task"
    assert d["ended_at"] == now - 1 * DAY


def test_identical_decision_text_is_deduplicated_keeping_newest():
    now = 100 * DAY
    runs = [
        _run("t_old", now - 5 * DAY, decisions=["Same decision"]),
        _run("t_new", now - 1 * DAY, decisions=["Same decision"]),
    ]
    digest = md.build_digest(runs, now=now, since_seconds=7 * DAY)
    assert len(digest["decisions"]) == 1
    assert digest["decisions"][0]["task_id"] == "t_new"


def test_operator_followups_are_collected():
    now = 100 * DAY
    runs = [_run("t_a", now - 1 * DAY, followup=["Restart gateway", "Review credits"])]
    digest = md.build_digest(runs, now=now, since_seconds=7 * DAY)
    assert [f["text"] for f in digest["operator_followups"]] == ["Restart gateway", "Review credits"]
    assert digest["operator_followups"][0]["task_id"] == "t_a"


def test_superseded_refs_are_collected_from_both_sources():
    now = 100 * DAY
    runs = [
        _run("t_a", now - 1 * DAY, decisions=[{"decision": "new way", "supersedes": "t_old1"}]),
        _run("t_b", now - 2 * DAY, supersedes=["t_old2"]),
    ]
    digest = md.build_digest(runs, now=now, since_seconds=7 * DAY)
    refs = {s["ref"] for s in digest["superseded"]}
    assert refs == {"t_old1", "t_old2"}


def test_profile_filter_limits_runs():
    now = 100 * DAY
    runs = [
        _run("t_a", now - 1 * DAY, decisions=["coder one"], profile="coder"),
        _run("t_b", now - 1 * DAY, decisions=["premium one"], profile="premium"),
    ]
    digest = md.build_digest(runs, now=now, since_seconds=7 * DAY, profile="premium")
    assert [d["text"] for d in digest["decisions"]] == ["premium one"]


def test_limit_caps_decisions():
    now = 100 * DAY
    runs = [_run(f"t_{i}", now - i * 3600, decisions=[f"d{i}"]) for i in range(5)]
    digest = md.build_digest(runs, now=now, since_seconds=7 * DAY, limit=2)
    assert len(digest["decisions"]) == 2


def test_stats_reflect_scanned_and_extracted():
    now = 100 * DAY
    runs = [
        _run("t_a", now - 1 * DAY, decisions=["d1", "d2"]),
        _run("t_b", now - 1 * DAY),  # no decisions
        _run("t_old", now - 30 * DAY, decisions=["ignored"]),  # outside window
    ]
    digest = md.build_digest(runs, now=now, since_seconds=7 * DAY)
    assert digest["stats"]["runs_in_window"] == 2
    assert digest["stats"]["runs_with_decisions"] == 1
    assert digest["stats"]["decisions"] == 2


# --------------------------------------------------------------------------- #
# render_digest                                                                #
# --------------------------------------------------------------------------- #


def test_render_text_contains_decisions_and_source():
    now = 100 * DAY
    runs = [_run("t_src", now - 1 * DAY, decisions=["Pin pymilvus"], title="Milvus fix")]
    digest = md.build_digest(runs, now=now, since_seconds=7 * DAY)
    text = md.render_digest(digest, fmt="text")
    assert "Pin pymilvus" in text
    assert "t_src" in text


def test_render_json_round_trips():
    now = 100 * DAY
    runs = [_run("t_src", now - 1 * DAY, decisions=["x"], followup=["y"])]
    digest = md.build_digest(runs, now=now, since_seconds=7 * DAY)
    out = md.render_digest(digest, fmt="json")
    parsed = json.loads(out)
    assert parsed["decisions"][0]["text"] == "x"
    assert parsed["operator_followups"][0]["text"] == "y"


def test_render_empty_digest_is_friendly():
    now = 100 * DAY
    digest = md.build_digest([], now=now, since_seconds=7 * DAY)
    text = md.render_digest(digest, fmt="text")
    assert "No decisions" in text or "keine" in text.lower()


# --------------------------------------------------------------------------- #
# Write-path integration: completions store the canonical schema               #
# --------------------------------------------------------------------------- #


def test_complete_task_stores_canonical_decision_schema(tmp_path, monkeypatch):
    from hermes_cli import kanban_db as kb

    home = tmp_path / ".hermes"
    home.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(home))
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    kb.init_db()
    conn = kb.connect()

    tid = kb.create_task(conn, title="ship the digest", assignee="coder")
    assert kb.complete_task(
        conn,
        tid,
        summary="done",
        metadata={
            "decisions": ["Pin pymilvus 2.6.15"],  # string shorthand on the way in
            "operator_followup": "Restart the gateway",  # lone string
            "changed_files": ["x.py"],  # unrelated key must survive
        },
    )

    runs = kb.list_runs(conn, tid)
    stored = [r.metadata for r in runs if r.metadata and r.metadata.get("decisions")]
    assert stored, "completion should persist a run carrying the decisions"
    meta = stored[-1]
    # decisions canonicalised to records
    assert meta["decisions"] == [
        {"text": "Pin pymilvus 2.6.15", "scope": None, "supersedes": []}
    ]
    # follow-up coerced to a list
    assert meta["operator_followup"] == ["Restart the gateway"]
    # unrelated handoff fields preserved
    assert meta["changed_files"] == ["x.py"]
    conn.close()


def test_digest_reads_completed_runs_from_db(tmp_path, monkeypatch):
    from hermes_cli import kanban_db as kb

    home = tmp_path / ".hermes"
    home.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(home))
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    kb.init_db()
    conn = kb.connect()

    tid = kb.create_task(conn, title="decide things", assignee="coder")
    kb.complete_task(conn, tid, summary="done", metadata={"decisions": ["Use Milvus server"]})

    runs = md.load_completed_runs(conn, since_ts=0)
    digest = md.build_digest(runs, now=int(__import__("time").time()), since_seconds=DAY)
    texts = [d["text"] for d in digest["decisions"]]
    assert "Use Milvus server" in texts
    assert digest["decisions"][0]["task_id"] == tid
    conn.close()
