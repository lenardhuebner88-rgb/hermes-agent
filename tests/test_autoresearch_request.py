from __future__ import annotations

import importlib.util
import json
import subprocess
import sys
from pathlib import Path

from hermes_constants import get_hermes_home

ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "scripts" / "autoresearch_request.py"


def load_module():
    spec = importlib.util.spec_from_file_location("autoresearch_request", SCRIPT)
    module = importlib.util.module_from_spec(spec)
    assert spec and spec.loader
    spec.loader.exec_module(module)
    return module


def test_valid_request_writes_json(tmp_path):
    module = load_module()
    rc = module.main([
        "create",
        "--mode",
        "skills",
        "--area",
        "github",
        "--focus",
        "safety_gates_and_output_contracts",
        "--max-iterations",
        "3",
        "--mutation-policy",
        "requires_operator_go",
        "--request-dir",
        str(tmp_path),
    ])
    assert rc == 0
    files = list(tmp_path.glob("*.json"))
    assert len(files) == 1
    text = files[0].read_text(encoding="utf-8")
    assert '"schema": "autoresearch-run-request-v1"' in text
    assert '"mutation_policy": "requires_operator_go"' in text
    assert "/autoresearch mode=skills area=github" in text


def test_default_hermes_home_uses_profile_aware_constant():
    module = load_module()
    assert module.DEFAULT_HERMES_HOME == get_hermes_home()


def test_model_route_status_defaults_to_configured_with_pending_self_test():
    module = load_module()
    data = module.build_request(area="github", focus="output_contract")
    # MiniMax-M2.7 is configured in config.yaml, so the route is "configured",
    # not "unverified"; the runner self-test is separately tracked as pending.
    assert data["model_route_status"] == "configured"
    assert data["route_self_test"] == "pending"
    assert data["model_preference"] == "MiniMax-M2.7-highspeed"


def test_created_request_json_records_configured_route(tmp_path):
    module = load_module()
    path = module.create_request(
        mode="skills",
        area="github",
        focus="output_contract",
        requests_dir=tmp_path,
    )
    written = json.loads(path.read_text(encoding="utf-8"))
    assert written["model_route_status"] == "configured"
    assert written["route_self_test"] == "pending"


def test_skills_area_includes_profile_and_repo_skill_roots(tmp_path):
    module = load_module()
    hermes_home = tmp_path / "home"
    repo_root = tmp_path / "repo"
    paths = module.allowed_paths_for_area("github", repo_root=repo_root, hermes_home=hermes_home)
    assert str(hermes_home / "skills/github") in paths
    assert str(repo_root / "skills/github") in paths


def test_all_area_includes_profile_and_repo_skill_roots(tmp_path):
    module = load_module()
    hermes_home = tmp_path / "home"
    repo_root = tmp_path / "repo"
    paths = module.allowed_paths_for_area("all", repo_root=repo_root, hermes_home=hermes_home)
    assert str(hermes_home / "skills") in paths
    assert str(repo_root / "skills") in paths


def test_hermes_kanban_area_is_not_broad_devops(tmp_path):
    module = load_module()
    hermes_home = tmp_path / "home"
    repo_root = tmp_path / "repo"
    paths = module.allowed_paths_for_area("hermes-kanban", repo_root=repo_root, hermes_home=hermes_home)
    assert str(hermes_home / "skills/devops") not in paths
    assert str(repo_root / "skills/devops") not in paths
    assert str(hermes_home / "skills/hermes-kanban") in paths
    assert str(repo_root / "skills/devops/kanban-orchestrator") in paths


def test_cli_area_all_creates_request_json(tmp_path):
    result = subprocess.run(
        [
            sys.executable,
            str(SCRIPT),
            "create",
            "--area",
            "all",
            "--focus",
            "safety_gates",
            "--request-dir",
            str(tmp_path),
        ],
        check=False,
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )
    assert result.returncode == 0, result.stderr
    assert "/autoresearch mode=skills area=all focus=safety_gates" in result.stdout
    assert len(list(tmp_path.glob("*.json"))) == 1


def test_validate_request_rejects_allowed_paths_outside_area_roots(tmp_path):
    module = load_module()
    hermes_home = tmp_path / "home"
    repo_root = tmp_path / "repo"
    data = module.build_request(area="github", focus="output_contract", repo_root=repo_root, hermes_home=hermes_home)
    data["allowed_paths"] = [str(repo_root / "skills/devops")]
    try:
        module.validate_request(data, repo_root=repo_root, hermes_home=hermes_home)
    except ValueError as exc:
        assert "outside" in str(exc)
    else:
        raise AssertionError("validate_request should reject out-of-area allowed_paths")


def test_validate_request_rejects_invalid_focus(tmp_path):
    module = load_module()
    hermes_home = tmp_path / "home"
    repo_root = tmp_path / "repo"
    data = module.build_request(area="github", focus="output_contract", repo_root=repo_root, hermes_home=hermes_home)
    data["focus"] = "../../bad"
    try:
        module.validate_request(data, repo_root=repo_root, hermes_home=hermes_home)
    except ValueError as exc:
        assert "focus" in str(exc)
    else:
        raise AssertionError("validate_request should reject invalid focus")


def test_invalid_area_fails_closed():
    module = load_module()
    try:
        module.build_request(area="../../bad", focus="safety_gates")
    except ValueError as exc:
        assert "invalid area" in str(exc)
    else:
        raise AssertionError("invalid area should fail")


def test_path_traversal_fails_closed():
    module = load_module()
    try:
        module.validate_allowed_paths("github", ["/home/piet/.hermes/skills/github/../../.env"])
    except ValueError as exc:
        assert "forbidden" in str(exc) or "outside" in str(exc)
    else:
        raise AssertionError("path traversal should fail")


def test_secrets_config_db_paths_cannot_be_allowed():
    module = load_module()
    for path in [
        "/home/piet/.hermes/.env",
        "/home/piet/.hermes/config.yaml",
        "/home/piet/.hermes/auth.json",
        "/home/piet/.hermes/kanban.db",
    ]:
        try:
            module.validate_allowed_paths("all", [path])
        except ValueError:
            pass
        else:
            raise AssertionError(f"{path} should be rejected")


def test_cli_output_includes_copy_pasteable_next_command(tmp_path):
    result = subprocess.run(
        [
            sys.executable,
            str(SCRIPT),
            "create",
            "--area",
            "github",
            "--focus",
            "output_contract",
            "--request-dir",
            str(tmp_path),
        ],
        check=False,
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )
    assert result.returncode == 0, result.stderr
    assert "/autoresearch mode=skills area=github focus=output_contract" in result.stdout
