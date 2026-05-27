"""Tests for Codex auth — tokens stored in Hermes auth store (~/.hermes/auth.json)."""

import json
import time
import base64
from pathlib import Path
from types import SimpleNamespace

import pytest
import yaml

from hermes_cli.auth import (
    AuthError,
    DEFAULT_CODEX_BASE_URL,
    PROVIDER_REGISTRY,
    _read_codex_tokens,
    _save_codex_tokens,
    _import_codex_cli_tokens,
    _login_openai_codex,
    get_codex_auth_status,
    get_provider_auth_state,
    refresh_codex_oauth_pure,
    resolve_codex_runtime_credentials,
    resolve_provider,
)


def _setup_hermes_auth(hermes_home: Path, *, access_token: str = "access", refresh_token: str = "refresh"):
    """Write Codex tokens into the Hermes auth store."""
    hermes_home.mkdir(parents=True, exist_ok=True)
    auth_store = {
        "version": 1,
        "active_provider": "openai-codex",
        "providers": {
            "openai-codex": {
                "tokens": {
                    "access_token": access_token,
                    "refresh_token": refresh_token,
                },
                "last_refresh": "2026-02-26T00:00:00Z",
                "auth_mode": "chatgpt",
            },
        },
    }
    auth_file = hermes_home / "auth.json"
    auth_file.write_text(json.dumps(auth_store, indent=2))
    return auth_file


def _jwt_with_exp(exp_epoch: int) -> str:
    payload = {"exp": exp_epoch}
    encoded = base64.urlsafe_b64encode(json.dumps(payload).encode("utf-8")).rstrip(b"=").decode("utf-8")
    return f"h.{encoded}.s"


def test_read_codex_tokens_success(tmp_path, monkeypatch):
    hermes_home = tmp_path / "hermes"
    _setup_hermes_auth(hermes_home)
    monkeypatch.setenv("HERMES_HOME", str(hermes_home))

    data = _read_codex_tokens()
    assert data["tokens"]["access_token"] == "access"
    assert data["tokens"]["refresh_token"] == "refresh"


def test_read_codex_tokens_missing(tmp_path, monkeypatch):
    hermes_home = tmp_path / "hermes"
    hermes_home.mkdir(parents=True, exist_ok=True)
    # Empty auth store
    (hermes_home / "auth.json").write_text(json.dumps({"version": 1, "providers": {}}))
    monkeypatch.setenv("HERMES_HOME", str(hermes_home))

    with pytest.raises(AuthError) as exc:
        _read_codex_tokens()
    assert exc.value.code == "codex_auth_missing"


def test_resolve_codex_runtime_credentials_missing_access_token(tmp_path, monkeypatch):
    hermes_home = tmp_path / "hermes"
    _setup_hermes_auth(hermes_home, access_token="")
    monkeypatch.setenv("HERMES_HOME", str(hermes_home))

    with pytest.raises(AuthError) as exc:
        resolve_codex_runtime_credentials()
    assert exc.value.code == "codex_auth_missing_access_token"
    assert exc.value.relogin_required is True


def test_resolve_codex_runtime_credentials_refreshes_expiring_token(tmp_path, monkeypatch):
    hermes_home = tmp_path / "hermes"
    expiring_token = _jwt_with_exp(int(time.time()) - 10)
    _setup_hermes_auth(hermes_home, access_token=expiring_token, refresh_token="refresh-old")
    monkeypatch.setenv("HERMES_HOME", str(hermes_home))

    called = {"count": 0}

    def _fake_refresh(tokens, timeout_seconds, **_kwargs):
        called["count"] += 1
        return {"access_token": "access-new", "refresh_token": "refresh-new"}

    monkeypatch.setattr("hermes_cli.auth._refresh_codex_auth_tokens", _fake_refresh)

    resolved = resolve_codex_runtime_credentials()

    assert called["count"] == 1
    assert resolved["api_key"] == "access-new"


