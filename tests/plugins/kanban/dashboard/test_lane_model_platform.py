"""Backend contract tests for the Lanes model-platform slice."""

from __future__ import annotations

import importlib.util
import json
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest
import yaml
from fastapi import FastAPI
from fastapi.testclient import TestClient

from hermes_cli import kanban_db as kb


def _load_plugin_module():
    repo_root = Path(__file__).resolve().parents[4]
    plugin_file = repo_root / "plugins" / "kanban" / "dashboard" / "plugin_api.py"
    spec = importlib.util.spec_from_file_location(
        "hermes_dashboard_plugin_lane_model_platform_test",
        plugin_file,
    )
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


@pytest.fixture
def plugin_module():
    return _load_plugin_module()


@pytest.fixture
def kanban_home(tmp_path, monkeypatch):
    home = tmp_path / ".hermes"
    home.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(home))
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    monkeypatch.delenv("HERMES_KANBAN_HOME", raising=False)
    monkeypatch.delenv("HERMES_KANBAN_DB", raising=False)
    kb.init_db()
    return home


@pytest.fixture
def client(plugin_module, kanban_home):
    plugin_module._lane_profile_cache = None
    plugin_module._lane_model_probe_cache = {}
    plugin_module._lane_model_probe_cache_loaded = False
    app = FastAPI()
    app.include_router(plugin_module.router, prefix="/api/plugins/kanban")
    return TestClient(app)


@pytest.mark.parametrize(
    ("provider", "model", "expected"),
    [
        ("openai-codex", "gpt-5.6-sol", ["minimal", "low", "medium", "high"]),
        (None, "claude-fable-5", ["low", "medium", "high"]),
        ("moonshotai", "kimi-k2.6", ["low", "medium", "high"]),
        ("google", "gemini-3.1-pro", ["low", "medium", "high"]),
        ("openrouter", "qwen/qwen3.7-max", ["low", "medium", "high"]),
        ("xai", "grok-4", []),
        ("alibaba-token-plan", "qwen3.8-max-preview", []),
        ("neuralwatt", "glm-5.2-fast", []),
    ],
)
def test_reasoning_support_for_real_catalog_ids(
    plugin_module,
    provider,
    model,
    expected,
):
    assert plugin_module.reasoning_support_for(provider, model) == expected


def test_persist_reasoning_effort_writes_yaml_and_rejects_unsupported(
    plugin_module,
    kanban_home,
    client,
    monkeypatch,
):
    profile_dir = kanban_home / "profiles" / "coder"
    profile_dir.mkdir(parents=True)
    config_path = profile_dir / "config.yaml"
    config_path.write_text(
        "model:\n  provider: openai-codex\n  default: gpt-5.6-sol\n",
        encoding="utf-8",
    )

    from hermes_cli import profiles as profiles_mod

    monkeypatch.setattr(profiles_mod, "get_profile_dir", lambda _name: profile_dir)
    monkeypatch.setattr(
        plugin_module,
        "_lane_profile_catalog",
        lambda: [
            {
                "name": "coder",
                "worker_runtime": "hermes",
                "default_provider": "openai-codex",
                "default_model": "gpt-5.6-sol",
            },
        ],
    )
    monkeypatch.setattr(
        plugin_module,
        "_lane_model_catalog",
        lambda _profiles, _active=None: [
            {
                "id": "gpt-5.6-sol",
                "runtime": "hermes",
                "provider": "openai-codex",
            },
            {
                "id": "qwen3.8-max-preview",
                "runtime": "hermes",
                "provider": "alibaba-token-plan",
            },
        ],
    )

    ok = client.post(
        "/api/plugins/kanban/lanes/persist",
        json={
            "profiles": {
                "coder": {
                    "worker_runtime": "hermes",
                    "provider": "openai-codex",
                    "model": "gpt-5.6-sol",
                    "reasoning_effort": "high",
                },
            },
        },
    )
    assert ok.status_code == 200, ok.text
    config = yaml.safe_load(config_path.read_text(encoding="utf-8"))
    assert config["agent"]["reasoning_effort"] == "high"

    rejected = client.post(
        "/api/plugins/kanban/lanes/persist",
        json={
            "profiles": {
                "coder": {
                    "worker_runtime": "hermes",
                    "provider": "alibaba-token-plan",
                    "model": "qwen3.8-max-preview",
                    "reasoning_effort": "high",
                },
            },
        },
    )
    assert rejected.status_code == 400
    assert rejected.json()["detail"]["profiles"] == ["coder"]


