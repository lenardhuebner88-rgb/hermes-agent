from __future__ import annotations

import json
import os
import subprocess
import sys
import threading
import time
from pathlib import Path

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

import hermes_cli.pa_chat as pa


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
    }
    assert [(row["role"], row["content"]) for row in store.recent_messages()] == [
        ("user", "Was ist offen?"),
        ("assistant", "Zwei Aufgaben sind offen."),
    ]


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
    }


@pytest.mark.parametrize(
    ("engine", "model"),
    [("sol", "gpt-5.6-sol"), ("claude", "opus-4.8"), ("kimi", "k3")],
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
        roles = [m["role"] for m in messages.json()["messages"]]
        assert roles == ["user", "assistant"]
        assert messages.json()["messages"][0]["content"] == "Was ist offen?"


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


@pytest.mark.parametrize(
    ("engine", "model"),
    [("claude", "fable-5"), ("kimi", "k3")],
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
