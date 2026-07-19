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


def test_api_pending_to_done_history_upload_and_attachment(
    isolated_pa_home: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    release = threading.Event()
    seen: dict[str, object] = {}

    def fake_context(project_scope: str | None) -> str:
        return json.dumps({"project_scope": project_scope, "open": 3})

    def fake_engine(prompt: str, *, model: str, image_paths: list[Path]) -> str:
        seen.update(prompt=prompt, model=model, image_paths=image_paths)
        assert release.wait(2)
        return "Es sind drei Punkte offen (Board-Zustand)."

    monkeypatch.setattr(pa, "build_context_pack", fake_context)
    monkeypatch.setattr(pa, "run_sol_engine", fake_engine)
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
        assert seen["model"] == pa.SOL_MODEL
        assert pa.PA_SYSTEM_PROMPT in str(seen["prompt"])
        assert '"open": 3' in str(seen["prompt"])
        assert [path.name for path in seen["image_paths"]] == [asset_id]

        history = client.get("/api/pa/history")
        assert history.status_code == 200
        assert history.json()["turns"][0]["turn_id"] == turn_id


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
        created = client.post("/api/pa/message", json={"text": "Hallo"})
        assert created.status_code == 200
        turn = _poll(client, created.json()["turn_id"], "error")
        assert turn["reply"] == "Engine-Zeitlimit erreicht"
        assert turn["error"] == "Engine-Zeitlimit erreicht"


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
    '/api/pa/upload', '/api/pa/history',
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
