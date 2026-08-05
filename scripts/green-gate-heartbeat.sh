#!/usr/bin/env bash
# shellcheck disable=SC2012,SC2317
# green-gate-heartbeat.sh — nightly green-gate for the committed Hermes branch.
#
# The Python full suite runs from an isolated checkout of the live repository's
# committed HEAD. Frontend gates and the stale-diff check deliberately continue
# to inspect the live working tree.
set -uo pipefail

REPO="${HERMES_AGENT_REPO:-/home/piet/.hermes/hermes-agent}"
WEB="$REPO/web"
WEB_BIN="$REPO/node_modules/.bin"
ALERT_CHANNEL="${GREEN_GATE_ALERT_CHANNEL:-1486480074491559966}"
NOTIFY="${GREEN_GATE_NOTIFY:-/home/piet/.hermes/scripts/discord-notify.py}"
TMP_BUILD="$(mktemp -d /tmp/green-gate-build.XXXXXX)"

# The checkout is disposable; the independently-created environment persists
# across nights. Keeping both below one gate-owned root makes cleanup exact and
# avoids touching the live repository's many foreign worktrees.
PY_STATE_ROOT="${GREEN_GATE_PYTHON_STATE_DIR:-$HOME/.hermes/green-gate-python}"
PY_GIT_DIR="$PY_STATE_ROOT/repo.git"
PY_CHECKOUT="$PY_STATE_ROOT/checkout"
PY_ENV="$PY_STATE_ROOT/venv"
remove_python_checkout_on_exit=""

ts() { date "+%Y-%m-%d %H:%M:%S"; }
log() { printf '[%s] %s\n' "$(ts)" "$*"; }

safe_remove_python_checkout() {
  # Only this dedicated bare repository can own PY_CHECKOUT. A killed prior run
  # may have left either worktree metadata, a directory, or both.
  if [ -d "$PY_GIT_DIR" ] && \
     git --git-dir="$PY_GIT_DIR" rev-parse --is-bare-repository >/dev/null 2>&1; then
    git --git-dir="$PY_GIT_DIR" worktree remove --force "$PY_CHECKOUT" \
      >/dev/null 2>&1 || true
    git --git-dir="$PY_GIT_DIR" worktree prune --expire=now >/dev/null 2>&1 || true
  fi
  if [ -e "$PY_CHECKOUT" ] || [ -L "$PY_CHECKOUT" ]; then
    case "$PY_CHECKOUT" in
      "$PY_STATE_ROOT/checkout")
        CONFIRMED=1 rm -rf -- "$PY_CHECKOUT" 2>/dev/null || true
        ;;
      *)
        log "WARN: refusing unsafe Python checkout cleanup: $PY_CHECKOUT"
        return 1
        ;;
    esac
  fi
}

cleanup() {
  if [ -n "$remove_python_checkout_on_exit" ]; then
    safe_remove_python_checkout || true
  fi
  CONFIRMED=1 rm -rf -- "$TMP_BUILD" 2>/dev/null || true
}
trap cleanup EXIT
trap 'remove_python_checkout_on_exit=1; exit 129' HUP
trap 'remove_python_checkout_on_exit=1; exit 130' INT
trap 'remove_python_checkout_on_exit=1; exit 143' TERM

# Full gate logs survive the run (the 8-line Discord tail was the ONLY
# artifact of the 2026-06-12 onboarding RED — root cause undiagnosable).
# One dir per run, pruned to the newest 14 runs.
LOG_ROOT="${GREEN_GATE_LOG_DIR:-/home/piet/.hermes/logs/green-gate}"
RUN_LOG_DIR="$LOG_ROOT/$(date +%Y%m%d-%H%M%S)"
mkdir -p "$RUN_LOG_DIR"
ls -1d "$LOG_ROOT"/2* 2>/dev/null | head -n -14 | while read -r old; do
  CONFIRMED=1 rm -rf "$old" 2>/dev/null || true
done

# Uncommitted work in the live checkout whose newest file is older than
# this is considered stranded (approved-but-never-landed worker output,
# like t_4cc0fe1b on 2026-06-12) and worth a ping even when gates are green.
STALE_DIFF_HOURS="${GREEN_GATE_STALE_DIFF_HOURS:-2}"

fails=()
fail_gates=()
warns=()

# Freeze the exact committed revision before any gate starts. The live checkout
# may keep changing while the full suite runs.
branch="$(git -C "$REPO" symbolic-ref --quiet --short HEAD 2>/dev/null || echo detached)"
head="$(git -C "$REPO" rev-parse HEAD 2>/dev/null || echo '?')"

