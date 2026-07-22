#!/usr/bin/env bash
# preview-realdata.sh — Hermes /control Dashboard mit ECHTEN Daten in einem
# Wegwerf-Seed-Home starten und eine token-faehige Preview-URL ausgeben.
#
# Fuer alle Agententypen (Kimi/Codex/Claude/Grok). Ersetzt die manuelle
# 3-Iterations-Prozedur (401-Falle: der Prozess-Token steckt im injizierten
# SPA-HTML, nicht in der Config).
#
# STARTKOMMANDO (verifiziert 2026-07-17 gegen die systemd-Unit
# ~/.config/systemd/user/hermes-dashboard.service, ExecStart):
#     venv/bin/python -m hermes_cli.main dashboard --host 127.0.0.1 \
#         --port 9119 --no-open --skip-build --insecure
# Dieses Script nutzt dasselbe Kommando mit drei dokumentierten Abweichungen:
#   * --insecure weggelassen — verifizierter Legacy-NO-OP
#     (should_require_auth() in hermes_cli/web_server.py: Loopback = kein Gate).
#   * --isolated hinzugefuegt — verhindert den Unified-Launch-Re-Exec auf das
#     Machine-Dashboard (hermes_cli/main.py, cmd_dashboard). Schutz des LIVE
#     Servers auf 9119.
#   * HERMES_HOME=<seed-home> + PYTHONPATH=<repo-root>: Daten aus dem Seed,
#     Code aus DIESEM Checkout (Worktree hat keine eigene venv; der Python-
#     Interpreter kommt per Default aus der Live-vENV und liefert nur die
#     Dependencies — ueberschreibbar via HERMES_PREVIEW_PYTHON).
#
# SICHERHEIT:
#   * Schreibt NIE nach ~/.hermes. DBs werden read-only geoeffnet
#     (sqlite3 URI mode=ro) und per Backup-API kopiert — NIE cp auf WAL-DBs.
#   * Der Server bindet nur 127.0.0.1. Der Session-Token ist prozess-
#     ephemer und gilt nur fuer diesen Preview-Prozess.
#   * Health-Pfad ist /api/status (PUBLIC_API_PATHS, wie deploy_dashboard.sh);
#     /api/health-status ist token-gated und daher ungeeignet.
#
# TOKEN: Bei Loopback-Bind injiziert der Server window.__HERMES_SESSION_TOKEN__
# in jede SPA-Index-Auslieferung (web_server.py, _serve_index). Die SPA
# braucht daher KEIN ?token= in der URL — PREVIEW_URL reicht fuer Browser/
# ui-shot.sh. PREVIEW_TOKEN wird fuer curl/API-Aufrufe zusaetzlich ausgegeben.
#
# Usage:
#   scripts/preview-realdata.sh [--home <seed-home>] [--port <p>] [--scenario realdata|terminal_bridge] [--route <route>] [--no-build] [--keep]
#
#   --home <dir>   Seed-Home (default: mktemp -d /tmp/hermes-preview-seed.XXXX).
#                  Ein selbst angegebenes Home wird NIEMALS geloescht.
#   --port <p>     Port (default: erster freier Port 9100-9199, 9119 ausgenommen).
#   --no-build     Bestehendes hermes_cli/web_dist serven statt web/ zu bauen.
#   --keep         Server nach Script-Ende laufen lassen UND Seed-Home behalten
#                  (Stop: kill -INT <PREVIEW_PID>). Default: Exit-Trap killt den
#                  Server (SIGINT, wie die systemd-Unit) und loescht ein von
#                  diesem Script erstelltes Seed-Home.
#
# OUTPUT-KONTRAKT (letzte drei Zeilen, maschinenlesbar):
#   PREVIEW_HOME=<seed-home>
#   PREVIEW_PID=<pid>
#   PREVIEW_URL=http://127.0.0.1:<port>/control
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "$SCRIPT_DIR/.." && pwd)"
SRC_HOME="${HERMES_SRC_HOME:-${HOME:-/tmp}/.hermes}"

SEED_HOME=""
PORT=""
NO_BUILD=0
KEEP=0
SCENARIO=realdata
ROUTE=control
SEED_FIXTURE_DB=0

