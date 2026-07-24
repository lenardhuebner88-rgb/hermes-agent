#!/usr/bin/env bash
# scores-weekly-digest.sh — Weekly Kanban score digest for Discord delivery.
#
# Usage (cron):  scripts/cron/scores-weekly-digest.sh [--weeks N]
#
# Always prints the Markdown digest to stdout (no silent contract).
# Exits non-zero on any failure so the cron wrapper can alert.
set -euo pipefail

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"

# Resolve the hermes CLI — prefer the venv in the repo, fall back to PATH.
if [[ -x "${REPO_ROOT}/.venv/bin/hermes" ]]; then
    HERMES="${REPO_ROOT}/.venv/bin/hermes"
elif [[ -x "${REPO_ROOT}/venv/bin/hermes" ]]; then
    HERMES="${REPO_ROOT}/venv/bin/hermes"
else
    HERMES="hermes"
fi

exec "$HERMES" kanban scores --digest "$@"
