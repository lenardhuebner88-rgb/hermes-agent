#!/usr/bin/env bash
# Remove stale disposable worktrees after checking their Kanban lifecycle.
# Canonical source for the hermes-prune-worktrees service script. Installation
# and service reload are deliberately separate operator actions.
set -euo pipefail

APPLY=0
MIN_AGE_HOURS="${MIN_AGE_HOURS:-6}"
PRUNE_REPOS="${PRUNE_REPOS:-$HOME/.hermes/hermes-agent $HOME/family-organizer}"
if [[ -n "${HERMES_KANBAN_HOME:-}" ]]; then
  KANBAN_ROOT="$HERMES_KANBAN_HOME"
elif [[ -z "${HERMES_HOME:-}" || "$HERMES_HOME" == "$HOME/.hermes" || "$HERMES_HOME" == "$HOME/.hermes/"* ]]; then
  KANBAN_ROOT="$HOME/.hermes"
elif [[ "$(basename -- "$(dirname -- "$HERMES_HOME")")" == "profiles" ]]; then
  KANBAN_ROOT="$(dirname -- "$(dirname -- "$HERMES_HOME")")"
else
  KANBAN_ROOT="$HERMES_HOME"
fi
KANBAN_DB_PATH="${KANBAN_DB_PATH:-${HERMES_KANBAN_DB:-$KANBAN_ROOT/kanban.db}}"

if [[ "${1:-}" == "--apply" ]]; then
  APPLY=1
fi

now=$(date +%s)

