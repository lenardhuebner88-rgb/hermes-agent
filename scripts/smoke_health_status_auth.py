#!/usr/bin/env python3
"""Authenticated smoke test for the gated dashboard health endpoint.

Logs in through /auth/password-login, keeps the session cookie in memory, then
fetches /api/health-status. Passwords, tokens, and cookies are never printed.
"""
from __future__ import annotations

import argparse
import getpass
import json
import os
import re
import sys
import urllib.error
import urllib.parse
import urllib.request
from http.cookiejar import CookieJar
from typing import Any


DEFAULT_URL = "http://127.0.0.1:9119"


class SmokeError(RuntimeError):
    """User-facing smoke failure."""


def _json_request(
    opener: urllib.request.OpenerDirector,
    method: str,
    url: str,
    *,
    payload: dict[str, Any] | None = None,
    extra_headers: dict[str, str] | None = None,
    timeout: float,
) -> dict[str, Any]:
    data = None
    headers = {"Accept": "application/json"}
    if extra_headers:
        headers.update(extra_headers)
    if payload is not None:
        data = json.dumps(payload).encode("utf-8")
        headers["Content-Type"] = "application/json"

    request = urllib.request.Request(url, data=data, headers=headers, method=method)
    try:
        with opener.open(request, timeout=timeout) as response:
            raw = response.read().decode("utf-8")
            status = response.status
    except urllib.error.HTTPError as exc:
        body = exc.read().decode("utf-8", errors="replace")
        raise SmokeError(f"{method} {url} returned HTTP {exc.code}: {body[:300]}") from exc
    except urllib.error.URLError as exc:
        raise SmokeError(f"{method} {url} failed: {exc.reason}") from exc

    if status < 200 or status >= 300:
        raise SmokeError(f"{method} {url} returned HTTP {status}")
    try:
        parsed = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise SmokeError(f"{method} {url} returned invalid JSON") from exc
    if not isinstance(parsed, dict):
        raise SmokeError(f"{method} {url} returned a non-object JSON payload")
    return parsed


def _text_request(
    opener: urllib.request.OpenerDirector,
    url: str,
    *,
    timeout: float,
) -> str:
    request = urllib.request.Request(url, headers={"Accept": "text/html"}, method="GET")
    try:
        with opener.open(request, timeout=timeout) as response:
            return response.read().decode("utf-8")
    except urllib.error.HTTPError as exc:
        body = exc.read().decode("utf-8", errors="replace")
        raise SmokeError(f"GET {url} returned HTTP {exc.code}: {body[:300]}") from exc
    except urllib.error.URLError as exc:
        raise SmokeError(f"GET {url} failed: {exc.reason}") from exc


_SESSION_TOKEN_RE = re.compile(
    r"window\.__HERMES_SESSION_TOKEN__=(\"(?:\\.|[^\"\\])*\")"
)


def _session_token_from_html(html: str) -> str:
    """Extract the JSON-escaped loopback token without ever logging it."""
    match = _SESSION_TOKEN_RE.search(html)
    if match is None:
        raise SmokeError("authenticated dashboard HTML is missing the session token injection")
    try:
        token = json.loads(match.group(1))
    except json.JSONDecodeError as exc:
        raise SmokeError("dashboard session token injection is malformed") from exc
    if not isinstance(token, str) or not token:
        raise SmokeError("dashboard session token injection is empty")
    return token


def _base_url(raw: str) -> str:
    parsed = urllib.parse.urlparse(raw)
    if not parsed.scheme or not parsed.netloc:
        raise SmokeError(f"invalid dashboard URL: {raw!r}")
    return raw.rstrip("/")


def _read_password(env_name: str, *, no_prompt: bool) -> str:
    value = os.environ.get(env_name, "")
    if value:
        return value
    if no_prompt:
        raise SmokeError(f"{env_name} is not set")
    return getpass.getpass(f"{env_name}: ")


