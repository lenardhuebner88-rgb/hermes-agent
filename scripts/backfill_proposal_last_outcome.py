#!/usr/bin/env python3
"""Backfill Autoresearch proposal last_outcome with optional dry-run."""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

_REPO = Path(__file__).resolve().parents[1]
if str(_REPO) not in sys.path:
    sys.path.insert(0, str(_REPO))

from hermes_cli.autoresearch_proposals import backfill_last_outcome


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--apply", action="store_true", help="write changes after creating a proposals backup")
    args = parser.parse_args()
    result = backfill_last_outcome(dry_run=not args.apply)
    print(json.dumps(result, indent=2, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
