"""``hermes vision`` — Vision-Flywheel harness subcommands.

Repo-side logic for the self-improving PlanSpec pipeline. All leaf commands are
pure repo-side logic; the cron / heartbeat that invokes them is an operator step.

* ``hermes vision strategist --mode propose|reflect`` — the self-gated,
  budget-disciplined ROI proposer / reflector the ``strategist-cron`` invokes via
  ``claude -p --model claude-opus-4-8``. See ``docs/vision-strategist-harness.md``
  for the full call contract (inputs / outputs / exit codes).
* ``hermes vision metrics-snapshot`` — write the distilled metrics file
  (autonomy %, cost/task trend, escalation rate, green-gate streak — each with a
  paired counter metric).
* ``hermes vision record-gate-result pass|fail`` — append a structured green-gate
  record so the consecutive-green-nights streak is derivable.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Optional

from hermes_cli import gate_leaker
from hermes_cli import strategist
from hermes_cli import vision_metrics as vm


def build_vision_parser(subparsers) -> None:
    parser = subparsers.add_parser(
        "vision",
        help="Vision-Flywheel: self-improving PlanSpec pipeline harness + metrics",
        description=(
            "Vision-Flywheel harness: the strategist propose/reflect loop plus the "
            "distilled trust metrics and green-gate ledger that feed it."
        ),
    )
    parser.set_defaults(func=vision_command, _vision_parser=parser)
    sub = parser.add_subparsers(dest="vision_action")

    # --- strategist (propose / reflect) ---
    strat = sub.add_parser(
        "strategist",
        help="Self-gated, budget-disciplined ROI proposer / reflector",
        description=(
            "PROPOSE: read vision metrics + Heiler ledger, draft <=5 ROI-positive, "
            "self-gated PlanSpecs and ingest the survivors held (freigabe:operator, "
            "created_by=strategist-cron). REFLECT: score approved-vs-vetoed since "
            "morning and update the learning notes (vetoed feeds suppression)."
        ),
    )
    strat.add_argument(
        "--mode",
        choices=["propose", "reflect", "harvest", "digest"],
        required=True,
        help="propose, reflect, harvest oder digest",
    )
    strat.add_argument("--board", default=None, help="Kanban board slug (defaults to current board)")
    strat.add_argument("--json", action="store_true", help="Emit JSON output")
    strat.add_argument(
        "--budget-provider",
        default=strategist.BUDGET_PROVIDER,
        help="Subscription provider whose weekly window gates the run",
    )
    strat.add_argument(
        "--budget-threshold",
        type=float,
        default=strategist.BUDGET_THRESHOLD,
        help="Skip the run when weekly usage exceeds this percent",
    )
    strat.add_argument(
        "--cap", type=int, default=strategist.CAP_MAX, help="Max proposals per run (cap 3-5)"
    )
    strat.add_argument(
        "--out-dir",
        default=None,
        help="Directory for drafted PlanSpec markdown (default: <hermes-home>/state/strategist/specs)",
    )
    strat.add_argument(
        "--drafts-file",
        default=None,
        help="Optional JSON of Opus-judged lever drafts fed through the same self-gate + ingest rails",
    )
    strat.add_argument(
        "--digest-file",
        default=None,
        help="(--mode digest) JSON of the harvest clustering decision (clusters[]+left[]) "
        "to validate + persist as disposition_digest.json",
    )
    strat.add_argument(
        "--dry-run",
        action="store_true",
        help="Derive + self-gate only; do not write or ingest",
    )

    # --- gate-fix-check (GREEN-GATE-AUTOHEAL-LOOP-S1) ---
    gatefix = sub.add_parser(
        "gate-fix-check",
        help=(
            "Open a HELD fix-PlanSpec when the nightly green-gate has been red "
            ">=2 consecutive nights with the same first_fail cause"
        ),
        description=(
            "Bounded, idempotent self-heal loop for the nightly green-gate: when "
            "the most recent recorded night is red AND >= --min-nights "
            "consecutive recorded nights share the same first_fail cause, ingest "
            "a single freigabe:operator (HELD) fix-PlanSpec so the recurring "
            "cause is surfaced for operator triage instead of sitting on "
            "green_gate_streak=0 unnoticed. Never auto-releases, never deploys; "
            "re-running on the same cause dedups (no spam). Idle when nothing "
            "recurs. Intended to run right after `record-gate-result` in the "
            "nightly heartbeat."
        ),
    )
    gatefix.add_argument("--board", default=None, help="Kanban board slug (defaults to current board)")
    gatefix.add_argument(
        "--min-nights",
        type=int,
        default=strategist.GATE_FIX_MIN_NIGHTS,
        help="Consecutive same-cause red nights required to open a fix-PlanSpec (default 2)",
    )
    gatefix.add_argument(
        "--out-dir",
        default=None,
        help="Directory for the drafted fix-PlanSpec markdown (default: <hermes-home>/state/strategist/specs)",
    )
    gatefix.add_argument(
        "--dry-run",
        action="store_true",
        help="Detect only; do not write or ingest a PlanSpec",
    )
    gatefix.add_argument("--json", action="store_true", help="Emit JSON output")

    # --- triage-check (GREEN-GATE-PERSISTENT-RED-TRIAGE-S1) ---
    triage = sub.add_parser(
        "triage-check",
        help=(
            "Open a HELD Triage-PlanSpec when the green-gate is red >=N of M "
            "nights, regardless of first_fail cause (changing-cause trigger)"
        ),
        description=(
            "Bounded, idempotent N-of-M persistent-red triage: when the most "
            "recent recorded night is red AND >= --min-reds of the last "
            "--window recorded nights are red — REGARDLESS of whether the "
            "first_fail cause changed between nights — ingest a single "
            "freigabe:operator (HELD) Triage-PlanSpec listing the CURRENTLY "
            "red test files. Orthogonal to gate-fix-check (which requires the "
            "SAME first_fail cause on consecutive nights); the two paths "
            "produce distinct keys so neither double-ingests. Never "
            "auto-releases, never deploys; re-running on the same red file set "
            "dedups (no spam). Idle when the head is green or fewer than "
            "--min-reds reds are in the window. Intended to run right after "
            "gate-fix-check in the nightly heartbeat."
        ),
    )
    triage.add_argument("--board", default=None, help="Kanban board slug (defaults to current board)")
    triage.add_argument(
        "--min-reds",
        type=int,
        default=strategist.GATE_TRIAGE_MIN_REDS,
        help="Red nights required in the window to open a triage-PlanSpec (default 2)",
    )
    triage.add_argument(
        "--window",
        type=int,
        default=strategist.GATE_TRIAGE_WINDOW,
        help="Number of recent nights to examine (default 3)",
    )
    triage.add_argument(
        "--out-dir",
        default=None,
        help="Directory for the drafted triage-PlanSpec markdown (default: <hermes-home>/state/strategist/specs)",
    )
    triage.add_argument(
        "--dry-run",
        action="store_true",
        help="Detect only; do not write or ingest a PlanSpec",
    )
    triage.add_argument("--json", action="store_true", help="Emit JSON output")

    # --- metrics-snapshot ---
    snapshot = sub.add_parser(
        "metrics-snapshot",
        help="Compute and write ~/.hermes/state/vision-metrics.json",
    )
    snapshot.add_argument("--board", default=None, help="Kanban board slug")
    snapshot.add_argument(
        "--window-days",
        type=int,
        default=7,
        help="Window (days) for the rate/trend metrics (default 7)",
    )
    snapshot.add_argument("--json", action="store_true", help="Emit JSON output")

    # --- record-gate-result ---
    record = sub.add_parser(
        "record-gate-result",
        help="Append a structured green-gate record (pass|fail) to the ledger",
    )
    record.add_argument("result", choices=list(vm.GATE_RESULTS))
    record.add_argument(
        "--ts",
        default=None,
        help="ISO-8601 timestamp of the gate run (default: now)",
    )
    record.add_argument(
        "--first-fail-gate",
        default=None,
        help=(
            "On a fail: the first failing gate (python|tsc|vitest|build). "
            "Stored on the ledger entry as a machine-readable cause; ignored "
            "for a pass."
        ),
    )
    record.add_argument(
        "--first-fail-detail",
        default=None,
        help=(
            "On a fail: the first non-empty failure text (a short stderr "
            "tail). Redacted and capped before it is written to the ledger; "
            "ignored for a pass."
        ),
    )
    record.add_argument(
        "--leakers-json",
        default=None,
        help=(
            "On a fail: JSON array of demoted leaker entries (\"<gate>: "
            "<file>\" — failing files that passed alone in the isolation "
            "rerun). Stored bounded/redacted for operator visibility, never as "
            "a cause; ignored for a pass."
        ),
    )
    record.add_argument(
        "--leaker-only",
        action="store_true",
        help=(
            "On a fail: every reported fail was a test-isolation leaker -> "
            "record the night red with NO product cause (first_fail "
            "suppressed). The red verdict / streak is unchanged."
        ),
    )
    record.add_argument("--json", action="store_true", help="Emit JSON output")

    # --- isolate-fails (GREEN-GATE-LEAKER-CAUSE-PURITY-S1) ---
    isolate = sub.add_parser(
        "isolate-fails",
        help=(
            "Re-run a red gate's reported failing files in isolation and emit "
            "the cleaned first_fail cause + the demoted leaker list"
        ),
        description=(
            "Cause-purity step for the nightly green-gate: given the failing "
            "gate logs, re-run each reported failing FILE once in isolation "
            "(bounded on count + time). Files that pass alone are demoted as "
            "test-isolation leakers; the first IN-ISOLATION reproducible failure "
            "(in gate order) becomes first_fail. Emits JSON the heartbeat feeds "
            "to record-gate-result so a leaker never becomes the canonical "
            "red-cause (nor a gate-fix-check cause). Never changes the red "
            "verdict — only the cause attribution."
        ),
    )
    isolate.add_argument(
        "--repo",
        default=None,
        help="Repo root for the isolation reruns (default: current directory)",
    )
    isolate.add_argument(
        "--gate-log",
        action="append",
        default=[],
        metavar="GATE=PATH",
        help=(
            "A failing gate and its full log file, e.g. "
            "'python=/logs/python.log' (repeatable; only failing gates)"
        ),
    )
    isolate.add_argument(
        "--max-files",
        type=int,
        default=gate_leaker.ISOLATION_MAX_FILES,
        help=f"Cap on files re-run in isolation (default {gate_leaker.ISOLATION_MAX_FILES})",
    )
    isolate.add_argument(
        "--max-seconds",
        type=float,
        default=gate_leaker.ISOLATION_MAX_SECONDS,
        help=(
            "Cap on total wall time for the isolation reruns "
            f"(default {gate_leaker.ISOLATION_MAX_SECONDS:.0f}s)"
        ),
    )
    isolate.add_argument(
        "--per-file-timeout",
        type=float,
        default=gate_leaker.ISOLATION_PER_FILE_TIMEOUT,
        help=(
            "Per-file wall-clock cap for one isolation rerun "
            f"(default {gate_leaker.ISOLATION_PER_FILE_TIMEOUT:.0f}s)"
        ),
    )
    isolate.add_argument("--json", action="store_true", help="Emit JSON output")


def vision_command(args: argparse.Namespace) -> int:
    action = getattr(args, "vision_action", None)

    if action == "strategist":
        try:
            if args.mode == "propose":
                result = strategist.run_propose(args)
            elif args.mode == "harvest":
                result = strategist.run_harvest(args)
            elif args.mode == "digest":
                result = strategist.run_digest(args)
            else:
                result = strategist.run_reflect(args)
        except (FileNotFoundError, ValueError) as exc:
            print(f"vision strategist: {exc}", file=sys.stderr)
            return 2

        if getattr(args, "json", False):
            print(json.dumps(result, ensure_ascii=False))
            return 0

        if result["mode"] == "propose":
            if result.get("skipped"):
                print(f"strategist propose: SKIPPED — {result['reason']}")
            elif result.get("idle"):
                print(
                    f"strategist propose: idle — {result['candidates']} candidate(s), "
                    f"none ROI-positive after self-gate ({len(result.get('gated_out', []))} gated out)"
                )
            else:
                print(
                    f"strategist propose: {len(result['ingested'])} held proposal(s) ingested "
                    f"(cap {result['cap']}, {result['candidates']} candidates, "
                    f"{len(result.get('gated_out', []))} gated out)"
                )
                for item in result["ingested"]:
                    print(f"  - {item['key']}: {item['title']} → root {item.get('root_task_id')}")
        elif result["mode"] == "harvest":
            print(
                f"strategist harvest: {result['receipts']} receipt(s), "
                f"{result['candidates']} candidate(s) → {result['candidates_path']}"
            )
        elif result["mode"] == "digest":
            print(
                f"strategist digest: {result['clusters']} cluster(s), "
                f"{result['reaped']} reaped, {result['left']} left "
                f"(of {result['total_open']} open) → {result['digest_path']}"
            )
        else:
            print(
                f"strategist reflect: {result['note']['approved']} approved, "
                f"{result['note']['vetoed']} vetoed, {result['note']['shipped']} shipped "
                f"(suppressed levers: {', '.join(result['note']['vetoed_levers']) or 'none'})"
            )
        return 0

    if action == "gate-fix-check":
        result = strategist.run_gate_fix(args)
        if getattr(args, "json", False):
            print(json.dumps(result, ensure_ascii=False))
            return 0
        if not result.get("triggered"):
            print(f"gate-fix-check: idle — {result.get('reason')}")
            return 0
        ingested = result.get("ingested") or {}
        if ingested.get("dry_run"):
            print(
                f"gate-fix-check (dry-run): would open HELD fix-PlanSpec "
                f"{result['key']} for gate '{result['gate']}' "
                f"({result['red_nights']} red nights, fingerprint {result['fingerprint']})"
            )
        elif result.get("ingest_error"):
            print(
                f"gate-fix-check: detection fired for gate '{result['gate']}' but ingest "
                f"was blocked — {'; '.join(result['ingest_error'].get('findings', []))}"
            )
        else:
            verb = "already held (dedup)" if ingested.get("already_ingested") else "ingested HELD"
            print(
                f"gate-fix-check: gate '{result['gate']}' red {result['red_nights']} nights → "
                f"{verb} fix-PlanSpec {ingested.get('key')} → root {ingested.get('root_task_id')}"
            )
        return 0

    if action == "triage-check":
        result = strategist.run_persistent_red_triage(args)
        if getattr(args, "json", False):
            print(json.dumps(result, ensure_ascii=False))
            return 0
        if not result.get("triggered"):
            print(f"triage-check: idle — {result.get('reason')}")
            return 0
        ingested = result.get("ingested") or {}
        if ingested.get("dry_run"):
            print(
                f"triage-check (dry-run): would open HELD Triage-PlanSpec "
                f"{result['key']} for gate '{result['gate']}' "
                f"({result['red_count']} red of {result['window']} nights, "
                f"fingerprint {result['fingerprint']})"
            )
        elif result.get("ingest_error"):
            print(
                f"triage-check: detection fired for gate '{result['gate']}' but ingest "
                f"was blocked — {'; '.join(result['ingest_error'].get('findings', []))}"
            )
        else:
            verb = "already held (dedup)" if ingested.get("already_ingested") else "ingested HELD"
            print(
                f"triage-check: gate '{result['gate']}' red {result['red_count']} of "
                f"{result['window']} nights → {verb} Triage-PlanSpec "
                f"{ingested.get('key')} → root {ingested.get('root_task_id')}"
            )
        return 0

    if action == "metrics-snapshot":
        path, snapshot = vm.write_metrics_snapshot(
            board=getattr(args, "board", None),
            window_days=getattr(args, "window_days", 7),
        )
        if getattr(args, "json", False):
            print(json.dumps(snapshot, ensure_ascii=False))
        else:
            print(vm.render_snapshot_summary(snapshot))
            print(f"\nwrote {path}")
        return 0

    if action == "record-gate-result":
        leakers = _parse_leakers_json(getattr(args, "leakers_json", None))
        record = vm.record_gate_result(
            args.result,
            ts=getattr(args, "ts", None),
            first_fail_gate=getattr(args, "first_fail_gate", None),
            first_fail_detail=getattr(args, "first_fail_detail", None),
            leakers=leakers,
            leaker_only=getattr(args, "leaker_only", False),
        )
        if getattr(args, "json", False):
            print(json.dumps(record, ensure_ascii=False))
        else:
            note = ""
            if record.get("leaker_only"):
                note = " (leaker-only: no product cause)"
            elif record.get("leakers"):
                note = f" ({len(record['leakers'])} leaker(s) demoted)"
            print(
                f"recorded green-gate {record['result']} "
                f"for {record['date']}{note} → {vm.gate_ledger_path()}"
            )
        return 0

    if action == "isolate-fails":
        result = run_isolate_fails(args)
        if getattr(args, "json", False):
            print(json.dumps(result, ensure_ascii=False))
        else:
            ff = result.get("first_fail_gate")
            if result.get("leaker_only"):
                print(
                    f"isolate-fails: leaker-only — all {result['leaker_total']} "
                    f"reported fail(s) passed alone (no product cause)"
                )
            elif ff:
                print(
                    f"isolate-fails: first_fail gate '{ff}' "
                    f"({result['reproduced_total']} reproduced, "
                    f"{result['leaker_total']} leaker(s) demoted"
                    f"{', CAPPED' if result.get('capped') else ''})"
                )
            else:
                print("isolate-fails: nothing to isolate (no failing gate logs)")
        return 0

    parser = getattr(args, "_vision_parser", None)
    if parser is not None:
        parser.print_help()
    return 0


def _parse_leakers_json(raw: Optional[str]) -> Optional[list]:
    """Parse the ``--leakers-json`` value into a list of strings (or None)."""
    if not raw:
        return None
    try:
        parsed = json.loads(raw)
    except (json.JSONDecodeError, TypeError, ValueError):
        return None
    if not isinstance(parsed, list):
        return None
    return [str(x) for x in parsed]


def _read_log(path: str) -> str:
    try:
        return Path(path).expanduser().read_text(encoding="utf-8", errors="replace")
    except OSError:
        return ""


def run_isolate_fails(args) -> dict:
    """Drive :func:`gate_leaker.isolate_from_logs` for the ``isolate-fails`` CLI.

    Reads each ``--gate-log GATE=PATH``, runs the bounded isolation rerun via the
    real subprocess runner, and flattens the result into the fields the heartbeat
    consumes (``first_fail_gate`` / ``first_fail_detail`` / ``leakers`` /
    ``leaker_only``). Never raises on a missing log — a best-effort empty string
    is used so the heartbeat can always fall back to the raw cause.
    """
    repo = Path(getattr(args, "repo", None) or ".").expanduser()
    gate_logs: list[tuple[str, str]] = []
    for spec in getattr(args, "gate_log", None) or []:
        if "=" not in spec:
            continue
        gate, _, path = spec.partition("=")
        gate = gate.strip().lower()
        if gate:
            gate_logs.append((gate, _read_log(path.strip())))

    def factory(gate: str):
        return gate_leaker.build_runner(
            gate,
            repo,
            per_file_timeout=getattr(
                args, "per_file_timeout", gate_leaker.ISOLATION_PER_FILE_TIMEOUT
            ),
        )

    result = gate_leaker.isolate_from_logs(
        gate_logs,
        runner_factory=factory,
        max_files=getattr(args, "max_files", gate_leaker.ISOLATION_MAX_FILES),
        max_seconds=getattr(args, "max_seconds", gate_leaker.ISOLATION_MAX_SECONDS),
    )
    ff = result.get("first_fail")
    return {
        "first_fail_gate": (ff or {}).get("gate"),
        "first_fail_detail": gate_leaker.format_first_fail_detail(ff),
        "leakers": result.get("leakers", []),
        "leaker_only": result.get("leaker_only", False),
        "checked": result.get("checked", 0),
        "leaker_total": result.get("leaker_total", 0),
        "reproduced_total": result.get("reproduced_total", 0),
        "capped": result.get("capped", False),
    }