usage() { sed -n '2,60p' "${BASH_SOURCE[0]}"; exit "${1:-0}"; }

while [ $# -gt 0 ]; do
  case "$1" in
    --home) SEED_HOME="${2:?--home braucht ein Verzeichnis}"; shift 2 ;;
    --port) PORT="${2:?--port braucht eine Nummer}"; shift 2 ;;
    --scenario) SCENARIO="${2:?--scenario braucht einen Namen}"; shift 2 ;;
    --route) ROUTE="${2:?--route braucht einen Namen}"; shift 2 ;;
    --no-build) NO_BUILD=1; shift ;;
    --keep) KEEP=1; shift ;;
    -h|--help) usage 0 ;;
    *) echo "[preview] FEHLER: unbekanntes Argument: $1" >&2; usage 1 ;;
  esac
done

log() { echo "[preview] $*"; }
die() { echo "[preview] FEHLER: $*" >&2; exit 1; }

# ---------------------------------------------------------------- seed home
CREATED_HOME=0
if [ -z "$SEED_HOME" ]; then
  SEED_HOME="$(mktemp -d /tmp/hermes-preview-seed.XXXXXX)"
  CREATED_HOME=1
fi
mkdir -p "$SEED_HOME"
chmod 700 "$SEED_HOME" || true

# ------------------------------------------------------------------- port
port_in_use() { (echo > "/dev/tcp/127.0.0.1/$1") 2>/dev/null; }
if [ -z "$PORT" ]; then
  for p in $(seq 9100 9199); do
    [ "$p" = "9119" ] && continue  # Live-Dashboard
    if ! port_in_use "$p"; then PORT="$p"; break; fi
  done
  [ -n "$PORT" ] || die "kein freier Port in 9100-9199 (9119 ausgenommen)"
fi
if port_in_use "$PORT"; then die "Port $PORT ist bereits belegt"; fi

# ------------------------------------------------------------- seed: DBs
# sqlite3-Backup-API auf read-only URI — sicher gegen WAL-Live-DBs.
copy_db() {
  local name="$1"
  local src="$SRC_HOME/$name"
  [ -f "$src" ] || { log "  $name: nicht vorhanden, uebersprungen"; return 0; }
  log "  $name: Backup-Kopie ($(du -h "$src" | cut -f1)) ..."
  python3 - "$src" "$SEED_HOME/$name" <<'PYEOF'
import sqlite3, sys
src, dst = sys.argv[1], sys.argv[2]
s = sqlite3.connect(f"file:{src}?mode=ro", uri=True)
d = sqlite3.connect(dst)
s.backup(d)
d.close(); s.close()
PYEOF
}

if [ "$SCENARIO" = "terminal_bridge" ]; then
  # Fixture mode is intentionally disconnected from every live Hermes path.
  # The branch server initializes this empty board in its own sandbox.
  SEED_FIXTURE_DB=1
  : >"$SEED_HOME/kanban.db"
  chmod 600 "$SEED_HOME/kanban.db"
  mkdir -p "$SEED_HOME/tmux"
  chmod 700 "$SEED_HOME/tmux"
  TERMINAL_RUN_ID="ui-terminal-bridge"
  TERMINAL_RUN_DIR="$SEED_HOME/terminal-runs/$TERMINAL_RUN_ID"
  FIXTURE_SCRIPT="$SEED_HOME/terminal-bridge-fixture.sh"
  mkdir -p "$TERMINAL_RUN_DIR"
  printf '%s\n' \
    '#!/usr/bin/env bash' \
    'printf "\033[2J\033[H"' \
    'printf "Hermes terminal_bridge fixture\n"' \
    'printf "Handoff: available\n"' \
    'printf "Candidate status: HELD (record-only, no submit)\n"' \
    'while :; do sleep 60; done' >"$FIXTURE_SCRIPT"
  chmod 700 "$FIXTURE_SCRIPT"
  TMUX_TMPDIR="$SEED_HOME/tmux" tmux new-session -d -s work -n codex "$FIXTURE_SCRIPT"
  TMUX_TMPDIR="$SEED_HOME/tmux" tmux set-option -w -t work:codex @hermes_terminal_run_id "$TERMINAL_RUN_ID"
  TMUX_TMPDIR="$SEED_HOME/tmux" tmux set-option -w -t work:codex @hermes_kind agent
  TMUX_TMPDIR="$SEED_HOME/tmux" tmux set-option -w -t work:codex @hermes_workdir "$REPO_ROOT"
  python3 - "$TERMINAL_RUN_DIR/manifest.json" "$REPO_ROOT" "$(git -C "$REPO_ROOT" rev-parse HEAD)" "$(git -C "$REPO_ROOT" branch --show-current)" <<'PYFIXTURE'
