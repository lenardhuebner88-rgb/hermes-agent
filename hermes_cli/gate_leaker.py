"""Green-gate leaker/cause-purity: separate test-isolation leakers from real
regressions before either becomes the canonical ``first_fail`` cause.

The nightly ``green-gate-heartbeat`` runs the full suite with per-file parallel
isolation (``run_tests_parallel.py``) — so files run concurrently and a test can
fail only because of a *concurrent* neighbour (a shared kanban.db, a port, a temp
dir) yet pass when re-run alone. Such a test is a **leaker/flaky**, not a product
regression. Before this module the heartbeat took the raw first failing gate's
output (``fails[0]``) verbatim and recorded it as the night's ``first_fail`` —
so a leaker became the canonical red-cause exactly like a real regression, and
once the autoheal loop (``hermes vision gate-fix-check``) fires on a recurring
same-cause red streak it would open a HELD fix-PlanSpec for a non-product
problem.

This module adds the missing step (AC-1): on a red gate, re-run each reported
failing FILE once in **isolation** (bounded — :data:`ISOLATION_MAX_FILES` /
:data:`ISOLATION_MAX_SECONDS`), demote files that pass alone to *leakers*, and
pick the first IN-ISOLATION reproducible failure as ``first_fail``. The red
verdict itself is untouched (AC-2): the run is still booked as a fail; only the
*cause attribution* is cleaned. A file that genuinely fails only inside the suite
(real order/state coupling — a product defect) keeps failing alone and is NOT
demoted. A capped run never claims a clean ``leaker_only`` night — an unchecked
file behind the cap stays a first_fail candidate so a real regression can never
be hidden by the bound.

Two pure pieces live here so they are unit-testable without a test environment:

* :func:`parse_failed_files` — scrape the failing test-FILE paths from a gate's
  full log (pytest's ``run_tests_parallel.py`` summary; vitest ``FAIL`` lines).
* :func:`isolate_failures` — classify an isolation rerun via an INJECTED
  ``run_one`` callback (leaker vs reproduced, choose ``first_fail``, decide
  ``leaker_only``, respect the count/time bound).

:func:`build_runner` returns the real subprocess ``run_one`` the CLI wires up;
the bound and the classification are exercised by tests through the injection.
"""

from __future__ import annotations

import re
import subprocess
import time
from pathlib import Path
from typing import Callable, Optional

# Gates whose unit of work is a per-file test runner, so a single failing file
# can be re-run on its own. tsc / build are whole-project compiles — a failure
# there is deterministic (not a concurrency leaker), so they are never isolated.
ISOLATABLE_GATES = ("python", "vitest")

# Canonical gate order — matches the nightly heartbeat's run order, so the
# cross-gate ``first_fail`` chosen here lines up with the gate the operator sees
# fail first.
GATE_ORDER = ("python", "tsc", "vitest", "build")

# Bounded isolation defaults (AC-2: the rerun must be capped on count AND time
# so a pathological red night can't make the heartbeat run for hours). The
# nightly heartbeat already has an 1800s TimeoutStartSec; these stay well under
# it and are overridable from the CLI.
ISOLATION_MAX_FILES = 12
ISOLATION_MAX_SECONDS = 600.0
ISOLATION_PER_FILE_TIMEOUT = 240.0

# How many trailing log lines to keep as the reproduced-failure detail. The
# ledger re-redacts and caps this; a short tail keeps the cause readable.
_DETAIL_TAIL_LINES = 12

# run_tests_parallel.py end-of-run summary section headers that enumerate the
# canonical failing files (one ``  <path>.py  (...)`` line each).
_PY_FAIL_SECTIONS = (
    "with test failures",
    "where all tests passed but pytest exited non-zero",
    "where no tests ran",
)

