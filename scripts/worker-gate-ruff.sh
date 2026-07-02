#!/usr/bin/env bash
# worker-gate-ruff.sh — ruff-Gate-Wrapper for the kanban worker_gate
#
# Runs ruff on Python files changed relative to main, affected-only.
# Designed to run with cwd = Worker-Worktree (subprocess.run without shell).
# Does NOT rely on cwd; resolves everything relative to its own checkout root.
#
# Exit codes:
#   0  — ruff clean (or no changed .py files)
#   1  — ruff found violations, or ruff binary not found (when files exist)
set -euo pipefail

# ---------------------------------------------------------------------------
# 1. Resolve checkout root (script lives in <root>/scripts/)
# ---------------------------------------------------------------------------
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
ROOT="$(cd "$SCRIPT_DIR/.." && pwd)"

# ---------------------------------------------------------------------------
# 2. Collect affected .py files (diff relative to merge-base with main)
#    File selection happens BEFORE the ruff probe so we can exit 0 early
#    when there are no Python files to lint (avoids a spurious "ruff not found"
#    error on worktrees that only touched non-.py files).
# ---------------------------------------------------------------------------
BASE="$(git -C "$ROOT" merge-base HEAD main 2>/dev/null || echo HEAD)"

# Committed + uncommitted changes vs merge-base, plus untracked files
mapfile -t _raw_changed < <(
    git -C "$ROOT" diff --name-only "$BASE" 2>/dev/null
    git -C "$ROOT" ls-files --others --exclude-standard 2>/dev/null
)

# Filter: only *.py, exclude per-file-ignore dirs, skip deleted files
PYFILES=()
EXCLUDED_PREFIXES=("tests/" "skills/" "optional-skills/" "plugins/")
for _f in "${_raw_changed[@]}"; do
    # Only Python files
    [[ "$_f" == *.py ]] || continue
    # Skip excluded path prefixes (mirrors pyproject per-file-ignores)
    _skip=0
    for _pfx in "${EXCLUDED_PREFIXES[@]}"; do
        if [[ "$_f" == "$_pfx"* ]]; then
            _skip=1
            break
        fi
    done
    [[ "$_skip" -eq 1 ]] && continue
    # Skip deleted files — ruff crashes on missing paths
    [[ -f "$ROOT/$_f" ]] || continue
    PYFILES+=("$ROOT/$_f")
done

# Early exit: nothing to lint
if [[ "${#PYFILES[@]}" -eq 0 ]]; then
    echo "worker-gate-ruff: no changed .py files — skip"
    exit 0
fi

# ---------------------------------------------------------------------------
# 3. Find ruff — worktree-aware probe order
#    a. common-dir → main-repo venv (worktrees have no own venv)
#    b. PATH (systemd venv/bin first)
#    c. python3 -m ruff fallback
#    d. Nothing found → fail-closed (loud error, exit 1)
# ---------------------------------------------------------------------------
RUFF=""
USE_MODULE=0

# 3a. Resolve main-repo via git common-dir
COMMON_DIR="$(git -C "$ROOT" rev-parse --path-format=absolute --git-common-dir 2>/dev/null || true)"
if [[ "$COMMON_DIR" == */.git ]]; then
    MAIN_REPO="${COMMON_DIR%/.git}"
    for _candidate in "$MAIN_REPO/venv/bin/ruff" "$MAIN_REPO/.venv/bin/ruff"; do
        if [[ -x "$_candidate" ]]; then
            RUFF="$_candidate"
            break
        fi
    done
fi

# 3b. PATH
if [[ -z "$RUFF" ]] && command -v ruff >/dev/null 2>&1; then
    RUFF="$(command -v ruff)"
fi

# 3c. python3 -m ruff
if [[ -z "$RUFF" ]]; then
    if python3 -m ruff --version >/dev/null 2>&1; then
        USE_MODULE=1
    else
        echo "worker-gate-ruff: ruff not found (tried main-repo venv, PATH, python3 -m ruff)" >&2
        exit 1
    fi
fi

# ---------------------------------------------------------------------------
# 4. Run ruff — Exit-Code is forwarded unchanged via exec
# ---------------------------------------------------------------------------
if [[ "$USE_MODULE" -eq 1 ]]; then
    exec python3 -m ruff check "${PYFILES[@]}"
else
    exec "$RUFF" check "${PYFILES[@]}"
fi
