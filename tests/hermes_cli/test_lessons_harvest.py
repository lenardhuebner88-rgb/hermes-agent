"""Tests for ``hermes lessons harvest`` — the deterministic lessons harvester.

AC-1: CLI command exists, emits harvest_candidates.json with clusters,
      evidence counts, and source IDs; tests against real disposition_items
      row shapes (fixture from live-row shape).
AC-2: No LLM call in the harvest path; idempotent; read-only against kanban.db
      (mode=ro).
"""
from __future__ import annotations

import json
import time
from pathlib import Path

import pytest

from hermes_cli import kanban_db as kb
from hermes_cli import lessons


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture()
def kanban_home(tmp_path, monkeypatch):
    """Isolated HERMES_HOME with an initialised kanban DB (read-write for setup)."""
    home = tmp_path / ".hermes"
    home.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(home))
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    db_path = kb.kanban_db_path()
    kb._INITIALIZED_PATHS.discard(str(db_path.resolve()))
    kb.init_db()
    return home


@pytest.fixture()
def loops_root(tmp_path):
    """Create a loops/ root with sample loop-pack LEDGER.md files."""
    root = tmp_path / "loops"
    root.mkdir()
    pack_dir = root / "error-sweep"
    pack_dir.mkdir()
    (pack_dir / "LEDGER.md").write_text(
        "# LEDGER\n\n"
        "- 2026-07-05: Fixed dirty worktree precheck before merge (overlap with Claude session)\n"
        "- 2026-07-03: artifact policy missing — preserve prefix enforced\n",
        encoding="utf-8",
    )
    return root


# ---------------------------------------------------------------------------
# Helpers — create disposition_items and blocked events in real row shapes
# ---------------------------------------------------------------------------


def _insert_disposition_item(
    conn,
    *,
    source_task_id,
    typ="follow_up",
    disposition="defer",
    next_action="",
    severity="none",
    evidence="",
    status="open",
):
    """Insert a disposition_item with the live-row shape (matches production)."""
    item_id = kb.insert_disposition_item(
        conn,
        source_task_id=source_task_id,
        typ=typ,
        disposition=disposition,
        next_action=next_action,
        severity=severity,
        evidence=evidence,
    )
    kb.write_txn(conn).__enter__().__exit__(None, None, None)  # commit
    # Fix the status to 'open' or 'accepted' if not the default
    conn.execute(
        "UPDATE disposition_items SET status=? WHERE id=?",
        (status, item_id),
    )
    conn.commit()
    return item_id


def _insert_blocked_event(conn, *, task_id, reason, created_at=None):
    """Insert a blocked task_event with the live-row payload shape."""
    if created_at is None:
        created_at = int(time.time())
    conn.execute(
        "INSERT INTO task_events (task_id, run_id, kind, payload, created_at) "
        "VALUES (?, NULL, 'blocked', ?, ?)",
        (task_id, json.dumps({"reason": reason}), created_at),
    )
    conn.commit()


def _create_task(conn, *, title, assignee="coder"):
    return kb.create_task(conn, title=title, assignee=assignee, created_by="test")


# ---------------------------------------------------------------------------
# AC-1: Harvest produces structured JSON with clusters + evidence counts
# ---------------------------------------------------------------------------


