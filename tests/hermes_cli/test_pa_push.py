from __future__ import annotations

import sys
import types
from pathlib import Path

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

import hermes_cli.pa_chat as pa
import hermes_cli.pa_push as pp
from hermes_cli import kanban_db as kb


@pytest.fixture
def isolated_pa_home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    home = tmp_path / "home"
    hermes_home = home / ".hermes"
    hermes_home.mkdir(parents=True)
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("HERMES_HOME", str(hermes_home))
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: home))
    return hermes_home


def _insert_subscription(endpoint: str = "https://push.example/sub1") -> None:
    with kb.connect_closing() as conn:
        conn.execute(
            "INSERT INTO push_subscriptions (endpoint, keys_p256dh, keys_auth, "
            "created_at, fail_count) VALUES (?, 'k1', 'k2', 1, 0)",
            (endpoint,),
        )
        conn.commit()


def _fake_pywebpush(monkeypatch: pytest.MonkeyPatch, calls: list[dict]) -> None:
    module = types.ModuleType("pywebpush")

    class WebPushException(Exception):
        def __init__(self, *args: object, status_code: int | None = None) -> None:
            super().__init__(*args)
            self.response = types.SimpleNamespace(status_code=status_code)

    def webpush(**kwargs: object) -> None:
        calls.append(kwargs)

    module.WebPushException = WebPushException  # type: ignore[attr-defined]
    module.webpush = webpush  # type: ignore[attr-defined]
    monkeypatch.setitem(sys.modules, "pywebpush", module)
    return WebPushException


def test_send_disabled_without_vapid(isolated_pa_home: Path, monkeypatch) -> None:
    for name in ("VAPID_PRIVATE_KEY", "VAPID_PUBLIC_KEY", "VAPID_CLAIMS_SUB"):
        monkeypatch.delenv(name, raising=False)

    result = pp.send_pa_push(title="t", body="b", tag="x")

    assert result == {"enabled": False, "sent": 0, "removed": 0, "failed": 0}


def test_send_delivers_and_records_success(
    isolated_pa_home: Path, monkeypatch
) -> None:
    monkeypatch.setenv("VAPID_PRIVATE_KEY", "priv")
    monkeypatch.setenv("VAPID_PUBLIC_KEY", "pub")
    monkeypatch.setenv("VAPID_CLAIMS_SUB", "mailto:test@example.com")
    calls: list[dict] = []
    _fake_pywebpush(monkeypatch, calls)
    _insert_subscription()

    result = pp.send_pa_push(title="Jarvis", body="Hallo", tag="t1")

    assert result == {"enabled": True, "sent": 1, "removed": 0, "failed": 0}
    assert calls[0]["vapid_private_key"] == "priv"
    assert calls[0]["timeout"] == 10
    import json

    payload = json.loads(calls[0]["data"])
    assert payload["type"] == "pa"
    assert payload["url"] == pp.PA_THREAD_URL
    with kb.connect_closing() as conn:
        row = conn.execute(
            "SELECT last_success_at, fail_count FROM push_subscriptions"
        ).fetchone()
    assert row["last_success_at"] is not None and row["fail_count"] == 0


def test_send_removes_gone_subscription(
    isolated_pa_home: Path, monkeypatch
) -> None:
    monkeypatch.setenv("VAPID_PRIVATE_KEY", "priv")
    monkeypatch.setenv("VAPID_PUBLIC_KEY", "pub")
    monkeypatch.setenv("VAPID_CLAIMS_SUB", "mailto:test@example.com")
    calls: list[dict] = []
    exc_cls = _fake_pywebpush(monkeypatch, calls)
    _insert_subscription("https://push.example/gone")

    def boom(**kwargs: object) -> None:
        raise exc_cls("gone", status_code=410)

    monkeypatch.setitem(sys.modules, "pywebpush", types.SimpleNamespace(
        WebPushException=exc_cls, webpush=boom
    ))

    result = pp.send_pa_push(title="t", body="b", tag="x")

    assert result["removed"] == 1 and result["sent"] == 0
    with kb.connect_closing() as conn:
        assert conn.execute("SELECT COUNT(*) FROM push_subscriptions").fetchone()[0] == 0


def test_enqueue_pushes_only_on_fresh_insert(
    isolated_pa_home: Path, monkeypatch
) -> None:
    from hermes_cli import pa_actions

    pushed: list[int] = []
    monkeypatch.setattr(
        pa_actions, "_notify_enqueued", lambda eid, text: pushed.append(eid)
    )

    first = pa_actions.enqueue_pa_action(
        "tmux.interrupt", {"session": "work", "window": "kimi"}, reason="r"
    )
    second = pa_actions.enqueue_pa_action(
        "tmux.interrupt", {"session": "work", "window": "kimi"}, reason="r"
    )

    assert first == second  # dedup: gleiche offene Aktion
    assert pushed == [first]  # Push nur beim ersten Insert


def test_push_test_endpoint(isolated_pa_home: Path, monkeypatch) -> None:
    monkeypatch.setattr(
        pp, "send_pa_push", lambda **kwargs: {"enabled": True, "sent": 2, "removed": 0, "failed": 0}
    )
    app = FastAPI()
    pa.register_pa_routes(app)

    with TestClient(app) as client:
        response = client.post("/api/pa/push/test")

    assert response.status_code == 200
    assert response.json() == {"enabled": True, "sent": 2, "removed": 0, "failed": 0}
