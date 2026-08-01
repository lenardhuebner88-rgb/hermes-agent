"""Regression coverage for slice-scoped worker gate diff bases."""

from __future__ import annotations

import os
import shutil
import sqlite3
import subprocess
from dataclasses import dataclass
from pathlib import Path

import pytest


REPO_ROOT = Path(__file__).resolve().parents[2]
SCRIPTS = REPO_ROOT / "scripts"
TASK_ID = "t_626713b9"

_REAL_SCRIPTS = (
    "affected-tests.sh",
    "affected_tests.py",
    "gate_diff_base.py",
    "run-affected.sh",
    "worker-gate-android.sh",
    "worker-gate-frontend.sh",
    "worker-gate-ruff.sh",
)
_MAPPING_MODULES = (
    "affected_test_budget.py",
    "affected_test_mapping.py",
    "symbol_test_narrowing.py",
)


@dataclass(frozen=True)
class SliceRepo:
    root: Path
    db: Path
    merge_base: str
    first_run_base: str
    frontend_head: str
    fake_bin: Path
    frontend_calls: Path
    ruff_calls: Path
    test_calls: Path


def _git(repo: Path, *args: str) -> str:
    return subprocess.run(
        [
            "git",
            "-c",
            "user.email=t@t",
            "-c",
            "user.name=t",
            "-c",
            "commit.gpgsign=false",
            *args,
        ],
        cwd=repo,
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip()


def _write_executable(path: Path, content: str) -> None:
    path.write_text(content, encoding="utf-8")
    path.chmod(0o755)


def _task_runs_db(
    path: Path,
    *,
    first_run_base: str,
    current_run_base: str,
) -> None:
    with sqlite3.connect(path) as connection:
        connection.execute(
            """
            CREATE TABLE task_runs (
                id INTEGER PRIMARY KEY,
                task_id TEXT NOT NULL,
                started_at TEXT,
                pre_run_commit_sha TEXT
            )
            """
        )
        connection.executemany(
            """
            INSERT INTO task_runs (id, task_id, started_at, pre_run_commit_sha)
            VALUES (?, ?, ?, ?)
            """,
            (
                (8911, TASK_ID, "2026-08-01T10:00:00Z", first_run_base),
                (8912, TASK_ID, "2026-08-01T10:10:00Z", current_run_base),
                (8913, TASK_ID, "2026-08-01T10:20:00Z", None),
            ),
        )


@pytest.fixture
def slice_repo(tmp_path: Path) -> SliceRepo:
    repo = tmp_path / "repo"
    scripts = repo / "scripts"
    scripts.mkdir(parents=True)
    for name in _REAL_SCRIPTS:
        shutil.copy2(SCRIPTS / name, scripts / name)
        (scripts / name).chmod(0o755)

    hermes_cli = repo / "hermes_cli"
    hermes_cli.mkdir()
    (hermes_cli / "__init__.py").write_text("", encoding="utf-8")
    for name in _MAPPING_MODULES:
        shutil.copy2(REPO_ROOT / "hermes_cli" / name, hermes_cli / name)

    config = repo / "config"
    config.mkdir()
    (config / "affected-test-exceptions.json").write_text(
        '{"schema_version": 1, "exceptions": []}\n',
        encoding="utf-8",
    )

    frontend_calls = tmp_path / "frontend-calls"
    ruff_calls = tmp_path / "ruff-calls"
    test_calls = tmp_path / "test-calls"
    _write_executable(
        scripts / "gate-frontend.sh",
        '#!/usr/bin/env bash\nprintf "%s\\n" "$*" >> "$FRONTEND_CALLS"\n',
    )
    _write_executable(scripts / "check-branch-age.sh", "#!/bin/sh\nexit 0\n")
    _write_executable(
        scripts / "run_tests.sh",
        '#!/usr/bin/env bash\nprintf "%s\\n" "$*" >> "$TEST_CALLS"\n',
    )
    _write_executable(
        scripts / "check_test_wallclock_expiry.py",
        "#!/usr/bin/env python3\nraise SystemExit(0)\n",
    )
    _write_executable(
        scripts / "gate_load_stamp.py",
        "#!/usr/bin/env python3\nraise SystemExit(0)\n",
    )
    fake_bin = tmp_path / "bin"
    fake_bin.mkdir()
    _write_executable(
        fake_bin / "ruff",
        '#!/usr/bin/env bash\nprintf "%s\\n" "$*" >> "$RUFF_CALLS"\n',
    )

    backend = repo / "pkg" / "backend.py"
    backend.parent.mkdir()
    backend.write_text("VALUE = 1\n", encoding="utf-8")
    test_backend = repo / "tests" / "pkg" / "test_backend.py"
    test_backend.parent.mkdir(parents=True)
    test_backend.write_text(
        "from pkg import backend\n\n"
        "def test_backend():\n"
        "    assert backend.VALUE > 0\n",
        encoding="utf-8",
    )
    frontend = repo / "web" / "ui.ts"
    frontend.parent.mkdir()
    frontend.write_text("export const value = 1\n", encoding="utf-8")

    _git(repo, "init", "-q", "-b", "main")
    _git(repo, "add", "-A")
    _git(repo, "commit", "-q", "-m", "main base")
    merge_base = _git(repo, "rev-parse", "HEAD")
    _git(repo, "checkout", "-q", "-b", "chain")

    backend.write_text("VALUE = 2\n", encoding="utf-8")
    _git(repo, "add", "pkg/backend.py")
    _git(repo, "commit", "-q", "-m", "previous backend slice")
    first_run_base = _git(repo, "rev-parse", "HEAD")

    frontend.write_text("export const value = 2\n", encoding="utf-8")
    _git(repo, "add", "web/ui.ts")
    _git(repo, "commit", "-q", "-m", "frontend slice")
    frontend_head = _git(repo, "rev-parse", "HEAD")

    db = tmp_path / "kanban.db"
    _task_runs_db(
        db,
        first_run_base=first_run_base,
        current_run_base=frontend_head,
    )

    return SliceRepo(
        root=repo,
        db=db,
        merge_base=merge_base,
        first_run_base=first_run_base,
        frontend_head=frontend_head,
        fake_bin=fake_bin,
        frontend_calls=frontend_calls,
        ruff_calls=ruff_calls,
        test_calls=test_calls,
    )


def _gate_env(repo: SliceRepo) -> dict[str, str]:
    env = os.environ.copy()
    env.update(
        {
            "FRONTEND_CALLS": str(repo.frontend_calls),
            "HERMES_GATE_DIFF_BASE": "",
            "HERMES_KANBAN_DB": str(repo.db),
            "HERMES_KANBAN_TASK": TASK_ID,
            "RUFF_CALLS": str(repo.ruff_calls),
            "TEST_CALLS": str(repo.test_calls),
            "PATH": f"{repo.fake_bin}{os.pathsep}{env.get('PATH', '/usr/bin:/bin')}",
        }
    )
    return env


def _run(
    repo: SliceRepo,
    script: str,
    *,
    env: dict[str, str] | None = None,
) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [str(repo.root / "scripts" / script)],
        cwd=repo.root,
        env=env or _gate_env(repo),
        capture_output=True,
        text=True,
    )


