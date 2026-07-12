from __future__ import annotations

import sys
import types
from pathlib import Path

import pytest

from scripts import control_shot


class FakeResponse:
    def __init__(self, *, ok: bool = False) -> None:
        self.status = 200 if ok else 401
        self.ok = ok

    def json(self):
        return {"ok": self.ok}


class FakeRequest:
    def __init__(self, *, login_ok: bool = False) -> None:
        self.login_ok = login_ok

    def post(self, *args, **kwargs):
        return FakeResponse(ok=self.login_ok)


class FakeResource:
    def __init__(self) -> None:
        self.close_calls = 0

    def close(self) -> None:
        self.close_calls += 1


class FakeContext(FakeResource):
    def __init__(self, *, login_ok: bool = False) -> None:
        super().__init__()
        self.request = FakeRequest(login_ok=login_ok)
        self.page = FakePage()

    def new_page(self):
        return self.page


class FakePage:
    def goto(self, *args, **kwargs):
        return FakeResponse(ok=True)

    def wait_for_timeout(self, wait_ms: int) -> None:
        pass

    def screenshot(self, *args, **kwargs) -> None:
        pass


class FakeBrowser(FakeResource):
    def __init__(self, *, login_ok: bool = False) -> None:
        super().__init__()
        self.context = FakeContext(login_ok=login_ok)

    def new_context(self, **kwargs):
        return self.context


class FakeChromium:
    def __init__(self, browser: FakeBrowser) -> None:
        self.browser = browser

    def launch(self, **kwargs):
        return self.browser


class FakePlaywright:
    def __init__(self, browser: FakeBrowser) -> None:
        self.chromium = FakeChromium(browser)

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc, tb):
        return False


def test_login_failure_closes_context_and_browser(monkeypatch, tmp_path: Path):
    browser = FakeBrowser()
    fake_module = types.SimpleNamespace(sync_playwright=lambda: FakePlaywright(browser))
    monkeypatch.setitem(sys.modules, "playwright.sync_api", fake_module)
    monkeypatch.setattr(control_shot, "_credentials", lambda: ("user", "password"))

    with pytest.raises(control_shot.ShotError, match="login failed: HTTP 401"):
        control_shot.take_shot(
            "http://example.test",
            "/control",
            tmp_path / "shot.png",
            width=800,
            height=600,
            wait_ms=0,
            full_page=False,
        )

    assert browser.context.close_calls == 1
    assert browser.close_calls == 1


def test_successful_capture_closes_context_and_browser(monkeypatch, tmp_path: Path):
    browser = FakeBrowser(login_ok=True)
    fake_module = types.SimpleNamespace(sync_playwright=lambda: FakePlaywright(browser))
    monkeypatch.setitem(sys.modules, "playwright.sync_api", fake_module)
    monkeypatch.setattr(control_shot, "_credentials", lambda: ("user", "password"))

    control_shot.take_shot(
        "http://example.test",
        "/control",
        tmp_path / "shot.png",
        width=800,
        height=600,
        wait_ms=0,
        full_page=False,
    )

    assert browser.context.close_calls == 1
    assert browser.close_calls == 1