_PY_SUMMARY_FILE_RE = re.compile(r"^\s+(\S+\.py)\b")
_PY_REPRO_RE = re.compile(r"Repro:\s*python -m pytest\s+(\S+\.py)\b")
# vitest: " FAIL  src/foo.test.tsx > suite > name" and the file-summary
# " ❯ src/foo.test.ts (5 tests | 2 failed)".
_VITEST_TEST_FILE = r"(\S+\.(?:test|spec)\.[cm]?[jt]sx?)"
_VITEST_FAIL_RE = re.compile(r"^\s*FAIL\s+" + _VITEST_TEST_FILE + r"\b")
_VITEST_ARROW_RE = re.compile(r"❯\s+" + _VITEST_TEST_FILE + r"\b[^\n]*\bfailed")


def _dedup(items: list[str]) -> list[str]:
    seen: set[str] = set()
    out: list[str] = []
    for it in items:
        if it not in seen:
            seen.add(it)
            out.append(it)
    return out


def _parse_python(log_text: str) -> list[str]:
    files: list[str] = []
    in_section = False
    for line in log_text.splitlines():
        stripped = line.strip()
        if stripped.startswith("===") and stripped.endswith("==="):
            # Only the explicit failing-file summary sections count — the
            # "Failure output" raw dumps and the "Top 10 slowest" diagnostic
            # both mention tests/*.py paths that are NOT the canonical list.
            in_section = any(h in stripped for h in _PY_FAIL_SECTIONS)
            continue
        if in_section:
            m = _PY_SUMMARY_FILE_RE.match(line)
            if m:
                files.append(m.group(1))
    # Fallback for a truncated log with no summary block: the per-file inline
    # failures each print a ``Repro: python -m pytest <file>`` line.
    files.extend(m.group(1) for m in _PY_REPRO_RE.finditer(log_text))
    return _dedup(files)


def _parse_vitest(log_text: str) -> list[str]:
    files: list[str] = []
    for line in log_text.splitlines():
        m = _VITEST_FAIL_RE.match(line)
        if m:
            files.append(m.group(1))
    files.extend(m.group(1) for m in _VITEST_ARROW_RE.finditer(log_text))
    return _dedup(files)


def parse_failed_files(gate: Optional[str], log_text: str) -> list[str]:
    """Extract the failing test-FILE paths from a gate's full log.

    Returns the files in report order (deduped). Non-isolatable gates
    (tsc/build/unknown) return ``[]`` — there is nothing to re-run in isolation.
    """
    g = str(gate or "").strip().lower()
    if g == "python":
        return _parse_python(log_text or "")
    if g == "vitest":
        return _parse_vitest(log_text or "")
    return []


def isolate_failures(
    gate: Optional[str],
    failed_files: list[str],
    *,
    run_one: Callable[[str], tuple[bool, str]],
    max_files: int = ISOLATION_MAX_FILES,
    max_seconds: float = ISOLATION_MAX_SECONDS,
    monotonic: Callable[[], float] = time.monotonic,
) -> dict:
    """Classify a bounded isolation rerun of one gate's failing files.

    ``run_one(file)`` returns ``(passed_alone, detail_tail)``. A file that passes
    alone is a *leaker* (demoted from the cause); a file that fails alone
    *reproduces* (a real product failure). The rerun is bounded: it stops once
    ``max_files`` files have been checked OR ``max_seconds`` of wall time has
    elapsed; remaining files are ``unchecked`` (``capped`` is then ``True``).

    ``first_fail`` is the FIRST failing file in report order that is not a
    confirmed leaker — a reproduced file, or (when the cap was hit) an unchecked
    one. That keeps a real regression behind the cap from being silently dropped
    (AC-2). ``leaker_only`` is ``True`` only when every failing file was checked
    and every one passed alone — i.e. the whole gate failure is harness noise; a
    capped run never claims it.
    """
    g = str(gate or "").strip().lower()
    leakers: list[str] = []
    reproduced: list[dict] = []
    checked: list[str] = []
    unchecked: list[str] = []

    start = monotonic()
    for f in failed_files:
        budget_hit = len(checked) >= max_files or (monotonic() - start) >= max_seconds
        if budget_hit:
            unchecked.append(f)
            continue
        passed, detail = run_one(f)
        checked.append(f)
        if passed:
            leakers.append(f)
        else:
            reproduced.append({"file": f, "detail": detail or ""})

    leaker_set = set(leakers)
    repro_detail = {r["file"]: r["detail"] for r in reproduced}
    first_fail: Optional[dict] = None
    for f in failed_files:
        if f in leaker_set:
            continue
        first_fail = {"gate": g, "file": f, "detail": repro_detail.get(f, "")}
        break

    capped = bool(unchecked)
    leaker_only = (
        len(failed_files) > 0
        and not capped
        and not reproduced
        and len(leakers) == len(failed_files)
    )
    return {
        "gate": g,
        "failed_total": len(failed_files),
        "checked": checked,
        "leakers": leakers,
        "reproduced": reproduced,
        "unchecked": unchecked,
        "capped": capped,
        "first_fail": first_fail,
        "leaker_only": leaker_only,
    }


