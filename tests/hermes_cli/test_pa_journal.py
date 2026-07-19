from __future__ import annotations

import argparse
import json
import re
import sqlite3
from datetime import date, datetime, timezone
from pathlib import Path

import pytest

import hermes_cli.pa_journal as journal
from cron.blueprint_catalog import fill_blueprint, get_blueprint
from hermes_cli.pa_chat import PAStore, SOL_MODEL
from hermes_cli.subcommands.cron import build_cron_parser

JOURNAL_DATE = date(2033, 5, 6)
NOW = datetime(2033, 5, 6, 21, 45, tzinfo=timezone.utc)


@pytest.fixture
def isolated_journal_sources(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> dict[str, Path]:
    home = tmp_path / "home"
    hermes_home = home / ".hermes"
    journal_dir = tmp_path / "memsearch" / "shared" / "memory"
    hermes_home.mkdir(parents=True)
    journal_dir.mkdir(parents=True)
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("HERMES_HOME", str(hermes_home))
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: home))
    monkeypatch.setattr(journal, "JARVIS_JOURNAL_DIR", journal_dir)

    PAStore().ensure_schema()
    question_db = hermes_home / "question_events.db"
    conn = sqlite3.connect(question_db)
    conn.executescript(
        """
        CREATE TABLE question_events (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            ts TEXT NOT NULL,
            updated_ts TEXT NOT NULL,
            source TEXT NOT NULL,
            kind TEXT,
            question_text TEXT NOT NULL,
            status TEXT NOT NULL,
            answered_by TEXT,
            answer_source TEXT,
            action_payload TEXT,
            action_result TEXT
        );
        """
    )
    conn.close()
    return {
        "home": home,
        "hermes_home": hermes_home,
        "journal_dir": journal_dir,
        "question_db": question_db,
    }


def _add_pa_turn(*, timestamp: int) -> None:
    store = PAStore()
    turn_id = store.create_turn(
        text="Was habe ich heute geliefert?",
        engine="sol",
        model=SOL_MODEL,
        project_scope=None,
        attachments=[],
        now=timestamp,
    )
    assert store.set_running(turn_id, now=timestamp)
    store.finish_turn(turn_id, "Zwei Slices sind fertig.", now=timestamp)


def _add_action_and_inbox(question_db: Path) -> None:
    stamp = "2033-05-06T12:00:00.000000Z"
    envelope = {"version": 1, "category": "kanban.nudge", "payload": {"card_id": "t_1"}}
    evidence = {
        "version": 1,
        "category": "kanban.nudge",
        "status": "succeeded",
        "executed": True,
        "result": {"ok": True},
    }
    conn = sqlite3.connect(question_db)
    conn.execute(
        "INSERT INTO question_events(ts, updated_ts, source, kind, question_text, "
        "status, answered_by, answer_source, action_payload, action_result) "
        "VALUES (?, ?, 'pa', 'pa_action', 'Nudge?', 'answered', 'operator', "
        "'operator_free', ?, ?)",
        (stamp, stamp, json.dumps(envelope), json.dumps(evidence)),
    )
    conn.execute(
        "INSERT INTO question_events(ts, updated_ts, source, kind, question_text, "
        "status, answered_by, answer_source, action_payload, action_result) "
        "VALUES (?, ?, 'hook', 'codex', 'Darf ich fortfahren?', 'open', NULL, "
        "NULL, NULL, NULL)",
        (stamp, stamp),
    )
    conn.commit()
    conn.close()


def _mark_evening_brief(*, timestamp: int) -> None:
    store = PAStore()
    with store.connect() as conn:
        conn.executescript(
            """
            CREATE TABLE IF NOT EXISTS pa_brief_state (
                kind TEXT PRIMARY KEY,
                last_brief_ts INTEGER NOT NULL,
                last_payload_hash TEXT NOT NULL,
                last_content_hash TEXT NOT NULL,
                last_inbox_hash TEXT NOT NULL,
                updated_at INTEGER NOT NULL
            );
            """
        )
        conn.execute(
            "INSERT INTO pa_brief_state VALUES ('evening', ?, 'p', 'c', 'i', ?)",
            (timestamp, timestamp),
        )


