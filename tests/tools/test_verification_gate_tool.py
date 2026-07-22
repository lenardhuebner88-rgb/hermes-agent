from __future__ import annotations

import json
import os
import stat
import subprocess
from dataclasses import replace
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from hermes_cli.gate_evidence import GateEvidence, GateEvidenceStore, build_gate_fingerprint


def git(cwd, *args):
    return subprocess.check_output(["git", *args], cwd=cwd, text=True).strip()


@pytest.fixture
def repo(tmp_path):
    root = tmp_path / "repo"
    root.mkdir()
    git(root, "init", "-q")
    git(root, "config", "user.email", "tests@example.invalid")
    git(root, "config", "user.name", "Tests")
    (root / "pyproject.toml").write_text("[tool.pytest.ini_options]\n")
    (root / "uv.lock").write_text("version = 1\n")
    git(root, "add", ".")
    git(root, "commit", "-qm", "initial")
    return root


def test_fingerprint_is_stable_complete_and_path_secret_free(repo):
    kwargs = dict(gate_id="backend_targets", gate_version="7", config_paths=["pyproject.toml"],
                  lockfile_paths=["uv.lock"], allowed_env=("CI",),
                  runtime={"python": "3.test", "tool": "v1"})
    first = build_gate_fingerprint(repo, test_selection=["tests/b.py", "tests/a.py", "tests/a.py"],
                                   env={"CI": "1", "TOKEN": "raw-secret", "HOME": str(repo)}, **kwargs)
    second = build_gate_fingerprint(repo, test_selection=["tests/a.py", "tests/b.py"],
                                    env={"CI": "1", "TOKEN": "different", "HOME": "/other"}, **kwargs)
    payload = json.dumps(first.payload, sort_keys=True)
    assert first.digest == second.digest
    assert first.payload["tests"] == ["tests/a.py", "tests/b.py"]
    assert first.payload["tree_sha"] == git(repo, "rev-parse", "HEAD^{tree}")
    assert first.payload["head_sha"] == git(repo, "rev-parse", "HEAD")
    assert "raw-secret" not in payload and str(repo) not in payload
    assert first.payload["env"] == {"CI": "1"}


def test_fingerprint_changes_for_lock_runtime_gate_and_history(repo):
    def fp(**updates):
        data = dict(gate_id="affected", gate_version="1", test_selection=[],
                    config_paths=["pyproject.toml"], lockfile_paths=["uv.lock"],
                    runtime={"python": "3.13"})
        data.update(updates)
        return build_gate_fingerprint(repo, **data).digest
    baseline = fp()
    assert fp(gate_version="2") != baseline
    assert fp(runtime={"python": "3.14"}) != baseline
    (repo / "uv.lock").write_text("version = 2\n")
    assert fp() != baseline
    (repo / "uv.lock").write_text("version = 1\n")
    git(repo, "commit", "--allow-empty", "-qm", "new history")
    assert fp() != baseline


def evidence(fingerprint="f" * 64, status="passed", age_hours=0):
    when = datetime.now(timezone.utc) - timedelta(hours=age_hours)
    return GateEvidence(fingerprint=fingerprint, gate_id="affected", gate_version="1",
                        phase="review", status=status, started_at=when.isoformat(),
                        finished_at=when.isoformat(), duration_seconds=0.0,
                        results=[{"command_id": "run_affected", "exit_code": 0}], head_sha="h")


def test_record_only_default_and_strict_reuse_policy(tmp_path):
    store = GateEvidenceStore(tmp_path / "artifacts")
    store.write(evidence())
    assert store.find_reusable("f" * 64, phase="review", reuse_enabled=False) is None
    assert store.find_reusable("f" * 64, phase="review", reuse_enabled=True) is not None
    assert store.find_reusable("f" * 64, phase="post_merge", reuse_enabled=True) is None
    store.write(evidence("e" * 64, age_hours=25))
    store.write(evidence("d" * 64, status="failed"))
    assert store.find_reusable("e" * 64, phase="review", reuse_enabled=True) is None
    assert store.find_reusable("d" * 64, phase="review", reuse_enabled=True) is None


def test_store_writes_raw_free_0600_json(tmp_path):
    receipt = GateEvidenceStore(tmp_path / "terminal-run" / "artifacts").write(evidence(status="failed"))
    data = receipt.path.read_text()
    assert stat.S_IMODE(os.stat(receipt.path).st_mode) == 0o600
    assert not list(receipt.path.parent.glob("*.tmp"))
    assert "stdout" not in data and "stderr" not in data
    assert receipt.digest == __import__("hashlib").sha256(receipt.path.read_bytes()).hexdigest()


def test_store_persists_only_named_ui_artifacts(tmp_path):
    item = replace(evidence(), artifacts=["agent-terminals-1280x900.png"])
    receipt = GateEvidenceStore(tmp_path / "artifacts").write(item)
    payload = json.loads(receipt.path.read_text())
    assert payload["artifacts"] == ["agent-terminals-1280x900.png"]
    assert str(tmp_path) not in receipt.path.read_text()


def test_actions_are_closed_and_affected_has_separate_exit_codes(repo, monkeypatch):
    from tools import verification_gate_tool as tool
    calls = []
    def fake_run(specs, root):
        calls.extend(specs)
        return [
            {"command_id": "run_affected", "exit_code": 2, "timed_out": False, "duration_seconds": 0.1},
            {"command_id": "worker_gate_ruff", "exit_code": 3, "timed_out": False, "duration_seconds": 0.1},
        ]
    monkeypatch.setattr(tool, "_run_commands", fake_run)
    result = tool.run_verification_gate(action="affected", workspace=repo,
                                        artifact_dir=repo / ".evidence", phase="review")
    assert [x["exit_code"] for x in result["results"]] == [2, 3]
    assert [x["command_id"] for x in result["results"]] == ["run_affected", "worker_gate_ruff"]
    assert "secret" not in json.dumps(result)
    assert len(calls) == 2
    with pytest.raises(ValueError, match="unknown verification gate action"):
        tool.run_verification_gate(action="shell", workspace=repo, artifact_dir=repo / ".evidence")


