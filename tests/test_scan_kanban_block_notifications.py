"""Regression coverage for the blocked-notification RCA scanner."""
from __future__ import annotations

import hashlib
import importlib.util
import json
import sqlite3
from pathlib import Path

_SCRIPT = Path(__file__).resolve().parents[1] / "scripts" / "scan_kanban_block_notifications.py"
_spec = importlib.util.spec_from_file_location("scan_kanban_block_notifications", _SCRIPT)
assert _spec is not None and _spec.loader is not None
scanner = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(scanner)


def test_run_report_groups_blocks_by_run_context_with_explicit_buckets():
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    conn.executescript(
        """
        CREATE TABLE task_events (
            id INTEGER PRIMARY KEY,
            task_id TEXT NOT NULL,
            run_id INTEGER,
            kind TEXT NOT NULL,
            payload TEXT,
            created_at INTEGER NOT NULL
        );
        CREATE TABLE task_runs (
            id INTEGER PRIMARY KEY,
            task_id TEXT NOT NULL,
            profile TEXT,
            outcome TEXT,
            verdict TEXT,
            status TEXT NOT NULL
        );
        """
    )
    conn.executemany(
        "INSERT INTO task_runs VALUES (?, ?, ?, ?, NULL, 'done')",
        [(1, "with-context", "reviewer", "blocked"), (2, "unknown-context", None, None)],
    )
    conn.executemany(
        "INSERT INTO task_events VALUES (?, ?, ?, ?, ?, ?)",
        [
            (1, "with-context", 1, "submitted_for_review", json.dumps({"review_stage": 2}), 100),
            (2, "with-context", 1, "blocked", json.dumps({"kind": "review_revision"}), 101),
            (3, "unknown-context", 2, "blocked", json.dumps({"kind": "capacity"}), 102),
            (4, "unmatched-context", None, "submitted_for_review", "{}", 103),
            (5, "unmatched-context", None, "blocked", json.dumps({"kind": "dependency"}), 104),
        ],
    )

    report = scanner.run_report(conn, days=1, focus_task="with-context")

    assert report["metrics"]["blocks_by_run_outcome"] == {
        "blocked": 1,
        "unknown": 1,
        "unmatched": 1,
    }
    assert report["metrics"]["blocks_by_profile"] == {
        "reviewer": 1,
        "unknown": 1,
        "unmatched": 1,
    }
    assert report["metrics"]["blocks_by_review_stage"] == {
        "2": 1,
        "unknown": 1,
        "unmatched": 1,
    }


def test_review_stage_does_not_cross_completion_boundary():
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    conn.executescript(
        """
        CREATE TABLE task_events (
            id INTEGER PRIMARY KEY,
            task_id TEXT NOT NULL,
            run_id INTEGER,
            kind TEXT NOT NULL,
            payload TEXT,
            created_at INTEGER NOT NULL
        );
        CREATE TABLE task_runs (
            id INTEGER PRIMARY KEY,
            task_id TEXT NOT NULL,
            profile TEXT,
            outcome TEXT,
            verdict TEXT,
            status TEXT NOT NULL
        );
        """
    )
    conn.executemany(
        "INSERT INTO task_events VALUES (?, 'reopened-task', NULL, ?, ?, ?)",
        [
            (1, "submitted_for_review", json.dumps({"review_stage": 1}), 100),
            (2, "completed", "{}", 101),
            (3, "unblocked", "{}", 102),
            (4, "blocked", json.dumps({"kind": "needs_input"}), 103),
        ],
    )

    report = scanner.run_report(conn, days=1, focus_task="reopened-task")

    assert report["metrics"]["blocks_by_review_stage"] == {
        "unknown": 0,
        "unmatched": 1,
    }


