"""Regression test for the cron wrapper entry point.

Guards against the entry-point regression where ``python -m hermes_cli``
exits 1 with ``No module named hermes_cli.__main__`` because the
``hermes_cli`` package has no ``__main__.py`` — the entry guard lives in
``hermes_cli/main.py``.  See scripts/cron/export-langfuse-scores.sh.
"""
from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]
WRAPPER = REPO_ROOT / "scripts" / "cron" / "export-langfuse-scores.sh"


def _resolve_repo_python() -> str | None:
    """Return the repo venv python the wrapper selects, or None."""
    for cand in (REPO_ROOT / "venv" / "bin" / "python",
                 REPO_ROOT / ".venv" / "bin" / "python"):
        if cand.exists():
            return str(cand)
    return None


def test_wrapper_invokes_hermes_cli_main_not_bare_package() -> None:
    """The wrapper must call ``-m hermes_cli.main``, never bare ``-m hermes_cli``.

    A bare ``-m hermes_cli`` exits 1 because the package has no
    ``__main__.py``.  This guards the entry point against silent regression.
    """
    text = WRAPPER.read_text(encoding="utf-8")
    # The corrected invocation must be present.
    assert "-m hermes_cli.main" in text, (
        "wrapper must invoke '-m hermes_cli.main' (hermes_cli has no __main__.py)"
    )
    # The broken bare invocation must not remain.
    assert "-m hermes_cli " not in text and 'python -m hermes_cli kanban' not in text, (
        "wrapper must not invoke bare '-m hermes_cli kanban ...' (no __main__.py)"
    )


def test_hermes_cli_main_is_executable_entry_point() -> None:
    """``-m hermes_cli.main`` runs (exit 0 on --help); bare ``-m hermes_cli``
    must fail with the __main__ error this card fixes."""
    py = _resolve_repo_python() or sys.executable
    # Correct entry point: hermes_cli.main is executable.
    rc_ok = subprocess.run(
        [py, "-m", "hermes_cli.main", "kanban", "export-langfuse-scores", "--help"],
        cwd=str(REPO_ROOT),
        capture_output=True,
        text=True,
    )
    assert rc_ok.returncode == 0, (
        f"-m hermes_cli.main --help must exit 0; got {rc_ok.returncode}: "
        f"{rc_ok.stderr[:300]}"
    )
    # Broken entry point: bare -m hermes_cli fails (no __main__.py).
    rc_bad = subprocess.run(
        [py, "-m", "hermes_cli", "kanban", "export-langfuse-scores", "--help"],
        cwd=str(REPO_ROOT),
        capture_output=True,
        text=True,
    )
    assert rc_bad.returncode != 0, "bare -m hermes_cli must fail (no __main__.py)"
    assert "__main__" in rc_bad.stderr, (
        f"expected __main__ error on stderr; got: {rc_bad.stderr[:300]}"
    )


@pytest.mark.skipif(
    not WRAPPER.exists(),
    reason="wrapper script not present in this checkout",
)
def test_wrapper_stderr_on_missing_venv(tmp_path: Path) -> None:
    """When no repo venv python exists, the wrapper exits non-zero with a
    diagnostics line on stderr (never silently falls back to PATH python)."""
    import shutil

    # Run a COPY of the wrapper placed so script_dir/../.. is tmp_path (no
    # hermes_cli/main.py): the HERMES_AGENT_REPO fallback kicks in — a fake
    # repo that HAS hermes_cli/main.py but no venv. Running the in-repo
    # wrapper here would resolve the REAL checkout and fire a real Langfuse
    # export from a unit test — never do that.
    script_copy = tmp_path / "scripts" / "cron" / "export-langfuse-scores.sh"
    script_copy.parent.mkdir(parents=True)
    shutil.copy(WRAPPER, script_copy)
    fake_repo = tmp_path / "fakerepo"
    (fake_repo / "hermes_cli").mkdir(parents=True)
    (fake_repo / "hermes_cli" / "main.py").write_text("# fake\n")
    env = dict(os.environ)
    env["HERMES_AGENT_REPO"] = str(fake_repo)
    # Keep HOME away from the real ~/.hermes/hermes-agent fallback.
    env["HOME"] = str(tmp_path)
    env.pop("HERMES_HOME", None)
    rc = subprocess.run(
        ["bash", str(script_copy)],
        capture_output=True,
        text=True,
        env=env,
    )
    assert rc.returncode != 0, (
        "wrapper must exit non-zero when repo venv python is missing"
    )
    assert "venv" in rc.stderr.lower(), (
        f"expected venv diagnostic on stderr; got: {rc.stderr[:300]}"
    )


@pytest.mark.skipif(
    not WRAPPER.exists(),
    reason="wrapper script not present in this checkout",
)
def test_wrapper_resolves_repo_by_content_not_git(tmp_path: Path) -> None:
    """Installed at ~/.hermes/scripts, script_dir/../.. is the home dir —
    which may carry its own .git (dotfiles). The wrapper must NOT resolve
    that .git-bearing home as repo root (live failure 24.07., cron
    23717e2f32ff) but fall through to a candidate containing
    hermes_cli/main.py."""
    import shutil
    import stat

    # Installed layout: home with dotfiles .git, script under .hermes/scripts.
    home = tmp_path / "home"
    scripts_dir = home / ".hermes" / "scripts"
    scripts_dir.mkdir(parents=True)
    (home / ".git").mkdir()
    script_copy = scripts_dir / "export-langfuse-scores.sh"
    shutil.copy(WRAPPER, script_copy)
    # Content-valid fake repo with a stub venv python that just echoes args.
    fake_repo = tmp_path / "fakerepo"
    (fake_repo / "hermes_cli").mkdir(parents=True)
    (fake_repo / "hermes_cli" / "main.py").write_text("# fake\n")
    stub = fake_repo / "venv" / "bin" / "python"
    stub.parent.mkdir(parents=True)
    stub.write_text('#!/bin/sh\necho "FAKEPY $@"\nexit 0\n')
    stub.chmod(stub.stat().st_mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)
    env = dict(os.environ)
    env["HERMES_AGENT_REPO"] = str(fake_repo)
    env["HOME"] = str(home)
    env.pop("HERMES_HOME", None)
    rc = subprocess.run(
        ["bash", str(script_copy)],
        capture_output=True,
        text=True,
        env=env,
    )
    assert rc.returncode == 0, (
        f"wrapper must use the content-valid repo, not the .git home; "
        f"got {rc.returncode}: {rc.stderr[:300]}"
    )
    assert "FAKEPY -m hermes_cli.main kanban export-langfuse-scores --cron" in rc.stdout, (
        f"expected stub python invoked via -m hermes_cli.main; got: {rc.stdout[:300]}"
    )