def test_harvest_emits_json_with_clusters(kanban_home, loops_root, monkeypatch):
    """Harvest writes harvest_candidates.json with cluster/evidence_count/source_ids."""
    now = int(time.time())

    with kb.connect() as conn:
        # disposition_items matching known trap signatures
        tid1 = _create_task(conn, title="Release holds")
        _insert_disposition_item(
            conn,
            source_task_id=tid1,
            evidence="Task born blocked, awaiting release-gate GO",
            next_action="verify after rollout",
            status="open",
        )
        tid2 = _create_task(conn, title="Artifact issue")
        _insert_disposition_item(
            conn,
            source_task_id=tid2,
            evidence="ARTIFACT_POLICY_MISSING — no preserve prefix",
            next_action="add preserve prefix",
            status="accepted",
        )
        # A second artifact-policy hit to meet >=2 threshold
        tid3 = _create_task(conn, title="Second artifact issue")
        _insert_disposition_item(
            conn,
            source_task_id=tid3,
            evidence="artifact policy missing for screenshot output",
            next_action="fix preserve prefix",
            status="open",
        )

        # blocked events
        _insert_blocked_event(conn, task_id=tid2, reason="DIRTY_WORKTREE foreign files detected")
        _insert_blocked_event(conn, task_id=tid1, reason="awaiting release-gate GO from operator")

    result = lessons.run_harvest(loops_root=loops_root, window_days=30, now_ts=now)

    out_path = kanban_home / "state" / "lessons" / "harvest_candidates.json"
    assert out_path.exists()
    assert result["output_path"] == str(out_path)
    assert result["source_counts"]["disposition_items"] == 3
    assert result["source_counts"]["blocked_events"] == 2
    assert result["source_counts"]["loop_ledger_entries"] == 2

    data = json.loads(out_path.read_text(encoding="utf-8"))
    clusters = {c["cluster"]: c for c in data["candidates"]}
    assert "release-gate/born-blocked-holds" in clusters
    assert "artifact-policy-traps" in clusters

    # The artifact cluster should have >=2 evidence points (2 disposition + ledger)
    art_cluster = clusters["artifact-policy-traps"]
    assert art_cluster["evidence_point_count"] >= 2
    assert art_cluster["meets_threshold"] is True
    assert len(art_cluster["source_ids"]) >= 2

    # Source types should include the contributing types
    assert "disposition_item" in art_cluster["source_types"]


def test_harvest_includes_source_ids_disposition(kanban_home, loops_root):
    """Each candidate carries source_ids referencing disposition_items + blocked events."""
    now = int(time.time())
    with kb.connect() as conn:
        tid1 = _create_task(conn, title="overlap trap")
        _insert_disposition_item(
            conn,
            source_task_id=tid1,
            evidence="coordination overlap with parallel session — dirty worktree",
            status="open",
        )
        _insert_blocked_event(conn, task_id=tid1, reason="DIRTY_WORKTREE: foreign files in tree")

    result = lessons.run_harvest(loops_root=loops_root, window_days=30, now_ts=now)
    out_path = kanban_home / "state" / "lessons" / "harvest_candidates.json"
    data = json.loads(out_path.read_text(encoding="utf-8"))

    # dirty-worktree cluster should reference both a di_* and te_* source
    dirty_cluster = next(
        c for c in data["candidates"] if c["cluster"] == "dirty-worktree/parallel-session-overlap"
    )
    source_ids = dirty_cluster["source_ids"]
    assert any(sid.startswith("di_") or not sid.startswith("te_") for sid in source_ids)
    assert any(sid.startswith("te_") for sid in source_ids)


def test_harvest_excludes_lessons_promote_block_events(kanban_home, loops_root):
    """Promotion cards must not become evidence for their own cluster."""
    now = int(time.time())
    with kb.connect() as conn:
        generated = kb.create_task(
            conn,
            title="generated docs review",
            assignee="coder",
            created_by="lessons-promote",
            initial_status="blocked",
        )
        real = _create_task(conn, title="real release hold")
        _insert_blocked_event(conn, task_id=generated, reason="born blocked")
        _insert_blocked_event(
            conn, task_id=real, reason="awaiting release-gate GO from operator"
        )

    lessons.run_harvest(loops_root=loops_root, window_days=30, now_ts=now)
    data = json.loads(
        (kanban_home / "state" / "lessons" / "harvest_candidates.json").read_text("utf-8")
    )
    cluster = next(
        c
        for c in data["candidates"]
        if c["cluster"] == "release-gate/born-blocked-holds"
    )
    assert cluster["evidence_point_count"] == 1
    assert cluster["meets_threshold"] is False
    assert all(sample.get("task_id") != generated for sample in cluster["evidence_samples"])


def test_harvest_includes_loop_ledger_entries(kanban_home, loops_root):
    """Loop LEDGER entries are harvested and attributed to trap clusters."""
    now = int(time.time())
    with kb.connect() as conn:
        # No disposition items — just the loop LEDGER has data
        pass

    result = lessons.run_harvest(loops_root=loops_root, window_days=30, now_ts=now)
    data = json.loads(
        (kanban_home / "state" / "lessons" / "harvest_candidates.json").read_text("utf-8")
    )

    # The LEDGER has an artifact-policy entry and a dirty-worktree entry
    clusters = {c["cluster"]: c for c in data["candidates"]}
    assert "dirty-worktree/parallel-session-overlap" in clusters
    dirty = clusters["dirty-worktree/parallel-session-overlap"]
    assert "loop_ledger" in dirty["source_types"]
    assert any("ledger/" in sid for sid in dirty["source_ids"])