def test_resolve_codex_runtime_credentials_force_refresh(tmp_path, monkeypatch):
    hermes_home = tmp_path / "hermes"
    _setup_hermes_auth(hermes_home, access_token="access-current", refresh_token="refresh-old")
    monkeypatch.setenv("HERMES_HOME", str(hermes_home))

    called = {"count": 0}

    def _fake_refresh(tokens, timeout_seconds, **_kwargs):
        called["count"] += 1
        return {"access_token": "access-forced", "refresh_token": "refresh-new"}

    monkeypatch.setattr("hermes_cli.auth._refresh_codex_auth_tokens", _fake_refresh)

    resolved = resolve_codex_runtime_credentials(force_refresh=True, refresh_if_expiring=False)

    assert called["count"] == 1
    assert resolved["api_key"] == "access-forced"


def test_resolve_provider_explicit_codex_does_not_fallback(monkeypatch):
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    monkeypatch.delenv("OPENROUTER_API_KEY", raising=False)
    assert resolve_provider("openai-codex") == "openai-codex"


def test_save_codex_tokens_roundtrip(tmp_path, monkeypatch):
    hermes_home = tmp_path / "hermes"
    hermes_home.mkdir(parents=True, exist_ok=True)
    (hermes_home / "auth.json").write_text(json.dumps({"version": 1, "providers": {}}))
    monkeypatch.setenv("HERMES_HOME", str(hermes_home))

    _save_codex_tokens({"access_token": "at123", "refresh_token": "rt456"})
    data = _read_codex_tokens()

    assert data["tokens"]["access_token"] == "at123"
    assert data["tokens"]["refresh_token"] == "rt456"


def test_import_codex_cli_tokens(tmp_path, monkeypatch):
    codex_home = tmp_path / "codex-cli"
    codex_home.mkdir(parents=True, exist_ok=True)
    (codex_home / "auth.json").write_text(json.dumps({
        "tokens": {"access_token": "cli-at", "refresh_token": "cli-rt"},
    }))
    monkeypatch.setenv("CODEX_HOME", str(codex_home))

    tokens = _import_codex_cli_tokens()
    assert tokens is not None
    assert tokens["access_token"] == "cli-at"
    assert tokens["refresh_token"] == "cli-rt"


def test_import_codex_cli_tokens_missing(tmp_path, monkeypatch):
    monkeypatch.setenv("CODEX_HOME", str(tmp_path / "nonexistent"))
    assert _import_codex_cli_tokens() is None


def test_codex_tokens_not_written_to_shared_file(tmp_path, monkeypatch):
    """Verify _save_codex_tokens writes only to Hermes auth store, not ~/.codex/."""
    hermes_home = tmp_path / "hermes"
    codex_home = tmp_path / "codex-cli"
    hermes_home.mkdir(parents=True, exist_ok=True)
    codex_home.mkdir(parents=True, exist_ok=True)

    (hermes_home / "auth.json").write_text(json.dumps({"version": 1, "providers": {}}))
    monkeypatch.setenv("HERMES_HOME", str(hermes_home))
    monkeypatch.setenv("CODEX_HOME", str(codex_home))

    _save_codex_tokens({"access_token": "hermes-at", "refresh_token": "hermes-rt"})

    # ~/.codex/auth.json should NOT exist — _save_codex_tokens only touches Hermes store
    assert not (codex_home / "auth.json").exists()

    # Hermes auth store should have the tokens
    data = _read_codex_tokens()
    assert data["tokens"]["access_token"] == "hermes-at"


def test_resolve_returns_hermes_auth_store_source(tmp_path, monkeypatch):
    hermes_home = tmp_path / "hermes"
    _setup_hermes_auth(hermes_home)
    monkeypatch.setenv("HERMES_HOME", str(hermes_home))

    creds = resolve_codex_runtime_credentials()
    assert creds["source"] == "hermes-auth-store"
    assert creds["provider"] == "openai-codex"
    assert creds["base_url"] == DEFAULT_CODEX_BASE_URL


class _StubHTTPResponse:
    def __init__(self, status_code: int, payload):
        self.status_code = status_code
        self._payload = payload
        self.text = json.dumps(payload) if isinstance(payload, (dict, list)) else str(payload)

    def json(self):
        if isinstance(self._payload, Exception):
            raise self._payload
        return self._payload


class _StubHTTPClient:
    def __init__(self, response):
        self._response = response

    def __enter__(self):
        return self

    def __exit__(self, *args):
        return False

    def post(self, *args, **kwargs):
        return self._response


def _patch_httpx(monkeypatch, response):
    def _factory(*args, **kwargs):
        return _StubHTTPClient(response)

    monkeypatch.setattr("hermes_cli.auth.httpx.Client", _factory)


