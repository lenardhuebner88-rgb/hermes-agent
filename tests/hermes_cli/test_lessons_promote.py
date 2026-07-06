"""Tests for ``hermes lessons promote`` — L3 promote path.

AC-1: Promote reads harvest_candidates.json, creates held (blocked) Kanban
      tasks for clusters with >=2 evidence points, deduplicates against
      already-documented pitfalls in AGENTS.md / docs/agent-dev-guide.md,
      and caps at N tasks per run. Idempotent (re-run creates no duplicates).
AC-2: Decision documented — old skill_promote_pipeline.py superseded by
      lessons->docs loop (not tested here; verified via cron pause/decision
      receipt).
"""
from __future__ import annotations

import json
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
def repo_dir(tmp_path):
    """Minimal repo dir with AGENTS.md containing existing pitfalls."""
    repo = tmp_path / "repo"
    repo.mkdir()
    docs = repo / "docs"
    docs.mkdir()
    (repo / "AGENTS.md").write_text(
        "# AGENTS\n\n"
        "## Important Pitfalls\n\n"
        "- Never git reset --hard origin/main in this fork.\n"
        "- Do not introduce new simple_term_menu usage.\n"
        "- artifact policy missing — preserve prefix enforced\n",
        encoding="utf-8",
    )
    (docs / "agent-dev-guide.md").write_text(
        "# Dev Guide\n\n"
        "## Pitfalls\n\n"
        "- Never hardcode ~/.hermes in state code.\n",
        encoding="utf-8",
    )
    return repo


def _make_harvest_file(
    home: Path,
    clusters: list[dict],
    *,
    sources: dict | None = None,
    generated_ts: str = "2026-07-07T00:00:00Z",
) -> Path:
    """Write a harvest_candidates.json artefact into the lessons state dir."""
    state_dir = home / "state" / "lessons"
    state_dir.mkdir(parents=True, exist_ok=True)
    out_path = state_dir / "harvest_candidates.json"
    payload = {
        "generated_ts": generated_ts,
        "sources": sources
        or {"disposition_items": 0, "blocked_events": 0, "loop_ledger_entries": 0},
        "candidates": clusters,
        "candidate_count": sum(1 for c in clusters if c.get("meets_threshold", False)),
        "total_clusters": len(clusters),
    }
    out_path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    return out_path


def _candidate(
    cluster: str,
    *,
    evidence_count: int = 2,
    meets: bool = True,
    signature: tuple[str, ...] = (),
    source_ids: list[str] | None = None,
    source_types: list[str] | None = None,
    samples: list[str] | None = None,
) -> dict:
    return {
        "cluster": cluster,
        "signature": list(signature),
        "evidence_point_count": evidence_count,
        "meets_threshold": meets,
        "source_ids": source_ids or [],
        "source_types": source_types or [],
        "evidence_samples": samples or [],
    }


# ---------------------------------------------------------------------------
# AC-1a: Promote creates held docs-edit tasks for eligible clusters
# ---------------------------------------------------------------------------


def test_promote_creates_held_tasks(kanban_home, repo_dir):
    """Eligible clusters become blocked (held) Kanban tasks assigned to coder."""
    _make_harvest_file(
        kanban_home,
        [
            _candidate(
                "release-gate/born-blocked-holds",
                evidence_count=3,
                source_ids=["t_001", "t_002"],
                source_types=["disposition_item"],
                samples=["Task born blocked awaiting release-gate GO"],
            ),
            _candidate(
                "dirty-worktree/parallel-session-overlap",
                evidence_count=2,
                source_ids=["t_003"],
                source_types=["blocked_event"],
                samples=["DIRTY_WORKTREE: foreign files in tree"],
            ),
        ],
    )

    result = lessons.run_promote(harvest_path=None, repo_dir=repo_dir, cap=5)

    assert result["promoted"] == 2
    assert result["skipped_documented"] == 0
    assert result["capped"] == 0
    assert len(result["created"]) == 2

    # Verify the tasks landed in the DB as blocked (held) and assigned to coder
    with kb.connect_closing(board=None) as conn:
        rows = conn.execute(
            "SELECT title, status FROM tasks WHERE assignee='coder'"
        ).fetchall()
    titles = [r[0] for r in rows]
    statuses = [r[1] for r in rows]
    title_text = " ".join(titles)
    assert "release-gate/born-blocked-holds" in title_text
    assert "dirty-worktree/parallel-session-overlap" in title_text
    assert all(s == "blocked" for s in statuses), f"Expected all blocked, got {statuses}"


