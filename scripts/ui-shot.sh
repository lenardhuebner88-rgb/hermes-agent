#!/usr/bin/env bash
# ui-shot.sh — Screenshot + Konsolen-Check einer URL fuer ALLE Agententypen.
#
# Duenner Wrapper um ./node_modules/.bin/agent-browser (Playwright-basiert,
# liegt im Repo — KEIN Neucode, keine neuen Dependencies).
# Subcommand-Syntax verifiziert 2026-07-17 gegen `agent-browser --help`:
#   --session <name> (global, vor dem Subcommand erlaubt)
#   --state <path>   laedt Storage-State (cookies+storage) — NUR LESEN
#   set viewport <w> <h> · open <url> · wait --load networkidle · wait <ms>
#   screenshot <path> · errors [--json] · console · close
#
# SICHERHEITSGRENZE (hart, fuer Worker-Nutzung):
#   Dieses Script nutzt AUSSCHLIESSLICH navigate / wait / screenshot /
#   errors / console. KEIN click, KEIN type, KEIN eval, KEIN state save.
#   Der Storage-State unter --auth wird nur geladen — sein Inhalt wird
#   NIEMALS ausgegeben, kopiert oder geloggt.
#
# Usage:
#   scripts/ui-shot.sh <url> [--out <pfad.png>] [--viewport 1440x900|390x844]
#                      [--auth hermes-dashboard] [--console]
#
#   --out          Ziel-PNG (default /tmp/ui-shot-<timestamp>.png)
#   --viewport     WxH (default 1440x900; 390x844 = Mobile)
#   --auth         einziger erlaubter Wert: hermes-dashboard — laedt
#                  /home/piet/.hermes/agent-browser/hermes-dashboard-storage-state.json
#                  (nur noetig fuer NICHT-loopback/gated Dashboards; Loopback-
#                  Previews aus preview-realdata.sh brauchen kein --auth)
#   --console      nach dem Screenshot `errors` + `console` des Browsers dumps
#
# OUTPUT-KONTRAKT (letzte zwei Zeilen, maschinenlesbar):
#   SHOT=<pfad.png>
#   CONSOLE_ERRORS=<n>     (Anzahl uncaught JS page-errors aus `errors --json`)
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "$SCRIPT_DIR/.." && pwd)"
AB="$REPO_ROOT/node_modules/.bin/agent-browser"
[ -x "$AB" ] || { echo "[ui-shot] FEHLER: $AB nicht ausfuehrbar (Repo-Root node_modules?)" >&2; exit 1; }

URL=""
OUT="/tmp/ui-shot-$(date +%Y%m%d-%H%M%S).png"
VIEWPORT="1440x900"
AUTH=""
CONSOLE=0

usage() { sed -n '2,40p' "${BASH_SOURCE[0]}"; exit "${1:-0}"; }

while [ $# -gt 0 ]; do
  case "$1" in
    --out) OUT="${2:?--out braucht einen Pfad}"; shift 2 ;;
    --viewport) VIEWPORT="${2:?--viewport braucht WxH}"; shift 2 ;;
    --auth) AUTH="${2:?--auth braucht einen Namen}"; shift 2 ;;
    --console) CONSOLE=1; shift ;;
    -h|--help) usage 0 ;;
    -*) echo "[ui-shot] FEHLER: unbekanntes Argument: $1" >&2; usage 1 ;;
    *) if [ -z "$URL" ]; then URL="$1"; else echo "[ui-shot] FEHLER: genau eine URL erlaubt" >&2; usage 1; fi; shift ;;
  esac
done
[ -n "$URL" ] || { echo "[ui-shot] FEHLER: URL fehlt" >&2; usage 1; }
[[ "$VIEWPORT" =~ ^[0-9]+x[0-9]+$ ]] || { echo "[ui-shot] FEHLER: --viewport muss WxH sein (z.B. 1440x900)" >&2; exit 1; }
VW="${VIEWPORT%x*}"; VH="${VIEWPORT#*x}"

STATE_ARGS=()
if [ -n "$AUTH" ]; then
  [ "$AUTH" = "hermes-dashboard" ] || { echo "[ui-shot] FEHLER: --auth kennt nur 'hermes-dashboard'" >&2; exit 1; }
  STATE="/home/piet/.hermes/agent-browser/hermes-dashboard-storage-state.json"
  [ -f "$STATE" ] || { echo "[ui-shot] FEHLER: Storage-State fehlt: $STATE" >&2; exit 1; }
  STATE_ARGS=(--state "$STATE")  # nur laden — Inhalt NIE ausgeben/kopieren/loggen
fi

SESSION="ui-shot-$$"
# --no-sandbox via ENV (nicht per --args-Flag): auf diesem Host (Ubuntu +
# AppArmor userns-Restriction) startet Chromium sonst gar nicht ("No usable
# sandbox") — gleiche Praxis wie scripts/deploy_dashboard.sh (--smoke). Der
# Daemon liest AGENT_BROWSER_ARGS nur beim Launch, so gibt es kein
# "--args ignored: daemon already running"-Rauschen bei Folgekommandos.
# Abwaegung: Sandbox-Aufweichung fuer lokalen Shot-Wrapper akzeptabel;
# trotzdem NUR vertrauenswuerdige/loopback URLs abfotografieren.
export AGENT_BROWSER_ARGS="--no-sandbox"
ab() { "$AB" --session "$SESSION" ${STATE_ARGS[@]+"${STATE_ARGS[@]}"} "$@"; }

cleanup() { "$AB" --session "$SESSION" close >/dev/null 2>&1 || true; }
trap cleanup EXIT

ab set viewport "$VW" "$VH" >/dev/null
ab open "$URL" >/dev/null
if ! ab wait --load networkidle >/dev/null 2>&1; then
  echo "[ui-shot] WARN: networkidle nicht erreicht (laufendes Polling/WS?) — weiter mit festem Puffer" >&2
fi
ab wait 1500 >/dev/null  # Spinner-Puffer: SPA-Hydration + erste API-Fills abwarten
ab screenshot "$OUT" >/dev/null
[ -s "$OUT" ] || { echo "[ui-shot] FEHLER: Screenshot leer/fehlend: $OUT" >&2; exit 1; }

# Uncaught page-errors zaehlen (JSON primaer, Text-Fallback).
ERR_JSON="$(ab errors --json 2>/dev/null || true)"
CONSOLE_ERRORS="$(printf '%s' "$ERR_JSON" | python3 -c '
import json, sys
raw = sys.stdin.read()
try:
    d = json.loads(raw)
    print(len(d) if isinstance(d, list) else len(d.get("errors", [])))
except Exception:
    print(-1)
' 2>/dev/null || echo -1)"
if [ "$CONSOLE_ERRORS" = "-1" ]; then
  CONSOLE_ERRORS="$(ab errors 2>/dev/null | grep -cve '^\s*$' -e '^No page errors' || true)"
fi

if [ "$CONSOLE" = "1" ]; then
  echo "--- errors ---"
  ab errors 2>/dev/null || true
  echo "--- console ---"
  ab console 2>/dev/null || true
fi

echo "SHOT=$OUT"
echo "CONSOLE_ERRORS=$CONSOLE_ERRORS"
