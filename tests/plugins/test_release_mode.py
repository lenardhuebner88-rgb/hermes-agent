"""API test for GET/POST /release-mode (release.autonomous toggle endpoint).

Round-trip test: POST true → GET true → POST false → GET false, against a
real temporary config.yaml under a fake HERMES_HOME.

Uses the same bare-router TestClient harness as test_release_status.py.
"""
from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from hermes_cli import kanban_db as kb

PREFIX = "/api/plugins/kanban"


def _load_plugin_router():
    repo_root = Path(__file__).resolve().parents[2]
    plugin_file = repo_root / "plugins" / "kanban" / "dashboard" / "plugin_api.py"
    assert plugin_file.exists(), f"plugin file missing: {plugin_file}"
    spec = importlib.util.spec_from_file_location(
        "hermes_dashboard_plugin_release_mode_test", plugin_file,
    )
    assert spec is not None and spec.loader is not None
    mod = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = mod
    spec.loader.exec_module(mod)
    return mod.router


@pytest.fixture
def kanban_home(tmp_path, monkeypatch):
    home = tmp_path / ".hermes"
    home.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(home))
    monkeypatch.delenv("HERMES_VISION_METRICS_PATH", raising=False)
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    kb.init_db()
    return home


@pytest.fixture
def client(kanban_home):
    app = FastAPI()
    app.include_router(_load_plugin_router(), prefix=PREFIX)
    return TestClient(app)


def _write_config(home: Path, release_block: dict | None = None):
    """Write a minimal config.yaml with the given ``release:`` sub-map."""
    cfg = home / "config.yaml"
    lines = ["release:"]
    if release_block is None:
        lines.append("  autonomous: false")
    else:
        for k, v in release_block.items():
            lines.append(f"  {k}: {v}")
    cfg.write_text("\n".join(lines) + "\n")
    return cfg


def test_release_mode_round_trip(client, kanban_home):
    """POST true → GET true → POST false → GET false against a temp config."""
    _write_config(kanban_home, {"autonomous": False, "max_tier_autonomous": "review", "pause_on_red_streak": 0})

    # ── GET baseline: autonomous is false ──
    resp = client.get(f"{PREFIX}/release-mode")
    assert resp.status_code == 200
    body = resp.json()
    assert body["autonomous"] is False
    assert body["max_tier_autonomous"] == "review"
    assert body["pause_on_red_streak"] == 0

    # ── POST autonomous=true (backup → write → reload) ──
    resp = client.post(f"{PREFIX}/release-mode", json={"autonomous": True})
    assert resp.status_code == 200
    body = resp.json()
    assert body["ok"] is True
    assert body["autonomous"] is True
    assert body["max_tier_autonomous"] == "review"
    assert body["pause_on_red_streak"] == 0
    assert "backup" in body and body["backup"].endswith("config.yaml.bak")

    # The backup file must exist and contain the pre-flip content.
    backup_path = Path(body["backup"])
    assert backup_path.is_file(), "backup file must exist after POST"
    backup_text = backup_path.read_text().lower()
    assert "autonomous: false" in backup_text

    # The live config.yaml must now reflect autonomous: true.
    live_text = (kanban_home / "config.yaml").read_text().lower()
    assert "autonomous: true" in live_text

    # ── GET confirms the new persisted state ──
    resp = client.get(f"{PREFIX}/release-mode")
    assert resp.status_code == 200
    assert resp.json()["autonomous"] is True

    # ── POST autonomous=false flips it back ──
    resp = client.post(f"{PREFIX}/release-mode", json={"autonomous": False})
    assert resp.status_code == 200
    body = resp.json()
    assert body["ok"] is True
    assert body["autonomous"] is False

    # ── GET confirms the final persisted state ──
    resp = client.get(f"{PREFIX}/release-mode")
    assert resp.status_code == 200
    assert resp.json()["autonomous"] is False


def test_release_mode_preserves_sibling_keys(client, kanban_home):
    """Flipping ``release.autonomous`` must not clobber max_tier_autonomous."""
    _write_config(kanban_home, {"autonomous": False, "max_tier_autonomous": "critical", "pause_on_red_streak": 3})

    resp = client.post(f"{PREFIX}/release-mode", json={"autonomous": True})
    assert resp.status_code == 200
    body = resp.json()
    assert body["autonomous"] is True
    # Sibling policy knobs must survive the atomic YAML round-trip.
    assert body["max_tier_autonomous"] == "critical"
    assert body["pause_on_red_streak"] == 3
