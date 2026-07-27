#!/usr/bin/env python3
"""CLI: harvest codex/kimi/grok/qwen CLI usage into the usage-facts DB.

Example (probe copy of the live DB — never required, but recommended):

  mkdir -p /tmp/s4-harvest
  cp /mnt/data/hermes-observability/usage_facts.db /tmp/s4-harvest/probe.db
  python scripts/harvest_foreign_lanes.py --db /tmp/s4-harvest/probe.db

Idempotent and incremental via a state file next to the DB. A second run
without new source files writes 0 run rows.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

# Allow running from a worktree without install.
_REPO_ROOT = Path(__file__).resolve().parents[1]
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

from hermes_cli.foreign_lane_harvest import (  # noqa: E402
    ORIGIN_CODEX,
    ORIGIN_GROK,
    ORIGIN_KIMI,
    ORIGIN_QWEN,
    harvest_all,
)
from hermes_cli.usage_facts_db import initialize_usage_facts_db  # noqa: E402


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--db",
        default=None,
        help="usage_facts.db path (default: HERMES_USAGE_FACTS_DB or /mnt/data/...)",
    )
    parser.add_argument(
        "--state",
        default=None,
        help="incremental state JSON (default: <db-dir>/foreign_lane_harvest_state.json)",
    )
    parser.add_argument(
        "--rate-limit-out",
        default=None,
        help="JSONL sidecar for Codex rate-limit snapshots",
    )
    parser.add_argument(
        "--origin",
        action="append",
        choices=[ORIGIN_CODEX, ORIGIN_KIMI, ORIGIN_GROK, ORIGIN_QWEN],
        help="Limit to one or more origins (repeatable). Default: all four.",
    )
    parser.add_argument("--codex-sessions", default=None, help="Codex sessions root")
    parser.add_argument("--kimi-index", default=None, help="Kimi session_index.jsonl")
    parser.add_argument("--qwen-usage-dir", default=None, help="Qwen usage directory")
    parser.add_argument("--grok-unified", default=None, help="Grok unified.jsonl path")
    parser.add_argument(
        "--force",
        action="store_true",
        help="Ignore source-file fingerprints (still skips existing run_ids unless re-upsert)",
    )
    parser.add_argument(
        "--include-calls",
        action="store_true",
        help="Also write per-turn run_llm_calls (slow on large Codex rollouts; default off)",
    )
    parser.add_argument(
        "--json",
        action="store_true",
        help="Print machine-readable stats JSON on stdout",
    )
    args = parser.parse_args(argv)

    db_path = initialize_usage_facts_db(args.db)
    kwargs: dict = {
        "db_path": db_path,
        "state_path": args.state,
        "rate_limit_path": args.rate_limit_out,
        "origins": args.origin,
        "force": args.force,
        "include_calls": args.include_calls,
    }
    if args.codex_sessions:
        kwargs["codex_sessions"] = args.codex_sessions
    if args.kimi_index:
        kwargs["kimi_index"] = args.kimi_index
    if args.qwen_usage_dir:
        kwargs["qwen_usage_dir"] = args.qwen_usage_dir
    if args.grok_unified:
        kwargs["grok_unified"] = args.grok_unified

    result = harvest_all(**kwargs)
    if args.json:
        print(json.dumps(result, indent=2, sort_keys=True))
    else:
        print(f"db={result['db_path']}")
        print(f"state={result['state_path']}")
        print(f"rate_limits={result['rate_limit_path']}")
        print(f"written_runs_total={result['written_runs_total']}")
        for name, stats in result["origins"].items():
            print(
                f"{name}: scanned={stats['scanned']} extracted={stats['extracted']} "
                f"written_runs={stats['written_runs']} written_calls={stats['written_calls']} "
                f"skipped_unchanged={stats['skipped_unchanged']} "
                f"skipped_existing={stats['skipped_existing']} "
                f"errors={stats['errors']} duration_s={stats['duration_s']:.3f}"
            )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
