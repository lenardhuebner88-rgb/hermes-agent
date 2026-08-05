# shellcheck shell=bash
#
# Fork-owned helper: pick the interpreter that is allowed to run this repo's
# Python test suite. Sourced, not executed.
#
# Why this file exists
# --------------------
# The probe used to live twice: inline in `scripts/run_tests.sh` — which is an
# UPSTREAM-owned file — and again in `scripts/collect_check.sh`. On 2026-07-25
# the two drifted while a comment still claimed they matched, so
# `collect_check.sh` could pick an interpreter that `run_tests.sh` deliberately
# rejects. One copy, in a fork-owned file, removes that failure mode.
#
# It also keeps the next upstream sync cheap. AGENTS.md, standing rule: new fork
# code goes into a fork-owned module and is called from one line — never grown
# inside an upstream-owned file. `hermes_cli/kanban_db.py` is the cautionary
# tale this rule was written for.
#
# Usage:
#     . "$SCRIPT_DIR/lib/select_test_python.sh"
#     PYTHON="$(select_test_python "$REPO_ROOT")" || exit 1
#
# Writes the interpreter path to stdout; all diagnostics go to stderr, so the
# caller can capture stdout safely. Returns 1 when nothing usable was found,
# after printing the exact repair command.

select_test_python() {
  local repo_root="$1"
  # Upstream's three candidates, in UPSTREAM'S ORDER. scripts/run_tests.sh
  # probed them inline before the fork moved interpreter selection in here;
  # reordering them would pick a different interpreter whenever two are usable,
  # which is exactly the kind of silent divergence this helper exists to avoid.
  local candidates=(
    "$repo_root/.venv"
    "$repo_root/venv"
    "$HOME/.hermes/hermes-agent/venv"
  )

  # Fork addition, appended AFTER upstream's list so it can only ever be reached
  # when none of upstream's candidates is usable: a worktree has no venv of its
  # own and may reuse the live checkout's.
  if [ "$repo_root" != "$HOME/.hermes/hermes-agent" ]; then
    candidates+=("$HOME/.hermes/hermes-agent/.venv")
  fi

  local candidate
  for candidate in "${candidates[@]}"; do
    [ -f "$candidate/bin/activate" ] || continue

    # Guard 1 — the Hermes-managed release venv. It carries [all] but not [dev]
    # by policy, so it has no pytest and must never be selected for tests. It is
    # identified by the generation path its pyvenv.cfg points at, not by name.
    if [ -f "$candidate/pyvenv.cfg" ] \
        && grep -Eq '^home = .*/\.hermes-runtime/python/generation-' "$candidate/pyvenv.cfg"; then
      echo "note: skipping $candidate (Hermes-managed release venv; tests require [all,dev,messaging])" >&2
      continue
    fi

    # Guard 2 — an empty or partial venv. A bare `uv venv` drops a .venv that
    # would otherwise shadow the real one and break every run in the checkout.
    if "$candidate/bin/python" -c "import pytest" >/dev/null 2>&1; then
      printf '%s\n' "$candidate/bin/python"
      return 0
    fi
    echo "note: skipping $candidate (no pytest importable — empty/partial venv)" >&2
  done

  # Nix devShell fallback. The import check is load-bearing: HERMES_PYTHON can
  # point at the RELEASE venv (no pytest) when it was inherited from a wrapped
  # `hermes` binary rather than exported by the devShell hook.
  if [ -n "${HERMES_PYTHON:-}" ] && [ -x "$HERMES_PYTHON" ] \
      && "$HERMES_PYTHON" -c 'import pytest' >/dev/null 2>&1; then
    printf '%s\n' "$HERMES_PYTHON"
    return 0
  fi

  local uv_bin
  uv_bin="$(command -v uv 2>/dev/null || printf 'uv')"
  echo "error: no test Python; run: cd $repo_root && $uv_bin sync --locked --extra all --extra dev --extra messaging" >&2
  return 1
}
