"""CLI subcommand parser builder for ``hermes lessons``.

Provides ``hermes lessons harvest`` — a deterministic, no-LLM harvester that
clusters disposition_items + blocked task_events + loop-pack LEDGERs into a
JSON candidate artefact.

Provides ``hermes lessons promote`` — promote candidates with >= 2 evidence
points to held docs-edit Kanban tasks (AGENTS.md pitfall or SKILL.md).

Part of the LESSONS-TO-DOCS-LOOP PlanSpec (L2/L3).
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

    # -- promote ----------------------------------------------------------
    promote = lessons_sub.add_parser(
        "promote",
        help="Promote harvested candidates to held docs-edit Kanban tasks.",
        description=(
            "Read harvest_candidates.json, filter clusters with >= 2 evidence "
            "points, deduplicate against existing pitfalls in AGENTS.md and "
            "docs/agent-dev-guide.md, and create held (blocked) Kanban tasks "
            "for the top --cap candidates. Tasks target AGENTS.md Important "
            "Pitfalls or the affected SKILL.md. Idempotent via "
            "idempotency_key='lessons:<slug>'. No direct commits."
        ),
    )
    promote.add_argument(
        "--input",
        type=str,
        default=None,
        help="Override the harvest artefact path (default: <state>/lessons/harvest_candidates.json).",
    )
    promote.add_argument(
        "--repo-dir",
        type=str,
        default=None,
        help="Repo root for dedup against AGENTS.md / docs/agent-dev-guide.md (default: auto-detect).",
    )
    promote.add_argument(
        "--cap",
        type=int,
        default=5,
        help="Maximum number of docs-edit tasks to create per run (default: 5).",
    )
    promote.add_argument(
        "--dry-run",
        action="store_true",
        default=False,
        help="Print what would be created without writing to kanban.db.",
    )
    promote.add_argument(
        "--board",
        type=str,
        default=None,
        help="Override the target Kanban board slug.",
    )
    promote.set_defaults(func=_cmd_promote)

    # -- cycle --------------------------------------------------------------
    cycle = lessons_sub.add_parser(
        "cycle",
        help="One-shot: harvest, then promote if candidates were produced.",
        description=(
            "Run harvest, then (only if candidates meeting the threshold "
            "were produced) run promote against the fresh harvest artefact. "
            "Promoted tasks stay HELD (blocked) — no auto-unblock. Prints a "
            "compact summary of harvested clusters and promoted/held task ids."
        ),
    )
    cycle.add_argument(
        "--window-days",
        type=int,
        default=30,
        help="Lookback window in days for the harvest step (default: 30).",
    )
    cycle.add_argument(
        "--output",
        type=str,
        default=None,
        help="Override the harvest output path (default: <state>/lessons/harvest_candidates.json).",
    )
    cycle.add_argument(
        "--loops-root",
        type=str,
        default=None,
        help="Override the loops root directory (default: <hermes_home>/loops).",
    )
    cycle.add_argument(
        "--repo-dir",
        type=str,
        default=None,
        help="Repo root for dedup against AGENTS.md / docs/agent-dev-guide.md (default: auto-detect).",
    )
    cycle.add_argument(
        "--cap",
        type=int,
        default=5,
        help="Maximum number of docs-edit tasks to create in the promote step (default: 5).",
    )
    cycle.add_argument(
        "--dry-run",
        action="store_true",
        default=False,
        help="Print what promote would create without writing to kanban.db.",
    )
    cycle.add_argument(
        "--board",
        type=str,
        default=None,
        help="Override the target Kanban board slug.",
    )
    cycle.set_defaults(func=_cmd_cycle)

    # If no subcommand given, print help
    parser.set_defaults(func=_cmd_lessons_help)
    return parser


def _cmd_lessons_help(args: argparse.Namespace) -> int:
    print("Usage: hermes lessons {harvest,promote} [options]")
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


def _cmd_promote(args: argparse.Namespace) -> int:
    from pathlib import Path

    from hermes_cli.lessons import run_promote

    input_path = Path(args.input) if getattr(args, "input", None) else None
    repo_dir = Path(args.repo_dir) if getattr(args, "repo_dir", None) else None

    result = run_promote(
        harvest_path=input_path,
        repo_dir=repo_dir,
        cap=getattr(args, "cap", 5),
        dry_run=getattr(args, "dry_run", False),
        board=getattr(args, "board", None),
    )
    print(json.dumps(result, indent=2, ensure_ascii=False, default=str))
    return 0


def _cmd_cycle(args: argparse.Namespace) -> int:
    from pathlib import Path

    from hermes_cli.lessons import run_lessons_cycle

    output_path = Path(args.output) if getattr(args, "output", None) else None
    loops_root = Path(args.loops_root) if getattr(args, "loops_root", None) else None
    repo_dir = Path(args.repo_dir) if getattr(args, "repo_dir", None) else None

    result = run_lessons_cycle(
        loops_root=loops_root,
        output_path=output_path,
        window_days=getattr(args, "window_days", 30),
        repo_dir=repo_dir,
        cap=getattr(args, "cap", 5),
        dry_run=getattr(args, "dry_run", False),
        board=getattr(args, "board", None),
    )

    harvest = result["harvest"]
    promote = result["promote"]
    clusters = harvest.get("total_clusters", 0)
    candidates = harvest.get("candidate_count", 0)
    print(
        f"harvest: {clusters} clusters, {candidates} candidates >= threshold "
        f"({harvest.get('output_path')})"
    )
    if promote is None:
        print("promote: skipped (no candidates)")
    else:
        promoted_ids = [
            str(c.get("task_id", c.get("title"))) for c in promote.get("created", [])
        ]
        print(
            f"promote: {promote.get('promoted', 0)} promoted (held), "
            f"{promote.get('skipped_documented', 0)} already documented, "
            f"{promote.get('capped', 0)} capped -> {promoted_ids}"
        )
    print(json.dumps(result, indent=2, ensure_ascii=False, default=str))
    return 0
