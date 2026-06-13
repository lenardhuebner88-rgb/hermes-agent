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

# wait for it to come back — poll the public liveness route (real backend code,
# no auth in any mode) rather than the SPA, so we wait on the Python process.
for i in $(seq 1 15); do
  if curl -s -o /dev/null --max-time 2 http://127.0.0.1:9119/api/status; then break; fi
  sleep 1
done

# SPA reachability + DNS-rebinding host-guard. Accept 200 (loopback/--insecure
# mode → token-gated SPA) OR 302 (gated/OAuth mode → SPA redirects to /login;
# the route IS served and the auth gate is live). Prod binds 0.0.0.0, so even
# loopback is gated → both legitimately 302. 403 (host guard blocked the tailnet
# Host), 000 (down) or 5xx are unhealthy. --max-redirs 0: read the raw code.
LB=$(curl -s -o /dev/null -w '%{http_code}' --max-time 8 --max-redirs 0 http://127.0.0.1:9119/control || echo 000)
TN=$(curl -s -o /dev/null -w '%{http_code}' --max-time 8 --max-redirs 0 -H "Host: huebners.tail50819a.ts.net" http://127.0.0.1:9119/control || echo 000)
echo "[deploy] health: loopback=$LB tailnet_guard=$TN  service=$(systemctl --user is-active hermes-dashboard.service)"
# Deeper-than-liveness check that works in BOTH auth modes: /api/status is in
# PUBLIC_API_PATHS (bypasses the loopback token gate AND the OAuth cookie gate),
# yet its handler runs real backend code — check_config_version, get_running_pid,
# gateway config read — so a 200 + valid JSON proves the Python backend (not just
# the static SPA) is alive after restart. The old probe scraped an ephemeral
# session token from /control HTML, which is NOT injected in gated mode → it
# false-failed on every prod deploy. (truth = API payload, mode-agnostic)
PAYLOAD_OK=$(curl -fsS --max-time 8 http://127.0.0.1:9119/api/status | python3 -c 'import json,sys; d=json.load(sys.stdin); assert isinstance(d, dict) and d.get("version"); v=d.get("version"); g=d.get("gateway_running"); c=d.get("config_version"); print(f"version={v} gateway_running={g} config_version={c}")' 2>/tmp/hermes-deploy-payload.err || true)
if [ -z "$PAYLOAD_OK" ]; then
  echo "[deploy] FAILED — /api/status did not return valid JSON (backend down?)"
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

# 200 (non-gated) or 302 (gated → /login) both mean the SPA + auth gate are up.
if { [ "$LB" = "200" ] || [ "$LB" = "302" ]; } && { [ "$TN" = "200" ] || [ "$TN" = "302" ]; }; then
  echo "[deploy] OK — live + mobile reachable"
else
  echo "[deploy] FAILED — loopback=$LB tailnet_guard=$TN (want 200 or 302); check: systemctl --user status hermes-dashboard.service"
  exit 1
fi
