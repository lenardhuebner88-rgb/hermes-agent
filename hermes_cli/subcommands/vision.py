"""``hermes vision`` — distilled vision-flywheel metrics + green-gate ledger.

Two leaf commands, both pure repo-side logic (the cron/heartbeat that calls
them is an operator step):

* ``hermes vision metrics-snapshot`` — write the distilled metrics file.
* ``hermes vision record-gate-result pass|fail`` — append a green-gate record.
"""

from __future__ import annotations

import argparse
import json

from hermes_cli import vision_metrics as vm


def build_vision_parser(subparsers) -> None:
    parser = subparsers.add_parser(
        "vision",
        help="Distilled vision-flywheel metrics + the green-gate ledger",
        description=(
            "Precompute the distilled trust metrics (autonomy %, cost/task "
            "trend, escalation rate, green-gate streak — each with a paired "
            "counter metric) and record structured green-gate results so the "
            "consecutive-green-nights streak is derivable."
        ),
    )
    parser.set_defaults(func=vision_command, _vision_parser=parser)
    sub = parser.add_subparsers(dest="vision_action")

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
    record.add_argument("--json", action="store_true", help="Emit JSON output")


def vision_command(args: argparse.Namespace) -> int:
    action = getattr(args, "vision_action", None)
    if not action:
        parser = getattr(args, "_vision_parser", None)
        if parser is not None:
            parser.print_help()
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
        record = vm.record_gate_result(args.result, ts=getattr(args, "ts", None))
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
