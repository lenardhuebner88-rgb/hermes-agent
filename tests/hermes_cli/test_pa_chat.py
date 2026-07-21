from __future__ import annotations

import json
import os
import sqlite3
import subprocess
import sys
import threading
import time
from pathlib import Path

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

import hermes_cli.pa_chat as pa
from hermes_cli import agent_questions as aq


@pytest.fixture
def isolated_pa_home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    home = tmp_path / "home"
    hermes_home = home / ".hermes"
    hermes_home.mkdir(parents=True)
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("HERMES_HOME", str(hermes_home))
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: home))
    return hermes_home


def _poll(client: TestClient, turn_id: str, expected: str) -> dict[str, object]:
    deadline = time.monotonic() + 3
    payload: dict[str, object] = {}
    while time.monotonic() < deadline:
        response = client.get(f"/api/pa/turns/{turn_id}")
        assert response.status_code == 200
        payload = response.json()
        if payload["status"] == expected:
            return payload
        time.sleep(0.01)
    raise AssertionError(f"turn never reached {expected}: {payload}")


def test_store_wal_busy_timeout_roundtrip_and_idempotent_schema(
    isolated_pa_home: Path,
) -> None:
    store = pa.PAStore()

    store.ensure_schema()
    store.ensure_schema()
    with store.connect() as conn:
        assert conn.execute("PRAGMA journal_mode").fetchone()[0] in {"wal", "delete"}
        assert (
            conn.execute("PRAGMA busy_timeout").fetchone()[0] == pa.DB_BUSY_TIMEOUT_MS
        )

    turn_id = store.create_turn(
        text="Was ist offen?",
        engine=pa.ENGINE_NAME,
        model=pa.SOL_MODEL,
        project_scope="hermes-infra",
        attachments=[],
        now=1_700_000_000,
    )
    assert store.get_turn(turn_id)["status"] == "pending"
    assert store.set_running(turn_id, now=1_700_000_001)
    store.finish_turn(turn_id, "Zwei Aufgaben sind offen.", now=1_700_000_002)

    turn = store.get_turn(turn_id)
    assert turn == {
        "turn_id": turn_id,
        "status": "done",
        "reply": "Zwei Aufgaben sind offen.",
        "engine": pa.ENGINE_NAME,
        "model": pa.SOL_MODEL,
        "ts": 1_700_000_000,
        "error": None,
        "rating": None,
    }
    assert [(row["role"], row["content"]) for row in store.recent_messages()] == [
        ("user", "Was ist offen?"),
        ("assistant", "Zwei Aufgaben sind offen."),
    ]


def test_message_attachment_migration_preserves_legacy_rows(
    isolated_pa_home: Path,
) -> None:
    db_path = isolated_pa_home / "pa" / "legacy.db"
    db_path.parent.mkdir(parents=True)
    conn = sqlite3.connect(db_path)
    conn.executescript(
        """
        CREATE TABLE pa_conversations (
            id TEXT PRIMARY KEY, created_at INTEGER NOT NULL, updated_at INTEGER NOT NULL
        );
        CREATE TABLE pa_turns (
            id TEXT PRIMARY KEY,
            conversation_id TEXT NOT NULL REFERENCES pa_conversations(id),
            status TEXT NOT NULL,
            reply TEXT,
            error TEXT,
            engine TEXT NOT NULL,
            model TEXT NOT NULL,
            project_scope TEXT,
            attachments_json TEXT NOT NULL DEFAULT '[]',
            ts INTEGER NOT NULL,
            updated_ts INTEGER NOT NULL
        );
        CREATE TABLE pa_messages (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            conversation_id TEXT NOT NULL REFERENCES pa_conversations(id),
            turn_id TEXT NOT NULL REFERENCES pa_turns(id),
            role TEXT NOT NULL,
            content TEXT NOT NULL,
            engine TEXT NOT NULL,
            model TEXT NOT NULL,
            ts INTEGER NOT NULL
        );
        INSERT INTO pa_conversations VALUES ('default', 1, 1);
        INSERT INTO pa_turns VALUES (
            'legacy-turn', 'default', 'done', 'alt', NULL,
            'sol', 'gpt-5.6-sol', NULL, '[]', 1, 1
        );
        INSERT INTO pa_messages(
            conversation_id, turn_id, role, content, engine, model, ts
        ) VALUES ('default', 'legacy-turn', 'assistant', 'alt', 'sol', 'gpt-5.6-sol', 1);
        """
    )
    conn.commit()
    conn.close()

    store = pa.PAStore(db_path)
    store.ensure_schema()

    with store.connect() as migrated:
        columns = {
            str(row[1]) for row in migrated.execute("PRAGMA table_info(pa_messages)")
        }
    assert "attachments_json" in columns
    page = store.message_page()
    assert page["messages"][0]["content"] == "alt"
    assert page["messages"][0]["attachments"] == []


