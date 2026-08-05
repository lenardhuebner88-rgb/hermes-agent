"""Behavior tests for the read-only loop escalations inbox endpoint."""

from __future__ import annotations

import hashlib
from pathlib import Path

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from hermes_cli import control_loops


@pytest.fixture
def escalations_api(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    packs_dir = tmp_path / "packs"
    state_root = tmp_path / "state"
    for name in ("alpha-pack", "missing-pack"):
        (packs_dir / name).mkdir(parents=True)

    escalation_path = state_root / "alpha-pack" / "ESCALATIONS.md"
    escalation_path.parent.mkdir(parents=True)
    escalation_path.write_bytes(
        b"# Vorspann wird ignoriert\n\n"
        b"## E3 -- Bestehender Fund\n\n"
        b"**Schwere:** Hoch\n"
        b"Erste Detailzeile\n"
        b"---\n\n"
        b"## E4 -- Kaputtes UTF-8\n"
        b"Schwere: mittel\n"
        b"Defektes Byte: \xff\n"
    )

    monkeypatch.setattr(control_loops, "PACKS_DIR_OVERRIDE", packs_dir)
    monkeypatch.setattr(control_loops, "STATE_ROOT_OVERRIDE", state_root)

    app = FastAPI()
    control_loops.register_loops_routes(app)
    return TestClient(app), escalation_path


def test_escalations_endpoint_aggregates_parses_and_keeps_ids_stable(escalations_api):
    client, escalation_path = escalations_api

    response = client.get("/api/loops/escalations")

    assert response.status_code == 200
    payload = response.json()
    assert payload["total"] == 2
    assert payload["packs_with_escalations"] == 1
    assert [entry["title"] for entry in payload["entries"]] == [
        "E3 -- Bestehender Fund",
        "E4 -- Kaputtes UTF-8",
    ]
    assert payload["entries"][0] == {
        "id": hashlib.sha1(
            b"alpha-pack:E3 -- Bestehender Fund", usedforsecurity=False
        ).hexdigest()[:12],
        "pack": "alpha-pack",
        "source": "repo",
        "title": "E3 -- Bestehender Fund",
        "severity": "hoch",
        "body_lines": ["**Schwere:** Hoch", "Erste Detailzeile"],
        "line_start": 3,
    }
    assert payload["entries"][1]["severity"] == "mittel"
    assert payload["entries"][1]["line_start"] == 9
    assert payload["entries"][1]["body_lines"][-1] == "Defektes Byte: �"

    original_ids = [entry["id"] for entry in payload["entries"]]
    with escalation_path.open("ab") as handle:
        handle.write(b"\n---\n\n## E5 -- Spaeter angehaengt\nSchwere: niedrig\nNeu\n")

    appended_payload = client.get("/api/loops/escalations").json()
    assert appended_payload["total"] == 3
    assert [entry["id"] for entry in appended_payload["entries"][:2]] == original_ids


def test_escalations_endpoint_disambiguates_duplicate_titles(escalations_api):
    client, escalation_path = escalations_api
    escalation_path.write_text(
        "## Gleicher Titel\nErster Fund\n\n## Gleicher Titel\nZweiter Fund\n",
        encoding="utf-8",
    )

    entries = client.get("/api/loops/escalations").json()["entries"]

    assert len(entries) == 2
    assert entries[0]["title"] == entries[1]["title"]
    assert entries[0]["id"] != entries[1]["id"]
