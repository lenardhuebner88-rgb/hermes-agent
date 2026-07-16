#!/usr/bin/env bash
# check-design-tokens.sh — Design-Token-Ratchet für web/src/control.
#
# Zählt rohe Farb-Literale (hex-Literale + Tailwind arbitrary color classes wie
# `[#...]`/`[rgb(...)]`), ausgenommen theme.css (die eine Datei, die die rohen
# hex-Werte selbst deklarieren darf — siehe web/src/control/DESIGN.md, Regel 8:
# "no raw hex in components, tokens only"). Baseline liegt in
# scripts/design-token-baseline.txt; der Check schlägt fehl, wenn die Zahl
# STEIGT (neue Rohfarben statt Tokens).
#
# EINE Implementierung, geteilt von scripts/gate-frontend.sh (lokales Gate) und
# webs `npm run check` (CI via js-tests.yml) — damit CI denselben Vertrag
# erzwingt wie das lokale Gate.
set -euo pipefail

script_dir="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
repo_root="$(cd "$script_dir/.." && pwd -P)"

cd "$repo_root/web"

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
echo "design-tokens OK: $design_token_count raw color literals (baseline $design_token_baseline)"