def test_review_stage_requires_matching_nested_review_revision_candidate():
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    conn.executescript(
        """
        CREATE TABLE task_events (
            id INTEGER PRIMARY KEY,
            task_id TEXT NOT NULL,
            run_id INTEGER,
            kind TEXT NOT NULL,
            payload TEXT,
            created_at INTEGER NOT NULL
        );
        CREATE TABLE task_runs (
            id INTEGER PRIMARY KEY,
            task_id TEXT NOT NULL,
            profile TEXT,
            outcome TEXT,
            verdict TEXT,
            status TEXT NOT NULL
        );
        """
    )
    conn.executemany(
        "INSERT INTO task_events VALUES (?, 'candidate-task', NULL, ?, ?, ?)",
        [
            (
                1,
                "submitted_for_review",
                json.dumps(
                    {"review_stage": 2, "diff_candidate_commit": "83BCC9A"}
                ),
                100,
            ),
            (
                2,
                "blocked",
                json.dumps(
                    {
                        "kind": "review_revision",
                        "review_revision": {
                            "reviewed_commit": "83bcc9a72001a3c2fd0de5e3ed7566ae5fb990e3"
                        },
                    }
                ),
                101,
            ),
            (
                3,
                "blocked",
                json.dumps(
                    {
                        "kind": "review_revision",
                        "review_revision": {
                            "reviewed_commit": "93bcc9a72001a3c2fd0de5e3ed7566ae5fb990e3"
                        },
                    }
                ),
                102,
            ),
        ],
    )

    report = scanner.run_report(conn, days=1, focus_task="candidate-task")

    assert report["metrics"]["blocks_by_review_stage"] == {
        "2": 1,
        "unknown": 0,
        "unmatched": 1,
    }


def test_in_window_block_uses_review_context_before_cutoff():
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    conn.executescript(
        """
        CREATE TABLE task_events (
            id INTEGER PRIMARY KEY,
            task_id TEXT NOT NULL,
            run_id INTEGER,
            kind TEXT NOT NULL,
            payload TEXT,
            created_at INTEGER NOT NULL
        );
        CREATE TABLE task_runs (
            id INTEGER PRIMARY KEY,
            task_id TEXT NOT NULL,
            profile TEXT,
            outcome TEXT,
            verdict TEXT,
            status TEXT NOT NULL
        );
        """
    )
    conn.executemany(
        "INSERT INTO task_events VALUES (?, ?, NULL, ?, ?, ?)",
        [
            (
                1,
                "boundary-task",
                "submitted_for_review",
                json.dumps(
                    {
                        "review_stage": 1,
                        "diff_candidate_commit": "boundary-candidate",
                    }
                ),
                0,
            ),
            (
                2,
                "boundary-task",
                "blocked",
                json.dumps(
                    {
                        "kind": "review_revision",
                        "review_revision": {
                            "reviewed_commit": "boundary-candidate"
                        },
                    }
                ),
                1,
            ),
            (3, "window-anchor", "unblocked", "{}", 86401),
        ],
    )

    report = scanner.run_report(conn, days=1, focus_task="boundary-task")

    assert report["metrics"]["block_transitions"] == 1
    assert report["metrics"]["blocks_by_review_stage"] == {
        "1": 1,
        "unknown": 0,
        "unmatched": 0,
    }
    assert report["metrics"]["tasks_with_repeated_blocks_within_5m"] == 0
    expected_window_headers = (
        "2|boundary-task|blocked|1|\n"
        "3|window-anchor|unblocked|86401|"
    )
    assert report["snapshot"]["window_event_header_sha256"] == hashlib.sha256(
        expected_window_headers.encode()
    ).hexdigest()


