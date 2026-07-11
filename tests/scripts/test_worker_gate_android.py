"""Regression tests for the self-skipping Android worker gate."""

from __future__ import annotations

import shutil
import subprocess
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
SCRIPT = REPO_ROOT / "scripts" / "worker-gate-android.sh"
GRADLEW_STUB = """#!/usr/bin/env bash
echo "$PWD|$*" >> "$GRADLE_CALLS"
"""


def _git(repo: Path, *args: str) -> None:
    subprocess.run(
        ["git", "-c", "user.email=t@t", "-c", "user.name=t", "-c", "commit.gpgsign=false", *args],
        cwd=repo,
        check=True,
        capture_output=True,
        text=True,
    )


def _make_repo(tmp_path: Path) -> tuple[Path, Path]:
    repo = tmp_path / "repo"
    scripts = repo / "scripts"
    scripts.mkdir(parents=True)
    shutil.copy2(SCRIPT, scripts / SCRIPT.name)
    (scripts / SCRIPT.name).chmod(0o755)

    for project in ("hermes-voice", "hermes-dictate"):
        source = repo / "android" / project / "app" / "src" / "main" / "kotlin"
        source.mkdir(parents=True)
        (source / "App.kt").write_text("class App\n")
        gradlew = repo / "android" / project / "gradlew"
        gradlew.write_text(GRADLEW_STUB)
        gradlew.chmod(0o755)

    _git(repo, "init", "-q")
    _git(repo, "add", "-A")
    _git(repo, "commit", "-q", "-m", "init")
    return repo, tmp_path / "gradle-calls"


def _run_gate(repo: Path, calls: Path, ref: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [str(repo / "scripts" / SCRIPT.name), ref],
        cwd=repo,
        env={"PATH": "/usr/bin:/bin", "GRADLE_CALLS": str(calls)},
        capture_output=True,
        text=True,
    )


def test_empty_diff_skips_without_gradle(tmp_path: Path) -> None:
    repo, calls = _make_repo(tmp_path)

    proc = _run_gate(repo, calls, "HEAD")

    assert proc.returncode == 0, proc.stderr
    assert "skipping Kotlin compile" in proc.stdout
    assert not calls.exists()


def test_voice_kotlin_diff_runs_compile_debug_kotlin(tmp_path: Path) -> None:
    repo, calls = _make_repo(tmp_path)
    source = repo / "android" / "hermes-voice" / "app" / "src" / "main" / "kotlin" / "App.kt"
    source.write_text("class AppChanged\n")

    proc = _run_gate(repo, calls, "HEAD")

    assert proc.returncode == 0, proc.stderr
    assert calls.read_text().splitlines() == [
        f"{repo}/android/hermes-voice|:app:compileDebugKotlin"
    ]


def test_both_projects_compile_when_both_are_affected(tmp_path: Path) -> None:
    repo, calls = _make_repo(tmp_path)
    for project in ("hermes-voice", "hermes-dictate"):
        source = repo / "android" / project / "app" / "src" / "main" / "kotlin" / "App.kt"
        source.write_text("class AppChanged\n")

    proc = _run_gate(repo, calls, "HEAD")

    assert proc.returncode == 0, proc.stderr
    assert calls.read_text().splitlines() == [
        f"{repo}/android/hermes-dictate|:app:compileDebugKotlin",
        f"{repo}/android/hermes-voice|:app:compileDebugKotlin",
    ]


def test_missing_gradle_wrapper_fails_clearly(tmp_path: Path) -> None:
    repo, calls = _make_repo(tmp_path)
    source = repo / "android" / "hermes-voice" / "app" / "src" / "main" / "kotlin" / "App.kt"
    source.write_text("class AppChanged\n")
    (repo / "android" / "hermes-voice" / "gradlew").unlink()

    proc = _run_gate(repo, calls, "HEAD")

    assert proc.returncode == 1
    assert "missing Gradle wrapper" in proc.stderr
    assert not calls.exists()