is_session_holder() {
  local wt="$1"
  local pid cwd cmd
  for proc_dir in /proc/[0-9]*; do
    pid=${proc_dir##*/}
    [[ "$pid" == "$$" || "$pid" == "$PPID" ]] && continue
    cwd=$(readlink "$proc_dir/cwd" 2>/dev/null || true)
    [[ -n "$cwd" && ( "$cwd" == "$wt" || "$cwd" == "$wt"/* ) ]] || continue
    cmd=$(tr '\0' ' ' < "$proc_dir/cmdline" 2>/dev/null || true)
    case "$cmd" in
      *hermes*|*claude*|*codex*|*opencode*|*kanban-worker*|*goal_worker*) return 0 ;;
    esac
  done
  return 1
}

# Exit codes: 0 = board allows pruning, 10 = associated nonterminal task,
# 11 = board/path lookup failed (fail closed).
kanban_lifecycle_guard() {
  local wt="$1"
  python3 - "$KANBAN_DB_PATH" "$KANBAN_ROOT" "$wt" <<'PY'
import errno
import os
import sqlite3
import stat
import sys
from urllib.parse import quote

TERMINAL_STATUSES = {"done", "archived", "failed", "cancelled"}
selected_db, kanban_root, workspace = sys.argv[1:]


def resolve_db(path, *, optional):
    try:
        real = os.path.realpath(path, strict=True)
        mode = os.stat(real).st_mode
    except FileNotFoundError as exc:
        if optional and exc.errno == errno.ENOENT:
            return None
        raise
    if not stat.S_ISREG(mode):
        raise OSError(errno.EINVAL, "board DB is not a regular file", real)
    return real


def require_db(path):
    return resolve_db(path, optional=False)


try:
    workspace_real = os.path.realpath(workspace)
    task_id = os.path.basename(workspace_real)
    db_paths = [require_db(selected_db)]

    default_db = resolve_db(
        os.path.join(kanban_root, "kanban.db"),
        optional=True,
    )
    if default_db is not None:
        db_paths.append(default_db)

    boards_dir_path = os.path.join(kanban_root, "kanban", "boards")
    try:
        boards_dir = os.path.realpath(boards_dir_path, strict=True)
        boards_mode = os.stat(boards_dir).st_mode
    except FileNotFoundError as exc:
        if exc.errno != errno.ENOENT:
            raise
        boards_dir = None
    if boards_dir is not None:
        if not stat.S_ISDIR(boards_mode):
            raise NotADirectoryError(errno.ENOTDIR, "not a directory", boards_dir)
        with os.scandir(boards_dir) as entries:
            for entry in entries:
                try:
                    entry_mode = entry.stat(follow_symlinks=True).st_mode
                except FileNotFoundError as exc:
                    if exc.errno == errno.ENOENT:
                        continue
                    raise
                if not stat.S_ISDIR(entry_mode):
                    continue
                candidate = resolve_db(
                    os.path.join(entry.path, "kanban.db"),
                    optional=True,
                )
                if candidate is not None:
                    db_paths.append(candidate)

    associated = []
    for db_real in dict.fromkeys(db_paths):
        uri = f"file:{quote(db_real)}?mode=ro"
        with sqlite3.connect(uri, uri=True) as conn:
            rows = conn.execute(
                "SELECT id, status, workspace_path FROM tasks "
                "WHERE workspace_path IS NOT NULL OR id = ?",
                (task_id,),
            ).fetchall()
        for row_task_id, status, row_workspace in rows:
            same_id = row_task_id == task_id and task_id.startswith("t_")
            same_workspace = bool(row_workspace) and os.path.realpath(row_workspace) == workspace_real
            if same_id or same_workspace:
                associated.append(str(status))
except Exception:
    raise SystemExit(11)

if any(status not in TERMINAL_STATUSES for status in associated):
    raise SystemExit(10)
raise SystemExit(0)
PY
}

for repo in $PRUNE_REPOS; do
  [[ -d "$repo/.git" ]] || continue
  main_ref=$(git -C "$repo" rev-parse main 2>/dev/null || true)
  [[ -n "$main_ref" ]] || continue

  mapfile -t candidates < <(
    git -C "$repo" worktree list --porcelain | awk '/^worktree /{print substr($0,10)}' |
      awk -v repo="$repo" '$0 != repo' |
      while read -r wt; do
        case "$wt" in
          "$repo"/.worktrees/kanban/*|"$repo"/.worktrees/*/kanban/*|"$repo"/.worktrees/bridges/*)
            printf '%s\n' "$wt"
            ;;
        esac
      done
  )

  for wt in "${candidates[@]:-}"; do
    [[ -n "$wt" && -d "$wt" ]] || continue

    set +e
    kanban_lifecycle_guard "$wt"
    guard_status=$?
    set -e
    if [[ "$guard_status" -eq 10 ]]; then
      echo "kept(nonterminal task): $wt"
      continue
    fi
    if [[ "$guard_status" -ne 0 ]]; then
      echo "kept(board unavailable): $wt"
      continue
    fi

    head_ref=$(git -C "$wt" rev-parse HEAD 2>/dev/null || true)
    [[ -n "$head_ref" ]] || continue
    git -C "$repo" merge-base --is-ancestor "$head_ref" "$main_ref" 2>/dev/null || continue
    [[ -z "$(git -C "$wt" status --porcelain 2>/dev/null)" ]] || continue
    stamp=$(stat -c %Y "$wt/.git" 2>/dev/null || stat -c %Y "$wt" 2>/dev/null || echo "$now")
    age_h=$(( (now - stamp) / 3600 ))
    (( age_h >= MIN_AGE_HOURS )) || continue
    if is_session_holder "$wt"; then
      echo "kept(session): $wt"
      continue
    fi
    if (( APPLY )); then
      git -C "$repo" worktree remove "$wt"
      echo "removed: $wt (idle ${age_h}h, no session holder)"
    else
      echo "would remove: $wt (idle ${age_h}h, no session holder)"
    fi
  done

done

# ---------------------------------------------------------------------------
# Terminal isolated-write worktrees (.worktrees/terminal/{terminal_run_id})
# ---------------------------------------------------------------------------
# Separate from the regular fifteen worktree slots. Only prune when:
# - matching ended terminal-runs/{id}/manifest.json exists
# - no live tmux holder stamped with @hermes_terminal_run_id
# - path is the exact registered non-main worktree under
#   <PRUNE_REPO>/.worktrees/terminal/<terminal_run_id> (never trust worktree_path)
# - clean tree, HEAD already contained in main, age exceeded (PlanSpec: 7d UTC)
# Dirty/ahead/unmerged is never auto-removed. Free manifests have no cleanup path.
# NEVER fall back to raw rm -rf.
TERMINAL_RUNS_ROOT="${HERMES_TERMINAL_RUNS_ROOT:-}"
if [[ -z "${TERMINAL_RUNS_ROOT}" ]]; then
  if [[ -n "${HERMES_HOME:-}" ]]; then
    _tr_home="${HERMES_HOME}"
    if [[ "$(basename "$(dirname "${_tr_home}")")" == "profiles" ]]; then
      _tr_home="$(dirname "$(dirname "${_tr_home}")")"
    fi
    TERMINAL_RUNS_ROOT="${_tr_home}/terminal-runs"
  else
    TERMINAL_RUNS_ROOT="${HOME}/.hermes/terminal-runs"
  fi
fi

is_live_tmux_holder_for_run() {
  local run_id="$1"
  command -v tmux >/dev/null 2>&1 || return 1
  local target val
  while IFS= read -r target; do
    [[ -z "${target}" ]] && continue
    val="$(tmux show-options -w -v -t "${target}" @hermes_terminal_run_id 2>/dev/null || true)"
    if [[ "${val}" == "${run_id}" ]]; then
      return 0
    fi
  done < <(tmux list-windows -a -F '#{session_name}:#{window_name}' 2>/dev/null || true)
  return 1
}

# Resolve the only deletable path for a terminal_run_id: the exact registered
# non-main worktree at <repo>/.worktrees/terminal/<run_id>. Empty on any mismatch.
terminal_canonical_registered_path() {
  local repo="$1"
  local run_id="$2"
  local expected canonical main_wt main_canon registered wt_path wt_canon
  local common common_abs repo_canon common_real

  [[ "${run_id}" =~ ^[a-zA-Z0-9][a-zA-Z0-9_-]{1,63}$ ]] || return 0
  expected="${repo}/.worktrees/terminal/${run_id}"
  [[ -d "${expected}" ]] || return 0
  # Reject symlink escape: physical path must still be the expected leaf.
  canonical="$(cd "${expected}" 2>/dev/null && pwd -P || true)"
  [[ -n "${canonical}" ]] || return 0
  [[ "${canonical}" == "$(cd "${repo}/.worktrees/terminal/${run_id}" 2>/dev/null && pwd -P)" ]] || return 0
  case "${canonical}" in
    "${repo}/.worktrees/terminal/${run_id}") ;;
    *)
      # Allow when repo itself is a symlink-resolved path.
      local repo_phys expected_phys
      repo_phys="$(cd "${repo}" 2>/dev/null && pwd -P || true)"
      expected_phys="${repo_phys}/.worktrees/terminal/${run_id}"
      [[ -n "${repo_phys}" && "${canonical}" == "${expected_phys}" ]] || return 0
      ;;
  esac

  main_wt="$(git -C "${repo}" rev-parse --show-toplevel 2>/dev/null || true)"
  [[ -n "${main_wt}" ]] || return 0
  main_canon="$(cd "${main_wt}" 2>/dev/null && pwd -P || true)"
  if [[ -n "${main_canon}" && "${canonical}" == "${main_canon}" ]]; then
    return 0
  fi

  registered=0
  while IFS= read -r line; do
    case "${line}" in
      worktree\ *)
        wt_path="${line#worktree }"
        wt_canon="$(cd "${wt_path}" 2>/dev/null && pwd -P || true)"
        if [[ "${wt_canon}" == "${canonical}" ]]; then
          registered=1
          break
        fi
        ;;
    esac
  done < <(git -C "${repo}" worktree list --porcelain 2>/dev/null || true)
  [[ "${registered}" -eq 1 ]] || return 0

  # Foreign-repo guard via git-common-dir.
  common="$(git -C "${canonical}" rev-parse --git-common-dir 2>/dev/null || true)"
  [[ -n "${common}" ]] || return 0
  case "${common}" in
    /*) common_abs="${common}" ;;
    *) common_abs="${canonical}/${common}" ;;
  esac
  common_real="$(cd "${common_abs}" 2>/dev/null && pwd -P || true)"
  repo_canon="$(cd "${repo}" 2>/dev/null && pwd -P || true)"
  [[ -n "${common_real}" && -n "${repo_canon}" ]] || return 0
  case "${common_real}" in
    "${repo_canon}"/.git|"${repo_canon}"/.git/*) ;;
    *) return 0 ;;
  esac

  printf '%s\n' "${canonical}"
}

if [[ -d "${TERMINAL_RUNS_ROOT}" ]] && command -v python3 >/dev/null 2>&1; then
  shopt -s nullglob
  # Default PlanSpec age: 7 days. Do NOT inherit the generic 6h kanban MIN_AGE.
  : "${TERMINAL_PRUNE_MIN_AGE_SECONDS:=$((7 * 24 * 3600))}"
  for manifest in "${TERMINAL_RUNS_ROOT}"/*/manifest.json; do
    parsed="$(
      TERMINAL_PRUNE_MIN_AGE_SECONDS="${TERMINAL_PRUNE_MIN_AGE_SECONDS}" \
      python3 - "$manifest" <<'PY'
import json, os, re, sys
from datetime import datetime, timezone
from pathlib import Path

path = Path(sys.argv[1])
try:
    data = json.loads(path.read_text(encoding="utf-8"))
except Exception:
    raise SystemExit(0)
if not isinstance(data, dict):
    raise SystemExit(0)
# Free manifests have no worktree cleanup path (ignore any adversarial path).
if data.get("start_mode") != "isolated_write":
    raise SystemExit(0)
if data.get("status") != "ended":
    raise SystemExit(0)
run_id = str(path.parent.name).strip()
manifest_run = str(data.get("terminal_run_id") or "").strip()
if manifest_run and manifest_run != run_id:
    raise SystemExit(0)
if not re.fullmatch(r"[a-zA-Z0-9][a-zA-Z0-9_-]{1,63}", run_id):
    raise SystemExit(0)
ended = data.get("ended_at")
if not ended:
    raise SystemExit(0)
# UTC-only age policy; reject naive or non-UTC timestamps.
try:
    ended_text = str(ended)
    if not ended_text.endswith("Z"):
        raise ValueError("ended_at must be UTC Z time")
    text = ended_text[:-1] + "+00:00"
    dt = datetime.fromisoformat(text)
    if dt.tzinfo is None or dt.utcoffset() != timezone.utc.utcoffset(dt):
        raise ValueError("ended_at must be UTC")
    ts = dt.timestamp()
except Exception:
    raise SystemExit(0)
min_age = int(os.environ.get("TERMINAL_PRUNE_MIN_AGE_SECONDS", str(7 * 24 * 3600)))
now = datetime.now(timezone.utc).timestamp()
if now - ts < min_age:
    raise SystemExit(0)
declared = data.get("worktree_path")
if not isinstance(declared, str) or not declared or "\n" in declared or "\t" in declared:
    raise SystemExit(0)
# The shell derives the only deletable path from run_id, then requires this
# untrusted declaration to canonicalize to that exact registered worktree.
print(f"{run_id}\t{declared}")
PY
    )" || true
    [[ -z "${parsed}" ]] && continue
    run_id="${parsed%%$'\t'*}"
    manifest_wt_path="${parsed#*$'\t'}"
    if is_live_tmux_holder_for_run "${run_id}"; then
      echo "kept(tmux-holder): run ${run_id}"
      continue
    fi

    removed_or_planned=0
    for repo in ${PRUNE_REPOS}; do
      [[ -d "${repo}/.git" || -f "${repo}/.git" ]] || continue
      wt_path="$(terminal_canonical_registered_path "${repo}" "${run_id}")"
      [[ -n "${wt_path}" ]] || continue
      manifest_wt_canon="$(cd "${manifest_wt_path}" 2>/dev/null && pwd -P || true)"
      if [[ -z "${manifest_wt_canon}" || "${manifest_wt_canon}" != "${wt_path}" ]]; then
        echo "kept(manifest-path-mismatch): run ${run_id}"
        continue
      fi

      # Never auto-remove dirty / ahead / unmerged worktrees.
      if [[ -n "$(git -C "${wt_path}" status --porcelain 2>/dev/null || true)" ]]; then
        echo "kept(dirty): $wt_path"
        continue
      fi
      head_sha="$(git -C "${wt_path}" rev-parse HEAD 2>/dev/null || true)"
      [[ -n "${head_sha}" ]] || continue
      main_ref=""
      if git -C "${repo}" rev-parse --verify main >/dev/null 2>&1; then
        main_ref="main"
      elif git -C "${repo}" rev-parse --verify master >/dev/null 2>&1; then
        main_ref="master"
      fi
      [[ -n "${main_ref}" ]] || continue
      if ! git -C "${repo}" merge-base --is-ancestor "${head_sha}" "${main_ref}" 2>/dev/null; then
        echo "kept(ahead-or-diverged): $wt_path"
        continue
      fi
      if [[ -n "$(git -C "${wt_path}" ls-files -u 2>/dev/null || true)" ]]; then
        echo "kept(unmerged): $wt_path"
        continue
      fi
      if (( APPLY )); then
        if git -C "${repo}" worktree remove "${wt_path}" >/dev/null 2>&1; then
          echo "removed(terminal): $wt_path (run ${run_id})"
          removed_or_planned=1
        else
          # No rm -rf fallback — refuse unsafe cleanup.
          echo "kept(remove-failed): $wt_path (run ${run_id})"
        fi
      else
        echo "would remove(terminal): $wt_path (run ${run_id})"
        removed_or_planned=1
      fi
    done
    if (( removed_or_planned == 0 )); then
      :
    fi
  done
  shopt -u nullglob
fi
