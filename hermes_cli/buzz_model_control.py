"""Fork-owned FastAPI routes: read and change the ACP model per Buzz agent.

Buzz agents are configured through ``~/.config/buzz-agents/<stem>.env`` files
that carry a quoted ``BUZZ_ACP_MODEL`` value, read by the ``buzz-agent@<stem>``
systemd unit at process start. There is no ``ExecReload=`` for that unit, so a
model change only takes effect after a restart — this module writes the new
value atomically, then restarts and waits for the same readiness marker the
nightly cleanup worker (``buzz_agent_cleanup.py``) already waits on.
"""

from __future__ import annotations

import json
import os
import re
import subprocess
import time
from pathlib import Path
from typing import Any, Callable, Protocol

from fastapi import FastAPI, HTTPException
from pydantic import BaseModel

from hermes_cli.buzz_agent_cleanup import _SUBSCRIPTION_MARKER as _READINESS_MARKER

_STEM_RE = re.compile(r"^[a-z0-9-]+$")
_UNIT_RE = re.compile(r"^buzz-agent@([a-z0-9-]+)\.service$")
_ENV_LINE_RE = re.compile(r"^([A-Za-z_][A-Za-z0-9_]*)=(.*)$")

_MODEL_KEY = "BUZZ_ACP_MODEL"
_COMMAND_KEY = "BUZZ_ACP_AGENT_COMMAND"
_ARGS_KEY = "BUZZ_ACP_AGENT_ARGS"
_KNOWN_KEYS = frozenset({_MODEL_KEY, _COMMAND_KEY, _ARGS_KEY})

DEFAULT_BUZZ_ACP_BINARY = "/mnt/data/services/buzz/target/release/buzz-acp"
_DEFAULT_MODELS_TIMEOUT_SECONDS = 15.0
_DEFAULT_MODELS_CACHE_TTL_SECONDS = 600.0
_DEFAULT_RESTART_WAIT_TIMEOUT_SECONDS = 30.0


class CommandResult:
    def __init__(self, returncode: int, stdout: str, stderr: str) -> None:
        self.returncode = returncode
        self.stdout = stdout
        self.stderr = stderr


class Runner(Protocol):
    def run(self, args: list[str], *, timeout: float | None = None) -> CommandResult: ...


class SubprocessRunner:
    """Run fixed argv vectors without invoking a shell."""

    def run(self, args: list[str], *, timeout: float | None = None) -> CommandResult:
        try:
            completed = subprocess.run(
                args,
                capture_output=True,
                text=True,
                check=False,
                timeout=timeout,
            )
        except subprocess.TimeoutExpired:
            return CommandResult(-1, "", "timeout")
        return CommandResult(completed.returncode, completed.stdout, completed.stderr)


def _validated_stem(stem: str) -> str:
    if _STEM_RE.fullmatch(stem) is None:
        raise HTTPException(status_code=400, detail={"code": "invalid_stem"})
    return stem


def _unit_for_stem(stem: str) -> str:
    _validated_stem(stem)
    unit = f"buzz-agent@{stem}.service"
    if _UNIT_RE.fullmatch(unit) is None:
        raise HTTPException(status_code=400, detail={"code": "invalid_stem"})
    return unit


def _read_env_lines(path: Path) -> list[str]:
    return path.read_text(encoding="utf-8").splitlines(keepends=True)


def _unquote(raw: str) -> str:
    if len(raw) >= 2 and raw[0] == raw[-1] and raw[0] in "\"'":
        return raw[1:-1]
    return raw


def _extract_known(lines: list[str]) -> dict[str, str]:
    """Pull only the whitelisted keys out of an env file's lines.

    Every other line (including any secret) is never parsed into a value that
    could later be echoed back through the API — it only ever survives as an
    opaque, unread line for the round-trip write in ``_set_env_value``.
    """
    values: dict[str, str] = {}
    for line in lines:
        match = _ENV_LINE_RE.match(line.rstrip("\n"))
        if match is None:
            continue
        key = match.group(1)
        if key in _KNOWN_KEYS:
            values[key] = _unquote(match.group(2))
    return values


def _set_env_value(lines: list[str], key: str, value: str) -> list[str]:
    """Replace ``key``'s line with a quoted ``value``, preserving every other line and order.

    Appends a new quoted line if ``key`` was absent (the config normally
    already has ``BUZZ_ACP_MODEL``, but a missing line should not be a hard
    failure).
    """
    quoted = json.dumps(value)
    result: list[str] = []
    replaced = False
    for line in lines:
        match = _ENV_LINE_RE.match(line.rstrip("\n"))
        if not replaced and match is not None and match.group(1) == key:
            newline = "\n" if line.endswith("\n") else ""
            result.append(f"{key}={quoted}{newline}")
            replaced = True
        else:
            result.append(line)
    if not replaced:
        if result and not result[-1].endswith("\n"):
            result[-1] += "\n"
        result.append(f"{key}={quoted}\n")
    return result


