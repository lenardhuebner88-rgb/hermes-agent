#!/usr/bin/env bash
# One-command Hermes Control deploy: rebuild the frontend bundle and restart the
# durable dashboard service. Replaces the old "manual --skip-build process +
# remember to npm run build" dance (C1 deploy-hygiene).
#
# The dashboard serves web/dist (-> hermes_cli/web_dist) statically, so a
# rebuild is what makes frontend changes live; the systemd restart picks up any
# backend Python changes and keeps the service durable (Restart=always, survives
# reboot). Tailnet host-guard stays intact via the unit's HERMES_DASHBOARD_PUBLIC_URL.
# Optional: pass --smoke to capture a headless Chromium screenshot under ~/.hermes/reports/.
set -euo pipefail
SMOKE=0
if [ "${1:-}" = "--smoke" ]; then
  SMOKE=1
fi
cd "$(dirname "$0")/.."

echo "[deploy] building frontend bundle (web/) ..."
( cd web && npm run build )

echo "[deploy] restarting hermes-dashboard.service ..."
systemctl --user restart hermes-dashboard.service

# wait for it to come back
for i in $(seq 1 15); do
  if curl -s -o /dev/null --max-time 2 http://127.0.0.1:9119/control/autoresearch; then break; fi
  sleep 1
done

LB=$(curl -s -o /dev/null -w '%{http_code}' --max-time 8 http://127.0.0.1:9119/control/autoresearch || echo 000)
TN=$(curl -s -o /dev/null -w '%{http_code}' --max-time 8 -H "Host: huebners.tail50819a.ts.net" http://127.0.0.1:9119/control/autoresearch || echo 000)
echo "[deploy] health: loopback=$LB tailnet_guard=$TN  service=$(systemctl --user is-active hermes-dashboard.service)"
PAYLOAD_OK=$(curl -fsS --max-time 8 http://127.0.0.1:9119/autoresearch/proposals | python3 -c 'import json,sys; d=json.load(sys.stdin); assert isinstance(d.get("open_count"), int) and isinstance(d.get("count"), int); print(f"count={d.get(chr(99)+chr(111)+chr(117)+chr(110)+chr(116))} open_count={d.get(chr(111)+chr(112)+chr(101)+chr(110)+chr(95)+chr(99)+chr(111)+chr(117)+chr(110)+chr(116))}")' 2>/tmp/hermes-deploy-payload.err || true)
if [ -z "$PAYLOAD_OK" ]; then
  echo "[deploy] FAILED — /autoresearch/proposals did not return valid count/open_count JSON"
  cat /tmp/hermes-deploy-payload.err || true
  exit 1
fi

echo "[deploy] payload: $PAYLOAD_OK"
if [ "$SMOKE" = "1" ]; then
  mkdir -p "$HOME/.hermes/reports"
  shot="$HOME/.hermes/reports/control-smoke-$(date +%Y%m%d-%H%M%S).png"
  chromium --headless=new --no-sandbox --disable-gpu --disable-background-timer-throttling --disable-renderer-backgrounding --virtual-time-budget=5000 --window-size=390,844 --screenshot="$shot" http://127.0.0.1:9119/control/autoresearch >/tmp/hermes-control-smoke.log 2>&1 || { cat /tmp/hermes-control-smoke.log; exit 1; }
  test -s "$shot" || { echo "[deploy] FAILED — smoke screenshot is empty: $shot"; exit 1; }
  echo "[deploy] smoke screenshot: $shot"
fi

if [ "$LB" = "200" ] && [ "$TN" = "200" ]; then
  echo "[deploy] OK — live + mobile reachable"
else
  echo "[deploy] FAILED — check: systemctl --user status hermes-dashboard.service"
  exit 1
fi
