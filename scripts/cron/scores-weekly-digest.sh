#!/usr/bin/env bash
# scores-weekly-digest.sh — Weekly Kanban score digest for Discord delivery.
#
# Usage (cron):  scripts/cron/scores-weekly-digest.sh [--weeks N]
#
# Always prints the Markdown digest to stdout (no silent contract).
# Exits non-zero on any failure so the cron wrapper can alert.
set -euo pipefail

# Resolve the hermes CLI.
# 1. Prefer the venv next to this script (in-repo layout: <repo>/scripts/cron/).
# 2. Fall back to the canonical Hermes install under HERMES_HOME (copied-script
#    layout: ~/.hermes/scripts/ — the cron deployment path).
# 3. Last resort: PATH (may be stale; the exec will fail non-zero if the CLI
#    does not support `kanban scores --digest`).
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "${SCRIPT_DIR}/../.." && pwd)"

HERMES_HOME="${HERMES_HOME:-${HOME}/.hermes}"

if [[ -x "${REPO_ROOT}/.venv/bin/hermes" ]]; then
    HERMES="${REPO_ROOT}/.venv/bin/hermes"
elif [[ -x "${REPO_ROOT}/venv/bin/hermes" ]]; then
    HERMES="${REPO_ROOT}/venv/bin/hermes"
elif [[ -x "${HERMES_HOME}/hermes-agent/.venv/bin/hermes" ]]; then
    HERMES="${HERMES_HOME}/hermes-agent/.venv/bin/hermes"
elif [[ -x "${HERMES_HOME}/hermes-agent/venv/bin/hermes" ]]; then
    HERMES="${HERMES_HOME}/hermes-agent/venv/bin/hermes"
else
    HERMES="hermes"
fi

exec "$HERMES" kanban scores --digest "$@"
