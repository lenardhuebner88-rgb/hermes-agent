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
    # Reset the module-global resilience cache so mock rows from one endpoint
    # test cannot leak into a later GET /lanes test in the same process.
    plugin_module._LANE_INVENTORY_CACHE = []
    app = FastAPI()
    app.include_router(plugin_module.router, prefix="/api/plugins/kanban")
    return TestClient(app)


@pytest.mark.parametrize(
    ("provider", "model", "expected"),
    [
        ("openai-codex", "gpt-5.6-sol", ["minimal", "low", "medium", "high"]),
        (None, "claude-fable-5", ["low", "medium", "high"]),
        ("moonshotai", "kimi-k2.6", ["low", "medium", "high"]),
        # F-REASONING-K3: short kimi-family ids on the kimi/kimi-coding transport
        # must match their siblings, not fall through to [] via the "kimi"
        # substring check (k3 probed ok on kimi-coding — a real working model).
        ("kimi-coding", "k3", ["low", "medium", "high"]),
        ("kimi", "k3", ["low", "medium", "high"]),
        ("kimi-coding", "kimi-for-coding", ["low", "medium", "high"]),
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
    # Merge semantics: a reasoning write must NOT clobber the existing model
    # block (persist uses a dotted-path roundtrip, verified by review).
    assert config["model"]["provider"] == "openai-codex"
    assert config["model"]["default"] == "gpt-5.6-sol"

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


def test_persist_updates_one_lane_profile_and_removes_another(
    plugin_module,
    kanban_home,
    client,
    monkeypatch,
):
    from hermes_cli import profiles as profiles_mod

    profile_dirs = {}
    for name in ("coder", "research"):
        profile_dir = kanban_home / "profiles" / name
        profile_dir.mkdir(parents=True)
        (profile_dir / "config.yaml").write_text(
            "model:\n  provider: openai-codex\n  default: gpt-5.6-sol\n",
            encoding="utf-8",
        )
        profile_dirs[name] = profile_dir
    monkeypatch.setattr(
        profiles_mod,
        "get_profile_dir",
        lambda name: profile_dirs[name],
    )
    monkeypatch.setattr(
        plugin_module,
        "_lane_profile_catalog",
        lambda: [
            {
                "name": name,
                "worker_runtime": "hermes",
                "default_provider": "openai-codex",
                "default_model": "gpt-5.6-sol",
            }
            for name in ("coder", "research")
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
        ],
    )

    conn = kb.connect()
    try:
        lane = kb.create_lane(
            conn,
            name="remove-one",
            profiles={
                "coder": {
                    "worker_runtime": "hermes",
                    "provider": "openai-codex",
                    "model": "old-coder",
                },
                "research": {
                    "worker_runtime": "hermes",
                    "provider": "openrouter",
                    "model": "old-research",
                },
            },
        )
        kb.activate_lane(conn, lane["id"])
    finally:
        conn.close()

    response = client.post(
        "/api/plugins/kanban/lanes/persist",
        json={
            "profiles": {
                "coder": {
                    "worker_runtime": "hermes",
                    "provider": "openai-codex",
                    "model": "gpt-5.6-sol",
                },
            },
            "removed_profiles": ["research"],
        },
    )
    assert response.status_code == 200, response.text

    conn = kb.connect()
    try:
        active = kb.get_active_lane(conn)
        assert active is not None
        assert active["profiles"]["coder"]["model"] == "gpt-5.6-sol"
        assert "research" not in active["profiles"]
    finally:
        conn.close()


def test_persist_overlap_clears_reasoning_and_removes_lane_profile(
    plugin_module,
    kanban_home,
    client,
    monkeypatch,
):
    from hermes_cli import profiles as profiles_mod

    profile_dir = kanban_home / "profiles" / "coder"
    profile_dir.mkdir(parents=True)
    config_path = profile_dir / "config.yaml"
    config_path.write_text(
        "model:\n"
        "  provider: openai-codex\n"
        "  default: gpt-5.6-sol\n"
        "agent:\n"
        "  reasoning_effort: high\n",
        encoding="utf-8",
    )
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
        ],
    )

    conn = kb.connect()
    try:
        lane = kb.create_lane(
            conn,
            name="clear-overlap",
            profiles={
                "coder": {
                    "worker_runtime": "hermes",
                    "provider": "openai-codex",
                    "model": "gpt-5.6-sol",
                },
            },
        )
        kb.activate_lane(conn, lane["id"])
    finally:
        conn.close()

    response = client.post(
        "/api/plugins/kanban/lanes/persist",
        json={
            "profiles": {
                "coder": {
                    "worker_runtime": "hermes",
                    "provider": "openai-codex",
                    "model": "gpt-5.6-sol",
                    "reasoning_effort": "",
                },
            },
            "removed_profiles": ["coder"],
        },
    )
    assert response.status_code == 200, response.text
    config = yaml.safe_load(config_path.read_text(encoding="utf-8"))
    assert config["agent"]["reasoning_effort"] == ""

    conn = kb.connect()
    try:
        active = kb.get_active_lane(conn)
        assert active is not None
        assert "coder" not in active["profiles"]
    finally:
        conn.close()


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


