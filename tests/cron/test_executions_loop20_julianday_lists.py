"""Audit loop 20 / F4: julianday ordering for the execution-ledger lists.

The loop-18 prune already compares times through SQLite julianday(), but
list_executions()/latest_executions() still ordered and cursor-compared RAW
ISO strings — and a tz-aware ISO string's offset varies with the
configured/local timezone, so mixed offsets misorder: a lexicographically
larger local-time string can be the EARLIER instant
("2026-07-29T12:00:00+02:00" = 10:00Z sorts after "2026-07-29T11:30:00+00:00"
= 11:30Z as raw text, yet is older). Ordering, cursor pagination and the
per-job "latest" pick now all go through julianday() — one consistent UTC
scale, matching the prune.

These tests pin rows with deliberately mixed offsets (rewritten verbatim
into the ledger, like loop 18) and assert true-time ordering, cursor page 2,
and latest_executions() agreeing with the list head.
"""

from __future__ import annotations

import sqlite3


def _point_ledger(monkeypatch, tmp_path):
    import cron.executions as executions

    monkeypatch.setattr(
        executions, "EXECUTIONS_FILE", tmp_path / "cron" / "executions.db"
    )
    return executions


def _run(executions, job_id):
    row = executions.create_execution(job_id, source="builtin")
    executions.finish_execution(row["id"], success=True)
    return row["id"]


def _set_claimed(executions, execution_id, claimed_at: str):
    """Rewrite one row's claimed_at verbatim (raw string, any ISO variant)."""
    conn = sqlite3.connect(executions._current_executions_file())
    try:
        conn.execute(
            "UPDATE executions SET claimed_at=? WHERE id=?",
            (claimed_at, execution_id),
        )
        conn.commit()
    finally:
        conn.close()


# Three instants whose TRUE order is C > B > A (newest-first), while the
# RAW-STRING order is A > C > B — the loop-20 bug exactly:
#   A: 2026-07-29T12:00:00+02:00 = 10:00Z  (string sorts FIRST — raw bug)
#   B: 2026-07-29T09:30:00-01:00 = 10:30Z  (string sorts LAST — raw bug)
#   C: 2026-07-29T11:30:00+00:00 = 11:30Z
CLAIMED_A = "2026-07-29T12:00:00+02:00"
CLAIMED_B = "2026-07-29T09:30:00-01:00"
CLAIMED_C = "2026-07-29T11:30:00+00:00"


def _seed_mixed(executions, job_id="mixed"):
    ids = {}
    for tag, claimed in (("a", CLAIMED_A), ("b", CLAIMED_B), ("c", CLAIMED_C)):
        ids[tag] = _run(executions, job_id)
        _set_claimed(executions, ids[tag], claimed)
    return ids


class TestListExecutionsJuliandayOrdering:
    def test_mixed_offsets_sort_by_true_time(self, monkeypatch, tmp_path):
        executions = _point_ledger(monkeypatch, tmp_path)
        ids = _seed_mixed(executions)

        rows = executions.list_executions(job_id="mixed", limit=10)
        got = [row["id"] for row in rows]
        assert got == [ids["c"], ids["b"], ids["a"]]  # true newest-first

    def test_cursor_page2_uses_julianday_comparison(self, monkeypatch, tmp_path):
        """Cursor pagination must agree with the ordering: page 1 takes the
        two newest, page 2 (before_claimed_at = last row of page 1) returns
        exactly the remaining oldest row — no overlap, no gap."""
        executions = _point_ledger(monkeypatch, tmp_path)
        ids = _seed_mixed(executions)

        page1 = executions.list_executions(job_id="mixed", limit=2)
        assert [row["id"] for row in page1] == [ids["c"], ids["b"]]

        cursor = page1[-1]["claimed_at"]  # CLAIMED_B, a -01:00 offset string
        page2 = executions.list_executions(
            job_id="mixed", limit=10, before_claimed_at=cursor,
        )
        assert [row["id"] for row in page2] == [ids["a"]]

    def test_cursor_with_mixed_offset_string_filters_correctly(
        self, monkeypatch, tmp_path,
    ):
        """The cursor value itself may carry a weird offset: an instant equal
        to the cursor must NOT reappear on the next page (strictly older),
        and everything genuinely newer must stay out."""
        executions = _point_ledger(monkeypatch, tmp_path)
        ids = _seed_mixed(executions)

        # Cursor = B's instant, expressed with yet another offset spelling.
        page = executions.list_executions(
            job_id="mixed", limit=10,
            before_claimed_at="2026-07-29T10:30:00+00:00",  # == CLAIMED_B
        )
        assert [row["id"] for row in page] == [ids["a"]]

        page = executions.list_executions(
            job_id="mixed", limit=10,
            before_claimed_at="2026-07-29T13:30:00+02:00",  # == CLAIMED_C
        )
        assert [row["id"] for row in page] == [ids["b"], ids["a"]]

    def test_unfiltered_list_spans_jobs_in_true_time_order(
        self, monkeypatch, tmp_path,
    ):
        executions = _point_ledger(monkeypatch, tmp_path)
        ids = _seed_mixed(executions, job_id="j1")
        other = _run(executions, "j2")
        _set_claimed(executions, other, "2026-07-29T11:00:00+00:00")  # 11:00Z

        rows = executions.list_executions(limit=10)
        assert [row["id"] for row in rows] == [ids["c"], other, ids["b"], ids["a"]]


class TestLatestExecutionsJuliandayOrdering:
    def test_latest_picks_true_newest_with_mixed_offsets(
        self, monkeypatch, tmp_path,
    ):
        executions = _point_ledger(monkeypatch, tmp_path)
        ids = _seed_mixed(executions, job_id="j1")
        d1 = _run(executions, "j2")
        _set_claimed(executions, d1, CLAIMED_B)  # 10:30Z
        d2 = _run(executions, "j2")
        _set_claimed(executions, d2, CLAIMED_A)  # 10:00Z — string-sort NEWER

        latest = executions.latest_executions(["j1", "j2"])
        assert latest["j1"]["id"] == ids["c"]
        # Raw-string ordering would wrongly pick d2 (12:00 > 09:30 as text);
        # true time picks d1 (10:30Z > 10:00Z).
        assert latest["j2"]["id"] == d1

    def test_latest_agrees_with_list_head(self, monkeypatch, tmp_path):
        """latest_executions() and list_executions(limit=1) must be the same
        row — both orderings now live on the julianday scale."""
        executions = _point_ledger(monkeypatch, tmp_path)
        ids = _seed_mixed(executions, job_id="j1")

        head = executions.list_executions(job_id="j1", limit=1)[0]
        latest = executions.latest_executions(["j1"])["j1"]
        assert head["id"] == latest["id"] == ids["c"]

    def test_naive_and_z_suffixed_variants_normalize(self, monkeypatch, tmp_path):
        """'Z' suffix and space separator are ISO variants julianday() also
        normalizes onto the UTC scale."""
        executions = _point_ledger(monkeypatch, tmp_path)
        z = _run(executions, "j")
        _set_claimed(executions, z, "2026-07-29T10:00:00Z")
        spaced = _run(executions, "j")
        _set_claimed(executions, spaced, "2026-07-29 12:30:00+02:00")  # 10:30Z

        rows = executions.list_executions(job_id="j", limit=10)
        assert [row["id"] for row in rows] == [spaced, z]
        assert executions.latest_executions(["j"])["j"]["id"] == spaced
