"""Loops-Bereich des /control-Dashboards — API über dem Loop-Runner.

Read-Side liest Pack-Manifeste (loops/packs/) + Laufzeit-State (~/.hermes/loops/<pack>/);
Write-Side schreibt overrides.env/STOP-Datei und schaltet die systemd-Units
(hermes-loop@<pack>.service/.timer, siehe loops/systemd/). Auth kommt automatisch
aus der Dashboard-Middleware (jeder /api/*-Pfad) — hier ist nichts zu tun, solange
kein Pfad in die Public-Whitelist eingetragen wird.

Namespace-Hinweis: Dieses Modul heißt bewusst NICHT `loops` — das Top-Level-Package
`loops/` ist der Runner selbst (`python -m loops.runner`).

Design (bindend): vault/03-Agents/Claude-Code/plans/2026-07-02-loop-runner-v1-v2.md
(v1: Start/Stop/Timer/Status + Modell-Overrides; Landung bleibt Morgen-Review).
"""

from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager

import fcntl
import json
import os
import re
import shlex
import shutil
import sqlite3
import subprocess
import sys
import tempfile
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

import yaml
from fastapi import FastAPI, HTTPException
from pydantic import BaseModel

try:
    from loops import runner as loop_runner
except ModuleNotFoundError:  # editable install paketiert loops/ nicht → Repo-Root nachschieben
    import sys

    sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
    from loops import runner as loop_runner

# Test-Seams: Tests setzen diese Overrides, statt echte Repo-/State-Pfade zu nutzen.
PACKS_DIR_OVERRIDE: Path | None = None
STATE_ROOT_OVERRIDE: Path | None = None
MODELS_PATH_OVERRIDE: Path | None = None
SYSTEMD_USER_DIR_OVERRIDE: Path | None = None

DEFAULT_TIMER_SCHEDULE = "23:37"
_TIMER_SCHEDULE_RE = re.compile(r"^(?:[01][0-9]|2[0-3]):[0-5][0-9]$")
_TIMER_ON_CALENDAR_RE = re.compile(
    r"OnCalendar=\*-\*-\* ((?:[01][0-9]|2[0-3]):[0-5][0-9]):00(?:\s|;|$)",
)

# Mutationen nur für reguläre Packs — Unterstrich-Packs (_blank) sind Vorlagen.
# Muss dieselben Namen zulassen wie runner._PACK_NAME_RE (minus führenden Unterstrich),
# sonst sind Packs sichtbar, aber nicht bedienbar.
_PACK_NAME_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_-]{0,63}$")

# overrides.env-Whitelist: die festen Runner-Knobs; Pack-PARAMS werden dynamisch
# gegen das Manifest validiert (siehe start_loop) — so funktioniert jeder Pack-
# Parameter (focus/fokus/services/…) statt eines hartkodierten FOCUS-Felds.
_OVERRIDE_KEY_RE = re.compile(
    r"(PHASE_[A-Z]+_(MODEL|ENGINE|TIMEOUT|EFFORT)"
    r"|MAX_ROUNDS|MAX_HOURS|FAIL_STREAK|DRY_ROUNDS|DISCORD_CHANNEL|SKIP_PLAN"
    r"|AUTOLAND|AUTOLAND_ACK)"
)
_OVERRIDE_VALUE_RE = re.compile(r"[^\r\n\x00]{0,400}")
# Night-Overrides speichern Engine/Model-IDs — enger als One-Shot (kein Shell-Metachar).
_NIGHT_OVERRIDE_VALUE_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._+-]{0,199}$")
_NIGHT_OVERRIDE_KEY_RE = re.compile(r"^PHASE_[A-Z]+_(ENGINE|MODEL|EFFORT)$")
NIGHT_OVERRIDES_FILENAME = "night-overrides.env"
LANDING_AUTOMATION_FILENAME = "automation.json"
LANDING_TRIGGER_FILENAME = "trigger-state.json"
LANDING_RUNTIME_FILENAME = "runtime-status.json"
LANDING_COLLECTION_WINDOW_SECONDS = 600
# Manueller Dry-Run (Vorschau) — Ergebnis landet als Datei im Landing-State-Dir,
# die Detail-Route liest es additiv ein (LL2-S5).
LANDING_PREVIEW_STATE_FILENAME = "preview-state.json"
LANDING_PREVIEW_DONE_FILENAME = "preview-done.json"
LANDING_PREVIEW_OUTPUT_FILENAME = "preview-output.txt"
LANDING_PREVIEW_OUTPUT_TAIL = 4000
# Älter als das ohne done-Marker ⇒ Preview-Prozess gilt als verwaist (Crash/Restart).
LANDING_PREVIEW_STALE_SECONDS = 900
LANDING_RECOVERY_EVENT_LIMIT = 20


def _packs_dir() -> Path:
    return PACKS_DIR_OVERRIDE or loop_runner.PACKS_DIR


def _dir_for(name: str) -> Path:
    """Pack-Verzeichnis auflösen: Test-Override gewinnt, sonst Repo→Custom-Suchpfad."""
    if PACKS_DIR_OVERRIDE is not None:
        return PACKS_DIR_OVERRIDE
    return loop_runner.resolve_packs_dir(name)


def _all_pack_names() -> list[tuple[str, str]]:
    """(name, source) über Repo- und Custom-Packs; Unterstrich-Vorlagen bleiben unsichtbar."""
    if PACKS_DIR_OVERRIDE is not None:
        dirs = [(PACKS_DIR_OVERRIDE, "repo")]
    else:
        dirs = [(loop_runner.PACKS_DIR, "repo"), (loop_runner.CUSTOM_PACKS_DIR, "custom")]
    seen: dict[str, str] = {}
    for base, source in dirs:
        if not base.is_dir():
            continue
        for p in sorted(base.iterdir()):
            if p.is_dir() and not p.name.startswith("_") and p.name not in seen:
                seen[p.name] = source
    return sorted(seen.items())


def _state_root() -> Path:
    return STATE_ROOT_OVERRIDE or loop_runner.DEFAULT_STATE_ROOT


def _models_path() -> Path:
    return MODELS_PATH_OVERRIDE or (loop_runner.REPO_ROOT / "loops" / "models.yaml")


def _systemctl(*args: str) -> subprocess.CompletedProcess:
    """Seam für systemd-Aufrufe (Tests monkeypatchen genau diese Funktion)."""
    command = ["systemctl", "--user", *args]
    try:
        return subprocess.run(
            command,
            capture_output=True, encoding="utf-8", errors="replace",
            timeout=30, check=False,
        )
    except OSError as exc:
        return subprocess.CompletedProcess(command, 127, stdout="", stderr=str(exc))


def _systemd_user_dir() -> Path:
    """User-systemd ist absichtlich HOME-verankert, nicht HERMES_HOME-verankert."""
    return SYSTEMD_USER_DIR_OVERRIDE or (Path.home() / ".config" / "systemd" / "user")


def _timer_unit(name: str) -> str:
    return f"hermes-loop@{name}.timer"


def _timer_dropin_path(name: str) -> Path:
    return _systemd_user_dir() / f"{_timer_unit(name)}.d" / "schedule.conf"


@contextmanager
def _timer_mutation_lock(name: str) -> Iterator[None]:
    """Toggle und Schedule-Write desselben Packs dürfen sich nicht überholen."""
    lock_path = _state_root() / name / ".timer-schedule.lock"
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    with lock_path.open("a+", encoding="utf-8") as fh:
        fcntl.flock(fh, fcntl.LOCK_EX)
        try:
            yield
        finally:
            fcntl.flock(fh, fcntl.LOCK_UN)


@contextmanager
def _night_overrides_lock(name: str) -> Iterator[None]:
    """Night-Overrides pro Pack atomar ersetzen."""
    lock_path = _state_root() / name / ".night-overrides.lock"
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    with lock_path.open("a+", encoding="utf-8") as fh:
        fcntl.flock(fh, fcntl.LOCK_EX)
        try:
            yield
        finally:
            fcntl.flock(fh, fcntl.LOCK_UN)


