#!/usr/bin/env python3
"""Stamp load + duration dilation into run-affected gate output.

Observability only. Never influences gate exit codes: every public entry point
swallows exceptions and exits 0. Callers in run-affected.sh also use ``|| true``
so a missing interpreter or script cannot trip ``set -e``.

Usage (two calls around the first test run)::

    gate_load_stamp.py start  --state FILE [--durations PATH] -- file1 file2 ...
    gate_load_stamp.py finish --state FILE [--durations PATH]

Prints exactly one compact stamp line on ``finish``. Prediction is the *serial*
sum of cached per-file durations; the real run is parallel (HERMES_TEST_WORKERS,
default 8). Dilation = actual_wall / predicted_serial — values < 1 are normal.
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import time
from pathlib import Path
from typing import Any


def is_repo_relative_test_key(key: str) -> bool:
    """True only for repo-relative test paths (filters absolute probe junk)."""
    if not key or key.startswith("/") or key.startswith("\\"):
        return False
    # Normalise accidental leading ./
    normalised = key[2:] if key.startswith("./") else key
    return normalised.startswith("tests/")


def filter_duration_map(raw: dict[Any, Any]) -> dict[str, float]:
    """Keep only repo-relative tests/… keys with numeric durations."""
    out: dict[str, float] = {}
    for key, value in raw.items():
        if not isinstance(key, str) or not is_repo_relative_test_key(key):
            continue
        try:
            seconds = float(value)
        except (TypeError, ValueError):
            continue
        if seconds < 0:
            continue
        normalised = key[2:] if key.startswith("./") else key
        out[normalised] = seconds
    return out


def load_duration_map(path: Path) -> dict[str, float] | None:
    """Load and filter test_durations.json. None if missing or corrupt."""
    try:
        if not path.is_file():
            return None
        raw = json.loads(path.read_text(encoding="utf-8"))
        if not isinstance(raw, dict):
            return None
        return filter_duration_map(raw)
    except (OSError, UnicodeError, json.JSONDecodeError, TypeError, ValueError):
        return None


def predict_serial(
    files: list[str],
    durations: dict[str, float] | None,
) -> tuple[float | None, int, int]:
    """Return (predicted_serial_seconds|None, matched_count, missing_count).

    When the cache is unavailable, predicted is None and every file is missing.
    """
    if not files:
        return (0.0 if durations is not None else None, 0, 0)
    if durations is None:
        return None, 0, len(files)

    matched = 0
    total = 0.0
    for path in files:
        key = path[2:] if path.startswith("./") else path
        if key in durations:
            total += durations[key]
            matched += 1
    missing = len(files) - matched
    return total, matched, missing


def parse_loadavg_1min(text: str) -> float | None:
    """Parse field 1 of /proc/loadavg content. None if unreadable format."""
    try:
        first = text.strip().split()[0]
        return float(first)
    except (IndexError, ValueError, AttributeError):
        return None


def read_load_1min(loadavg_path: Path = Path("/proc/loadavg")) -> float | None:
    try:
        return parse_loadavg_1min(loadavg_path.read_text(encoding="utf-8"))
    except OSError:
        return None


def cpu_count() -> int | None:
    try:
        n = os.cpu_count()
        return int(n) if n and n > 0 else None
    except (TypeError, ValueError):
        return None


def workers_default() -> int:
    raw = os.environ.get("HERMES_TEST_WORKERS", "8")
    try:
        n = int(raw)
        return n if n > 0 else 8
    except ValueError:
        return 8


def _fmt_seconds(value: float | None) -> str:
    if value is None:
        return "unknown"
    if value >= 100:
        return f"{value:.0f}s"
    if value >= 10:
        return f"{value:.1f}s"
    return f"{value:.2f}s"


def _fmt_load(value: float | None) -> str:
    if value is None:
        return "?"
    return f"{value:.1f}"


def format_stamp_line(
    *,
    file_count: int,
    predicted_serial: float | None,
    missing_count: int,
    actual_wall: float | None,
    load_before: float | None,
    load_after: float | None,
    cores: int | None,
    workers: int,
) -> str:
    """One compact observability line for the gate log."""
    noun = "file" if file_count == 1 else "files"
    pred = _fmt_seconds(predicted_serial)
    if predicted_serial is None:
        pred_part = "predicted serial unknown"
    else:
        pred_part = f"predicted serial {pred}"
        if missing_count:
            pred_part += f" ({missing_count} no-hist)"

    actual = _fmt_seconds(actual_wall)
    if predicted_serial is not None and predicted_serial > 0 and actual_wall is not None:
        dilation = actual_wall / predicted_serial
        dil_part = f"dilation {dilation:.2f}x (wall/serial-pred; parallel×{workers})"
    elif predicted_serial == 0.0 and actual_wall is not None:
        dil_part = f"dilation n/a (zero serial-pred; parallel×{workers})"
    else:
        dil_part = f"dilation n/a (parallel×{workers})"

    cores_s = str(cores) if cores is not None else "?"
    lb = _fmt_load(load_before)
    la = _fmt_load(load_after)
    if cores is not None and cores > 0 and load_before is not None and load_after is not None:
        per_b = load_before / cores
        per_a = load_after / cores
        load_part = (
            f"load before {lb} after {la} / {cores_s} cores "
            f"({per_b:.2f}→{per_a:.2f} per core)"
        )
    else:
        load_part = f"load before {lb} after {la} / {cores_s} cores"

    return (
        f"run-affected: {file_count} {noun} · {pred_part} · "
        f"actual wall {actual} · {dil_part} · {load_part}"
    )


def _default_durations_path() -> Path:
    return Path(__file__).resolve().parents[1] / "test_durations.json"


def _write_state(path: Path, payload: dict[str, Any]) -> None:
    path.write_text(json.dumps(payload), encoding="utf-8")


def _read_state(path: Path) -> dict[str, Any] | None:
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
        return raw if isinstance(raw, dict) else None
    except (OSError, UnicodeError, json.JSONDecodeError, TypeError, ValueError):
        return None


def cmd_start(args: argparse.Namespace) -> int:
    files = list(args.files)
    durations_path = Path(args.durations) if args.durations else _default_durations_path()
    durations = load_duration_map(durations_path)
    predicted, matched, missing = predict_serial(files, durations)
    state = {
        "t0": time.monotonic(),
        "files": files,
        "file_count": len(files),
        "predicted_serial": predicted,
        "matched": matched,
        "missing": missing,
        "load_before": read_load_1min(),
        "cores": cpu_count(),
        "workers": workers_default(),
        "durations_path": str(durations_path),
    }
    _write_state(Path(args.state), state)
    return 0


def cmd_finish(args: argparse.Namespace) -> int:
    state_path = Path(args.state)
    state = _read_state(state_path)
    load_after = read_load_1min()
    cores = cpu_count()
    workers = workers_default()

    if state is None:
        line = format_stamp_line(
            file_count=0,
            predicted_serial=None,
            missing_count=0,
            actual_wall=None,
            load_before=None,
            load_after=load_after,
            cores=cores,
            workers=workers,
        )
        print(line)
        return 0

    t0 = state.get("t0")
    actual: float | None
    try:
        actual = time.monotonic() - float(t0)
        if actual < 0:
            actual = None
    except (TypeError, ValueError):
        actual = None

    files = state.get("files") if isinstance(state.get("files"), list) else []
    file_count = int(state.get("file_count") or len(files))
    predicted = state.get("predicted_serial")
    if predicted is not None:
        try:
            predicted = float(predicted)
        except (TypeError, ValueError):
            predicted = None
    missing = int(state.get("missing") or 0)
    load_before = state.get("load_before")
    if load_before is not None:
        try:
            load_before = float(load_before)
        except (TypeError, ValueError):
            load_before = None
    if state.get("cores") is not None:
        try:
            cores = int(state["cores"])
        except (TypeError, ValueError):
            pass
    if state.get("workers") is not None:
        try:
            workers = int(state["workers"])
        except (TypeError, ValueError):
            pass

    # Re-resolve prediction if start left it unknown but a durations path is now usable
    # (normally start already resolved it).
    if predicted is None and args.durations:
        durations = load_duration_map(Path(args.durations))
        str_files = [str(f) for f in files]
        predicted, _matched, missing = predict_serial(str_files, durations)

    line = format_stamp_line(
        file_count=file_count,
        predicted_serial=predicted,
        missing_count=missing,
        actual_wall=actual,
        load_before=load_before,
        load_after=load_after,
        cores=cores,
        workers=workers,
    )
    print(line)
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="gate_load_stamp.py",
        description="Observability stamp for run-affected (never fails the gate).",
    )
    sub = parser.add_subparsers(dest="command", required=True)

    start = sub.add_parser("start", help="Record t0, load, and serial prediction.")
    start.add_argument("--state", required=True, help="Path to write stamp state JSON.")
    start.add_argument(
        "--durations",
        default=None,
        help="Path to test_durations.json (default: <repo>/test_durations.json).",
    )
    start.add_argument(
        "files",
        nargs="*",
        help="Selected test files (same set forwarded to run_tests.sh).",
    )
    start.set_defaults(func=cmd_start)

    finish = sub.add_parser("finish", help="Emit the stamp line after the run.")
    finish.add_argument("--state", required=True, help="Path to stamp state JSON.")
    finish.add_argument(
        "--durations",
        default=None,
        help="Optional durations path (normally unused; start already resolved).",
    )
    finish.set_defaults(func=cmd_finish)
    return parser


def main(argv: list[str] | None = None) -> int:
    """CLI entry — always returns 0 so set -e callers stay safe."""
    try:
        parser = build_parser()
        args = parser.parse_args(argv)
        args.func(args)
    except SystemExit as exc:
        # argparse uses SystemExit(2) on usage errors — still must not fail the gate.
        if exc.code not in (0, None):
            print(
                "run-affected: load-stamp unavailable (usage)",
                file=sys.stderr,
            )
        return 0
    except Exception as exc:  # noqa: BLE001 — observability must never raise out
        print(f"run-affected: load-stamp unavailable ({exc})", file=sys.stderr)
        return 0
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