def test_refresh_parses_openai_nested_error_shape_refresh_token_reused(monkeypatch):
    """OpenAI returns {"error": {"code": "refresh_token_reused", "message": "..."}}
    — parser must surface relogin_required and the dedicated message.
    """
    response = _StubHTTPResponse(
        401,
        {
            "error": {
                "message": "Your refresh token has already been used to generate a new access token. Please try signing in again.",
                "type": "invalid_request_error",
                "param": None,
                "code": "refresh_token_reused",
            }
        },
    )
    _patch_httpx(monkeypatch, response)

    with pytest.raises(AuthError) as exc_info:
        refresh_codex_oauth_pure("a-tok", "r-tok")

    err = exc_info.value
    assert err.code == "refresh_token_reused"
    assert err.relogin_required is True
    # The existing dedicated branch should override the message with actionable guidance.
    assert "already consumed by another client" in str(err)


def test_refresh_parses_openai_nested_error_shape_generic_code(monkeypatch):
    """Nested error with arbitrary code still surfaces code + message."""
    response = _StubHTTPResponse(
        400,
        {
            "error": {
                "message": "Invalid client credentials.",
                "type": "invalid_request_error",
                "code": "invalid_client",
            }
        },
    )
    _patch_httpx(monkeypatch, response)

    with pytest.raises(AuthError) as exc_info:
        refresh_codex_oauth_pure("a-tok", "r-tok")

    err = exc_info.value
    assert err.code == "invalid_client"
    assert "Invalid client credentials." in str(err)


def test_refresh_parses_oauth_spec_flat_error_shape_invalid_grant(monkeypatch):
    """Fallback path: OAuth spec-shape {"error": "invalid_grant", "error_description": "..."}
    must still map to relogin_required=True via the existing code set.
    """
    response = _StubHTTPResponse(
        400,
        {
            "error": "invalid_grant",
            "error_description": "Refresh token is expired or revoked.",
        },
    )
    _patch_httpx(monkeypatch, response)

    with pytest.raises(AuthError) as exc_info:
        refresh_codex_oauth_pure("a-tok", "r-tok")

    err = exc_info.value
    assert err.code == "invalid_grant"
    assert err.relogin_required is True
    assert "Refresh token is expired or revoked." in str(err)


def test_refresh_falls_back_to_generic_message_on_unparseable_body(monkeypatch):
    """No JSON body → generic 'with status 401' message; 401 always forces relogin."""
    response = _StubHTTPResponse(401, ValueError("not json"))
    _patch_httpx(monkeypatch, response)

    with pytest.raises(AuthError) as exc_info:
        refresh_codex_oauth_pure("a-tok", "r-tok")

    err = exc_info.value
    assert err.code == "codex_refresh_failed"
    # 401/403 from the token endpoint always means the refresh token is
    # invalid/expired — force relogin even without a parseable error body.
    assert err.relogin_required is True
    assert "status 401" in str(err)


def test_login_openai_codex_force_new_login_skips_existing_reuse_prompt(monkeypatch):
    called = {"device_login": 0}

    monkeypatch.setattr(
        "hermes_cli.auth.resolve_codex_runtime_credentials",
        lambda: {"base_url": DEFAULT_CODEX_BASE_URL},
    )
    monkeypatch.setattr(
        "hermes_cli.auth._import_codex_cli_tokens",
        lambda: {"access_token": "cli-at", "refresh_token": "cli-rt"},
    )
    monkeypatch.setattr(
        "hermes_cli.auth._codex_device_code_login",
        lambda: {
            "tokens": {"access_token": "fresh-at", "refresh_token": "fresh-rt"},
            "last_refresh": "2026-04-01T00:00:00Z",
            "base_url": DEFAULT_CODEX_BASE_URL,
        },
    )

    def _fake_save(tokens, last_refresh=None):
        called["device_login"] += 1
        called["tokens"] = dict(tokens)
        called["last_refresh"] = last_refresh

    monkeypatch.setattr("hermes_cli.auth._save_codex_tokens", _fake_save)
    monkeypatch.setattr("hermes_cli.auth._update_config_for_provider", lambda *args, **kwargs: "/tmp/config.yaml")
    monkeypatch.setattr(
        "builtins.input",
        lambda prompt="": (_ for _ in ()).throw(AssertionError("force_new_login should not prompt for reuse/import")),
    )

    _login_openai_codex(SimpleNamespace(), PROVIDER_REGISTRY["openai-codex"], force_new_login=True)

    assert called["device_login"] == 1
    assert called["tokens"]["access_token"] == "fresh-at"


