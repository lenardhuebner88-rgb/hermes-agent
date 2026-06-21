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
        "--mode", choices=["propose", "reflect"], required=True, help="propose or reflect"
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
    record.add_argument("--json", action="store_true", help="Emit JSON output")


def vision_command(args: argparse.Namespace) -> int:
    action = getattr(args, "vision_action", None)

    if action == "strategist":
        try:
            if args.mode == "propose":
                result = strategist.run_propose(args)
            else:
                result = strategist.run_reflect(args)
        except FileNotFoundError as exc:
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
        record = vm.record_gate_result(
            args.result,
            ts=getattr(args, "ts", None),
            first_fail_gate=getattr(args, "first_fail_gate", None),
            first_fail_detail=getattr(args, "first_fail_detail", None),
        )
        if getattr(args, "json", False):
            print(json.dumps(record, ensure_ascii=False))
        else:
            print(
                f"recorded green-gate {record['result']} "
                f"for {record['date']} → {vm.gate_ledger_path()}"
            )
        return 0

    parser = getattr(args, "_vision_parser", None)
    if parser is not None:
        parser.print_help()
    return 0
