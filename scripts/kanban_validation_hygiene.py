#!/usr/bin/env python3
"""S6: Hygiene-Sweep für verwaiste kanban-validation-Worktrees.

Entfernt nur vom Integrator angelegte Pfade unter
``<repo>/.worktrees/kanban-validation/<token>`` (Crash/ENOSPC-Reste).
Niemals Chain-Worktrees (``kanban/t_*``), Bridges oder fremde Worktrees.

Usage:
  scripts/kanban_validation_hygiene.py [--apply] [--max-age-seconds N] [REPO…]

Default ist Dry-Run. ``--apply`` führt den Sweep aus.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

# Repo-Root auf sys.path, damit der Import im Worktree ohne Install funktioniert.
_ROOT = Path(__file__).resolve().parents[1]
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

from hermes_cli.kanban_worktrees import (  # noqa: E402
    VALIDATION_HYGIENE_MAX_AGE_SECONDS,
    hygiene_sweep_validation_worktrees,
)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description=(
            "S6: Verwaiste Validation-Worktrees bereinigen "
            "(nur .worktrees/kanban-validation/*)."
        )
    )
    parser.add_argument(
        "repos",
        nargs="*",
        type=Path,
        default=[Path.home() / ".hermes" / "hermes-agent"],
        help="Repo-Roots (Default: ~/.hermes/hermes-agent)",
    )
    parser.add_argument(
        "--apply",
        action="store_true",
        help="Tatsächlich entfernen (Default: Dry-Run)",
    )
    parser.add_argument(
        "--max-age-seconds",
        type=int,
        default=VALIDATION_HYGIENE_MAX_AGE_SECONDS,
        help=f"Mindestalter in Sekunden (Default {VALIDATION_HYGIENE_MAX_AGE_SECONDS})",
    )
    parser.add_argument(
        "--json",
        action="store_true",
        help="Maschinenlesbares JSON auf stdout",
    )
    args = parser.parse_args(argv)

    reports = []
    for repo in args.repos:
        report = hygiene_sweep_validation_worktrees(
            repo,
            max_age_seconds=args.max_age_seconds,
            dry_run=not args.apply,
        )
        reports.append({"repo": str(repo), **report})
        if not args.json:
            mode = "dry-run" if not args.apply else "apply"
            print(f"[{mode}] {repo}: scanned={report['scanned']} "
                  f"removed={len(report['removed'])} "
                  f"skipped={len(report['skipped'])} "
                  f"errors={len(report['errors'])}")
            for item in report["removed"]:
                print(f"  remove: {item['path']} (age {item.get('age_seconds', '?')}s)")
            for item in report["skipped"]:
                print(f"  skip: {item['path']} ({item.get('reason')})")
            for err in report["errors"]:
                print(f"  error: {err}", file=sys.stderr)

    if args.json:
        json.dump(reports, sys.stdout, ensure_ascii=False, indent=2)
        sys.stdout.write("\n")
    return 1 if any(r["errors"] for r in reports) else 0


if __name__ == "__main__":
    raise SystemExit(main())