def _night_overrides_path(name: str) -> Path:
    return _state_root() / name / NIGHT_OVERRIDES_FILENAME


@contextmanager
def _landing_state_lock(state_dir: Path | None = None) -> Iterator[None]:
    """Serialize the landing automation, trigger, and runtime state files."""
    root = state_dir or (_state_root() / "landing")
    lock_path = root / ".automation.lock"
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    with lock_path.open("a+", encoding="utf-8") as fh:
        fcntl.flock(fh, fcntl.LOCK_EX)
        try:
            yield
        finally:
            fcntl.flock(fh, fcntl.LOCK_UN)


def _read_json_object(path: Path) -> dict[str, Any]:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError, TypeError):
        return {}
    return value if isinstance(value, dict) else {}


def _landing_state_dir(state_dir: Path | None = None) -> Path:
    return state_dir or (_state_root() / "landing")


def get_landing_automation_enabled(state_dir: Path | None = None) -> bool:
    """Read the persistent kill switch fail-closed and without side effects."""
    value = _read_json_object(
        _landing_state_dir(state_dir) / LANDING_AUTOMATION_FILENAME
    )
    return value.get("schema_version") == 1 and value.get("enabled") is True


def set_landing_automation_enabled(
    enabled: bool,
    *,
    updated_by: str,
    state_dir: Path | None = None,
    now: datetime | None = None,
) -> dict[str, Any]:
    """Atomically persist the operator-controlled landing kill switch."""
    if not isinstance(enabled, bool):
        raise ValueError("enabled must be a boolean")
    if not updated_by.strip():
        raise ValueError("updated_by must not be empty")
    root = _landing_state_dir(state_dir)
    payload = {
        "schema_version": 1,
        "enabled": enabled,
        "updated_at": (now or datetime.now(timezone.utc)).isoformat(),
        "updated_by": updated_by,
    }
    with _landing_state_lock(root):
        _atomic_write(
            root / LANDING_AUTOMATION_FILENAME,
            (json.dumps(payload, sort_keys=True) + "\n").encode("utf-8"),
        )
    return payload


def register_landing_trigger(
    signal_id: str,
    *,
    running: bool,
    state_dir: Path | None = None,
    now: datetime | None = None,
) -> dict[str, Any]:
    """Register one signal without extending an open fixed collection window."""
    root = _landing_state_dir(state_dir)
    timestamp = now or datetime.now(timezone.utc)
    with _landing_state_lock(root):
        state = _read_json_object(root / LANDING_TRIGGER_FILENAME)
        if not get_landing_automation_enabled(root):
            return {"accepted": False, "reason": "automation_disabled"}
        seen = [item for item in state.get("seen_signals", []) if isinstance(item, str)]
        if signal_id in seen:
            return {**state, "accepted": False, "reason": "duplicate"}
        seen = (seen + [signal_id])[-256:]
        if running:
            accepted = not bool(state.get("followup_pending"))
            state["followup_pending"] = True
            reason = "followup_queued" if accepted else "followup_already_queued"
        else:
            window = state.get("collection_window")
            closes_at = None
            if isinstance(window, dict):
                try:
                    closes_at = datetime.fromisoformat(str(window.get("closes_at")))
                except ValueError:
                    closes_at = None
            if closes_at is None or timestamp >= closes_at:
                closes_at = timestamp + timedelta(seconds=LANDING_COLLECTION_WINDOW_SECONDS)
                window = {
                    "opened_at": timestamp.isoformat(),
                    "closes_at": closes_at.isoformat(),
                }
                reason = "window_opened"
            else:
                reason = "window_collected"
            state["collection_window"] = window
            state["next_trigger_at"] = closes_at.isoformat()
            accepted = True
        state.update({"schema_version": 1, "seen_signals": seen})
        _atomic_write(
            root / LANDING_TRIGGER_FILENAME,
            (json.dumps(state, sort_keys=True) + "\n").encode("utf-8"),
        )
    return {**state, "accepted": accepted, "reason": reason}


def consume_landing_followup(state_dir: Path | None = None) -> bool:
    """Consume the single pending follow-up run after an active run finishes."""
    root = _landing_state_dir(state_dir)
    with _landing_state_lock(root):
        path = root / LANDING_TRIGGER_FILENAME
        state = _read_json_object(path)
        pending = state.get("followup_pending") is True
        if pending:
            state["followup_pending"] = False
            _atomic_write(
                path,
                (json.dumps(state, sort_keys=True) + "\n").encode("utf-8"),
            )
    return pending


def write_landing_runtime_status(
    status: dict[str, Any], *, state_dir: Path | None = None
) -> None:
    root = _landing_state_dir(state_dir)
    with _landing_state_lock(root):
        _atomic_write(
            root / LANDING_RUNTIME_FILENAME,
            (json.dumps({"schema_version": 1, **status}, sort_keys=True) + "\n").encode(
                "utf-8"
            ),
        )


def _landing_control_payload(state_dir: Path | None = None) -> dict[str, Any]:
    root = _landing_state_dir(state_dir)
    runtime = _read_json_object(root / LANDING_RUNTIME_FILENAME)
    trigger = _read_json_object(root / LANDING_TRIGGER_FILENAME)
    return {
        "automation_enabled": get_landing_automation_enabled(root),
        "baseline_sha": runtime.get("baseline_sha"),
        "baseline_ok": runtime.get("baseline_ok"),
        "baseline_reason": runtime.get("baseline_reason"),
        "baseline_source": runtime.get("baseline_source"),
        "queue_summary": runtime.get("queue_summary", {}),
        "next_trigger_at": trigger.get("next_trigger_at"),
        "last_result": runtime.get("last_result"),
        "collection_window": trigger.get("collection_window"),
        "candidates": runtime.get("candidates", []),
    }


def _landing_repo() -> Path:
    """Repo-Pfad des Landing-Packs — identisch zur pack.yaml/landing_loop-Default."""
    try:
        from hermes_cli.landing_loop import HERMES_REPO

        return Path(HERMES_REPO)
    except Exception:  # landing_loop nicht importierbar → hermes_home-Default
        from hermes_cli.hermes_constants import get_hermes_home

        return get_hermes_home() / "hermes-agent"


def _landing_gate_stages() -> list[str]:
    """Statische Gate-Reihenfolge aus landing_loop (lazy: landing_loop importiert uns)."""
    try:
        from hermes_cli.landing_loop import GateStage
    except Exception:
        return []
    return [stage.value for stage in GateStage]


def _landing_recovery_entries(limit: int = LANDING_RECOVERY_EVENT_LIMIT) -> list[dict[str, Any]]:
    """Recovery-Anfragen/-Versuche aus dem Kanban-Event-Ledger (read-only).

    Pro (task_id, fingerprint) genau ein Eintrag mit dem weitesten Zustand
    (requested → started → exhausted) — so sieht der Drawer Fingerprint und
    Versuch, ohne das Board zu duplizieren. Fehler (fehlende DB, fehlende
    Tabelle) sind bewusst fail-open: der Rest des Detail-Payloads bleibt nutzbar.
    """
    try:
        from hermes_cli import kanban_db as _kanban_db

        db_path = Path(_kanban_db.kanban_db_path())
    except Exception:
        return []
    if not db_path.is_file():
        return []
    kinds = (
        "landing_recovery_requested",
        "landing_recovery_started",
        "landing_recovery_exhausted",
    )
    try:
        conn = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
    except sqlite3.Error:
        return []
    try:
        rows = conn.execute(
            "SELECT task_id, kind, payload, created_at FROM task_events "
            "WHERE kind IN (?, ?, ?) ORDER BY id",
            kinds,
        ).fetchall()
    except sqlite3.Error:
        return []
    finally:
        conn.close()

    grouped: dict[tuple[str, str], dict[str, Any]] = {}
    for task_id, kind, payload_raw, created_at in rows:
        try:
            payload = json.loads(payload_raw) if payload_raw else {}
        except (TypeError, ValueError):
            payload = {}
        fingerprint = str(payload.get("fingerprint") or "")
        key = (str(task_id), fingerprint)
        entry = grouped.setdefault(
            key,
            {
                "task_id": str(task_id),
                "fingerprint": fingerprint,
                "failure_class": str(payload.get("failure_class") or ""),
                "failing_gate": str(payload.get("failing_gate") or ""),
                "candidate_commit": str(payload.get("candidate_commit") or ""),
                "state": "requested",
                "requested_at": None,
                "started_at": None,
            },
        )
        if kind == "landing_recovery_requested":
            entry["requested_at"] = payload.get("requested_at") or entry["requested_at"]
            for field in ("failure_class", "failing_gate", "candidate_commit"):
                if payload.get(field):
                    entry[field] = str(payload[field])
        elif kind == "landing_recovery_started":
            entry["state"] = "started"
            entry["started_at"] = payload.get("started_at") or entry["started_at"]
        elif kind == "landing_recovery_exhausted":
            entry["state"] = "exhausted"
        if not entry["requested_at"] and created_at:
            entry["requested_at"] = datetime.fromtimestamp(
                int(created_at), tz=timezone.utc
            ).isoformat(timespec="seconds")

    ordered = sorted(
        grouped.values(),
        key=lambda e: (e.get("requested_at") or "", e["task_id"]),
        reverse=True,
    )
    return ordered[:limit]


