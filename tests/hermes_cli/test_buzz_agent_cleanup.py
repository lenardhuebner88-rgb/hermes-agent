from __future__ import annotations

import json
import subprocess
from pathlib import Path
from typing import Callable

from fastapi import FastAPI
from fastapi.testclient import TestClient

from hermes_cli.buzz_agent_cleanup import (
    CleanupService,
    CommandResult,
    _parse_next_timer_run,
    register_buzz_agent_cleanup_routes,
)


class FakeRunner:
    def __init__(self, units: tuple[str, ...] = ("buzz-agent@alpha.service",)) -> None:
        self.units = units
        self.calls: list[list[str]] = []
        self.restart_failures: set[str] = set()
        self.subscription_failures: set[str] = set()
        self.memory = {unit: 1024 * (index + 1) for index, unit in enumerate(units)}

    def run(self, args: list[str]) -> CommandResult:
        self.calls.append(list(args))
        if args[:5] == [
            "systemctl",
            "--user",
            "list-units",
            "--type=service",
            "--all",
        ]:
            rows = [
                f"{unit} loaded active running Buzz ACP agent" for unit in self.units
            ]
            rows.append(
                "buzz-agent@.service loaded inactive dead Buzz ACP agent template"
            )
            rows.append("buzz-agent@alpha.service.bak loaded inactive dead backup")
            return CommandResult(0, "\n".join(rows), "")
        if args[:3] == ["systemctl", "--user", "list-timers"]:
            return CommandResult(
                0,
                json.dumps([
                    {
                        "next": 1785636029805599,
                        "unit": "buzz-agents-nightly-restart.timer",
                        "activates": "buzz-agents-nightly-restart.service",
                    }
                ]),
                "",
            )
        if args[:3] == ["systemctl", "--user", "show"]:
            unit = args[3]
            index = self.units.index(unit) + 1
            return CommandResult(
                0,
                "\n".join([
                    f"MainPID={1000 + index}",
                    f"MemoryCurrent={self.memory[unit]}",
                    f"ControlGroup=/user.slice/{unit}",
                    "ActiveState=active",
                ]),
                "",
            )
        if args[:3] == ["systemctl", "--user", "restart"]:
            unit = args[3]
            if unit in self.restart_failures:
                return CommandResult(1, "", "restart failed with SECRET=value")
            self.memory[unit] //= 2
            return CommandResult(0, "", "")
        if args[:3] == ["systemctl", "--user", "is-active"]:
            return CommandResult(0, "active\n", "")
        if args and args[0] == "journalctl":
            unit = args[args.index("-u") + 1]
            if unit in self.subscription_failures:
                return CommandResult(1, "", "relay SECRET=value")
            return CommandResult(0, "subscribed to membership notifications\n", "")
        raise AssertionError(f"unexpected command: {args}")


def _service(
    tmp_path: Path,
    runner: FakeRunner,
    *,
    cgroup_reader: Callable[[str], int] | None = None,
) -> CleanupService:
    config_dir = tmp_path / "config"
    config_dir.mkdir()
    for unit in runner.units:
        stem = unit.removeprefix("buzz-agent@").removesuffix(".service")
        (config_dir / f"{stem}.env").write_text(
            "SECRET=not-for-logs\n", encoding="utf-8"
        )
    return CleanupService(
        runner=runner,
        config_dir=config_dir,
        runtime_dir=tmp_path / "runtime",
        log_dir=tmp_path / "logs",
        cgroup_process_count=cgroup_reader or (lambda _group: 2),
        sleep=lambda _seconds: None,
        wait_timeout=0.01,
    )


def test_target_discovery_uses_exact_loaded_unit_and_config_stems(
    tmp_path: Path,
) -> None:
    runner = FakeRunner(("buzz-agent@alpha.service", "buzz-agent@beta-2.service"))
    service = _service(tmp_path, runner)
    (service.config_dir / "alpha.env.bak").write_text("", encoding="utf-8")
    (service.config_dir / "UPPER.env").write_text("", encoding="utf-8")

    targets = service.discover_targets()

    assert targets.equal is True
    assert targets.units == ("buzz-agent@alpha.service", "buzz-agent@beta-2.service")
    assert targets.config_stems == ("alpha", "beta-2")


def test_empty_targets_stop_before_restart(tmp_path: Path) -> None:
    runner = FakeRunner(())
    service = _service(tmp_path, runner)

    receipt = service.run()

    assert receipt["exit_code"] != 0
    assert receipt["error"]["code"] == "no_targets"
    assert not any("restart" in call for call in runner.calls)


def test_unit_config_difference_stops_before_restart(tmp_path: Path) -> None:
    runner = FakeRunner(("buzz-agent@alpha.service",))
    service = _service(tmp_path, runner)
    (service.config_dir / "beta.env").write_text("", encoding="utf-8")

    receipt = service.run()

    assert receipt["error"] == {
        "code": "target_mismatch",
        "units_without_config": [],
        "configs_without_unit": ["beta"],
    }
    assert not any("restart" in call for call in runner.calls)


