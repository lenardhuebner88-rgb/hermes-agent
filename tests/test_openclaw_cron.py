from __future__ import annotations

import importlib
import json
from pathlib import Path

import httpx
import pytest
from fastapi import FastAPI


def _app_with_cron_dir(monkeypatch, cron_dir: Path) -> FastAPI:
    view = importlib.import_module("hermes_cli.openclaw_view")
    monkeypatch.setattr(view, "_OPENCLAW_CRON_DIR", cron_dir)
    app = FastAPI()
    view.register_openclaw_routes(app)
    return app


async def _get(app: FastAPI, path: str) -> httpx.Response:
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://testserver") as client:
        return await client.get(path)


def _write_json(path: Path, payload: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload), encoding="utf-8")


@pytest.mark.asyncio
async def test_openclaw_cron_errors_returns_only_enabled_error_jobs(monkeypatch, tmp_path):
    cron_dir = tmp_path / ".openclaw" / "cron"
    _write_json(
        cron_dir / "jobs.json",
        {
            "version": 1,
            "jobs": [
                {
                    "id": "errored-inline",
                    "name": "Errored Inline",
                    "enabled": True,
                    "payload": {"token": "must-not-leak"},
                    "state": {
                        "lastStatus": "error",
                        "lastError": "inline failed",
                        "consecutiveErrors": 2,
                        "lastRunAtMs": 1_780_041_720_987,
                    },
                },
                {
                    "id": "errored-persisted",
                    "name": "Errored Persisted",
                    "enabled": True,
                    "state": {"lastStatus": "ok"},
                },
                {
                    "id": "green-enabled",
                    "name": "Green Enabled",
                    "enabled": True,
                    "state": {
                        "lastStatus": "success",
                        "lastRunStatus": "success",
                        "consecutiveErrors": 7,
                    },
                },
            ],
        },
    )
    _write_json(
        cron_dir / "jobs-state.json",
        {
            "version": 1,
            "jobs": {
                "errored-persisted": {
                    "sessionKey": "must-not-leak",
                    "state": {
                        "lastRunStatus": "error",
                        "lastError": "persisted failed",
                        "consecutiveErrors": 3,
                        "lastRunAtMs": 1_780_041_721_111,
                    },
                }
            },
        },
    )

    response = await _get(_app_with_cron_dir(monkeypatch, cron_dir), "/api/openclaw/cron-errors")

    assert response.status_code == 200
    body = response.json()
    assert body == {
        "errors": [
            {
                "id": "errored-inline",
                "name": "Errored Inline",
                "lastError": "inline failed",
                "consecutiveErrors": 2,
                "lastRunAt": 1_780_041_720,
            },
            {
                "id": "errored-persisted",
                "name": "Errored Persisted",
                "lastError": "persisted failed",
                "consecutiveErrors": 3,
                "lastRunAt": 1_780_041_721,
            },
        ]
    }
    assert "must-not-leak" not in response.text


@pytest.mark.asyncio
async def test_openclaw_cron_errors_missing_state_file_is_stale(monkeypatch, tmp_path):
    cron_dir = tmp_path / ".openclaw" / "cron"
    _write_json(
        cron_dir / "jobs.json",
        {"version": 1, "jobs": [{"id": "job", "name": "Job", "enabled": True}]},
    )

    response = await _get(_app_with_cron_dir(monkeypatch, cron_dir), "/api/openclaw/cron-errors")

    assert response.status_code == 200
    body = response.json()
    assert body["errors"] == []
    assert body["stale"]


@pytest.mark.asyncio
async def test_openclaw_cron_errors_corrupt_json_is_stale(monkeypatch, tmp_path):
    cron_dir = tmp_path / ".openclaw" / "cron"
    cron_dir.mkdir(parents=True)
    (cron_dir / "jobs.json").write_text("{invalid", encoding="utf-8")
    _write_json(cron_dir / "jobs-state.json", {"version": 1, "jobs": {}})

    response = await _get(_app_with_cron_dir(monkeypatch, cron_dir), "/api/openclaw/cron-errors")

    assert response.status_code == 200
    body = response.json()
    assert body["errors"] == []
    assert body["stale"]


@pytest.mark.asyncio
async def test_openclaw_cron_errors_invalid_utf8_is_stale(monkeypatch, tmp_path):
    cron_dir = tmp_path / ".openclaw" / "cron"
    cron_dir.mkdir(parents=True)
    (cron_dir / "jobs.json").write_bytes(b"\xff\xfe\x00")
    _write_json(cron_dir / "jobs-state.json", {"version": 1, "jobs": {}})

    response = await _get(_app_with_cron_dir(monkeypatch, cron_dir), "/api/openclaw/cron-errors")

    assert response.status_code == 200
    body = response.json()
    assert body["errors"] == []
    assert body["stale"]


@pytest.mark.asyncio
async def test_openclaw_cron_errors_excludes_disabled_error_jobs(monkeypatch, tmp_path):
    cron_dir = tmp_path / ".openclaw" / "cron"
    _write_json(
        cron_dir / "jobs.json",
        {
            "version": 1,
            "jobs": [
                {
                    "id": "disabled-error",
                    "name": "Disabled Error",
                    "enabled": False,
                }
            ],
        },
    )
    _write_json(
        cron_dir / "jobs-state.json",
        {
            "version": 1,
            "jobs": {
                "disabled-error": {
                    "state": {
                        "lastStatus": "error",
                        "lastRunStatus": "error",
                        "lastError": "disabled failed",
                        "consecutiveErrors": 9,
                        "lastRunAtMs": 1_780_041_722_000,
                    }
                }
            },
        },
    )

    response = await _get(_app_with_cron_dir(monkeypatch, cron_dir), "/api/openclaw/cron-errors")

    assert response.status_code == 200
    assert response.json() == {"errors": []}


@pytest.mark.asyncio
async def test_openclaw_cron_errors_does_not_leak_structured_last_error(monkeypatch, tmp_path):
    cron_dir = tmp_path / ".openclaw" / "cron"
    _write_json(
        cron_dir / "jobs.json",
        {
            "version": 1,
            "jobs": [
                {
                    "id": "structured-error",
                    "name": "Structured Error",
                    "enabled": True,
                }
            ],
        },
    )
    _write_json(
        cron_dir / "jobs-state.json",
        {
            "version": 1,
            "jobs": {
                "structured-error": {
                    "state": {
                        "lastStatus": "error",
                        "lastError": {
                            "message": "failed",
                            "token": "must-not-leak",
                            "sessionKey": "must-not-leak",
                            "payload": {"delivery": "must-not-leak"},
                        },
                    }
                }
            },
        },
    )

    response = await _get(_app_with_cron_dir(monkeypatch, cron_dir), "/api/openclaw/cron-errors")

    assert response.status_code == 200
    assert response.json() == {
        "errors": [
            {
                "id": "structured-error",
                "name": "Structured Error",
                "lastError": None,
                "consecutiveErrors": 0,
                "lastRunAt": None,
            }
        ]
    }
    assert "must-not-leak" not in response.text
