"""Process-level coverage for scripts/kanban-await.py."""

from __future__ import annotations

import json
import os
from pathlib import Path
import signal
import sqlite3
import subprocess
import sys
import time


REPO_ROOT = Path(__file__).resolve().parents[2]
SCRIPT = REPO_ROOT / "scripts" / "kanban-await.py"


def _database(path: Path) -> None:
    with sqlite3.connect(path) as connection:
        connection.execute(
            """
            CREATE TABLE task_events (
                id         INTEGER PRIMARY KEY AUTOINCREMENT,
                task_id    TEXT NOT NULL,
                run_id     INTEGER,
                kind       TEXT NOT NULL,
                payload    TEXT,
                created_at INTEGER NOT NULL
            )
            """
        )


def _insert_event(
    path: Path,
    task_id: str,
    kind: str,
    payload: object,
    *,
    created_at: int = 1_754_069_400,
) -> int:
    with sqlite3.connect(path) as connection:
        cursor = connection.execute(
            "INSERT INTO task_events (task_id, kind, payload, created_at) "
            "VALUES (?, ?, ?, ?)",
            (task_id, kind, json.dumps(payload), created_at),
        )
    return int(cursor.lastrowid)


def _command(
    path: Path,
    *,
    task_id: str = "t_target",
    until: str = "completed",
    timeout: float = 1,
    since_id: int | None = None,
) -> tuple[list[str], dict[str, str]]:
    command = [
        sys.executable,
        str(SCRIPT),
        "--task",
        task_id,
        "--until",
        until,
        "--timeout",
        str(timeout),
        "--interval",
        "0.02",
        "--json",
    ]
    if since_id is not None:
        command.extend(("--since-id", str(since_id)))
    environment = os.environ.copy()
    environment["HERMES_KANBAN_DB"] = str(path)
    return command, environment


def _run(path: Path, **kwargs: object) -> subprocess.CompletedProcess[str]:
    command, environment = _command(path, **kwargs)
    return subprocess.run(
        command,
        cwd=REPO_ROOT,
        env=environment,
        capture_output=True,
        text=True,
        timeout=5,
    )


def test_since_id_replays_preexisting_event_but_default_seed_skips_it(tmp_path: Path):
    database = tmp_path / "kanban.db"
    _database(database)
    event_id = _insert_event(database, "t_target", "completed", {"result": "done"})

    replayed = _run(database, since_id=event_id - 1, timeout=0.2)
    assert replayed.returncode == 0
    assert json.loads(replayed.stdout)["id"] == event_id

    started = time.monotonic()
    skipped = _run(database, timeout=0.12)
    elapsed = time.monotonic() - started
    assert skipped.returncode == 2
    assert skipped.stdout == ""
    assert skipped.stderr.count("\n") == 1
    assert "timeout after 0.12s" in skipped.stderr
    assert 0.1 <= elapsed < 1.5


def test_output_id_is_a_round_trip_cursor(tmp_path: Path):
    database = tmp_path / "kanban.db"
    _database(database)
    first_id = _insert_event(database, "t_target", "blocked", {"reason": "review"})
    second_id = _insert_event(database, "t_target", "crashed", {"exit_code": 1})

    first = _run(database, until="blocked,crashed", since_id=0)
    assert first.returncode == 0
    assert len(first.stdout.splitlines()) == 1
    first_event = json.loads(first.stdout)
    assert first_event == {
        "id": first_id,
        "task_id": "t_target",
        "kind": "blocked",
        "payload": {"reason": "review"},
        "created_at": 1_754_069_400,
    }

    second = _run(
        database,
        until="blocked,crashed",
        since_id=first_event["id"],
    )
    assert second.returncode == 0
    assert len(second.stdout.splitlines()) == 1
    assert json.loads(second.stdout)["id"] == second_id
    assert json.loads(second.stdout)["kind"] == "crashed"


def test_matching_kind_for_another_task_does_not_finish_wait(tmp_path: Path):
    database = tmp_path / "kanban.db"
    _database(database)
    _insert_event(database, "t_other", "gave_up", {"attempts": 3})

    result = _run(database, until="gave_up", since_id=0, timeout=0.12)

    assert result.returncode == 2
    assert result.stdout == ""
    assert "task 't_target'" in result.stderr


def test_default_seed_waits_for_event_written_after_start(tmp_path: Path):
    database = tmp_path / "kanban.db"
    _database(database)
    command, environment = _command(database, until="gave_up", timeout=3)
    process = subprocess.Popen(
        command,
        cwd=REPO_ROOT,
        env=environment,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )
    time.sleep(0.3)
    event_id = _insert_event(database, "t_target", "gave_up", {"attempts": 3})

    stdout, stderr = process.communicate(timeout=5)

    assert process.returncode == 0
    assert stderr == ""
    assert json.loads(stdout)["id"] == event_id


def test_empty_until_is_a_usage_error(tmp_path: Path):
    database = tmp_path / "kanban.db"
    _database(database)

    result = _run(database, until=" , ")

    assert result.returncode == 2
    assert result.stdout == ""
    assert "--until must contain at least one event kind" in result.stderr


def test_sigint_and_sigterm_exit_cleanly_with_distinct_codes(tmp_path: Path):
    database = tmp_path / "kanban.db"
    _database(database)
    for signum, signal_name in (
        (signal.SIGINT, "SIGINT"),
        (signal.SIGTERM, "SIGTERM"),
    ):
        command, environment = _command(database, until="not_yet", timeout=5)
        process = subprocess.Popen(
            command,
            cwd=REPO_ROOT,
            env=environment,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
        )
        time.sleep(0.3)
        process.send_signal(signum)

        stdout, stderr = process.communicate(timeout=5)

        assert process.returncode == 128 + signum
        assert stdout == ""
        assert stderr == f"kanban-await: interrupted by {signal_name}\n"
        assert "Traceback" not in stderr
