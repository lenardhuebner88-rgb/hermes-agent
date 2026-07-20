"""Tests for tools/openrouter_client (key check + lazy shared-client caching).

resolve_provider_client is patched at its source module; get_async_client does
a lazy `from agent.auxiliary_client import resolve_provider_client` per call,
so it re-resolves the patched attribute. The module-global _client cache is
reset per test.
"""

from __future__ import annotations

import pytest

import tools.openrouter_client as oc


@pytest.fixture(autouse=True)
def _reset_cached_client(monkeypatch):
    # Ensure each test starts without a cached client and restore afterward.
    monkeypatch.setattr(oc, "_client", None)


class TestCheckApiKey:
    def test_present_key_returns_true(self, monkeypatch):
        monkeypatch.setenv("OPENROUTER_API_KEY", "sk-or-test")
        assert oc.check_api_key() is True

    def test_absent_key_returns_false(self, monkeypatch):
        monkeypatch.delenv("OPENROUTER_API_KEY", raising=False)
        assert oc.check_api_key() is False

    def test_empty_key_returns_false(self, monkeypatch):
        monkeypatch.setenv("OPENROUTER_API_KEY", "")
        assert oc.check_api_key() is False


class TestGetAsyncClient:
    def test_raises_when_no_client_can_be_resolved(self, monkeypatch):
        monkeypatch.setattr(
            "agent.auxiliary_client.resolve_provider_client",
            lambda *a, **k: (None, None),
        )
        with pytest.raises(ValueError, match="OPENROUTER_API_KEY"):
            oc.get_async_client()

    def test_returns_resolved_client_and_caches_it(self, monkeypatch):
        sentinel = object()
        calls = {"n": 0}

        def fake_resolver(*args, **kwargs):
            calls["n"] += 1
            return sentinel, "openrouter/some-model"

        monkeypatch.setattr(
            "agent.auxiliary_client.resolve_provider_client", fake_resolver
        )

        first = oc.get_async_client()
        second = oc.get_async_client()

        assert first is sentinel
        assert second is sentinel
        assert calls["n"] == 1, "client must be constructed once and cached"

    def test_return_annotation_is_async_openai(self):
        # Pins the type fix: the lazy client is annotated AsyncOpenAI.
        assert oc.get_async_client.__annotations__.get("return") == "AsyncOpenAI"
