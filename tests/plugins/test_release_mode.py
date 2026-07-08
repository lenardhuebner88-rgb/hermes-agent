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


def test_release_mode_max_tier_autonomous_round_trip(client, kanban_home):
    """POST max_tier_autonomous review->critical, autonomous untouched."""
    _write_config(kanban_home, {"autonomous": True, "max_tier_autonomous": "review", "pause_on_red_streak": 0})

    resp = client.post(f"{PREFIX}/release-mode", json={"max_tier_autonomous": "critical"})
    assert resp.status_code == 200
    body = resp.json()
    assert body["ok"] is True
    assert body["max_tier_autonomous"] == "critical"
    # A Reichweite-only POST must not clobber the sibling autonomous flag.
    assert body["autonomous"] is True

    resp = client.get(f"{PREFIX}/release-mode")
    assert resp.status_code == 200
    body = resp.json()
    assert body["max_tier_autonomous"] == "critical"
    assert body["autonomous"] is True


def test_release_mode_max_tier_autonomous_rejects_invalid_tier(client, kanban_home):
    _write_config(kanban_home, {"autonomous": True, "max_tier_autonomous": "review", "pause_on_red_streak": 0})

    resp = client.post(f"{PREFIX}/release-mode", json={"max_tier_autonomous": "high"})
    assert resp.status_code == 400

    # Rejected write must not touch the persisted config.
    resp = client.get(f"{PREFIX}/release-mode")
    assert resp.json()["max_tier_autonomous"] == "review"


def test_release_mode_requires_at_least_one_field(client, kanban_home):
    _write_config(kanban_home, {"autonomous": True, "max_tier_autonomous": "review", "pause_on_red_streak": 0})

    resp = client.post(f"{PREFIX}/release-mode", json={})
    assert resp.status_code == 400


def test_release_mode_exposes_red_streak_and_max_in_progress(client, kanban_home):
    """GET /release-mode also carries the safety-line inputs the old
    /release-status endpoint never exposed: red_streak (advisory, defaults
    to 0 with no gate-records file) and max_in_progress (kanban.max_in_progress,
    default 3 when unset)."""
    _write_config(kanban_home, {"autonomous": True, "max_tier_autonomous": "review", "pause_on_red_streak": 3})

    resp = client.get(f"{PREFIX}/release-mode")
    assert resp.status_code == 200
    body = resp.json()
    assert body["red_streak"] == 0
    assert body["max_in_progress"] == 3


def test_release_concurrency_round_trip(client, kanban_home):
    """POST /release-concurrency 3->5, GET /release-mode + response reflect it."""
    cfg = kanban_home / "config.yaml"
    cfg.write_text("release:\n  autonomous: true\nkanban:\n  max_in_progress: 3\n")

    resp = client.post(f"{PREFIX}/release-concurrency", json={"max_in_progress": 5})
    assert resp.status_code == 200
    body = resp.json()
    assert body["ok"] is True
    assert body["max_in_progress"] == 5
    assert "backup" in body and body["backup"].endswith("config.yaml.bak")

    backup_path = Path(body["backup"])
    assert backup_path.is_file()
    assert "max_in_progress: 3" in backup_path.read_text().lower()

    live_text = cfg.read_text().lower()
    assert "max_in_progress: 5" in live_text

    resp = client.get(f"{PREFIX}/release-mode")
    assert resp.status_code == 200
    assert resp.json()["max_in_progress"] == 5


def test_release_concurrency_rejects_below_one(client, kanban_home):
    cfg = kanban_home / "config.yaml"
    cfg.write_text("release:\n  autonomous: true\nkanban:\n  max_in_progress: 3\n")

    resp = client.post(f"{PREFIX}/release-concurrency", json={"max_in_progress": 0})
    assert resp.status_code == 400

    # Rejected write must not touch the persisted config.
    resp = client.get(f"{PREFIX}/release-mode")
    assert resp.json()["max_in_progress"] == 3


