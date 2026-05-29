#!/usr/bin/env python3
"""Tiny dry-run heartbeat stub for the Autoresearch live dashboard (Phase 4).

This writes the runner-state contract files (``current.lock``,
``current.heartbeat``, ``current.status``) so the read-only ``/autoresearch``
view in the 9119 dashboard can be built and exercised **before** the real
applying runner exists (Phase 5).

It mutates NOTHING except these three state files. It never edits skills, never
runs evals, never touches config/secrets. ``--clear`` removes the state files to
return the dashboard to ``idle``.

Examples::

    # simulate a running loop on iteration 2/5, fresh heartbeat
    python3 scripts/autoresearch_heartbeat_stub.py --dry-run --iteration 2 --max 5

    # simulate a crashed loop (stale heartbeat)
    python3 scripts/autoresearch_heartbeat_stub.py --dry-run --stale

    # reset to idle
    python3 scripts/autoresearch_heartbeat_stub.py --clear
"""
from __future__ import annotations

import argparse
import json
import os
import time
from datetime import datetime, timezone
from pathlib import Path

_REPO = Path(__file__).resolve().parents[1]
_DEFAULT_STATE_DIR = _REPO / ".hermes" / "skill-audit" / "runner-state"


def _state_dir() -> Path:
    override = os.environ.get("HERMES_AUTORESEARCH_STATE_DIR")
    return Path(override) if override else _DEFAULT_STATE_DIR


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")


def write_heartbeat(
    *,
    state_dir: Path,
    request_id: str,
    iteration: int,
    max_iterations: int,
    last_step: str,
    last_eval: str,
    stale: bool = False,
    route_status: str = "configured",
) -> Path:
    state_dir.mkdir(parents=True, exist_ok=True)
    pid = os.getpid()
    now = time.time()
    ts = now - 100000 if stale else now  # stale heartbeat → dashboard shows "crashed"

    (state_dir / "current.lock").write_text(
        json.dumps({"pid": pid, "request_id": request_id, "started_at": _utc_now()}, indent=2) + "\n",
        encoding="utf-8",
    )
    (state_dir / "current.heartbeat").write_text(
        json.dumps(
            {
                "pid": pid,
                "request_id": request_id,
                "iteration": iteration,
                "max": max_iterations,
                "last_step": last_step,
                "last_eval": last_eval,
                "ts": ts,
            },
            indent=2,
        )
        + "\n",
        encoding="utf-8",
    )
    (state_dir / "current.status").write_text(
        json.dumps(
            {
                "state": "running",
                "route_status": route_status,
                "last_receipt": None,
                "updated_at": _utc_now(),
                "dry_run": True,
            },
            indent=2,
        )
        + "\n",
        encoding="utf-8",
    )
    return state_dir


def clear(state_dir: Path) -> None:
    for name in ("current.lock", "current.heartbeat", "current.status"):
        target = state_dir / name
        if target.exists():
            target.unlink()


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--dry-run", action="store_true", help="write a simulated running heartbeat")
    parser.add_argument("--clear", action="store_true", help="remove state files (reset to idle)")
    parser.add_argument("--request-id", default="stub-dry-run")
    parser.add_argument("--iteration", type=int, default=1)
    parser.add_argument("--max", dest="max_iterations", type=int, default=5)
    parser.add_argument("--last-step", default="eval")
    parser.add_argument("--last-eval", default="keep")
    parser.add_argument("--stale", action="store_true", help="write an old ts so status shows crashed")
    args = parser.parse_args(argv)

    state_dir = _state_dir()
    if args.clear:
        clear(state_dir)
        print(f"cleared runner-state in {state_dir}")
        return 0
    if not args.dry_run:
        parser.error("pass --dry-run to write a simulated heartbeat, or --clear to reset")

    write_heartbeat(
        state_dir=state_dir,
        request_id=args.request_id,
        iteration=args.iteration,
        max_iterations=args.max_iterations,
        last_step=args.last_step,
        last_eval=args.last_eval,
        stale=args.stale,
    )
    print(f"wrote dry-run heartbeat ({'stale' if args.stale else 'fresh'}) to {state_dir}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
