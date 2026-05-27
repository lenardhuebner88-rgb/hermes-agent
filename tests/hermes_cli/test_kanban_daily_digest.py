from __future__ import annotations

import argparse
import json
import sqlite3
from pathlib import Path

import pytest

from hermes_cli import kanban as kc
from hermes_cli import kanban_daily_digest as kdd
from hermes_cli import kanban_db as kb


EXPECTED_NON_ACTIONS = [
    "standard_kanban_cli_startup_may_init_or_migrate_db",
    "no_task_changes",
    "no_run_changes",
    "no_event_changes",
    "no_dispatch",
    "no_cron_activation",
    "no_delivery",
]


@pytest.fixture
def kanban_home(tmp_path, monkeypatch):
    home = tmp_path / ".hermes"
    home.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(home))
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    kb._INITIALIZED_PATHS.clear()
    kb.init_db()
    return home


def _full_report_metadata() -> dict:
    return {
        "report_contract_version": 1,
        "verification_evidence": ["unit tests"],
        "receipt_reference": "vault/receipt.md",
        "scope_contract_read": True,
        "scope_contract_version": 2,
        "scope_attestation": True,
        "forbidden_actions_taken": 0,
    }


def _run_cli(argv: list[str]) -> int:
    parser = argparse.ArgumentParser(prog="hermes")
    sub = parser.add_subparsers(dest="command")
    kc.build_parser(sub)
    args = parser.parse_args(["kanban", *argv])
    return kc.kanban_command(args)


def test_clean_db_returns_empty_valid_digest(kanban_home, tmp_path):
    with kb.connect() as conn:
        digest = kdd.build_daily_digest(
            conn,
            now_ts=1_700_000_000,
            signal_path=tmp_path / "missing.jsonl",
        )

    assert digest["summary"]["tasks_total"] == 0
    assert digest["summary"]["tasks_with_diagnostics"] == 0
    assert digest["summary"]["diagnostics_total"] == 0
    assert digest["summary"]["highest_severity"] is None
    assert digest["groups"] == []
    assert digest["external_signals"] == []
    assert digest["non_actions"] == EXPECTED_NON_ACTIONS


def test_groups_diagnostics_by_kind_and_severity_with_bounded_tasks(kanban_home, tmp_path):
    with kb.connect() as conn:
        first = kb.create_task(conn, title="first missing evidence", assignee="coder")
        second = kb.create_task(conn, title="second missing evidence", assignee="coder")
        clean = kb.create_task(conn, title="clean completion", assignee="coder")
        kb.complete_task(conn, first, summary="done", metadata={"report_contract_version": 1})
        kb.complete_task(conn, second, summary="done", metadata={"report_contract_version": 1})
        kb.complete_task(conn, clean, summary="done", metadata=_full_report_metadata())

        digest = kdd.build_daily_digest(
            conn,
            now_ts=1_700_000_000,
            signal_path=tmp_path / "signals.jsonl",
            max_tasks_per_group=1,
        )

    assert digest["summary"]["tasks_with_diagnostics"] == 2
    assert digest["summary"]["highest_severity"] == "error"
    missing_evidence = next(
        group for group in digest["groups"] if group["kind"] == "missing_verification_evidence"
    )
    assert missing_evidence["severity"] == "error"
    assert missing_evidence["count"] == 2
    assert len(missing_evidence["tasks"]) == 1
    assert missing_evidence["omitted_tasks"] == 1
    assert "receipt" not in {task["title"] for task in missing_evidence["tasks"]}
    assert digest["groups"][0]["severity"] == "error"


def test_load_daily_digest_signals_tolerates_missing_malformed_and_since(tmp_path):
    path = tmp_path / "daily_digest_signals.jsonl"
    path.write_text(
        "\n".join(
            [
                "not-json",
                json.dumps({"kind": "old", "ts": "2026-05-26T00:00:00Z"}),
                json.dumps({"kind": "fresh", "ts": "2026-05-27T12:00:00Z"}),
                json.dumps(["not", "object"]),
                json.dumps({"kind": "timeless", "evidence": {"note": "kept"}}),
            ]
        ),
        encoding="utf-8",
    )
    since = 1_779_871_200  # 2026-05-27T00:00:00Z

    signals = kdd.load_daily_digest_signals(path, since_ts=since)

    assert [signal["kind"] for signal in signals] == ["fresh", "timeless"]
    assert signals[0]["ts_epoch"] >= since


