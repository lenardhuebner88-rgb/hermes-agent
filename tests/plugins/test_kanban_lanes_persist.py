"""Tests for POST /api/plugins/kanban/lanes/persist.

The endpoint writes per-profile config.yaml and mirrors the primary choice into
the active lane, preserving existing fallbacks.
"""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

import pytest
import yaml
from fastapi import FastAPI
from fastapi.testclient import TestClient

from hermes_cli import kanban_db as kb


def _load_plugin_router():
    """Dynamically load plugins/kanban/dashboard/plugin_api.py and return its router."""
    repo_root = Path(__file__).resolve().parents[2]
    plugin_file = repo_root / "plugins" / "kanban" / "dashboard" / "plugin_api.py"
    assert plugin_file.exists(), f"plugin file missing: {plugin_file}"

    spec = importlib.util.spec_from_file_location(
        "hermes_dashboard_plugin_kanban_persist_test", plugin_file,
    )
    assert spec is not None and spec.loader is not None
    mod = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = mod
    spec.loader.exec_module(mod)
    return mod


@pytest.fixture
def plugin_module():
    return _load_plugin_router()


@pytest.fixture
def kanban_home(tmp_path, monkeypatch):
    """Isolated HERMES_HOME with an empty kanban DB."""
    home = tmp_path / ".hermes"
    home.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(home))
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    monkeypatch.delenv("HERMES_KANBAN_HOME", raising=False)
    monkeypatch.delenv("HERMES_KANBAN_DB", raising=False)
    monkeypatch.delenv("HERMES_CLAUDE_CLI_PROFILES", raising=False)
    kb.init_db()
    return home


@pytest.fixture
def client(kanban_home, plugin_module, monkeypatch):
    # Clear lane profile/model caches so freshly created profile dirs are picked up.
    plugin_module._lane_profile_cache = None
    # Stabilize the model catalog for tests; real inventory may be empty in CI.
    monkeypatch.setattr(
        plugin_module,
        "_lane_model_catalog",
        lambda _profiles: [
            {
                "id": "gpt-5.5",
                "label": "GPT-5.5",
                "runtime": "hermes",
                "group": "OpenAI Codex",
                "provider": "openai-codex",
            },
            {
                "id": "claude-opus-4-8",
                "label": "Claude Opus 4.8",
                "runtime": "claude-cli",
                "group": "Claude (Max-Abo)",
                "provider": None,
            },
        ],
    )
    app = FastAPI()
    app.include_router(plugin_module.router, prefix="/api/plugins/kanban")
    return TestClient(app)


def _write_profile_config(kanban_home: Path, name: str, text: str) -> Path:
    profile_dir = kanban_home / "profiles" / name
    profile_dir.mkdir(parents=True, exist_ok=True)
    config_path = profile_dir / "config.yaml"
    config_path.write_text(text, encoding="utf-8")
    return config_path


def test_lane_model_catalog_marks_cloud_max_models_selectable(plugin_module, monkeypatch):
    monkeypatch.setattr(plugin_module, "_append_openrouter_extra_model_options", lambda _out, _seen: None)

    models = plugin_module._lane_model_catalog([])

    by_id = {row["id"]: row for row in models if row.get("runtime") == "claude-cli"}
    assert by_id["claude-opus-4-8"]["provider"] is None
    assert by_id["claude-opus-4-8"]["group"] == "Claude (Max-Abo)"
    assert by_id["claude-opus-4-8"]["locked"] is False
    assert by_id["claude-sonnet-4-6"]["locked"] is False
    assert by_id["claude-fable-5"]["locked"] is False

def test_persist_hermes_branch_writes_model_default_and_provider(kanban_home, client):
    _write_profile_config(
        kanban_home,
        "coder",
        "model:\n  provider: openrouter\n  default: qwen/qwen3.7-max\n",
    )

    response = client.post(
        "/api/plugins/kanban/lanes/persist",
        json={"profiles": {"coder": {"worker_runtime": "hermes", "provider": "openai-codex", "model": "gpt-5.5"}}},
    )

    assert response.status_code == 200, response.text
    data = response.json()
    assert data["written"] == ["coder"]
    assert data["failed"] == []

    cfg_path = kanban_home / "profiles" / "coder" / "config.yaml"
    cfg = yaml.safe_load(cfg_path.read_text(encoding="utf-8"))
    assert cfg["model"]["default"] == "gpt-5.5"
    assert cfg["model"]["provider"] == "openai-codex"
    assert cfg["worker_runtime"] == "hermes"

    active = next(l for l in data["lanes"] if l["active"])
    assert active["profiles"]["coder"]["worker_runtime"] == "hermes"
    assert active["profiles"]["coder"]["provider"] == "openai-codex"
    assert active["profiles"]["coder"]["model"] == "gpt-5.5"


