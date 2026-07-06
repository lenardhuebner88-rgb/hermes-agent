"""CLI subcommand parser builder for ``hermes lessons``.

Provides ``hermes lessons harvest`` — a deterministic, no-LLM harvester that
clusters disposition_items + blocked task_events + loop-pack LEDGERs into a
JSON candidate artefact.

Part of the LESSONS-TO-DOCS-LOOP PlanSpec (L2).
"""
from __future__ import annotations

import argparse
import json
import sys


def build_lessons_parser(subparsers: argparse._SubParsersAction) -> argparse.ArgumentParser:
    """Build the ``hermes lessons`` subcommand group."""
    parser = subparsers.add_parser(
        "lessons",
        help="Harvest recurring trap classes from kanban + loop-ledger sources.",
        description=(
            "Deterministic harvester: clusters disposition_items, blocked "
            "task_events, and loop-pack LEDGER entries into a candidate "
            "JSON artefact for downstream docs/skill-edit promotion. "
            "No LLM calls; reads kanban.db read-only."
        ),
    )
    lessons_sub = parser.add_subparsers(dest="lessons_command")

    # -- harvest ----------------------------------------------------------
    harvest = lessons_sub.add_parser(
        "harvest",
        help="Cluster trap classes and emit harvest_candidates.json.",
        description=(
            "Read disposition_items (open/accepted), blocked task_events, "
            "and loop-pack LEDGER.md files from the last --window-days, "
            "cluster by keyword signatures, and write "
            "<state_dir>/lessons/harvest_candidates.json. Idempotent; "
            "read-only against kanban.db."
        ),
    )
    harvest.add_argument(
        "--window-days",
        type=int,
        default=30,
        help="Lookback window in days for disposition_items and blocked events (default: 30).",
    )
    harvest.add_argument(
        "--output",
        type=str,
        default=None,
        help="Override the output path (default: <state>/lessons/harvest_candidates.json).",
    )
    harvest.add_argument(
        "--loops-root",
        type=str,
        default=None,
        help="Override the loops root directory (default: <hermes_home>/loops).",
    )
    harvest.set_defaults(func=_cmd_harvest)

    # If no subcommand given, print help
    parser.set_defaults(func=_cmd_lessons_help)
    return parser


def _cmd_lessons_help(args: argparse.Namespace) -> int:
    print("Usage: hermes lessons harvest [--window-days N] [--output PATH]")
    return 0


def _cmd_harvest(args: argparse.Namespace) -> int:
    from pathlib import Path

    from hermes_cli.lessons import run_harvest

    output_path = Path(args.output) if getattr(args, "output", None) else None
    loops_root = Path(args.loops_root) if getattr(args, "loops_root", None) else None

    result = run_harvest(
        output_path=output_path,
        loops_root=loops_root,
        window_days=getattr(args, "window_days", 30),
    )
    print(json.dumps(result, indent=2, ensure_ascii=False))
    return 0
