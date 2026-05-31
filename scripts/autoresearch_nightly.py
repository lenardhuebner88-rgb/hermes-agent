#!/usr/bin/env python3
"""Nightly autoresearch sweep (systemd-timer entrypoint) — rotating coverage.

Alternates lane by day-of-year so the whole surface gets covered over time,
unattended and always **dry-run** (proposals only — no skill or code file is
mutated; fixes are applied solely through the operator's judge + batch-confirm
step in the dashboard):

  * even day  → SKILL lane: capability sweep over the used-skill set, with
    ``HERMES_AUTORESEARCH_MIN_USE_COUNT=1`` so it reaches the long tail the
    converged ≥5-threshold never touches.
  * odd day   → CODE lane: one incremental code-weakness batch over the
    ``hermes_cli/`` allowlist (only changed/unscanned files, capped).

Iteration count = env ``AR_NIGHTLY_ITERATIONS`` or the backend ``MAX_ITERATIONS``
ceiling. Needs no session token.
"""
from __future__ import annotations

import json
import os
import sys
from datetime import datetime, timezone
from pathlib import Path

_REPO = Path(__file__).resolve().parents[1]
for _p in (str(_REPO), str(_REPO / "scripts")):
    if _p not in sys.path:
        sys.path.insert(0, _p)

import autoresearch_request as arr  # noqa: E402
import run_autoresearch_request as runner  # noqa: E402


def _is_code_night() -> bool:
    """Coverage rotation: odd day-of-year = code scan, even = skill long-tail."""
    return datetime.now(timezone.utc).timetuple().tm_yday % 2 == 1


def _run_skill_night() -> int:
    iterations = int(os.environ.get("AR_NIGHTLY_ITERATIONS", arr.MAX_ITERATIONS))
    iterations = max(1, min(iterations, arr.MAX_ITERATIONS))
    # Long-tail: research skills below the default use-count threshold. The runner
    # reads this via hermes_cli.autoresearch_proposals._usage_min_use_count().
    os.environ.setdefault("HERMES_AUTORESEARCH_MIN_USE_COUNT", "1")
    request_path = arr.create_request(
        mode="skills", area="all", focus="capability",
        max_iterations=iterations, mutation_policy="requires_operator_go",
    )
    # Dry-run (no --apply): proposals queued, no skill file mutated.
    return runner.main([str(request_path), "--max-iterations", str(iterations)])


def _run_code_night() -> int:
    from hermes_cli.autoresearch_proposals import generate_code_weakness_proposals
    # Deep caps: the unattended nightly walks far more of the allowlist and keeps
    # more proposals than the snappy interactive default.
    res = generate_code_weakness_proposals(scope="incremental", max_files=40, limit=8)
    print(json.dumps({
        "lane": "code", "created_count": res.get("created_count"),
        "files_seen": res.get("files_seen"), "skipped_unchanged": res.get("skipped_unchanged"),
        "vetoed": res.get("vetoed"),
        "tokens": res.get("tokens"), "scope": res.get("scope"),
    }, indent=2))
    return 0


def main() -> int:
    """Rotating, unattended, dry-run coverage: alternate skill long-tail / code scan."""
    return _run_code_night() if _is_code_night() else _run_skill_night()


if __name__ == "__main__":
    raise SystemExit(main())