def _tail(log_text: str, lines: int = _DETAIL_TAIL_LINES) -> str:
    return "\n".join((log_text or "").rstrip().splitlines()[-lines:])


def _deterministic_gate_result(gate: str, log_text: str) -> dict:
    """A non-isolatable (tsc/build) or unparseable red gate: a deterministic
    failure that is NEVER a leaker — keep its log tail as the cause."""
    return {
        "gate": gate,
        "failed_total": 0,
        "checked": [],
        "leakers": [],
        "reproduced": [],
        "unchecked": [],
        "capped": False,
        "first_fail": {"gate": gate, "file": None, "detail": _tail(log_text)},
        "leaker_only": False,
    }


def format_first_fail_detail(first_fail: Optional[dict]) -> str:
    """Render a ``first_fail`` dict into the detail string the ledger stores."""
    if not first_fail:
        return ""
    detail = first_fail.get("detail") or ""
    file = first_fail.get("file")
    gate = first_fail.get("gate") or ""
    if file:
        head = f"{gate} (isolated): {file}"
        return f"{head}\n{detail}" if detail else head
    return detail


def isolate_from_logs(
    gate_logs: list[tuple[str, str]],
    *,
    runner_factory: Callable[[str], Callable[[str], tuple[bool, str]]],
    max_files: int = ISOLATION_MAX_FILES,
    max_seconds: float = ISOLATION_MAX_SECONDS,
    monotonic: Callable[[], float] = time.monotonic,
) -> dict:
    """Clean the night's cause across every failing gate.

    Walks the failing gates in :data:`GATE_ORDER`; isolatable gates (python,
    vitest) get a bounded isolation rerun (the count/time bound is shared across
    gates), non-isolatable gates (tsc, build) and unparseable isolatable logs are
    treated as deterministic non-leaker failures. ``first_fail`` is the first
    non-leaker failure in gate order; ``leaker_only`` is ``True`` only when there
    was at least one failing gate and EVERY one was pure harness noise.

    ``runner_factory(gate)`` returns the ``run_one`` for that gate (injected so
    tests need no subprocess). Returns an aggregate dict the CLI flattens for the
    heartbeat.
    """
    by_gate: dict[str, str] = {}
    for gate, text in gate_logs:
        g = str(gate or "").strip().lower()
        if g:
            by_gate[g] = text or ""
    ordered = [g for g in GATE_ORDER if g in by_gate]
    ordered += [g for g in by_gate if g not in GATE_ORDER]

    global_start = monotonic()
    per_gate: dict[str, dict] = {}
    all_leakers: list[str] = []
    first_fail: Optional[dict] = None
    leaker_only = True
    any_fail_gate = False
    files_checked = 0
    leaker_total = 0
    reproduced_total = 0

    for g in ordered:
        text = by_gate[g]
        any_fail_gate = True
        if g in ISOLATABLE_GATES:
            files = parse_failed_files(g, text)
            if files:
                remaining_files = max(0, max_files - files_checked)
                remaining_seconds = max(
                    0.0, max_seconds - (monotonic() - global_start)
                )
                res = isolate_failures(
                    g,
                    files,
                    run_one=runner_factory(g),
                    max_files=remaining_files,
                    max_seconds=remaining_seconds,
                    monotonic=monotonic,
                )
                files_checked += len(res["checked"])
                leaker_total += len(res["leakers"])
                reproduced_total += len(res["reproduced"])
                all_leakers.extend(f"{g}: {x}" for x in res["leakers"])
                if not res["leaker_only"]:
                    leaker_only = False
                if first_fail is None and res["first_fail"]:
                    first_fail = res["first_fail"]
                per_gate[g] = res
                continue
            res = _deterministic_gate_result(g, text)
        else:
            res = _deterministic_gate_result(g, text)
        per_gate[g] = res
        leaker_only = False
        if first_fail is None and res["first_fail"]:
            first_fail = res["first_fail"]

    return {
        "first_fail": first_fail,
        "leakers": all_leakers,
        "leaker_only": leaker_only and any_fail_gate,
        "checked": files_checked,
        "leaker_total": leaker_total,
        "reproduced_total": reproduced_total,
        "capped": any(r.get("capped") for r in per_gate.values()),
        "per_gate": per_gate,
    }


