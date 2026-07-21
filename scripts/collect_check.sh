#!/usr/bin/env bash
# Safe, fast test-collection sweep for hermes-agent.
#
# Why this exists (Qwen-R2 finding, 2026-07-21):
#   A bare `pytest --co -q tests/` on a cold bytecode cache is CPU-bound for
#   90s+ (assertion-rewrite + compile of ~47.8k tests across hundreds of
#   modules; the hung run shows user≈real — it is NOT a deadlock). It also
#   reads the LIVE ~/.hermes, so it can abort with INTERNALERROR when
#   ~/.hermes/active_profile points at an unresolvable profile: hermes_cli/
#   main.py runs _apply_profile_override() at import time, which can
#   sys.exit(1), and tests/cli/test_cli_provider_resolution.py imports
#   hermes_cli.main during collection. scripts/run_tests.sh avoids both
#   problems by pre-compiling bytecode and isolating HERMES_HOME per subprocess.
#
# This wrapper brings the SAME two protections to the collection sweep:
#   1. pre-compile the bytecode cache once (kills the cold-start CPU spike on
#      production-module imports);
#   2. run under a throw-away HERMES_HOME + clean env (immune to live
#      active_profile state; deterministic TZ/hashseed).
#
# Usage:
#   scripts/collect_check.sh                 # collect the whole tests/ tree
#   scripts/collect_check.sh tests/agent/    # collect a subtree
#   scripts/collect_check.sh -q tests/cli/   # extra pytest flags pass through
#
# Additive helper — does NOT modify run_tests.sh / run-affected.sh and touches
# no production code.

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "$SCRIPT_DIR/.." && pwd)"

# Locate a venv that can actually run pytest (same probe as run_tests.sh:
# a bare `uv venv` drops an empty .venv that would shadow the real one).
VENV=""
for candidate in "$REPO_ROOT/.venv" "$REPO_ROOT/venv" "$HOME/.hermes/hermes-agent/venv"; do
  if [ -f "$candidate/bin/activate" ] && "$candidate/bin/python" -c "import pytest" >/dev/null 2>&1; then
    VENV="$candidate"
    break
  fi
done
if [ -z "$VENV" ]; then
  echo "error: no virtualenv with pytest found in $REPO_ROOT/.venv, venv, or ~/.hermes/hermes-agent/venv" >&2
  exit 1
fi
PYTHON="$VENV/bin/python"

# Throw-away HERMES_HOME so collection never reads live ~/.hermes state
# (active_profile, config.yaml). conftest.py only isolates HERMES_HOME at
# test-execution time via a fixture; imports happen earlier, during
# collection, so the isolation must be in the process environment.
HERMES_TEST_HOME="$(mktemp -d -t hermes_collect_home.XXXXXX)"
trap 'rm -rf "$HERMES_TEST_HOME"' EXIT

cd "$REPO_ROOT"

# Pre-compile the bytecode cache once so the collection import phase doesn't
# pay cold compilation for every production module. (Do NOT set
# PYTHONDONTWRITEBYTECODE here — we WANT pytest's assertion-rewrite cache to
# persist so subsequent sweeps are warm.)
echo "▶ pre-compiling bytecode cache"
"$PYTHON" -m compileall -q -j 0 -- $(git ls-files '*.py') >/dev/null 2>&1 || true

# Default to the whole tests/ tree when no args are given.
if [ "$#" -eq 0 ]; then
  set -- tests/
fi

echo "▶ collecting (isolated HERMES_HOME=$HERMES_TEST_HOME): $*"
exec env -i \
  PATH="$PATH" \
  HOME="$HOME" \
  TZ=UTC \
  LANG=C.UTF-8 \
  LC_ALL=C.UTF-8 \
  PYTHONHASHSEED=0 \
  HERMES_HOME="$HERMES_TEST_HOME" \
  "$PYTHON" -m pytest --co "$@"