# ---------------------------------------------------------------------------
# Codex CLI co-existence — adopt fresher tokens, recover from refresh_token_reused
# ---------------------------------------------------------------------------


def _write_codex_cli_auth(codex_home: Path, *, access_token: str, refresh_token: str, last_refresh: str) -> None:
    codex_home.mkdir(parents=True, exist_ok=True)
    (codex_home / "auth.json").write_text(json.dumps({
        "auth_mode": "chatgpt",
        "tokens": {
            "access_token": access_token,
            "refresh_token": refresh_token,
            "id_token": "id",
            "account_id": "acct",
        },
        "last_refresh": last_refresh,
    }))


def test_refresh_adopts_codex_cli_tokens_when_cli_is_strictly_newer(tmp_path, monkeypatch):
    """When the Codex CLI has rotated its refresh token after Hermes' last
    refresh, Hermes must adopt instead of calling the upstream endpoint —
    otherwise it burns its (now-stale) refresh token and gets a
    refresh_token_reused error back."""
    from hermes_cli.auth import _refresh_codex_auth_tokens

    hermes_home = tmp_path / "hermes"
    codex_home = tmp_path / "codex-cli"
    _setup_hermes_auth(hermes_home)
    monkeypatch.setenv("HERMES_HOME", str(hermes_home))
    monkeypatch.setenv("CODEX_HOME", str(codex_home))

    # CLI tokens are NEWER and not expiring
    fresh_jwt = _jwt_with_exp(int(time.time()) + 3600)
    _write_codex_cli_auth(
        codex_home,
        access_token=fresh_jwt,
        refresh_token="cli-rt-NEW",
        last_refresh="2099-01-01T00:00:00Z",
    )

    refresh_called = {"count": 0}

    def _should_not_be_called(*a, **kw):
        refresh_called["count"] += 1
        raise AssertionError("refresh_codex_oauth_pure should NOT be called when CLI is fresher")

    monkeypatch.setattr("hermes_cli.auth.refresh_codex_oauth_pure", _should_not_be_called)

    result = _refresh_codex_auth_tokens(
        {"access_token": "expiring-hermes-at", "refresh_token": "old-hermes-rt"},
        timeout_seconds=5.0,
        current_last_refresh="2026-02-26T00:00:00Z",
    )

    assert refresh_called["count"] == 0
    assert result["access_token"] == fresh_jwt
    assert result["refresh_token"] == "cli-rt-NEW"
    # Hermes auth store should now hold the CLI tokens.
    persisted = _read_codex_tokens()
    assert persisted["tokens"]["refresh_token"] == "cli-rt-NEW"
    assert persisted["last_refresh"] == "2099-01-01T00:00:00Z"


def test_refresh_ignores_codex_cli_when_cli_is_older(tmp_path, monkeypatch):
    """When the CLI is older (e.g. user hasn't run codex recently) the
    refresh must fall through to the upstream endpoint as before."""
    from hermes_cli.auth import _refresh_codex_auth_tokens

    hermes_home = tmp_path / "hermes"
    codex_home = tmp_path / "codex-cli"
    _setup_hermes_auth(hermes_home)
    monkeypatch.setenv("HERMES_HOME", str(hermes_home))
    monkeypatch.setenv("CODEX_HOME", str(codex_home))

    stale_jwt = _jwt_with_exp(int(time.time()) + 3600)
    _write_codex_cli_auth(
        codex_home,
        access_token=stale_jwt,
        refresh_token="cli-rt-OLD",
        last_refresh="2020-01-01T00:00:00Z",
    )

    refresh_called = {"count": 0}

    def _fake_refresh(access_token, refresh_token, *, timeout_seconds):
        refresh_called["count"] += 1
        return {"access_token": "hermes-at-NEW", "refresh_token": "hermes-rt-NEW"}

    monkeypatch.setattr("hermes_cli.auth.refresh_codex_oauth_pure", _fake_refresh)

    # Hermes' own access token is still live, so neither "cli is newer" nor
    # "hermes is expiring" trigger adoption — upstream refresh runs.
    hermes_live_jwt = _jwt_with_exp(int(time.time()) + 3600)
    result = _refresh_codex_auth_tokens(
        {"access_token": hermes_live_jwt, "refresh_token": "hermes-rt-CURRENT"},
        timeout_seconds=5.0,
        current_last_refresh="2026-02-26T00:00:00Z",
    )

    assert refresh_called["count"] == 1
    assert result["refresh_token"] == "hermes-rt-NEW"


