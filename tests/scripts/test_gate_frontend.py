"""Regression tests for scripts/gate-frontend.sh — the shared-node_modules trap.

Incident 2026-07-12: the frontend release-gate ran into a *timeout* because it
blindly trusted the ambient ``node_modules/.bin/tsc`` — a stale symlink whose
target (``../../web/node_modules/typescript/bin/tsc``) had been gutted by a
parallel npm op in the shared/live checkout. ``npx tsc`` then hung until the
release-gate timed out.

The fix adds a dependency preflight that runs BEFORE the gate steps and
decouples the gate from whatever ambient node_modules happens to be present.
Two independent conditions are enforced:

  * **Foreign symlink** — ``node_modules`` (root or web) symlinked into a
    checkout OUTSIDE this worktree makes the gate depend on mutable shared/live
    deps that a parallel npm op can gut mid-run. This is rejected *even when the
    toolchain currently resolves*: the preflight either materializes
    worktree-local deps from the lockfile (remove the symlink, then ``npm ci``)
    or blocks fast with an actionable diagnosis.
  * **Stale/missing toolchain** — a broken ``tsc``/``vitest``/``eslint`` in a
    worktree-local ``node_modules`` is restored deterministically from the
    lockfile (``npm ci``) or the gate blocks fast (no more timeout).

Crucially, the symlink is removed BEFORE ``npm ci`` — ``rm`` of a symlink can
never touch its target — so the foreign/live checkout is never rewritten.

These drive the real ``scripts/gate-frontend.sh`` in ``GATE_FRONTEND_PREFLIGHT_ONLY``
mode against a throwaway repo, with a *stubbed* ``npm`` on PATH so we can assert
positively whether ``npm ci`` was (or was never) reached and that it produced a
worktree-local toolchain.
"""
from __future__ import annotations

import os
import shutil
import stat
import subprocess
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
GATE_SCRIPT = REPO_ROOT / "scripts" / "gate-frontend.sh"

_EXEC = stat.S_IRWXU | stat.S_IRGRP | stat.S_IXGRP | stat.S_IROTH | stat.S_IXOTH

# A stubbed `npm`: records its args, and on `ci` MATERIALIZES a real worktree-local
# toolchain (mkdir node_modules/.bin + real tsc/vitest/eslint), simulating a
# deterministic lockfile restore. It never touches anything outside the cwd.
_NPM_STUB = """#!/usr/bin/env bash
echo "$*" >> "$GATE_TEST_NPM_SENTINEL"
if [[ "${1:-}" == "ci" ]]; then
  mkdir -p node_modules/.bin
  for b in tsc vitest eslint; do
    rm -f "node_modules/.bin/$b"
    printf '#!/bin/sh\\nexit 0\\n' > "node_modules/.bin/$b"
    chmod +x "node_modules/.bin/$b"
  done
fi
exit 0
"""

_TOOL_STUB = """#!/usr/bin/env bash
printf '%s:%s\\n' "$(basename "$0")" "$*" >> "$GATE_TEST_TOOL_SENTINEL"
exit 0
"""

_NPX_FAIL_STUB = """#!/usr/bin/env bash
echo "npx must not be used by gate-frontend.sh" >&2
exit 97
"""


def _write_exec(path: Path, body: str = "#!/bin/sh\nexit 0\n") -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(body)
    path.chmod(_EXEC)


