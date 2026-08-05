#!/usr/bin/env bash
# Deterministic floor for newly failed systemd user units. It observes and
# reports; it deliberately never restarts or resets a unit.
set -euo pipefail

SCRIPT_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
ALLOWLIST="${FAILED_UNITS_FLOOR_ALLOWLIST:-$SCRIPT_DIR/loop_failed_units_allowlist.txt}"
default_state_dir="${HERMES_LOOP_STATE_DIR:-${HERMES_HOME:-$HOME/.hermes}/state}"
STATE="${FAILED_UNITS_FLOOR_STATE:-$default_state_dir/failed-units-floor.txt}"
SYSTEMCTL="${FAILED_UNITS_FLOOR_SYSTEMCTL:-systemctl}"
NOTIFY="${FAILED_UNITS_FLOOR_NOTIFY:-/home/piet/.hermes/scripts/discord-notify.py}"
CHANNEL="${FAILED_UNITS_FLOOR_CHANNEL:-1486480074491559966}"

if [[ ! -r "$ALLOWLIST" ]]; then
  printf 'failed-units-floor: allowlist is not readable: %s\n' "$ALLOWLIST" >&2
  exit 1
fi

state_dir="$(dirname -- "$STATE")"
mkdir -p "$state_dir"
work_dir="$(mktemp -d "${TMPDIR:-/tmp}/failed-units-floor.XXXXXX")"
next_state="$state_dir/.failed-units-floor.$$.tmp"
trap 'rm -rf "$work_dir"; rm -f "$next_state"' EXIT

"$SYSTEMCTL" --user list-units --state=failed --plain --no-legend \
  >"$work_dir/systemctl.txt"
awk '{print $1}' "$work_dir/systemctl.txt" | LC_ALL=C sort -u \
  >"$work_dir/all-failed.txt"
awk '
  NR == FNR {
    if ($0 !~ /^[[:space:]]*(#|$)/) allowed[$1] = 1
    next
  }
  !($1 in allowed) { print $1 }
' "$ALLOWLIST" "$work_dir/all-failed.txt" >"$work_dir/current.txt"

if [[ -r "$STATE" ]]; then
  LC_ALL=C sort -u "$STATE" >"$work_dir/previous.txt"
else
  : >"$work_dir/previous.txt"
fi
comm -13 "$work_dir/previous.txt" "$work_dir/current.txt" \
  >"$work_dir/new.txt"

if [[ -s "$work_dir/new.txt" ]]; then
  message='⚠️ Neue fehlgeschlagene systemd-User-Units:'
  while IFS= read -r unit; do
    message+=$'\n'"• \`$unit\`"
  done <"$work_dir/new.txt"
  printf '%s\n' "$message" | python3 "$NOTIFY" --channel "$CHANNEL" --stdin
fi

cp "$work_dir/current.txt" "$next_state"
mv -f "$next_state" "$STATE"
