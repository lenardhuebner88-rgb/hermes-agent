"""Closed server-side verification gates with raw-free evidence receipts."""
from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import tempfile
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Mapping, Sequence

from hermes_cli.config import cfg_get, load_config
from hermes_cli.gate_evidence import GateEvidence, GateEvidenceStore, build_gate_fingerprint
from hermes_constants import terminal_runs_root
from tools.registry import registry, tool_error, tool_result

GATE_IMPLEMENTATION_VERSION = "1"
ACTIONS = (
    "agent_cli_capabilities",
    "backend_targets",
    "affected",
    "frontend_skip_build",
    "ui_shot",
)
PHASES = ("pre_submit", "review", "post_merge", "release")
VIEWPORTS = ("1280x900", "768x1024", "390x844")
ALLOWED_ENV = ("CI", "LANG", "LC_ALL", "PYTHONHASHSEED", "TZ")
BACKEND_TARGETS = (
    "tests/hermes_cli/test_terminal_candidates.py",
    "tests/hermes_cli/test_terminal_candidate_e2e.py",
    "tests/hermes_cli/test_kanban_worktrees_integrator.py",
    "tests/hermes_cli/test_kanban_worktrees_commit_gates.py",
    "tests/hermes_cli/test_kanban_worktrees_provision.py",
    "tests/hermes_cli/test_kanban_shadow_routing.py",
    "tests/hermes_cli/test_kanban_db_lifecycle.py",
    "tests/hermes_cli/test_kanban_decompose.py",
    "tests/hermes_cli/test_planspecs.py",
    "tests/hermes_cli/test_kanban_db_heiler.py",
    "tests/hermes_cli/test_vision_metrics.py",
    "tests/hermes_cli/test_config.py",
    "tests/plugins/test_kanban_attachments.py",
    "tests/plugins/test_kanban_dashboard_plugin.py",
    "tests/test_planspec_handoff.py",
    "tests/hermes_cli/test_agent_terminals.py",
    "tests/hermes_cli/test_web_server_agent_terminals.py",
    "tests/tools/test_verification_gate_tool.py",
)
CONFIG_PATHS = ("pyproject.toml", "setup.cfg", "ruff.toml", "package.json", "web/package.json")
LOCKFILE_PATHS = ("uv.lock", "requirements.txt", "package-lock.json", "web/package-lock.json")


def capabilities() -> dict[str, Any]:
    return {
        "version": GATE_IMPLEMENTATION_VERSION,
        "execution_class": "verify_exec",
        "inspect_only": ["agent_cli_capabilities"],
        "verify_exec": ["backend_targets", "affected", "frontend_skip_build", "ui_shot"],
        "actions": list(ACTIONS),
        "record_only_default": True,
        "reuse_phases": ["pre_submit", "review"],
    }


