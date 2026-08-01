from pathlib import Path


_REPO_ROOT = Path(__file__).resolve().parents[2]
_SYSTEMD_DIR = _REPO_ROOT / "scripts" / "systemd"
_SERVICE_PATH = _SYSTEMD_DIR / "buzz-agents-nightly-restart.service"
_TIMER_PATH = _SYSTEMD_DIR / "buzz-agents-nightly-restart.timer"
_WIRING_PATH = _REPO_ROOT / "scripts" / "buzz_agent_cleanup.WIRING.md"


def _read(path: Path) -> str:
    return path.read_text(encoding="utf-8")


def test_nightly_service_runs_versioned_cleanup_worker_without_fixed_targets() -> None:
    service = _read(_SERVICE_PATH)

    assert (
        "ExecStart=%h/.hermes/hermes-agent/venv/bin/python "
        "-m hermes_cli.buzz_agent_cleanup"
    ) in service
    assert "WorkingDirectory=%h/.hermes/hermes-agent" in service
    assert "Environment=XDG_RUNTIME_DIR=%t" in service
    assert "buzz-agent@" not in service
    assert "systemctl --user restart" not in service
    assert "/usr/bin/systemctl" not in service


def test_nightly_timer_preserves_schedule_contract() -> None:
    timer = _read(_TIMER_PATH)

    assert "OnCalendar=*-*-* 04:00:00" in timer
    assert "RandomizedDelaySec=5min" in timer
    assert "Persistent=false" in timer
    assert "Unit=buzz-agents-nightly-restart.service" in timer


def test_wiring_documents_safe_operator_rollout() -> None:
    wiring = _read(_WIRING_PATH)

    required_snippets = (
        "cp --archive",
        "systemctl --user daemon-reload",
        "--status",
        "--canary fable",
        "systemctl --user start buzz-agents-nightly-restart.service",
        "$XDG_RUNTIME_DIR/hermes/buzz-agent-cleanup.lock",
    )
    for snippet in required_snippets:
        assert snippet in wiring

    assert wiring.index("--status") < wiring.index("--canary fable")
    assert wiring.index("--canary fable") < wiring.index(
        "systemctl --user start buzz-agents-nightly-restart.service"
    )
