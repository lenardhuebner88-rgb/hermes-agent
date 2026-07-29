"""Audit loop 23b / F1: composite (claimed_at, id) cursor pagination.

list_executions() ordered by ``julianday(claimed_at) DESC, id DESC`` since
loop 20, but the cursor compared ONLY ``before_claimed_at``: rows whose
julianday(claimed_at) is IDENTICAL to the cursor row's were silently
skipped — page 2 lost every tie. The optional ``before_id`` parameter now
makes the cursor composite: ``(jd(claimed_at), id) < (jd(cursor), cursor_id)``
continues ties onto the next page in id order — no gap, no duplicate.
Callers that keep passing only ``before_claimed_at`` get the unchanged
strictly-older behavior (backward compatible).

NOTE: ``id`` is a TEXT uuid, so the ``id DESC`` tiebreak is lexicographic,
not insertion order. The tests therefore derive the expected order from
``sorted(ids, reverse=True)`` instead of assuming it.
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


TIE = "2026-07-29T12:00:00+00:00"


def _seed_ties(executions, job_id="tied", n=3):
    """n rows with EXACTLY identical claimed_at; returns ids in the list's
    expected newest-first order (jd tie ⇒ id DESC, lexicographic)."""
    ids = [_run(executions, job_id) for _ in range(n)]
    for execution_id in ids:
        _set_claimed(executions, execution_id, TIE)
    return sorted(ids, reverse=True)


class TestCompositeCursorTies:
    def test_page2_returns_ties_completely(self, monkeypatch, tmp_path):
        """3 rows with EXACTLY identical claimed_at; page 1 takes the two
        first ids in tiebreak order, page 2 (composite cursor from page 1's
        last row) must return the remaining tie — the old claimed_at-only
        cursor dropped it."""
        executions = _point_ledger(monkeypatch, tmp_path)
        ordered = _seed_ties(executions)

        page1 = executions.list_executions(job_id="tied", limit=2)
        assert [row["id"] for row in page1] == ordered[:2]

        page2 = executions.list_executions(
            job_id="tied", limit=10,
            before_claimed_at=page1[-1]["claimed_at"],
            before_id=page1[-1]["id"],
        )
        assert [row["id"] for row in page2] == ordered[2:]

    def test_full_pagination_no_gap_no_duplicate(self, monkeypatch, tmp_path):
        """Paginating through ALL rows with the composite cursor must visit
        every row exactly once — ties split across the boundary included."""
        executions = _point_ledger(monkeypatch, tmp_path)
        ordered = _seed_ties(executions, n=5)

        seen: list = []
        cursor_at, cursor_id = None, None
        for _ in range(6):
            kwargs = {"job_id": "tied", "limit": 2}
            if cursor_at is not None:
                kwargs["before_claimed_at"] = cursor_at
                kwargs["before_id"] = cursor_id
            page = executions.list_executions(**kwargs)
            if not page:
                break
            seen.extend(row["id"] for row in page)
            cursor_at, cursor_id = page[-1]["claimed_at"], page[-1]["id"]
        assert seen == ordered
        assert len(seen) == len(set(seen))  # no duplicates

    def test_cursor_row_itself_not_repeated(self, monkeypatch, tmp_path):
        """The composite comparison is STRICT on both components: the cursor
        row must not reappear on the next page."""
        executions = _point_ledger(monkeypatch, tmp_path)
        ordered = _seed_ties(executions)

        page = executions.list_executions(
            job_id="tied", limit=10, before_claimed_at=TIE, before_id=ordered[1],
        )
        assert [row["id"] for row in page] == ordered[2:]
        assert ordered[1] not in [row["id"] for row in page]

    def test_ties_mixed_with_older_rows(self, monkeypatch, tmp_path):
        """Composite cursor also works when the tie group sits on top of
        genuinely older rows: ties continue in id order, older rows follow
        by time."""
        executions = _point_ledger(monkeypatch, tmp_path)
        ordered = _seed_ties(executions, n=3)
        old = _run(executions, "tied")
        _set_claimed(executions, old, "2026-07-29T11:00:00+00:00")  # older

        page1 = executions.list_executions(job_id="tied", limit=2)
        assert [row["id"] for row in page1] == ordered[:2]

        page2 = executions.list_executions(
            job_id="tied", limit=10,
            before_claimed_at=page1[-1]["claimed_at"],
            before_id=page1[-1]["id"],
        )
        assert [row["id"] for row in page2] == ordered[2:] + [old]


class TestCursorBackwardCompatibility:
    def test_claimed_at_only_cursor_keeps_old_contract(self, monkeypatch, tmp_path):
        """A caller passing only before_claimed_at gets the unchanged
        strictly-older behavior — the API stays backward compatible and
        before_id is purely optional."""
        executions = _point_ledger(monkeypatch, tmp_path)
        ordered = _seed_ties(executions)
        old = _run(executions, "tied")
        _set_claimed(executions, old, "2026-07-29T11:00:00+00:00")

        page = executions.list_executions(
            job_id="tied", limit=10, before_claimed_at=TIE,
        )
        # Old contract: strictly older — ties (including the cursor row)
        # are excluded, only the genuinely older row remains.
        assert [row["id"] for row in page] == [old]
        assert not any(row["id"] in ordered for row in page)

    def test_before_id_without_claimed_at_is_ignored(self, monkeypatch, tmp_path):
        """before_id alone has no time component to compare against — it
        must not filter anything."""
        executions = _point_ledger(monkeypatch, tmp_path)
        ordered = _seed_ties(executions)

        rows = executions.list_executions(
            job_id="tied", limit=10, before_id=ordered[1],
        )
        assert [row["id"] for row in rows] == ordered

    def test_cursor_with_mixed_offset_spelling(self, monkeypatch, tmp_path):
        """The cursor claimed_at may be spelled with a different offset —
        the composite comparison still runs on the julianday() scale."""
        executions = _point_ledger(monkeypatch, tmp_path)
        ordered = _seed_ties(executions)

        page = executions.list_executions(
            job_id="tied", limit=10,
            before_claimed_at="2026-07-29T14:00:00+02:00",  # == TIE (12:00Z)
            before_id=ordered[0],
        )
        assert [row["id"] for row in page] == ordered[1:]
