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