def _landing_preview_status(state_dir: Path | None = None) -> dict[str, Any] | None:
    """Zustand des letzten manuellen Dry-Runs; None, wenn nie eine Vorschau lief."""
    root = _landing_state_dir(state_dir)
    state = _read_json_object(root / LANDING_PREVIEW_STATE_FILENAME)
    done = _read_json_object(root / LANDING_PREVIEW_DONE_FILENAME)
    # _read_json_object liefert {} bei fehlender/kaputter Datei — beide leer
    # heisst: es lief noch nie eine Vorschau.
    if not state and not done:
        return None
    output_tail = ""
    out_path = root / LANDING_PREVIEW_OUTPUT_FILENAME
    try:
        if out_path.is_file():
            output_tail = out_path.read_text(encoding="utf-8", errors="replace")[
                -LANDING_PREVIEW_OUTPUT_TAIL:
            ]
    except OSError:
        pass
    running = bool(state) and not done
    if running:
        started_raw = (state or {}).get("started_at")
        try:
            started = datetime.fromisoformat(str(started_raw))
            if started.tzinfo is None:
                started = started.replace(tzinfo=timezone.utc)
            age = (datetime.now(timezone.utc) - started).total_seconds()
        except (TypeError, ValueError):
            age = LANDING_PREVIEW_STALE_SECONDS + 1
        if age > LANDING_PREVIEW_STALE_SECONDS:
            running = False  # verwaist: kein done-Marker nach Timeout
    return {
        "running": running,
        "started_at": (state or {}).get("started_at"),
        "finished_at": (done or {}).get("finished_at"),
        "rc": (done or {}).get("rc"),
        "output_tail": output_tail,
    }


def _spawn_landing_preview(root: Path) -> str:
    """Startet einen manuellen Dry-Run (landing_loop --dry-run) detached.

    Kein systemd: die Vorschau ist ein kurzer, referenz-freier Lauf, dessen
    Receipt in Dateien unter dem Landing-State-Dir landet. Aufräumen des
    vorherigen Ergebnisses passiert VOR dem Spawn, damit die Status-Logik
    (state ohne done ⇒ running) eindeutig bleibt.
    """
    for fname in (LANDING_PREVIEW_DONE_FILENAME, LANDING_PREVIEW_OUTPUT_FILENAME):
        try:
            (root / fname).unlink(missing_ok=True)
        except OSError:
            pass
    root.mkdir(parents=True, exist_ok=True)
    started_at = datetime.now(timezone.utc).isoformat(timespec="seconds")
    (root / LANDING_PREVIEW_STATE_FILENAME).write_text(
        json.dumps({"started_at": started_at}) + "\n", encoding="utf-8"
    )
    from hermes_cli.hermes_constants import get_hermes_home

    repo = _landing_repo()
    venv_py = repo / "venv" / "bin" / "python"
    py = str(venv_py) if venv_py.exists() else sys.executable
    loops_root = get_hermes_home() / "loops"
    out_tmp = root / "preview-output.tmp"
    done_tmp = root / "preview-done.tmp"
    script = (
        f"{shlex.quote(py)} -m hermes_cli.landing_loop"
        f" --repo {shlex.quote(str(repo))}"
        f" --loops-root {shlex.quote(str(loops_root))}"
        f" --dry-run"
        f" > {shlex.quote(str(out_tmp))} 2>&1"
        f"; rc=$?"
        f"; mv {shlex.quote(str(out_tmp))} {shlex.quote(str(root / LANDING_PREVIEW_OUTPUT_FILENAME))}"
        f"; printf '%s\\n' \"{{\\\"rc\\\": $rc, \\\"finished_at\\\": \\\"$(date -Iseconds)\\\"}}\""
        f" > {shlex.quote(str(done_tmp))}"
        f" && mv {shlex.quote(str(done_tmp))} {shlex.quote(str(root / LANDING_PREVIEW_DONE_FILENAME))}"
    )
    env = dict(os.environ)
    env["HERMES_LOOP_STATE_DIR"] = str(root)
    subprocess.Popen(  # noqa: S603 - alle Pfade serverseitig konstant, kein User-Input
        ["bash", "-c", script],
        cwd=str(repo) if repo.is_dir() else None,
        env=env,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        start_new_session=True,
    )
    return started_at


def _read_night_overrides(name: str) -> dict[str, str]:
    path = _night_overrides_path(name)
    if not path.is_file():
        return {}
    try:
        return loop_runner.parse_night_overrides(path)
    except RuntimeError as exc:
        raise HTTPException(status_code=500, detail=str(exc)) from exc


def _load_models_catalog() -> dict[str, Any]:
    """Engine-Katalog inklusive der dynamisch ermittelten Modelle.

    Muss durch dieselbe Erweiterung laufen wie das Dropdown und der Runner —
    sonst wiese die Night-Override-Validierung genau die Modelle ab, die das
    UI anbietet.
    """
    path = _models_path()
    if not path.is_file():
        return {}
    data = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    engines = data.get("engines")
    if not isinstance(engines, dict):
        return {}
    from loops.model_catalog import catalog_with_dynamic_models

    return catalog_with_dynamic_models(engines)


def _resolve_touched_phase_engine(
    pack: loop_runner.Pack,
    cleaned: dict[str, str],
    phase_names: dict[str, str],
    touched: set[str],
) -> list[tuple[str, str, str]]:
    """Je beruehrter Phase die EFFEKTIVE Engine aufloesen und registrieren pruefen.

    Bewusst von Start- UND Night-Overrides geteilt. Bis 2026-07-29 pruefte nur
    der Night-Pfad ueberhaupt eine Engine: ein Dashboard-*Start* mit unbekanntem
    Engine-Namen kam durch die HTTP-Schicht und starb erst in der systemd-Unit —
    der Operator sah "Loop-Unit sofort gescheitert" statt der Ursache. Zwei
    Kopien derselben Regel waren genau der Grund, warum eine zurueckblieb.

    Aufgeloest wird effektiv: fehlt der Teil-Override, gilt die pack.yaml.
    Gibt (phase_up, phase_name, engine) zurueck, damit Aufrufer darauf
    aufbauen koennen — Night prueft zusaetzlich den Modell-Katalog, Start
    erlaubt bewusst einen Engine-Wechsel ohne mitgeschicktes Modell.
    """
    from loops import engines as loop_engines

    resolved: list[tuple[str, str, str]] = []
    for phase_up in sorted(touched):
        phase_name = phase_names[phase_up]
        base = pack.phases[phase_name]
        engine = cleaned.get(f"PHASE_{phase_up}_ENGINE", base.engine)
        if engine not in loop_engines.ENGINES:
            raise HTTPException(
                status_code=400,
                detail=f"Engine {engine!r} für Phase {phase_name!r} unbekannt",
            )
        resolved.append((phase_up, phase_name, engine))
    return resolved


