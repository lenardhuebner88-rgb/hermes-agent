"""Tests for hermes_cli/sqlite_util (idempotent migration + IMMEDIATE write txn).

Uses in-memory SQLite with isolation_level=None (autocommit) so the explicit
BEGIN IMMEDIATE / COMMIT / ROLLBACK the helpers issue are honored verbatim —
this is how the projects/kanban stores drive real transactions.
"""

from __future__ import annotations

import sqlite3

import pytest

from hermes_cli.sqlite_util import add_column_if_missing, write_txn


@pytest.fixture
def conn():
    c = sqlite3.connect(":memory:", isolation_level=None)
    c.execute("CREATE TABLE t (id INTEGER)")
    yield c
    c.close()


class TestAddColumnIfMissing:
    def test_adds_missing_column_and_returns_true(self, conn):
        added = add_column_if_missing(conn, "t", "newcol", "newcol TEXT")
        assert added is True
        cols = {row[1] for row in conn.execute("PRAGMA table_info(t)")}
        assert "newcol" in cols

    def test_second_call_is_idempotent_returns_false(self, conn):
        assert add_column_if_missing(conn, "t", "newcol", "newcol TEXT") is True
        # The duplicate-column error a concurrent migrator would hit is swallowed.
        assert add_column_if_missing(conn, "t", "newcol", "newcol TEXT") is False

    def test_non_duplicate_operational_error_is_reraised(self, conn):
        with pytest.raises(sqlite3.OperationalError):
            # "no such table" is NOT a duplicate-column error -> must propagate.
            add_column_if_missing(conn, "missing_table", "c", "c TEXT")


class TestWriteTxn:
    def test_commits_on_success(self, conn):
        with write_txn(conn):
            conn.execute("INSERT INTO t VALUES (1)")
        assert conn.execute("SELECT COUNT(*) FROM t").fetchone()[0] == 1

    def test_rolls_back_on_exception_and_propagates(self, conn):
        with pytest.raises(RuntimeError, match="boom"):
            with write_txn(conn):
                conn.execute("INSERT INTO t VALUES (1)")
                raise RuntimeError("boom")
        # The insert must not survive the rollback.
        assert conn.execute("SELECT COUNT(*) FROM t").fetchone()[0] == 0

    def test_connection_is_usable_after_rolled_back_txn(self, conn):
        with pytest.raises(RuntimeError):
            with write_txn(conn):
                conn.execute("INSERT INTO t VALUES (9)")
                raise RuntimeError("fail")
        # A subsequent clean transaction still works (no leaked txn state).
        with write_txn(conn):
            conn.execute("INSERT INTO t VALUES (2)")
        assert conn.execute("SELECT COUNT(*) FROM t").fetchone()[0] == 1
