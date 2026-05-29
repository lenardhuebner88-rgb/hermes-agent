#!/usr/bin/env python3
"""A3 code-gate worker.

Spawned detached by ``autoresearch_proposals._apply_code_proposal`` for one
``mode='code'`` proposal that is already live-written and marked "testing".
Runs the full canonical test suite and resolves the proposal: keep on green
(status=applied), auto-revert on red (status back to proposed). All the
keep/revert logic lives in ``finalize_code_gate`` so it is unit-testable; this
script is only the thin entry point that the detached process runs.

Usage:
    run_proposal_code_gate.py <proposal-id>
"""
from __future__ import annotations

import sys
from pathlib import Path

_REPO = Path(__file__).resolve().parents[1]
if str(_REPO) not in sys.path:
    sys.path.insert(0, str(_REPO))

from hermes_cli import autoresearch_proposals as proposals  # noqa: E402


def main(argv: list[str]) -> int:
    if len(argv) < 2 or not argv[1].strip():
        print("usage: run_proposal_code_gate.py <proposal-id>", file=sys.stderr)
        return 2
    result = proposals.finalize_code_gate(argv[1].strip())
    return 0 if result.get("ok") else 1


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