def _validate_night_overrides(
    pack: loop_runner.Pack,
    raw: dict[str, Any],
) -> dict[str, str]:
    """Pack-/Phase-/Key-/Katalogvalidierung; partielle Paare nur via pack.yaml."""
    if not isinstance(raw, dict):
        raise HTTPException(status_code=400, detail="overrides muss ein Objekt sein")
    cleaned: dict[str, str] = {}
    for key, val in raw.items():
        if not isinstance(key, str) or not _NIGHT_OVERRIDE_KEY_RE.fullmatch(key):
            raise HTTPException(
                status_code=400,
                detail=(
                    f"Night-Override-Key nicht erlaubt: {key!r} "
                    "(nur PHASE_[A-Z]+_(ENGINE|MODEL|EFFORT))"
                ),
            )
        sval = str(val).strip()
        if not sval or not _NIGHT_OVERRIDE_VALUE_RE.fullmatch(sval):
            raise HTTPException(
                status_code=400,
                detail=f"Night-Override-Wert ungültig für {key}",
            )
        cleaned[key] = sval

    phase_names = {name.upper(): name for name in pack.phases}
    touched: set[str] = set()
    for key in cleaned:
        # PHASE_<NAME>_(ENGINE|MODEL)
        body = key[len("PHASE_") :]
        phase_up, kind = body.rsplit("_", 1)
        if phase_up not in phase_names:
            raise HTTPException(
                status_code=400,
                detail=f"Unbekannte Phase in Night-Override: {key}",
            )
        touched.add(phase_up)

    catalog = _load_models_catalog()
    from loops import engines as loop_engines

    for phase_up, phase_name, engine in _resolve_touched_phase_engine(
        pack, cleaned, phase_names, touched
    ):
        # Night schreibt eine PERSISTENTE Konfiguration — hier zusätzlich das
        # Modell gegen den Katalog pinnen, damit nicht jede Nacht auf einer
        # unmöglichen Engine/Modell-Kombination läuft.
        engine_entry = catalog.get(engine)
        if not isinstance(engine_entry, dict):
            raise HTTPException(
                status_code=400,
                detail=f"Engine {engine!r} für Phase {phase_name!r} unbekannt",
            )
        model = cleaned.get(f"PHASE_{phase_up}_MODEL", pack.phases[phase_name].model)
        models = engine_entry.get("models") or []
        if model not in models:
            raise HTTPException(
                status_code=400,
                detail=(
                    f"Modell {model!r} ist für Engine {engine!r} "
                    f"(Phase {phase_name!r}) nicht freigegeben"
                ),
            )
        # Effort gegen die EFFEKTIVE Engine prüfen (nach einem Engine-Override
        # gilt deren Set). Die Wahrheit kommt aus der Engine selbst, damit
        # Dashboard-Angebot und CLI-Transport nicht auseinanderlaufen können.
        eff_key = f"PHASE_{phase_up}_EFFORT"
        if eff_key in cleaned:
            try:
                loop_engines.validate_effort(engine, cleaned[eff_key])
            except loop_engines.EffortUnsupported as exc:
                raise HTTPException(
                    status_code=400,
                    detail=f"Phase {phase_name!r}: {exc}",
                ) from None
    return cleaned


def _loop_engines():
    """Engine-Registry lazy — der Import zieht die CLI-Adapter nach."""
    from loops import engines as loop_engines

    return loop_engines


def _validate_start_overrides(
    pack: loop_runner.Pack,
    raw: dict[str, Any],
) -> None:
    """One-Shot-Overrides prüfen, BEVOR die Unit startet.

    Ohne das würde ein falscher Effort oder ein fehlender Autoland-Zweitschlüssel
    erst im Runner auffallen — der Operator sähe im Dashboard nur ein generisches
    "Loop-Unit sofort gescheitert" statt zu erfahren, was er falsch gesetzt hat.
    """
    from loops import engines as loop_engines

    cleaned = {str(k): str(v).strip() for k, v in raw.items()}

    autoland_raw = cleaned.get(loop_runner.AUTOLAND_RUNTIME_KEY, "").lower()
    if autoland_raw not in ("", "0", "false", "no"):
        if autoland_raw not in ("1", "true", "yes"):
            raise HTTPException(
                status_code=400,
                detail=(
                    f"{loop_runner.AUTOLAND_RUNTIME_KEY} muss 1 oder 0 sein "
                    f"(erhalten: {autoland_raw!r})"
                ),
            )
        if pack.type != "pipeline":
            raise HTTPException(
                status_code=400,
                detail=f"Autoland braucht type=pipeline (Pack {pack.name!r} ist {pack.type})",
            )
        ack = cleaned.get(loop_runner.AUTOLAND_RUNTIME_ACK_KEY, "")
        if ack != pack.name:
            raise HTTPException(
                status_code=400,
                detail=(
                    "Autoland braucht den Zweitschlüssel "
                    f"{loop_runner.AUTOLAND_RUNTIME_ACK_KEY}={pack.name!r} "
                    f"(erhalten: {ack!r})"
                ),
            )

    phase_names = {name.upper(): name for name in pack.phases}

    # Engine/Modell derselben Katalogpruefung unterwerfen wie die Night-Overrides.
    # Auch ein Teil-Override zaehlt: PHASE_BUILD_ENGINE allein (genau die
    # Nacht-Konfiguration vom 2026-07-29) muss gegen das Pack-Modell geprueft
    # werden, sonst startet der Loop mit einer unmoeglichen Kombination.
    touched: set[str] = set()
    for key in cleaned:
        if not key.startswith("PHASE_"):
            continue
        body = key[len("PHASE_") :]
        if "_" not in body:
            continue
        phase_up, kind = body.rsplit("_", 1)
        if kind not in ("ENGINE", "MODEL"):
            continue
        if phase_up not in phase_names:
            raise HTTPException(
                status_code=400, detail=f"Unbekannte Phase in Override: {key}"
            )
        touched.add(phase_up)
    _resolve_touched_phase_engine(pack, cleaned, phase_names, touched)

    for key, val in cleaned.items():
        if not key.endswith("_EFFORT") or not key.startswith("PHASE_"):
            continue
        phase_up = key[len("PHASE_") : -len("_EFFORT")]
        phase_name = phase_names.get(phase_up)
        if phase_name is None:
            raise HTTPException(
                status_code=400, detail=f"Unbekannte Phase in Override: {key}"
            )
        # Gegen die EFFEKTIVE Engine prüfen — ein gleichzeitiger Engine-Override
        # entscheidet, welches Effort-Set gilt.
        engine = cleaned.get(f"PHASE_{phase_up}_ENGINE") or pack.phases[phase_name].engine
        try:
            loop_engines.validate_effort(engine, val)
        except loop_engines.EffortUnsupported as exc:
            raise HTTPException(
                status_code=400, detail=f"Phase {phase_name!r}: {exc}"
            ) from None


def _write_night_overrides(name: str, overrides: dict[str, str]) -> None:
    """Atomischer Replace (0600). Leeres Dict löscht die Ressource."""
    path = _night_overrides_path(name)
    path.parent.mkdir(parents=True, exist_ok=True)
    if not overrides:
        path.unlink(missing_ok=True)
        return
    content = (
        "# night-overrides — persistent; One-Shot overrides.env hat Vorrang\n"
        + "\n".join(f"{k}={v}" for k, v in sorted(overrides.items()))
        + "\n"
    )
    fd, tmp_name = tempfile.mkstemp(
        prefix=".night-overrides.",
        suffix=".tmp",
        dir=str(path.parent),
    )
    tmp_path = Path(tmp_name)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            fh.write(content)
            fh.flush()
            os.fsync(fh.fileno())
        os.chmod(tmp_path, 0o600)
        os.replace(tmp_path, path)
        os.chmod(path, 0o600)
    except Exception:
        tmp_path.unlink(missing_ok=True)
        raise