def _safe_workspace(value: str | Path) -> Path:
    root = Path(value).resolve()
    if not root.is_dir() or not (root / ".git").exists():
        # Linked worktrees have a .git file, regular repos a directory.
        if not root.is_dir() or not (root / ".git").is_file():
            raise ValueError("workspace must be a git worktree")
    subprocess.run(["git", "rev-parse", "--show-toplevel"], cwd=root, check=True,
                   stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    return root


def _artifact_dir(terminal_run_id: str | None, explicit: str | Path | None) -> Path:
    if explicit is not None:
        return Path(explicit).resolve()
    if terminal_run_id is None:
        task_id = os.environ.get("HERMES_KANBAN_TASK", "")
        run_id = os.environ.get("HERMES_KANBAN_RUN_ID", "")
        if (re.fullmatch(r"[A-Za-z0-9_.-]{1,128}", task_id)
                and re.fullmatch(r"[A-Za-z0-9_.-]{1,128}", run_id)):
            # Dispatcher workers have no interactive terminal-run id. Give the
            # verification run an isolated artifact directory under the same
            # protected terminal-runs root so the closed action remains usable.
            return terminal_runs_root() / f"verification-{task_id}-{run_id}" / "artifacts"
        raise ValueError("terminal_run_id is required outside a Kanban worker run")
    if not re.fullmatch(r"[A-Za-z0-9_.-]{1,128}", terminal_run_id):
        raise ValueError("terminal_run_id must be a safe identifier")
    run_dir = terminal_runs_root() / terminal_run_id
    manifest = run_dir / "manifest.json"
    if not manifest.is_file():
        raise ValueError("terminal run manifest does not exist")
    return run_dir / "artifacts"


def _command_specs(action: str, root: Path) -> list[tuple[str, list[str]]]:
    if action == "backend_targets":
        return [("backend_targets", [str(root / "scripts/run_tests.sh"), *BACKEND_TARGETS])]
    if action == "affected":
        return [
            ("run_affected", [str(root / "scripts/run-affected.sh")]),
            ("worker_gate_ruff", [str(root / "scripts/worker-gate-ruff.sh")]),
        ]
    if action == "frontend_skip_build":
        return [("frontend_skip_build", [str(root / "scripts/gate-frontend.sh"), "--skip-build"])]
    return []


def _safe_env() -> dict[str, str]:
    inherited = {key: os.environ[key] for key in ALLOWED_ENV if key in os.environ}
    inherited["PATH"] = os.environ.get("PATH", "/usr/local/bin:/usr/bin:/bin")
    inherited["HERMES_SANDBOX_MODE"] = "1"
    return inherited


def _run_commands(specs: Sequence[tuple[str, list[str]]], root: Path) -> list[dict[str, Any]]:
    results: list[dict[str, Any]] = []
    for command_id, argv in specs:
        started = time.monotonic()
        try:
            completed = subprocess.run(argv, cwd=root, env=_safe_env(), text=True,
                                       stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                                       check=False, timeout=3600)
            exit_code: int | None = completed.returncode
            timed_out = False
        except subprocess.TimeoutExpired:
            exit_code = None
            timed_out = True
        results.append({
            "command_id": command_id,
            "exit_code": exit_code,
            "timed_out": timed_out,
            "duration_seconds": round(time.monotonic() - started, 3),
        })
    return results


def _run_ui_shot(root: Path, artifact_dir: Path, route: str, scenario: str) -> dict[str, Any]:
    if route != "agent-terminals" or scenario != "terminal_bridge":
        raise ValueError("ui_shot only allows route=agent-terminals, scenario=terminal_bridge")
    artifact_dir.mkdir(parents=True, exist_ok=True)
    home = Path(tempfile.mkdtemp(prefix="hermes-terminal-bridge-"))
    tmux_tmp = home / "tmux"
    tmux_tmp.mkdir(mode=0o700)
    preview: subprocess.Popen[str] | None = None
    results: list[dict[str, Any]] = []
    screenshots: list[str] = []
    try:
        launch = subprocess.run(
            [str(root / "scripts/preview-realdata.sh"), "--scenario", scenario,
             "--route", route, "--home", str(home), "--no-build", "--keep"],
            cwd=root, env={**_safe_env(), "TMUX_TMPDIR": str(tmux_tmp)}, text=True,
            stdout=subprocess.PIPE, stderr=subprocess.PIPE, check=False, timeout=120,
        )
        results.append({"command_id": "preview_terminal_bridge", "exit_code": launch.returncode,
                        "timed_out": False, "duration_seconds": 0.0})
        if launch.returncode == 75 or "ui_preview_busy" in launch.stdout:
            return {"status": "ui_preview_busy", "results": results, "artifacts": []}
        if launch.returncode:
            return {"status": "failed", "results": results, "artifacts": []}
        output = dict(line.split("=", 1) for line in launch.stdout.splitlines() if "=" in line)
        url = output.get("PREVIEW_URL", "") + "/agent-terminals"
        pid = int(output.get("PREVIEW_PID", "0"))
        for viewport in VIEWPORTS:
            target = artifact_dir / f"ui-shot-agent-terminals-terminal_bridge-{viewport}.png"
            shot = subprocess.run(
                [str(root / "scripts/ui-shot.sh"), url, "--out", str(target),
                 "--viewport", viewport, "--console"],
                cwd=root, env=_safe_env(), text=True, stdout=subprocess.PIPE,
                stderr=subprocess.PIPE, check=False, timeout=120,
            )
            results.append({"command_id": f"ui_shot_{viewport}", "exit_code": shot.returncode,
                            "timed_out": False, "duration_seconds": 0.0})
            if shot.returncode == 0 and target.is_file() and target.stat().st_size:
                screenshots.append(target.name)
        if pid > 1:
            subprocess.run(["kill", "-INT", str(pid)], check=False,
                           stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        status = "passed" if len(screenshots) == len(VIEWPORTS) else "failed"
        return {"status": status, "results": results, "artifacts": screenshots}
    except subprocess.TimeoutExpired:
        results.append({"command_id": "ui_shot_timeout", "exit_code": None,
                        "timed_out": True, "duration_seconds": 120.0})
        return {"status": "failed", "results": results, "artifacts": screenshots}
    finally:
        shutil.rmtree(home, ignore_errors=True)


def run_verification_gate(
    *,
    action: str,
    workspace: str | Path,
    terminal_run_id: str | None = None,
    artifact_dir: str | Path | None = None,
    phase: str = "review",
    reuse_enabled: bool = False,
    route: str = "agent-terminals",
    scenario: str = "terminal_bridge",
) -> dict[str, Any]:
    if action not in ACTIONS:
        raise ValueError(f"unknown verification gate action: {action}")
    if action == "agent_cli_capabilities":
        return capabilities()
    normalized_phase = phase.lower().replace("-", "_")
    if normalized_phase not in PHASES:
        raise ValueError(f"unknown gate phase: {phase}")
    root = _safe_workspace(workspace)
    evidence_dir = _artifact_dir(terminal_run_id, artifact_dir)
    tests = BACKEND_TARGETS if action == "backend_targets" else ()
    fingerprint = build_gate_fingerprint(
        root, gate_id=action, gate_version=GATE_IMPLEMENTATION_VERSION,
        test_selection=tests, config_paths=CONFIG_PATHS, lockfile_paths=LOCKFILE_PATHS,
        allowed_env=ALLOWED_ENV,
    )
    store = GateEvidenceStore(evidence_dir)
    reusable = store.find_reusable(fingerprint.digest, phase=normalized_phase,
                                   reuse_enabled=reuse_enabled)
    if reusable is not None:
        return _public_result(reusable.evidence, reusable.digest, reused=True,
                              evidence_file=reusable.path.name)
    started_wall = datetime.now(timezone.utc)
    started = time.monotonic()
    if action == "ui_shot":
        executed = _run_ui_shot(root, evidence_dir, route, scenario)
        results = executed["results"]
        artifacts = list(executed.get("artifacts", []))
        status = executed.get("status", "failed")
        if status != "passed" or not all(any(viewport in item for item in artifacts)
                                         for viewport in VIEWPORTS):
            status = "failed"
    else:
        results = _run_commands(_command_specs(action, root), root)
        artifacts = []
        status = "passed" if results and all(item["exit_code"] == 0 and not item["timed_out"]
                                              for item in results) else "failed"
    finished = datetime.now(timezone.utc)
    evidence = GateEvidence(
        fingerprint=fingerprint.digest, gate_id=action,
        gate_version=GATE_IMPLEMENTATION_VERSION, phase=normalized_phase,
        status=status, started_at=started_wall.isoformat(), finished_at=finished.isoformat(),
        duration_seconds=round(time.monotonic() - started, 3), results=results,
        head_sha=fingerprint.payload["head_sha"],
    )
    receipt = store.write(evidence)
    result = _public_result(evidence, receipt.digest, reused=False,
                            evidence_file=receipt.path.name)
    if artifacts:
        result["artifacts"] = artifacts
    return result


def _public_result(evidence: GateEvidence, digest: str, *, reused: bool,
                   evidence_file: str) -> dict[str, Any]:
    return {
        "fingerprint": evidence.fingerprint,
        "gate_id": evidence.gate_id,
        "status": evidence.status,
        "started_at": evidence.started_at,
        "finished_at": evidence.finished_at,
        "evidence_digest": digest,
        "evidence_file": evidence_file,
        "reused": reused,
        "results": evidence.results,
    }


def _check_requirements() -> tuple[bool, str]:
    workspace = os.environ.get("HERMES_KANBAN_WORKSPACE")
    if workspace and Path(workspace).is_dir():
        return True, ""
    return False, "verification_gate is available only in a Kanban workspace"


def _handle_verification_gate(args: Mapping[str, Any]) -> Any:
    try:
        action = str(args.get("action", ""))
        if action == "agent_cli_capabilities":
            return tool_result(capabilities())
        config = load_config()
        reuse = cfg_get(config, "kanban.gate_evidence_reuse", False) is True
        result = run_verification_gate(
            action=action,
            workspace=os.environ.get("HERMES_KANBAN_WORKSPACE", os.getcwd()),
            terminal_run_id=args.get("terminal_run_id"), phase=str(args.get("phase", "review")),
            reuse_enabled=reuse, route=str(args.get("route", "agent-terminals")),
            scenario=str(args.get("scenario", "terminal_bridge")),
        )
        return tool_result(result)
    except (OSError, ValueError, subprocess.SubprocessError) as exc:
        return tool_error(str(exc))


registry.register(
    name="verification_gate",
    toolset="kanban",
    schema={
        "name": "verification_gate",
        "description": "Run one closed, server-side verification action and record raw-free evidence.",
        "parameters": {
            "type": "object",
            "properties": {
                "action": {"type": "string", "enum": list(ACTIONS)},
                "terminal_run_id": {"type": "string", "minLength": 1, "maxLength": 128},
                "phase": {"type": "string", "enum": list(PHASES), "default": "review"},
                "route": {"type": "string", "enum": ["agent-terminals"], "default": "agent-terminals"},
                "scenario": {"type": "string", "enum": ["terminal_bridge"], "default": "terminal_bridge"},
            },
            "required": ["action"],
            "additionalProperties": False,
        },
    },
    handler=_handle_verification_gate,
    check_fn=_check_requirements,
    description="Closed verification gates for Kanban workers.",
    emoji="✅",
)
