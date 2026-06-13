"""GET /api/promptforge/catalog — read-only static catalog endpoint."""
from fastapi import FastAPI
from fastapi.testclient import TestClient

from hermes_cli import promptforge_view


def _client() -> TestClient:
    app = FastAPI()
    promptforge_view.register_promptforge_routes(app)
    return TestClient(app)


def test_catalog_returns_200_and_full_schema():
    resp = _client().get("/api/promptforge/catalog")
    assert resp.status_code == 200
    data = resp.json()
    assert data["version"] == 1
    for key in ("blocks", "taskTypes", "modes", "targets", "heuristic", "evalEvidence"):
        assert isinstance(data[key], list) and data[key], f"{key} must be a non-empty list"
    assert len(data["blocks"]) == 12
    assert len(data["taskTypes"]) == 5
    assert len(data["modes"]) == 3
    assert len(data["targets"]) == 4
    assert len(data["heuristic"]) == 10
    assert len(data["evalEvidence"]) == 4


def test_blocks_have_required_fields():
    data = _client().get("/api/promptforge/catalog").json()
    for block in data["blocks"]:
        for field in ("id", "letter", "label", "description", "body", "source", "category"):
            assert block.get(field), f"block {block.get('id')} missing {field}"
        assert block["category"] in ("core", "long-run", "optional")


def test_task_types_reference_known_blocks():
    data = _client().get("/api/promptforge/catalog").json()
    known = {b["id"] for b in data["blocks"]}
    for tt in data["taskTypes"]:
        for field in ("id", "label", "blockIds", "typeBody", "defaultDoneWhen", "checklist", "rawTemplate", "source"):
            assert tt.get(field), f"taskType {tt.get('id')} missing {field}"
        for bid in tt["blockIds"]:
            assert bid in known, f"taskType {tt['id']} references unknown block {bid}"