def test_release_mode_exposes_parallelism_lever_fields_with_defaults(client, kanban_home):
    """GET /release-mode carries the Risiko-Tab "Parallele Worker pro Profil"
    lever's read-side fields. max_in_progress_per_profile's real default is
    unlimited (None) — must NOT be faked as 1 when absent from config."""
    _write_config(kanban_home, {"autonomous": True, "max_tier_autonomous": "review", "pause_on_red_streak": 0})

    resp = client.get(f"{PREFIX}/release-mode")
    assert resp.status_code == 200
    body = resp.json()
    assert body["max_in_progress_per_profile"] is None
    assert body["max_concurrent_per_repo"] == 1
    assert body["serialize_by_repo"] is True


def test_release_mode_exposes_parallelism_lever_fields_when_set(client, kanban_home):
    cfg = kanban_home / "config.yaml"
    cfg.write_text(
        "release:\n  autonomous: true\n"
        "kanban:\n  max_in_progress: 5\n  max_in_progress_per_profile: 2\n"
        "  max_concurrent_per_repo: 2\n  serialize_by_repo: true\n"
    )

    resp = client.get(f"{PREFIX}/release-mode")
    assert resp.status_code == 200
    body = resp.json()
    assert body["max_in_progress"] == 5
    assert body["max_in_progress_per_profile"] == 2
    assert body["max_concurrent_per_repo"] == 2
    assert body["serialize_by_repo"] is True


def test_release_concurrency_coupled_lever_sets_per_profile_and_per_repo(client, kanban_home):
    """The coupled Risiko-Tab stepper POSTs both fields with the same N in
    one request; max_in_progress must stay untouched."""
    cfg = kanban_home / "config.yaml"
    cfg.write_text(
        "release:\n  autonomous: true\n"
        "kanban:\n  max_in_progress: 3\n  max_in_progress_per_profile: 1\n"
        "  max_concurrent_per_repo: 1\n"
    )

    resp = client.post(
        f"{PREFIX}/release-concurrency",
        json={"max_in_progress_per_profile": 2, "max_concurrent_per_repo": 2},
    )
    assert resp.status_code == 200
    body = resp.json()
    assert body["ok"] is True
    assert body["max_in_progress_per_profile"] == 2
    assert body["max_concurrent_per_repo"] == 2
    # The global cap was never in this POST's body — must be unchanged.
    assert body["max_in_progress"] == 3

    live_text = cfg.read_text().lower()
    assert "max_in_progress_per_profile: 2" in live_text
    assert "max_concurrent_per_repo: 2" in live_text
    assert "max_in_progress: 3" in live_text

    resp = client.get(f"{PREFIX}/release-mode")
    body = resp.json()
    assert body["max_in_progress_per_profile"] == 2
    assert body["max_concurrent_per_repo"] == 2
    assert body["max_in_progress"] == 3


def test_release_concurrency_rejects_per_profile_below_one(client, kanban_home):
    cfg = kanban_home / "config.yaml"
    cfg.write_text(
        "release:\n  autonomous: true\n"
        "kanban:\n  max_in_progress_per_profile: 1\n  max_concurrent_per_repo: 1\n"
    )

    resp = client.post(f"{PREFIX}/release-concurrency", json={"max_in_progress_per_profile": 0})
    assert resp.status_code == 400

    resp = client.post(f"{PREFIX}/release-concurrency", json={"max_concurrent_per_repo": 0})
    assert resp.status_code == 400

    # Neither rejected write may touch the persisted config.
    resp = client.get(f"{PREFIX}/release-mode")
    body = resp.json()
    assert body["max_in_progress_per_profile"] == 1
    assert body["max_concurrent_per_repo"] == 1


def test_release_concurrency_empty_body_rejected(client, kanban_home):
    _write_config(kanban_home, {"autonomous": True, "max_tier_autonomous": "review", "pause_on_red_streak": 0})

    resp = client.post(f"{PREFIX}/release-concurrency", json={})
    assert resp.status_code == 400
