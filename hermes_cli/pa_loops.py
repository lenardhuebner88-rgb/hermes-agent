"""Gated PA actions for starting and observing Loop-Runner packs.

The runner remains the source of truth for pack manifests and phase names.  This
adapter adds the narrower PA allowlist, validates a singular model against the
catalogued engine phases, writes truly-one-run overrides, and captures bounded
operator evidence.  It deliberately does not set ``HERMES_SANDBOX_MODE``.
"""

from __future__ import annotations

import json
import fcntl
import re
import subprocess
import time
from collections import deque
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import yaml

from hermes_constants import get_hermes_home
from loops import runner as loop_runner

PACKS_DIR_OVERRIDE: Path | None = None
MODELS_FILE_OVERRIDE: Path | None = None
STATE_ROOT_OVERRIDE: Path | None = None

_PACK_NAME_RE = re.compile(r"^[a-z0-9][a-z0-9-]{0,63}$")
_SYSTEMCTL_TIMEOUT_S = 30
_LEDGER_TAIL_LINES = 20
_OUTPUT_MAX_CHARS = 4_000
_FAST_FAIL_PROBE_S = 0.6


class LoopActionError(ValueError):
    """A requested loop action is invalid or cannot be evidenced safely."""


def _repo_packs_dir() -> Path:
    return PACKS_DIR_OVERRIDE or loop_runner.PACKS_DIR


def _custom_packs_dir() -> Path:
    return get_hermes_home() / "loops" / "packs-custom"


def _state_root() -> Path:
    return STATE_ROOT_OVERRIDE or (get_hermes_home() / "loops")


def _models_file() -> Path:
    return MODELS_FILE_OVERRIDE or loop_runner.MODELS_FILE


def _pack_names_in(base: Path) -> set[str]:
    if not base.is_dir():
        return set()
    return {
        entry.name
        for entry in base.iterdir()
        if entry.is_dir()
        and _PACK_NAME_RE.fullmatch(entry.name)
        and (entry / "pack.yaml").is_file()
    }


def known_pack_names() -> list[str]:
    """Return manifest-backed repo/custom packs accepted by the PA allowlist."""
    return sorted(_pack_names_in(_repo_packs_dir()) | _pack_names_in(_custom_packs_dir()))


def _valid_pack_hint() -> str:
    names = known_pack_names()
    return ", ".join(names) if names else "(keine)"


def resolve_pack(name: str) -> loop_runner.Pack:
    """Resolve exactly like the runner (repo then packs-custom), fail closed."""
    if not isinstance(name, str) or not _PACK_NAME_RE.fullmatch(name):
        raise LoopActionError(
            f"Pack-Name ungültig: {name!r}; gültige Packs: {_valid_pack_hint()}"
        )
    repo = _repo_packs_dir()
    custom = _custom_packs_dir()
    in_repo = (repo / name / "pack.yaml").is_file()
    in_custom = (custom / name / "pack.yaml").is_file()
    if in_repo and in_custom:
        raise LoopActionError(
            f"Pack {name!r} existiert doppelt (Repo + packs-custom); "
            f"gültige Packs: {_valid_pack_hint()}"
        )
    if not in_repo and not in_custom:
        raise LoopActionError(
            f"Unbekanntes Pack {name!r}; gültige Packs: {_valid_pack_hint()}"
        )
    try:
        return loop_runner.load_pack(custom if in_custom else repo, name)
    except loop_runner.ManifestError as exc:
        raise LoopActionError(str(exc)) from exc