def test_repeated_blocks_count_unique_tasks_across_candidates():
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    conn.executescript(
        """
        CREATE TABLE task_events (
            id INTEGER PRIMARY KEY,
            task_id TEXT NOT NULL,
            run_id INTEGER,
            kind TEXT NOT NULL,
            payload TEXT,
            created_at INTEGER NOT NULL
        );
        CREATE TABLE task_runs (
            id INTEGER PRIMARY KEY,
            task_id TEXT NOT NULL,
            profile TEXT,
            outcome TEXT,
            verdict TEXT,
            status TEXT NOT NULL
        );
        """
    )
    events = []
    event_id = 1
    for candidate, base_time in (("candidate-a", 100), ("candidate-b", 1_000)):
        events.extend(
            [
                (
                    event_id,
                    "same-task",
                    None,
                    "submitted_for_review",
                    json.dumps({"diff_candidate_commit": candidate}),
                    base_time,
                ),
                (
                    event_id + 1,
                    "same-task",
                    None,
                    "blocked",
                    json.dumps(
                        {
                            "kind": "review_revision",
                            "review_revision": {"reviewed_commit": candidate},
                        }
                    ),
                    base_time + 1,
                ),
                (
                    event_id + 2,
                    "same-task",
                    None,
                    "blocked",
                    json.dumps(
                        {
                            "kind": "review_revision",
                            "review_revision": {"reviewed_commit": candidate},
                        }
                    ),
                    base_time + 2,
                ),
            ]
        )
        event_id += 3
    conn.executemany("INSERT INTO task_events VALUES (?, ?, ?, ?, ?, ?)", events)

    report = scanner.run_report(conn, days=1, focus_task="same-task")

    assert report["metrics"]["tasks_with_repeated_blocks_within_5m"] == 1


def test_repeated_blocks_use_payload_candidate_with_prefix_equivalence():
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    conn.executescript(
        """
        CREATE TABLE task_events (
            id INTEGER PRIMARY KEY,
            task_id TEXT NOT NULL,
            run_id INTEGER,
            kind TEXT NOT NULL,
            payload TEXT,
            created_at INTEGER NOT NULL
        );
        CREATE TABLE task_runs (
            id INTEGER PRIMARY KEY,
            task_id TEXT NOT NULL,
            profile TEXT,
            outcome TEXT,
            verdict TEXT,
            status TEXT NOT NULL
        );
        """
    )
    candidate_a = "83bcc9a72001a3c2fd0de5e3ed7566ae5fb990e3"
    candidate_b = "93bcc9a72001a3c2fd0de5e3ed7566ae5fb990e3"
    candidate_c = "a3bcc9a72001a3c2fd0de5e3ed7566ae5fb990e3"

    # Different payload candidates must not form a false repeat even when the
    # open submission candidate would have paired them under the old keying.
    different_events = [
        (1, "different", None, "submitted_for_review", json.dumps({"diff_candidate_commit": candidate_a}), 100),
        (2, "different", None, "blocked", json.dumps({"kind": "review_revision", "review_revision": {"reviewed_commit": candidate_b}}), 101),
        (3, "different", None, "blocked", json.dumps({"kind": "review_revision", "review_revision": {"reviewed_commit": candidate_a}}), 102),
    ]
    conn.executemany("INSERT INTO task_events VALUES (?, ?, ?, ?, ?, ?)", different_events)
    different_report = scanner.run_report(conn, days=1, focus_task="different")
    assert different_report["metrics"]["tasks_with_repeated_blocks_within_5m"] == 0

    # Short/full forms of the same candidate must pair as one repeat group.
    equivalent_events = [
        (4, "equivalent", None, "submitted_for_review", json.dumps({"diff_candidate_commit": candidate_c}), 1_000),
        (5, "equivalent", None, "blocked", json.dumps({"kind": "review_revision", "review_revision": {"reviewed_commit": candidate_c[:7]}}), 1_001),
        (6, "equivalent", None, "blocked", json.dumps({"kind": "review_revision", "review_revision": {"reviewed_commit": candidate_c}}), 1_002),
    ]
    conn.executemany("INSERT INTO task_events VALUES (?, ?, ?, ?, ?, ?)", equivalent_events)
    both_report = scanner.run_report(conn, days=1, focus_task="equivalent")
    assert both_report["metrics"]["tasks_with_repeated_blocks_within_5m"] == 1

    conn.execute("DELETE FROM task_events WHERE task_id = ?", ("different",))
    equivalent_report = scanner.run_report(conn, days=1, focus_task="equivalent")
    assert equivalent_report["metrics"]["tasks_with_repeated_blocks_within_5m"] == 1


