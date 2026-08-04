#!/usr/bin/env bash
# Real-phone helper retained for operator use. It is intentionally not used by automated runs.
set -euo pipefail

PHONE_HOST="${HERMES_PHONE_HOST:-s26-von-lenard}"
PHONE_IP="${HERMES_PHONE_IP:-100.99.233.123}"
PORT_FILE="${HERMES_PHONE_PORT_FILE:-$HOME/.android/hermes-phone-port}"
export ANDROID_SDK_ROOT="${ANDROID_SDK_ROOT:-/home/piet/Android/Sdk}"
export PATH="$ANDROID_SDK_ROOT/platform-tools:$PATH"

die() { echo "phone.sh: $*" >&2; exit 1; }
reachable() {
  timeout 8 tailscale ping -c 1 "$PHONE_IP" >/dev/null 2>&1 \
    || die "$PHONE_HOST ($PHONE_IP) is not reachable over Tailscale"
}
cmd_pair() {
  local port="${1:?usage: phone.sh pair <pair-port> <code>}" code="${2:?missing 6-digit code}"
  reachable
  adb pair "${PHONE_IP}:${port}" "$code"
}
cmd_discover() {
  reachable
  echo "phone.sh: scanning ${PHONE_IP} for the wireless-debugging port ..." >&2
  local port
  port="$(nmap -Pn -p 30000-49999 --open -T4 "$PHONE_IP" 2>/dev/null | awk -F/ '/^[0-9]+\/tcp/{print $1}' | head -1)"
  [ -n "${port:-}" ] || die "no open port found"
  echo "$port" | tee "$PORT_FILE"
}
cmd_status() { adb devices -l | grep -E "^${PHONE_IP}" || echo "phone.sh: $PHONE_HOST not connected"; }
cmd_connect() {
  local port="${1:-}"
  [ -n "$port" ] || port="$(cmd_discover)"
  reachable
  adb connect "${PHONE_IP}:${port}"
  echo "$port" >"$PORT_FILE"
  cmd_status
}
case "${1:-status}" in
  pair) shift; cmd_pair "$@" ;;
  connect) shift; cmd_connect "${1:-}" ;;
  discover) cmd_discover ;;
  status) cmd_status ;;
  disconnect) adb disconnect "${PHONE_IP}" || true ;;
  serial) echo "${PHONE_IP}:$(cat "$PORT_FILE" 2>/dev/null || echo '?')" ;;
  *) die "usage: $0 {pair|connect|discover|status|disconnect|serial}" ;;
esac