def isolation_command(gate: Optional[str], file: str) -> list[str]:
    """The argv that re-runs one failing file ALONE for a gate.

    pytest goes through ``scripts/run_tests.sh <file>`` so the isolation rerun
    uses the exact same hermetic env (``env -i`` / TZ / PYTHONHASHSEED) the
    nightly gate used — only without concurrent neighbours. vitest runs the
    single file via the repo-local binary.
    """
    g = str(gate or "").strip().lower()
    if g == "python":
        return ["scripts/run_tests.sh", file]
    if g == "vitest":
        return ["node_modules/.bin/vitest", "run", file]
    raise ValueError(f"gate {g!r} is not per-file isolatable")


def build_runner(
    gate: Optional[str],
    repo_root,
    *,
    per_file_timeout: float = ISOLATION_PER_FILE_TIMEOUT,
    tail_lines: int = _DETAIL_TAIL_LINES,
    env: Optional[dict] = None,
) -> Callable[[str], tuple[bool, str]]:
    """Return the real subprocess ``run_one`` for an isolatable gate.

    The closure runs the single failing file alone and reports
    ``(passed_alone, detail_tail)``. A timeout counts as *reproduced* (not a
    leaker) — a file that cannot even finish alone is not harmless noise.
    """
    repo_root = Path(repo_root)
    g = str(gate or "").strip().lower()
    cwd = repo_root / "web" if g == "vitest" else repo_root

    def run_one(file: str) -> tuple[bool, str]:
        cmd = isolation_command(g, file)
        try:
            proc = subprocess.run(
                cmd,
                cwd=str(cwd),
                capture_output=True,
                text=True,
                timeout=per_file_timeout,
                env=env,
            )
        except subprocess.TimeoutExpired as exc:
            out = exc.output if isinstance(exc.output, str) else ""
            tail = "\n".join((out or "").rstrip().splitlines()[-tail_lines:])
            return (False, f"isolation rerun TIMEOUT after {per_file_timeout}s\n{tail}")
        except (OSError, ValueError) as exc:  # toolchain missing etc.
            # Cannot verify -> do NOT demote: report as reproduced so the file
            # stays a candidate cause rather than being wrongly cleared.
            return (False, f"isolation rerun could not run: {exc}")
        out = (proc.stdout or "") + (proc.stderr or "")
        tail = "\n".join(out.rstrip().splitlines()[-tail_lines:])
        return (proc.returncode == 0, tail)

    return run_one
