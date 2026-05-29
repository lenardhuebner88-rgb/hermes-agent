#!/usr/bin/env bash
# One-command Hermes Control deploy: rebuild the frontend bundle and restart the
# durable dashboard service. Replaces the old "manual --skip-build process +
# remember to npm run build" dance (C1 deploy-hygiene).
#
# The dashboard serves web/dist (-> hermes_cli/web_dist) statically, so a
# rebuild is what makes frontend changes live; the systemd restart picks up any
# backend Python changes and keeps the service durable (Restart=always, survives
# reboot). Tailnet host-guard stays intact via the unit's HERMES_DASHBOARD_PUBLIC_URL.
set -euo pipefail
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
if [ "$LB" = "200" ] && [ "$TN" = "200" ]; then
  echo "[deploy] OK — live + mobile reachable"
else
  echo "[deploy] FAILED — check: systemctl --user status hermes-dashboard.service"
  exit 1
fi