def test_lane_offer_exclusion_and_image_detection(plugin_module, monkeypatch):
    """W1 + W2 classifier: codex -pro and image/video models drop out of the
    chat-lane offer; chat models and multimodal (text-out) models stay in."""
    from agent import models_dev

    # Capability path: a model models.dev knows is judged by its OUTPUT modalities.
    capability = {"out": {}}

    def fake_get_model_info(_provider, model):
        outs = capability["out"].get(model)
        if outs is None:
            return None  # unknown to models.dev → id-pattern fallback
        return SimpleNamespace(output_modalities=outs)

    monkeypatch.setattr(models_dev, "get_model_info", fake_get_model_info)

    # W1 — openai-codex -pro ids are not directly served (fallback-only).
    assert plugin_module._lane_offer_exclusion("openai-codex", "gpt-5.6-sol-pro") is not None
    assert plugin_module._lane_offer_exclusion("openai-codex", "gpt-5.6-terra-pro") is not None
    assert plugin_module._lane_offer_exclusion("openai-codex", "gpt-5.6-luna-pro") is not None
    # The non-pro primary stays offered.
    assert plugin_module._lane_offer_exclusion("openai-codex", "gpt-5.6-sol") is None
    # A -pro id on ANOTHER provider is not a codex-pro exclusion.
    assert plugin_module._lane_offer_exclusion("openrouter", "nex-agi/nex-n2-pro:free") is None

    # W2 — documented Alibaba image/video id-patterns (custom endpoint, no
    # models.dev entry → get_model_info returns None → pattern fallback).
    for image_id in ("qwen-image-2.0", "qwen-image-2.0-pro", "wan2.7-image", "wan2.7-image-pro"):
        assert plugin_module._lane_offer_exclusion("alibaba-token-plan", image_id) is not None
        assert plugin_module._lane_image_model("alibaba-token-plan", image_id) is True
    # A normal chat model on the same custom endpoint stays offered.
    assert plugin_module._lane_offer_exclusion("alibaba-token-plan", "qwen3.8-max-preview") is None

    # Capability-first detection: image-only output → generator; text output
    # (incl. multimodal text+image) → chat-capable, NOT excluded.
    capability["out"] = {
        "img-gen": ("image",),
        "text-chat": ("text",),
        "multimodal": ("text", "image"),
    }
    assert plugin_module._lane_image_model("someprovider", "img-gen") is True
    assert plugin_module._lane_image_model("someprovider", "text-chat") is False
    assert plugin_module._lane_image_model("someprovider", "multimodal") is False