def _timer_schedule(name: str) -> str:
    """Effektiven systemd-Kalender lesen; Datei/Repo-Default sind Fallbacks."""
    effective = _systemctl(
        "show", "--property=TimersCalendar", "--value", _timer_unit(name),
    )
    if effective.returncode == 0:
        match = _TIMER_ON_CALENDAR_RE.search(effective.stdout)
        if match:
            return match.group(1)

    path = _timer_dropin_path(name)
    try:
        content = path.read_text(encoding="utf-8")
    except OSError:
        return DEFAULT_TIMER_SCHEDULE
    match = _TIMER_ON_CALENDAR_RE.search(content)
    return match.group(1) if match else DEFAULT_TIMER_SCHEDULE


def _timer_next_run(name: str) -> str | None:
    res = _systemctl("list-timers", _timer_unit(name), "--output=json", "--no-legend")
    if res.returncode != 0:
        return None
    try:
        rows = json.loads(res.stdout)
        next_usec = rows[0]["next"]
        if not isinstance(next_usec, int) or next_usec <= 0:
            return None
        next_run = datetime.fromtimestamp(next_usec / 1_000_000, tz=timezone.utc)
    except (IndexError, KeyError, TypeError, ValueError, json.JSONDecodeError, OSError):
        return None
    return next_run.isoformat(timespec="seconds").replace("+00:00", "Z")


def _timer_snapshot(names: list[str]) -> dict[str, dict[str, Any]]:
    """Read all timer facts with two systemctl processes instead of three per pack."""
    if not names:
        return {}
    units = [_timer_unit(name) for name in names]
    show = _systemctl(
        "show",
        *units,
        "--property=Id",
        "--property=UnitFileState",
        "--property=TimersCalendar",
    )
    snapshot: dict[str, dict[str, Any]] = {}
    if show.returncode == 0:
        for block in re.split(r"\n\s*\n", show.stdout.strip()):
            fields = dict(line.split("=", 1) for line in block.splitlines() if "=" in line)
            unit = fields.get("Id", "")
            if not unit.startswith("hermes-loop@") or not unit.endswith(".timer"):
                continue
            name = unit.removeprefix("hermes-loop@").removesuffix(".timer")
            schedule_match = _TIMER_ON_CALENDAR_RE.search(fields.get("TimersCalendar", ""))
            snapshot[name] = {
                "timer_enabled": fields.get("UnitFileState") == "enabled",
                "timer_schedule": schedule_match.group(1) if schedule_match else DEFAULT_TIMER_SCHEDULE,
                "timer_next_run": None,
            }
    timers = _systemctl("list-timers", "--all", "--output=json", "--no-legend")
    try:
        rows = json.loads(timers.stdout) if timers.returncode == 0 else []
    except (TypeError, ValueError):
        rows = []
    for row in rows if isinstance(rows, list) else []:
        if not isinstance(row, dict):
            continue
        unit = row.get("unit")
        next_usec = row.get("next")
        if not isinstance(unit, str) or not unit.startswith("hermes-loop@") or not unit.endswith(".timer"):
            continue
        if not isinstance(next_usec, int) or next_usec <= 0:
            continue
        name = unit.removeprefix("hermes-loop@").removesuffix(".timer")
        if name in snapshot:
            next_run = datetime.fromtimestamp(next_usec / 1_000_000, tz=timezone.utc)
            snapshot[name]["timer_next_run"] = next_run.isoformat(timespec="seconds").replace("+00:00", "Z")
    return snapshot


def _atomic_write(path: Path, content: bytes) -> None:
    """Datei im Zielverzeichnis schreiben und atomar ersetzen."""
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp_path: Path | None = None
    try:
        with tempfile.NamedTemporaryFile(
            mode="wb", dir=path.parent, prefix=f".{path.name}.", delete=False,
        ) as fh:
            tmp_path = Path(fh.name)
            fh.write(content)
            fh.flush()
            os.fsync(fh.fileno())
        tmp_path.replace(path)
        # Rename-Metadaten best effort ebenfalls persistieren. Ein fsync-Fehler
        # darf den bereits atomar ersetzten Inhalt nicht als fehlgeschlagen melden.
        try:
            dir_fd = os.open(path.parent, os.O_RDONLY | os.O_DIRECTORY)
            try:
                os.fsync(dir_fd)
            finally:
                os.close(dir_fd)
        except OSError:
            pass
        tmp_path = None
    finally:
        if tmp_path is not None:
            tmp_path.unlink(missing_ok=True)


def _restore_timer_dropin(path: Path, previous: bytes | None, *, restart: bool, unit: str) -> None:
    """Best-effort-Rollback nach einem systemd-Fehler; Originalfehler gewinnt."""
    try:
        if previous is None:
            path.unlink(missing_ok=True)
            try:
                dir_fd = os.open(path.parent, os.O_RDONLY | os.O_DIRECTORY)
                try:
                    os.fsync(dir_fd)
                finally:
                    os.close(dir_fd)
            except OSError:
                pass
        else:
            _atomic_write(path, previous)
        _systemctl("daemon-reload")
        if restart:
            _systemctl("restart", unit)
    except OSError:
        pass


def _set_timer_schedule(name: str, schedule: str, *, enabled: bool) -> None:
    path = _timer_dropin_path(name)
    try:
        previous = path.read_bytes() if path.is_file() else None
        content = (
            "# Verwaltet vom Hermes /control Loop-Tab.\n"
            "[Timer]\n"
            "OnCalendar=\n"
            f"OnCalendar=*-*-* {schedule}:00\n"
        ).encode("utf-8")
        _atomic_write(path, content)
    except OSError as exc:
        raise RuntimeError(f"Timer-Zeit konnte nicht gespeichert werden: {exc}") from exc

    unit = _timer_unit(name)
    reload_result = _systemctl("daemon-reload")
    if reload_result.returncode != 0:
        _restore_timer_dropin(path, previous, restart=False, unit=unit)
        detail = reload_result.stderr.strip() or reload_result.stdout.strip()
        raise RuntimeError(f"systemctl daemon-reload fehlgeschlagen: {detail}")

    if enabled:
        restart_result = _systemctl("restart", unit)
        if restart_result.returncode != 0:
            _restore_timer_dropin(path, previous, restart=True, unit=unit)
            detail = restart_result.stderr.strip() or restart_result.stdout.strip()
            raise RuntimeError(f"Timer konnte nicht neu eingeplant werden: {detail}")


def _load_pack_or_404(name: str) -> loop_runner.Pack:
    if not _PACK_NAME_RE.match(name):
        raise HTTPException(status_code=404, detail=f"unbekanntes Pack: {name!r}")
    try:
        return loop_runner.load_pack(_dir_for(name), name)
    except loop_runner.ManifestError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc


