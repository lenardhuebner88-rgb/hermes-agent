"""Offline tests for agent/ssl_guard CA-bundle validation.

Everything here runs without network access: ``ssl.create_default_context(cafile=…)``
only parses a local PEM file. We exercise the env-skip switch, the repair hint,
the error builder, ``_validate_bundle_path`` (missing / non-file / too-small /
garbage / valid) and the ``verify_ca_bundle`` orchestrator (skip / bad env var /
happy path against the real certifi bundle).
"""

from __future__ import annotations

import certifi
import pytest

from agent import ssl_guard
from agent.errors import SSLConfigurationError

_ALL_CA_ENV_VARS = (
    "HERMES_CA_BUNDLE",
    "SSL_CERT_FILE",
    "REQUESTS_CA_BUNDLE",
    "CURL_CA_BUNDLE",
    "HERMES_SKIP_SSL_GUARD",
)


@pytest.fixture
def clean_env(monkeypatch):
    """Remove every CA-bundle / skip env var so tests start from a known state."""
    for var in _ALL_CA_ENV_VARS:
        monkeypatch.delenv(var, raising=False)
    return monkeypatch


class TestRepairHintAndError:
    def test_repair_hint_mentions_force_reinstall(self):
        assert "pip install --force-reinstall certifi" in ssl_guard._repair_hint()

    def test_ssl_err_wraps_message_and_hint(self):
        err = ssl_guard._ssl_err("boom")
        assert isinstance(err, SSLConfigurationError)
        assert "boom" in str(err)
        assert "Repair:" in str(err)


class TestSkipSwitch:
    @pytest.mark.parametrize("val", ["1", "true", "YES", " on ", "On"])
    def test_truthy_values_enable_skip(self, monkeypatch, val):
        monkeypatch.setenv("HERMES_SKIP_SSL_GUARD", val)
        assert ssl_guard._skip_ssl_guard_enabled() is True

    @pytest.mark.parametrize("val", ["", "0", "no", "false", "off"])
    def test_falsy_values_do_not_skip(self, monkeypatch, val):
        monkeypatch.setenv("HERMES_SKIP_SSL_GUARD", val)
        assert ssl_guard._skip_ssl_guard_enabled() is False

    def test_unset_does_not_skip(self, monkeypatch):
        monkeypatch.delenv("HERMES_SKIP_SSL_GUARD", raising=False)
        assert ssl_guard._skip_ssl_guard_enabled() is False


class TestValidateBundlePath:
    def test_missing_path_raises(self, tmp_path):
        missing = tmp_path / "nope.pem"
        with pytest.raises(SSLConfigurationError, match="missing CA bundle"):
            ssl_guard._validate_bundle_path("TEST", str(missing))

    def test_directory_is_not_a_file(self, tmp_path):
        d = tmp_path / "adir"
        d.mkdir()
        with pytest.raises(SSLConfigurationError, match="does not point to a CA bundle file"):
            ssl_guard._validate_bundle_path("TEST", str(d))

    def test_too_small_when_substantial_required(self, tmp_path):
        tiny = tmp_path / "tiny.pem"
        tiny.write_text("x" * 10, encoding="utf-8")  # < 1024 bytes
        with pytest.raises(SSLConfigurationError, match="too small"):
            ssl_guard._validate_bundle_path("TEST", str(tiny), require_substantial=True)

    def test_garbage_file_cannot_be_loaded(self, tmp_path):
        garbage = tmp_path / "garbage.pem"
        # Large enough to pass the size check, but not a valid PEM bundle.
        garbage.write_text("not a certificate\n" * 200, encoding="utf-8")
        with pytest.raises(SSLConfigurationError, match="cannot be loaded|did not load"):
            ssl_guard._validate_bundle_path("TEST", str(garbage))

    def test_valid_certifi_bundle_passes(self):
        # The real certifi bundle must validate (also with the size requirement).
        ssl_guard._validate_bundle_path("certifi", certifi.where(), require_substantial=True)


class TestVerifyCaBundle:
    def test_skip_switch_short_circuits(self, monkeypatch):
        # Even with a broken env var set, the skip switch must return cleanly.
        monkeypatch.setenv("HERMES_SKIP_SSL_GUARD", "1")
        monkeypatch.setenv("SSL_CERT_FILE", "/definitely/missing.pem")
        assert ssl_guard.verify_ca_bundle() is None

    def test_bad_env_var_raises(self, clean_env):
        clean_env.setenv("SSL_CERT_FILE", "/definitely/missing.pem")
        with pytest.raises(SSLConfigurationError, match="missing CA bundle"):
            ssl_guard.verify_ca_bundle()

    def test_happy_path_with_real_certifi(self, clean_env):
        # All CA env vars unset -> only certifi's own bundle is validated.
        assert ssl_guard.verify_ca_bundle() is None

    def test_fallback_wrapper_delegates(self, monkeypatch):
        monkeypatch.setenv("HERMES_SKIP_SSL_GUARD", "1")
        assert ssl_guard.verify_ca_bundle_with_fallback() is None