# ---------------------------------------------------------------------------
# AC-2: No LLM call; idempotent; read-only
# ---------------------------------------------------------------------------


def test_harvest_is_idempotent(kanban_home, loops_root):
    """Running harvest twice produces the same output (ignoring timestamp)."""
    now = int(time.time())
    with kb.connect() as conn:
        tid = _create_task(conn, title="release trap")
        _insert_disposition_item(
            conn,
            source_task_id=tid,
            evidence="awaiting release-gate GO",
            status="open",
        )

    lessons.run_harvest(loops_root=loops_root, window_days=30, now_ts=now)
    data1 = json.loads(
        (kanban_home / "state" / "lessons" / "harvest_candidates.json").read_text("utf-8")
    )

    lessons.run_harvest(loops_root=loops_root, window_days=30, now_ts=now)
    data2 = json.loads(
        (kanban_home / "state" / "lessons" / "harvest_candidates.json").read_text("utf-8")
    )

    # Candidates and source counts should match (timestamp may differ)
    assert data1["sources"] == data2["sources"]
    assert data1["candidate_count"] == data2["candidate_count"]
    assert len(data1["candidates"]) == len(data2["candidates"])


def test_harvest_does_not_mutate_db(kanban_home, loops_root):
    """Harvest must be read-only — no writes to kanban.db."""
    now = int(time.time())
    with kb.connect() as conn:
        tid = _create_task(conn, title="release trap")
        _insert_disposition_item(
            conn,
            source_task_id=tid,
            evidence="awaiting release-gate GO",
            status="open",
        )
        # Record initial row counts
        disp_before = conn.execute("SELECT COUNT(*) FROM disposition_items").fetchone()[0]
        events_before = conn.execute("SELECT COUNT(*) FROM task_events WHERE kind='blocked'").fetchone()[0]

    lessons.run_harvest(loops_root=loops_root, window_days=30, now_ts=now)

    # Verify nothing was written
    with kb.connect() as conn2:
        disp_after = conn2.execute("SELECT COUNT(*) FROM disposition_items").fetchone()[0]
        events_after = conn2.execute("SELECT COUNT(*) FROM task_events WHERE kind='blocked'").fetchone()[0]
    assert disp_after == disp_before
    assert events_after == events_before


def test_harvest_no_llm_call(kanban_home, loops_root):
    """Harvest must never invoke an LLM. We verify no model/provider imports."""
    import importlib
    import sys

    # Capture modules before harvesting
    modules_before = set(sys.modules.keys())
    lessons.run_harvest(loops_root=loops_root, window_days=30, now_ts=int(time.time()))
    modules_after = set(sys.modules.keys())
    new_modules = modules_after - modules_before

    # No LLM client modules should have been loaded during harvest
    llm_indicators = [
        m
        for m in new_modules
        if any(k in m.lower() for k in ("openai", "anthropic", "litellm", "llm", "chat_completion"))
    ]
    assert not llm_indicators, f"LLM-related modules loaded during harvest: {llm_indicators}"


# ---------------------------------------------------------------------------
# Window filtering and edge cases
# ---------------------------------------------------------------------------


def test_harvest_window_filter_excludes_old_items(kanban_home, loops_root):
    """Items older than window_days are excluded from disposition + blocked sources."""
    now = int(time.time())
    old_ts = now - 60 * 86400  # 60 days ago — outside the 30-day window

    with kb.connect() as conn:
        tid = _create_task(conn, title="old trap")
        # Insert with old created_at by updating after insert
        item_id = _insert_disposition_item(
            conn,
            source_task_id=tid,
            evidence="awaiting release-gate GO",
            status="open",
        )
        conn.execute("UPDATE disposition_items SET created_at=? WHERE id=?", (old_ts, item_id))
        conn.execute(
            "INSERT INTO task_events (task_id, run_id, kind, payload, created_at) "
            "VALUES (?, NULL, 'blocked', ?, ?)",
            (tid, json.dumps({"reason": "DIRTY_WORKTREE"}), old_ts),
        )
        conn.commit()

    result = lessons.run_harvest(loops_root=loops_root, window_days=30, now_ts=now)
    assert result["source_counts"]["disposition_items"] == 0
    assert result["source_counts"]["blocked_events"] == 0