def test_persist_claude_cli_branch_writes_claude_model_and_runtime(kanban_home, client):
    _write_profile_config(
        kanban_home,
        "premium",
        "worker_runtime: claude-cli\nclaude_model: claude-fable-5\n",
    )

    response = client.post(
        "/api/plugins/kanban/lanes/persist",
        json={"profiles": {"premium": {"worker_runtime": "claude-cli", "provider": None, "model": "claude-opus-4-8"}}},
    )

    assert response.status_code == 200, response.text
    data = response.json()
    assert data["written"] == ["premium"]

    cfg_path = kanban_home / "profiles" / "premium" / "config.yaml"
    cfg = yaml.safe_load(cfg_path.read_text(encoding="utf-8"))
    assert cfg["claude_model"] == "claude-opus-4-8"
    assert cfg["worker_runtime"] == "claude-cli"

    active = next(l for l in data["lanes"] if l["active"])
    assert active["profiles"]["premium"]["worker_runtime"] == "claude-cli"
    assert active["profiles"]["premium"]["model"] == "claude-opus-4-8"


def test_persist_can_switch_coder_to_claude_max_runtime(kanban_home, client):
    _write_profile_config(
        kanban_home,
        "coder",
        "model:\n  provider: neuralwatt\n  default: glm-5.2-fast\n",
    )

    response = client.post(
        "/api/plugins/kanban/lanes/persist",
        json={"profiles": {"coder": {"worker_runtime": "claude-cli", "provider": None, "model": "claude-opus-4-8"}}},
    )

    assert response.status_code == 200, response.text
    data = response.json()
    assert data["written"] == ["coder"]
    assert data["failed"] == []

    cfg_path = kanban_home / "profiles" / "coder" / "config.yaml"
    cfg = yaml.safe_load(cfg_path.read_text(encoding="utf-8"))
    assert cfg["worker_runtime"] == "claude-cli"
    assert cfg["claude_model"] == "claude-opus-4-8"

    active = next(l for l in data["lanes"] if l["active"])
    assert active["profiles"]["coder"] == {
        "worker_runtime": "claude-cli",
        "provider": None,
        "model": "claude-opus-4-8",
        "fallback_providers": [],
    }


def test_lane_model_catalog_does_not_alias_cloud_max_to_hermes_profile_default(plugin_module, monkeypatch):
    monkeypatch.setattr(plugin_module, "_append_openrouter_extra_model_options", lambda _out, _seen: None)

    models = plugin_module._lane_model_catalog([
        {
            "name": "coder",
            "worker_runtime": "hermes",
            "default_model": "claude-opus-4-8",
            "default_provider": "openrouter",
        },
    ])

    assert not any(
        row.get("id") == "claude-opus-4-8"
        and row.get("runtime") == "hermes"
        and row.get("provider") == "openrouter"
        for row in models
    )


def test_lane_model_catalog_filters_cloud_max_from_openrouter_extras(plugin_module, monkeypatch):
    def fake_openrouter_extras(out, seen):
        plugin_module._append_lane_model_option(
            out,
            seen,
            model="claude-opus-4-8",
            runtime="hermes",
            group="OpenRouter",
            provider="openrouter",
        )
        plugin_module._append_lane_model_option(
            out,
            seen,
            model="qwen/qwen3.7-max",
            runtime="hermes",
            group="OpenRouter",
            provider="openrouter",
        )

    monkeypatch.setattr(plugin_module, "_append_openrouter_extra_model_options", fake_openrouter_extras)

    models = plugin_module._lane_model_catalog([])
    assert not any(row.get("id") == "claude-opus-4-8" and row.get("runtime") == "hermes" for row in models)
    assert any(row.get("id") == "qwen/qwen3.7-max" and row.get("provider") == "openrouter" for row in models)


