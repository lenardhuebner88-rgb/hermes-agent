#!/usr/bin/env python3
"""Nightly AR3 capability sweep (systemd-timer entrypoint).

Builds a dry-run capability run-request and runs the autoresearch loop over the
full used-skill set, filling the review queue with grounded-fix proposals. It
writes NO skill: dry-run means proposals only, and AR3 fixes are applied solely
through the operator's judge + batch-confirm step in the dashboard.

Mirrors the dashboard ``/autoresearch/trigger`` path (mode=skills, focus=capability)
but needs no session token. Iteration count = env ``AR_NIGHTLY_ITERATIONS`` or the
backend ``MAX_ITERATIONS`` ceiling, so one run sweeps as many skills as allowed.
"""
from __future__ import annotations

import os
import sys
from pathlib import Path

_REPO = Path(__file__).resolve().parents[1]
for _p in (str(_REPO), str(_REPO / "scripts")):
    if _p not in sys.path:
        sys.path.insert(0, _p)

import autoresearch_request as arr  # noqa: E402
import run_autoresearch_request as runner  # noqa: E402


def main() -> int:
    iterations = int(os.environ.get("AR_NIGHTLY_ITERATIONS", arr.MAX_ITERATIONS))
    iterations = max(1, min(iterations, arr.MAX_ITERATIONS))
    request_path = arr.create_request(
        mode="skills",
        area="all",
        focus="capability",
        max_iterations=iterations,
        mutation_policy="requires_operator_go",
    )
    # Dry-run (no --apply): proposals are queued, no skill file is mutated.
    return runner.main([str(request_path), "--max-iterations", str(iterations)])


if __name__ == "__main__":
    raise SystemExit(main())