def _model_catalog() -> dict[str, set[str]]:
    path = _models_file()
    try:
        raw = yaml.safe_load(path.read_text(encoding="utf-8"))
    except OSError as exc:
        raise LoopActionError(f"Loop-Modellkatalog nicht lesbar: {path}: {exc}") from exc
    except yaml.YAMLError as exc:
        raise LoopActionError(f"Loop-Modellkatalog ist ungültiges YAML: {path}: {exc}") from exc
    engines = raw.get("engines") if isinstance(raw, dict) else None
    if not isinstance(engines, dict):
        raise LoopActionError(f"Loop-Modellkatalog hat kein engines-Mapping: {path}")
    catalog: dict[str, set[str]] = {}
    for engine, config in engines.items():
        models = config.get("models") if isinstance(config, dict) else None
        if not isinstance(engine, str) or not isinstance(models, list):
            continue
        catalog[engine] = {model for model in models if isinstance(model, str) and model}
    return catalog


def _start_overrides(pack: loop_runner.Pack, payload: dict[str, Any]) -> list[str]:
    lines: list[str] = []
    model = payload.get("model")
    if model is not None:
        catalog = _model_catalog()
        all_models = sorted({item for models in catalog.values() for item in models})
        if model not in all_models:
            raise LoopActionError(
                f"Unbekanntes Loop-Modell {model!r}; Katalogmodelle: "
                + (", ".join(all_models) if all_models else "(keine)")
            )
        matching_phases = [
            phase
            for phase, config in pack.phases.items()
            if model in catalog.get(config.engine, set())
        ]
        if not matching_phases:
            engines = ", ".join(
                f"{phase}={config.engine}" for phase, config in pack.phases.items()
            )
            raise LoopActionError(
                f"Modell {model!r} passt zu keiner Engine-Phase von Pack "
                f"{pack.name!r} ({engines})"
            )
        lines.extend(f"PHASE_{phase.upper()}_MODEL={model}" for phase in matching_phases)
    if "max_rounds" in payload:
        lines.append(f"MAX_ROUNDS={payload['max_rounds']}")
    return lines


def validate_start_payload(payload: dict[str, Any]) -> tuple[loop_runner.Pack, list[str]]:
    pack = resolve_pack(payload["pack"])
    return pack, _start_overrides(pack, payload)


def build_action_question(category: str, envelope: dict[str, Any]) -> str:
    """Build category-specific, operator-readable card text for ``loops.*``."""
    payload = envelope.get("payload") if isinstance(envelope, dict) else None
    if not isinstance(payload, dict):
        raise LoopActionError(f"{category} braucht ein Payload-Objekt")
    if category == "loops.start_pack":
        pack, override_lines = validate_start_payload(payload)
        model = payload.get("model") or "Pack-Defaults"
        lines = [
            "Nachtlauf-Pack jetzt einmalig starten?",
            f"Pack: `{pack.name}`",
            f"Modell: `{model}`",
            "One-Run: startet sofort; optionale Overrides gelten nur für diesen Lauf.",
        ]
        if override_lines:
            lines.append("One-Run-Overrides: " + " · ".join(override_lines))
    elif category == "loops.status":
        requested = payload.get("pack")
        if requested:
            pack = resolve_pack(requested)
            scope = f"`{pack.name}`"
        else:
            scope = "alle bekannten Packs"
        lines = [
            "Loop-Status read-only abrufen?",
            f"Packs: {scope}",
            "Evidenz: letzte 20 LEDGER-Zeilen, Heartbeat-Phase/-Alter und STOP-Datei.",
        ]
    else:
        raise LoopActionError(f"Unbekannte Loop-Aktionskategorie: {category}")
    reason = payload.get("reason") or envelope.get("reason")
    if reason:
        lines.append(f"Grund: {str(reason)[:500]}")
    return "\n".join(lines)


def _bounded_output(value: str | None) -> str:
    return (value or "")[-_OUTPUT_MAX_CHARS:]


def _is_running(state: Path) -> bool:
    """Mirror the runner/dashboard flock probe without mutating pack state."""
    lock = state / ".lock"
    if not lock.exists():
        return False
    try:
        with lock.open("r+", encoding="utf-8") as handle:
            try:
                fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError:
                return True
            fcntl.flock(handle, fcntl.LOCK_UN)
    except OSError:
        return False
    return False