def test_promote_task_body_contains_evidence(kanban_home, repo_dir):
    """The created task body must carry evidence from the harvest artefact."""
    _make_harvest_file(
        kanban_home,
        [
            _candidate(
                "release-gate/born-blocked-holds",
                evidence_count=4,
                source_ids=["t_a", "t_b", "t_c"],
                source_types=["disposition_item", "blocked_event"],
                samples=["Sample evidence one", "Sample evidence two"],
            ),
        ],
    )

    result = lessons.run_promote(harvest_path=None, repo_dir=repo_dir, cap=5)
    assert result["promoted"] == 1

    with kb.connect_closing(board=None) as conn:
        body = conn.execute(
            "SELECT body FROM tasks WHERE assignee='coder' LIMIT 1"
        ).fetchone()[0]
    assert "evidence point" in body.lower() or "evidence points" in body.lower()
    assert "Sample evidence one" in body
    assert "AGENTS.md" in body


# ---------------------------------------------------------------------------
# AC-1b: Dedup — already-documented pitfalls are skipped
# ---------------------------------------------------------------------------


def test_promote_skips_documented_pitfalls(kanban_home, repo_dir):
    """Clusters whose signature keywords already appear in the docs are skipped."""
    # repo_dir's AGENTS.md already contains "artifact policy missing"
    _make_harvest_file(
        kanban_home,
        [
            _candidate(
                "artifact-policy-traps",
                evidence_count=3,
                signature=("artifact policy missing", "preserve prefix"),
                source_ids=["t_01"],
            ),
            _candidate(
                "release-gate/born-blocked-holds",
                evidence_count=2,
                signature=("waiting for go",),
                source_ids=["t_02"],
            ),
        ],
    )

    result = lessons.run_promote(harvest_path=None, repo_dir=repo_dir, cap=5)

    # The artifact cluster is documented → skipped; the release-gate one is promoted
    assert result["promoted"] == 1
    assert result["skipped_documented"] == 1
    assert "artifact-policy-traps" in result["documented_clusters"]


def test_promote_skips_when_cluster_slug_in_docs(kanban_home, repo_dir):
    """A cluster whose slug already appears in docs is skipped even without keyword overlap."""
    # AGENTS.md has "artifact policy" — the slug "artifact-policy-traps" would match
    # the word "artifact" if the keyword were just "artifact". But let's test the slug
    # path directly by adding the exact slug to the docs.
    (repo_dir / "AGENTS.md").write_text(
        "# AGENTS\n\nrelease-gate-born-blocked-holds already documented here\n",
        encoding="utf-8",
    )
    _make_harvest_file(
        kanban_home,
        [
            _candidate(
                "release-gate/born-blocked-holds",
                evidence_count=2,
                signature=("totally-unique-keyword-not-in-docs",),
                source_ids=["t_01"],
            ),
        ],
    )

    result = lessons.run_promote(harvest_path=None, repo_dir=repo_dir, cap=5)
    assert result["promoted"] == 0
    assert result["skipped_documented"] == 1


# ---------------------------------------------------------------------------
# AC-1c: Cap enforcement
# ---------------------------------------------------------------------------


def test_promote_respects_cap(kanban_home, repo_dir):
    """More eligible clusters than the cap → only top-N created, rest capped."""
    clusters = [
        _candidate(f"trap-class-{i}/cluster-a", evidence_count=10 - i, source_ids=[f"t_{i}"])
        for i in range(8)
    ]
    _make_harvest_file(kanban_home, clusters)

    result = lessons.run_promote(harvest_path=None, repo_dir=repo_dir, cap=3)

    assert result["promoted"] == 3
    assert result["capped"] == 5
    assert len(result["created"]) == 3


def test_promote_default_cap_is_5(kanban_home, repo_dir):
    """Default cap is 5."""
    clusters = [
        _candidate(f"trap-class-{i}/x", evidence_count=5, source_ids=[f"t_{i}"])
        for i in range(7)
    ]
    _make_harvest_file(kanban_home, clusters)

    result = lessons.run_promote(harvest_path=None, repo_dir=repo_dir)
    assert result["promoted"] == 5
    assert result["capped"] == 2


# ---------------------------------------------------------------------------
# AC-1d: Idempotency — re-run creates no duplicates
# ---------------------------------------------------------------------------


