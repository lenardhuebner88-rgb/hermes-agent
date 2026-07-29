"""Multiplex profile scoping for the cron executions ledger (A-C1).

``cron/executions.py`` used to freeze EXECUTIONS_FILE at import time, so under
``multiplex_profiles`` every profile shared the import-home ledger — leaking
execution history across profiles, colliding on job ids, and attributing
``recover_interrupted_executions`` to the wrong profile. The ledger now
resolves its store per call with the same precedence as
``cron/jobs.py::_current_cron_store()``; these tests pin that isolation.
"""

from __future__ import annotations

import sqlite3


def test_multiplex_profiles_write_isolated_ledgers(tmp_path):
    """Two profiles scoped via the cron-store ContextVar get separate
    ledgers, even for identical job ids; neither sees the other's rows."""
    import cron.executions as executions
    from cron.jobs import use_cron_store

    home_a = tmp_path / "profiles" / "alpha"
    home_b = tmp_path / "profiles" / "beta"

    # Prod scopes via cron.jobs.use_cron_store (scheduler_provider multiplex);
    # use_executions_store is the executions-local alias for the same scope.
    with use_cron_store(home_a):
        row_a = executions.create_execution("report", source="builtin")
        executions.finish_execution(row_a["id"], success=True)
    with executions.use_executions_store(home_b):
        row_b = executions.create_execution("report", source="builtin")

    assert (home_a / "cron" / "executions.db").exists()
    assert (home_b / "cron" / "executions.db").exists()
    assert row_a["id"] != row_b["id"]

    with use_cron_store(home_a):
        assert [r["id"] for r in executions.list_executions()] == [row_a["id"]]
        assert executions.latest_execution("report")["status"] == "completed"
    with use_cron_store(home_b):
        assert [r["id"] for r in executions.list_executions()] == [row_b["id"]]
        assert executions.latest_execution("report")["status"] == "claimed"


def test_recovery_attributes_to_scoped_profile_only(tmp_path):
    """recover_interrupted_executions under a profile scope only rewrites
    that profile's ledger — an identical job id in another profile's ledger
    stays claimed."""
    import cron.executions as executions
    from cron.jobs import use_cron_store

    home_a = tmp_path / "profiles" / "alpha"
    home_b = tmp_path / "profiles" / "beta"

    with use_cron_store(home_a):
        row_a = executions.create_execution("nightly", source="builtin")
    with use_cron_store(home_b):
        row_b = executions.create_execution("nightly", source="builtin")
        # Simulate B's scheduler having died mid-claim: foreign process_id
        # plus an impossible process-start fingerprint (same pattern as
        # test_execution_ledger.py::test_recovery_rejects_recycled_pid).
        with sqlite3.connect(home_b / "cron" / "executions.db") as conn:
            conn.execute(
                "UPDATE executions SET process_id=?, process_started_at=? WHERE id=?",
                ("dead-import", -1, row_b["id"]),
            )

        assert executions.recover_interrupted_executions() == 1
        recovered = executions.latest_execution("nightly")
        assert recovered["id"] == row_b["id"]
        assert recovered["status"] == "unknown"

    # A's row is owned by THIS process and, more importantly, lives in a
    # different ledger: recovery under A's scope must not touch B's file and
    # must see A's attempt still claimed.
    with use_cron_store(home_a):
        assert executions.recover_interrupted_executions() == 0
        assert executions.latest_execution("nightly")["id"] == row_a["id"]
        assert executions.latest_execution("nightly")["status"] == "claimed"

    # And B's recovery really did persist to B's ledger only.
    with sqlite3.connect(home_a / "cron" / "executions.db") as conn:
        statuses = [r[0] for r in conn.execute("SELECT status FROM executions")]
    assert statuses == ["claimed"]


def test_ledger_follows_hermes_home_repointed_after_import(tmp_path, monkeypatch):
    """A caller that re-points HERMES_HOME after cron.executions was imported
    writes its OWN ledger, not the one the import happened to freeze."""
    import cron.executions as executions

    home = tmp_path / "late-home"
    monkeypatch.setenv("HERMES_HOME", str(home))

    row = executions.create_execution("late", source="builtin")

    assert (home / "cron" / "executions.db").exists()
    assert executions.latest_execution("late")["id"] == row["id"]


def test_repointed_executions_file_constant_is_honored(tmp_path, monkeypatch):
    """The documented escape hatch — monkeypatching EXECUTIONS_FILE — still
    wins over HERMES_HOME resolution (precedence 2 over 3)."""
    import cron.executions as executions

    pointed = tmp_path / "pointed" / "executions.db"
    monkeypatch.setattr(executions, "EXECUTIONS_FILE", pointed)
    monkeypatch.setenv("HERMES_HOME", str(tmp_path / "elsewhere"))

    row = executions.create_execution("compat", source="builtin")

    assert pointed.exists()
    assert not (tmp_path / "elsewhere" / "cron" / "executions.db").exists()
    assert executions.latest_execution("compat")["id"] == row["id"]
