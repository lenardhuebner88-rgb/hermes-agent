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
#      Gate-Lauf aushöhlen (exakt der Incident). Ein Symlink auf den exakt für
#      diesen Worktree reservierten Baum unter HERMES_WORKTREE_DEPS_ROOT ist
#      dagegen exklusiv und erlaubt. Nur fremde/geteilte Links werden entfernt;
#      dedizierte Deps entstehen lazy beim ersten `npm ci`.
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
#   HERMES_WORKTREE_DEPS_ROOT=/... Basis für exklusive Worktree-Deps
#                                    (Default /mnt/data/hermes-worktree-deps).
set -euo pipefail
"$(dirname "$0")/check-branch-age.sh" || exit 1

script_dir="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
repo_root="$(cd "$script_dir/.." && pwd -P)"
worktree_deps_root="${HERMES_WORKTREE_DEPS_ROOT:-/mnt/data/hermes-worktree-deps}"
if [[ "$worktree_deps_root" != /* ]]; then
  echo "FAIL (frontend-preflight): HERMES_WORKTREE_DEPS_ROOT must be absolute." >&2
  exit 1
fi
worktree_deps_root="$(readlink -m -- "$worktree_deps_root")"
worktree_deps_hash="$(
  printf '%s' "$repo_root" | sha256sum | awk '{print substr($1, 1, 16)}'
)"
worktree_deps_identity="$(basename -- "$repo_root")-${worktree_deps_hash}"
worktree_deps_tree="$worktree_deps_root/$worktree_deps_identity"

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

_gate_expected_dedicated_target() {
  local p="$1"
  case "$p" in
    "$repo_root/node_modules")
      printf '%s\n' "$worktree_deps_tree/node_modules"
      ;;
    "$repo_root/web/node_modules")
      printf '%s\n' "$worktree_deps_tree/web/node_modules"
      ;;
    *)
      return 1
      ;;
  esac
}

_gate_is_dedicated_nm_link() {
  local p="$1" actual expected
  [[ -L "$p" ]] || return 1
  actual="$(readlink -m -- "$p" 2>/dev/null || true)"
  expected="$(_gate_expected_dedicated_target "$p")" || return 1
  [[ -n "$actual" && "$actual" == "$expected" ]]
}

_gate_has_dedicated_nm_link() {
  local p
  for p in "${_GATE_NM_PATHS[@]}"; do
    _gate_is_dedicated_nm_link "$p" && return 0
  done
  return 1
}

_gate_prepare_dedicated_layout() {
  local p target workspace workspace_list
  for p in "${_GATE_NM_PATHS[@]}"; do
    target="$(_gate_expected_dedicated_target "$p")"
    if [[ ! -e "$p" && ! -L "$p" ]]; then
      if ! mkdir -p -- "$(dirname -- "$p")" || ! ln -s -- "$target" "$p"; then
        echo "FAIL (frontend-preflight): could not create dedicated link $p" >&2
        return 1
      fi
    fi
    if ! _gate_is_dedicated_nm_link "$p"; then
      echo "FAIL (frontend-preflight): mixed local/dedicated node_modules layout at $p" >&2
      return 1
    fi
    if ! mkdir -p -- "$target"; then
      echo "FAIL (frontend-preflight): could not create dedicated target $target" >&2
      return 1
    fi
  done

  if ! cp -f -- "$repo_root/package.json" "$worktree_deps_tree/package.json" ||
     ! cp -f -- "$repo_root/package-lock.json" "$worktree_deps_tree/package-lock.json"; then
    echo "FAIL (frontend-preflight): could not stage root manifests in $worktree_deps_tree" >&2
    return 1
  fi
  if ! workspace_list="$(
    node -e '
      const fs = require("fs");
      const lock = JSON.parse(fs.readFileSync(process.argv[1], "utf8"));
      for (const key of Object.keys(lock.packages || {})) {
        if (key && !key.startsWith("node_modules/") && !key.includes("/node_modules/")) {
          process.stdout.write(`${key}\n`);
        }
      }
    ' "$repo_root/package-lock.json"
  )"; then
    echo "FAIL (frontend-preflight): could not enumerate npm workspaces" >&2
    return 1
  fi
  while IFS= read -r workspace; do
    [[ -n "$workspace" ]] || continue
    if [[ ! -f "$repo_root/$workspace/package.json" ]]; then
      echo "FAIL (frontend-preflight): workspace manifest missing: $workspace/package.json" >&2
      return 1
    fi
    if ! mkdir -p -- "$worktree_deps_tree/$workspace" ||
       ! cp -f -- \
         "$repo_root/$workspace/package.json" \
         "$worktree_deps_tree/$workspace/package.json"; then
      echo "FAIL (frontend-preflight): could not stage workspace $workspace" >&2
      return 1
    fi
  done <<< "$workspace_list"
}

_gate_rewire_workspace_links() {
  local workspace package_name link workspace_links
  if ! workspace_links="$(
    node -e '
      const fs = require("fs");
      const path = require("path");
      const repo = process.argv[1];
      const lock = JSON.parse(fs.readFileSync(path.join(repo, "package-lock.json"), "utf8"));
      for (const key of Object.keys(lock.packages || {})) {
        if (!key || key.startsWith("node_modules/") || key.includes("/node_modules/")) continue;
        const manifest = JSON.parse(fs.readFileSync(path.join(repo, key, "package.json"), "utf8"));
        if (manifest.name) process.stdout.write(`${key}\t${manifest.name}\n`);
      }
    ' "$repo_root"
  )"; then
    echo "FAIL (frontend-preflight): could not enumerate workspace package links" >&2
    return 1
  fi
  while IFS=$'\t' read -r workspace package_name; do
    [[ -n "$workspace" && -n "$package_name" ]] || continue
    link="$worktree_deps_tree/node_modules/$package_name"
    if [[ -L "$link" ]]; then
      rm -f -- "$link"
    elif [[ -e "$link" ]]; then
      echo "FAIL (frontend-preflight): workspace install is not a symlink: $link" >&2
      return 1
    fi
    mkdir -p -- "$(dirname -- "$link")"
    ln -s -- "$repo_root/$workspace" "$link"
  done <<< "$workspace_links"
}

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
  # Emit one line per shared/foreign node_modules symlink (empty = all local or
  # dedicated). The exact per-worktree targets under worktree_deps_root are
  # accepted even though they live outside repo_root; a link into any checkout
  # (including live) or any other worktree's deps tree remains foreign.
  local p real
  for p in "${_GATE_NM_PATHS[@]}"; do
    [[ -L "$p" ]] || continue
    real="$(readlink -m -- "$p" 2>/dev/null || true)"
    if [[ -n "$real" && ( "$real" == "$repo_root" || "$real" == "$repo_root"/* ) ]]; then
      continue   # symlink resolves inside this worktree → local, fine
    fi
    if _gate_is_dedicated_nm_link "$p"; then
      continue   # exact exclusive tree for this worktree → isolated, fine
    fi
    printf '  - %s -> %s\n' "$p" "${real:-$(readlink "$p") (dangling)}"
  done
}

_gate_npm_ci() {
  # Deterministic install from the project lockfile. Dedicated symlink targets
  # remain lazy until this first npm ci. npm 11 removes a node_modules symlink
  # before reifying, so dedicated installs run in a manifest-only shadow root
  # on the data volume; workspace package links are then pointed back at this
  # worktree's source tree.
  local install_root="$repo_root"
  if [[ ! -f "$repo_root/package-lock.json" ]]; then
    {
      echo "FAIL (frontend-preflight): no package-lock.json in $repo_root —"
      echo "cannot prepare worktree-local deps deterministically."
    } >&2
    return 1
  fi
  if _gate_has_dedicated_nm_link; then
    _gate_prepare_dedicated_layout || return 1
    install_root="$worktree_deps_tree"
  fi
  if ! ( cd "$install_root" && npm ci ); then
    echo "FAIL (frontend-preflight): 'npm ci' did not complete; cannot proceed." >&2
    return 1
  fi
  if [[ "$install_root" == "$worktree_deps_tree" ]]; then
    _gate_rewire_workspace_links || return 1
  fi
  return 0
}

preflight_deps() {
  local stale foreign
  stale="$(_gate_stale_report)"
  foreign="$(_gate_foreign_nm_report)"

  # Fast path when deps are local or exactly dedicated AND the toolchain resolves.
  # A link into another checkout or worktree is rejected even when it currently
  # resolves: only the identity-bound external tree is exclusive to this gate.
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

  echo "frontend-preflight: isolated toolchain prepared from package-lock.json." >&2
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

# Opt-in E2E stage (Frage-Assistent Klick-Regression). Off by default so the
# gate stays byte-identical for normal iteration; needs a fresh build + free
# preview port. Enable with GATE_E2E=1 (e.g. before land/deploy of answer-sheet).
if [[ "${GATE_E2E:-0}" == "1" ]]; then
  if [[ $skip_build -eq 1 ]]; then
    {
      echo "FAIL (gate-e2e): GATE_E2E=1 requires a fresh build (do not pass --skip-build)."
    } >&2
    exit 1
  fi
  step "playwright e2e/agent-questions (GATE_E2E=1, vite preview)"
  preview_port=4173
  playwright_bin=""
  if [[ -x "$repo_root/node_modules/.bin/playwright" ]]; then
    playwright_bin="$repo_root/node_modules/.bin/playwright"
  elif [[ -x "$repo_root/web/node_modules/.bin/playwright" ]]; then
    playwright_bin="$repo_root/web/node_modules/.bin/playwright"
  else
    echo "FAIL (gate-e2e): playwright binary not found under node_modules/.bin/" >&2
    exit 1
  fi
  vite_bin=""
  if [[ -x "$repo_root/node_modules/.bin/vite" ]]; then
    vite_bin="$repo_root/node_modules/.bin/vite"
  elif [[ -x "$repo_root/web/node_modules/.bin/vite" ]]; then
    vite_bin="$repo_root/web/node_modules/.bin/vite"
  else
    echo "FAIL (gate-e2e): vite binary not found under node_modules/.bin/" >&2
    exit 1
  fi
  # Port MUSS frei sein BEVOR wir starten: die Readiness-curl unten kann sonst
  # einen fremden Server auf :4173 treffen, bevor vite am --strictPort stirbt —
  # Playwright testet dann einen fremden Build (false-green, Codex-Lens I1 #3).
  if curl -sf -o /dev/null --max-time 2 "http://127.0.0.1:${preview_port}/"; then
    echo "FAIL (gate-e2e): port ${preview_port} ist bereits belegt — Abbruch statt fremden Server zu testen." >&2
    exit 1
  fi
  preview_log="$(mktemp -t gate-e2e-preview.XXXXXX.log)"
  preview_pid=""
  _gate_e2e_cleanup() {
    if [[ -n "${preview_pid:-}" ]] && kill -0 "$preview_pid" 2>/dev/null; then
      kill "$preview_pid" 2>/dev/null || true
      wait "$preview_pid" 2>/dev/null || true
    fi
    rm -f "$preview_log"
  }
  trap _gate_e2e_cleanup EXIT
  # Serve the dist just built (web/ dist → hermes_cli/web_dist or vite outDir).
  # --strictPort so a foreign process on 4173 fails loudly instead of drifting.
  ( cd "$repo_root/web" && "$vite_bin" preview --host 127.0.0.1 --strictPort --port "$preview_port" ) \
    >"$preview_log" 2>&1 &
  preview_pid=$!
  # Wait until preview answers (max ~20s).
  ready=0
  for _ in $(seq 1 40); do
    if ! kill -0 "$preview_pid" 2>/dev/null; then
      echo "FAIL (gate-e2e): vite preview exited early. log:" >&2
      cat "$preview_log" >&2 || true
      exit 1
    fi
    if curl -sf -o /dev/null "http://127.0.0.1:${preview_port}/"; then
      ready=1
      break
    fi
    sleep 0.5
  done
  if [[ "$ready" -ne 1 ]]; then
    echo "FAIL (gate-e2e): vite preview did not become ready on :${preview_port}" >&2
    cat "$preview_log" >&2 || true
    exit 1
  fi
  (
    cd "$repo_root/web"
    PLAYWRIGHT_BASE_URL="http://127.0.0.1:${preview_port}" \
      "$playwright_bin" test e2e/agent-questions.spec.ts
  )
  e2e_rc=$?
  _gate_e2e_cleanup
  trap - EXIT
  if [[ "$e2e_rc" -ne 0 ]]; then
    exit "$e2e_rc"
  fi
fi

printf '\n=== FRONTEND-GATE GRÜN (exit 0) ===\n'
