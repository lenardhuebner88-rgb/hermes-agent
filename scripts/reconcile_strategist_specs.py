#!/usr/bin/env python3
"""Reconcile proposed Strategist PlanSpecs with their Board disposition.

The default is a dry run.  Pass ``--apply`` only after reviewing the printed
per-file mapping; unplayed sources become ``nie-eingespielt`` rather than being
silently indistinguishable from pending proposals.
"""

from __future__ import annotations

import argparse
from contextlib import closing
from pathlib import Path
import sys

REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from hermes_constants import get_hermes_home
from hermes_cli import kanban_db as kb
from hermes_cli.strategist_specs import reconcile_proposed_specs


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--plans-root",
        type=Path,
        default=get_hermes_home() / "state" / "strategist" / "specs",
        help="PlanSpec directory (default: %(default)s)",
    )
    parser.add_argument("--board", help="Kanban board to inspect (default: active board)")
    parser.add_argument("--author", default="strategist-reconciler")
    parser.add_argument(
        "--apply", action="store_true", help="write reconciled statuses (default: dry run)"
    )
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    mode = "APPLY" if args.apply else "DRY-RUN"
    with closing(kb.connect(board=args.board)) as conn:
        results = reconcile_proposed_specs(
            conn,
            plans_root=args.plans_root,
            apply=args.apply,
            author=args.author,
        )
    for result in results:
        print(f"{mode} {result['path']} -> {result['status']} ({result['action']})")
    print(f"{mode} summary: {len(results)} vorgeschlagen PlanSpecs classified")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