def _atomic_write(path: Path, content: str, mode: int) -> None:
    tmp = path.with_name(path.name + ".tmp")
    with tmp.open("w", encoding="utf-8") as handle:
        handle.write(content)
        handle.flush()
        os.fsync(handle.fileno())
    os.chmod(tmp, mode)
    os.replace(tmp, path)


def _parse_show_output(output: str) -> dict[str, str]:
    values: dict[str, str] = {}
    for line in output.splitlines():
        key, separator, value = line.partition("=")
        if separator:
            values[key] = value
    return values


def _parse_models_json(raw: str) -> list[dict[str, Any]] | None:
    """Extract the ``model`` config option's choices from ``buzz-acp models --json``.

    Returns ``None`` (never an empty guess) if the shape doesn't match what
    the live binary produces, so callers can distinguish "no model option
    reported" from "nothing there".
    """
    try:
        data = json.loads(raw)
    except json.JSONDecodeError:
        return None
    if not isinstance(data, dict):
        return None
    stable = data.get("stable")
    if not isinstance(stable, dict):
        return None
    config_options = stable.get("configOptions")
    if not isinstance(config_options, list):
        return None
    for option in config_options:
        if not isinstance(option, dict) or option.get("id") != "model":
            continue
        raw_options = option.get("options")
        if not isinstance(raw_options, list):
            return None
        models: list[dict[str, Any]] = []
        for entry in raw_options:
            if not isinstance(entry, dict):
                continue
            value = entry.get("value")
            if not isinstance(value, str):
                continue
            name = entry.get("name")
            description = entry.get("description")
            models.append({
                "value": value,
                "name": name if isinstance(name, str) else None,
                "description": description if isinstance(description, str) else None,
            })
        return models
    return None