def test_review_revision_resume_stage_is_authoritative_over_submission_stage():
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    conn.executescript(
        """
        CREATE TABLE task_events (id INTEGER PRIMARY KEY, task_id TEXT NOT NULL,
            run_id INTEGER, kind TEXT NOT NULL, payload TEXT, created_at INTEGER NOT NULL);
        CREATE TABLE task_runs (id INTEGER PRIMARY KEY, task_id TEXT NOT NULL,
            profile TEXT, outcome TEXT, verdict TEXT, status TEXT NOT NULL);
        """
    )
    conn.executemany(
        "INSERT INTO task_events VALUES (?, 'resume-stage', NULL, ?, ?, ?)",
        [
            (1, "submitted_for_review", json.dumps({"review_stage": 1, "diff_candidate_commit": "old-candidate"}), 100),
            (2, "blocked", json.dumps({"kind": "review_revision", "review_revision": {"resume_stage": 3, "reviewed_commit": "different-candidate"}}), 101),
        ],
    )
    report = scanner.run_report(conn, days=1, focus_task="resume-stage")
    assert report["metrics"]["blocks_by_review_stage"] == {"3": 1, "unknown": 0, "unmatched": 0}


def test_repeat_groups_do_not_cross_submission_or_lifecycle_epochs():
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    conn.executescript(
        """
        CREATE TABLE task_events (id INTEGER PRIMARY KEY, task_id TEXT NOT NULL,
            run_id INTEGER, kind TEXT NOT NULL, payload TEXT, created_at INTEGER NOT NULL);
        CREATE TABLE task_runs (id INTEGER PRIMARY KEY, task_id TEXT NOT NULL,
            profile TEXT, outcome TEXT, verdict TEXT, status TEXT NOT NULL);
        """
    )
    conn.executemany(
        "INSERT INTO task_events VALUES (?, 'epochs', NULL, ?, ?, ?)",
        [
            (1, "submitted_for_review", json.dumps({"diff_candidate_commit": "same-candidate"}), 100),
            (2, "blocked", json.dumps({"kind": "review_revision"}), 101),
            (3, "submitted_for_review", "{}", 102),
            (4, "blocked", json.dumps({"kind": "review_revision"}), 103),
            (5, "completed", "{}", 104),
            (6, "submitted_for_review", json.dumps({"diff_candidate_commit": "same-candidate"}), 105),
            (7, "blocked", json.dumps({"kind": "review_revision"}), 106),
            (8, "blocked", json.dumps({"kind": "review_revision"}), 107),
        ],
    )
    report = scanner.run_report(conn, days=1, focus_task="epochs")
    # Only the two blocks within the final submission epoch are repeats.
    assert report["metrics"]["tasks_with_repeated_blocks_within_5m"] == 1
    assert scanner.candidate_for_event(conn.execute("SELECT * FROM task_events ORDER BY id").fetchall(), 3) is None


def test_ambiguous_short_sha_does_not_transitively_group_distinct_full_shas():
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    conn.executescript(
        """
        CREATE TABLE task_events (id INTEGER PRIMARY KEY, task_id TEXT NOT NULL,
            run_id INTEGER, kind TEXT NOT NULL, payload TEXT, created_at INTEGER NOT NULL);
        CREATE TABLE task_runs (id INTEGER PRIMARY KEY, task_id TEXT NOT NULL,
            profile TEXT, outcome TEXT, verdict TEXT, status TEXT NOT NULL);
        """
    )
    prefix = "deadbee"
    full_a = prefix + "a" * 33
    full_b = prefix + "b" * 33
    conn.executemany(
        "INSERT INTO task_events VALUES (?, 'ambiguous', NULL, ?, ?, ?)",
        [
            (1, "submitted_for_review", json.dumps({"diff_candidate_commit": full_a}), 100),
            (2, "blocked", json.dumps({"kind": "review_revision", "review_revision": {"reviewed_commit": full_a}}), 101),
            (3, "blocked", json.dumps({"kind": "review_revision", "review_revision": {"reviewed_commit": prefix}}), 102),
            (4, "blocked", json.dumps({"kind": "review_revision", "review_revision": {"reviewed_commit": full_b}}), 103),
        ],
    )
    report = scanner.run_report(conn, days=1, focus_task="ambiguous")
    assert report["metrics"]["tasks_with_repeated_blocks_within_5m"] == 0


# ---------------------------------------------------------------------------
# Helper units
# ---------------------------------------------------------------------------