def test_offer_excludes_codex_pro_but_keeps_them_renderable(
    plugin_module,
    kanban_home,
    client,
    monkeypatch,
):
    """W1: a codex -pro model is NOT sinnvoll/probe-able in the offer even though
    its provider is a profile default (used_in_profiles), but it STAYS in the
    catalog so an already-persisted override keeps rendering."""
    profile_dir = kanban_home / "profiles" / "coder"
    profile_dir.mkdir(parents=True)
    (profile_dir / "config.yaml").write_text(
        "model:\n  provider: openai-codex\n  default: gpt-5.6-sol\n",
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
                    # -pro and non-pro both come from the authenticated catalog.
                    "models": ["gpt-5.6-sol", "gpt-5.6-sol-pro", "gpt-5.6-terra-pro"],
                    "authenticated": True,
                    "configured": True,
                },
            ],
        },
    )
    monkeypatch.setattr(models_dev, "get_model_capabilities", lambda _p, _m: None)
    monkeypatch.setattr(models_dev, "get_model_info", lambda _p, _m: None)
    plugin_module._lane_profile_cache = None

    response = client.get("/api/plugins/kanban/lanes")
    assert response.status_code == 200, response.text
    models = response.json()["models"]

    def model(model_id):
        return next(row for row in models if row["id"] == model_id)

    # Non-pro primary: offered (sinnvoll), not excluded.
    primary = model("gpt-5.6-sol")
    assert primary["sinnvoll"] is True
    assert primary["offer_excluded"] is False

    # -pro ids: provider IS a profile default (used_in_profiles True) but the
    # exclusion overrides → not sinnvoll, flagged offer_excluded.
    for pro_id in ("gpt-5.6-sol-pro", "gpt-5.6-terra-pro"):
        pro = model(pro_id)
        assert pro["used_in_profiles"] is True  # would be sinnvoll without exclusion
        assert pro["sinnvoll"] is False
        assert pro["offer_excluded"] is True

    # The -pro model is still in the catalog (so a persisted override renders).
    assert {"gpt-5.6-sol-pro", "gpt-5.6-terra-pro"} <= {row["id"] for row in models}


def test_offer_excludes_image_models_and_probe_skips_them(
    plugin_module,
    kanban_home,
    client,
    monkeypatch,
):
    """W2: image/video models are excluded from the offer AND the chat-probe scope
    (model-probe returns 'skipped' without burning a call); a chat model on the
    same custom endpoint is unaffected."""
    profile_dir = kanban_home / "profiles" / "coder"
    profile_dir.mkdir(parents=True)
    (profile_dir / "config.yaml").write_text(
        "model:\n  provider: alibaba-token-plan\n  default: qwen3.8-max-preview\n",
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
                    "slug": "alibaba-token-plan",
                    "models": ["qwen3.8-max-preview", "qwen-image-2.0", "wan2.7-image"],
                    "authenticated": True,
                    "configured": True,
                },
            ],
        },
    )
    monkeypatch.setattr(models_dev, "get_model_capabilities", lambda _p, _m: None)
    # Custom endpoint → not in models.dev → id-pattern detection in the classifier.
    monkeypatch.setattr(models_dev, "get_model_info", lambda _p, _m: None)
    plugin_module._lane_profile_cache = None

    response = client.get("/api/plugins/kanban/lanes")
    assert response.status_code == 200, response.text
    models = response.json()["models"]

    def model(model_id):
        return next(row for row in models if row["id"] == model_id)

    assert model("qwen3.8-max-preview")["sinnvoll"] is True
    for image_id in ("qwen-image-2.0", "wan2.7-image"):
        img = model(image_id)
        assert img["sinnvoll"] is False
        assert img["offer_excluded"] is True
    # Still renderable for a persisted override.
    assert {"qwen-image-2.0", "wan2.7-image"} <= {row["id"] for row in models}

    # Probe scope: an explicit model-probe on an image model is skipped honestly
    # and must NOT invoke the (Abo-burning) auth-smoke runner.
    called: list[str] = []

    def fake_smoke(role, *, timeout_seconds):
        called.append(role["model"])
        return {"status": "ok", "duration_ms": 1, "observed_provider": role["provider"],
                "observed_model": role["model"], "error_class": None, "reason": "ok"}

    monkeypatch.setattr(plugin_module, "_run_single_lanes_auth_smoke", fake_smoke)
    probe = client.post(
        "/api/plugins/kanban/lanes/model-probe",
        json={"provider": "alibaba-token-plan", "model": "qwen-image-2.0", "profile": "coder"},
    )
    assert probe.status_code == 200, probe.text
    assert probe.json()["status"] == "skipped"
    assert called == []  # no real probe call was made for the image model

    # A chat model on the same provider still probes normally.
    chat_probe = client.post(
        "/api/plugins/kanban/lanes/model-probe",
        json={"provider": "alibaba-token-plan", "model": "qwen3.8-max-preview", "profile": "coder"},
    )
    assert chat_probe.status_code == 200, chat_probe.text
    assert chat_probe.json()["status"] == "ok"
    assert called == ["qwen3.8-max-preview"]