def test_refresh_recovers_from_refresh_token_reused_via_cli_adoption(tmp_path, monkeypatch):
    """Real-world scenario: Codex CLI refreshed in parallel after Hermes
    decided to refresh. Hermes' upstream call gets refresh_token_reused.
    Hermes must then re-check ~/.codex/auth.json — if it now holds the
    post-rotation tokens, adopt them rather than forcing re-login."""
    from hermes_cli.auth import _refresh_codex_auth_tokens

    hermes_home = tmp_path / "hermes"
    codex_home = tmp_path / "codex-cli"
    _setup_hermes_auth(hermes_home)
    monkeypatch.setenv("HERMES_HOME", str(hermes_home))
    monkeypatch.setenv("CODEX_HOME", str(codex_home))

    # Initially: no CLI tokens (so first adoption check returns None and
    # upstream refresh runs). After upstream fails with refresh_token_reused,
    # the CLI file appears with newer tokens — recovery should adopt.
    def _refresh_with_simulated_concurrent_cli(access_token, refresh_token, *, timeout_seconds):
        # Simulate: CLI rotated its token AFTER we already read ours but
        # BEFORE our upstream call landed. CLI now has the new pair.
        post_rotation_jwt = _jwt_with_exp(int(time.time()) + 3600)
        _write_codex_cli_auth(
            codex_home,
            access_token=post_rotation_jwt,
            refresh_token="cli-rt-AFTER-ROTATION",
            last_refresh="2099-06-15T12:00:00Z",
        )
        raise AuthError(
            "Codex refresh token was already consumed",
            provider="openai-codex",
            code="refresh_token_reused",
            relogin_required=True,
        )

    monkeypatch.setattr("hermes_cli.auth.refresh_codex_oauth_pure", _refresh_with_simulated_concurrent_cli)

    result = _refresh_codex_auth_tokens(
        {"access_token": "hermes-stale-at", "refresh_token": "hermes-stale-rt"},
        timeout_seconds=5.0,
        current_last_refresh="2026-02-26T00:00:00Z",
    )

    assert result["refresh_token"] == "cli-rt-AFTER-ROTATION"
    persisted = _read_codex_tokens()
    assert persisted["tokens"]["refresh_token"] == "cli-rt-AFTER-ROTATION"
    assert persisted["last_refresh"] == "2099-06-15T12:00:00Z"


def test_refresh_token_reused_without_cli_fresh_tokens_still_raises(tmp_path, monkeypatch):
    """If refresh_token_reused fires AND no fresher CLI tokens are available,
    the original AuthError must propagate so the operator is asked to re-login."""
    from hermes_cli.auth import _refresh_codex_auth_tokens

    hermes_home = tmp_path / "hermes"
    codex_home = tmp_path / "codex-cli"
    _setup_hermes_auth(hermes_home)
    monkeypatch.setenv("HERMES_HOME", str(hermes_home))
    monkeypatch.setenv("CODEX_HOME", str(codex_home))  # empty dir, no CLI file

    def _refresh_fails(access_token, refresh_token, *, timeout_seconds):
        raise AuthError(
            "Codex refresh token was already consumed",
            provider="openai-codex",
            code="refresh_token_reused",
            relogin_required=True,
        )

    monkeypatch.setattr("hermes_cli.auth.refresh_codex_oauth_pure", _refresh_fails)

    with pytest.raises(AuthError) as exc:
        _refresh_codex_auth_tokens(
            {"access_token": "hermes-stale-at", "refresh_token": "hermes-stale-rt"},
            timeout_seconds=5.0,
            current_last_refresh="2026-02-26T00:00:00Z",
        )
    assert exc.value.code == "refresh_token_reused"
