from __future__ import annotations

import argparse
import json
import os
import sqlite3
from pathlib import Path

import pytest

import hermes_cli.pa_brief as brief
from cron.blueprint_catalog import fill_blueprint, get_blueprint
from hermes_cli.pa_chat import PAStore
from hermes_cli.subcommands.cron import build_cron_parser


@pytest.fixture
def isolated_brief_home(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> dict[str, Path]:
    home = tmp_path / "home"
    hermes_home = home / ".hermes"
    vault = tmp_path / "vault"
    db_path = tmp_path / "kanban.db"
    hermes_home.mkdir(parents=True)
    (vault / "03-Agents").mkdir(parents=True)
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("HERMES_HOME", str(hermes_home))
    monkeypatch.setenv("HERMES_KANBAN_DB", str(db_path))
    monkeypatch.setenv("OBSIDIAN_VAULT_PATH", str(vault))
    monkeypatch.setattr(
        brief,
        "build_inbox",
        lambda: {"generated_at": 0, "items": [], "errors": []},
    )
    conn = sqlite3.connect(db_path)
    conn.executescript(
        """
        CREATE TABLE tasks (
            id TEXT PRIMARY KEY,
            title TEXT NOT NULL,
            status TEXT NOT NULL
        );
        CREATE TABLE task_events (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            task_id TEXT NOT NULL,
            kind TEXT NOT NULL,
            payload TEXT,
            created_at INTEGER NOT NULL
        );
        """
    )
    conn.close()
    return {"home": home, "hermes_home": hermes_home, "vault": vault, "db": db_path}


def _insert_event(
    db_path: Path,
    *,
    task_id: str,
    title: str,
    status: str,
    kind: str,
    ts: int,
    payload: dict[str, object] | None = None,
) -> None:
    conn = sqlite3.connect(db_path)
    conn.execute(
        "INSERT OR REPLACE INTO tasks(id, title, status) VALUES (?, ?, ?)",
        (task_id, title, status),
    )
    conn.execute(
        "INSERT INTO task_events(task_id, kind, payload, created_at) "
        "VALUES (?, ?, ?, ?)",
        (task_id, kind, json.dumps(payload) if payload else None, ts),
    )
    conn.commit()
    conn.close()


def _add_receipt(vault: Path, *, ts: int, title: str = "Test ship") -> Path:
    path = vault / "03-Agents" / "Codex" / "receipts" / "ship-receipt.md"
    path.parent.mkdir(parents=True)
    path.write_text(f"---\nstatus: complete\n---\n# {title}\n", encoding="utf-8")
    os.utime(path, (ts, ts))
    return path


def test_first_run_then_followup_reads_only_new_task_events_and_advances_state(
    isolated_brief_home: dict[str, Path], monkeypatch: pytest.MonkeyPatch
) -> None:
    now = 2_000_000_000
    _insert_event(
        isolated_brief_home["db"],
        task_id="t_new",
        title="Neue Karte",
        status="ready",
        kind="created",
        ts=now - 60,
        payload={"status": "ready"},
    )
    prompts: list[str] = []

    def fake_engine(
        engine: str,
        prompt: str,
        *,
        model: str,
        image_paths: list[Path],
    ) -> str:
        assert (engine, model, image_paths) == ("sol", "gpt-5.6-sol", [])
        prompts.append(prompt)
        return f"Brief {len(prompts)}"

    monkeypatch.setattr(brief.time, "time", lambda: now)
    monkeypatch.setattr(brief, "run_engine", fake_engine)

    assert brief.deliver_brief("morning") == "Brief 1"
    state = brief.BriefStore().get_state("morning")
    assert state.last_brief_ts == now
    assert '"event":"created"' in prompts[0]

    _insert_event(
        isolated_brief_home["db"],
        task_id="t_done",
        title="Fertige Karte",
        status="done",
        kind="completed",
        ts=now + 30,
    )
    monkeypatch.setattr(brief.time, "time", lambda: now + 60)

    assert brief.deliver_brief("morning") == "Brief 2"
    assert '"event":"completed"' in prompts[1]
    assert '"event":"created"' not in prompts[1]

    page = PAStore().message_page()
    assert [message["content"] for message in page["messages"]] == [
        "Brief 1",
        "Brief 2",
    ]
    assert {message["engine"] for message in page["messages"]} == {"pa-brief"}
    assert {message["model"] for message in page["messages"]} == {
        "tagesbrief-v1"
    }


def test_silent_when_delta_and_inbox_are_empty_without_engine_call(
    isolated_brief_home: dict[str, Path], monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(brief.time, "time", lambda: 2_000_000_000)

    def should_not_run(*args: object, **kwargs: object) -> str:
        raise AssertionError("engine must stay silent")

    monkeypatch.setattr(brief, "run_engine", should_not_run)

    assert brief.build_daily_brief("morning") is None
    assert brief.deliver_brief("evening") is None
    assert PAStore().message_page()["messages"] == []


def test_engine_failure_falls_back_to_bounded_raw_german_brief(
    isolated_brief_home: dict[str, Path], monkeypatch: pytest.MonkeyPatch
) -> None:
    now = 2_000_000_000
    _insert_event(
        isolated_brief_home["db"],
        task_id="t_block",
        title="Deployment blockiert",
        status="blocked",
        kind="blocked",
        ts=now - 5,
        payload={"reason": "Gate rot"},
    )
    monkeypatch.setattr(brief.time, "time", lambda: now)
    monkeypatch.setattr(
        brief,
        "run_engine",
        lambda *args, **kwargs: (_ for _ in ()).throw(RuntimeError("offline")),
    )

    result = brief.build_daily_brief("evening")

    assert result is not None
    assert result.startswith("Abendlicher Jarvis Ship-Report")
    assert "Deployment blockiert" in result
    assert "1 blockiert" in result


def test_same_inbox_snapshot_is_not_posted_twice_on_retry(
    isolated_brief_home: dict[str, Path], monkeypatch: pytest.MonkeyPatch
) -> None:
    now = 2_000_000_000
    inbox = {
        "generated_at": now,
        "items": [
            {
                "type": "question",
                "id": "q1",
                "title": "Freigabe erteilen?",
                "block_radius": 3,
                "ts": now - 10,
            }
        ],
        "errors": [],
    }
    monkeypatch.setattr(brief, "build_inbox", lambda: inbox)
    clock = {"now": now}
    monkeypatch.setattr(brief.time, "time", lambda: clock["now"])
    calls = 0

    def fake_engine(*args: object, **kwargs: object) -> str:
        nonlocal calls
        calls += 1
        return "Eine Entscheidung wartet."

    monkeypatch.setattr(brief, "run_engine", fake_engine)

    assert brief.deliver_brief("morning") == "Eine Entscheidung wartet."
    clock["now"] += 60
    assert brief.deliver_brief("morning") is None
    assert calls == 1
    assert len(PAStore().message_page()["messages"]) == 1


def test_source_failures_are_isolated_and_receipt_still_produces_fallback(
    isolated_brief_home: dict[str, Path], monkeypatch: pytest.MonkeyPatch
) -> None:
    now = 2_000_000_000
    _add_receipt(isolated_brief_home["vault"], ts=now - 1, title="S3 geliefert")
    monkeypatch.setattr(brief.time, "time", lambda: now)
    monkeypatch.setattr(
        brief,
        "_collect_kanban_delta",
        lambda *args, **kwargs: (_ for _ in ()).throw(sqlite3.OperationalError("busy")),
    )
    monkeypatch.setattr(
        brief,
        "run_engine",
        lambda *args, **kwargs: (_ for _ in ()).throw(RuntimeError("offline")),
    )

    result = brief.build_daily_brief("morning")

    assert result is not None
    assert "S3 geliefert" in result
    assert "kanban: busy" in result


def test_brief_state_migration_is_additive_to_existing_pa_database(
    isolated_brief_home: dict[str, Path],
) -> None:
    pa_store = PAStore()
    turn_id = pa_store.create_turn(
        text="Altbestand",
        engine="sol",
        model="gpt-5.6-sol",
        project_scope=None,
        attachments=[],
        now=123,
    )
    assert pa_store.set_running(turn_id, now=124)
    pa_store.finish_turn(turn_id, "Bleibt erhalten", now=125)

    store = brief.BriefStore()
    store.ensure_schema()
    store.ensure_schema()

    with pa_store.connect() as conn:
        tables = {
            row[0]
            for row in conn.execute(
                "SELECT name FROM sqlite_master WHERE type='table'"
            ).fetchall()
        }
        columns = {
            row[1] for row in conn.execute("PRAGMA table_info(pa_brief_state)")
        }
    assert "pa_brief_state" in tables
    assert {
        "kind",
        "last_brief_ts",
        "last_payload_hash",
        "last_content_hash",
        "last_inbox_hash",
        "updated_at",
    } <= columns
    assert pa_store.get_turn(turn_id)["reply"] == "Bleibt erhalten"


def test_cli_entry_prints_silent_and_rejects_unknown_kind(
    isolated_brief_home: dict[str, Path],
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    monkeypatch.setattr(brief, "deliver_brief", lambda kind: None)

    assert brief.main(["morning"]) == 0
    assert capsys.readouterr().out == "[SILENT]\n"
    with pytest.raises(SystemExit) as exc_info:
        brief.main(["noon"])
    assert exc_info.value.code == 2


def test_jarvis_blueprints_and_documented_cron_commands_validate() -> None:
    morning = get_blueprint("jarvis-tagesbrief-am")
    evening = get_blueprint("jarvis-tagesbrief-pm")
    assert morning is not None and evening is not None
    morning_spec = fill_blueprint(morning, {})
    evening_spec = fill_blueprint(evening, {})
    assert morning_spec == {
        "prompt": morning.prompt_template,
        "schedule": "45 7 * * *",
        "name": "Jarvis Tagesbrief AM",
        "deliver": "local",
    }
    assert evening_spec == {
        "prompt": evening.prompt_template,
        "schedule": "30 21 * * *",
        "name": "Jarvis Tagesbrief PM",
        "deliver": "local",
    }

    parser = argparse.ArgumentParser()
    subparsers = parser.add_subparsers(dest="command")
    build_cron_parser(subparsers, cmd_cron=lambda args: None)
    for schedule, prompt, name in (
        (
            "45 7 * * *",
            morning.prompt_template,
            "Jarvis Tagesbrief AM",
        ),
        (
            "30 21 * * *",
            evening.prompt_template,
            "Jarvis Tagesbrief PM",
        ),
    ):
        args = parser.parse_args(
            [
                "cron",
                "add",
                schedule,
                prompt,
                "--name",
                name,
                "--deliver",
                "local",
                "--workdir",
                "/home/piet/.hermes/hermes-agent",
            ]
        )
        assert args.cron_command == "add"
        assert args.schedule == schedule
        assert args.prompt == prompt
        assert args.deliver == "local"
        assert args.workdir == "/home/piet/.hermes/hermes-agent"