import json
import os
import sys

path, root, head, branch = sys.argv[1:]
payload = {
    "terminal_run_id": "ui-terminal-bridge",
    "run_id": "ui-terminal-bridge",
    "agent_kind": "codex",
    "start_mode": "isolated_write",
    "context_profile": "full",
    "cwd": root,
    "repo_root": root,
    "base_sha": head,
    "worktree_path": root,
    "worktree_branch": branch,
    "action": "fresh",
    "tmux_session": "work",
    "tmux_window": "codex",
    "candidate_status": "held",
}
with open(path, "w", encoding="utf-8") as handle:
    json.dump(payload, handle, sort_keys=True)
    handle.write("\n")
os.chmod(path, 0o600)
PYFIXTURE
  log "Terminal-Bridge-Fixture: isoliertes Home/Board/tmux; held candidate"
else
  [ "$SCENARIO" = "realdata" ] || die "unbekanntes Scenario: $SCENARIO"
  log "Seed-Home: $SEED_HOME (Quelle: $SRC_HOME, read-only)"
  copy_db state.db
  copy_db kanban.db
  copy_db projects.db

  # Live-derived config is permitted only in explicit realdata mode.
  for f in projects.yaml profile.yaml config.yaml; do
    if [ -f "$SRC_HOME/$f" ]; then cp -p "$SRC_HOME/$f" "$SEED_HOME/$f"; fi
  done
fi
mkdir -p "$SEED_HOME/tmux"
chmod 700 "$SEED_HOME/tmux"

# ------------------------------------------------------------------ build
WEB_DIST="$SEED_HOME/web-dist"
if [ "$NO_BUILD" = "0" ]; then
  [ -d "$REPO_ROOT/web/node_modules" ] || die \
    "web/node_modules fehlt — erst 'cd web && npm ci'"
  log "Baue Branch-Frontend isoliert nach $WEB_DIST ..."
  ( cd "$REPO_ROOT/web" && HERMES_WEB_DIST="$WEB_DIST" npm run build )
else
  WEB_DIST="${HERMES_WEB_DIST:-$REPO_ROOT/hermes_cli/web_dist}"
  [ -f "$WEB_DIST/index.html" ] || die \
    "--no-build gesetzt, aber $WEB_DIST/index.html fehlt"
  log "Nutze bestehendes HERMES_WEB_DIST (--no-build): $WEB_DIST"
fi

# ----------------------------------------------------------------- python
PY="${HERMES_PREVIEW_PYTHON:-}"
if [ -z "$PY" ]; then
  if [ -x "$REPO_ROOT/venv/bin/python" ]; then PY="$REPO_ROOT/venv/bin/python"
  elif [ -x "$REPO_ROOT/.venv/bin/python" ]; then PY="$REPO_ROOT/.venv/bin/python"
  else PY="/home/piet/.hermes/hermes-agent/venv/bin/python"; fi
fi
[ -x "$PY" ] || die "Python nicht gefunden: $PY (HERMES_PREVIEW_PYTHON setzen)"
"$PY" -c "import fastapi, uvicorn" 2>/dev/null || die \
  "$PY hat keine Web-Deps (fastapi/uvicorn). HERMES_PREVIEW_PYTHON auf eine Hermes-venv zeigen lassen."