def test_adapter_argv_prompt_history_images_and_no_resume(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    image = tmp_path / "photo.png"
    image.write_bytes(b"\x89PNG\r\n\x1a\nfixture")
    seen: dict[str, object] = {}

    def fake_run(argv: list[str], **kwargs: object) -> subprocess.CompletedProcess[str]:
        seen["argv"] = argv
        seen["kwargs"] = kwargs
        return subprocess.CompletedProcess(
            argv, 0, stdout="Geerdete Antwort\n", stderr=""
        )

    monkeypatch.setattr(pa.subprocess, "run", fake_run)
    prompt = pa.compose_prompt(
        text="Und jetzt?",
        context_pack='{"projects":[{"name":"Hermes"}]}',
        history=[
            {"role": "user", "content": "Was ist offen?"},
            {"role": "assistant", "content": "Zwei Aufgaben."},
        ],
    )
    reply = pa.run_sol_engine(prompt, model=pa.SOL_MODEL, image_paths=[image])

    assert reply == "Geerdete Antwort"
    assert pa.PA_SYSTEM_PROMPT in prompt
    assert '"projects"' in prompt
    assert "Was ist offen?" in prompt
    argv = seen["argv"]
    assert Path(str(argv[0])).name == "hermes"
    assert argv[1:3] == ["chat", "-Q"]
    assert argv[argv.index("-m") + 1] == pa.SOL_MODEL
    assert argv[argv.index("-t") + 1] == pa.READ_ONLY_TOOLSETS
    assert pa.READ_ONLY_TOOLSETS == "context_engine"
    assert "search" not in argv
    assert "--resume" not in argv
    assert argv.count("--image") == 1
    assert argv[argv.index("--image") + 1] == str(image)


def test_adapter_without_attachments_has_no_image_flag(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        pa.subprocess,
        "run",
        lambda argv, **kwargs: subprocess.CompletedProcess(
            argv, 0, stdout="ok\n", stderr=""
        ),
    )
    assert pa.run_sol_engine("prompt", model=pa.SOL_MODEL, image_paths=[]) == "ok"


@pytest.mark.parametrize(
    ("model", "cli_model"),
    [
        ("opus-4.8", "claude-opus-4-8"),
        ("fable-5", "claude-fable-5"),
    ],
)
def test_claude_argv_is_stateless_text_only_and_read_only(
    model: str, cli_model: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(pa, "_claude_bin", lambda: "/opt/bin/claude")

    argv = pa.build_claude_argv("prompt", model=model, image_paths=[])

    assert argv[:3] == ["/opt/bin/claude", "-p", "prompt"]
    assert argv[argv.index("--model") + 1] == cli_model
    assert argv[argv.index("--permission-mode") + 1] == "plan"
    assert argv[argv.index("--tools") + 1] == ""
    assert argv[argv.index("--output-format") + 1] == "text"
    assert "--no-session-persistence" in argv
    assert "--resume" not in argv
    assert "--continue" not in argv
    assert "-c" not in argv


def test_kimi_argv_is_one_shot_text_only_without_auto_approval(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(pa, "_kimi_bin", lambda: "/opt/bin/kimi")

    argv = pa.build_kimi_argv("prompt", model="k3", image_paths=[])

    assert argv[:3] == ["/opt/bin/kimi", "-p", "prompt"]
    assert argv[argv.index("-m") + 1] == "kimi-code/k3"
    assert argv[argv.index("--output-format") + 1] == "text"
    assert "--plan" not in argv
    assert "--session" not in argv
    assert "-S" not in argv
    assert "--continue" not in argv
    assert "-c" not in argv
    assert "--yolo" not in argv
    assert "--auto" not in argv


def test_qwen_argv_is_stateless_safe_mode_one_shot(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(pa, "_qwen_bin", lambda: "/opt/bin/qwen")

    argv = pa.build_qwen_argv("prompt", model="qwen3.7-plus", image_paths=[])

    assert argv[:3] == ["/opt/bin/qwen", "-p", "prompt"]
    assert argv[argv.index("-m") + 1] == "qwen3.7-plus"
    assert argv[argv.index("-o") + 1] == "text"
    assert "--safe-mode" in argv
    assert "--resume" not in argv
    assert "--continue" not in argv
    assert "-c" not in argv


def test_qwen_argv_inlines_images_as_at_refs(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(pa, "_qwen_bin", lambda: "/opt/bin/qwen")

    argv = pa.build_qwen_argv(
        "Was ist das?", model="qwen3.7-plus", image_paths=[Path("/tmp/a.png")]
    )

    prompt = argv[argv.index("-p") + 1]
    assert "@/tmp/a.png" in prompt
    assert "Was ist das?" in prompt
    assert "--image" not in argv


def test_qwen_argv_rejects_cross_engine_model(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(pa, "_qwen_bin", lambda: "/opt/bin/qwen")

    with pytest.raises(pa.PAEngineError, match="PA-Modell passt nicht zur Engine"):
        pa.build_qwen_argv("prompt", model="qwen3.7-max", image_paths=[])


def test_engine_registry_has_complete_roster_and_vision_contract() -> None:
    assert {
        engine: {
            "models": spec.models,
            "default_model": spec.default_model,
            "supports_images": spec.supports_images,
        }
        for engine, spec in pa.ENGINE_REGISTRY.items()
    } == {
        "sol": {
            "models": ("gpt-5.6-sol",),
            "default_model": "gpt-5.6-sol",
            "supports_images": True,
        },
        "claude": {
            "models": ("opus-4.8", "fable-5"),
            "default_model": "opus-4.8",
            "supports_images": False,
        },
        "kimi": {
            "models": ("k3",),
            "default_model": "k3",
            "supports_images": False,
        },
        "qwen": {
            "models": ("qwen3.7-plus",),
            "default_model": "qwen3.7-plus",
            "supports_images": True,
        },
    }


@pytest.mark.parametrize(
    ("engine", "model"),
    [("sol", "gpt-5.6-sol"), ("claude", "opus-4.8"), ("kimi", "k3"), ("qwen", "qwen3.7-plus")],
)
def test_run_engine_maps_nonzero_exit(
    engine: str, model: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(
        pa.subprocess,
        "run",
        lambda argv, **kwargs: subprocess.CompletedProcess(
            argv, 7, stdout="", stderr="adapter down"
        ),
    )

    with pytest.raises(pa.PAEngineError, match="Engine-Fehler: adapter down"):
        pa.run_engine(engine, "prompt", model=model, image_paths=[])


def test_run_engine_maps_subprocess_timeout(monkeypatch: pytest.MonkeyPatch) -> None:
    def timeout(*args: object, **kwargs: object) -> subprocess.CompletedProcess[str]:
        raise subprocess.TimeoutExpired("claude", pa.TURN_TIMEOUT_SECONDS)

    monkeypatch.setattr(pa.subprocess, "run", timeout)

    with pytest.raises(pa.PAEngineError, match="Engine-Zeitlimit erreicht"):
        pa.run_engine("claude", "prompt", model="fable-5", image_paths=[])


def test_run_engine_maps_missing_binary(monkeypatch: pytest.MonkeyPatch) -> None:
    def missing(*args: object, **kwargs: object) -> subprocess.CompletedProcess[str]:
        raise FileNotFoundError("kimi")

    monkeypatch.setattr(pa.subprocess, "run", missing)

    with pytest.raises(pa.PAEngineError, match="Engine nicht verfügbar"):
        pa.run_engine("kimi", "prompt", model="k3", image_paths=[])


def test_run_engine_rejects_empty_stdout(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        pa.subprocess,
        "run",
        lambda argv, **kwargs: subprocess.CompletedProcess(
            argv, 0, stdout=" \n", stderr=""
        ),
    )

    with pytest.raises(pa.PAEngineError, match="Engine lieferte keine Antwort"):
        pa.run_engine("claude", "prompt", model="opus-4.8", image_paths=[])


def test_run_engine_qwen_runs_in_image_dir_for_workspace_access(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    seen: dict[str, object] = {}

    def fake_run(argv: list[str], **kwargs: object) -> subprocess.CompletedProcess[str]:
        seen.update(kwargs)
        return subprocess.CompletedProcess(argv, 0, stdout="ok", stderr="")

    monkeypatch.setattr(pa.subprocess, "run", fake_run)
    monkeypatch.setattr(pa, "_qwen_bin", lambda: "/opt/bin/qwen")

    pa.run_engine(
        "qwen", "prompt", model="qwen3.7-plus", image_paths=[Path("/up/a.png")]
    )
    assert seen["cwd"] == "/up"

    pa.run_engine("qwen", "prompt", model="qwen3.7-plus", image_paths=[])
    assert seen["cwd"] is None

    pa.run_engine("sol", "prompt", model="gpt-5.6-sol", image_paths=[Path("/up/a.png")])
    assert seen["cwd"] is None


def test_api_pending_to_done_history_upload_and_attachment(
    isolated_pa_home: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    release = threading.Event()
    seen: dict[str, object] = {}

    def fake_context(project_scope: str | None) -> str:
        return json.dumps({"project_scope": project_scope, "open": 3})

    def fake_engine(
        engine: str, prompt: str, *, model: str, image_paths: list[Path]
    ) -> str:
        seen.update(engine=engine, prompt=prompt, model=model, image_paths=image_paths)
        assert release.wait(2)
        return "Es sind drei Punkte offen (Board-Zustand)."

    monkeypatch.setattr(pa, "build_context_pack", fake_context)
    monkeypatch.setattr(pa, "run_engine", fake_engine)
    app = FastAPI()
    pa.register_pa_routes(app)

    with TestClient(app) as client:
        upload = client.post(
            "/api/pa/upload",
            files={"file": ("board.png", b"\x89PNG\r\n\x1a\nfixture", "image/png")},
        )
        assert upload.status_code == 200
        asset_id = upload.json()["asset_id"]

        response = client.post(
            "/api/pa/message",
            json={
                "text": "Was ist offen?",
                "project_scope": "hermes-infra",
                "model": "sol",
                "attachments": [{"asset_id": asset_id}],
            },
        )
        assert response.status_code == 200
        turn_id = response.json()["turn_id"]
        pending = client.get(f"/api/pa/turns/{turn_id}")
        assert pending.status_code == 200
        assert pending.json()["status"] in {"pending", "running"}

        release.set()
        done = _poll(client, turn_id, "done")
        assert done["reply"] == "Es sind drei Punkte offen (Board-Zustand)."
        assert done["engine"] == pa.ENGINE_NAME
        assert done["model"] == pa.SOL_MODEL
        assert done["error"] is None
        assert seen["engine"] == "sol"
        assert seen["model"] == pa.SOL_MODEL
        assert pa.PA_SYSTEM_PROMPT in str(seen["prompt"])
        assert '"open": 3' in str(seen["prompt"])
        assert [path.name for path in seen["image_paths"]] == [asset_id]

        history = client.get("/api/pa/history")
        assert history.status_code == 200
        assert history.json()["turns"][0]["turn_id"] == turn_id

        messages = client.get("/api/pa/messages")
        assert messages.status_code == 200
        message_rows = messages.json()["messages"]
        roles = [m["role"] for m in message_rows]
        assert roles == ["user", "assistant"]
        assert message_rows[0]["content"] == "Was ist offen?"
        assert message_rows[0]["attachments"] == [{"asset_id": asset_id}]
        assert message_rows[1]["attachments"] == []
        assert {row["status"] for row in message_rows} == {"done"}
        assert {row["error"] for row in message_rows} == {None}

        asset = client.get(f"/api/pa/asset/{asset_id}")
        assert asset.status_code == 200
        assert asset.headers["content-type"].startswith("image/png")
        assert asset.content == b"\x89PNG\r\n\x1a\nfixture"

        invalid_asset = client.get("/api/pa/asset/bad$id.png")
        assert invalid_asset.status_code == 400
        missing_asset = client.get("/api/pa/asset/asset_missing.png")
        assert missing_asset.status_code == 404


def test_api_engine_error_is_persisted_and_http_poll_stays_200(
    isolated_pa_home: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(pa, "build_context_pack", lambda scope: "{}")

    def timeout(*args: object, **kwargs: object) -> subprocess.CompletedProcess[str]:
        raise subprocess.TimeoutExpired(
            cmd="hermes chat", timeout=pa.TURN_TIMEOUT_SECONDS
        )

    monkeypatch.setattr(pa.subprocess, "run", timeout)
    app = FastAPI()
    pa.register_pa_routes(app)

    with TestClient(app) as client:
        created = client.post(
            "/api/pa/message",
            json={"text": "Hallo", "engine": "claude", "model": "opus-4.8"},
        )
        assert created.status_code == 200
        turn = _poll(client, created.json()["turn_id"], "error")
        assert turn["reply"] == "Engine-Zeitlimit erreicht"
        assert turn["error"] == "Engine-Zeitlimit erreicht"
        assert turn["engine"] == "claude"
        assert turn["model"] == "opus-4.8"
        messages = client.get("/api/pa/messages").json()["messages"]
        assert [row["status"] for row in messages] == ["error", "error"]
        assert [row["error"] for row in messages] == [
            "Engine-Zeitlimit erreicht",
            "Engine-Zeitlimit erreicht",
        ]


@pytest.mark.parametrize(
    ("engine", "model"),
    [("claude", "fable-5"), ("kimi", "k3"), ("qwen", "qwen3.7-plus")],
)
def test_api_dispatches_engine_and_model(
    isolated_pa_home: Path,
    monkeypatch: pytest.MonkeyPatch,
    engine: str,
    model: str,
) -> None:
    seen: dict[str, str] = {}
    monkeypatch.setattr(pa, "build_context_pack", lambda scope: "{}")

    def fake_engine(
        selected_engine: str,
        prompt: str,
        *,
        model: str,
        image_paths: list[Path],
    ) -> str:
        seen.update(engine=selected_engine, model=model)
        return "ok"

    monkeypatch.setattr(pa, "run_engine", fake_engine)
    app = FastAPI()
    pa.register_pa_routes(app)

    with TestClient(app) as client:
        created = client.post(
            "/api/pa/message",
            json={"text": "Hallo", "engine": engine, "model": model},
        )
        assert created.status_code == 200
        turn = _poll(client, created.json()["turn_id"], "done")

    assert turn["engine"] == engine
    assert turn["model"] == model
    assert seen == {"engine": engine, "model": model}


@pytest.mark.parametrize(
    ("payload", "detail"),
    [
        ({"text": "x", "engine": "unknown"}, "Unbekannte PA-Engine"),
        (
            {"text": "x", "engine": "claude", "model": "gpt-5.6-sol"},
            "PA-Modell passt nicht zur Engine",
        ),
        (
            {"text": "x", "engine": "kimi", "model": "fable-5"},
            "PA-Modell passt nicht zur Engine",
        ),
    ],
)
def test_api_rejects_unknown_engine_and_cross_engine_models(
    isolated_pa_home: Path, payload: dict[str, object], detail: str
) -> None:
    app = FastAPI()
    pa.register_pa_routes(app)
    with TestClient(app) as client:
        response = client.post("/api/pa/message", json=payload)
    assert response.status_code == 400
    assert response.json() == {"detail": detail}


@pytest.mark.parametrize("engine,model", [("claude", "opus-4.8"), ("kimi", "k3")])
def test_api_rejects_images_for_non_vision_engines(
    isolated_pa_home: Path, engine: str, model: str
) -> None:
    app = FastAPI()
    pa.register_pa_routes(app)
    with TestClient(app) as client:
        response = client.post(
            "/api/pa/message",
            json={
                "text": "Was ist hier?",
                "engine": engine,
                "model": model,
                "attachments": [{"asset_id": "not-resolved.png"}],
            },
        )
    assert response.status_code == 400
    assert response.json() == {
        "detail": f"Engine '{engine}' unterstützt keine Bilder im One-Shot-Modus"
    }


@pytest.mark.parametrize(
    "payload",
    [
        {"text": "x", "model": "unknown-model"},
        {"text": "x", "attachments": [{"asset_id": "missing.png"}]},
        {"text": "x", "attachments": [{"asset_id": "../escape.png"}]},
        {"text": "x", "attachments": [{"asset_id": "/tmp/escape.png"}]},
    ],
)
def test_api_rejects_unknown_models_missing_assets_and_traversal(
    isolated_pa_home: Path, payload: dict[str, object]
) -> None:
    app = FastAPI()
    pa.register_pa_routes(app)
    with TestClient(app) as client:
        response = client.post("/api/pa/message", json=payload)
    assert response.status_code == 400


def test_real_web_server_import_registers_pa_routes_with_isolated_home(
    isolated_pa_home: Path,
) -> None:
    code = """
import json
from hermes_cli.web_server import _PUBLIC_API_PATHS, app
paths = {getattr(route, 'path', '') for route in app.routes}
required = {
    '/api/pa/message', '/api/pa/turns/{turn_id}',
    '/api/pa/upload', '/api/pa/history', '/api/pa/messages',
    '/api/pa/asset/{asset_id}', '/api/pa/feed',
}
print(json.dumps({
    'ok': required <= paths and not (required & set(_PUBLIC_API_PATHS)),
    'missing': sorted(required - paths),
}))
"""
    env = os.environ.copy()
    env["HOME"] = str(isolated_pa_home.parent)
    env["HERMES_HOME"] = str(isolated_pa_home)
    result = subprocess.run(
        [sys.executable, "-c", code],
        cwd=Path(__file__).parents[2],
        env=env,
        capture_output=True,
        text=True,
        timeout=60,
        check=False,
    )
    assert result.returncode == 0, result.stderr
    assert json.loads(result.stdout.splitlines()[-1]) == {"ok": True, "missing": []}


def test_messages_endpoint_pages_with_before_id_cursor(
    isolated_pa_home: Path,
) -> None:
    store = pa.PAStore()
    for index in range(4):
        turn_id = store.create_turn(
            text=f"Frage {index}",
            engine="sol",
            model=pa.SOL_MODEL,
            project_scope=None,
            attachments=[],
            now=100 + index,
        )
        assert store.set_running(turn_id, now=200 + index)
        store.finish_turn(turn_id, f"Antwort {index}", now=300 + index)

    app = FastAPI()
    pa.register_pa_routes(app)
    with TestClient(app) as client:
        newest = client.get("/api/pa/messages?limit=3")
        assert newest.status_code == 200
        newest_body = newest.json()
        cursor = newest_body["next_before_id"]
        assert isinstance(cursor, int)
        older = client.get(f"/api/pa/messages?limit=3&before_id={cursor}")
        assert older.status_code == 200
        invalid = client.get("/api/pa/messages?before_id=0")
        assert invalid.status_code == 400

    newest_ids = [row["id"] for row in newest_body["messages"]]
    older_ids = [row["id"] for row in older.json()["messages"]]
    assert newest_ids == sorted(newest_ids)
    assert older_ids == sorted(older_ids)
    assert set(newest_ids).isdisjoint(older_ids)
    assert all(row_id < cursor for row_id in older_ids)
    assert all("status" in row and "error" in row for row in newest_body["messages"])


def test_feed_schema_and_endpoint_use_bounded_ascending_since_cursor(
    isolated_pa_home: Path,
) -> None:
    store = pa.PAStore()
    store.ensure_schema()
    with store.connect() as conn:
        columns = {
            str(row[1]): str(row[2])
            for row in conn.execute("PRAGMA table_info(pa_feed)")
        }
        assert columns == {
            "id": "INTEGER",
            "ts": "INTEGER",
            "kind": "TEXT",
            "severity": "TEXT",
            "title": "TEXT",
            "ref": "TEXT",
            "delivered_push": "INTEGER",
        }
        conn.executemany(
            "INSERT INTO pa_feed(ts, kind, severity, title, ref) "
            "VALUES (?, ?, ?, ?, ?)",
            [
                (100, "watcher_bundle", "info", "eins", "t1"),
                (101, "watcher_bundle", "warning", "zwei", "receipt.md"),
                (102, "watcher_bundle", "critical", "drei", "t3"),
            ],
        )

    app = FastAPI()
    pa.register_pa_routes(app)
    with TestClient(app) as client:
        first = client.get("/api/pa/feed?since_id=0&limit=2")
        assert first.status_code == 200
        first_body = first.json()
        assert [item["id"] for item in first_body["items"]] == [1, 2]
        assert first_body["next_since_id"] == 2
        assert first_body["has_more"] is True
        assert first_body["items"][0] == {
            "id": 1,
            "ts": 100,
            "kind": "watcher_bundle",
            "severity": "info",
            "title": "eins",
            "ref": "t1",
            "delivered_push": 0,
        }
        second = client.get("/api/pa/feed?since_id=2&limit=500")
        assert second.status_code == 200
        assert [item["id"] for item in second.json()["items"]] == [3]
        empty = client.get("/api/pa/feed?since_id=3")
        assert empty.json()["next_since_id"] == 3
        invalid = client.get("/api/pa/feed?since_id=-1")
        assert invalid.status_code == 400


def test_route_registration_reaps_pending_and_running_turns(
    isolated_pa_home: Path,
) -> None:
    store = pa.PAStore()
    pending = store.create_turn(
        text="pending",
        engine="sol",
        model=pa.SOL_MODEL,
        project_scope=None,
        attachments=[],
        now=1,
    )
    running = store.create_turn(
        text="running",
        engine="sol",
        model=pa.SOL_MODEL,
        project_scope=None,
        attachments=[],
        now=2,
    )
    done = store.create_turn(
        text="done",
        engine="sol",
        model=pa.SOL_MODEL,
        project_scope=None,
        attachments=[],
        now=3,
    )
    assert store.set_running(running, now=4)
    assert store.set_running(done, now=5)
    store.finish_turn(done, "fertig", now=6)

    app = FastAPI()
    pa.register_pa_routes(app)

    assert store.get_turn(pending)["status"] == "error"
    assert store.get_turn(pending)["error"] == "Server-Neustart"
    assert store.get_turn(running)["status"] == "error"
    assert store.get_turn(done)["status"] == "done"
    executor_errors = [
        row
        for row in store.message_page(limit=20)["messages"]
        if row["role"] == "assistant" and row["content"] == "Server-Neustart"
    ]
    assert len(executor_errors) == 2


def test_upload_retention_ttl_soft_cap_startup_and_upload_hook(
    isolated_pa_home: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    root = pa.uploads_dir()
    root.mkdir(parents=True)
    now = 2_000_000_000.0
    old = root / "asset_old.png"
    cap_oldest = root / "asset_cap_oldest.png"
    newest = root / "asset_newest.png"
    ignored = root / "operator$note"
    old.write_bytes(b"old")
    cap_oldest.write_bytes(b"12345678")
    newest.write_bytes(b"abcdefgh")
    ignored.write_bytes(b"keep")
    os.utime(old, (now - 31 * 86_400, now - 31 * 86_400))
    os.utime(cap_oldest, (now - 20, now - 20))
    os.utime(newest, (now - 10, now - 10))
    os.utime(ignored, (now - 40 * 86_400, now - 40 * 86_400))

    result = pa.prune_uploads(now=now, ttl_days=30, max_total_bytes=10)

    assert result == {"removed": 2, "removed_bytes": 11, "remaining_bytes": 8}
    assert not old.exists()
    assert not cap_oldest.exists()
    assert newest.exists()
    assert ignored.exists()
    with pytest.raises(pa.AssetNotFoundError):
        pa.resolve_asset(old.name)

    startup_old = root / "asset_startup.png"
    startup_old.write_bytes(b"stale")
    real_now = time.time()
    os.utime(startup_old, (real_now - 31 * 86_400, real_now - 31 * 86_400))
    app = FastAPI()
    pa.register_pa_routes(app)
    assert not startup_old.exists()

    prune_calls = 0

    def fake_prune() -> dict[str, int]:
        nonlocal prune_calls
        prune_calls += 1
        return {"removed": 0, "removed_bytes": 0, "remaining_bytes": 0}

    monkeypatch.setattr(pa, "prune_uploads", fake_prune)
    with TestClient(app) as client:
        upload = client.post(
            "/api/pa/upload",
            files={"file": ("new.png", b"png", "image/png")},
        )
    assert upload.status_code == 200
    assert prune_calls == 1


def test_fake_engine_valid_action_proposal_is_hidden_and_enqueued(
    isolated_pa_home: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    proposal = {
        "category": "kanban.nudge",
        "payload": {"card_id": "t_123", "reason": "Bitte Status prüfen"},
        "reason": "Die Karte wirkt still",
    }
    monkeypatch.setattr(pa, "build_context_pack", lambda scope: "{}")
    monkeypatch.setattr(
        pa,
        "run_engine",
        lambda *args, **kwargs: (
            "Ich schlage eine kontrollierte Aktion vor.\n"
            f"```pa_action {json.dumps(proposal)}```"
        ),
    )
    app = FastAPI()
    pa.register_pa_routes(app)

    with TestClient(app) as client:
        created = client.post("/api/pa/message", json={"text": "Was tun?"})
        assert created.status_code == 200
        turn = _poll(client, created.json()["turn_id"], "done")

    assert "pa_action" not in str(turn["reply"])
    assert turn["reply"] == "Ich schlage eine kontrollierte Aktion vor."
    events = aq.list_question_events(status="open")
    assert len(events) == 1
    assert events[0]["kind"] == "pa_action"
    assert events[0]["action_payload"] == {
        "version": 1,
        **proposal,
    }


@pytest.mark.parametrize(
    ("reply", "notice_fragment"),
    [
        (
            "Text\n```pa_action {\"category\":\"unknown\",\"payload\":{}}```",
            "ungültiger oder unbekannter Daten",
        ),
        (
            "Text\n```pa_action {\"category\":\"tmux.interrupt\",\"payload\":{\"session\":\"work\",\"window\":\"a\"}}```\n"
            "```pa_action {\"category\":\"tmux.interrupt\",\"payload\":{\"session\":\"work\",\"window\":\"b\"}}```",
            "höchstens einer",
        ),
    ],
)
def test_proposal_parser_removes_invalid_unknown_and_multiple_blocks(
    reply: str,
    notice_fragment: str,
) -> None:
    visible, proposal, notice = pa.parse_pa_action_proposal(reply)

    assert proposal is None
    assert notice is not None and notice_fragment in notice
    assert "```pa_action" not in visible
    assert visible == "Text"


def test_proposal_parser_accepts_single_fenced_json_block() -> None:
    reply = (
        "Weiter nur nach Bestätigung.\n"
        "```pa_action {\"category\":\"tmux.interrupt\","
        "\"payload\":{\"session\":\"work\",\"window\":\"codex\"},"
        "\"reason\":\"Prozess hängt\"}```"
    )

    visible, proposal, notice = pa.parse_pa_action_proposal(reply)

    assert visible == "Weiter nur nach Bestätigung."
    assert notice is None
    assert proposal == {
        "version": 1,
        "category": "tmux.interrupt",
        "payload": {"session": "work", "window": "codex"},
        "reason": "Prozess hängt",
    }


# ---------------------------------------------------------------------------
# S2.4 Entscheidungs-Inbox
# ---------------------------------------------------------------------------


def _insert_kanban_task(
    conn: sqlite3.Connection,
    task_id: str,
    *,
    status: str,
    freigabe: str | None = None,
    block_kind: str | None = None,
    body: str | None = None,
    created_at: int = 1000,
) -> None:
    conn.execute(
        "INSERT INTO tasks (id, title, status, freigabe, block_kind, body, created_at, "
        "workspace_kind) VALUES (?, ?, ?, ?, ?, ?, ?, 'scratch')",
        (task_id, f"Titel {task_id}", status, freigabe, block_kind, body, created_at),
    )


def test_inbox_empty_stores(isolated_pa_home: Path) -> None:
    payload = pa.build_inbox()

    assert payload["items"] == []
    assert payload["errors"] == []
    assert payload["generated_at"] > 0


def test_inbox_questions_and_pa_action_cards(isolated_pa_home: Path) -> None:
    aq.insert_question_event(
        session="work",
        window="kimi",
        pane_id="%1",
        fingerprint="fp-normal",
        question_text="Weiter mit dem Build?",
        options=[{"nr": 1, "label": "Ja"}, {"nr": 2, "label": "Nein"}],
        kind="kimi",
    )
    aq.insert_question_event(
        session="pa",
        window="pa",
        pane_id="pa",
        fingerprint="pa:" + "0" * 64,
        question_text="PA-Aktion ausführen: tmux.interrupt?",
        options=[],
        kind="pa_action",
        action_payload={
            "version": 1,
            "category": "tmux.interrupt",
            "payload": {"session": "work", "window": "codex"},
            "reason": "Prozess hängt",
        },
    )

    payload = pa.build_inbox()

    assert payload["errors"] == []
    by_type = {item["type"]: item for item in payload["items"]}
    assert by_type["question"]["kind"] == "kimi"
    assert by_type["question"]["question_id"]
    # S7.6: summary additiv; pa_action-Payload unberührt.
    assert by_type["question"]["summary"]
    assert len(by_type["question"]["summary"]) <= 80
    action = by_type["pa_action"]
    assert action["category"] == "tmux.interrupt"
    assert action["action_payload"]["payload"]["window"] == "codex"
    assert action["summary"]
    assert action["action_payload"]["category"] == "tmux.interrupt"


def test_inbox_kanban_held_tasks_block_radius_and_sorting(
    isolated_pa_home: Path,
) -> None:
    from hermes_cli import kanban_db as kb

    with kb.connect_closing() as conn:
        _insert_kanban_task(conn, "t_root", status="blocked", block_kind="needs_input")
        _insert_kanban_task(conn, "t_child1", status="todo")
        _insert_kanban_task(conn, "t_child2", status="done")
        _insert_kanban_task(conn, "t_grand", status="running")
        _insert_kanban_task(
            conn,
            "t_gate",
            status="scheduled",
            freigabe="build",
            body=(
                "# PlanSpec\n\n## Ziel\nDas grüne Gate kontrolliert freigeben.\n\n"
                "## Evidenz\nAlle fokussierten Tests sind grün.\n\n"
                "## Rollback\nBei Ablehnung bleibt die Kette geparkt.\n"
            ),
        )
        _insert_kanban_task(conn, "t_noise", status="running")
        conn.execute(
            "INSERT INTO task_links (parent_id, child_id) VALUES ('t_root', 't_child1')"
        )
        conn.execute(
            "INSERT INTO task_links (parent_id, child_id) VALUES ('t_root', 't_child2')"
        )
        conn.execute(
            "INSERT INTO task_links (parent_id, child_id) VALUES ('t_child1', 't_grand')"
        )
        # S7.6: langer PlanSpec-Titel → summary destilliert ≤80.
        conn.execute(
            "UPDATE tasks SET title=? WHERE id='t_gate'",
            (
                "PlanSpec GATE-GREEN-KANBAN-LIFECYCLE-REGRESSION-FIX: "
                "Green-Gate-Ursachenfix: die live-reproduzierten Fehler "
                "werden behoben — operator_release_required",
            ),
        )
        conn.commit()

    payload = pa.build_inbox()

    assert payload["errors"] == []
    cards = {item["card_id"]: item for item in payload["items"] if "card_id" in item}
    # live descendants of t_root: t_child1 + t_grand (t_child2 is done) → 3
    assert cards["t_root"]["type"] == "held_task"
    assert cards["t_root"]["block_radius"] == 3
    assert cards["t_gate"]["type"] == "freigabe_gate"
    assert cards["t_gate"]["block_radius"] == 1
    assert "summary" in cards["t_gate"]
    assert len(cards["t_gate"]["summary"]) <= 80
    assert "operator_release_required" not in cards["t_gate"]["summary"]
    assert cards["t_gate"]["why"] == (
        "Das grüne Gate kontrolliert freigeben. "
        "Alle fokussierten Tests sind grün."
    )
    assert cards["t_gate"]["consequence_on_decline"] == (
        "Bei Ablehnung bleibt die Kette geparkt."
    )
    assert cards["t_root"]["why"] == pa.INBOX_WHY_FALLBACK
    assert cards["t_root"]["consequence_on_decline"] == pa.INBOX_DECLINE_FALLBACK
    assert "t_noise" not in cards
    # block radius first: t_root (3) before t_gate (1)
    ordered = [item.get("card_id") for item in payload["items"] if "card_id" in item]
    assert ordered.index("t_root") < ordered.index("t_gate")


def test_inbox_decision_why_failure_isolated_per_card(
    isolated_pa_home: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from hermes_cli import kanban_db as kb

    with kb.connect_closing() as conn:
        _insert_kanban_task(
            conn,
            "t_gate",
            status="scheduled",
            freigabe="operator",
            body="# PlanSpec\n\n## Ziel\nFreigeben.",
        )
        conn.commit()

    def broken_why(_body: object) -> tuple[str, str]:
        raise ValueError("kaputter PlanSpec-Body")

    monkeypatch.setattr(pa, "distill_decision_why", broken_why)

    payload = pa.build_inbox()

    assert payload["errors"] == []
    assert len(payload["items"]) == 1
    assert payload["items"][0]["card_id"] == "t_gate"
    assert payload["items"][0]["why"] == pa.INBOX_WHY_FALLBACK
    assert payload["items"][0]["consequence_on_decline"] == pa.INBOX_DECLINE_FALLBACK


def test_inbox_kanban_failure_isolated(
    isolated_pa_home: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from hermes_cli import kanban_db as kb

    aq.insert_question_event(
        session="work",
        window="kimi",
        pane_id="%1",
        fingerprint="fp-survives",
        question_text="Frage überlebt Kanban-Ausfall?",
    )

    def boom(*args: object, **kwargs: object) -> object:
        raise RuntimeError("kanban db wedged")

    monkeypatch.setattr(kb, "connect_closing", boom)

    payload = pa.build_inbox()

    assert [item["type"] for item in payload["items"]] == ["question"]
    assert payload["errors"] == [
        {"source": "kanban", "error": "kanban db wedged"}
    ]


def test_inbox_endpoint(isolated_pa_home: Path) -> None:
    app = FastAPI()
    pa.register_pa_routes(app)

    with TestClient(app) as client:
        response = client.get("/api/pa/inbox")

    assert response.status_code == 200
    body = response.json()
    assert body["items"] == []
    assert body["errors"] == []


# ---------------------------------------------------------------------------
# S7.1 Kontextpack: memory + pending_decisions
# ---------------------------------------------------------------------------


def test_context_pack_memory_diary_and_pending_decisions(
    isolated_pa_home: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from hermes_cli import kanban_db as kb

    diary_dir = tmp_path / "diary"
    diary_dir.mkdir()
    (diary_dir / "2026-07-18-jarvis.md").write_text(
        "# Tag 18\n\nGestern: Deploy gelandet.\n", encoding="utf-8"
    )
    (diary_dir / "2026-07-19-jarvis.md").write_text(
        "# Tag 19\n\nHeute: Review-Queue geleert.\n", encoding="utf-8"
    )
    (diary_dir / "2026-07-20-jarvis.md").write_text(
        "# Tag 20\n\nSprint 7 gestartet.\n", encoding="utf-8"
    )
    pointer_dir = tmp_path / "memsearch"
    pointer_dir.mkdir()
    (pointer_dir / "2026-07-19.md").write_text(
        "# Session\nPointer-Notiz A\n", encoding="utf-8"
    )
    (pointer_dir / "2026-07-20.md").write_text(
        "# Session\nPointer-Notiz B\n", encoding="utf-8"
    )

    with kb.connect_closing() as conn:
        _insert_kanban_task(
            conn, "t_hold", status="blocked", block_kind="needs_input", created_at=50
        )
        conn.execute(
            "UPDATE tasks SET title=? WHERE id='t_hold'",
            ("Task t_holddead: Freigabe nötig — operator_release_required",),
        )
        conn.commit()

    _orig_memory = pa._context_memory_section

    monkeypatch.setattr(
        pa,
        "_context_memory_section",
        lambda: _orig_memory(diary_dir=diary_dir, pointer_dir=pointer_dir),
    )
    # registry/agents können in Isolation fehlen — nur S7-Felder prüfen.
    monkeypatch.setattr(
        "hermes_cli.projects_overview.load_projects_registry",
        lambda: type("R", (), {"projects": []})(),
    )
    monkeypatch.setattr(
        "hermes_cli.projects_overview.build_projects_payload",
        lambda reg: {"projects": []},
    )
    monkeypatch.setattr(
        "hermes_cli.projects_overview.build_agents_payload",
        lambda reg: {"agents": []},
    )
    monkeypatch.setattr(
        "hermes_cli.projects_overview.build_receipts_payload",
        lambda reg: {"receipts": []},
    )

    raw = pa.build_context_pack()
    payload = json.loads(raw)

    assert "memory" in payload
    diary = payload["memory"]["diary"]
    assert len(diary) == 3
    assert diary[-1]["date"] == "2026-07-20"
    assert "Sprint 7" in diary[-1]["excerpt"]
    assert len(payload["memory"]["pointers"]) >= 1
    pending = payload["pending_decisions"]
    assert pending["count"] >= 1
    assert pending["titles"]
    assert all(len(t) <= 80 for t in pending["titles"])
    assert not any("operator_release_required" in t for t in pending["titles"])


def test_context_pack_memory_failure_isolated(
    isolated_pa_home: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(
        "hermes_cli.projects_overview.load_projects_registry",
        lambda: type("R", (), {"projects": []})(),
    )
    monkeypatch.setattr(
        "hermes_cli.projects_overview.build_projects_payload",
        lambda reg: {"projects": [{"slug": "ok"}]},
    )
    monkeypatch.setattr(
        "hermes_cli.projects_overview.build_agents_payload",
        lambda reg: {"agents": []},
    )
    monkeypatch.setattr(
        "hermes_cli.projects_overview.build_receipts_payload",
        lambda reg: {"receipts": []},
    )

    def boom_memory() -> dict[str, object]:
        raise RuntimeError("memsearch down")

    monkeypatch.setattr(pa, "_context_memory_section", boom_memory)
    monkeypatch.setattr(
        pa,
        "_context_pending_decisions",
        lambda: {"count": 0, "titles": []},
    )

    payload = json.loads(pa.build_context_pack())
    assert payload["projects_summary"]["projects"] == [{"slug": "ok"}]
    assert payload["memory"]["error"] == "memsearch down"
    assert payload["memory"]["diary"] == []
    assert payload["pending_decisions"]["count"] == 0


def test_context_pack_respects_char_budget(
    isolated_pa_home: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    huge = "X" * 20_000
    monkeypatch.setattr(
        "hermes_cli.projects_overview.load_projects_registry",
        lambda: type("R", (), {"projects": []})(),
    )
    monkeypatch.setattr(
        "hermes_cli.projects_overview.build_projects_payload",
        lambda reg: {"blob": huge},
    )
    monkeypatch.setattr(
        "hermes_cli.projects_overview.build_agents_payload",
        lambda reg: {"agents": []},
    )
    monkeypatch.setattr(
        "hermes_cli.projects_overview.build_receipts_payload",
        lambda reg: {"receipts": []},
    )
    monkeypatch.setattr(
        pa, "_context_memory_section", lambda: {"diary": [], "pointers": []}
    )
    monkeypatch.setattr(
        pa, "_context_pending_decisions", lambda: {"count": 0, "titles": []}
    )

    raw = pa.build_context_pack()
    assert len(raw) <= pa.CONTEXT_PACK_MAX_CHARS
    envelope = json.loads(raw)
    assert envelope.get("truncated") is True or len(raw) <= pa.CONTEXT_PACK_MAX_CHARS


def test_engines_endpoint_exposes_roster(isolated_pa_home: Path) -> None:
    app = FastAPI()
    pa.register_pa_routes(app)

    with TestClient(app) as client:
        response = client.get("/api/pa/engines")

    assert response.status_code == 200
    body = response.json()
    assert body["default_engine"] == "sol"
    engines = {entry["engine"]: entry for entry in body["engines"]}
    assert engines["sol"]["models"] == ["gpt-5.6-sol"]
    assert engines["sol"]["supports_images"] is True
    assert engines["claude"]["default_model"] == "opus-4.8"
    assert set(engines["claude"]["models"]) == {"opus-4.8", "fable-5"}
    assert engines["claude"]["supports_images"] is False
    assert engines["kimi"]["models"] == ["k3"]
    assert engines["qwen"]["models"] == ["qwen3.7-plus"]
    assert engines["qwen"]["default_model"] == "qwen3.7-plus"
    assert engines["qwen"]["supports_images"] is True


def test_turn_rating_endpoint_persists_and_engine_stats_aggregate(
    isolated_pa_home: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    store = pa.PAStore(isolated_pa_home / "pa.db")
    known_turn = store.create_turn(
        "bekannte frage", engine="sol", model="gpt-5.6-sol"
    )
    store.finish_turn(known_turn, "bekannte antwort")
    unknown_turn = store.create_turn(
        "unbekannte frage", engine="qwen", model="qwen3.7-plus"
    )
    store.finish_turn(unknown_turn, "unbekannte antwort")
    monkeypatch.setattr(pa, "get_pa_store", lambda: store)
    app = FastAPI()
    pa.register_pa_routes(app)

    with TestClient(app) as client:
        rated = client.post(
            f"/api/pa/turns/{known_turn}/rating", json={"rating": 1}
        )
        assert rated.status_code == 200
        assert rated.json() == {"turn_id": known_turn, "rating": 1}
        assert client.post(
            f"/api/pa/turns/{known_turn}/rating", json={"rating": 0}
        ).status_code == 422
        assert client.post(
            "/api/pa/turns/missing/rating", json={"rating": -1}
        ).status_code == 404

        turn = client.get(f"/api/pa/turns/{known_turn}").json()
        assert turn["rating"] == 1
        stats = client.get("/api/pa/engine-stats")
        assert stats.status_code == 200
        by_engine = {row["engine"]: row for row in stats.json()["engines"]}
        assert by_engine["sol"]["turns_total"] == 1
        assert by_engine["sol"]["rated_turns"] == 1
        assert by_engine["sol"]["thumbs_up_rate"] == 100.0
        assert by_engine["sol"]["estimated_cost_usd"] is not None
        assert by_engine["qwen"]["estimated_cost_usd"] is None


def test_schema_upgrade_tolerates_legacy_turn_rows_without_rating(
    isolated_pa_home: Path,
) -> None:
    db_path = isolated_pa_home / "legacy-pa.db"
    with sqlite3.connect(db_path) as conn:
        conn.execute(
            """
            CREATE TABLE pa_turns (
                id TEXT PRIMARY KEY, conversation_id TEXT NOT NULL,
                engine TEXT NOT NULL, model TEXT NOT NULL, status TEXT NOT NULL,
                error TEXT, created_at REAL NOT NULL, updated_at REAL NOT NULL
            )
            """
        )
        conn.execute(
            "INSERT INTO pa_turns VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            ("legacy", "default", "sol", "gpt-5.6-sol", "done", None, 1.0, 1.0),
        )

    store = pa.PAStore(db_path)
    turn = store.get_turn("legacy")
    assert turn is not None
    assert turn["rating"] is None
    assert "rating" in {row[1] for row in sqlite3.connect(db_path).execute("PRAGMA table_info(pa_turns)")}


def test_turn_cost_estimator_known_and_unknown_prices() -> None:
    assert pa.estimate_turn_cost_usd("gpt-5.6-sol", 1_000_000, 1_000_000) == pytest.approx(15.75)
    assert pa.estimate_turn_cost_usd("qwen3.7-plus", 1_000, 1_000) is None