def _systemctl_call(argv: list[str]) -> tuple[subprocess.CompletedProcess[str] | None, dict[str, Any]]:
    """Run one bounded systemctl command and normalize its evidence."""
    try:
        completed = subprocess.run(
            argv,
            capture_output=True,
            encoding="utf-8",
            errors="replace",
            timeout=_SYSTEMCTL_TIMEOUT_S,
            check=False,
        )
    except subprocess.TimeoutExpired as exc:
        return None, {
            "argv": argv,
            "exit": 124,
            "stdout": _bounded_output(exc.stdout if isinstance(exc.stdout, str) else ""),
            "stderr": _bounded_output(exc.stderr if isinstance(exc.stderr, str) else ""),
            "error": f"Timeout nach {_SYSTEMCTL_TIMEOUT_S}s",
        }
    except OSError as exc:
        return None, {
            "argv": argv,
            "exit": 127,
            "stdout": "",
            "stderr": _bounded_output(str(exc)),
            "error": str(exc),
        }
    return completed, {
        "argv": argv,
        "exit": completed.returncode,
        "stdout": _bounded_output(completed.stdout),
        "stderr": _bounded_output(completed.stderr),
    }


def start_pack(payload: dict[str, Any]) -> dict[str, Any]:
    """Write optional one-run overrides and issue exactly one detached start."""
    try:
        pack, override_lines = validate_start_payload(payload)
    except (LoopActionError, OSError) as exc:
        return {"ok": False, "exit": 1, "error": str(exc)}

    state = _state_root() / pack.name
    if _is_running(state):
        return {
            "ok": False,
            "exit": 1,
            "error": f"Loop-Pack {pack.name!r} läuft bereits; kein Start ausgeführt",
        }

    path = state / "overrides.env"
    content = (
        "# geschrieben vom Jarvis PA Executor; gilt für genau einen Lauf\n"
        + "\n".join(override_lines)
        + ("\n" if override_lines else "")
    )
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        # Also replace a stale, unconsumed file when this start requests pack
        # defaults.  Otherwise an earlier failed start could leak its settings
        # into this operator-confirmed run.
        path.write_text(content, encoding="utf-8")
    except OSError as exc:
        return {
            "ok": False,
            "exit": 1,
            "error": f"One-Run-Overrides konnten nicht geschrieben werden: {exc}",
        }
    override_evidence: dict[str, Any] = {"path": str(path), "content": content}

    argv = [
        "systemctl",
        "--user",
        "start",
        "--no-block",
        f"hermes-loop@{pack.name}",
    ]
    unit = f"hermes-loop@{pack.name}.service"
    systemctl_output: dict[str, Any] = {}

    reset_argv = ["systemctl", "--user", "reset-failed", unit]
    reset, reset_evidence = _systemctl_call(reset_argv)
    systemctl_output["reset_failed"] = reset_evidence
    if reset is None or reset.returncode != 0:
        return {
            "ok": False,
            "exit": reset_evidence["exit"],
            "error": "systemctl reset-failed fehlgeschlagen: "
            + (
                reset_evidence.get("error")
                or reset_evidence["stderr"]
                or reset_evidence["stdout"]
                or "keine Ausgabe"
            ),
            "argv": argv,
            "systemctl_output": systemctl_output,
            "overrides": override_evidence,
        }

    completed, start_evidence = _systemctl_call(argv)
    systemctl_output["start"] = start_evidence
    if completed is None or completed.returncode != 0:
        return {
            "ok": False,
            "exit": start_evidence["exit"],
            "error": "systemctl start fehlgeschlagen: "
            + (
                start_evidence.get("error")
                or start_evidence["stderr"]
                or start_evidence["stdout"]
                or "keine Ausgabe"
            ),
            "argv": argv,
            "systemctl_output": systemctl_output,
            "overrides": override_evidence,
        }

    time.sleep(_FAST_FAIL_PROBE_S)
    probe_argv = ["systemctl", "--user", "is-active", unit]
    probe, probe_evidence = _systemctl_call(probe_argv)
    systemctl_output["is_active"] = probe_evidence
    active_state = probe_evidence["stdout"].strip()
    if probe is None or active_state == "failed" or (
        probe.returncode not in {0, 3} and not active_state
    ):
        detail = (
            probe_evidence.get("error")
            or probe_evidence["stderr"]
            or active_state
            or "keine Ausgabe"
        )
        return {
            "ok": False,
            "exit": 1,
            "error": f"Loop-Unit ist nach dem Start nicht angelaufen: {detail}",
            "pack": pack.name,
            "argv": argv,
            "systemctl_output": systemctl_output,
            "overrides": override_evidence,
            "one_run": True,
        }

    result = {
        "ok": True,
        "exit": completed.returncode,
        "pack": pack.name,
        "argv": argv,
        "systemctl_output": systemctl_output,
        "overrides": override_evidence,
        "one_run": True,
    }
    return result


