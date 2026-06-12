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

echo "[deploy] lint gate (fork-eigener Code: src/control + vite.config.ts + e2e) ..."
# eslint ist seit 2026-06-12 offizielles Gate für den fork-eigenen Frontend-
# Code: der Verifier prüft Lint, die Worker-Gates taten es nicht — diese
# Asymmetrie hat t_748896f7 in einen Dauerblock geschickt. Scope bewusst NUR
# fork-eigene Pfade; Upstream-Dateien (src/App.tsx, src/components/…) bleiben
# diff-relativ beim Verifier (Sync-Disziplin, keine Legacy-Aufräum-Pflicht).
( cd web && npm run lint:control )

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
# Deeper-than-liveness check: a real, token-gated API route must return valid
# data after restart — proving the backend + auth-token path work end-to-end,
# not just that the SPA HTML loads. The proposals API lives under /api/; the
# bare /autoresearch/proposals path falls through to the SPA catch-all and
# returns HTML (which used to fail this check on *every* deploy). Loopback runs
# non-gated, so the SPA injects the ephemeral session token into the /control
# HTML as window.__HERMES_SESSION_TOKEN__; extract it and pass it as the
# X-Hermes-Session-Token header (a bare curl to the /api/ route 401s).
TOKEN=$(curl -fsS --max-time 8 http://127.0.0.1:9119/control | grep -oP 'window\.__HERMES_SESSION_TOKEN__="\K[^"]+' | head -n1 || true)
if [ -z "$TOKEN" ]; then
  echo "[deploy] FAILED — could not extract session token from /control HTML (gated mode or service down?)"
  exit 1
fi
PAYLOAD_OK=$(curl -fsS --max-time 8 -H "X-Hermes-Session-Token: $TOKEN" http://127.0.0.1:9119/api/autoresearch/proposals | python3 -c 'import json,sys; d=json.load(sys.stdin); c=d.get("count"); o=d.get("open_count"); assert isinstance(c, int) and isinstance(o, int); print(f"count={c} open_count={o}")' 2>/tmp/hermes-deploy-payload.err || true)
if [ -z "$PAYLOAD_OK" ]; then
  echo "[deploy] FAILED — /api/autoresearch/proposals did not return valid count/open_count JSON"
  cat /tmp/hermes-deploy-payload.err || true
  exit 1
fi

echo "[deploy] payload: $PAYLOAD_OK"
if [ "$SMOKE" = "1" ]; then
  mkdir -p "$HOME/.hermes/reports"
  shot="$HOME/.hermes/reports/control-smoke-$(date +%Y%m%d-%H%M%S).png"
  smoke_tmp_dir="$HOME/snap/chromium/common"
  mkdir -p "$smoke_tmp_dir"
  tmp_shot="$smoke_tmp_dir/$(basename "$shot")"
  rm -f "$tmp_shot"
  chromium --headless=new --no-sandbox --disable-gpu --disable-background-timer-throttling --disable-renderer-backgrounding --virtual-time-budget=5000 --window-size=390,844 --screenshot="$tmp_shot" http://127.0.0.1:9119/control/autoresearch >/tmp/hermes-control-smoke.log 2>&1 || { cat /tmp/hermes-control-smoke.log; exit 1; }
  for i in $(seq 1 20); do
    if [ -s "$tmp_shot" ]; then break; fi
    sleep 0.2
  done
  test -s "$tmp_shot" || { echo "[deploy] FAILED — smoke screenshot is empty: $tmp_shot"; cat /tmp/hermes-control-smoke.log || true; exit 1; }
  mv "$tmp_shot" "$shot"
  echo "[deploy] smoke screenshot: $shot"
fi

if [ "$LB" = "200" ] && [ "$TN" = "200" ]; then
  echo "[deploy] OK — live + mobile reachable"
else
  echo "[deploy] FAILED — check: systemctl --user status hermes-dashboard.service"
  exit 1
fi
