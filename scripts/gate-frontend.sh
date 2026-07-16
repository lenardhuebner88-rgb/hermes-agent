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
# Gate-Schritten und entkoppelt das Gate deterministisch vom umgebenden
# node_modules-Zustand. Zwei Bedingungen werden geprüft:
#   1. Ist `node_modules` (root ODER web) ein Symlink in einen FREMDEN Checkout
#      (Ziel außerhalb des repo_root, z.B. Live)? Dann darf das Gate diese
#      mutablen Shared-Deps NICHT verwenden — auch wenn die Toolchain gerade
#      auflöst: ein paralleler npm-Lauf im Live-Checkout kann sie mitten im
#      Gate-Lauf aushöhlen (exakt der Incident). Der Preflight materialisiert
#      dann worktree-eigene Deps aus dem Lockfile (Symlink entfernen → `npm ci`)
#      ODER blockiert schnell mit handlungsfähiger Diagnose.
#   2. Löst die Toolchain (tsc/vitest/eslint) überhaupt auf echte Executables
#      auf? Stale/fehlend → reproduzierbar aus dem Lockfile wiederherstellen
#      (`npm ci`) ODER schnell blocken — statt in einen Timeout zu laufen.
# Der Symlink wird VOR `npm ci` entfernt (rm eines Symlinks kann das fremde
# Ziel nicht berühren) — so wird das Live-node_modules NIE überschrieben.
#
# Env-Schalter (Default = sicheres, deterministisches Verhalten):
#   GATE_FRONTEND_AUTO_INSTALL=0   Kein Auto-`npm ci`; stale Toolchain → schnell blocken.
#   GATE_FRONTEND_PREFLIGHT_ONLY=1 Nur den Preflight ausführen (Selbsttest), dann exit.
#   GATE_FRONTEND_MAX_WORKERS=4     Vitest-Forks begrenzen; überschreibbar für CI/starke Hosts.
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

# The node_modules that MUST be worktree-local for a trustworthy gate. A symlink
# from either into a foreign checkout makes the gate depend on mutable shared deps.
_GATE_NM_PATHS=("$repo_root/node_modules" "$repo_root/web/node_modules")

_gate_bin_ok() {
  # 0 (ok) if the bin resolves to a real executable somewhere npx would find it.
  # `-x` follows symlinks and tests the *target*: a stale/broken symlink → not ok.
  local b="$1"
  [[ -x "$repo_root/node_modules/.bin/$b" ]] && return 0
  [[ -x "$repo_root/web/node_modules/.bin/$b" ]] && return 0
  return 1
}