def test_worker_restarts_serially_and_continues_after_partial_failure(
    tmp_path: Path,
) -> None:
    units = (
        "buzz-agent@alpha.service",
        "buzz-agent@beta.service",
        "buzz-agent@gamma.service",
    )
    runner = FakeRunner(units)
    runner.restart_failures.add(units[1])
    service = _service(tmp_path, runner, cgroup_reader=lambda _group: 3)

    receipt = service.run()

    restart_calls = [call for call in runner.calls if "restart" in call]
    assert [call[-1] for call in restart_calls] == list(units)
    assert receipt["failed_units"] == [units[1]]
    assert receipt["exit_code"] != 0
    assert set(receipt["before"]) == set(units)
    assert set(receipt["after"]) == set(units)
    assert receipt["before"][units[0]]["cgroup_process_count"] == 3
    assert "orphan" not in json.dumps(receipt).lower()
    assert "SECRET" not in json.dumps(receipt)


def test_worker_waits_for_active_and_fresh_subscription_per_unit(
    tmp_path: Path,
) -> None:
    units = ("buzz-agent@alpha.service", "buzz-agent@beta.service")
    runner = FakeRunner(units)
    service = _service(tmp_path, runner)

    receipt = service.run()

    assert receipt["exit_code"] == 0
    significant = [
        call
        for call in runner.calls
        if "restart" in call
        or "is-active" in call
        or (call and call[0] == "journalctl")
    ]
    assert [call[2] if call[0] == "systemctl" else call[0] for call in significant] == [
        "restart",
        "is-active",
        "journalctl",
        "restart",
        "is-active",
        "journalctl",
    ]
    journal_calls = [call for call in runner.calls if call and call[0] == "journalctl"]
    assert all("--since" in call for call in journal_calls)
    assert all(
        "subscribed to membership notifications" in call for call in journal_calls
    )


def test_operator_canary_validates_full_set_then_restarts_only_selected_stem(
    tmp_path: Path,
) -> None:
    units = ("buzz-agent@alpha.service", "buzz-agent@beta.service")
    runner = FakeRunner(units)
    service = _service(tmp_path, runner)

    receipt = service.run(canary="beta")

    restart_calls = [call for call in runner.calls if "restart" in call]
    assert [call[-1] for call in restart_calls] == ["buzz-agent@beta.service"]
    assert receipt["mode"] == "canary"
    assert receipt["targets"] == ["buzz-agent@beta.service"]


def test_unknown_operator_canary_stops_before_restart(tmp_path: Path) -> None:
    runner = FakeRunner()
    service = _service(tmp_path, runner)

    receipt = service.run(canary="not-configured")

    assert receipt["error"]["code"] == "canary_not_configured"
    assert receipt["exit_code"] != 0
    assert not any("restart" in call for call in runner.calls)


def test_receipt_is_atomic_persistent_and_action_log_is_append_only(
    tmp_path: Path,
) -> None:
    runner = FakeRunner()
    service = _service(tmp_path, runner)
    first = service.run()
    second = service.run()

    assert service.read_last_receipt() == second
    log_lines = service.action_log_path.read_text(encoding="utf-8").splitlines()
    assert len(log_lines) == 2
    assert json.loads(log_lines[0])["run_id"] == first["run_id"]
    assert json.loads(log_lines[1])["run_id"] == second["run_id"]
    assert "SECRET" not in "\n".join(log_lines)
    assert not list(service.log_dir.glob("*.tmp"))
    service.receipt_path.unlink()
    assert service.read_last_receipt() == second


def test_parse_next_timer_run_converts_systemd_json_and_handles_missing_values() -> (
    None
):
    real_output = (
        '[{"next":1785636029805599,"left":1785636029805599,"last":0,'
        '"passed":0,"unit":"buzz-agents-nightly-restart.timer",'
        '"activates":"buzz-agents-nightly-restart.service"}]'
    )

    assert _parse_next_timer_run(real_output) == "2026-08-02T02:00:29.805599+00:00"
    assert _parse_next_timer_run("[]") is None
    assert (
        _parse_next_timer_run(
            '[{"unit":"buzz-agents-nightly-restart.timer","next":"n/a"}]'
        )
        is None
    )


def test_status_reconstructs_receipt_and_reports_honest_totals(tmp_path: Path) -> None:
    runner = FakeRunner(("buzz-agent@alpha.service", "buzz-agent@beta.service"))
    service = _service(tmp_path, runner)
    receipt = service.run()
    reloaded = CleanupService(
        runner=runner,
        config_dir=service.config_dir,
        runtime_dir=service.runtime_dir,
        log_dir=service.log_dir,
        cgroup_process_count=lambda _group: 2,
    )

    status = reloaded.status()

    assert status["target_sets_equal"] is True
    assert status["total_memory_current"] == sum(runner.memory.values())
    assert status["next_timer_run"] == "2026-08-02T02:00:29.805599+00:00"
    assert status["running"] is False
    assert status["last_receipt"] == receipt