def _is_running(state: Path) -> bool:
    """Runner hält während plan/run/night ein flock auf <state>/.lock."""
    lock = state / ".lock"
    if not lock.exists():
        return False
    try:
        with lock.open("r+", encoding="utf-8") as fh:
            try:
                fcntl.flock(fh, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError:
                return True
            fcntl.flock(fh, fcntl.LOCK_UN)
    except OSError:
        return False
    return False


_GIT_PROBE_ERROR_PREFIX = "git-Probe fehlgeschlagen: "


def _git(repo: Path, *args: str) -> subprocess.CompletedProcess:
    command = ["git", "-C", str(repo), *args]
    try:
        return subprocess.run(
            command,
            capture_output=True, encoding="utf-8", errors="replace",
            timeout=30, check=False,
        )
    except subprocess.TimeoutExpired as exc:
        return subprocess.CompletedProcess(
            command, 124, stdout="", stderr=f"{_GIT_PROBE_ERROR_PREFIX}{exc}",
        )
    except OSError as exc:
        return subprocess.CompletedProcess(
            command, 127, stdout="", stderr=f"{_GIT_PROBE_ERROR_PREFIX}{exc}",
        )


def _commits_ahead_with_error(pack: loop_runner.Pack) -> tuple[list[str], str | None]:
    """Return absent loop-branch commits and an infrastructure-error hint, if any.

    Loops can be reconciled by another session/worktree: the exact commit SHA then
    differs, but the patch is already present on ``main``. ``git log main..branch``
    counts those commits as still landable; ``git cherry`` compares stable patch-ids
    and marks equivalent changes with ``-``. The dashboard should only offer
    landing for ``+`` commits that are not patch-equivalent on main.
    """
    res = _git(pack.repo, "cherry", "-v", "main", pack.branch)
    if res.returncode != 0:
        error = res.stderr.strip()
        return [], error if error.startswith(_GIT_PROBE_ERROR_PREFIX) else None
    commits: list[str] = []
    for line in res.stdout.splitlines():
        line = line.strip()
        if not line.startswith("+"):
            continue
        _marker, _space, rest = line.partition(" ")
        sha, _space, subject = rest.strip().partition(" ")
        commits.append(f"{sha[:7]} {subject}".rstrip())
        if len(commits) >= 50:
            break
    return commits, None


def _commits_ahead(pack: loop_runner.Pack) -> list[str]:
    commits, _error = _commits_ahead_with_error(pack)
    return commits


def _heartbeat(state: Path) -> dict[str, Any] | None:
    hb = state / "heartbeat.json"
    if not hb.is_file():
        return None
    try:
        data = json.loads(hb.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    return data if isinstance(data, dict) else None


def _phase_usage(state: Path) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    path = state / "ledger.jsonl"
    events: list[dict[str, Any]] = []
    try:
        lines = path.read_text(encoding="utf-8").splitlines()
    except OSError:
        lines = []
    for line in lines:
        try:
            event = json.loads(line)
        except (TypeError, ValueError):
            continue
        if isinstance(event, dict) and event.get("event") == "phase_usage":
            events.append(event)
    token_values = [event["total_tokens"] for event in events if isinstance(event.get("total_tokens"), int)]
    costs = [float(event["metered_cost_eur"]) for event in events if isinstance(event.get("metered_cost_eur"), (int, float))]
    billings = {event.get("billing") for event in events if isinstance(event.get("billing"), str)}
    billing = next(iter(billings)) if len(billings) == 1 else "mixed" if billings else "unknown"
    return {
        "total_tokens": sum(token_values) if token_values else None,
        "metered_cost_eur": sum(costs) if costs else None,
        "billing": billing,
    }, events


_FILENAME_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,63}$")


def _lint_pack_dir(base: Path, name: str) -> str | None:
    """Werkstatt-Lint: Manifest muss laden, referenzierte Prompts müssen die
    Pflicht-Konventionen tragen (wie tests/loops Pack-Lint). None = ok."""
    try:
        pack = loop_runner.load_pack(base, name)
    except loop_runner.ManifestError as exc:
        return str(exc)
    for pname, phase in pack.phases.items():
        text = (pack.pack_dir / phase.prompt).read_text(encoding="utf-8")
        for needle, warum in (
            ("{{STATE_DIR}}", "STATE_DIR-Platzhalter fehlt"),
            ("last-status", "last-status-Protokoll fehlt"),
            ("push", "Verbote-Block fehlt (push)"),
        ):
            if needle not in text:
                return f"Phase {pname} ({phase.prompt}): {warum}"
    return None


def _spawn_land(pack: loop_runner.Pack, log_path: Path) -> None:
    """Landung detached starten (dauert Minuten: Gates). Seam für Tests."""
    py = pack.repo / "venv" / "bin" / "python"
    with log_path.open("w", encoding="utf-8") as log_fh:
        subprocess.Popen(  # noqa: S603 — Argumente stammen aus validiertem Pack
            [str(py), "-m", "loops.runner", "--pack", pack.name, "--cmd", "land"],
            cwd=str(pack.repo),
            stdout=log_fh,
            stderr=subprocess.STDOUT,
            start_new_session=True,
            env={"PYTHONPATH": str(pack.repo), "PATH": "/usr/bin:/bin", "HOME": str(Path.home())},
        )


def _timer_enabled(name: str) -> bool:
    res = _systemctl("is-enabled", _timer_unit(name))
    return res.returncode == 0 and res.stdout.strip() == "enabled"


def _unit_failed_fast(unit: str, probe: float = 0.6) -> bool:
    """True, wenn die Unit unmittelbar nach dem Start bereits 'failed' ist (Sofort-Fail
    wie 203/EXEC). 'activating'/'active' = ordentlich angelaufen → False. Seam für Tests."""
    import time as _time

    _time.sleep(probe)
    return _systemctl("is-active", unit).stdout.strip() == "failed"


def _pack_summary(
    name: str,
    source: str = "repo",
    timer_snapshot: dict[str, dict[str, Any]] | None = None,
) -> dict[str, Any]:
    try:
        pack = loop_runner.load_pack(_dir_for(name), name)
    except loop_runner.ManifestError as exc:
        return {"name": name, "error": str(exc)}
    state = _state_root() / name
    qcounts = {
        stage: len(list((state / "queue" / stage).glob("*.md")))
        if (state / "queue" / stage).is_dir() else 0
        for stage in loop_runner.QUEUE_STAGES
    }
    timer = timer_snapshot.get(pack.name) if timer_snapshot else None
    timer_enabled = timer["timer_enabled"] if timer else _timer_enabled(pack.name)
    token_usage, _phase_events = _phase_usage(state)
    commits, commits_error = _commits_ahead_with_error(pack)
    summary = {
        "name": pack.name,
        "type": pack.type,
        "source": source,
        "repo": str(pack.repo),
        # Ein Pack, dessen repo-Pfad nicht mehr existiert, sah im Dashboard
        # normal aus und starb erst beim Start (runner.py: "Pack-Repo
        # existiert nicht") — belegt am 28.07. an xai-hard-gate und
        # loops-date-audit, die beide auf einen geloeschten Worktree zeigten.
        # Gleiche Semantik wie der Runner-Check, damit UI und Start nicht
        # auseinanderlaufen.
        "repo_exists": pack.repo.is_dir(),
        "base_branch": pack.base_branch,
        "land_remote": pack.land_remote,
        "land_push": pack.land_push,
        "land_gates": pack.land_gates,
        "description": pack.description,
        "stability": pack.stability,
        "phases": {
            pname: {
                "engine": ph.engine,
                "model": ph.model,
                "timeout": ph.timeout,
                "effort": ph.effort,
                # Was die aktuelle Engine dieser Phase transportieren kann;
                # [] = kein Effort-Control (gleiche Konvention wie Lanes).
                "effort_support": list(_loop_engines().effort_levels_for(ph.engine)),
            }
            for pname, ph in pack.phases.items()
        },
        # Autoland ist pro Lauf schaltbar, sobald das Pack eine Pipeline ist —
        # `autoland` oben bleibt der Manifest-Zustand (Vertrags-Autoland).
        "autoland_capable": pack.type == "pipeline",
        "stop": pack.stop,
        "params": pack.params,
        "autoland": pack.autoland,
        "running": _is_running(state),
        "heartbeat": _heartbeat(state),
        "stop_requested": (state / "STOP").exists(),
        "queue": qcounts if pack.type == "pipeline" else None,
        "commits_ahead": len(commits),
        "timer_enabled": timer_enabled,
        "timer_schedule": timer["timer_schedule"] if timer else _timer_schedule(pack.name),
        "timer_next_run": timer["timer_next_run"] if timer else _timer_next_run(pack.name),
        "token_usage": token_usage,
    }
    if commits_error:
        summary["error"] = commits_error
    if pack.name == "landing":
        summary.update(_landing_control_payload(state))
    return summary


class StartBody(BaseModel):
    overrides: dict[str, Any] = {}


class TimerBody(BaseModel):
    enabled: bool


class TimerScheduleBody(BaseModel):
    time: str


class FileBody(BaseModel):
    content: str


class DuplicateBody(BaseModel):
    source: str
    name: str


class NightOverridesBody(BaseModel):
    overrides: dict[str, Any] = {}


class LandingAutomationBody(BaseModel):
    enabled: bool


def register_loops_routes(app: FastAPI) -> None:
    """Loops-Endpoints registrieren (vor dem SPA-Catch-all aufrufen)."""

    @app.get("/api/loops")
    def list_loops() -> dict[str, Any]:
        packs = _all_pack_names()
        timers = _timer_snapshot([name for name, _source in packs])
        return {"packs": [_pack_summary(name, source, timers) for name, source in packs]}

    @app.get("/api/loops/models")
    def loop_models() -> dict[str, Any]:
        path = _models_path()
        if not path.is_file():
            return {"engines": {}}
        data = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
        # Voller, dynamischer Katalog wie im Lanes-Tab: kuratierte Defaults
        # zuerst, dann alles, was Hermes an Providern/Modellen kennt.
        from loops.model_catalog import catalog_with_dynamic_models

        engines_out = dict(catalog_with_dynamic_models(data.get("engines", {})))
        # `effort_levels` kommt aus der Engine-Registrierung, NICHT aus
        # models.yaml: die Engine ist die einzige Stelle, die weiß, welches
        # Flag ihr CLI wirklich akzeptiert. Leere Liste = kein Control (gleiche
        # Konvention wie `reasoning_support: []` im Lanes-Tab).
        from loops import engines as loop_engines

        for engine_name, entry in engines_out.items():
            if isinstance(entry, dict):
                engines_out[engine_name] = {
                    **entry,
                    "effort_levels": list(loop_engines.effort_levels_for(engine_name)),
                }
        return {"engines": engines_out}

    @app.get("/api/loops/{pack}/detail")
    def loop_detail(pack: str) -> dict[str, Any]:
        loaded = _load_pack_or_404(pack)
        source = "custom" if _dir_for(loaded.name) == loop_runner.CUSTOM_PACKS_DIR else "repo"
        state = _state_root() / loaded.name
        ledger_path = state / "LEDGER.md"
        ledger_tail = (
            ledger_path.read_text(encoding="utf-8").splitlines()[-50:]
            if ledger_path.is_file() else []
        )
        queue_entries = {
            stage: sorted(
                p.name for p in (state / "queue" / stage).glob("*.md")
            ) if (state / "queue" / stage).is_dir() else []
            for stage in loop_runner.QUEUE_STAGES
        }
        overrides_path = state / "overrides.env"
        _token_usage, phase_usage = _phase_usage(state)
        detail: dict[str, Any] = {
            **_pack_summary(loaded.name, source),
            "ledger_tail": ledger_tail,
            "queue_entries": queue_entries if loaded.type == "pipeline" else None,
            "commits": _commits_ahead(loaded),
            "overrides": loop_runner.parse_overrides(overrides_path),
            "phase_usage": phase_usage,
        }
        if loaded.name == "landing":
            # Additiver Drawer-Vertrag (LL2-S5): Kandidaten stehen bereits in
            # der Summary; hier kommen Gate-Stufen, Trigger-Historie, Recovery
            # und der letzte manuelle Dry-Run dazu.
            trigger_state = _read_json_object(state / LANDING_TRIGGER_FILENAME)
            seen = trigger_state.get("seen_signals")
            detail["gate_stages"] = _landing_gate_stages()
            detail["trigger_history"] = (
                [str(s) for s in seen][-10:] if isinstance(seen, list) else []
            )
            detail["followup_pending"] = bool(trigger_state.get("followup_pending"))
            detail["recovery"] = _landing_recovery_entries()
            detail["preview"] = _landing_preview_status(state)
        return detail

    @app.put("/api/loops/landing/automation")
    @app.post("/api/loops/landing/automation")
    def set_landing_automation(body: LandingAutomationBody) -> dict[str, Any]:
        _load_pack_or_404("landing")
        return set_landing_automation_enabled(body.enabled, updated_by="control-api")

    @app.post("/api/loops/landing/preview")
    def landing_preview() -> dict[str, Any]:
        """Manueller Dry-Run (Vorschau) — referenzfrei, auch bei Automation=off."""
        _load_pack_or_404("landing")
        state = _landing_state_dir()
        if _is_running(state):
            raise HTTPException(status_code=409, detail="Landing-Loop läuft bereits")
        current = _landing_preview_status(state)
        if current and current.get("running"):
            raise HTTPException(status_code=409, detail="Vorschau läuft bereits")
        started_at = _spawn_landing_preview(state)
        return {"ok": True, "started_at": started_at}

    @app.post("/api/loops/{pack}/start")
    def start_loop(pack: str, body: StartBody) -> dict[str, Any]:
        loaded = _load_pack_or_404(pack)
        state = _state_root() / loaded.name
        if _is_running(state):
            raise HTTPException(status_code=409, detail="Loop läuft bereits")
        # Fehlt das Repo, stirbt der Runner erst in der Unit und der Fehler
        # kommt als kryptischer 502 ("Loop-Unit sofort gescheitert") zurueck.
        # Hier sagen wir vorher, WAS fehlt.
        if not loaded.repo.is_dir():
            raise HTTPException(
                status_code=409,
                detail=(
                    f"Pack-Repo existiert nicht: {loaded.repo} — Pack im "
                    "Manifest neu binden oder stilllegen."
                ),
            )
        param_keys = {p.upper() for p in loaded.params}
        lines = []
        for key, val in body.overrides.items():
            sval = str(val).strip()
            # fullmatch statt match: "$" ließe ein trailing \n im Key durch (Review-Nit).
            if not (_OVERRIDE_KEY_RE.fullmatch(key) or key in param_keys):
                raise HTTPException(
                    status_code=400,
                    detail=f"Override-Key nicht erlaubt: {key!r} (Pack-Params: {sorted(loaded.params) or '—'})",
                )
            if not _OVERRIDE_VALUE_RE.fullmatch(sval):
                raise HTTPException(status_code=400, detail=f"Override-Wert ungültig für {key}")
            if sval:
                lines.append(f"{key}={sval}")
        _validate_start_overrides(loaded, body.overrides)
        state.mkdir(parents=True, exist_ok=True)
        (state / "overrides.env").write_text(
            "# geschrieben vom /control-Dashboard\n" + "\n".join(lines) + "\n",
            encoding="utf-8",
        )
        # --no-block: oneshot-Units halten den systemctl-Client sonst bis zum
        # Prozessende (Stunden) — empirisch bewiesen, Review-Blocker 2026-07-02.
        unit = f"hermes-loop@{loaded.name}.service"
        _systemctl("reset-failed", unit)  # alten failed-Zustand räumen, sonst blockt der Restart
        res = _systemctl("start", "--no-block", unit)
        if res.returncode != 0:
            raise HTTPException(
                status_code=502,
                detail=f"systemctl start fehlgeschlagen: {res.stderr.strip() or res.stdout.strip()}",
            )
        # Ehrlichkeits-Check: --no-block kehrt sofort zurück; ein Sofort-Fail (z.B.
        # 203/EXEC) wäre sonst als "started" durchgerutscht (UI-Start-Bug 2026-07-03).
        if _unit_failed_fast(unit):
            log = _systemctl("show", "-p", "StatusText", "--value", unit).stdout.strip()
            raise HTTPException(
                status_code=502,
                detail=f"Loop-Unit sofort gescheitert (nicht angelaufen). {log or 'journalctl --user -u ' + unit}",
            )
        return {"started": True, "pack": loaded.name, "overrides_written": len(lines)}

    @app.post("/api/loops/{pack}/stop")
    def stop_loop(pack: str) -> dict[str, Any]:
        loaded = _load_pack_or_404(pack)
        state = _state_root() / loaded.name
        state.mkdir(parents=True, exist_ok=True)
        (state / "STOP").write_text("", encoding="utf-8")
        return {"stop_requested": True, "pack": loaded.name,
                "note": "greift vor der nächsten Phase; laufende Phase endet regulär"}

    @app.get("/api/loops/{pack}/queue/{stage}/{filename:path}")
    def loop_queue_file(pack: str, stage: str, filename: str) -> dict[str, Any]:
        loaded = _load_pack_or_404(pack)
        if stage not in loop_runner.QUEUE_STAGES:
            raise HTTPException(status_code=400, detail=f"Queue-Stufe nicht erlaubt: {stage!r}")
        if not _FILENAME_RE.fullmatch(filename) or not filename.endswith(".md"):
            raise HTTPException(status_code=400, detail=f"Dateiname nicht erlaubt: {filename!r}")
        stage_dir = (_state_root() / loaded.name / "queue" / stage).resolve()
        target = (stage_dir / filename).resolve()
        if not target.is_relative_to(stage_dir):
            raise HTTPException(status_code=400, detail=f"Dateiname nicht erlaubt: {filename!r}")
        if not target.is_file():
            raise HTTPException(status_code=404, detail=f"Queue-Datei existiert nicht: {filename!r}")
        return {
            "pack": loaded.name,
            "stage": stage,
            "filename": filename,
            "content": target.read_text(encoding="utf-8"),
        }

    @app.get("/api/loops/{pack}/files")
    def loop_files(pack: str) -> dict[str, Any]:
        loaded = _load_pack_or_404(pack)
        source = "custom" if _dir_for(loaded.name) == loop_runner.CUSTOM_PACKS_DIR else "repo"
        editable = source == "custom"  # Repo-Packs sind kuratiert: via Git ändern
        files = []
        names = ["pack.yaml"] + sorted(
            p.name for p in loaded.pack_dir.glob("*.md") if p.is_file()
        )
        for fname in names:
            files.append({
                "name": fname,
                "content": (loaded.pack_dir / fname).read_text(encoding="utf-8"),
                "editable": editable,
            })
        return {"pack": loaded.name, "source": source, "files": files}

    @app.put("/api/loops/{pack}/files/{filename}")
    def loop_file_save(pack: str, filename: str, body: FileBody) -> dict[str, Any]:
        loaded = _load_pack_or_404(pack)
        source = "custom" if _dir_for(loaded.name) == loop_runner.CUSTOM_PACKS_DIR else "repo"
        if source != "custom":
            raise HTTPException(status_code=403, detail="Repo-Packs sind kuratiert — via Git ändern; zum Editieren erst duplizieren")
        if not _FILENAME_RE.fullmatch(filename) or not (
            filename == "pack.yaml" or filename.endswith(".md")
        ):
            raise HTTPException(status_code=400, detail=f"Dateiname nicht erlaubt: {filename!r}")
        target = loaded.pack_dir / filename
        if not target.is_file():
            raise HTTPException(status_code=404, detail=f"Datei existiert nicht: {filename!r} (Werkstatt v1 editiert nur Bestehendes)")
        if len(body.content) > 200_000:
            raise HTTPException(status_code=400, detail="Datei zu groß")
        # Erst in einer Schattenkopie validieren, dann persistieren — ein kaputtes
        # Manifest darf nie live liegen.
        with tempfile.TemporaryDirectory(prefix="loop-werkstatt-") as tmp:
            shadow_base = Path(tmp)
            shutil.copytree(loaded.pack_dir, shadow_base / loaded.name)
            (shadow_base / loaded.name / filename).write_text(body.content, encoding="utf-8")
            problem = _lint_pack_dir(shadow_base, loaded.name)
        if problem:
            raise HTTPException(status_code=400, detail=f"Lint: {problem}")
        target.write_text(body.content, encoding="utf-8")
        return {"saved": True, "pack": loaded.name, "file": filename}

    @app.post("/api/loops/duplicate")
    def loop_duplicate(body: DuplicateBody) -> dict[str, Any]:
        src = _load_pack_or_404(body.source)
        name = body.name.strip()
        if not loop_runner._PACK_NAME_RE.match(name) or name.startswith("_"):
            raise HTTPException(status_code=400, detail=f"Ziel-Name ungültig: {name!r}")
        custom = loop_runner.CUSTOM_PACKS_DIR if PACKS_DIR_OVERRIDE is None else PACKS_DIR_OVERRIDE
        if (custom / name).exists() or ((_packs_dir() / name / "pack.yaml").is_file()):
            raise HTTPException(status_code=409, detail=f"Pack {name!r} existiert bereits")
        custom.mkdir(parents=True, exist_ok=True)
        shutil.copytree(src.pack_dir, custom / name)
        manifest = custom / name / "pack.yaml"
        data = yaml.safe_load(manifest.read_text(encoding="utf-8"))
        data["name"] = name
        data["stability"] = "experimental"
        data["description"] = f"(Kopie von {src.name}) " + str(data.get("description", ""))
        manifest.write_text(yaml.safe_dump(data, allow_unicode=True, sort_keys=False), encoding="utf-8")
        problem = _lint_pack_dir(custom, name)
        if problem:
            shutil.rmtree(custom / name, ignore_errors=True)
            raise HTTPException(status_code=500, detail=f"Kopie lintet nicht: {problem}")
        return {"created": name, "source": src.name}

    @app.post("/api/loops/{pack}/land")
    def land_loop(pack: str) -> dict[str, Any]:
        loaded = _load_pack_or_404(pack)
        state = _state_root() / loaded.name
        if _is_running(state):
            raise HTTPException(status_code=409, detail="Loop läuft — erst stoppen/auslaufen lassen")
        (state / "logs").mkdir(parents=True, exist_ok=True)
        log_path = state / "logs" / f"land-{datetime.now().strftime('%Y%m%d-%H%M%S')}.log"
        try:
            _spawn_land(loaded, log_path)
        except OSError as exc:
            raise HTTPException(status_code=502, detail=f"Landung ließ sich nicht starten: {exc}") from exc
        return {"land_started": True, "pack": loaded.name, "log": log_path.name,
                "note": "läuft detached mit allen Schienen; Ergebnis im Ledger (LAND ✅ / rollback / Abbruch)"}

    @app.post("/api/loops/{pack}/timer")
    def toggle_timer(pack: str, body: TimerBody) -> dict[str, Any]:
        loaded = _load_pack_or_404(pack)
        action = "enable" if body.enabled else "disable"
        with _timer_mutation_lock(loaded.name):
            res = _systemctl(action, "--now", _timer_unit(loaded.name))
            if res.returncode != 0:
                raise HTTPException(
                    status_code=502,
                    detail=f"systemctl {action} fehlgeschlagen: {res.stderr.strip() or res.stdout.strip()}",
                )
            timer_enabled = _timer_enabled(loaded.name)
            timer_schedule = _timer_schedule(loaded.name)
            timer_next_run = _timer_next_run(loaded.name)
        return {
            "pack": loaded.name,
            "timer_enabled": timer_enabled,
            "timer_schedule": timer_schedule,
            "timer_next_run": timer_next_run,
        }

    @app.put("/api/loops/{pack}/timer/schedule")
    def save_timer_schedule(pack: str, body: TimerScheduleBody) -> dict[str, Any]:
        loaded = _load_pack_or_404(pack)
        schedule = body.time
        if not _TIMER_SCHEDULE_RE.fullmatch(schedule):
            raise HTTPException(status_code=400, detail="Uhrzeit muss im Format HH:MM (00:00–23:59) vorliegen")
        with _timer_mutation_lock(loaded.name):
            timer_enabled = _timer_enabled(loaded.name)
            try:
                _set_timer_schedule(loaded.name, schedule, enabled=timer_enabled)
            except RuntimeError as exc:
                raise HTTPException(status_code=502, detail=str(exc)) from exc
            timer_schedule = _timer_schedule(loaded.name)
            timer_next_run = _timer_next_run(loaded.name)
        return {
            "pack": loaded.name,
            "timer_enabled": timer_enabled,
            "timer_schedule": timer_schedule,
            "timer_next_run": timer_next_run,
        }

    @app.get("/api/loops/{pack}/night-overrides")
    def get_night_overrides(pack: str) -> dict[str, Any]:
        loaded = _load_pack_or_404(pack)
        return {
            "pack": loaded.name,
            "overrides": _read_night_overrides(loaded.name),
        }

    @app.put("/api/loops/{pack}/night-overrides")
    def put_night_overrides(pack: str, body: NightOverridesBody) -> dict[str, Any]:
        loaded = _load_pack_or_404(pack)
        cleaned = _validate_night_overrides(loaded, body.overrides)
        with _night_overrides_lock(loaded.name):
            try:
                _write_night_overrides(loaded.name, cleaned)
            except OSError as exc:
                raise HTTPException(
                    status_code=500,
                    detail=f"night-overrides konnten nicht geschrieben werden: {exc}",
                ) from exc
            stored = _read_night_overrides(loaded.name)
        return {"ok": True, "pack": loaded.name, "overrides": stored}
