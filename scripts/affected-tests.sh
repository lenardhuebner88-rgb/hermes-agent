#!/usr/bin/env bash
# Print the pytest files affected by the current diff — the *targeted* test
# scope (full suite runs only nightly; see AGENTS.md -> Testing). Pure stdlib,
# so it works even in a worktree without the venv.
#
#   scripts/run_tests.sh $(scripts/affected-tests.sh)        # vs merge-base with main
#   scripts/run_tests.sh $(scripts/affected-tests.sh HEAD~1) # since a ref
set -euo pipefail
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
exec python3 "$SCRIPT_DIR/affected_tests.py" "$@"