def test_same_candidate_needs_two_bounded_non_empty_spells():
    """An empty side can never match (and must not crash), and a side
    shorter than 7 chars is too ambiguous to claim SHA equality."""
    assert scanner.candidates_refer_to_same_candidate("", "abc1234567") is False
    assert scanner.candidates_refer_to_same_candidate("abc123", "abc1234567890") is False
    assert scanner.candidates_refer_to_same_candidate("abc1234567890", "abc1234") is True


def test_copy_readonly_snapshot_copies_the_given_database(tmp_path):
    """The snapshot must back up the GIVEN database through the ro URI —
    treating the URI as a plain filename would scan a brand-new empty db
    and report zero blocks on a real board."""
    db = tmp_path / "kanban.db"
    source = sqlite3.connect(db)
    source.execute("CREATE TABLE probe (x INTEGER)")
    source.execute("INSERT INTO probe VALUES (41)")
    source.commit()
    source.close()

    snapshot = scanner.copy_readonly_snapshot(db)
    try:
        assert snapshot.execute("SELECT x FROM probe").fetchone()[0] == 41
    finally:
        snapshot.close()


def _bare_board() -> sqlite3.Connection:
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    conn.executescript(
        """
        CREATE TABLE task_events (
            id INTEGER PRIMARY KEY,
            task_id TEXT NOT NULL,
            run_id INTEGER,
            kind TEXT NOT NULL,
            payload TEXT,
            created_at INTEGER NOT NULL
        );
        CREATE TABLE task_runs (
            id INTEGER PRIMARY KEY,
            task_id TEXT NOT NULL,
            profile TEXT,
            outcome TEXT,
            verdict TEXT,
            status TEXT NOT NULL
        );
        """
    )
    return conn


def test_block_without_payload_kind_counts_as_unclassified():
    """A blocked event with an empty payload must land in the literal
    'unclassified' kind bucket — the string 'None' would split the
    metrics and hide the block."""
    conn = _bare_board()
    conn.execute(
        "INSERT INTO task_events VALUES (1, 't1', NULL, 'blocked', '{}', 100)"
    )
    report = scanner.run_report(conn, days=1, focus_task="t1")
    assert report["metrics"]["blocks_by_kind"] == {"unclassified": 1}


def test_main_requires_the_output_flag(monkeypatch, tmp_path, capsys):
    """--output is required: without it the CLI must exit with a usage
    error, not crash later on a None path."""
    db = tmp_path / "kanban.db"
    sqlite3.connect(db).close()
    monkeypatch.setattr(
        "sys.argv", ["scan_kanban_block_notifications.py", "--db", str(db)]
    )
    try:
        scanner.main()
    except SystemExit as exc:
        assert exc.code != 0
    else:
        raise AssertionError("main without --output must exit non-zero")


def test_main_creates_nested_output_dir_and_tolerates_existing_one(
    monkeypatch, tmp_path
):
    """The report writer must create a missing nested output directory
    AND survive a second run into the same existing directory."""
    db = tmp_path / "board.db"
    conn = sqlite3.connect(db)
    conn.executescript(
        """
        CREATE TABLE task_events (
            id INTEGER PRIMARY KEY,
            task_id TEXT NOT NULL,
            run_id INTEGER,
            kind TEXT NOT NULL,
            payload TEXT,
            created_at INTEGER NOT NULL
        );
        CREATE TABLE task_runs (
            id INTEGER PRIMARY KEY,
            task_id TEXT NOT NULL,
            profile TEXT,
            outcome TEXT,
            verdict TEXT,
            status TEXT NOT NULL
        );
        """
    )
    conn.commit()
    conn.close()

    out = tmp_path / "nested" / "deep" / "report.json"
    monkeypatch.setattr(
        "sys.argv",
        ["scan_kanban_block_notifications.py", "--db", str(db), "--output", str(out)],
    )
    assert scanner.main() == 0
    assert json.loads(out.read_text(encoding="utf-8"))["schema_version"] == 1

    # second run: the parent directory now EXISTS — exist_ok must hold
    assert scanner.main() == 0