def test_resolver_uses_first_nonempty_run_and_env_wins_db(
    slice_repo: SliceRepo,
) -> None:
    env = _gate_env(slice_repo)
    resolved = _run(slice_repo, "gate_diff_base.py", env=env)

    assert resolved.returncode == 0
    assert resolved.stdout.strip() == slice_repo.first_run_base
    assert resolved.stderr == ""
    assert slice_repo.first_run_base != slice_repo.merge_base

    env["HERMES_GATE_DIFF_BASE"] = slice_repo.frontend_head
    explicit = _run(slice_repo, "gate_diff_base.py", env=env)

    assert explicit.returncode == 0
    assert explicit.stdout.strip() == slice_repo.frontend_head
    assert explicit.stderr == ""


def test_unresolvable_first_run_warns_and_allows_merge_base_fallback(
    slice_repo: SliceRepo,
    tmp_path: Path,
) -> None:
    invalid_db = tmp_path / "invalid-kanban.db"
    _task_runs_db(
        invalid_db,
        first_run_base="commit-not-in-worktree",
        current_run_base=slice_repo.frontend_head,
    )
    env = _gate_env(slice_repo)
    env["HERMES_KANBAN_DB"] = str(invalid_db)

    resolved = _run(slice_repo, "gate_diff_base.py", env=env)

    assert resolved.returncode == 0
    assert resolved.stdout == ""
    assert "not a commit" in resolved.stderr
    assert "falling back to merge-base HEAD main" in resolved.stderr