def test_model_probe_status_cache_and_get_lanes_join(
    plugin_module,
    kanban_home,
    client,
    monkeypatch,
):
    profile_dir = kanban_home / "profiles" / "coder"
    profile_dir.mkdir(parents=True)
    (profile_dir / "config.yaml").write_text(
        "model:\n"
        "  provider: openai-codex\n"
        "  default: gpt-5.6-sol\n"
        "agent:\n"
        "  reasoning_effort: high\n",
        encoding="utf-8",
    )

    smoke_results = iter(
        [
            {
                "status": "ok",
                "duration_ms": 17,
                "observed_provider": "openai-codex",
                "observed_model": "gpt-5.6-sol",
                "error_class": None,
                "reason": "exact response",
            },
            {
                "status": "auth_error",
                "duration_ms": 23,
                "observed_provider": None,
                "observed_model": None,
                "error_class": "auth_error",
                "reason": "authentication failed",
            },
        ],
    )
    monkeypatch.setattr(
        plugin_module,
        "_run_single_lanes_auth_smoke",
        lambda _role, *, timeout_seconds: next(smoke_results),
    )

    ok = client.post(
        "/api/plugins/kanban/lanes/model-probe",
        json={
            "provider": "openai-codex",
            "model": "gpt-5.6-sol",
            "profile": "coder",
        },
    )
    assert ok.status_code == 200, ok.text
    assert ok.json()["status"] == "ok"
    assert ok.json()["duration_ms"] == 17

    failed = client.post(
        "/api/plugins/kanban/lanes/model-probe",
        json={
            "provider": "alibaba-token-plan",
            "model": "qwen3.8-max-preview",
            "profile": "coder",
        },
    )
    assert failed.status_code == 200, failed.text
    assert failed.json()["status"] == "auth_error"
    assert failed.json()["duration_ms"] == 23

    cache_path = kanban_home / "cache" / "lanes_model_probes.json"
    cached = json.loads(cache_path.read_text(encoding="utf-8"))
    assert {(row["provider"], row["model"]) for row in cached} == {
        ("openai-codex", "gpt-5.6-sol"),
        ("alibaba-token-plan", "qwen3.8-max-preview"),
    }

    from agent import models_dev
    from hermes_cli import inventory

    monkeypatch.setattr(inventory, "load_picker_context", lambda: object())
    monkeypatch.setattr(
        inventory,
        "build_models_payload",
        lambda *_args, **_kwargs: {
            "providers": [
                {
                    "slug": "openai-codex",
                    "name": "OpenAI Codex",
                    "authenticated": True,
                    "configured": True,
                    "models": ["gpt-5.6-sol"],
                },
            ],
        },
    )
    monkeypatch.setattr(
        models_dev,
        "get_model_capabilities",
        lambda _provider, _model: SimpleNamespace(context_window=400_000),
    )
    monkeypatch.setattr(
        models_dev,
        "get_model_info",
        lambda _provider, _model: SimpleNamespace(
            cost_input=1.25,
            cost_output=10.0,
            has_cost_data=lambda: True,
        ),
    )
    monkeypatch.setattr(
        plugin_module,
        "_append_openrouter_extra_model_options",
        lambda _out, _seen: None,
    )
    plugin_module._lane_profile_cache = None

    response = client.get("/api/plugins/kanban/lanes")
    assert response.status_code == 200, response.text
    data = response.json()
    coder = next(row for row in data["profiles"] if row["name"] == "coder")
    assert coder["reasoning_effort"] == "high"
    assert coder["reasoning_support"] == ["minimal", "low", "medium", "high"]

    model = next(
        row
        for row in data["models"]
        if row["provider"] == "openai-codex" and row["id"] == "gpt-5.6-sol"
    )
    assert model["authenticated"] is True
    assert model["configured"] is True
    assert model["price_in_per_mtok_usd"] == 1.25
    assert model["price_out_per_mtok_usd"] == 10.0
    assert model["context_window"] == 400_000
    assert model["reasoning_support"] == ["minimal", "low", "medium", "high"]
    assert model["probe"]["status"] == "ok"
    assert {
        "authenticated",
        "configured",
        "price_in_per_mtok_usd",
        "price_out_per_mtok_usd",
        "context_window",
        "reasoning_support",
        "probe",
    } <= model.keys()