# ----------------------------------------------------------------- launch
SERVER_PID=""
READY_FOR_OWNER=0
cleanup() {
  if [ "$KEEP" = "0" ] || [ "$READY_FOR_OWNER" = "0" ]; then
    if [ -n "$SERVER_PID" ] && kill -0 "$SERVER_PID" 2>/dev/null; then
      kill -INT "$SERVER_PID" 2>/dev/null || true  # Unit-Kontrakt: SIGINT, nicht SIGTERM
      for _ in $(seq 1 20); do
        kill -0 "$SERVER_PID" 2>/dev/null || break
        sleep 0.5
      done
      kill -KILL "$SERVER_PID" 2>/dev/null || true
    fi
    if [ "$SCENARIO" = "terminal_bridge" ]; then
      TMUX_TMPDIR="$SEED_HOME/tmux" tmux kill-server 2>/dev/null || true
    fi
    if [ "$CREATED_HOME" = "1" ]; then rm -rf "$SEED_HOME"; fi
  fi
}
trap cleanup EXIT

log "Starte Dashboard auf 127.0.0.1:$PORT (HERMES_HOME=$SEED_HOME) ..."
# exec: die Subshell wird durch den Server ersetzt → SERVER_PID ist der
# Python-Prozess selbst (sauberes kill -INT). Redirection der GANZEN Subshell +
# </dev/null: kein geerbter stdout/stdin-fd haelt die Pipe des Aufrufers offen
# (sonst haengt $(preview-realdata.sh --keep) beim Command-Substitution).
( cd "$REPO_ROOT" && \
  exec env -u HERMES_KANBAN_TASK -u HERMES_KANBAN_WORKSPACE -u HERMES_KANBAN_BOARD \
      -u HERMES_KANBAN_BRANCH -u HERMES_KANBAN_WORKTREE \
      HERMES_HOME="$SEED_HOME" HERMES_KANBAN_DB="$SEED_HOME/kanban.db" \
      HERMES_SANDBOX_MODE=1 HERMES_WEB_DIST="$WEB_DIST" \
      TMUX_TMPDIR="$SEED_HOME/tmux" PYTHONPATH="$REPO_ROOT" \
  "$PY" -m hermes_cli.main dashboard \
    --host 127.0.0.1 --port "$PORT" --no-open --skip-build --isolated \
) >"$SEED_HOME/server.log" 2>&1 </dev/null &
SERVER_PID=$!

# ----------------------------------------------------------------- health
READY=0
for _ in $(seq 1 60); do
  if curl -fsS --max-time 2 "http://127.0.0.1:$PORT/api/status" >/dev/null 2>&1; then
    READY=1; break
  fi
  kill -0 "$SERVER_PID" 2>/dev/null || break
  sleep 1
done
if [ "$READY" != "1" ]; then
  echo "[preview] FEHLER: Server nicht bereit nach 60s. Letzte Log-Zeilen:" >&2
  tail -20 "$SEED_HOME/server.log" >&2 || true
  exit 1
fi

# ------------------------------------------------------------------ token
TOKEN="$(curl -fsS --max-time 5 "http://127.0.0.1:$PORT/control" \
  | grep -o 'window\.__HERMES_SESSION_TOKEN__="[^"]*"' | head -1 | cut -d'"' -f2 || true)"
[ -n "$TOKEN" ] || die "Token-Injektion nicht gefunden (gated mode? server.log pruefen)"

log "bereit. Die SPA erhaelt den Token automatisch per HTML-Injektion;"
log "PREVIEW_TOKEN nur fuer curl/API noetig (Header siehe web/src/lib/api.ts)."
if [ "$KEEP" = "1" ]; then
  READY_FOR_OWNER=1
  log "--keep: Server laeuft weiter. Stop: kill -INT $SERVER_PID ; Seed-Home bleibt: $SEED_HOME"
else
  log "ohne --keep stoppt der Exit-Trap den Server jetzt beim Verlassen."
fi
echo "PREVIEW_TOKEN=$TOKEN"
echo "PREVIEW_HOME=$SEED_HOME"
echo "PREVIEW_PID=$SERVER_PID"
echo "PREVIEW_URL=http://127.0.0.1:$PORT/control"
echo "PREVIEW_WEB_DIST=$WEB_DIST"