def test_memsearch_format_and_all_daily_sources_are_in_engine_prompt(
    isolated_journal_sources: dict[str, Path], monkeypatch: pytest.MonkeyPatch
) -> None:
    timestamp = int(NOW.replace(hour=12).timestamp())
    _add_pa_turn(timestamp=timestamp)
    _add_action_and_inbox(isolated_journal_sources["question_db"])
    _mark_evening_brief(timestamp=timestamp)
    seen: dict[str, object] = {}

    def fake_engine(
        engine: str, prompt: str, *, model: str, image_paths: list[Path]
    ) -> str:
        seen.update(engine=engine, prompt=prompt, model=model, image_paths=image_paths)
        return "- Ich habe zwei Slices begleitet.\n- Ich habe eine Aktion belegt ausgeführt."

    monkeypatch.setattr(journal, "run_engine", fake_engine)

    path = journal.write_daily_journal(now=NOW)

    assert path == isolated_journal_sources["journal_dir"] / "2033-05-06-jarvis.md"
    assert path is not None
    content = path.read_text(encoding="utf-8")
    assert content.startswith("## Session 21:45\n\n### 21:45\n")
    assert (
        "<!-- session:jarvis journal:2033-05-06 source:hermes_cli.pa_journal -->"
        in content
    )
    assert content.count("- **Jarvis-Tagebuch**") == 1
    assert "- Ich habe zwei Slices begleitet." in content
    assert (path.stat().st_mode & 0o777) == 0o664
    assert seen["engine"] == "sol"
    assert seen["model"] == SOL_MODEL
    assert seen["image_paths"] == []
    prompt = str(seen["prompt"])
    assert '"pa_turns":[{' in prompt
    assert '"executor_actions":[{' in prompt
    assert '"inbox_development":[{' in prompt
    assert '"evening":{"status":"delivered"' in prompt


def test_daily_file_is_replaced_not_appended(
    isolated_journal_sources: dict[str, Path], monkeypatch: pytest.MonkeyPatch
) -> None:
    _add_pa_turn(timestamp=int(NOW.replace(hour=12).timestamp()))
    outputs = iter(("- Ich bin der erste Eintrag.", "- Ich ersetze den ersten Eintrag."))
    monkeypatch.setattr(journal, "run_engine", lambda *args, **kwargs: next(outputs))

    first = journal.write_daily_journal(now=NOW)
    second = journal.write_daily_journal(now=NOW)

    assert first == second
    assert second is not None
    content = second.read_text(encoding="utf-8")
    assert "Ich bin der erste Eintrag" not in content
    assert content.count("Ich ersetze den ersten Eintrag") == 1
    assert content.count("## Session") == 1


def test_engine_failure_writes_deterministic_raw_journal(
    isolated_journal_sources: dict[str, Path], monkeypatch: pytest.MonkeyPatch
) -> None:
    _add_pa_turn(timestamp=int(NOW.replace(hour=12).timestamp()))
    monkeypatch.setattr(
        journal,
        "run_engine",
        lambda *args, **kwargs: (_ for _ in ()).throw(RuntimeError("engine offline")),
    )

    path = journal.write_daily_journal(now=NOW)

    assert path is not None
    content = path.read_text(encoding="utf-8")
    assert "Ich habe heute 1 PA-Turns begleitet; davon endeten 0 mit einem Fehler." in content
    assert "Was habe ich heute geliefert?" in content


def test_silent_when_nothing_happened_and_cli_prints_contract(
    isolated_journal_sources: dict[str, Path],
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    monkeypatch.setattr(
        journal,
        "run_engine",
        lambda *args, **kwargs: pytest.fail("engine must not run for an empty day"),
    )

    assert journal.write_daily_journal(now=NOW) is None
    assert list(isolated_journal_sources["journal_dir"].iterdir()) == []
    monkeypatch.setattr(journal, "write_daily_journal", lambda: None)
    assert journal.main([]) == 0
    assert capsys.readouterr().out == "[SILENT]\n"


def test_word_limit_is_bounded_to_about_400_words(
    isolated_journal_sources: dict[str, Path], monkeypatch: pytest.MonkeyPatch
) -> None:
    _add_pa_turn(timestamp=int(NOW.replace(hour=12).timestamp()))
    monkeypatch.setattr(journal, "run_engine", lambda *args, **kwargs: "wort " * 450)

    path = journal.write_daily_journal(now=NOW)

    assert path is not None
    body = path.read_text(encoding="utf-8")
    journal_body = body.split("- **Jarvis-Tagebuch**\n", 1)[1]
    # The body adds only the Markdown bullet marker and truncation ellipsis.
    assert len(re.findall(r"\S+", journal_body)) <= journal.MAX_JOURNAL_WORDS + 2
    assert "…" in body


def test_jarvis_journal_blueprint_loads_and_command_parses() -> None:
    blueprint = get_blueprint("jarvis-tagebuch")
    assert blueprint is not None
    spec = fill_blueprint(blueprint, {})
    assert spec == {
        "prompt": blueprint.prompt_template,
        "schedule": "45 21 * * *",
        "name": "Jarvis Tagebuch",
        "deliver": "local",
    }

    parser = argparse.ArgumentParser()
    subparsers = parser.add_subparsers(dest="command")
    build_cron_parser(subparsers, cmd_cron=lambda args: None)
    args = parser.parse_args(
        [
            "cron",
            "add",
            "45 21 * * *",
            blueprint.prompt_template,
            "--name",
            "Jarvis Tagebuch",
            "--deliver",
            "local",
            "--workdir",
            "/home/piet/.hermes/hermes-agent",
        ]
    )
    assert args.cron_command == "add"
    assert args.schedule == "45 21 * * *"
    assert args.prompt == blueprint.prompt_template
    assert args.deliver == "local"
    assert args.workdir == "/home/piet/.hermes/hermes-agent"