@pytest.mark.parametrize("ui_status", ["ui_preview_busy", "skipped"])
def test_ui_shot_incomplete_states_are_red(repo, monkeypatch, ui_status):
    from tools import verification_gate_tool as tool
    monkeypatch.setattr(tool, "_run_ui_shot", lambda *a, **k: {
        "status": ui_status, "results": [], "artifacts": [],
    })
    result = tool.run_verification_gate(action="ui_shot", workspace=repo,
                                        artifact_dir=repo / ".evidence",
                                        route="agent-terminals", scenario="terminal_bridge")
    assert result["status"] == "failed"


def test_registry_schema_exposes_only_closed_actions():
    from tools import verification_gate_tool  # noqa: F401
    from tools.registry import registry
    actions = registry.get_entry("verification_gate").schema["parameters"]["properties"]["action"]["enum"]
    assert actions == ["agent_cli_capabilities", "backend_targets", "affected",
                       "frontend_skip_build", "ui_shot"]


def test_record_only_executes_twice_and_opt_in_reuses(repo, monkeypatch):
    from tools import verification_gate_tool as tool
    calls = []
    def fake_run(specs, root):
        calls.append([item[0] for item in specs])
        return [{"command_id": item[0], "exit_code": 0, "timed_out": False,
                 "duration_seconds": 0.1} for item in specs]
    monkeypatch.setattr(tool, "_run_commands", fake_run)
    artifact_dir = repo / ".evidence"
    first = tool.run_verification_gate(action="affected", workspace=repo,
                                       artifact_dir=artifact_dir, phase="review")
    second = tool.run_verification_gate(action="affected", workspace=repo,
                                        artifact_dir=artifact_dir, phase="review")
    assert first["reused"] is False and second["reused"] is False
    assert len(calls) == 2 and len(list(artifact_dir.glob("*.json"))) == 2
    hit = tool.run_verification_gate(action="affected", workspace=repo,
                                     artifact_dir=artifact_dir, phase="review",
                                     reuse_enabled=True)
    assert hit["reused"] is True and len(calls) == 2
    post_merge = tool.run_verification_gate(action="affected", workspace=repo,
                                            artifact_dir=artifact_dir, phase="post_merge",
                                            reuse_enabled=True)
    assert post_merge["reused"] is False and len(calls) == 3


def test_only_literal_true_enables_reuse(tmp_path):
    store = GateEvidenceStore(tmp_path / "artifacts")
    store.write(evidence())
    assert store.find_reusable("f" * 64, phase="review", reuse_enabled="true") is None


def test_ui_summary_requires_all_exact_viewports_and_structured_checks(tmp_path):
    from tools import verification_gate_tool as tool

    expected = ("1280x900", "768x1024", "390x844")
    results = []
    for viewport in expected:
        screenshot = f"agent-terminals-{viewport}.png"
        (tmp_path / screenshot).write_bytes(b"png")
        results.append({
            "viewport": viewport,
            "status": "passed",
            "screenshot": screenshot,
            "checks": {
                "console_error_count": 0,
                "page_error_count": 0,
                "horizontal_overflow": False,
                "terminal_width_usable": True,
                "terminal_width_px": 640,
                "terminal_width_min_px": 480,
                "bottom_navigation_clear": True,
                "bottom_navigation_clearance_px": 16,
                "handoff_visible": True,
                "held_candidate_visible": True,
            },
        })
    summary = tmp_path / "summary.json"
    summary.write_text(json.dumps({"allPassed": True, "results": results}))

    parsed = tool._parse_ui_summary(summary, tmp_path)

    assert parsed["status"] == "passed"
    assert parsed["artifacts"] == [f"agent-terminals-{v}.png" for v in expected]
    assert [item["viewport"] for item in parsed["results"]] == list(expected)
    assert all(item["console_error_count"] == 0 for item in parsed["results"])
    assert all(item["held_candidate_visible"] is True for item in parsed["results"])
    assert "stdout" not in json.dumps(parsed) and "stderr" not in json.dumps(parsed)


def test_ui_summary_missing_or_failed_viewport_is_red(tmp_path):
    from tools import verification_gate_tool as tool

    summary = tmp_path / "summary.json"
    summary.write_text(json.dumps({"allPassed": True, "results": [{
        "viewport": "1280x900",
        "status": "passed",
        "screenshot": "missing.png",
        "checks": {},
    }]}))

    parsed = tool._parse_ui_summary(summary, tmp_path)

    assert parsed["status"] == "failed"
    assert parsed["artifacts"] == []
    assert {item["viewport"] for item in parsed["results"]} == {"1280x900", "768x1024", "390x844"}
    assert any(item["exit_code"] != 0 for item in parsed["results"])


def test_ui_shot_allocates_non_live_preview_port(tmp_path, monkeypatch):
    from tools import verification_gate_tool as tool

    calls = []
    class Result:
        returncode = 1
        stdout = ""
        stderr = ""
    def fake_run(command, **kwargs):
        calls.append(command)
        return Result()
    monkeypatch.setattr(tool.subprocess, "run", fake_run)

    tool._run_ui_shot(tmp_path, tmp_path / "artifacts", "agent-terminals", "terminal_bridge")

    preview = calls[0]
    assert preview[preview.index("--port") + 1] != "9119"
