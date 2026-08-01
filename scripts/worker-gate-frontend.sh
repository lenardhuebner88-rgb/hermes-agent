#!/usr/bin/env bash
# worker-gate-frontend.sh — frontend gate wrapper for the Kanban worker gate
#
# Runs the Control frontend gate when a worker diff touches web/. Designed to
# run with cwd = worker worktree and to leave generated web_dist assets alone.
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
ROOT="$(cd "$SCRIPT_DIR/.." && pwd)"
BASE="$(python3 "$SCRIPT_DIR/gate_diff_base.py" --repo-root "$ROOT" || true)"
if [[ -z "$BASE" ]]; then
    BASE="$(git -C "$ROOT" merge-base HEAD main 2>/dev/null || echo HEAD)"
fi
export HERMES_GATE_DIFF_BASE="$BASE"

mapfile -t _raw_changed < <(
    git -C "$ROOT" diff --name-only "$BASE" 2>/dev/null
    git -C "$ROOT" ls-files --others --exclude-standard 2>/dev/null
)

for _file in "${_raw_changed[@]}"; do
    if [[ "$_file" == web/* ]]; then
        exec "$SCRIPT_DIR/gate-frontend.sh" --skip-build
    fi
done

echo "worker-gate-frontend: no changed web/ files — skip"
exit 0
