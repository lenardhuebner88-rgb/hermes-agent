"""``hermes vision`` — Vision-Flywheel harness subcommands.

``hermes vision strategist --mode propose|reflect`` is the repo-side logic the
operator's ``strategist-cron`` invokes via ``claude -p --model claude-opus-4-8``.
See ``docs/vision-strategist-harness.md`` for the full call contract
(inputs / outputs / exit codes).
"""

from __future__ import annotations

import argparse
import json
import sys

from hermes_cli import strategist


def build_vision_parser(subparsers) -> None:
    parser = subparsers.add_parser(
        "vision",
        help="Vision-Flywheel: the self-improving PlanSpec pipeline harness",
        description="Strategist propose/reflect harness (Phase 2 of the vision flywheel).",
    )
    parser.set_defaults(func=vision_command, _vision_parser=parser)
    sub = parser.add_subparsers(dest="vision_action")

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
    strat.set_defaults(_vision_parser=parser)


def vision_command(args: argparse.Namespace) -> int:
    action = getattr(args, "vision_action", None)
    if action != "strategist":
        parser = getattr(args, "_vision_parser", None)
        if parser is not None:
            parser.print_help()
        return 0
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
