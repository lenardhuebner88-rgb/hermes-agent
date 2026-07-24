#!/usr/bin/env bash
# scores-weekly-digest.sh — Weekly Kanban score digest for Discord delivery.
#
# Usage (cron):  scripts/cron/scores-weekly-digest.sh [--weeks N]
#
# Always prints the Markdown digest to stdout (no silent contract).
# Exits non-zero on any failure so the cron wrapper can alert.
set -euo pipefail

# Resolve the hermes CLI.
# In the repo layout (<repo>/scripts/cron/), prefer the venv next to the repo.
# In the copied layout (${HERMES_HOME}/scripts/), REPO_ROOT resolves to $HOME;
# a stale $HOME/.venv must NOT win over the canonical HERMES_HOME venv.
# Require BOTH the pyproject.toml marker AND the actual path layout
# <REPO_ROOT>/scripts/cron/<script> to accept repo-relative venvs.
# Precedence: `venv` is the canonical editable install; `.venv` is a stale
# legacy env (no current hermes_cli) and must only ever be a last resort.
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "${SCRIPT_DIR}/../.." && pwd)"

HERMES_HOME="${HERMES_HOME:-${HOME}/.hermes}"

HERMES=""
if [[ "${SCRIPT_DIR}" == "${REPO_ROOT}/scripts/cron" && -f "${REPO_ROOT}/pyproject.toml" ]]; then
    if [[ -x "${REPO_ROOT}/venv/bin/hermes" ]]; then
        HERMES="${REPO_ROOT}/venv/bin/hermes"
    elif [[ -x "${REPO_ROOT}/.venv/bin/hermes" ]]; then
        HERMES="${REPO_ROOT}/.venv/bin/hermes"
    fi
fi
if [[ -z "${HERMES}" ]]; then
    if [[ -x "${HERMES_HOME}/hermes-agent/venv/bin/hermes" ]]; then
        HERMES="${HERMES_HOME}/hermes-agent/venv/bin/hermes"
    elif [[ -x "${HERMES_HOME}/hermes-agent/.venv/bin/hermes" ]]; then
        HERMES="${HERMES_HOME}/hermes-agent/.venv/bin/hermes"
    else
        HERMES="hermes"
    fi
fi

exec "$HERMES" kanban scores --digest "$@"
