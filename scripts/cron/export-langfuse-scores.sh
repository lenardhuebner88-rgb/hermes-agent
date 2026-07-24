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

# Resolve the Hermes Agent repo root by CONTENT (hermes_cli/main.py must
# exist), not by .git presence: from the installed location
# (~/.hermes/scripts/) script_dir/../.. is the home directory, which may
# legitimately have its own .git (dotfiles) — a bare .git check would then
# resolve the home dir as repo root (live failure 24.07., cron 23717e2f32ff).
script_dir="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
repo_root=""
for cand in "$(cd "${script_dir}/../.." 2>/dev/null && pwd || true)" \
            "${HERMES_AGENT_REPO:-}" \
            "${HERMES_HOME:-$HOME/.hermes}/hermes-agent"; do
    if [[ -n "${cand}" && -f "${cand}/hermes_cli/main.py" ]]; then
        repo_root="${cand}"
        break
    fi
done
if [[ -z "${repo_root}" ]]; then
    echo "export-langfuse-scores: error: hermes repo root not found (checked script location, HERMES_AGENT_REPO, hermes home)" >&2
    exit 1
fi

cd "${repo_root}"

# Resolve the repo venv python (venv/ preferred, .venv/ fallback).
# Never fall back to ~/.local/bin/hermes — the digest-script lesson (24.07.)
# showed that a PATH-resolved `hermes` can point at a stale install.
if [[ -x "${repo_root}/venv/bin/python" ]]; then
    py="${repo_root}/venv/bin/python"
elif [[ -x "${repo_root}/.venv/bin/python" ]]; then
    py="${repo_root}/.venv/bin/python"
else
    echo "export-langfuse-scores: error: repo venv python not found (${repo_root}/venv or .venv)" >&2
    exit 1
fi

# hermes_cli is a package without __main__.py; the entry guard lives in
# hermes_cli/main.py.  Invoke -m hermes_cli.main (never bare -m hermes_cli,
# which exits 1 with "No module named hermes_cli.__main__").
# Pass stdout/exit-code through; --cron controls the silent contract.
"${py}" -m hermes_cli.main kanban export-langfuse-scores --cron