def test_frontend_only_slice_skips_python_ruff_and_android(
    slice_repo: SliceRepo,
) -> None:
    env = _gate_env(slice_repo)

    affected = _run(slice_repo, "run-affected.sh", env=env)
    ruff = _run(slice_repo, "worker-gate-ruff.sh", env=env)
    android = _run(slice_repo, "worker-gate-android.sh", env=env)
    frontend = _run(slice_repo, "worker-gate-frontend.sh", env=env)

    assert affected.returncode == 0, affected.stderr
    assert "no applicable Python production paths" in affected.stdout
    assert not slice_repo.test_calls.exists()
    assert ruff.returncode == 0, ruff.stderr
    assert "no changed .py files" in ruff.stdout
    assert not slice_repo.ruff_calls.exists()
    assert android.returncode == 0, android.stderr
    assert "no Android changes" in android.stdout
    assert frontend.returncode == 0, frontend.stderr
    assert slice_repo.frontend_calls.read_text(encoding="utf-8").splitlines() == [
        "--skip-build"
    ]


def test_missing_task_anchor_preserves_merge_base_selection(
    slice_repo: SliceRepo,
) -> None:
    env = _gate_env(slice_repo)
    env["HERMES_GATE_DIFF_BASE"] = ""
    env["HERMES_KANBAN_DB"] = str(slice_repo.db.parent / "missing.db")
    env["HERMES_KANBAN_TASK"] = ""

    affected = _run(slice_repo, "affected-tests.sh", env=env)

    assert affected.returncode == 0, affected.stderr
    assert affected.stdout.strip() == "tests/pkg/test_backend.py"


def test_mixed_slice_runs_python_ruff_and_frontend_gates(
    slice_repo: SliceRepo,
) -> None:
    backend = slice_repo.root / "pkg" / "backend.py"
    backend.write_text("VALUE = 3\n", encoding="utf-8")
    _git(slice_repo.root, "add", "pkg/backend.py")
    _git(slice_repo.root, "commit", "-q", "-m", "mixed backend change")
    env = _gate_env(slice_repo)

    affected = _run(slice_repo, "run-affected.sh", env=env)
    ruff = _run(slice_repo, "worker-gate-ruff.sh", env=env)
    frontend = _run(slice_repo, "worker-gate-frontend.sh", env=env)

    assert affected.returncode == 0, affected.stderr
    assert slice_repo.test_calls.read_text(encoding="utf-8").splitlines() == [
        "tests/pkg/test_backend.py"
    ]
    assert ruff.returncode == 0, ruff.stderr
    assert slice_repo.ruff_calls.read_text(encoding="utf-8").splitlines() == [
        f"check {backend}"
    ]
    assert frontend.returncode == 0, frontend.stderr
    assert slice_repo.frontend_calls.read_text(encoding="utf-8").splitlines() == [
        "--skip-build"
    ]
