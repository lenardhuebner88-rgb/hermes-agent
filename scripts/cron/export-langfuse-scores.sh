#!/usr/bin/env bash
# Cron wrapper for `hermes kanban export-langfuse-scores --cron`.
#
# Sources ~/.hermes/.env for the Langfuse credentials
# (HERMES_LANGFUSE_BASE_URL/PUBLIC_KEY/SECRET_KEY) without echoing any
# values, then invokes the exporter in --cron mode from the repo root.
# Exit code and stdout are passed through unchanged so a no-op export
# (0 newly posted scores) produces empty stdout = no Discord delivery,
# while N>0 posted scores produces exactly one summary line.
#
# Install: copy this script to ~/.hermes/scripts/export-langfuse-scores.sh
# then `hermes cron create --script ~/.hermes/scripts/export-langfuse-scores.sh
# --no-agent --deliver discord --name "Langfuse Scores Export"`.

set -euo pipefail

# Source credentials without printing any values.
if [[ -f "${HERMES_HOME:-$HOME/.hermes}/.env" ]]; then
    set -a
    # shellcheck disable=SC1091
    . "${HERMES_HOME:-$HOME/.hermes}/.env"
    set +a
fi

# Resolve the Hermes Agent repo root from the script location, falling
# back to the canonical checkout path.
script_dir="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
repo_root="$(cd "${script_dir}/../.." 2>/dev/null && pwd)"
if [[ ! -d "${repo_root}/.git" ]]; then
    repo_root="${HERMES_AGENT_REPO:-$HOME/.hermes/hermes-agent}"
fi

cd "${repo_root}"

# Pass stdout/exit-code through; --cron controls the silent contract.
python -m hermes_cli kanban export-langfuse-scores --cron