_gate_bin_path() {
  local b="$1"
  if [[ -x "$repo_root/node_modules/.bin/$b" ]]; then
    printf '%s\n' "$repo_root/node_modules/.bin/$b"
    return 0
  fi
  if [[ -x "$repo_root/web/node_modules/.bin/$b" ]]; then
    printf '%s\n' "$repo_root/web/node_modules/.bin/$b"
    return 0
  fi
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

_gate_foreign_nm_report() {
  # Emit one line per node_modules symlinked to a target OUTSIDE repo_root
  # (empty = all worktree-local). A dangling symlink counts as foreign too:
  # its target can't be proven to live inside this worktree.
  local p real
  for p in "${_GATE_NM_PATHS[@]}"; do
    [[ -L "$p" ]] || continue
    real="$(readlink -f "$p" 2>/dev/null || true)"
    if [[ -n "$real" && ( "$real" == "$repo_root" || "$real" == "$repo_root"/* ) ]]; then
      continue   # symlink resolves inside this worktree → local, fine
    fi
    printf '  - %s -> %s\n' "$p" "${real:-$(readlink "$p") (dangling)}"
  done
}

_gate_npm_ci() {
  # Deterministic install from the project lockfile into worktree-local dirs.
  if [[ ! -f "$repo_root/package-lock.json" ]]; then
    {
      echo "FAIL (frontend-preflight): no package-lock.json in $repo_root —"
      echo "cannot prepare worktree-local deps deterministically."
    } >&2
    return 1
  fi
  if ! ( cd "$repo_root" && npm ci ); then
    echo "FAIL (frontend-preflight): 'npm ci' did not complete; cannot proceed." >&2
    return 1
  fi
  return 0
}

preflight_deps() {
  local stale foreign
  stale="$(_gate_stale_report)"
  foreign="$(_gate_foreign_nm_report)"

  # Fast path ONLY when deps are worktree-local AND the toolchain resolves.
  # A foreign-symlinked node_modules is rejected even when it currently resolves:
  # the gate must not depend on mutable shared/live deps — a parallel npm op in
  # the live checkout can gut them mid-run (the 2026-07-12 release-gate timeout).
  if [[ -z "$stale" && -z "$foreign" ]]; then
    return 0
  fi

  step "dependency preflight"

  # (1) node_modules symlinked into a foreign checkout → decouple or block fast.
  if [[ -n "$foreign" ]]; then
    if [[ "${GATE_FRONTEND_AUTO_INSTALL:-1}" != "1" ]]; then
      {
        echo "FAIL (frontend-preflight): node_modules is symlinked into a FOREIGN checkout:"
        printf '%s\n' "$foreign"
        echo "The gate must not run against shared/live deps (a parallel npm op can gut them"
        echo "mid-run → hang/timeout). Refusing to trust ambient node_modules."
        echo "Fix: materialize worktree-local deps — rm the symlink(s) above, then 'npm ci' in"
        echo "     '$repo_root' (restores deterministically from package-lock.json)."
      } >&2
      return 1
    fi
    {
      echo "frontend-preflight: node_modules symlinked into a foreign checkout; decoupling by"
      echo "materializing worktree-local deps from package-lock.json ..."
      printf '%s\n' "$foreign"
    } >&2
    # Remove ONLY the symlinks (rm of a symlink can't touch the foreign target →
    # the live checkout is never rewritten), then npm ci into real local dirs.
    local p
    for p in "${_GATE_NM_PATHS[@]}"; do
      if [[ -L "$p" ]]; then rm -f "$p"; fi
    done
    _gate_npm_ci || return 1
    stale="$(_gate_stale_report)"   # re-evaluate the toolchain against local deps
    foreign="$(_gate_foreign_nm_report)"
    if [[ -n "$foreign" ]]; then
      { echo "FAIL (frontend-preflight): node_modules still symlinked foreign after npm ci:"
        printf '%s\n' "$foreign"; } >&2
      return 1
    fi
  fi

  # (2) worktree-local but toolchain stale/missing → restore or block fast.
  if [[ -n "$stale" ]]; then
    if [[ "${GATE_FRONTEND_AUTO_INSTALL:-1}" != "1" ]]; then
      {
        echo "FAIL (frontend-preflight): the frontend toolchain is missing/stale:"
        printf '%s\n' "$stale"
        echo "Fix: run 'npm ci' in '$repo_root' to restore deps deterministically from package-lock.json."
      } >&2
      return 1
    fi
    {
      echo "frontend-preflight: toolchain missing/stale; restoring deterministically"
      echo "via 'npm ci' from package-lock.json ..."
      printf '%s\n' "$stale"
    } >&2
    _gate_npm_ci || return 1
    stale="$(_gate_stale_report)"
    if [[ -n "$stale" ]]; then
      {
        echo "FAIL (frontend-preflight): toolchain still missing/stale after 'npm ci':"
        printf '%s\n' "$stale"
      } >&2
      return 1
    fi
  fi

  echo "frontend-preflight: worktree-local toolchain prepared from package-lock.json." >&2
  return 0
}

preflight_deps

if [[ "${GATE_FRONTEND_PREFLIGHT_ONLY:-0}" == "1" ]]; then
  printf '\n=== FRONTEND-PREFLIGHT OK (exit 0) ===\n'
  exit 0
fi

tsc_bin="$(_gate_bin_path tsc)"
vitest_bin="$(_gate_bin_path vitest)"
vitest_max_workers="${GATE_FRONTEND_MAX_WORKERS:-4}"

cd "$repo_root/web"

step "design-tokens ratchet"
# Implementierung geteilt mit webs `npm run check` (CI) — EIN Vertrag:
# rohe Farb-Literale in web/src/control dürfen die Baseline in
# scripts/design-token-baseline.txt nicht überschreiten (DESIGN.md Regel 8).
"$script_dir/check-design-tokens.sh"

step "npm run lint:control"
npm run lint:control

step "tsc -b --noEmit (worktree-local)"
"$tsc_bin" -b --noEmit

step "vitest run (worktree-local, maxWorkers=$vitest_max_workers)"
"$vitest_bin" run --maxWorkers="$vitest_max_workers"

if [[ $skip_build -eq 1 ]]; then
  step "build ÜBERSPRUNGEN (--skip-build)"
else
  step "npm run build"
  npm run build
fi

printf '\n=== FRONTEND-GATE GRÜN (exit 0) ===\n'
