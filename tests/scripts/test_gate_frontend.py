"""Regression tests for scripts/gate-frontend.sh — the stale-node_modules trap.

Incident 2026-07-12: the frontend release-gate ran into a *timeout* because it
blindly trusted the ambient ``node_modules/.bin/tsc`` — a stale symlink whose
target (``../../web/node_modules/typescript/bin/tsc``) had been gutted by a
parallel npm op in the shared/live checkout. ``npx tsc`` then hung until the
release-gate timed out.

The fix adds a dependency preflight that runs BEFORE the gate steps and
decouples the gate from whatever ambient node_modules happens to be present:

  * a stale/missing toolchain is either restored deterministically from the
    project lockfile (``npm ci``), or
  * the gate blocks *fast* with an actionable diagnosis (no more timeout), and
  * it NEVER runs ``npm ci`` against a node_modules symlinked into a foreign
    (shared/live) checkout — that would rewrite foreign deps.

These drive the real ``scripts/gate-frontend.sh`` in ``GATE_FRONTEND_PREFLIGHT_ONLY``
mode against a throwaway repo whose ``node_modules/.bin/tsc`` is a stale symlink,
with a *stubbed* ``npm`` on PATH so we can assert positively whether ``npm ci``
was (or was never) reached.
"""
from __future__ import annotations

import os
import stat
import subprocess
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
GATE_SCRIPT = REPO_ROOT / "scripts" / "gate-frontend.sh"

_EXEC = stat.S_IRWXU | stat.S_IRGRP | stat.S_IXGRP | stat.S_IROTH | stat.S_IXOTH

# A stubbed `npm`: records its args, and on `ci` REPAIRS the stale tsc symlink
# by writing a real executable in its stead (simulating a lockfile restore).
_NPM_STUB = """#!/usr/bin/env bash
echo "$*" >> "$GATE_TEST_NPM_SENTINEL"
if [[ "${1:-}" == "ci" ]]; then
  rm -f node_modules/.bin/tsc
  printf '#!/bin/sh\\nexit 0\\n' > node_modules/.bin/tsc
  chmod +x node_modules/.bin/tsc
fi
exit 0
"""


def _write_exec(path: Path, body: str = "#!/bin/sh\nexit 0\n") -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(body)
    path.chmod(_EXEC)


def _make_repo(tmp_path: Path, *, tsc: str) -> tuple[Path, Path, Path]:
    """Throwaway repo with the REAL gate script + a healthy vitest/eslint and a
    ``tsc`` in one of three states.

    ``tsc``: "stale" (broken symlink), "healthy" (real exec), or "missing".
    Returns (repo_root, npm_sentinel, npm_stub_dir).
    """
    repo = tmp_path / "repo"
    (repo / "scripts").mkdir(parents=True)
    # Copy the real script under test verbatim.
    (repo / "scripts" / "gate-frontend.sh").write_text(GATE_SCRIPT.read_text())
    (repo / "scripts" / "gate-frontend.sh").chmod(_EXEC)
    (repo / "package-lock.json").write_text("{}\n")

    bindir = repo / "node_modules" / ".bin"
    bindir.mkdir(parents=True)
    _write_exec(bindir / "vitest")
    _write_exec(bindir / "eslint")
    if tsc == "healthy":
        _write_exec(bindir / "tsc")
    elif tsc == "stale":
        # Broken symlink: mirrors the incident's dangling ../typescript/bin/tsc.
        (bindir / "tsc").symlink_to("../typescript/bin/tsc")
    elif tsc == "missing":
        pass
    else:  # pragma: no cover - test misuse
        raise ValueError(tsc)

    # A stubbed `npm` on a private bin dir we prepend to PATH.
    npm_dir = tmp_path / "fakebin"
    npm_dir.mkdir()
    _write_exec(npm_dir / "npm", _NPM_STUB)
    sentinel = tmp_path / "_npm_called"
    return repo, sentinel, npm_dir


def _run_preflight(
    repo: Path,
    sentinel: Path,
    npm_dir: Path,
    *,
    auto_install: str = "1",
) -> subprocess.CompletedProcess[str]:
    env = dict(os.environ)
    env["PATH"] = f"{npm_dir}{os.pathsep}{env['PATH']}"
    env["GATE_FRONTEND_PREFLIGHT_ONLY"] = "1"
    env["GATE_FRONTEND_AUTO_INSTALL"] = auto_install
    env["GATE_TEST_NPM_SENTINEL"] = str(sentinel)
    return subprocess.run(
        [str(repo / "scripts" / "gate-frontend.sh")],
        cwd=repo,
        env=env,
        capture_output=True,
        text=True,
    )


def test_preflight_blocks_fast_on_stale_tsc_symlink_without_autoinstall(tmp_path: Path) -> None:
    """The exact incident: stale tsc symlink. With auto-install OFF the gate must
    block fast with an actionable diagnosis — never hang, never touch npm."""
    repo, sentinel, npm_dir = _make_repo(tmp_path, tsc="stale")
    r = _run_preflight(repo, sentinel, npm_dir, auto_install="0")
    assert r.returncode != 0, r.stdout + r.stderr
    assert "frontend-preflight" in r.stderr
    assert "tsc" in r.stderr and "stale symlink" in r.stderr
    assert "npm ci" in r.stderr  # names the deterministic remedy
    assert not sentinel.exists(), "npm must NOT run when auto-install is disabled"


def test_preflight_restores_deterministically_via_npm_ci(tmp_path: Path) -> None:
    """With auto-install ON, a stale toolchain is restored deterministically from
    the lockfile (npm ci) and the preflight then passes."""
    repo, sentinel, npm_dir = _make_repo(tmp_path, tsc="stale")
    r = _run_preflight(repo, sentinel, npm_dir, auto_install="1")
    assert r.returncode == 0, r.stdout + r.stderr
    assert sentinel.exists(), "npm ci was expected to run"
    assert "ci" in sentinel.read_text().split(), sentinel.read_text()
    assert "restored from package-lock.json" in r.stderr


def test_preflight_refuses_npm_against_foreign_symlinked_node_modules(tmp_path: Path) -> None:
    """A node_modules symlinked into a FOREIGN checkout with a broken toolchain
    must block (not npm-install) — running npm ci there would rewrite live deps."""
    repo, sentinel, npm_dir = _make_repo(tmp_path, tsc="stale")
    # Replace the real node_modules with a symlink pointing OUTSIDE the repo.
    foreign = tmp_path / "live" / "node_modules"
    (foreign / ".bin").mkdir(parents=True)
    (foreign / ".bin" / "tsc").symlink_to("../typescript/bin/tsc")  # broken there too
    import shutil

    shutil.rmtree(repo / "node_modules")
    (repo / "node_modules").symlink_to(foreign)

    r = _run_preflight(repo, sentinel, npm_dir, auto_install="1")
    assert r.returncode != 0, r.stdout + r.stderr
    assert "foreign checkout" in r.stderr
    assert not sentinel.exists(), "npm ci must NEVER run against a foreign-linked node_modules"


def test_preflight_passes_with_healthy_toolchain(tmp_path: Path) -> None:
    """Baseline: a resolvable toolchain passes preflight untouched (no npm)."""
    repo, sentinel, npm_dir = _make_repo(tmp_path, tsc="healthy")
    r = _run_preflight(repo, sentinel, npm_dir, auto_install="1")
    assert r.returncode == 0, r.stdout + r.stderr
    assert "FRONTEND-PREFLIGHT OK" in r.stdout
    assert not sentinel.exists(), "healthy toolchain needs no npm ci"
