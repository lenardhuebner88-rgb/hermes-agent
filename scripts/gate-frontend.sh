#!/usr/bin/env bash
# gate-frontend.sh [--skip-build] — das komplette Frontend-Gate in EINEM Aufruf
# mit vertrauenswürdigem Exit-Code.
#
# Existiert, weil freihändige Pipe-Ketten Exit-Codes schlucken: `npx vitest run | tail`
# lieferte 2026-07-01 GATE_EXIT=0 bei 3 roten Tests (ohne pipefail zählt der Exit von
# tail, nicht der von vitest). Dieses Script pipet nichts, bricht beim ersten roten
# Schritt ab (set -e) und sein Exit-Code ist die Wahrheit. Output lang? Den Aufruf
# an einen log-analyst-Subagenten geben statt selbst zu pipen.
#
# Schritte: lint:control → tsc -b --noEmit → vitest run → build
# (tsc MUSS -b sein: web/ ist eine Solution-Config mit files:[], bare `tsc --noEmit`
# prüft nichts — belegte Falle 2026-06-16.)
#
# --skip-build: prüfen ohne zu bauen. `npm run build` schreibt direkt nach
# hermes_cli/web_dist (= de-facto Asset-Deploy des parallel editierten Live-Checkouts).
# Nur bauen, wenn der aktuelle Working-Tree-Stand auch serviert werden soll —
# bei fremdem dirty web/-Stand (git status prüfen!) --skip-build nutzen.
set -euo pipefail

cd "$(dirname "${BASH_SOURCE[0]}")/../web"

skip_build=0
if [[ "${1:-}" == "--skip-build" ]]; then
  skip_build=1
elif [[ $# -gt 0 ]]; then
  echo "usage: $0 [--skip-build]" >&2
  exit 2
fi

step() { printf '\n=== GATE: %s ===\n' "$1"; }

step "npm run lint:control"
npm run lint:control

step "npx tsc -b --noEmit"
npx tsc -b --noEmit

step "npx vitest run"
npx vitest run

if [[ $skip_build -eq 1 ]]; then
  step "build ÜBERSPRUNGEN (--skip-build)"
else
  step "npm run build"
  npm run build
fi

printf '\n=== FRONTEND-GATE GRÜN (exit 0) ===\n'
