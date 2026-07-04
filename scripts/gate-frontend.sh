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

script_dir="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$script_dir/../web"

skip_build=0
if [[ "${1:-}" == "--skip-build" ]]; then
  skip_build=1
elif [[ $# -gt 0 ]]; then
  echo "usage: $0 [--skip-build]" >&2
  exit 2
fi

step() { printf '\n=== GATE: %s ===\n' "$1"; }

step "design-tokens ratchet"
# Counts raw color literals in web/src/control (hex literals + Tailwind
# arbitrary color classes like `[#...]`/`[rgb(...)]`), excluding theme.css
# (the one file allowed to declare the raw hex values themselves — see
# web/src/control/DESIGN.md, rule 8: "no raw hex in components, tokens
# only"). Baseline lives in scripts/design-token-baseline.txt; the gate
# fails if the count goes UP (new raw colors instead of tokens).
design_token_baseline_file="$script_dir/design-token-baseline.txt"
design_token_baseline="$(cat "$design_token_baseline_file")"
design_token_matches="$(grep -rEno '#[0-9a-fA-F]{3,8}\b|\[(#|rgb)' src/control --include='*.tsx' --include='*.ts' | grep -v '/control/theme\.css' || true)"
design_token_count="$(printf '%s\n' "$design_token_matches" | grep -c . || true)"
if [[ "$design_token_count" -gt "$design_token_baseline" ]]; then
  echo "FAIL: raw color literals in web/src/control went from $design_token_baseline to $design_token_count." >&2
  echo "Use tokens from web/src/control/DESIGN.md (theme.css) instead of raw hex/arbitrary color classes. Offending lines:" >&2
  printf '%s\n' "$design_token_matches" >&2
  exit 1
elif [[ "$design_token_count" -lt "$design_token_baseline" ]]; then
  echo "hint: raw color count dropped ($design_token_baseline -> $design_token_count); consider lowering the baseline in $design_token_baseline_file"
fi

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