class ImmediateThread:
    def __init__(self, *, target: Callable[[], None], daemon: bool) -> None:
        self.target = target
        self.daemon = daemon

    def start(self) -> None:
        self.target()


class DeferredThread(ImmediateThread):
    def start(self) -> None:
        return None


def test_post_ignores_body_targets_and_runs_server_discovered_units(
    tmp_path: Path,
) -> None:
    runner = FakeRunner(("buzz-agent@alpha.service",))
    service = _service(tmp_path, runner)
    app = FastAPI()
    register_buzz_agent_cleanup_routes(
        app,
        service_factory=lambda: service,
        thread_factory=ImmediateThread,
    )

    response = TestClient(app).post(
        "/api/buzz-agent-cleanup/run",
        json={"units": ["buzz-agent@attacker.service"], "targets": ["root.service"]},
    )

    assert response.status_code == 202
    command_text = json.dumps(runner.calls)
    assert "attacker" not in command_text
    assert "root.service" not in command_text
    assert "buzz-agent@alpha.service" in command_text


def test_post_returns_structured_error_for_target_difference(tmp_path: Path) -> None:
    runner = FakeRunner(("buzz-agent@alpha.service",))
    service = _service(tmp_path, runner)
    (service.config_dir / "extra.env").write_text("", encoding="utf-8")
    app = FastAPI()
    register_buzz_agent_cleanup_routes(app, service_factory=lambda: service)

    response = TestClient(app).post("/api/buzz-agent-cleanup/run", json={})

    assert response.status_code == 422
    assert response.json()["detail"]["code"] == "target_mismatch"
    assert not any("restart" in call for call in runner.calls)


def test_second_post_gets_409_while_process_lock_is_held(tmp_path: Path) -> None:
    runner = FakeRunner()
    service = _service(tmp_path, runner)
    deferred: list[DeferredThread] = []

    def make_thread(*, target: Callable[[], None], daemon: bool) -> DeferredThread:
        thread = DeferredThread(target=target, daemon=daemon)
        deferred.append(thread)
        return thread

    app = FastAPI()
    register_buzz_agent_cleanup_routes(
        app,
        service_factory=lambda: service,
        thread_factory=make_thread,
    )
    client = TestClient(app)

    assert client.post("/api/buzz-agent-cleanup/run").status_code == 202
    assert client.post("/api/buzz-agent-cleanup/run").status_code == 409
    deferred[0].target()
    assert client.post("/api/buzz-agent-cleanup/run").status_code == 202
    deferred[1].target()


def test_runtime_lock_uses_injected_xdg_runtime_dir(
    tmp_path: Path, monkeypatch
) -> None:
    runtime_dir = tmp_path / "xdg-runtime"
    monkeypatch.setenv("XDG_RUNTIME_DIR", str(runtime_dir))
    runner = FakeRunner()
    config_dir = tmp_path / "config"
    config_dir.mkdir()
    (config_dir / "alpha.env").write_text("", encoding="utf-8")
    service = CleanupService(
        runner=runner,
        config_dir=config_dir,
        log_dir=tmp_path / "logs",
        cgroup_process_count=lambda _group: 1,
    )

    lock = service.acquire_lock()

    assert lock is not None
    assert service.lock_path == runtime_dir / "hermes" / "buzz-agent-cleanup.lock"
    assert (
        CleanupService(
            runner=runner,
            config_dir=config_dir,
            log_dir=tmp_path / "logs",
        ).acquire_lock()
        is None
    )
    lock.release()


def test_get_status_exposes_persistent_receipt_and_target_equality(
    tmp_path: Path,
) -> None:
    runner = FakeRunner()
    service = _service(tmp_path, runner)
    receipt = service.run()
    app = FastAPI()
    register_buzz_agent_cleanup_routes(app, service_factory=lambda: service)

    response = TestClient(app).get("/api/buzz-agent-cleanup/status")

    assert response.status_code == 200
    assert response.json()["target_sets_equal"] is True
    assert response.json()["last_receipt"] == receipt


def test_all_system_and_journal_invocations_are_argument_lists(tmp_path: Path) -> None:
    runner = FakeRunner()
    service = _service(tmp_path, runner)

    assert service.run()["exit_code"] == 0
    assert runner.calls
    assert all(isinstance(call, list) for call in runner.calls)
    assert all(
        not any(";" in arg or "&&" in arg for arg in call) for call in runner.calls
    )


def test_command_runner_never_enables_shell(monkeypatch) -> None:
    captured: dict[str, object] = {}

    def fake_run(args, **kwargs):
        captured["args"] = args
        captured.update(kwargs)
        return subprocess.CompletedProcess(args, 0, "ok", "")

    monkeypatch.setattr(subprocess, "run", fake_run)
    from hermes_cli.buzz_agent_cleanup import SubprocessRunner

    result = SubprocessRunner().run([
        "systemctl",
        "--user",
        "is-active",
        "safe.service",
    ])

    assert result.returncode == 0
    assert captured["args"] == ["systemctl", "--user", "is-active", "safe.service"]
    assert captured.get("shell") is not True
