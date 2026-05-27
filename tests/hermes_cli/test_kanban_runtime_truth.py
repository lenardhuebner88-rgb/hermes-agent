import argparse
import json
import sqlite3
from pathlib import Path

from hermes_cli import kanban as kanban_cli
from hermes_cli import kanban_runtime_truth as krt


def _parse_kanban(argv):
    parser = argparse.ArgumentParser()
    sub = parser.add_subparsers(dest="cmd")
    kanban_cli.build_parser(sub)
    return parser.parse_args(["kanban", *argv])


def test_collect_processes_redacts_runtime_env_flags():
    ps_output = (
        "  PID STARTED CMD\n"
        "123 Wed May 27 12:36:38 2026 /home/piet/.hermes/hermes-agent/venv/bin/python -m hermes_cli.main gateway run\n"
        "999 Wed May 27 12:36:39 2026 /usr/bin/other\n"
    )

    processes = krt.collect_processes(
        ps_output=ps_output,
        env_reader=lambda _pid: {
            "HERMES_AUTHORING_LINT": "enforce",
            "HERMES_PROFILE": "reviewer",
            "HERMES_KANBAN_TASK": "t_secret",
        },
        cwd_reader=lambda _pid: "/home/piet/.hermes/hermes-agent",
    )

    assert len(processes) == 1
    assert processes[0]["pid"] == 123
    assert processes[0]["env"] == {
        "HERMES_AUTHORING_LINT": "set",
        "HERMES_PROFILE": "reviewer",
        "HERMES_KANBAN_TASK": "set",
    }


def test_collect_git_state_preserves_dirty_path_first_character(tmp_path, monkeypatch):
    def fake_git(_repo, *args):
        if args == ("branch", "--show-current"):
            return 0, "main", ""
        if args == ("rev-parse", "--short", "HEAD"):
            return 0, "abc123", ""
        if args == ("log", "-1", "--pretty=%s"):
            return 0, "subject", ""
        if args == ("status", "--short"):
            return 0, " M hermes_cli/kanban.py\n?? tests/example.py", ""
        raise AssertionError(args)

    monkeypatch.setattr(krt, "_run_git", fake_git)

    state = krt.collect_git_state(tmp_path)

    assert state["dirty_files"][0]["path"] == "hermes_cli/kanban.py"
    assert state["dirty_files"][1]["path"] == "tests/example.py"


def test_collect_scheduler_state_filters_kanban_relevant_jobs(tmp_path):
    jobs_path = tmp_path / "jobs.json"
    jobs_path.write_text(
        json.dumps(
            {
                "jobs": [
                    {
                        "name": "Stale-Run-Sweeper",
                        "enabled": True,
                        "state": "scheduled",
                        "schedule_display": "*/5 * * * *",
                        "last_run_at": "2026-05-27T20:20:00+02:00",
                        "next_run_at": "2026-05-27T20:25:00+02:00",
                        "last_status": "ok",
                        "script": "stale-run-sweeper.sh",
                        "no_agent": True,
                        "deliver": "local",
                    },
                    {"name": "Completely unrelated", "enabled": True},
                ]
            }
        ),
        encoding="utf-8",
    )

    state = krt.collect_scheduler_state(jobs_path)

    assert state["exists"] is True
    assert [job["name"] for job in state["jobs"]] == ["Stale-Run-Sweeper"]
    assert state["jobs"][0]["script"] == "stale-run-sweeper.sh"