prepare_committed_python_checkout() {
  local setup_started setup_finished import_path checkout_common

  setup_started="$(date +%s%N)"
  [ "$head" != "?" ] || {
    echo "ERROR: cannot resolve committed HEAD in $REPO"
    return 1
  }
  mkdir -p "$PY_STATE_ROOT" || return 1

  # A shared local clone has its own refs/worktree registry but reuses the
  # source repository's immutable object store. That keeps the cold path cheap
  # without exposing any uncommitted source-tree content.
  if ! git --git-dir="$PY_GIT_DIR" rev-parse --is-bare-repository \
      >/dev/null 2>&1; then
    if [ -e "$PY_GIT_DIR" ] || [ -L "$PY_GIT_DIR" ]; then
      case "$PY_GIT_DIR" in
        "$PY_STATE_ROOT/repo.git")
          CONFIRMED=1 rm -rf -- "$PY_GIT_DIR" || return 1
          ;;
        *)
          echo "ERROR: refusing unsafe Git-dir cleanup: $PY_GIT_DIR"
          return 1
          ;;
      esac
    fi
    git clone --bare --shared --no-tags "$REPO" "$PY_GIT_DIR" || return 1
  fi
  git --git-dir="$PY_GIT_DIR" fetch --force --no-tags "$REPO" \
    "$head:refs/green-gate/committed-head" || return 1

  # Reuse the registered checkout on ordinary nights; recreating ~9k files is
  # far slower than switching the handful that changed. If a kill interrupted
  # initial creation, the ownership check fails and only this gate's dedicated
  # registry/path is removed before a clean add.
  checkout_common="$(
    git -C "$PY_CHECKOUT" rev-parse --path-format=absolute --git-common-dir \
      2>/dev/null || true
  )"
  if [ "$checkout_common" = "$PY_GIT_DIR" ]; then
    if [ -e "$PY_CHECKOUT/.venv" ] || [ -L "$PY_CHECKOUT/.venv" ]; then
      rm -f -- "$PY_CHECKOUT/.venv" || return 1
    fi
    git -C "$PY_CHECKOUT" checkout --detach --force \
      refs/green-gate/committed-head || return 1
    git -C "$PY_CHECKOUT" clean -ffd >/dev/null || return 1
  else
    safe_remove_python_checkout || return 1
    git --git-dir="$PY_GIT_DIR" worktree add --detach "$PY_CHECKOUT" \
      refs/green-gate/committed-head || return 1
  fi

  echo "SETUP: uv sync --locked --extra all --extra dev --extra messaging"
  if ! (
    cd "$PY_CHECKOUT" &&
    UV_PROJECT_ENVIRONMENT="$PY_ENV" \
      uv sync --locked --extra all --extra dev --extra messaging
  ); then
    return 1
  fi

  # run_tests.sh checks <checkout>/.venv before every fallback. This symlink is
  # to the gate's own uv environment, never to the live editable environment.
  ln -s "$PY_ENV" "$PY_CHECKOUT/.venv" || return 1
  import_path="$(
    cd "$PY_CHECKOUT" &&
      "$PY_ENV/bin/python" -c \
        'import pathlib, hermes_cli; print(pathlib.Path(hermes_cli.__file__).resolve())' \
        2>/dev/null
  )" || return 1
  case "$import_path" in
    "$PY_CHECKOUT"/*) ;;
    *)
      echo "ERROR: isolated environment imports hermes_cli from $import_path"
      return 1
      ;;
  esac
  setup_finished="$(date +%s%N)"
  python_setup_ms=$(( (setup_finished - setup_started) / 1000000 ))
  echo "SETUP_MS: $python_setup_ms"
  echo "IMPORT_PROOF: $import_path"
}

run_committed_python_gate() {
  local target
  local test_args=()

  prepare_committed_python_checkout || return 1
  target="${GREEN_GATE_PYTHON_TEST_TARGET:-}"
  [ -z "$target" ] || test_args+=("$target")
  (
    cd "$PY_CHECKOUT" &&
    HERMES_TEST_FILE_TIMEOUT=600 \
      scripts/run_tests.sh "${test_args[@]}"
  )
}

# --- Python gate: isolated committed HEAD -----------------------------------
log "Python gate: branch $branch committed HEAD $head"
py_log="$RUN_LOG_DIR/python.log"
{
  printf 'BRANCH: %s\n' "$branch"
  printf 'COMMITTED_HEAD: %s\n' "$head"
  printf 'LIVE_REPO: %s\n' "$REPO"
  printf 'ISOLATED_CHECKOUT: %s\n' "$PY_CHECKOUT"
} >"$py_log"
python_setup_ms="?"
python_gate_status="ROT"
if run_committed_python_gate >>"$py_log" 2>&1; then
  python_gate_status="GRÜN"
  log "Python gate GREEN — committed HEAD $head (setup ${python_setup_ms}ms)"
else
  tail_lines="$(tail -n 8 "$py_log")"
  fails+=("Python branch \`$head\` (run_tests.sh):"$'\n'"$tail_lines"$'\n'"Volles Log: $py_log")
  fail_gates+=("python")
  log "Python gate RED — committed HEAD $head"
fi

# --- Frontend gate (live web/, root node_modules/.bin) ----------------------
# GREEN_GATE_PYTHON_ONLY is a verification hook for the end-to-end isolation
# proofs. It is unset in production; the live frontend behavior stays unchanged.
if [ -z "${GREEN_GATE_PYTHON_ONLY:-}" ]; then
  if [ -x "$WEB_BIN/tsc" ] && [ -x "$WEB_BIN/vitest" ]; then
    export PATH="$WEB_BIN:$PATH"

    log "Frontend gate: tsc --noEmit"
    tsc_log="$RUN_LOG_DIR/tsc.log"
    if ( cd "$WEB" && tsc --noEmit ) >"$tsc_log" 2>&1; then
      log "tsc GREEN"
    else
      fails+=("Frontend tsc --noEmit:"$'\n'"$(tail -n 8 "$tsc_log")"$'\n'"Volles Log: $tsc_log")
      fail_gates+=("tsc")
      log "tsc RED"
    fi

    log "Frontend gate: vitest run"
    vi_log="$RUN_LOG_DIR/vitest.log"
    if ( cd "$WEB" && vitest run ) >"$vi_log" 2>&1; then
      log "vitest GREEN"
    else
      fails+=("Frontend vitest run:"$'\n'"$(tail -n 8 "$vi_log")"$'\n'"Volles Log: $vi_log")
      fail_gates+=("vitest")
      log "vitest RED"
    fi

    log "Frontend gate: vite build (temp outdir, does not touch live web_dist)"
    bd_log="$RUN_LOG_DIR/build.log"
    if ( cd "$WEB" && tsc -b && vite build --outDir "$TMP_BUILD" --emptyOutDir ) >"$bd_log" 2>&1; then
      log "build GREEN"
    else
      fails+=("Frontend vite build:"$'\n'"$(tail -n 8 "$bd_log")"$'\n'"Volles Log: $bd_log")
      fail_gates+=("build")
      log "build RED"
    fi
  else
    fails+=("Frontend toolchain missing: $WEB_BIN/{tsc,vitest} not executable — node_modules not installed?")
    fail_gates+=("build")
    log "Frontend toolchain MISSING"
  fi
fi

# --- Stale uncommitted work in the live checkout ----------------------------
# A dirty tree whose NEWEST file is old means stranded worker output
# (nobody is actively editing) — that diff is one `reset --hard` away
# from being lost and never went through a landing step.
log "Stale-diff check (threshold ${STALE_DIFF_HOURS}h)"
dirty_newest=0
dirty_list=""
dirty_count=0
while IFS= read -r line; do
  [ -n "$line" ] || continue
  f="${line:3}"
  case "$f" in *" -> "*) f="${f##* -> }";; esac
  dirty_count=$((dirty_count + 1))
  [ "$dirty_count" -le 10 ] && dirty_list+="$line"$'\n'
  m="$(stat -c %Y "$REPO/$f" 2>/dev/null || echo 0)"
  [ "$m" -gt "$dirty_newest" ] && dirty_newest="$m"
done < <(cd "$REPO" && git status --porcelain 2>/dev/null)
if [ "$dirty_count" -gt 0 ] && [ "$dirty_newest" -gt 0 ]; then
  age_s=$(( $(date +%s) - dirty_newest ))
  if [ "$age_s" -ge $(( STALE_DIFF_HOURS * 3600 )) ]; then
    warns+=("Uncommitteter Diff im Live-Checkout ($dirty_count Datei(en)), neueste Änderung vor $(( age_s / 3600 ))h — gestrandete Worker-Arbeit? Landen oder verwerfen:"$'\n'"$dirty_list")
    log "stale diff: $dirty_count files, newest ${age_s}s old"
  else
    log "dirty tree is fresh ($dirty_count files) — no warn"
  fi
else
  log "working tree clean"
fi
log "Branch $branch @ $head (Python): $python_gate_status"
log "Live-Baum: $dirty_count Datei(en) dirty"

# --- Verdict ----------------------------------------------------------------
# S3 commit attribution (2026-07-06): stamp the gate run's tested HEAD sha into
# every ledger record so triage can derive per-night suspect commit ranges.
sha_args=()
[ "$head" != "?" ] && sha_args=(--head-sha "$head")

# Vision-Flywheel: feed the green-gate streak the strategist reads. The
# verification hook avoids mutating the real ledger during disposable proofs.
use_purity=""
leakers_json="[]"
leaker_only=""
iso_leaker_summary=""
failure_classification=""
failed_test_files_json="[]"
quarantined_test_files_json="[]"
blocking_test_files_json="[]"
unattributed_fail_gates_json="[]"
quarantine_policy_sha256=""
if [ "${GREEN_GATE_RECORD_RESULT:-1}" != "0" ]; then
  if [ "${#fails[@]}" -eq 0 ]; then
    hermes vision record-gate-result pass "${sha_args[@]+"${sha_args[@]}"}" >/dev/null 2>&1 || \
      hermes vision record-gate-result pass >/dev/null 2>&1 || \
      log "WARN: vision record-gate-result pass failed"
  else
    first_fail_gate="${fail_gates[0]:-build}"
    first_fail_detail="${fails[0]}"

    # Python failures rerun against the committed checkout. Vitest remains
    # deliberately tied to the live frontend tree. Isolation changes cause
    # attribution only. The separate exact-name classification below stamps
    # whether landing may tolerate the otherwise still-RED gate record.
    iso_all_ok=1
    iso_any=0
    iso_all_leaker=1
    first_iso_gate=""
    first_iso_detail=""
    iso_leakers=()
    classification_gate_args=()
    classification_unattributed_args=()
    quarantine_allowlist="$PY_CHECKOUT/scripts/green_gate_quarantine_allowlist.json"
    [ -f "$quarantine_allowlist" ] || \
      quarantine_allowlist="$REPO/scripts/green_gate_quarantine_allowlist.json"

    # Collect classification evidence for EVERY red gate before isolation.
    # Isolation may stop early; classification must never inherit that truncation.
    for classification_gate in "${fail_gates[@]}"; do
      classification_log_path=""
      case "$classification_gate" in
        python) classification_log_path="${py_log:-}" ;;
        tsc) classification_log_path="${tsc_log:-}" ;;
        vitest) classification_log_path="${vi_log:-}" ;;
        build) classification_log_path="${bd_log:-}" ;;
        *) ;;
      esac
      if [ -n "$classification_log_path" ] && [ -f "$classification_log_path" ]; then
        classification_gate_args+=(
          --gate-log "$classification_gate=$classification_log_path"
        )
      else
        classification_unattributed_args+=(
          --unattributed-gate "$classification_gate"
        )
      fi
    done

    for g in "${fail_gates[@]}"; do
      case "$g" in
        python)
          [ -f "${py_log:-}" ] || continue
          gate_log="$py_log"
          gate_repo="$PY_CHECKOUT"
          ;;
        tsc)
          [ -f "${tsc_log:-}" ] || continue
          gate_log="$tsc_log"
          gate_repo="$REPO"
          ;;
        vitest)
          [ -f "${vi_log:-}" ] || continue
          gate_log="$vi_log"
          gate_repo="$REPO"
          ;;
        build)
          [ -f "${bd_log:-}" ] || continue
          gate_log="$bd_log"
          gate_repo="$REPO"
          ;;
        *) continue ;;
      esac
      iso_log="$RUN_LOG_DIR/isolate-${g}.log"
      if iso_json="$(hermes vision isolate-fails --repo "$gate_repo" \
          --gate-log "$g=$gate_log" --json 2>"$iso_log")"; then
        iso_any=1
        eval "$(printf '%s' "$iso_json" | python3 -c '
import json, shlex, sys
try:
    d = json.load(sys.stdin)
except Exception:
    sys.exit(0)
print("one_ok=1")
print("one_gate=%s" % shlex.quote(d.get("first_fail_gate") or ""))
print("one_detail=%s" % shlex.quote(d.get("first_fail_detail") or ""))
print("one_leakers=%s" % shlex.quote("\n".join(d.get("leakers") or [])))
print("one_leaker_only=%s" % shlex.quote("1" if d.get("leaker_only") else ""))
' 2>/dev/null)"
        if [ "${one_ok:-}" != "1" ]; then
          iso_all_ok=0
          break
        fi
        while IFS= read -r item; do
          [ -z "$item" ] || iso_leakers+=("$item")
        done <<<"${one_leakers:-}"
        if [ -z "${one_leaker_only:-}" ]; then
          iso_all_leaker=0
        fi
        if [ -z "$first_iso_gate" ] && [ -n "${one_gate:-}" ]; then
          first_iso_gate="$one_gate"
          first_iso_detail="${one_detail:-}"
        fi
      else
        iso_all_ok=0
        break
      fi
      unset one_ok one_gate one_detail one_leakers one_leaker_only
    done

    classification_log="$RUN_LOG_DIR/classify-failures.log"
    if classification_json="$(hermes vision classify-gate-failures \
        --quarantine-allowlist "$quarantine_allowlist" \
        ${classification_gate_args[@]+"${classification_gate_args[@]}"} \
        ${classification_unattributed_args[@]+"${classification_unattributed_args[@]}"} \
        --json 2>"$classification_log")"; then
      eval "$(printf '%s' "$classification_json" | python3 -c '
import json, shlex, sys
try:
    d = json.load(sys.stdin)
except Exception:
    sys.exit(0)
print("classification_ok=1")
print("failure_classification=%s" % shlex.quote(d.get("failure_classification") or ""))
for key in ("failed_test_files", "quarantined_test_files", "blocking_test_files", "unattributed_fail_gates"):
    print("%s_json=%s" % (key, shlex.quote(json.dumps(d.get(key) or [], ensure_ascii=False))))
print("quarantine_policy_sha256=%s" % shlex.quote(d.get("quarantine_policy_sha256") or ""))
' 2>/dev/null)"
      if [ "${classification_ok:-}" != "1" ] || \
         [ -z "$failure_classification" ] || \
         [ -z "$quarantine_policy_sha256" ]; then
        failure_classification=""
        log "WARN: gate failure classification returned incomplete data"
      else
        log "gate failures classified: $failure_classification"
      fi
    else
      log "WARN: gate failure classification unavailable; landing stays blocked"
    fi

    if [ "$iso_all_ok" -eq 1 ] && [ "$iso_any" -eq 1 ]; then
      use_purity=1
      if [ "${#iso_leakers[@]}" -gt 0 ]; then
        leakers_json="$(printf '%s\n' "${iso_leakers[@]}" | python3 -c \
          'import json, sys; print(json.dumps([x.rstrip("\n") for x in sys.stdin if x.rstrip("\n")], ensure_ascii=False))')"
        iso_leaker_summary="$(IFS=', '; echo "${iso_leakers[*]}")"
      fi
      if [ "$iso_all_leaker" -eq 1 ]; then
        leaker_only=1
        first_fail_detail=""
        log "isolation: leaker-only red night (no product cause); leakers: $leakers_json"
      else
        [ -n "$first_iso_gate" ] && first_fail_gate="$first_iso_gate"
        [ -n "$first_iso_detail" ] && first_fail_detail="$first_iso_detail"
        log "isolation: first_fail gate '$first_fail_gate'; demoted leakers: $leakers_json"
      fi
    else
      log "WARN: isolate-fails skipped/unavailable; using raw first_fail"
    fi

    rec_args=(fail --first-fail-gate "$first_fail_gate" \
      --first-fail-detail "$first_fail_detail" \
      "${sha_args[@]+"${sha_args[@]}"}")
    if [ -n "$use_purity" ]; then
      rec_args+=(--leakers-json "$leakers_json")
      [ -n "$leaker_only" ] && rec_args+=(--leaker-only)
    fi
    if [ -n "$failure_classification" ]; then
      rec_args+=(--failure-classification "$failure_classification" \
        --failed-test-files-json "$failed_test_files_json" \
        --quarantined-test-files-json "$quarantined_test_files_json" \
        --blocking-test-files-json "$blocking_test_files_json" \
        --unattributed-fail-gates-json "$unattributed_fail_gates_json" \
        --quarantine-policy-sha256 "$quarantine_policy_sha256")
    fi
    if ! hermes vision record-gate-result "${rec_args[@]}" >/dev/null 2>&1; then
      fallback_args=(fail --first-fail-gate "$first_fail_gate" \
        --first-fail-detail "$first_fail_detail" \
        "${sha_args[@]+"${sha_args[@]}"}")
      hermes vision record-gate-result "${fallback_args[@]}" >/dev/null 2>&1 || \
        log "WARN: vision record-gate-result fail failed"
    fi

    ah_log="$RUN_LOG_DIR/autoheal.log"
    hermes vision gate-fix-check --json >>"$ah_log" 2>&1 || \
      log "WARN: gate-fix-check failed"
    hermes vision triage-check --json >>"$ah_log" 2>&1 || \
      log "WARN: triage-check failed"
    log "autoheal: gate-fix-check + triage-check ran (see $ah_log)"
  fi
fi

if [ "${#fails[@]}" -eq 0 ] && [ "${#warns[@]}" -eq 0 ]; then
  log "ALL GREEN — staying silent (branch $branch @ $head)"
  exit 0
fi

if [ "${#fails[@]}" -gt 0 ]; then
  msg="🔴 **Green-Gate RED**"$'\n'
else
  msg="🟡 **Green-Gate WARN**"$'\n'
fi
msg+="Branch \`$branch\` @ \`$head\` (Python): **$python_gate_status**"$'\n'
msg+="Live-Baum: **$dirty_count Datei(en) dirty**"$'\n'"$(ts)"$'\n'
for f in "${fails[@]+"${fails[@]}"}"; do
  msg+=$'\n'"❌ ${f}"$'\n'
done
for w in "${warns[@]+"${warns[@]}"}"; do
  msg+=$'\n'"⚠️ ${w}"$'\n'
done
if [ -n "$use_purity" ]; then
  if [ -n "$leaker_only" ]; then
    msg+=$'\n'"🧪 Alle gemeldeten Fails sind Test-Isolations-Leaker (grün im Einzel-Rerun) — keine Produktursache. Rot bleibt rot."$'\n'
  elif [ -n "$iso_leaker_summary" ]; then
    msg+=$'\n'"🧪 Demoted Leaker (grün allein, NICHT die Ursache): ${iso_leaker_summary}"$'\n'
  fi
fi

# Chronic-red demotion (S3, 2026-07-06): from the 3rd consecutive red night
# on, the full fail-wall ping degrades to one compact message.
chronic_prior=0
if [ "${#fails[@]}" -gt 0 ]; then
  chronic_prior="$(python3 - <<'PY' 2>/dev/null || echo 0
import datetime, json, os
path = os.path.expanduser("~/.hermes/state/green-gate-ledger.jsonl")
today = datetime.datetime.now(datetime.timezone.utc).strftime("%Y-%m-%d")
per = {}
try:
    for line in open(path, encoding="utf-8"):
        line = line.strip()
        if not line:
            continue
        r = json.loads(line)
        d = str(r.get("date") or "")
        if not d or d == today:
            continue
        per[d] = per.get(d, False) or (str(r.get("result", "")).lower() != "pass")
except (FileNotFoundError, ValueError):
    pass
streak = 0
for d in sorted(per, reverse=True):
    if per[d]:
        streak += 1
    else:
        break
print(streak)
PY
)"
  case "$chronic_prior" in (*[!0-9]*|"") chronic_prior=0;; esac
fi
if [ "${#fails[@]}" -gt 0 ] && [ "$chronic_prior" -ge 2 ]; then
  night_n=$((chronic_prior + 1))
  msg="🟠 **Green-Gate chronisch rot** — Nacht ${night_n} in Folge."$'\n'
  msg+="Branch \`$branch\` @ \`$head\` (Python): **$python_gate_status**"$'\n'
  msg+="Live-Baum: **$dirty_count Datei(en) dirty**"$'\n'
  msg+="Details: Ledger/red_files_by_night + $RUN_LOG_DIR"$'\n'"$(ts)"
  log "chronic red (prior streak $chronic_prior) — demoted to compact ping"
fi
msg="${msg:0:1850}"

log "RED — posting alert to channel $ALERT_CHANNEL"
delivery_rc=0
if [ -x "$NOTIFY" ] || [ -f "$NOTIFY" ]; then
  if ! printf '%s' "$msg" | python3 "$NOTIFY" --channel "$ALERT_CHANNEL" --stdin; then
    log "WARN: discord-notify failed; alert not delivered"
    delivery_rc=1
  fi
else
  log "WARN: $NOTIFY not found; alert not delivered"
  delivery_rc=1
fi
exit "$delivery_rc"
