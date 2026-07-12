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
#
# Dependency-Preflight (entkoppelt vom Shared-node_modules-Zustand):
# Historisch vertraute das Gate blind dem umgebenden `node_modules/.bin/tsc`.
# In geteilten/Live-Checkouts kann dieser Symlink stale sein — Ziel gelöscht,
# z.B. wenn ein paralleler npm-Lauf das Live-node_modules aushöhlt. `npx tsc`
# hängt dann bis zum Release-Gate-Timeout (Incident 2026-07-12: root
# `node_modules/.bin/tsc -> ../../web/node_modules/typescript/bin/tsc` fehlte,
# Release-Gate lief in einen Timeout). Der Preflight prüft daher VOR den
# Gate-Schritten deterministisch, dass die Toolchain wirklich auflösbar ist,
# und bereitet sie sonst reproduzierbar aus dem Lockfile vor (`npm ci`) ODER
# bricht schnell mit handlungsfähiger Diagnose ab — statt in einen Timeout zu
# laufen. Gegen ein fremd-verlinktes (Live-)node_modules wird NIE `npm ci`
# ausgeführt (das würde fremde Deps überschreiben); dann nur Diagnose+Abbruch.
#
# Env-Schalter (Default = sicheres, deterministisches Verhalten):
#   GATE_FRONTEND_AUTO_INSTALL=0   Kein Auto-`npm ci`; stale Toolchain → schnell blocken.
#   GATE_FRONTEND_PREFLIGHT_ONLY=1 Nur den Preflight ausführen (Selbsttest), dann exit.
set -euo pipefail

script_dir="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
repo_root="$(cd "$script_dir/.." && pwd -P)"

skip_build=0
if [[ "${1:-}" == "--skip-build" ]]; then
  skip_build=1
elif [[ $# -gt 0 ]]; then
  echo "usage: $0 [--skip-build]" >&2
  exit 2
fi

step() { printf '\n=== GATE: %s ===\n' "$1"; }

# --- Dependency-Preflight -----------------------------------------------------
# Required frontend toolchain binaries. Each is "ok" if it resolves to a real
# executable in EITHER the hoisted root node_modules OR the web workspace
# node_modules (npx from web/ falls back to root for hoisted deps).
_GATE_REQUIRED_BINS=(tsc vitest eslint)

_gate_bin_ok() {
  # 0 (ok) if the bin resolves to a real executable somewhere npx would find it.
  # `-x` follows symlinks and tests the *target*: a stale/broken symlink → not ok.
  local b="$1"
  [[ -x "$repo_root/node_modules/.bin/$b" ]] && return 0
  [[ -x "$repo_root/web/node_modules/.bin/$b" ]] && return 0
  return 1
}

_gate_stale_report() {
  # Emit one human line per broken/missing required bin (empty = all healthy).
  local b p
  for b in "${_GATE_REQUIRED_BINS[@]}"; do
    _gate_bin_ok "$b" && continue
    p="$repo_root/node_modules/.bin/$b"
    if [[ -L "$p" ]]; then
      printf '  - %s: stale symlink %s -> %s (target missing)\n' "$b" "$p" "$(readlink "$p")"
    elif [[ -e "$p" ]]; then
      printf '  - %s: not executable %s\n' "$b" "$p"
    else
      printf '  - %s: missing %s\n' "$b" "$p"
    fi
  done
}

preflight_deps() {
  local stale; stale="$(_gate_stale_report)"
  [[ -z "$stale" ]] && return 0   # toolchain resolves → nothing to prepare

  step "dependency preflight (toolchain missing/stale)"
  local nm="$repo_root/node_modules"

  # Guard: never `npm ci` against a node_modules symlinked into a FOREIGN
  # checkout — that would rewrite shared/live deps (documented gutting trap).
  if [[ -L "$nm" ]]; then
    local nm_real; nm_real="$(readlink -f "$nm" 2>/dev/null || true)"
    if [[ "$nm_real" != "$repo_root"/* ]]; then
      {
        echo "FAIL (frontend-preflight): the frontend toolchain is missing/stale AND"
        echo "node_modules is symlinked into a foreign checkout:"
        echo "  $nm -> $nm_real"
        printf '%s\n' "$stale"
        echo "Refusing to run 'npm ci' against a shared/live checkout (it would rewrite foreign deps)."
        echo "Fix: recreate real deps IN THIS worktree — rm '$nm' and '$repo_root/web/node_modules',"
        echo "     then 'npm ci' in '$repo_root' (restores deterministically from package-lock.json)."
      } >&2
      return 1
    fi
  fi

  if [[ "${GATE_FRONTEND_AUTO_INSTALL:-1}" != "1" ]]; then
    {
      echo "FAIL (frontend-preflight): the frontend toolchain is missing/stale:"
      printf '%s\n' "$stale"
      echo "Fix: run 'npm ci' in '$repo_root' to restore deps deterministically from package-lock.json."
    } >&2
    return 1
  fi

  if [[ ! -f "$repo_root/package-lock.json" ]]; then
    {
      echo "FAIL (frontend-preflight): toolchain missing/stale and no package-lock.json in"
      echo "  $repo_root — cannot prepare deterministically."
      printf '%s\n' "$stale"
    } >&2
    return 1
  fi

  {
    echo "frontend-preflight: toolchain missing/stale; restoring deterministically"
    echo "via 'npm ci' from package-lock.json ..."
    printf '%s\n' "$stale"
  } >&2
  if ! ( cd "$repo_root" && npm ci ); then
    echo "FAIL (frontend-preflight): 'npm ci' did not complete; cannot proceed." >&2
    return 1
  fi
  stale="$(_gate_stale_report)"
  if [[ -n "$stale" ]]; then
    {
      echo "FAIL (frontend-preflight): toolchain still missing/stale after 'npm ci':"
      printf '%s\n' "$stale"
    } >&2
    return 1
  fi
  echo "frontend-preflight: toolchain restored from package-lock.json." >&2
  return 0
}

preflight_deps

if [[ "${GATE_FRONTEND_PREFLIGHT_ONLY:-0}" == "1" ]]; then
  printf '\n=== FRONTEND-PREFLIGHT OK (exit 0) ===\n'
  exit 0
fi

cd "$repo_root/web"

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