def test_persist_rejects_cloud_max_model_on_hermes_provider(kanban_home, client):
    _write_profile_config(
        kanban_home,
        "coder",
        "model:\n  provider: openrouter\n  default: qwen/qwen3.7-max\n",
    )

    response = client.post(
        "/api/plugins/kanban/lanes/persist",
        json={"profiles": {"coder": {"worker_runtime": "hermes", "provider": "openrouter", "model": "claude-opus-4-8"}}},
    )

    assert response.status_code == 400
    assert response.json()["detail"]["error"] == "model runtime mismatch"
    assert response.json()["detail"]["models"] == [
        {
            "profile": "coder",
            "model": "claude-opus-4-8",
            "expected_runtime": "claude-cli",
            "worker_runtime": "hermes",
        },
    ]


def test_persist_runtime_switch_flips_worker_runtime(kanban_home, client):
    _write_profile_config(
        kanban_home,
        "coder",
        "worker_runtime: claude-cli\nclaude_model: claude-opus-4-8\n",
    )

    response = client.post(
        "/api/plugins/kanban/lanes/persist",
        json={"profiles": {"coder": {"worker_runtime": "hermes", "provider": "openai-codex", "model": "gpt-5.5"}}},
    )

    assert response.status_code == 200, response.text
    cfg = yaml.safe_load((kanban_home / "profiles" / "coder" / "config.yaml").read_text(encoding="utf-8"))
    assert cfg["worker_runtime"] == "hermes"
    assert cfg["model"]["default"] == "gpt-5.5"
    assert cfg["model"]["provider"] == "openai-codex"


def test_persist_preserves_existing_lane_fallbacks(kanban_home, client):
    _write_profile_config(
        kanban_home,
        "coder",
        "model:\n  provider: openrouter\n  default: qwen/qwen3.7-max\n",
    )
    with kb.connect() as conn:
        lane = kb.create_lane(
            conn,
            name="test-lane",
            profiles={
                "coder": {
                    "worker_runtime": "hermes",
                    "provider": "openrouter",
                    "model": "qwen/qwen3.7-max",
                    "fallback_providers": [{"provider": "openai-codex", "model": "gpt-5.5"}],
                },
            },
        )
        kb.activate_lane(conn, lane["id"])

    response = client.post(
        "/api/plugins/kanban/lanes/persist",
        json={"profiles": {"coder": {"worker_runtime": "hermes", "provider": "openai-codex", "model": "gpt-5.5"}}},
    )

    assert response.status_code == 200, response.text
    active = next(l for l in response.json()["lanes"] if l["active"])
    assert active["profiles"]["coder"]["model"] == "gpt-5.5"
    assert active["profiles"]["coder"]["provider"] == "openai-codex"
    assert active["profiles"]["coder"]["fallback_providers"] == [
        {"provider": "openai-codex", "model": "gpt-5.5"},
    ]


def test_persist_rejects_unknown_model(kanban_home, client):
    _write_profile_config(
        kanban_home,
        "coder",
        "model:\n  provider: openai-codex\n  default: gpt-5.5\n",
    )

    response = client.post(
        "/api/plugins/kanban/lanes/persist",
        json={"profiles": {"coder": {"worker_runtime": "hermes", "provider": "openai-codex", "model": "not-in-catalog-9"}}},
    )

    assert response.status_code == 400, response.text
    detail = response.json()["detail"]
    assert detail["error"] == "unknown models"


def test_persist_rejects_unknown_profile(kanban_home, client):
    response = client.post(
        "/api/plugins/kanban/lanes/persist",
        json={"profiles": {"does-not-exist": {"worker_runtime": "hermes", "provider": "openai-codex", "model": "gpt-5.5"}}},
    )

    assert response.status_code == 400, response.text
    detail = response.json()["detail"]
    assert detail["error"] == "unknown profiles"


def test_persist_hermes_without_provider_preserves_existing(kanban_home, client):
    # A profile the operator deliberately pinned to a provider earlier.
    _write_profile_config(
        kanban_home,
        "coder",
        "model:\n  provider: openrouter\n  default: qwen/qwen3.7-max\n",
    )
    with kb.connect() as conn:
        lane = kb.create_lane(
            conn,
            name="pinned-lane",
            profiles={
                "coder": {
                    "worker_runtime": "hermes",
                    "provider": "openrouter",
                    "model": "qwen/qwen3.7-max",
                },
            },
        )
        kb.activate_lane(conn, lane["id"])

    # Persist a new model WITHOUT a provider (e.g. a catalog pick that carries
    # no provider). The pinned provider must survive — not be clobbered to "".
    response = client.post(
        "/api/plugins/kanban/lanes/persist",
        json={"profiles": {"coder": {"worker_runtime": "hermes", "provider": None, "model": "gpt-5.5"}}},
    )

    assert response.status_code == 200, response.text
    assert response.json()["written"] == ["coder"]

    cfg = yaml.safe_load((kanban_home / "profiles" / "coder" / "config.yaml").read_text(encoding="utf-8"))
    assert cfg["model"]["default"] == "gpt-5.5"          # model updated
    assert cfg["model"]["provider"] == "openrouter"       # provider PRESERVED

    active = next(l for l in response.json()["lanes"] if l["active"])
    assert active["profiles"]["coder"]["model"] == "gpt-5.5"
    assert active["profiles"]["coder"]["provider"] == "openrouter"  # lane provider preserved


