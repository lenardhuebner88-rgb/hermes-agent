#!/usr/bin/env bash
# run-affected.sh [<ref>] — run ONLY the pytest files affected by the diff.
#
# If nothing is affected (the diff maps no .py to an existing test file),
# SKIP pytest entirely (exit 0) — NEVER fall back to the full suite.
#
# This replaces the unsafe one-liner `scripts/run_tests.sh $(scripts/affected-tests.sh)`:
# when affected-tests.sh prints nothing, the command substitution expands to
# nothing and `run_tests.sh` runs with no path args = the WHOLE suite (~44k
# tests, timeout / EXIT 124). That is exactly the "accidentally ran the full
# suite" failure the targeted-scope rule exists to prevent.
#
# The affected-set computation is pure stdlib/bash (works in a bare worktree
# without the venv); on a non-empty diff the forwarded run_tests.sh still needs a
# venv to run pytest and will fail loudly if none is found — which is correct, not
# a silent full-suite run. Forwards an optional <ref> straight to affected-tests.sh
# (e.g. HEAD~1, main...HEAD).
# Frontend gates stay separate as before — this wrapper covers only the pytest part.
# The classifier's own default is the narrow 1200s post-merge integrator budget
# used by hermes_cli/kanban_worktrees.py. This worker/loop path is allowed 3600s.
set -euo pipefail
export HERMES_AFFECTED_TIME_BUDGET="${HERMES_AFFECTED_TIME_BUDGET:-3600}"
if ! "$(dirname "$0")/check-branch-age.sh"; then
  echo "run-affected: branch-age preflight failed — NO test ran. Fix: git merge main; for a deliberate stale-worktree override use HERMES_GATE_STALE_OK=1." >&2
  exit 3
fi
DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
set +e
FILES=$("$DIR/affected-tests.sh" "$@")
mapping_status=$?
set -e
if [ "$mapping_status" -eq 4 ]; then
  echo "run-affected: affected-test mapping is incomplete — NO test ran." >&2
  exit 4
elif [ "$mapping_status" -eq 5 ]; then
  echo "run-affected: affected-test time budget exceeded — NO test ran." >&2
  exit 5
elif [ "$mapping_status" -ne 0 ]; then
  echo "run-affected: affected-test mapping failed with exit ${mapping_status} — NO test ran." >&2
  exit "$mapping_status"
fi
if [ -z "${FILES// /}" ]; then
  echo "run-affected: no applicable Python production paths for this diff — skipping pytest (targeted scope; full suite is nightly only)"
  exit 0
fi
run_output=$(mktemp)
stamp_state=$(mktemp)
trap 'rm -f "$run_output" "$stamp_state"' EXIT
# Observability only (A5): load + serial-duration stamp. Must never influence exit.
# Both calls are hardened: script exits 0 on any internal error, shell uses || true
# so a missing interpreter/script cannot trip set -e.
python3 "$DIR/gate_load_stamp.py" start --state "$stamp_state" -- $FILES || true
set +e
"$DIR/run_tests.sh" $FILES | tee "$run_output"   # intentionally unquoted: word-split paths into args
first_status=${PIPESTATUS[0]}
set -e
# Stamp AFTER PIPESTATUS capture — never insert anything between runner and PIPESTATUS[0].
python3 "$DIR/gate_load_stamp.py" finish --state "$stamp_state" || true
if [ "$first_status" -eq 0 ]; then
  exit 0
fi

mapfile -t failed_files < <(
  awk '
    /^=== [0-9]+ files? with test failures \([0-9]+ tests? failed\) ===$/ {
      in_failure_list = 1
      next
    }
    /^=== [0-9]+ files? with pytest setup\/teardown\/collection errors \([0-9]+ errors?\) ===$/ {
      in_failure_list = 1
      next
    }
    /^=== [0-9]+ files? where all tests passed but pytest exited non-zero \(warnings-as-errors, hook failures, etc\.\) ===$/ {
      in_failure_list = 1
      next
    }
    /^=== [0-9]+ files? where no tests ran \(collection\/import error, timeout before collection, etc\.\) ===$/ {
      in_failure_list = 1
      next
    }
    /^=== / {
      in_failure_list = 0
      next
    }
    in_failure_list && /^  [^[:space:]]/ {
      file = $0
      sub(/^  /, "", file)
      sub(/  \([^)]*\)$/, "", file)
      if (!seen[file]++) {
        print file
      }
      next
    }
    in_failure_list {
      in_failure_list = 0
    }
  ' "$run_output"
)

if [ "${#failed_files[@]}" -gt 0 ]; then
  failed_file_noun="files"
  if [ "${#failed_files[@]}" -eq 1 ]; then
    failed_file_noun="file"
  fi
  echo "run-affected: first affected-test run failed with exit ${first_status}; rerunning once (${#failed_files[@]} failed ${failed_file_noun}) to require reproduced red before hold"
  set +e
  "$DIR/run_tests.sh" "${failed_files[@]}"
  rerun_status=$?
  set -e
  exit "$rerun_status"
fi

full_file_count=0
for _file in $FILES; do
  full_file_count=$((full_file_count + 1))
done
full_file_noun="files"
if [ "$full_file_count" -eq 1 ]; then
  full_file_noun="file"
fi
echo "run-affected: runner produced no failure list — rerunning the full selection" >&2
echo "run-affected: first affected-test run failed with exit ${first_status}; rerunning once (${full_file_count} ${full_file_noun}, full selection) to require reproduced red before hold"
"$DIR/run_tests.sh" $FILES
