from __future__ import annotations

import sys
import threading
import time
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
        self.close_threads: list[threading.Thread] = []

    def close(self) -> None:
        self.close_calls += 1
        self.close_threads.append(threading.current_thread())


class HangingFakeResource(FakeResource):
    def close(self) -> None:
        super().close()
        time.sleep(1)


class FakeProcessTable:
    def __init__(self) -> None:
        self.chrome_pids: set[int] = set()

    def ps(self, process_name: str) -> set[int]:
        assert process_name == "chrome"
        return set(self.chrome_pids)


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
    def __init__(
        self,
        *,
        login_ok: bool = False,
        process_table: FakeProcessTable | None = None,
    ) -> None:
        super().__init__()
        self.context = FakeContext(login_ok=login_ok)
        self.process_table = process_table
        self.pid = 4242

    def new_context(self, **kwargs):
        return self.context

    def close(self) -> None:
        super().close()
        if self.process_table is not None:
            self.process_table.chrome_pids.discard(self.pid)


class FakeChromium:
    def __init__(self, browser: FakeBrowser) -> None:
        self.browser = browser

    def launch(self, **kwargs):
        if self.browser.process_table is not None:
            self.browser.process_table.chrome_pids.add(self.browser.pid)
        return self.browser


class FakePlaywright:
    def __init__(self, browser: FakeBrowser) -> None:
        self.chromium = FakeChromium(browser)

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc, tb):
        return False


def test_login_failure_closes_context_and_browser(monkeypatch, tmp_path: Path):
    calling_thread = threading.current_thread()
    process_table = FakeProcessTable()
    browser = FakeBrowser(process_table=process_table)
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
    assert browser.context.close_threads == [calling_thread]
    assert browser.close_threads == [calling_thread]
    assert process_table.ps("chrome") == set()


def test_close_quietly_bounds_hung_close_on_calling_thread():
    resource = HangingFakeResource()
    calling_thread = threading.current_thread()

    started = time.monotonic()
    control_shot._close_quietly(resource, timeout_seconds=0.01)
    elapsed = time.monotonic() - started

    assert elapsed < 0.5
    assert resource.close_calls == 1
    assert resource.close_threads == [calling_thread]


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


# ---------------------------------------------------------------------------
# Env/credential/URL helper pinning
# ---------------------------------------------------------------------------

def test_load_env_file_skips_comments_and_garbage_lines(tmp_path: Path):
    """Comments and lines without '=' are not credentials — parsing them
    would inject junk keys into the fallback lookup."""
    env = tmp_path / ".env"
    env.write_text(
        "# a comment\nnot-an-assignment\nKEY=value\n", encoding="utf-8"
    )
    assert control_shot._load_env_file(env) == {"KEY": "value"}


def test_credentials_prefers_process_env_and_fails_closed_on_partial(
    monkeypatch, tmp_path: Path
):
    """With only ONE of the two variables set and an empty env file the
    lookup must raise — returning (user, None) would log in with an empty
    password."""
    empty = tmp_path / "empty.env"
    empty.write_text("", encoding="utf-8")
    monkeypatch.setattr(control_shot, "ENV_FILE", empty)
    monkeypatch.setenv("HERMES_DASHBOARD_USERNAME", "u")
    monkeypatch.delenv("HERMES_DASHBOARD_PASSWORD", raising=False)
    with pytest.raises(control_shot.ShotError):
        control_shot._credentials()


def test_credentials_falls_back_to_env_file_per_variable(
    monkeypatch, tmp_path: Path
):
    """Process env and .env mix PER variable: username from the process,
    password from the file — an and-shortcut would discard the file half."""
    env = tmp_path / ".env"
    env.write_text(
        "HERMES_DASHBOARD_USERNAME=file-user\n"
        "HERMES_DASHBOARD_PASSWORD=file-pass\n",
        encoding="utf-8",
    )
    monkeypatch.setattr(control_shot, "ENV_FILE", env)

    monkeypatch.setenv("HERMES_DASHBOARD_USERNAME", "proc-user")
    monkeypatch.delenv("HERMES_DASHBOARD_PASSWORD", raising=False)
    assert control_shot._credentials() == ("proc-user", "file-pass")

    monkeypatch.delenv("HERMES_DASHBOARD_USERNAME", raising=False)
    monkeypatch.setenv("HERMES_DASHBOARD_PASSWORD", "proc-pass")
    assert control_shot._credentials() == ("file-user", "proc-pass")


def test_resolve_url_passes_absolute_routes_through():
    """A full http:// URL must be used verbatim — prepending the base
    would point the browser at a garbage path."""
    assert control_shot._resolve_url("http://127.0.0.1:9119", "http://x/y") == "http://x/y"
    assert control_shot._resolve_url("http://127.0.0.1:9119/", "https://x/y") == "https://x/y"
    assert control_shot._resolve_url("http://127.0.0.1:9119/", "control") == "http://127.0.0.1:9119/control"
