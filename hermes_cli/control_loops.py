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

import fcntl
import re
import subprocess
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

# Mutationen nur für reguläre Packs — Unterstrich-Packs (_blank) sind Vorlagen.
_PACK_NAME_RE = re.compile(r"^[a-z0-9][a-z0-9-]{0,63}$")

# overrides.env-Whitelist: genau die Knobs, die der Runner versteht. Werte bleiben
# einzeilig und kurz — die Datei wird vom Runner als KEY=VALUE geparst.
_OVERRIDE_KEY_RE = re.compile(
    r"^(PHASE_[A-Z]+_(MODEL|ENGINE|TIMEOUT)"
    r"|MAX_ROUNDS|MAX_HOURS|FAIL_STREAK|DRY_ROUNDS|MAX_PLANS|FOCUS|DISCORD_CHANNEL)$"
)
_OVERRIDE_VALUE_RE = re.compile(r"^[^\r\n\x00]{0,400}$")


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
    return subprocess.run(
        ["systemctl", "--user", *args],
        capture_output=True, encoding="utf-8", errors="replace",
        timeout=30, check=False,
    )


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
    res = _git(pack.repo, "log", "--oneline", "--max-count=50", f"main..{pack.branch}")
    if res.returncode != 0:
        return []  # Branch existiert (noch) nicht — kein Lauf bisher
    return [line for line in res.stdout.splitlines() if line.strip()]


def _timer_enabled(name: str) -> bool:
    res = _systemctl("is-enabled", f"hermes-loop@{name}.timer")
    return res.returncode == 0 and res.stdout.strip() == "enabled"


def _pack_summary(name: str, source: str = "repo") -> dict[str, Any]:
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
    return {
        "name": pack.name,
        "type": pack.type,
        "source": source,
        "description": pack.description,
        "stability": pack.stability,
        "phases": {
            pname: {"engine": ph.engine, "model": ph.model, "timeout": ph.timeout}
            for pname, ph in pack.phases.items()
        },
        "stop": pack.stop,
        "params": pack.params,
        "running": _is_running(state),
        "stop_requested": (state / "STOP").exists(),
        "queue": qcounts if pack.type == "pipeline" else None,
        "commits_ahead": len(_commits_ahead(pack)),
        "timer_enabled": _timer_enabled(pack.name),
    }


class StartBody(BaseModel):
    overrides: dict[str, Any] = {}


class TimerBody(BaseModel):
    enabled: bool


def register_loops_routes(app: FastAPI) -> None:
    """Loops-Endpoints registrieren (vor dem SPA-Catch-all aufrufen)."""

    @app.get("/api/loops")
    def list_loops() -> dict[str, Any]:
        return {"packs": [_pack_summary(name, source) for name, source in _all_pack_names()]}

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
        return {
            **_pack_summary(loaded.name),
            "ledger_tail": ledger_tail,
            "queue_entries": queue_entries if loaded.type == "pipeline" else None,
            "commits": _commits_ahead(loaded),
            "overrides": loop_runner.parse_overrides(overrides_path),
        }

    @app.post("/api/loops/{pack}/start")
    def start_loop(pack: str, body: StartBody) -> dict[str, Any]:
        loaded = _load_pack_or_404(pack)
        state = _state_root() / loaded.name
        if _is_running(state):
            raise HTTPException(status_code=409, detail="Loop läuft bereits")
        lines = []
        for key, val in body.overrides.items():
            sval = str(val).strip()
            if not _OVERRIDE_KEY_RE.match(key):
                raise HTTPException(status_code=400, detail=f"Override-Key nicht erlaubt: {key!r}")
            if not _OVERRIDE_VALUE_RE.match(sval):
                raise HTTPException(status_code=400, detail=f"Override-Wert ungültig für {key}")
            if sval:
                lines.append(f"{key}={sval}")
        state.mkdir(parents=True, exist_ok=True)
        (state / "overrides.env").write_text(
            "# geschrieben vom /control-Dashboard\n" + "\n".join(lines) + "\n",
            encoding="utf-8",
        )
        res = _systemctl("start", f"hermes-loop@{loaded.name}.service")
        if res.returncode != 0:
            raise HTTPException(
                status_code=502,
                detail=f"systemctl start fehlgeschlagen: {res.stderr.strip() or res.stdout.strip()}",
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

    @app.post("/api/loops/{pack}/timer")
    def toggle_timer(pack: str, body: TimerBody) -> dict[str, Any]:
        loaded = _load_pack_or_404(pack)
        action = "enable" if body.enabled else "disable"
        res = _systemctl(action, "--now", f"hermes-loop@{loaded.name}.timer")
        if res.returncode != 0:
            raise HTTPException(
                status_code=502,
                detail=f"systemctl {action} fehlgeschlagen: {res.stderr.strip() or res.stdout.strip()}",
            )
        return {"pack": loaded.name, "timer_enabled": _timer_enabled(loaded.name)}