def _tail_lines(path: Path, count: int) -> list[str]:
    if not path.is_file():
        return []
    with path.open("r", encoding="utf-8", errors="replace") as handle:
        return list(deque((line.rstrip("\n") for line in handle), maxlen=count))


def _age_seconds(value: Any, now: datetime) -> int | None:
    if not isinstance(value, str) or not value:
        return None
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=timezone.utc)
    except ValueError:
        return None
    return max(0, int((now - parsed.astimezone(timezone.utc)).total_seconds()))


def _heartbeat_evidence(path: Path, now: datetime) -> dict[str, Any]:
    evidence: dict[str, Any] = {
        "path": str(path),
        "present": path.is_file(),
        "phase": None,
        "age_seconds": None,
        "active": False,
    }
    if not path.is_file():
        return evidence
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        evidence["error"] = f"heartbeat.json nicht lesbar: {exc}"
        return evidence
    if not isinstance(raw, dict):
        evidence["error"] = "heartbeat.json muss ein JSON-Objekt sein"
        return evidence
    current = raw.get("current")
    last = raw.get("last")
    sample: dict[str, Any] | None = current if isinstance(current, dict) else None
    timestamp_key = "started_at"
    if sample is None and isinstance(last, list):
        sample = next((item for item in reversed(last) if isinstance(item, dict)), None)
        timestamp_key = "at"
    if sample is not None:
        evidence.update(
            {
                "phase": sample.get("phase"),
                "age_seconds": _age_seconds(sample.get(timestamp_key), now),
                "active": isinstance(current, dict),
                "sample": sample,
            }
        )
    return evidence


def _status_for_pack(name: str, now: datetime) -> dict[str, Any]:
    pack = resolve_pack(name)
    state = _state_root() / pack.name
    ledger_path = state / "LEDGER.md"
    heartbeat_path = state / "heartbeat.json"
    stop_path = state / "STOP"
    return {
        "pack": pack.name,
        "ledger": {
            "path": str(ledger_path),
            "present": ledger_path.is_file(),
            "lines": _tail_lines(ledger_path, _LEDGER_TAIL_LINES),
        },
        "heartbeat": _heartbeat_evidence(heartbeat_path, now),
        "stop": {"path": str(stop_path), "exists": stop_path.exists()},
    }


def status(payload: dict[str, Any]) -> dict[str, Any]:
    """Read bounded state evidence for one or all manifest-backed packs."""
    requested = payload.get("pack")
    names = [requested] if requested else known_pack_names()
    now = datetime.now(timezone.utc)
    packs: list[dict[str, Any]] = []
    errors: list[str] = []
    for name in names:
        try:
            packs.append(_status_for_pack(name, now))
        except (LoopActionError, OSError) as exc:
            errors.append(str(exc))
    result: dict[str, Any] = {
        "ok": not errors,
        "exit": 0 if not errors else 1,
        "packs": packs,
        "ledger_tail_limit": _LEDGER_TAIL_LINES,
    }
    if errors:
        result["error"] = "; ".join(errors)
    return result
