"""Append-only run-history for autoresearch (P2: ROI / observability).

A tiny JSON log of recent autoresearch runs — one record per skill-loop run or
per code-weakness scan — so the dashboard can answer "are the loops actually
producing improvements, and what do they cost". Capped to the last N runs.

Deliberately self-contained (no import of autoresearch_proposals/runner) to stay
free of import cycles, and best-effort: a broken/locked history file must never
sink a research run, so every write is wrapped and read tolerates garbage.
"""
from __future__ import annotations

import json
import os
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

_REPO = Path(__file__).resolve().parents[1]
_DEFAULT_AUDIT = _REPO / ".hermes" / "skill-audit"
_MAX_RUNS = 30
_VALID_LANES = ("skill", "code")


def _audit_dir() -> Path:
    # Same env override + default as autoresearch_proposals/run_autoresearch_request
    # so the history lands beside the receipts/proposals.
    override = os.environ.get("HERMES_AUTORESEARCH_AUDIT_DIR")
    return Path(override) if override else _DEFAULT_AUDIT


def _runs_path() -> Path:
    return _audit_dir() / "autoresearch-runs.json"


def _atomic_write_json(path: Path, payload: dict) -> None:
    """Write JSON atomically: temp file in the same dir + os.replace (atomic on
    POSIX). A concurrent reader/writer then sees either the old or the new file,
    never a torn/half-written one. Mirrors the repo's auth.py/backup.py pattern.

    (This removes the corruption/torn-write risk under concurrent append_run, e.g.
    a skill-runner subprocess and a dashboard code-scan thread finishing together.
    The benign read-modify-write race — two writers, last one wins, one record
    dropped — is accepted for a best-effort capped log.)"""
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(f"{path.name}.tmp.{os.getpid()}.{uuid.uuid4().hex}")
    try:
        with open(tmp, "w", encoding="utf-8") as fh:
            json.dump(payload, fh, indent=2, ensure_ascii=False)
            fh.write("\n")
        os.replace(tmp, path)
    finally:
        try:
            if tmp.exists():
                tmp.unlink()
        except OSError:
            pass


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")


def read_runs(limit: int = _MAX_RUNS) -> list[dict[str, Any]]:
    """Most-recent-first list of run records. Tolerant: [] on anything unreadable."""
    path = _runs_path()
    try:
        if not path.exists() or path.stat().st_size == 0:
            return []
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return []
    runs = data.get("runs") if isinstance(data, dict) else data
    if not isinstance(runs, list):
        return []
    clean = [r for r in runs if isinstance(r, dict)]
    n = max(1, int(limit)) if limit else _MAX_RUNS
    return clean[:n]


def append_run(*, lane: str, request_id: str | None = None, tokens: int = 0,
               proposed: int = 0, errors: int = 0, scanned: int = 0,
               vetoed: int = 0, model: str | None = None,
               at: str | None = None) -> None:
    """Prepend one run record (newest first), capped to the last _MAX_RUNS.
    Best-effort: never raises — history is observability, not a source of truth."""
    try:
        record = {
            "at": at or _utc_now(),
            "lane": lane if lane in _VALID_LANES else "skill",
            "request_id": request_id,
            "tokens": int(tokens or 0),
            "proposed": int(proposed or 0),
            "errors": int(errors or 0),
            "vetoed": int(vetoed or 0),
            "model": str(model or "") or None,
            "scanned": int(scanned or 0),
        }
        runs = [record, *read_runs(_MAX_RUNS)][:_MAX_RUNS]
        _atomic_write_json(_runs_path(), {"runs": runs})
    except Exception:
        pass