def test_claude_cli_rows_show_honest_no_reasoning_control(
    plugin_module,
    kanban_home,
    client,
    monkeypatch,
):
    """W3: claude-cli rows expose reasoning_support=[] (honest no-Knopf state)
    plus a pointer hint to `claude_effort`/`--effort` — NOT greyed segments that
    imply the hermes reasoning control (which claude-cli ignores) applies."""
    # A claude-cli profile so the profile-catalog path is exercised too.
    profile_dir = kanban_home / "profiles" / "maxprofile"
    profile_dir.mkdir(parents=True)
    (profile_dir / "config.yaml").write_text(
        "worker_runtime: claude-cli\nclaude_model: claude-fable-5\n",
        encoding="utf-8",
    )

    from agent import models_dev
    from hermes_cli import inventory

    monkeypatch.setattr(inventory, "load_picker_context", lambda: object())
    monkeypatch.setattr(
        inventory,
        "build_models_payload",
        lambda *_args, **_kwargs: {"providers": []},
    )
    monkeypatch.setattr(models_dev, "get_model_capabilities", lambda _p, _m: None)
    monkeypatch.setattr(models_dev, "get_model_info", lambda _p, _m: None)
    plugin_module._lane_profile_cache = None

    response = client.get("/api/plugins/kanban/lanes")
    assert response.status_code == 200, response.text
    data = response.json()

    # Every claude-cli MODEL row: empty support + an honest hint.
    claude_models = [row for row in data["models"] if row["runtime"] == "claude-cli"]
    assert claude_models, "expected the built-in claude-cli model rows"
    for row in claude_models:
        assert row["reasoning_support"] == []
        assert "claude_effort" in (row.get("reasoning_hint") or "")

    # The claude-cli PROFILE row carries the same honest state.
    profile = next(p for p in data["profiles"] if p["name"] == "maxprofile")
    assert profile["worker_runtime"] == "claude-cli"
    assert profile["reasoning_support"] == []
    assert "claude_effort" in (profile.get("reasoning_hint") or "")


def test_extra_models_for_all_providers_land_in_catalog(
    plugin_module,
    kanban_home,
    client,
    monkeypatch,
):
    """W4: extra_models land in the dropdown catalog for EVERY provider (real-data:
    alibaba-token-plan from config.yaml), the way openrouter already did —
    openrouter behavior stays byte-identical (group 'OpenRouter')."""
    (kanban_home / "config.yaml").write_text(
        "model_catalog:\n"
        "  providers:\n"
        "    openrouter:\n"
        "      extra_models:\n"
        "        - z-ai/glm-5.2\n"
        "    alibaba-token-plan:\n"
        "      extra_models:\n"
        "        - qwen3.8-max-preview\n"
        "        - kimi-k2.7-code\n",
        encoding="utf-8",
    )
    # No profile dir needed; the catalog comes from extra_models + the (empty)
    # inventory below.
    from agent import models_dev
    from hermes_cli import inventory

    monkeypatch.setattr(inventory, "load_picker_context", lambda: object())
    # Inventory does NOT carry the alibaba/openrouter extra models — the only way
    # they appear is via the extra_models injection (proves the additive path).
    monkeypatch.setattr(
        inventory,
        "build_models_payload",
        lambda *_args, **_kwargs: {"providers": []},
    )
    monkeypatch.setattr(models_dev, "get_model_capabilities", lambda _p, _m: None)
    monkeypatch.setattr(models_dev, "get_model_info", lambda _p, _m: None)
    plugin_module._lane_profile_cache = None

    # Direct accessor: every provider with extra_models is enumerated.
    from hermes_cli.model_catalog import get_all_configured_provider_extra_models

    extra = get_all_configured_provider_extra_models()
    assert extra.get("alibaba-token-plan") == ["qwen3.8-max-preview", "kimi-k2.7-code"]
    assert extra.get("openrouter") == ["z-ai/glm-5.2"]

    response = client.get("/api/plugins/kanban/lanes")
    assert response.status_code == 200, response.text
    models = response.json()["models"]

    def model(provider, model_id):
        return next(
            row for row in models
            if row["provider"] == provider and row["id"] == model_id
        )

    # alibaba-token-plan extra models now land in the catalog (source=config).
    for mid in ("qwen3.8-max-preview", "kimi-k2.7-code"):
        row = model("alibaba-token-plan", mid)
        assert row["source"] == "config"
        assert row["runtime"] == "hermes"

    # openrouter unchanged: still added, still grouped "OpenRouter".
    orow = model("openrouter", "z-ai/glm-5.2")
    assert orow["source"] == "config"
    assert orow["group"] == "OpenRouter"


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
