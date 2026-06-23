import os
from pathlib import Path


def test_kanban_path_pins_do_not_leak_from_ambient_environment():
    from hermes_cli import kanban_db

    hermes_home = Path(os.environ["HERMES_HOME"])
    assert "HERMES_KANBAN_DB" not in os.environ
    assert "HERMES_KANBAN_HOME" not in os.environ
    assert "HERMES_KANBAN_WORKSPACES_ROOT" not in os.environ
    assert kanban_db.kanban_home() == hermes_home
    assert kanban_db.kanban_db_path() == hermes_home / "kanban.db"
    assert str(kanban_db.kanban_db_path()).startswith(str(hermes_home))


def test_kanban_tests_can_still_opt_into_explicit_kanban_home(monkeypatch, tmp_path):
    from hermes_cli import kanban_db

    explicit_home = tmp_path / "explicit-kanban-home"
    monkeypatch.setenv("HERMES_KANBAN_HOME", str(explicit_home))

    assert kanban_db.kanban_home() == explicit_home
    assert kanban_db.kanban_db_path() == explicit_home / "kanban.db"
