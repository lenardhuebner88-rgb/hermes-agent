#!/usr/bin/env python3
"""Authenticated screenshot of a Hermes /control dashboard route.

Logs in through /auth/password-login (session cookie kept in-memory by the
Playwright browser context), navigates to the given route, and saves a PNG.
Passwords and cookies are never printed.

Usage:
    venv/bin/python scripts/control_shot.py /control/design-board /tmp/out.png
"""
from __future__ import annotations

import argparse
import os
import signal
import sys
import threading
from pathlib import Path

DEFAULT_BASE = "http://127.0.0.1:9119"
ENV_FILE = Path.home() / ".hermes" / ".env"


class ShotError(RuntimeError):
    """User-facing failure."""


class _CloseTimeout(BaseException):
    """Internal signal used to interrupt a hung synchronous Playwright close."""


def _close_quietly(resource: object | None, *, timeout_seconds: float = 2.0) -> None:
    """Bounded best-effort close on the Playwright sync API's calling thread."""
    if resource is None:
        return

    if threading.current_thread() is not threading.main_thread():
        raise RuntimeError("Playwright sync resources must close on the main thread")

    def interrupt_close(_signum: int, _frame: object) -> None:
        raise _CloseTimeout

    previous_handler = signal.signal(signal.SIGALRM, interrupt_close)
    previous_timer = signal.setitimer(signal.ITIMER_REAL, timeout_seconds)
    try:
        # SIGALRM bounds the call without moving thread-affine Playwright sync
        # objects into the incompatible worker thread used by the first fix.
        resource.close()  # type: ignore[attr-defined]
    except (_CloseTimeout, Exception):  # noqa: BLE001 - preserve the original outcome
        pass
    finally:
        signal.setitimer(signal.ITIMER_REAL, 0)
        signal.signal(signal.SIGALRM, previous_handler)
        signal.setitimer(signal.ITIMER_REAL, *previous_timer)


def _load_env_file(path: Path) -> dict[str, str]:
    values: dict[str, str] = {}
    if not path.exists():
        return values
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        values[key.strip()] = value.strip()
    return values


def _credentials() -> tuple[str, str]:
    username = os.environ.get("HERMES_DASHBOARD_USERNAME")
    password = os.environ.get("HERMES_DASHBOARD_PASSWORD")
    if username and password:
        return username, password
    env = _load_env_file(ENV_FILE)
    username = username or env.get("HERMES_DASHBOARD_USERNAME")
    password = password or env.get("HERMES_DASHBOARD_PASSWORD")
    if not username or not password:
        raise ShotError(
            "missing credentials: set HERMES_DASHBOARD_USERNAME and "
            "HERMES_DASHBOARD_PASSWORD (env or ~/.hermes/.env)"
        )
    return username, password


def _parse_viewport(spec: str) -> tuple[int, int]:
    try:
        width_s, height_s = spec.lower().split("x", 1)
        return int(width_s), int(height_s)
    except ValueError as exc:
        raise ShotError(f"invalid --viewport {spec!r}, expected WxH") from exc


def _resolve_url(base: str, route: str) -> str:
    if route.startswith("http://") or route.startswith("https://"):
        return route
    if not route.startswith("/"):
        route = "/" + route
    return base.rstrip("/") + route


def take_shot(
    base: str,
    route: str,
    out: Path,
    *,
    width: int,
    height: int,
    wait_ms: int,
    full_page: bool,
) -> None:
    username, password = _credentials()

    try:
        from playwright.sync_api import sync_playwright
    except ImportError as exc:
        raise ShotError(f"playwright not importable: {exc}") from exc

    with sync_playwright() as p:
        try:
            browser = p.chromium.launch(headless=True)
        except Exception as exc:  # noqa: BLE001 - surface as clear exit-3 case
            if "Executable doesn't exist" in str(exc):
                print(
                    "chromium missing: run "
                    "`venv/bin/python -m playwright install chromium`",
                    file=sys.stderr,
                )
                sys.exit(3)
            raise ShotError(f"failed to launch chromium: {exc}") from exc

        context = None
        try:
            context = browser.new_context(viewport={"width": width, "height": height})
            login_response = context.request.post(
                f"{base.rstrip('/')}/auth/password-login",
                data={"provider": "basic", "username": username, "password": password, "next": "/"},
            )
            status = login_response.status
            if not login_response.ok:
                raise ShotError(f"login failed: HTTP {status}")
            body = login_response.json()
            if not body.get("ok"):
                raise ShotError(f"login failed: server rejected credentials (HTTP {status})")
            print(f"login: ok ({status})")

            page = context.new_page()
            url = _resolve_url(base, route)
            response = page.goto(url, wait_until="networkidle", timeout=30_000)
            if response is not None and response.status >= 400:
                raise ShotError(f"route {url} returned HTTP {response.status}")
            page.wait_for_timeout(wait_ms)
            out.parent.mkdir(parents=True, exist_ok=True)
            page.screenshot(path=str(out), full_page=full_page)
        finally:
            _close_quietly(context)
            _close_quietly(browser)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("route", help="path like /control/design-board or full URL")
    parser.add_argument("out", type=Path, help="output PNG path")
    parser.add_argument("--base", default=DEFAULT_BASE, help=f"base URL (default {DEFAULT_BASE})")
    parser.add_argument("--viewport", default="1440x900", help="WxH (default 1440x900)")
    parser.add_argument("--wait-ms", type=int, default=800, help="extra settle delay in ms")
    parser.add_argument("--full-page", action="store_true", help="capture full scrollable page")
    args = parser.parse_args()

    width, height = _parse_viewport(args.viewport)

    try:
        take_shot(
            args.base,
            args.route,
            args.out,
            width=width,
            height=height,
            wait_ms=args.wait_ms,
            full_page=args.full_page,
        )
    except ShotError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1

    print(str(args.out))
    return 0


if __name__ == "__main__":
    sys.exit(main())
