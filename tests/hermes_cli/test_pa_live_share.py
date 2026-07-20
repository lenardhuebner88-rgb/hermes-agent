from __future__ import annotations

from pathlib import Path

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

import hermes_cli.pa_chat as pa
from hermes_cli.pa_live_share import (
    LiveShareNoFrame,
    LiveShareNotFound,
    LiveShareRegistry,
)


@pytest.fixture
def isolated_pa_home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    home = tmp_path / "home"
    hermes_home = home / ".hermes"
    hermes_home.mkdir(parents=True)
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("HERMES_HOME", str(hermes_home))
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: home))
    return hermes_home


# ── Pure registry ──────────────────────────────────────────────────────────


class _Clock:
    def __init__(self) -> None:
        self.t = 1_000.0

    def __call__(self) -> float:
        return self.t


def test_start_frame_and_latest_wins() -> None:
    clock = _Clock()
    reg = LiveShareRegistry(clock=clock)
    sid = reg.start()
    assert sid.startswith("live_")
    assert reg.active_count() == 1

    # No frame yet → NoFrame, not NotFound.
    with pytest.raises(LiveShareNoFrame):
        reg.latest_frame(sid)

    reg.put_frame(sid, b"first", ".jpg")
    reg.put_frame(sid, b"second", ".jpg")
    # Latest wins: only the newest frame is retained.
    assert reg.latest_frame(sid) == (b"second", ".jpg")


def test_unknown_session_raises_not_found() -> None:
    reg = LiveShareRegistry()
    with pytest.raises(LiveShareNotFound):
        reg.put_frame("live_missing", b"x", ".jpg")
    with pytest.raises(LiveShareNotFound):
        reg.latest_frame("live_missing")


def test_idle_ttl_sweeps_abandoned_session() -> None:
    clock = _Clock()
    reg = LiveShareRegistry(ttl_seconds=100.0, clock=clock)
    sid = reg.start()
    reg.put_frame(sid, b"frame", ".jpg")

    clock.t += 99.0
    # Still fresh — a frame keeps it alive.
    assert reg.latest_frame(sid) == (b"frame", ".jpg")

    clock.t += 101.0  # now > ttl since last activity
    assert reg.active_count() == 0
    with pytest.raises(LiveShareNotFound):
        reg.latest_frame(sid)


def test_stop_is_idempotent() -> None:
    reg = LiveShareRegistry()
    sid = reg.start()
    assert reg.stop(sid) is True
    assert reg.stop(sid) is False
    with pytest.raises(LiveShareNotFound):
        reg.latest_frame(sid)


def test_frame_byte_cap_rejected() -> None:
    reg = LiveShareRegistry(max_frame_bytes=8)
    sid = reg.start()
    with pytest.raises(Exception):
        reg.put_frame(sid, b"0123456789", ".jpg")


def test_max_sessions_evicts_oldest() -> None:
    clock = _Clock()
    reg = LiveShareRegistry(max_sessions=2, clock=clock)
    first = reg.start()
    clock.t += 1
    second = reg.start()
    clock.t += 1
    third = reg.start()  # over capacity → oldest (first) evicted
    assert reg.active_count() == 2
    with pytest.raises(LiveShareNotFound):
        reg.latest_frame(first)
    # second/third survive
    reg.put_frame(second, b"a", ".jpg")
    reg.put_frame(third, b"b", ".jpg")


# ── HTTP routes ─────────────────────────────────────────────────────────────

_JPEG = b"\xff\xd8\xff\xe0frame-bytes"


def test_live_share_http_roundtrip_materialises_one_asset(
    isolated_pa_home: Path,
) -> None:
    app = FastAPI()
    pa.register_pa_routes(app)
    with TestClient(app) as client:
        started = client.post("/api/pa/live-share/start")
        assert started.status_code == 200
        sid = started.json()["session_id"]

        # Stream two frames; latest wins.
        r1 = client.post(
            f"/api/pa/live-share/{sid}/frame",
            files={"file": ("f.jpg", b"\xff\xd8\xff\xe0old", "image/jpeg")},
        )
        assert r1.status_code == 200 and r1.json() == {"ok": True}
        r2 = client.post(
            f"/api/pa/live-share/{sid}/frame",
            files={"file": ("f.jpg", _JPEG, "image/jpeg")},
        )
        assert r2.status_code == 200

        attach = client.post(f"/api/pa/live-share/{sid}/attach")
        assert attach.status_code == 200
        asset_id = attach.json()["asset_id"]

        # The materialised asset is a normal upload asset the turn pipeline can
        # serve — and it holds the LATEST frame.
        asset = client.get(f"/api/pa/asset/{asset_id}")
        assert asset.status_code == 200
        assert asset.content == _JPEG

        stopped = client.post(f"/api/pa/live-share/{sid}/stop")
        assert stopped.status_code == 200 and stopped.json() == {"ok": True}
        # After stop the session is gone.
        assert client.post(f"/api/pa/live-share/{sid}/attach").status_code == 404


def test_live_share_frame_rejects_non_image_and_unknown_session(
    isolated_pa_home: Path,
) -> None:
    app = FastAPI()
    pa.register_pa_routes(app)
    with TestClient(app) as client:
        sid = client.post("/api/pa/live-share/start").json()["session_id"]
        bad = client.post(
            f"/api/pa/live-share/{sid}/frame",
            files={"file": ("f.txt", b"not-an-image", "text/plain")},
        )
        assert bad.status_code == 400

        missing = client.post(
            "/api/pa/live-share/live_missing/frame",
            files={"file": ("f.jpg", _JPEG, "image/jpeg")},
        )
        assert missing.status_code == 404


def test_live_share_attach_without_frame_is_409(isolated_pa_home: Path) -> None:
    app = FastAPI()
    pa.register_pa_routes(app)
    with TestClient(app) as client:
        sid = client.post("/api/pa/live-share/start").json()["session_id"]
        attach = client.post(f"/api/pa/live-share/{sid}/attach")
        assert attach.status_code == 409


def test_live_share_stop_unknown_is_ok_false(isolated_pa_home: Path) -> None:
    app = FastAPI()
    pa.register_pa_routes(app)
    with TestClient(app) as client:
        stopped = client.post("/api/pa/live-share/live_missing/stop")
        assert stopped.status_code == 200
        assert stopped.json() == {"ok": False}
