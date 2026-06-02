"""Tests for the read-only cron observability endpoints + output helpers."""
from __future__ import annotations

from pathlib import Path
from types import ModuleType
from typing import Any, Dict, List, Optional

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

import cron.jobs as cron_jobs
import hermes_cli.cron_observability as cobs
from hermes_cli.cron_observability import redact_job, register_cron_observability_routes


# ---------------------------------------------------------------------------
# cron/jobs.py output helpers (real tmp OUTPUT_DIR, real path-escape guard)
# ---------------------------------------------------------------------------

@pytest.fixture
def output_dir(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    out = tmp_path / "output"
    out.mkdir()
    monkeypatch.setattr(cron_jobs, "OUTPUT_DIR", out)
    return out


def test_list_output_files_empty_when_no_dir(output_dir: Path) -> None:
    assert cron_jobs.list_output_files("job-1") == []


def test_list_output_files_newest_first(output_dir: Path) -> None:
    job_dir = output_dir / "job-1"
    job_dir.mkdir()
    (job_dir / "2026-06-01_08-00-00.md").write_text("old")
    (job_dir / "2026-06-02_08-00-00.md").write_text("new")
    (job_dir / "ignore.txt").write_text("nope")
    files = cron_jobs.list_output_files("job-1")
    assert [f["filename"] for f in files] == ["2026-06-02_08-00-00.md", "2026-06-01_08-00-00.md"]
    assert all("size_bytes" in f and "mtime" in f for f in files)


def test_read_output_file_latest(output_dir: Path) -> None:
    job_dir = output_dir / "job-1"
    job_dir.mkdir()
    (job_dir / "2026-06-01_08-00-00.md").write_text("alpha")
    (job_dir / "2026-06-02_08-00-00.md").write_text("bravo")
    result = cron_jobs.read_output_file("job-1")
    assert result is not None
    assert result["filename"] == "2026-06-02_08-00-00.md"
    assert result["text"] == "bravo"
    assert result["truncated"] is False


def test_read_output_file_clips(output_dir: Path) -> None:
    job_dir = output_dir / "job-1"
    job_dir.mkdir()
    (job_dir / "2026-06-02_08-00-00.md").write_text("x" * 100)
    result = cron_jobs.read_output_file("job-1", max_bytes=10)
    assert result is not None
    assert result["text"] == "x" * 10
    assert result["truncated"] is True


def test_read_output_file_none_when_empty(output_dir: Path) -> None:
    assert cron_jobs.read_output_file("job-1") is None


def test_output_helpers_reject_path_escape(output_dir: Path) -> None:
    for bad in ["../etc", "a/b", "..", ""]:
        with pytest.raises(ValueError):
            cron_jobs.list_output_files(bad)
        with pytest.raises(ValueError):
            cron_jobs.read_output_file(bad)


def test_read_output_file_rejects_bad_filename(output_dir: Path) -> None:
    job_dir = output_dir / "job-1"
    job_dir.mkdir()
    (job_dir / "2026-06-02_08-00-00.md").write_text("ok")
    for bad in ["../escape.md", "nested/x.md", "notmarkdown.txt"]:
        with pytest.raises(ValueError):
            cron_jobs.read_output_file("job-1", filename=bad)


# ---------------------------------------------------------------------------
# redaction
# ---------------------------------------------------------------------------

def test_redact_job_drops_secrets() -> None:
    job = {
        "id": "j1",
        "name": "Morgenbrief",
        "prompt": "SECRET INSTRUCTIONS",
        "script": "rm -rf /",
        "base_url": "http://internal",
        "last_status": "ok",
        "deliver": "discord:123",
    }
    out = redact_job(job)
    assert "prompt" not in out
    assert "script" not in out
    assert "base_url" not in out
    assert out["has_prompt"] is True
    assert out["has_script"] is True
    assert out["name"] == "Morgenbrief"
    assert out["last_status"] == "ok"


# ---------------------------------------------------------------------------
# endpoints (fake web_server / gateway via sys.modules)
# ---------------------------------------------------------------------------

class _FakeBackend:
    def __init__(self, jobs: Dict[str, List[Dict[str, Any]]], pids: List[int]):
        self._jobs = jobs
        self._pids = pids

    def cron_profile_dicts(self) -> List[Dict[str, Any]]:
        return [{"name": name} for name in self._jobs]

    def call_cron_for_profile(self, profile: Optional[str], func_name: str, *args: Any):
        if func_name == "list_jobs":
            return list(self._jobs.get(profile or "", []))
        if func_name == "list_output_files":
            return [{"filename": "2026-06-02_08-00-00.md", "mtime": 1, "size_bytes": 3}]
        if func_name == "read_output_file":
            job_id = str(args[0])
            if "/" in job_id or "\\" in job_id or job_id in {".", ".."} or job_id.startswith(".."):
                raise ValueError(f"Invalid cron job id for output path: {job_id!r}")
            return {"filename": "2026-06-02_08-00-00.md", "text": "OUTPUT BODY", "truncated": False, "mtime": 1, "size_bytes": 11}
        raise AssertionError(func_name)

    def find_cron_job_profile(self, job_id: str) -> Optional[str]:
        for name, jobs in self._jobs.items():
            if any(j.get("id") == job_id for j in jobs):
                return name
        return None

    def find_gateway_pids(self, *a: Any, **k: Any) -> List[int]:
        return list(self._pids)


def _install_fakes(monkeypatch: pytest.MonkeyPatch, backend: _FakeBackend) -> None:
    ws = ModuleType("hermes_cli.web_server")
    ws._call_cron_for_profile = backend.call_cron_for_profile
    ws._cron_profile_dicts = backend.cron_profile_dicts
    ws._find_cron_job_profile = backend.find_cron_job_profile
    gw = ModuleType("hermes_cli.gateway")
    gw.find_gateway_pids = backend.find_gateway_pids
    monkeypatch.setitem(__import__("sys").modules, "hermes_cli.web_server", ws)
    monkeypatch.setitem(__import__("sys").modules, "hermes_cli.gateway", gw)


def _client(backend: _FakeBackend, monkeypatch: pytest.MonkeyPatch) -> TestClient:
    _install_fakes(monkeypatch, backend)
    app = FastAPI()
    register_cron_observability_routes(app)
    return TestClient(app)


def test_observability_redacts_and_reports_gateway(monkeypatch: pytest.MonkeyPatch) -> None:
    backend = _FakeBackend(
        {"default": [{"id": "j1", "name": "Morgenbrief", "prompt": "SECRET", "script": "echo", "last_status": "ok"}]},
        pids=[4242],
    )
    client = _client(backend, monkeypatch)
    res = client.get("/api/cron/observability")
    assert res.status_code == 200
    body = res.json()
    assert body["gateway"]["running"] is True
    assert body["gateway"]["pids"] == [4242]
    assert len(body["jobs"]) == 1
    job = body["jobs"][0]
    assert "prompt" not in job and "script" not in job
    assert job["has_prompt"] is True and job["has_script"] is True
    assert job["latest_output"]["run_count"] == 1


def test_observability_gateway_down(monkeypatch: pytest.MonkeyPatch) -> None:
    backend = _FakeBackend({"default": []}, pids=[])
    client = _client(backend, monkeypatch)
    body = client.get("/api/cron/observability").json()
    assert body["gateway"]["running"] is False
    assert body["jobs"] == []


def test_observability_never_500(monkeypatch: pytest.MonkeyPatch) -> None:
    backend = _FakeBackend({"default": []}, pids=[1])

    def boom() -> List[Dict[str, Any]]:
        raise RuntimeError("profiles blew up")

    backend.cron_profile_dicts = boom  # type: ignore[assignment]
    client = _client(backend, monkeypatch)
    res = client.get("/api/cron/observability")
    assert res.status_code == 200
    body = res.json()
    assert body["jobs"] == []
    assert "error" in body


def test_output_endpoint_returns_text(monkeypatch: pytest.MonkeyPatch) -> None:
    backend = _FakeBackend({"default": [{"id": "j1", "name": "Morgenbrief"}]}, pids=[1])
    client = _client(backend, monkeypatch)
    res = client.get("/api/cron/observability/output/j1?profile=default")
    assert res.status_code == 200
    body = res.json()
    assert body["text"] == "OUTPUT BODY"
    assert body["filename"] == "2026-06-02_08-00-00.md"


def test_output_endpoint_rejects_path_escape(monkeypatch: pytest.MonkeyPatch) -> None:
    backend = _FakeBackend({"default": [{"id": "j1"}]}, pids=[1])
    client = _client(backend, monkeypatch)
    # Backslash stays a single path segment (unlike %2F, which decodes to "/" and
    # breaks routing), so it reaches the handler and hits the ValueError → 400 path.
    res = client.get("/api/cron/observability/output/..%5C..%5Cetc?profile=default")
    assert res.status_code == 400


def test_output_endpoint_unknown_job(monkeypatch: pytest.MonkeyPatch) -> None:
    backend = _FakeBackend({"default": [{"id": "j1"}]}, pids=[1])
    client = _client(backend, monkeypatch)
    res = client.get("/api/cron/observability/output/does-not-exist")
    assert res.status_code == 404
