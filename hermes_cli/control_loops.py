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
import shutil
import subprocess
import tempfile
from datetime import datetime, timezone
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
    r"(PHASE_[A-Z]+_(MODEL|ENGINE|TIMEOUT)"
    r"|MAX_ROUNDS|MAX_HOURS|FAIL_STREAK|DRY_ROUNDS|DISCORD_CHANNEL|SKIP_PLAN)"
)
_OVERRIDE_VALUE_RE = re.compile(r"[^\r\n\x00]{0,400}")


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


def _git(repo: Path, *args: str) -> subprocess.CompletedProcess:
    return subprocess.run(
        ["git", "-C", str(repo), *args],
        capture_output=True, encoding="utf-8", errors="replace",
        timeout=30, check=False,
    )


def _commits_ahead(pack: loop_runner.Pack) -> list[str]:
    """Return loop-branch commits that are still genuinely absent from main.

    Loops can be reconciled by another session/worktree: the exact commit SHA then
    differs, but the patch is already present on ``main``. ``git log main..branch``
    counts those commits as still landable; ``git cherry`` compares stable patch-ids
    and marks equivalent changes with ``-``. The dashboard should only offer
    landing for ``+`` commits that are not patch-equivalent on main.
    """
    res = _git(pack.repo, "cherry", "-v", "main", pack.branch)
    if res.returncode != 0:
        return []  # Branch existiert (noch) nicht — kein Lauf bisher
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
    return {
        "name": pack.name,
        "type": pack.type,
        "source": source,
        "repo": str(pack.repo),
        "base_branch": pack.base_branch,
        "land_remote": pack.land_remote,
        "land_push": pack.land_push,
        "land_gates": pack.land_gates,
        "description": pack.description,
        "stability": pack.stability,
        "phases": {
            pname: {"engine": ph.engine, "model": ph.model, "timeout": ph.timeout}
            for pname, ph in pack.phases.items()
        },
        "stop": pack.stop,
        "params": pack.params,
        "autoland": pack.autoland,
        "running": _is_running(state),
        "heartbeat": _heartbeat(state),
        "stop_requested": (state / "STOP").exists(),
        "queue": qcounts if pack.type == "pipeline" else None,
        "commits_ahead": len(_commits_ahead(pack)),
        "timer_enabled": timer_enabled,
        "timer_schedule": timer["timer_schedule"] if timer else _timer_schedule(pack.name),
        "timer_next_run": timer["timer_next_run"] if timer else _timer_next_run(pack.name),
        "token_usage": token_usage,
    }


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
        return {"engines": data.get("engines", {})}

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
        return {
            **_pack_summary(loaded.name, source),
            "ledger_tail": ledger_tail,
            "queue_entries": queue_entries if loaded.type == "pipeline" else None,
            "commits": _commits_ahead(loaded),
            "overrides": loop_runner.parse_overrides(overrides_path),
            "phase_usage": phase_usage,
        }

    @app.post("/api/loops/{pack}/start")
    def start_loop(pack: str, body: StartBody) -> dict[str, Any]:
        loaded = _load_pack_or_404(pack)
        state = _state_root() / loaded.name
        if _is_running(state):
            raise HTTPException(status_code=409, detail="Loop läuft bereits")
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