def test_get_lanes_model_sinnvoll_rule(
    plugin_module,
    kanban_home,
    client,
    monkeypatch,
):
    profile_dir = kanban_home / "profiles" / "coder"
    profile_dir.mkdir(parents=True)
    (profile_dir / "config.yaml").write_text(
        "model:\n"
        "  provider: openai-codex\n"
        "  default: gpt-5.6-sol\n",
        encoding="utf-8",
    )
    (kanban_home / "config.yaml").write_text(
        "model_catalog:\n"
        "  providers:\n"
        "    neuralwatt:\n"
        "      extra_models:\n"
        "        - glm-5.2-fast\n",
        encoding="utf-8",
    )

    from agent import models_dev
    from hermes_cli import inventory

    monkeypatch.setattr(inventory, "load_picker_context", lambda: object())
    monkeypatch.setattr(
        inventory,
        "build_models_payload",
        lambda *_args, **_kwargs: {
            "providers": [
                {
                    "slug": "openai-codex",
                    "models": ["gpt-5.6-sol"],
                    "authenticated": True,
                    "configured": True,
                },
                {
                    "slug": "nous",
                    "models": ["hermes-4-405b"],
                    "authenticated": True,
                    "configured": False,
                },
                {
                    "slug": "neuralwatt",
                    "models": ["glm-5.2-fast"],
                    "authenticated": True,
                    "configured": True,
                },
                {
                    "slug": "openrouter",
                    "models": ["unadmitted/openrouter-model"],
                    "authenticated": True,
                    "configured": True,
                },
            ],
        },
    )
    monkeypatch.setattr(
        models_dev,
        "get_model_capabilities",
        lambda _provider, _model: None,
    )
    monkeypatch.setattr(
        models_dev,
        "get_model_info",
        lambda _provider, _model: None,
    )
    plugin_module._lane_profile_cache = None

    response = client.get("/api/plugins/kanban/lanes")
    assert response.status_code == 200, response.text
    models = response.json()["models"]

    def model(provider, model_id):
        return next(
            row
            for row in models
            if row["provider"] == provider and row["id"] == model_id
        )

    codex = model("openai-codex", "gpt-5.6-sol")
    assert codex["used_in_profiles"] is True
    assert codex["admitted"] is False
    assert codex["sinnvoll"] is True

    nous = model("nous", "hermes-4-405b")
    assert nous["used_in_profiles"] is False
    assert nous["admitted"] is False
    assert nous["sinnvoll"] is False

    neuralwatt = model("neuralwatt", "glm-5.2-fast")
    assert neuralwatt["used_in_profiles"] is False
    assert neuralwatt["admitted"] is True
    assert neuralwatt["sinnvoll"] is True

    claude = model(None, "claude-fable-5")
    assert claude["used_in_profiles"] is False
    assert claude["admitted"] is False
    assert claude["sinnvoll"] is True

    openrouter = model("openrouter", "unadmitted/openrouter-model")
    assert openrouter["used_in_profiles"] is False
    assert openrouter["admitted"] is False
    assert openrouter["sinnvoll"] is False


def test_catalog_probe_honors_limit_and_marks_truncated(
    plugin_module,
    client,
    monkeypatch,
):
    monkeypatch.setattr(
        plugin_module,
        "_run_single_lanes_auth_smoke",
        lambda role, *, timeout_seconds: {
            "status": "ok",
            "duration_ms": timeout_seconds,
            "observed_provider": role["provider"],
            "observed_model": role["model"],
            "error_class": None,
            "reason": "ok",
        },
    )
    response = client.post(
        "/api/plugins/kanban/lanes/catalog-probe",
        json={
            "models": [
                {"provider": "openai-codex", "model": "gpt-5.6-sol"},
                {
                    "provider": "alibaba-token-plan",
                    "model": "qwen3.8-max-preview",
                },
                {"provider": "claude-cli", "model": "claude-fable-5"},
            ],
            "limit": 2,
            "timeout_seconds": 11,
        },
    )
    assert response.status_code == 200, response.text
    assert len(response.json()["results"]) == 2
    assert response.json()["truncated"] is True