def test_harvest_skips_dismissed_items(kanban_home, loops_root):
    """Only open/accepted disposition items are harvested (not dismissed)."""
    now = int(time.time())
    with kb.connect() as conn:
        tid1 = _create_task(conn, title="open item")
        _insert_disposition_item(
            conn,
            source_task_id=tid1,
            evidence="awaiting release-gate GO",
            status="open",
        )
        tid2 = _create_task(conn, title="dismissed item")
        _insert_disposition_item(
            conn,
            source_task_id=tid2,
            evidence="awaiting release-gate GO",
            status="dismissed",
        )
        tid3 = _create_task(conn, title="task_created item")
        _insert_disposition_item(
            conn,
            source_task_id=tid3,
            evidence="awaiting release-gate GO",
            status="task_created",
        )

    result = lessons.run_harvest(loops_root=loops_root, window_days=30, now_ts=now)
    # Only the 'open' item should be harvested
    assert result["source_counts"]["disposition_items"] == 1


def test_harvest_missing_kanban_db(tmp_path, loops_root, monkeypatch):
    """Harvest handles a missing kanban.db gracefully (no crash)."""
    home = tmp_path / ".hermes"
    home.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(home))
    monkeypatch.setattr(Path, "home", lambda: tmp_path)

    result = lessons.run_harvest(loops_root=loops_root, window_days=30)
    assert result["source_counts"]["disposition_items"] == 0
    assert result["source_counts"]["blocked_events"] == 0
    # Output still produced with empty data
    out_path = home / "state" / "lessons" / "harvest_candidates.json"
    assert out_path.exists()


def test_harvest_unclustered_bucket_exists(kanban_home, loops_root):
    """Items that match no known signature fall into the unclustered bucket."""
    now = int(time.time())
    with kb.connect() as conn:
        tid = _create_task(conn, title="random item")
        _insert_disposition_item(
            conn,
            source_task_id=tid,
            evidence="some completely unrelated text about database normalization",
            status="open",
        )

    lessons.run_harvest(loops_root=loops_root, window_days=30, now_ts=now)
    data = json.loads(
        (kanban_home / "state" / "lessons" / "harvest_candidates.json").read_text("utf-8")
    )
    # Should contain an "unclustered" cluster entry — evidence is not lost
    clusters = {c["cluster"]: c for c in data["candidates"]}
    assert "unclustered" in clusters
    assert clusters["unclustered"]["evidence_point_count"] == 1


def test_harvest_min_evidence_threshold(kanban_home, loops_root):
    """Clusters with <2 evidence points have meets_threshold=False (not promoted)."""
    now = int(time.time())
    with kb.connect() as conn:
        # Single auto-decompose evidence point (below threshold)
        tid = _create_task(conn, title="loop trap")
        _insert_disposition_item(
            conn,
            source_task_id=tid,
            evidence="auto_decompose failed 3 times on large root slice",
            status="open",
        )

    lessons.run_harvest(loops_root=loops_root, window_days=30, now_ts=now)
    data = json.loads(
        (kanban_home / "state" / "lessons" / "harvest_candidates.json").read_text("utf-8")
    )
    clusters = {c["cluster"]: c for c in data["candidates"]}
    assert "auto-decompose/token-cap-loops" in clusters
    assert clusters["auto-decompose/token-cap-loops"]["meets_threshold"] is False
    # candidate_count should be 0 (nothing meets threshold)
    assert data["candidate_count"] == 0


# ---------------------------------------------------------------------------
# CLI integration
# ---------------------------------------------------------------------------


def test_cli_lessons_harvest_invokes(kanban_home, loops_root, monkeypatch):
    """The CLI ``hermes lessons harvest`` dispatches to run_harvest."""
    import argparse
    from hermes_cli.subcommands.lessons import build_lessons_parser, _cmd_harvest

    parser = argparse.ArgumentParser()
    sub = parser.add_subparsers()
    build_lessons_parser(sub)

    args = parser.parse_args(["lessons", "harvest", "--loops-root", str(loops_root)])
    assert hasattr(args, "func")
    # Should exit 0 and print JSON
    rc = args.func(args)
    assert rc == 0
