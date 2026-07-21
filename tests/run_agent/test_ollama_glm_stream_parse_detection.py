"""Characterization tests for two AIAgent detection predicates in run_agent.py.

* ``_is_ollama_glm_backend`` — decides whether a model is an Ollama-hosted GLM
  whose ``finish_reason='stop'`` can be a truncation misreport. Getting this
  wrong either drops a genuinely truncated reply (silent work loss, BR 5) or —
  the historical #13971 regression — fires spurious truncation continuations on
  arbitrary local proxies (LiteLLM/sglang/vLLM/LM Studio) that report stop
  correctly.

* ``_is_provider_stream_parse_error`` — decides whether a ``ValueError`` from a
  provider stream is wire-format trouble (→ retry) vs local request validation
  (→ surface). A false negative skips the retry and the agent gets a None
  response; a false positive retries a genuinely bad request.

Both predicates are pure functions of a few attributes, so we drive the real
(unbound) methods with a lightweight stub — no logic is mocked.
"""
from __future__ import annotations

import json
from types import SimpleNamespace

from run_agent import AIAgent


def _stub(*, model="glm-4", provider="ollama", base_url="http://localhost:11434/v1",
          api_mode="chat_completions"):
    return SimpleNamespace(
        model=model,
        provider=provider,
        _base_url_lower=(base_url or "").lower(),
        api_mode=api_mode,
    )


# ─── _is_ollama_glm_backend ──────────────────────────────────────────────────


def test_glm_on_default_ollama_port_is_detected():
    assert AIAgent._is_ollama_glm_backend(
        _stub(model="glm-4", base_url="http://localhost:11434/v1", provider="")
    ) is True


def test_glm_on_ollama_named_host_is_detected():
    assert AIAgent._is_ollama_glm_backend(
        _stub(model="glm-4.7", base_url="http://ollama.local/v1", provider="")
    ) is True


def test_glm_with_explicit_ollama_provider_is_detected():
    # No ollama marker in the URL, but provider says ollama.
    assert AIAgent._is_ollama_glm_backend(
        _stub(model="glm-4", base_url="http://10.0.0.5/v1", provider="ollama")
    ) is True


def test_non_glm_model_on_ollama_is_not_flagged():
    # Only GLM models misreport stop; qwen on ollama reports correctly.
    assert AIAgent._is_ollama_glm_backend(
        _stub(model="qwen3:32b", base_url="http://localhost:11434/v1", provider="ollama")
    ) is False


def test_zai_cloud_provider_is_not_an_ollama_backend():
    # provider zai passes the GLM gate but the cloud endpoint is not Ollama.
    assert AIAgent._is_ollama_glm_backend(
        _stub(model="glm-4.6", base_url="https://api.z.ai/api/paas/v4", provider="zai")
    ) is False


def test_arbitrary_local_proxy_is_not_flagged():
    # The #13971 guard: a vLLM/LiteLLM/sglang proxy with no ollama signature and
    # a non-default port must NOT be treated as an Ollama GLM backend.
    assert AIAgent._is_ollama_glm_backend(
        _stub(model="glm-4", base_url="http://192.168.1.5:8000/v1", provider="openai")
    ) is False


def test_none_model_and_provider_are_safe():
    assert AIAgent._is_ollama_glm_backend(
        _stub(model=None, base_url="", provider=None)
    ) is False


def test_zai_provider_with_ollama_url_is_detected():
    # Passing the GLM/zai gate + an ollama URL marker → detected.
    assert AIAgent._is_ollama_glm_backend(
        _stub(model="something", base_url="http://ollama.local/v1", provider="zai")
    ) is True


# ─── _is_provider_stream_parse_error ─────────────────────────────────────────


def test_anthropic_stream_parse_value_error_is_detected():
    stub = _stub(api_mode="anthropic_messages")
    assert AIAgent._is_provider_stream_parse_error(
        stub, ValueError("Expected ident at line 1 column 149")
    ) is True


def test_detection_is_case_insensitive_and_stripped():
    stub = _stub(api_mode="anthropic_messages")
    assert AIAgent._is_provider_stream_parse_error(
        stub, ValueError("  expected ident at line 2 column 5  ")
    ) is True


def test_wrong_api_mode_is_not_detected():
    stub = _stub(api_mode="chat_completions")
    assert AIAgent._is_provider_stream_parse_error(
        stub, ValueError("Expected ident at line 1 column 149")
    ) is False


def test_non_value_error_is_not_detected():
    stub = _stub(api_mode="anthropic_messages")
    assert AIAgent._is_provider_stream_parse_error(
        stub, RuntimeError("Expected ident at line 1 column 149")
    ) is False


def test_json_decode_error_is_excluded_despite_marker():
    # JSONDecodeError IS a ValueError and its message carries the marker, but it
    # is explicitly excluded (it's a local body-parse issue, handled elsewhere).
    stub = _stub(api_mode="anthropic_messages")
    err = json.JSONDecodeError("Expected ident at line 1 column 149", "", 0)
    assert AIAgent._is_provider_stream_parse_error(stub, err) is False


def test_unicode_encode_error_is_excluded():
    stub = _stub(api_mode="anthropic_messages")
    err = UnicodeEncodeError("utf-8", "x", 0, 1, "reason")
    assert AIAgent._is_provider_stream_parse_error(stub, err) is False


def test_value_error_without_marker_is_not_detected():
    stub = _stub(api_mode="anthropic_messages")
    assert AIAgent._is_provider_stream_parse_error(
        stub, ValueError("Invalid request: missing required field")
    ) is False