class BuzzModelControlService:
    def __init__(
        self,
        *,
        runner: Runner | None = None,
        config_dir: Path | None = None,
        buzz_acp_binary: str = DEFAULT_BUZZ_ACP_BINARY,
        models_timeout: float = _DEFAULT_MODELS_TIMEOUT_SECONDS,
        models_cache_ttl: float = _DEFAULT_MODELS_CACHE_TTL_SECONDS,
        restart_wait_timeout: float = _DEFAULT_RESTART_WAIT_TIMEOUT_SECONDS,
        sleep: Callable[[float], None] = time.sleep,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self.runner = runner or SubprocessRunner()
        self.config_dir = config_dir or Path.home() / ".config" / "buzz-agents"
        self.buzz_acp_binary = buzz_acp_binary
        self.models_timeout = models_timeout
        self.models_cache_ttl = models_cache_ttl
        self.restart_wait_timeout = restart_wait_timeout
        self._sleep = sleep
        self._clock = clock
        self._models_cache: dict[str, tuple[float, list[dict[str, Any]]]] = {}

    def _env_path(self, stem: str) -> Path:
        _validated_stem(stem)
        path = self.config_dir / f"{stem}.env"
        if not path.is_file():
            raise HTTPException(status_code=404, detail={"code": "unknown_agent"})
        return path

    def _known_values(self, stem: str) -> dict[str, str]:
        return _extract_known(_read_env_lines(self._env_path(stem)))

    def list_agents(self) -> list[dict[str, Any]]:
        stems = sorted(
            path.name.removesuffix(".env")
            for path in self.config_dir.glob("*.env")
            if path.is_file() and _STEM_RE.fullmatch(path.name.removesuffix(".env")) is not None
        )
        return [self._agent_summary(stem) for stem in stems]

    def _agent_summary(self, stem: str) -> dict[str, Any]:
        values = self._known_values(stem)
        unit = _unit_for_stem(stem)
        show = self.runner.run([
            "systemctl",
            "--user",
            "show",
            unit,
            "--property=ActiveState,ActiveEnterTimestamp",
            "--no-pager",
        ])
        props = _parse_show_output(show.stdout) if show.returncode == 0 else {}
        last_start = props.get("ActiveEnterTimestamp") or None
        if last_start in ("", "n/a"):
            last_start = None
        return {
            "stem": stem,
            "display_name": stem.capitalize(),
            "model": values.get(_MODEL_KEY),
            "agent_command": values.get(_COMMAND_KEY),
            "active_state": props.get("ActiveState") or "unknown",
            "last_start": last_start,
        }

    def _cached_models(self, stem: str) -> list[dict[str, Any]] | None:
        entry = self._models_cache.get(stem)
        if entry is None:
            return None
        fetched_at, models = entry
        if self._clock() - fetched_at > self.models_cache_ttl:
            return None
        return models

    def get_models(self, stem: str) -> dict[str, Any]:
        values = self._known_values(stem)
        current_model = values.get(_MODEL_KEY)
        cached = self._cached_models(stem)
        if cached is not None:
            return {
                "stem": stem,
                "current_model": current_model,
                "models": cached,
                "cached": True,
                "error": None,
            }
        command = values.get(_COMMAND_KEY)
        if not command:
            return {
                "stem": stem,
                "current_model": current_model,
                "models": None,
                "cached": False,
                "error": {
                    "code": "missing_agent_command",
                    "detail": f"{_COMMAND_KEY} is not set for this agent",
                },
            }
        args = values.get(_ARGS_KEY, "")
        result = self.runner.run(
            [
                self.buzz_acp_binary,
                "models",
                "--agent-command",
                command,
                "--agent-args",
                args,
                "--json",
            ],
            timeout=self.models_timeout,
        )
        if result.returncode != 0:
            return {
                "stem": stem,
                "current_model": current_model,
                "models": None,
                "cached": False,
                "error": {
                    "code": "models_probe_failed",
                    "detail": f"buzz-acp models exited {result.returncode}",
                },
            }
        models = _parse_models_json(result.stdout)
        if models is None:
            return {
                "stem": stem,
                "current_model": current_model,
                "models": None,
                "cached": False,
                "error": {
                    "code": "models_probe_unparseable",
                    "detail": "buzz-acp models --json did not report a model option",
                },
            }
        self._models_cache[stem] = (self._clock(), models)
        return {
            "stem": stem,
            "current_model": current_model,
            "models": models,
            "cached": False,
            "error": None,
        }

    def set_model(self, stem: str, requested_model: str) -> dict[str, Any]:
        if not requested_model or not requested_model.strip():
            raise HTTPException(status_code=400, detail={"code": "empty_model"})
        path = self._env_path(stem)
        lines = _read_env_lines(path)
        old_model = _extract_known(lines).get(_MODEL_KEY)
        # Check against whatever model list is already cached (if any) before
        # invalidating it — the write below must not force a fresh, slow
        # buzz-acp probe just to answer "have we seen this id before".
        previously_probed = self._cached_models(stem)
        unverified = previously_probed is None or not any(
            model.get("value") == requested_model for model in previously_probed
        )
        new_lines = _set_env_value(lines, _MODEL_KEY, requested_model)
        mode = path.stat().st_mode & 0o777
        _atomic_write(path, "".join(new_lines), mode)
        self._models_cache.pop(stem, None)

        unit = _unit_for_stem(stem)
        restart = self._restart_and_wait(unit)
        return {
            "stem": stem,
            "old_model": old_model,
            "new_model": requested_model,
            "restart": restart,
            "unverified": unverified,
        }

    def _restart_and_wait(self, unit: str) -> dict[str, Any]:
        marker = time.time()
        restart = self.runner.run(["systemctl", "--user", "restart", unit])
        if restart.returncode != 0:
            return {"restarted": False, "ready": False, "error": "restart_failed"}

        deadline = self._clock() + self.restart_wait_timeout
        while True:
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
                    _READINESS_MARKER,
                ])
                if journal.returncode == 0 and _READINESS_MARKER in journal.stdout:
                    return {"restarted": True, "ready": True, "error": None}
            if self._clock() >= deadline:
                return {"restarted": True, "ready": False, "error": "readiness_timeout"}
            self._sleep(min(0.25, self.restart_wait_timeout))


_default_service: BuzzModelControlService | None = None


def _get_default_service() -> BuzzModelControlService:
    global _default_service
    if _default_service is None:
        _default_service = BuzzModelControlService()
    return _default_service


class ModelUpdateBody(BaseModel):
    model: str


def register_buzz_model_control_routes(
    app: FastAPI,
    *,
    service_factory: Callable[[], BuzzModelControlService] = _get_default_service,
) -> None:
    """Register read/list/set model-control endpoints before the SPA catch-all."""

    @app.get("/api/buzz/agents")
    def list_buzz_agents() -> dict[str, Any]:
        return {"agents": service_factory().list_agents()}

    @app.get("/api/buzz/agents/{stem}/models")
    def get_buzz_agent_models(stem: str) -> dict[str, Any]:
        return service_factory().get_models(stem)

    @app.post("/api/buzz/agents/{stem}/model")
    def set_buzz_agent_model(stem: str, body: ModelUpdateBody) -> dict[str, Any]:
        return service_factory().set_model(stem, body.model)
