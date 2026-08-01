"""Safe, serialized cleanup worker for the configured Buzz ACP agents."""

from __future__ import annotations

import argparse
import fcntl
import json
import os
import re
import subprocess
import threading
import time
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Protocol

from fastapi import FastAPI, HTTPException

from hermes_cli.config import get_hermes_home

_UNIT_RE = re.compile(r"^buzz-agent@([a-z0-9-]+)\.service$")
_STEM_RE = re.compile(r"^[a-z0-9-]+$")
_TIMER_UNIT = "buzz-agents-nightly-restart.timer"
_SUBSCRIPTION_MARKER = "subscribed to membership notifications"
_ACTION_LOG_MAX_BYTES = 256 * 1024


@dataclass(frozen=True)
class CommandResult:
    returncode: int
    stdout: str
    stderr: str


class Runner(Protocol):
    def run(self, args: list[str]) -> CommandResult: ...


class SubprocessRunner:
    """Run fixed argv vectors without invoking a shell."""

    def run(self, args: list[str]) -> CommandResult:
        completed = subprocess.run(
            args,
            capture_output=True,
            text=True,
            check=False,
        )
        return CommandResult(completed.returncode, completed.stdout, completed.stderr)


@dataclass(frozen=True)
class TargetSet:
    units: tuple[str, ...]
    config_stems: tuple[str, ...]
    equal: bool
    units_without_config: tuple[str, ...]
    configs_without_unit: tuple[str, ...]

    def error(self) -> dict[str, Any] | None:
        if not self.units:
            return {"code": "no_targets"}
        if not self.equal:
            return {
                "code": "target_mismatch",
                "units_without_config": list(self.units_without_config),
                "configs_without_unit": list(self.configs_without_unit),
            }
        return None


class CleanupLock:
    def __init__(self, path: Path, file_handle: Any) -> None:
        self.path = path
        self._file_handle = file_handle
        self._released = False

    @classmethod
    def acquire(cls, path: Path) -> CleanupLock | None:
        path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        handle = path.open("a+", encoding="utf-8")
        try:
            fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            handle.close()
            return None
        return cls(path, handle)

    def release(self) -> None:
        if self._released:
            return
        self._released = True
        fcntl.flock(self._file_handle.fileno(), fcntl.LOCK_UN)
        self._file_handle.close()