def test_collect_db_state_reads_counts_without_writes(tmp_path):
    db_path = tmp_path / "kanban.db"
    conn = sqlite3.connect(db_path)
    conn.executescript(
        """
        CREATE TABLE tasks (id TEXT, status TEXT);
        CREATE TABLE task_runs (
          id INTEGER,
          status TEXT,
          worker_exit_kind TEXT,
          worker_protocol_state TEXT
        );
        CREATE TABLE task_events (
          id INTEGER,
          task_id TEXT,
          run_id INTEGER,
          kind TEXT,
          created_at INTEGER
        );
        INSERT INTO tasks VALUES ('t_1', 'done'), ('t_2', 'archived');
        INSERT INTO task_runs VALUES (1, 'done', 'clean_exit_complete', 'complete_emitted');
        INSERT INTO task_runs VALUES (2, 'crashed', NULL, NULL);
        INSERT INTO task_events VALUES (1, 't_1', 1, 'completed', 100);
        INSERT INTO task_events VALUES (2, 't_2', NULL, 'gate_decision_parity', 200);
        INSERT INTO task_events VALUES (3, 't_2', NULL, 'heartbeat', 300);
        """
    )
    conn.commit()
    conn.close()
    before_mtime = db_path.stat().st_mtime_ns

    state = krt.collect_db_state(db_path=db_path)

    assert state["exists"] is True
    assert state["tasks_by_status"] == {"archived": 1, "done": 1}
    assert state["runs_by_status"] == {"crashed": 1, "done": 1}
    assert state["latest_event"]["kind"] == "heartbeat"
    assert state["event_counts"]["gate_decision_parity"] == 1
    assert state["event_counts"]["heartbeat"] == 1
    assert db_path.stat().st_mtime_ns == before_mtime


def test_build_runtime_truth_combines_sources(tmp_path, monkeypatch):
    jobs_path = tmp_path / "jobs.json"
    jobs_path.write_text('{"jobs": []}', encoding="utf-8")
    db_path = tmp_path / "missing.db"
    monkeypatch.setattr(
        krt,
        "collect_git_state",
        lambda repo: {"repo_root": str(repo), "branch": "main", "head": "abc123", "dirty": False},
    )
    monkeypatch.setattr(krt, "collect_processes", lambda ps_output=None: [])

    report = krt.build_runtime_truth(
        board="default",
        repo_root=tmp_path,
        now_ts=1779900000,
        config={"kanban": {"dispatch_in_gateway": False}},
        jobs_path=jobs_path,
        db_path=db_path,
    )

    assert report["generated_at"] == 1779900000
    assert report["board"] == "default"
    assert report["git"]["head"] == "abc123"
    assert report["config"]["kanban"]["dispatch_in_gateway"] is False
    assert report["db"]["exists"] is False
    assert "no_db_writes" in report["non_actions"]


def test_runtime_truth_cli_json_does_not_initialize_db(tmp_path, monkeypatch, capsys):
    home = tmp_path / ".hermes"
    home.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(home))
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    monkeypatch.setattr(
        krt,
        "build_runtime_truth",
        lambda **_kwargs: {
            "generated_at": 1,
            "board": "default",
            "git": {},
            "processes": [],
            "config": {"kanban": {}},
            "scheduler": {"jobs": []},
            "db": {"exists": False},
            "non_actions": ["no_db_writes"],
        },
    )

    rc = kanban_cli.kanban_command(_parse_kanban(["runtime-truth", "--json"]))

    assert rc == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["non_actions"] == ["no_db_writes"]
    assert not (home / "kanban.db").exists()


def test_render_runtime_truth_markdown_includes_core_sources():
    rendered = krt.render_runtime_truth_markdown(
        {
            "board": "default",
            "git": {"branch": "main", "head": "abc", "dirty": True, "dirty_files": []},
            "processes": [{"pid": 1, "start_local": "now", "cmd": "hermes", "env": {}}],
            "scheduler": {"jobs": [{"name": "Stale-Run-Sweeper", "enabled": True}]},
            "db": {
                "path": "/tmp/kanban.db",
                "tasks_by_status": {"done": 1},
                "runs_by_status": {},
                "event_counts": {"heartbeat": 0},
                "latest_event": None,
            },
            "non_actions": ["no_db_writes"],
        }
    )

    assert "Kanban Runtime Truth" in rendered
    assert "Branch: `main`" in rendered
    assert "Stale-Run-Sweeper" in rendered
    assert "`no_db_writes`" in rendered
