"""Tests for GET /api/operator/digest (operator_digest_view.py).

Covers the decisions probe against a fixture that mirrors the REAL current
shape of ~/.hermes/state/open-decisions.json (copied 2026-07-17 from the
live file — keys: id, title, action, source, opened_at, status), plus the
degraded-on-corrupt-file path. The systemd/nightgate probes are monkeypatched
directly so the tests don't depend on live host state.
"""
from __future__ import annotations

import json
from datetime import datetime

from fastapi import FastAPI
from fastapi.testclient import TestClient

from hermes_cli import operator_digest_view as odv

# Real shape harvested from /home/piet/.hermes/state/open-decisions.json
# (2026-07-17): 3 open entries, all opened at the same timestamp — used
# verbatim below except for opened_at, which each test freezes "now" against.
_REAL_FIXTURE = {
    "decisions": [
        {
            "id": "statedb-retention",
            "title": "state.db über Schwelle — Retention/Archiv alter Sessions",
            "action": "Preflight: `~/.hermes/scripts/state-db-retention-preflight.py --retention-days 14`, dann Retention ja/nein",
            "source": "cron-inventur",
            "opened_at": "2026-07-10T05:56:13+00:00",
            "status": "open",
        },
        {
            "id": "skill-promote-fate",
            "title": "Skill Promote Pipeline pausiert seit 06.07.",
            "action": "Reaktivieren (`hermes cron resume 836f32946e0a`) oder löschen (`hermes cron delete 836f32946e0a`)",
            "source": "cron-inventur",
            "opened_at": "2026-07-15T05:56:13+00:00",
            "status": "open",
        },
        {
            "id": "wm2026-jobs-fate",
            "title": "5 WM-2026-Jobs pausiert — Finale ~19.07.",
            "action": "Vor Finale reaktivieren (`hermes -p research cron resume <ids>`) oder nach Finale löschen",
            "source": "cron-inventur",
            "opened_at": "2026-07-16T05:56:13+00:00",
            "status": "open",
        },
    ]
}


def _quiet_client(monkeypatch, decisions_path):
    """A client with the decisions path swapped in and the two subprocess
    probes stubbed to "nothing found" so tests are hermetic against the host.
    """
    monkeypatch.setattr(odv, "_DECISIONS_PATH", decisions_path)
    monkeypatch.setattr(odv, "_systemd_failed_alerts", lambda: ([], []))
    monkeypatch.setattr(odv, "_nightgate_alert", lambda: ([], []))
    app = FastAPI()
    odv.register_operator_digest_routes(app)
    return TestClient(app)


def test_real_fixture_returns_three_decisions_ordered_by_age(tmp_path, monkeypatch):
    path = tmp_path / "open-decisions.json"
    path.write_text(json.dumps(_REAL_FIXTURE), encoding="utf-8")
    client = _quiet_client(monkeypatch, path)

    resp = client.get("/api/operator/digest")
    assert resp.status_code == 200
    body = resp.json()

    assert body["degraded"] == []
    ids = [d["id"] for d in body["decisions"]]
    assert ids == ["statedb-retention", "skill-promote-fate", "wm2026-jobs-fate"]

    ages = [d["age_days"] for d in body["decisions"]]
    assert ages == sorted(ages, reverse=True), "decisions must be sorted oldest-first (age desc)"
    for d, source in zip(body["decisions"], _REAL_FIXTURE["decisions"]):
        assert d["title"] == source["title"]
        assert d["action"] == source["action"]
        assert d["source"] == source["source"]
        assert d["age_days"] >= 0


def test_closed_decisions_are_excluded(tmp_path, monkeypatch):
    fixture = {
        "decisions": [
            {**_REAL_FIXTURE["decisions"][0], "status": "resolved"},
            _REAL_FIXTURE["decisions"][1],
        ]
    }
    path = tmp_path / "open-decisions.json"
    path.write_text(json.dumps(fixture), encoding="utf-8")
    client = _quiet_client(monkeypatch, path)

    body = client.get("/api/operator/digest").json()
    assert [d["id"] for d in body["decisions"]] == ["skill-promote-fate"]


def test_missing_file_degrades_gracefully(tmp_path, monkeypatch):
    path = tmp_path / "does-not-exist.json"
    client = _quiet_client(monkeypatch, path)

    body = client.get("/api/operator/digest").json()
    assert body["decisions"] == []
    assert "open-decisions" in body["degraded"]


def test_corrupt_file_degrades_gracefully(tmp_path, monkeypatch):
    path = tmp_path / "open-decisions.json"
    path.write_text("{not valid json", encoding="utf-8")
    client = _quiet_client(monkeypatch, path)

    body = client.get("/api/operator/digest").json()
    assert body["decisions"] == []
    assert "open-decisions" in body["degraded"]


def test_response_shape_and_no_crash_on_empty(tmp_path, monkeypatch):
    path = tmp_path / "open-decisions.json"
    path.write_text(json.dumps({"decisions": []}), encoding="utf-8")
    client = _quiet_client(monkeypatch, path)

    resp = client.get("/api/operator/digest")
    assert resp.status_code == 200
    body = resp.json()
    assert set(body.keys()) == {"generated_at", "decisions", "alerts", "degraded"}
    assert isinstance(body["generated_at"], int)
    assert body["decisions"] == []
    assert body["alerts"] == []
    assert body["degraded"] == []


def test_nightgate_red_alert_surfaces_gate_name(tmp_path, monkeypatch):
    path = tmp_path / "open-decisions.json"
    path.write_text(json.dumps({"decisions": []}), encoding="utf-8")
    monkeypatch.setattr(odv, "_DECISIONS_PATH", path)
    monkeypatch.setattr(odv, "_systemd_failed_alerts", lambda: ([], []))

    recent = datetime.now().strftime("%Y%m%d-%H%M%S")
    run_dir = tmp_path / "green-gate" / recent
    run_dir.mkdir(parents=True)
    (run_dir / "autoheal.log").write_text(
        json.dumps({"mode": "gate-fix", "triggered": True, "gate": "vitest", "red_files": ["a.test.ts"]}) + "\n",
        encoding="utf-8",
    )
    monkeypatch.setattr(odv, "_GREEN_GATE_ROOT", tmp_path / "green-gate")

    app = FastAPI()
    odv.register_operator_digest_routes(app)
    client = TestClient(app)

    body = client.get("/api/operator/digest").json()
    assert any(a["severity"] == "red" and "vitest" in a["detail"] for a in body["alerts"])