def test_markdown_renderer_includes_summary_signals_and_non_actions(kanban_home, tmp_path):
    signals = tmp_path / "signals.jsonl"
    signals.write_text(
        json.dumps({"kind": "hub-memory-growth-alert", "ts": "2026-05-27T12:00:00Z"}) + "\n",
        encoding="utf-8",
    )
    with kb.connect() as conn:
        task_id = kb.create_task(conn, title="needs evidence", assignee="coder")
        kb.complete_task(conn, task_id, summary="done", metadata={"report_contract_version": 1})
        digest = kdd.build_daily_digest(
            conn,
            now_ts=1_779_914_400,  # 2026-05-27T12:00:00Z
            since_ts=1_779_828_000,  # 24h prior
            signal_path=signals,
        )

    rendered = kdd.render_daily_digest_markdown(digest)

    assert "Daily Monitoring Digest — ERROR" in rendered
    assert "missing_verification_evidence" in rendered
    assert "hub-memory-growth-alert" in rendered
    assert "standard_kanban_cli_startup_may_init_or_migrate_db" in rendered
    assert "no_task_changes" in rendered
    assert "no_run_changes" in rendered
    assert "no_event_changes" in rendered
    assert "no_dispatch" in rendered
    assert "no_cron_activation" in rendered
    assert "no_delivery" in rendered


def test_cli_daily_digest_json_is_operationally_read_only(kanban_home, tmp_path, capsys):
    signals = tmp_path / "signals.jsonl"
    signals.write_text(
        json.dumps({"kind": "hub-memory-growth-alert", "ts": "2026-05-27T12:00:00Z"}) + "\n",
        encoding="utf-8",
    )
    with kb.connect() as conn:
        task_id = kb.create_task(conn, title="cli missing evidence", assignee="coder")
        kb.complete_task(conn, task_id, summary="done", metadata={"report_contract_version": 1})
        before_events = conn.execute("SELECT COUNT(*) FROM task_events").fetchone()[0]
        before_runs = conn.execute("SELECT COUNT(*) FROM task_runs").fetchone()[0]

    code = _run_cli(
        [
            "daily-digest",
            "--json",
            "--signal-path",
            str(signals),
            "--max-tasks-per-group",
            "2",
        ]
    )

    captured = capsys.readouterr()
    assert code == 0
    payload = json.loads(captured.out)
    assert payload["summary"]["diagnostics_total"] >= 1
    assert payload["summary"]["external_signals_total"] == 1
    assert payload["non_actions"] == EXPECTED_NON_ACTIONS
    with kb.connect() as conn:
        after_events = conn.execute("SELECT COUNT(*) FROM task_events").fetchone()[0]
        after_runs = conn.execute("SELECT COUNT(*) FROM task_runs").fetchone()[0]
    assert after_events == before_events
    assert after_runs == before_runs


def test_cli_daily_digest_contract_allows_standard_auto_init_without_content_mutation(
    tmp_path, monkeypatch, capsys
):
    home = tmp_path / ".hermes"
    home.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(home))
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    kb._INITIALIZED_PATHS.clear()

    code = _run_cli(["daily-digest", "--json", "--signal-path", str(tmp_path / "missing.jsonl")])

    captured = capsys.readouterr()
    assert code == 0
    payload = json.loads(captured.out)
    assert payload["summary"]["tasks_total"] == 0
    assert payload["summary"]["diagnostics_total"] == 0
    assert payload["non_actions"] == EXPECTED_NON_ACTIONS
    assert (home / "kanban.db").exists()
    with sqlite3.connect(home / "kanban.db") as conn:
        assert conn.execute("SELECT COUNT(*) FROM tasks").fetchone()[0] == 0
        assert conn.execute("SELECT COUNT(*) FROM task_runs").fetchone()[0] == 0
        assert conn.execute("SELECT COUNT(*) FROM task_events").fetchone()[0] == 0