def _validate_health_payload(payload: dict[str, Any]) -> None:
    if payload.get("schema") != "hermes-health-v1":
        raise SmokeError("health payload has unexpected schema")
    if payload.get("overall") not in {"healthy", "degraded", "offline"}:
        raise SmokeError("health payload has unexpected overall status")
    subsystems = payload.get("subsystems")
    if not isinstance(subsystems, dict) or not subsystems:
        raise SmokeError("health payload is missing subsystem details")


def _summary(payload: dict[str, Any]) -> str:
    subsystems = payload.get("subsystems") or {}
    parts = []
    for name in sorted(subsystems):
        subsystem = subsystems.get(name) or {}
        status = subsystem.get("status", "unknown")
        if name == "kanban_dispatcher":
            age = subsystem.get("heartbeat_age_s")
            parts.append(f"{name}={status}(age_s={age})")
        else:
            parts.append(f"{name}={status}")
    return f"overall={payload.get('overall')} " + " ".join(parts)


def _parse_args(argv: list[str]) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Smoke-test /api/health-status through dashboard password auth.",
    )
    parser.add_argument(
        "--url",
        default=os.environ.get("HERMES_DASHBOARD_URL", DEFAULT_URL),
        help=f"Dashboard base URL (default: {DEFAULT_URL}, or HERMES_DASHBOARD_URL).",
    )
    parser.add_argument(
        "--provider",
        default=os.environ.get("HERMES_DASHBOARD_AUTH_PROVIDER", "basic"),
        help="Password-auth provider name (default: basic).",
    )
    parser.add_argument(
        "--username",
        default=os.environ.get("HERMES_DASHBOARD_USERNAME", ""),
        help="Dashboard username (default: HERMES_DASHBOARD_USERNAME).",
    )
    parser.add_argument(
        "--password-env",
        default="HERMES_DASHBOARD_PASSWORD",
        help="Environment variable containing the dashboard password.",
    )
    parser.add_argument(
        "--timeout",
        type=float,
        default=10.0,
        help="HTTP timeout in seconds.",
    )
    parser.add_argument(
        "--no-prompt",
        action="store_true",
        help="Fail instead of prompting when the password is not in the environment.",
    )
    parser.add_argument(
        "--json",
        action="store_true",
        help="Print the full health JSON payload after validation.",
    )
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = _parse_args(list(argv or sys.argv[1:]))
    try:
        base = _base_url(args.url)
        username = args.username
        if not username:
            if args.no_prompt:
                raise SmokeError("HERMES_DASHBOARD_USERNAME is not set")
            username = input("HERMES_DASHBOARD_USERNAME: ").strip()
        password = _read_password(args.password_env, no_prompt=args.no_prompt)

        jar = CookieJar()
        opener = urllib.request.build_opener(urllib.request.HTTPCookieProcessor(jar))
        login = _json_request(
            opener,
            "POST",
            f"{base}/auth/password-login",
            payload={
                "provider": args.provider,
                "username": username,
                "password": password,
                "next": "/api/health-status",
            },
            timeout=args.timeout,
        )
        if login.get("ok") is not True:
            raise SmokeError("password login did not return ok=true")

        # Password auth establishes the browser/session cookie. In loopback
        # ``--insecure`` mode protected APIs additionally require the ephemeral
        # token injected into the authenticated SPA HTML; gated mode omits it
        # and authorizes the cookie directly. Mirror the browser in both modes.
        control_html = _text_request(opener, f"{base}/control", timeout=args.timeout)
        api_headers: dict[str, str] = {}
        if "window.__HERMES_SESSION_TOKEN__" in control_html:
            api_headers["X-Hermes-Session-Token"] = _session_token_from_html(control_html)

        payload = _json_request(
            opener,
            "GET",
            f"{base}/api/health-status",
            extra_headers=api_headers,
            timeout=args.timeout,
        )
        _validate_health_payload(payload)
    except SmokeError as exc:
        print(f"health-status smoke failed: {exc}", file=sys.stderr)
        return 1

    if args.json:
        print(json.dumps(payload, indent=2, sort_keys=True))
    else:
        print(_summary(payload))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
