#!/usr/bin/env python3
"""On-demand Claude Code → usage_facts ETL entrypoint.

Operator-invoked only.  Does not install or propose a cron.

Examples::

    # Probe DB (never the live DB without review):
    scripts/harvest_claude_code_usage.py \\
        --db /tmp/s3-harvest/probe.db \\
        --projects-root /home/piet/.claude/projects

    # Dry parse of a fixture tree:
    scripts/harvest_claude_code_usage.py \\
        --projects-root tests/fixtures/claude_code_harvest/projects \\
        --db /tmp/s3-harvest/fixture.db \\
        --dry-run
"""

from __future__ import annotations

import sys
from pathlib import Path

_REPO_ROOT = Path(__file__).resolve().parents[1]
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

from hermes_cli.claude_code_harvester import main  # noqa: E402


if __name__ == "__main__":
    raise SystemExit(main())