def test_promote_is_idempotent(kanban_home, repo_dir):
    """Re-running promote with the same harvest artefact creates no duplicate tasks."""
    _make_harvest_file(
        kanban_home,
        [
            _candidate(
                "release-gate/born-blocked-holds",
                evidence_count=2,
                source_ids=["t_01"],
            ),
        ],
    )

    result1 = lessons.run_promote(harvest_path=None, repo_dir=repo_dir, cap=5)
    assert result1["promoted"] == 1

    result2 = lessons.run_promote(harvest_path=None, repo_dir=repo_dir, cap=5)
    assert result2["promoted"] == 1
    assert result1["created"][0]["task_id"] == result2["created"][0]["task_id"]

    # Only one task in the DB for this cluster
    with kb.connect_closing(board=None) as conn:
        count = conn.execute(
            "SELECT COUNT(*) FROM tasks WHERE title LIKE '%release-gate/born-blocked-holds%'"
        ).fetchone()[0]
    assert count == 1


# ---------------------------------------------------------------------------
# Edge cases
# ---------------------------------------------------------------------------


def test_promote_skips_below_threshold(kanban_home, repo_dir):
    """Clusters with meets_threshold=False are not promoted."""
    _make_harvest_file(
        kanban_home,
        [
            _candidate("trap-a/low", evidence_count=1, meets=False, source_ids=["t_01"]),
            _candidate("trap-b/high", evidence_count=3, meets=True, source_ids=["t_02"]),
        ],
    )

    result = lessons.run_promote(harvest_path=None, repo_dir=repo_dir, cap=5)
    assert result["promoted"] == 1
    # The promoted task should be for trap-b/high
    assert "trap-b/high" in result["created"][0]["cluster"]


def test_promote_skips_unclustered(kanban_home, repo_dir):
    """The 'unclustered' bucket is never promoted even if it meets threshold."""
    _make_harvest_file(
        kanban_home,
        [
            _candidate("unclustered", evidence_count=5, meets=True, source_ids=["t_01"]),
        ],
    )

    result = lessons.run_promote(harvest_path=None, repo_dir=repo_dir, cap=5)
    assert result["promoted"] == 0


def test_promote_missing_harvest_artefact(kanban_home, repo_dir):
    """Promote raises FileNotFoundError when no harvest artefact exists."""
    with pytest.raises(FileNotFoundError, match="harvest"):
        lessons.run_promote(harvest_path=None, repo_dir=repo_dir, cap=5)


def test_promote_dry_run_creates_no_tasks(kanban_home, repo_dir):
    """Dry run returns the planned tasks without writing to kanban.db."""
    _make_harvest_file(
        kanban_home,
        [
            _candidate("trap-dry/x", evidence_count=2, source_ids=["t_01"]),
        ],
    )

    result = lessons.run_promote(
        harvest_path=None, repo_dir=repo_dir, cap=5, dry_run=True
    )
    assert result["promoted"] == 1
    assert result["created"][0].get("dry_run") is True

    # No task should exist in the DB
    with kb.connect_closing(board=None) as conn:
        count = conn.execute(
            "SELECT COUNT(*) FROM tasks WHERE title LIKE '%trap-dry%'"
        ).fetchone()[0]
    assert count == 0


def test_promote_no_candidates(kanban_home, repo_dir):
    """Empty harvest list produces zero promoted and no errors."""
    _make_harvest_file(kanban_home, [])

    result = lessons.run_promote(harvest_path=None, repo_dir=repo_dir, cap=5)
    assert result["promoted"] == 0
    assert result["capped"] == 0


# ---------------------------------------------------------------------------
# CLI integration
# ---------------------------------------------------------------------------


def test_cli_lessons_promote_dispatches(kanban_home, repo_dir):
    """The CLI ``hermes lessons promote`` dispatches to run_promote."""
    import argparse

    from hermes_cli.subcommands.lessons import build_lessons_parser, _cmd_promote

    _make_harvest_file(
        kanban_home,
        [_candidate("trap-cli/x", evidence_count=2, source_ids=["t_01"])],
    )

    parser = argparse.ArgumentParser()
    sub = parser.add_subparsers()
    build_lessons_parser(sub)

    args = parser.parse_args(
        ["lessons", "promote", "--repo-dir", str(repo_dir), "--cap", "3", "--dry-run"]
    )
    assert hasattr(args, "func")
    rc = args.func(args)
    assert rc == 0


def test_cli_lessons_promote_help_has_cap(kanban_home, repo_dir):
    """The promote subparser exposes --cap and --dry-run options."""
    import argparse

    from hermes_cli.subcommands.lessons import build_lessons_parser

    parser = argparse.ArgumentParser()
    sub = parser.add_subparsers()
    build_lessons_parser(sub)

    # Parse a partial promote command to check options exist
    args = parser.parse_args(["lessons", "promote", "--cap", "2"])
    assert args.cap == 2
    assert args.dry_run is False