class CleanupService:
    def __init__(
        self,
        *,
        runner: Runner | None = None,
        config_dir: Path | None = None,
        runtime_dir: Path | None = None,
        log_dir: Path | None = None,
        cgroup_process_count: Callable[[str], int | None] | None = None,
        sleep: Callable[[float], None] = time.sleep,
        wait_timeout: float = 30.0,
    ) -> None:
        self.runner = runner or SubprocessRunner()
        self.config_dir = config_dir or Path.home() / ".config" / "buzz-agents"
        self.runtime_dir = runtime_dir or _default_runtime_dir()
        self.log_dir = log_dir or get_hermes_home() / "logs"
        self._cgroup_process_count = cgroup_process_count or _read_cgroup_process_count
        self._sleep = sleep
        self.wait_timeout = wait_timeout

    @property
    def lock_path(self) -> Path:
        return self.runtime_dir / "hermes" / "buzz-agent-cleanup.lock"

    @property
    def action_log_path(self) -> Path:
        return self.log_dir / "buzz-agent-cleanup.log"

    @property
    def receipt_path(self) -> Path:
        return self.log_dir / "buzz-agent-cleanup.receipt.json"

    def acquire_lock(self) -> CleanupLock | None:
        return CleanupLock.acquire(self.lock_path)

    def is_running(self) -> bool:
        lock = self.acquire_lock()
        if lock is None:
            return True
        lock.release()
        return False

    def discover_targets(self) -> TargetSet:
        result = self.runner.run([
            "systemctl",
            "--user",
            "list-units",
            "--type=service",
            "--all",
            "--no-legend",
            "--plain",
            "buzz-agent@*.service",
        ])
        units: set[str] = set()
        if result.returncode == 0:
            for line in result.stdout.splitlines():
                token = line.split(maxsplit=1)[0] if line.strip() else ""
                if _UNIT_RE.fullmatch(token):
                    units.add(token)

        config_stems = {
            path.name.removesuffix(".env")
            for path in self.config_dir.glob("*.env")
            if path.is_file()
            and _STEM_RE.fullmatch(path.name.removesuffix(".env")) is not None
        }
        unit_stems = {_validated_stem(unit) for unit in units}
        return TargetSet(
            units=tuple(sorted(units)),
            config_stems=tuple(sorted(config_stems)),
            equal=bool(units) and unit_stems == config_stems,
            units_without_config=tuple(sorted(unit_stems - config_stems)),
            configs_without_unit=tuple(sorted(config_stems - unit_stems)),
        )

    def run(
        self,
        *,
        lock: CleanupLock | None = None,
        canary: str | None = None,
    ) -> dict[str, Any]:
        active_lock = lock or self.acquire_lock()
        if active_lock is None:
            return self._error_receipt({"code": "already_running"}, exit_code=75)
        try:
            targets = self.discover_targets()
            target_error = targets.error()
            if target_error is not None:
                return self._error_receipt(target_error, exit_code=2, persist=True)
            selected_units = targets.units
            if canary is not None:
                if _STEM_RE.fullmatch(canary) is None:
                    return self._error_receipt(
                        {"code": "invalid_canary"}, exit_code=2, persist=True
                    )
                canary_unit = f"buzz-agent@{canary}.service"
                _validated_stem(canary_unit)
                if canary_unit not in targets.units:
                    return self._error_receipt(
                        {"code": "canary_not_configured"}, exit_code=2, persist=True
                    )
                selected_units = (canary_unit,)

            started_at = _utc_timestamp()
            receipt: dict[str, Any] = {
                "schema_version": 1,
                "run_id": str(uuid.uuid4()),
                "started_at": started_at,
                "finished_at": None,
                "targets": list(selected_units),
                "mode": "canary" if canary is not None else "all",
                "before": {},
                "after": {},
                "total_memory_current_before": 0,
                "total_memory_current_after": 0,
                "failed_units": [],
                "exit_code": 0,
            }
            for unit in selected_units:
                _validated_stem(unit)
                before = self._snapshot(unit)
                receipt["before"][unit] = before
                receipt["total_memory_current_before"] += before["memory_current"] or 0

                failure = self._restart_and_wait(unit)
                if failure is not None:
                    receipt["failed_units"].append(unit)

                after = self._snapshot(unit)
                receipt["after"][unit] = after
                receipt["total_memory_current_after"] += after["memory_current"] or 0

            receipt["finished_at"] = _utc_timestamp()
            receipt["exit_code"] = 1 if receipt["failed_units"] else 0
            self._persist_receipt(receipt)
            return receipt
        finally:
            # A lock passed by the dashboard is deliberately transferred to this run.
            if active_lock is not None:
                active_lock.release()

    def _restart_and_wait(self, unit: str) -> str | None:
        _validated_stem(unit)
        marker = time.time()
        restart = self.runner.run(["systemctl", "--user", "restart", unit])
        if restart.returncode != 0:
            return "restart_failed"

        deadline = time.monotonic() + self.wait_timeout
        while True:
            _validated_stem(unit)
            active = self.runner.run(["systemctl", "--user", "is-active", unit])
            if active.returncode == 0 and active.stdout.strip() == "active":
                journal = self.runner.run([
                    "journalctl",
                    "--user",
                    "-u",
                    unit,
                    "--since",
                    f"@{marker:.6f}",
                    "--no-pager",
                    "-o",
                    "cat",
                    "-g",
                    _SUBSCRIPTION_MARKER,
                ])
                if journal.returncode == 0 and _SUBSCRIPTION_MARKER in journal.stdout:
                    return None
            if time.monotonic() >= deadline:
                return "readiness_timeout"
            self._sleep(min(0.25, self.wait_timeout))

    def _snapshot(self, unit: str) -> dict[str, Any]:
        _validated_stem(unit)
        result = self.runner.run([
            "systemctl",
            "--user",
            "show",
            unit,
            "--property=MainPID,MemoryCurrent,ControlGroup,ActiveState",
            "--no-pager",
        ])
        values: dict[str, str] = {}
        if result.returncode == 0:
            for line in result.stdout.splitlines():
                key, separator, value = line.partition("=")
                if separator:
                    values[key] = value
        control_group = values.get("ControlGroup", "")
        process_count = (
            self._cgroup_process_count(control_group) if control_group else None
        )
        return {
            "main_pid": _optional_nonnegative_int(values.get("MainPID")),
            "memory_current": _optional_nonnegative_int(values.get("MemoryCurrent")),
            "cgroup_process_count": process_count,
            "active_state": values.get("ActiveState") or "unknown",
        }

    def status(self) -> dict[str, Any]:
        targets = self.discover_targets()
        snapshots = {unit: self._snapshot(unit) for unit in targets.units}
        timer = self.runner.run([
            "systemctl",
            "--user",
            "show",
            _TIMER_UNIT,
            "--property=NextElapseUSecRealtime",
            "--no-pager",
        ])
        next_timer_run = None
        if timer.returncode == 0:
            for line in timer.stdout.splitlines():
                if line.startswith("NextElapseUSecRealtime="):
                    next_timer_run = line.partition("=")[2] or None
                    break
        return {
            "target_sets_equal": targets.equal,
            "units": list(targets.units),
            "config_stems": list(targets.config_stems),
            "units_without_config": list(targets.units_without_config),
            "configs_without_unit": list(targets.configs_without_unit),
            "total_memory_current": sum(
                snapshot["memory_current"] or 0 for snapshot in snapshots.values()
            ),
            "unit_metrics": snapshots,
            "next_timer_run": next_timer_run,
            "running": self.is_running(),
            "last_receipt": self.read_last_receipt(),
        }

    def _error_receipt(
        self,
        error: dict[str, Any],
        *,
        exit_code: int,
        persist: bool = False,
    ) -> dict[str, Any]:
        now = _utc_timestamp()
        receipt = {
            "schema_version": 1,
            "run_id": str(uuid.uuid4()),
            "started_at": now,
            "finished_at": now,
            "targets": [],
            "before": {},
            "after": {},
            "total_memory_current_before": 0,
            "total_memory_current_after": 0,
            "failed_units": [],
            "exit_code": exit_code,
            "error": error,
        }
        if persist:
            self._persist_receipt(receipt)
        return receipt

    def _persist_receipt(self, receipt: dict[str, Any]) -> None:
        self.log_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
        encoded = json.dumps(receipt, sort_keys=True, separators=(",", ":"))
        with self.action_log_path.open("a", encoding="utf-8") as handle:
            handle.write(encoded + "\n")
            handle.flush()
            os.fsync(handle.fileno())
        temporary = self.receipt_path.with_name(self.receipt_path.name + ".tmp")
        with temporary.open("w", encoding="utf-8") as handle:
            os.chmod(temporary, 0o600)
            handle.write(encoded + "\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, self.receipt_path)

    def read_last_receipt(self) -> dict[str, Any] | None:
        try:
            value = json.loads(self.receipt_path.read_text(encoding="utf-8"))
            return value if isinstance(value, dict) else None
        except (OSError, json.JSONDecodeError):
            pass
        try:
            with self.action_log_path.open("rb") as handle:
                handle.seek(0, os.SEEK_END)
                size = handle.tell()
                handle.seek(max(0, size - _ACTION_LOG_MAX_BYTES))
                lines = (
                    handle
                    .read(_ACTION_LOG_MAX_BYTES)
                    .decode("utf-8", errors="replace")
                    .splitlines()
                )
            for line in reversed(lines):
                try:
                    value = json.loads(line)
                except json.JSONDecodeError:
                    continue
                if isinstance(value, dict):
                    return value
        except OSError:
            return None
        return None


def register_buzz_agent_cleanup_routes(
    app: FastAPI,
    *,
    service_factory: Callable[[], CleanupService] = CleanupService,
    thread_factory: Callable[..., threading.Thread] = threading.Thread,
) -> None:
    """Register status and non-blocking run endpoints before the SPA catch-all."""

    @app.get("/api/buzz-agent-cleanup/status")
    def buzz_agent_cleanup_status() -> dict[str, Any]:
        return service_factory().status()

    @app.post("/api/buzz-agent-cleanup/run", status_code=202)
    def run_buzz_agent_cleanup() -> dict[str, Any]:
        service = service_factory()
        lock = service.acquire_lock()
        if lock is None:
            raise HTTPException(status_code=409, detail={"code": "already_running"})
        targets = service.discover_targets()
        error = targets.error()
        if error is not None:
            lock.release()
            raise HTTPException(status_code=422, detail=error)

        try:
            thread = thread_factory(
                target=lambda: service.run(lock=lock),
                daemon=True,
            )
            thread.start()
        except Exception:
            lock.release()
            raise HTTPException(
                status_code=500,
                detail={"code": "worker_start_failed"},
            ) from None
        return {"accepted": True, "targets": list(targets.units)}


def _validated_stem(unit: str) -> str:
    match = _UNIT_RE.fullmatch(unit)
    if match is None:
        raise ValueError("invalid Buzz agent unit")
    return match.group(1)


def _optional_nonnegative_int(value: str | None) -> int | None:
    if value is None or not value.isdigit():
        return None
    return int(value)


def _read_cgroup_process_count(control_group: str) -> int | None:
    if not control_group.startswith("/") or ".." in Path(control_group).parts:
        return None
    root = Path("/sys/fs/cgroup").resolve()
    candidate = (root / control_group.lstrip("/") / "cgroup.procs").resolve()
    if not candidate.is_relative_to(root):
        return None
    try:
        return sum(
            1 for line in candidate.read_text(encoding="utf-8").splitlines() if line
        )
    except OSError:
        return None


def _default_runtime_dir() -> Path:
    configured = os.environ.get("XDG_RUNTIME_DIR")
    if configured:
        return Path(configured)
    return Path("/tmp") / f"hermes-{os.getuid()}"


def _utc_timestamp() -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument(
        "--status", action="store_true", help="print status without restarting"
    )
    mode.add_argument(
        "--canary",
        metavar="STEM",
        help="restart one configured stem after validating the complete target set",
    )
    args = parser.parse_args(argv)
    service = CleanupService()
    result = service.status() if args.status else service.run(canary=args.canary)
    print(json.dumps(result, sort_keys=True))
    return 0 if args.status else int(result["exit_code"])


if __name__ == "__main__":
    raise SystemExit(main())