def _make_repo(tmp_path: Path, *, tsc: str) -> tuple[Path, Path, Path]:
    """Throwaway repo with the REAL gate script + a healthy vitest/eslint and a
    ``tsc`` in one of three states, in a *worktree-local* node_modules.

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


def _make_foreign_symlink(tmp_path: Path, repo: Path, *, healthy: bool) -> Path:
    """Replace ``repo/node_modules`` with a symlink into a FOREIGN checkout that
    is left INTACT so tests can prove it is never rewritten. Returns the marker
    path inside the foreign checkout that must survive the run.
    """
    foreign = tmp_path / "live" / "node_modules"
    (foreign / ".bin").mkdir(parents=True)
    _write_exec(foreign / ".bin" / "vitest")
    _write_exec(foreign / ".bin" / "eslint")
    if healthy:
        _write_exec(foreign / ".bin" / "tsc")
    else:
        (foreign / ".bin" / "tsc").symlink_to("../typescript/bin/tsc")  # broken there
    marker = foreign / "DO_NOT_TOUCH"
    marker.write_text("live deps — must never be rewritten by the gate\n")

    shutil.rmtree(repo / "node_modules")
    (repo / "node_modules").symlink_to(foreign)
    return marker


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
    """The exact incident: stale tsc symlink in a worktree-local node_modules.
    With auto-install OFF the gate must block fast with an actionable diagnosis —
    never hang, never touch npm."""
    repo, sentinel, npm_dir = _make_repo(tmp_path, tsc="stale")
    r = _run_preflight(repo, sentinel, npm_dir, auto_install="0")
    assert r.returncode != 0, r.stdout + r.stderr
    assert "frontend-preflight" in r.stderr
    assert "tsc" in r.stderr and "stale symlink" in r.stderr
    assert "npm ci" in r.stderr  # names the deterministic remedy
    assert "FRONTEND-PREFLIGHT OK" not in r.stdout
    assert not sentinel.exists(), "npm must NOT run when auto-install is disabled"


def test_preflight_restores_deterministically_via_npm_ci(tmp_path: Path) -> None:
    """With auto-install ON, a stale toolchain in a worktree-local node_modules is
    restored deterministically from the lockfile (npm ci) and preflight passes."""
    repo, sentinel, npm_dir = _make_repo(tmp_path, tsc="stale")
    r = _run_preflight(repo, sentinel, npm_dir, auto_install="1")
    assert r.returncode == 0, r.stdout + r.stderr
    assert sentinel.exists(), "npm ci was expected to run"
    assert "ci" in sentinel.read_text().split(), sentinel.read_text()
    assert "prepared from package-lock.json" in r.stderr


def test_preflight_healthy_foreign_symlink_blocks_without_autoinstall(tmp_path: Path) -> None:
    """CORE regression: a node_modules symlinked into a FOREIGN checkout with a
    fully HEALTHY toolchain must NOT pass through. With auto-install OFF it blocks
    fast with an actionable 'foreign checkout' diagnosis — it must never end with
    FRONTEND-PREFLIGHT OK and must never rewrite the live deps."""
    repo, sentinel, npm_dir = _make_repo(tmp_path, tsc="healthy")
    marker = _make_foreign_symlink(tmp_path, repo, healthy=True)
    r = _run_preflight(repo, sentinel, npm_dir, auto_install="0")
    assert r.returncode != 0, r.stdout + r.stderr
    assert "FOREIGN checkout" in r.stderr
    assert "FRONTEND-PREFLIGHT OK" not in r.stdout
    assert not sentinel.exists(), "npm must NOT run when auto-install is disabled"
    assert marker.exists(), "the foreign/live node_modules must be left intact"


def test_preflight_materializes_worktree_local_from_healthy_foreign_symlink(tmp_path: Path) -> None:
    """With auto-install ON, a healthy-but-FOREIGN node_modules is decoupled: the
    symlink is removed and npm ci materializes worktree-local deps. The foreign
    checkout is left intact and node_modules is no longer a symlink."""
    repo, sentinel, npm_dir = _make_repo(tmp_path, tsc="healthy")
    marker = _make_foreign_symlink(tmp_path, repo, healthy=True)
    r = _run_preflight(repo, sentinel, npm_dir, auto_install="1")
    assert r.returncode == 0, r.stdout + r.stderr
    assert sentinel.exists() and "ci" in sentinel.read_text().split(), r.stderr
    assert "decoupling" in r.stderr
    assert not (repo / "node_modules").is_symlink(), "node_modules must be worktree-local now"
    assert marker.exists(), "npm ci must NEVER rewrite the foreign/live checkout"


def test_preflight_materializes_from_broken_foreign_symlink(tmp_path: Path) -> None:
    """A FOREIGN node_modules whose toolchain is ALSO broken is decoupled the same
    way (symlink removed, npm ci), never leaving the gate on shared deps and never
    touching the foreign checkout."""
    repo, sentinel, npm_dir = _make_repo(tmp_path, tsc="stale")
    marker = _make_foreign_symlink(tmp_path, repo, healthy=False)
    r = _run_preflight(repo, sentinel, npm_dir, auto_install="1")
    assert r.returncode == 0, r.stdout + r.stderr
    assert sentinel.exists() and "ci" in sentinel.read_text().split(), r.stderr
    assert not (repo / "node_modules").is_symlink()
    assert marker.exists(), "npm ci must NEVER rewrite the foreign/live checkout"


def test_preflight_passes_with_healthy_worktree_local_toolchain(tmp_path: Path) -> None:
    """Baseline: a resolvable, worktree-local toolchain passes preflight untouched
    (no npm, no decoupling)."""
    repo, sentinel, npm_dir = _make_repo(tmp_path, tsc="healthy")
    r = _run_preflight(repo, sentinel, npm_dir, auto_install="1")
    assert r.returncode == 0, r.stdout + r.stderr
    assert "FRONTEND-PREFLIGHT OK" in r.stdout
    assert not sentinel.exists(), "healthy worktree-local toolchain needs no npm ci"


def test_full_gate_uses_local_bins_and_bounded_vitest_workers(tmp_path: Path) -> None:
    """The release gate must not ask npx to discover tools or let Vitest fan
    out to every host CPU. Both make the atomic gate depend on ambient host
    state under concurrent operator workloads."""
    repo, npm_sentinel, npm_dir = _make_repo(tmp_path, tsc="healthy")
    (repo / "web" / "src" / "control").mkdir(parents=True)
    (repo / "web" / "package.json").write_text('{"scripts":{"lint:control":"true"}}\n')
    (repo / "scripts" / "design-token-baseline.txt").write_text("0\n")

    tool_sentinel = tmp_path / "_tool_called"
    _write_exec(repo / "node_modules" / ".bin" / "tsc", _TOOL_STUB)
    _write_exec(repo / "node_modules" / ".bin" / "vitest", _TOOL_STUB)
    _write_exec(npm_dir / "npx", _NPX_FAIL_STUB)

    env = dict(os.environ)
    env["PATH"] = f"{npm_dir}{os.pathsep}{env['PATH']}"
    env["GATE_FRONTEND_AUTO_INSTALL"] = "0"
    env["GATE_FRONTEND_MAX_WORKERS"] = "3"
    env["GATE_TEST_NPM_SENTINEL"] = str(npm_sentinel)
    env["GATE_TEST_TOOL_SENTINEL"] = str(tool_sentinel)
    r = subprocess.run(
        [str(repo / "scripts" / "gate-frontend.sh"), "--skip-build"],
        cwd=repo,
        env=env,
        capture_output=True,
        text=True,
    )

    assert r.returncode == 0, r.stdout + r.stderr
    calls = tool_sentinel.read_text().splitlines()
    assert "tsc:-b --noEmit" in calls
    assert "vitest:run --maxWorkers=3" in calls