def test_persist_creates_missing_config_yaml(kanban_home, client):
    profile_dir = kanban_home / "profiles" / "coder"
    profile_dir.mkdir(parents=True, exist_ok=True)

    response = client.post(
        "/api/plugins/kanban/lanes/persist",
        json={"profiles": {"coder": {"worker_runtime": "hermes", "provider": "openai-codex", "model": "gpt-5.5"}}},
    )

    assert response.status_code == 200, response.text
    cfg_path = profile_dir / "config.yaml"
    assert cfg_path.exists()
    cfg = yaml.safe_load(cfg_path.read_text(encoding="utf-8"))
    assert cfg["model"]["default"] == "gpt-5.5"


def test_persist_succeeds_with_no_active_lane(kanban_home, client):
    """No active lane is a valid state (pure config-default routing). Persist must
    still write the profile config instead of returning 409 — otherwise the Lanes
    tab cannot steer providers whenever no lane override happens to be active.

    Regression: deleting the (formerly active) lane left the tab unable to save.
    """
    _write_profile_config(
        kanban_home,
        "coder",
        "model:\n  provider: openrouter\n  default: qwen/qwen3.7-max\n",
    )
    # Seed-on-first-contact, then turn every lane off → no active lane.
    with kb.connect() as conn:
        kb.list_lanes(conn)
        conn.execute("UPDATE lanes SET active = 0")
        conn.commit()

    response = client.post(
        "/api/plugins/kanban/lanes/persist",
        json={"profiles": {"coder": {"worker_runtime": "hermes", "provider": "openai-codex", "model": "gpt-5.5"}}},
    )

    assert response.status_code == 200, response.text
    data = response.json()
    assert data["written"] == ["coder"]
    assert data["failed"] == []
    assert data["active_id"] is None

    cfg = yaml.safe_load((kanban_home / "profiles" / "coder" / "config.yaml").read_text(encoding="utf-8"))
    assert cfg["model"]["default"] == "gpt-5.5"
    assert cfg["model"]["provider"] == "openai-codex"
    assert cfg["worker_runtime"] == "hermes"


def test_lane_model_catalog_reuses_last_good_inventory_on_failure(plugin_module, monkeypatch):
    """A transient inventory failure must not drop API models from the catalog.

    The dropdown and the /persist validator share ``_lane_model_catalog``; if a
    provider API blip empties the live inventory, a model that was valid moments
    ago would be 400-rejected. The last good inventory snapshot must be reused.
    """
    from hermes_cli import inventory

    monkeypatch.setattr(plugin_module, "_append_openrouter_extra_model_options", lambda _o, _s: None)
    plugin_module._LANE_INVENTORY_CACHE = []

    good_payload = {
        "providers": [
            {
                "slug": "neuralwatt",
                "authenticated": True,
                "configured": True,
                "models": ["kimi-k2.7-code", "qwen3.5-397b-fast"],
            },
        ],
    }
    monkeypatch.setattr(inventory, "load_picker_context", lambda *a, **k: object())
    monkeypatch.setattr(inventory, "build_models_payload", lambda *a, **k: good_payload)
    first = {row["id"] for row in plugin_module._lane_model_catalog([])}
    assert "kimi-k2.7-code" in first  # sanity: live inventory populates the catalog

    def boom(*a, **k):
        raise RuntimeError("provider API down")

    monkeypatch.setattr(inventory, "build_models_payload", boom)
    second = {row["id"] for row in plugin_module._lane_model_catalog([])}
    assert "kimi-k2.7-code" in second  # resilient: cached inventory reused
    assert "qwen3.5-397b-fast" in second
