"""Tests for scripts/check_skill_hygiene.py.

Real-tree regression plus synthetic violations and false-positive classes.
"""

from __future__ import annotations

import os
import stat
import subprocess
import sys
from pathlib import Path

from scripts import check_skill_hygiene as hygiene

REPO_ROOT = Path(__file__).resolve().parents[2]
SCRIPT = REPO_ROOT / "scripts" / "check_skill_hygiene.py"


def _run(*args: str, cwd: Path = REPO_ROOT) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [sys.executable, str(SCRIPT), *args],
        cwd=cwd,
        capture_output=True,
        text=True,
        check=False,
    )


def _write_skill(root: Path, body: str, name: str = "fixture-skill") -> Path:
    skill = root / ".claude" / "skills" / name
    skill.mkdir(parents=True, exist_ok=True)
    path = skill / "SKILL.md"
    path.write_text(body, encoding="utf-8")
    return path


def _write_prompt(root: Path, body: str, pack: str = "fixture-pack") -> Path:
    pack_dir = root / "loops" / "packs" / pack
    pack_dir.mkdir(parents=True, exist_ok=True)
    path = pack_dir / "ROUND-PROMPT.md"
    path.write_text(body, encoding="utf-8")
    return path


def _seed_minimal_tree(root: Path) -> None:
    """Create enough of a repo that path checks resolve against *root*."""
    (root / "scripts").mkdir(parents=True, exist_ok=True)
    (root / "web" / "src" / "control").mkdir(parents=True, exist_ok=True)
    (root / "package-lock.json").write_text("{}\n", encoding="utf-8")
    runner = root / "scripts" / "run_tests.sh"
    runner.write_text("#!/bin/sh\n", encoding="utf-8")
    runner.chmod(runner.stat().st_mode | stat.S_IXUSR)


# ---------------------------------------------------------------------------
# Real tree — regression shield
# ---------------------------------------------------------------------------


def test_real_tree_is_clean() -> None:
    result = _run("--root", str(REPO_ROOT))
    assert result.returncode == 0, result.stdout + result.stderr
    assert "skill/prompt files clean" in result.stdout


# ---------------------------------------------------------------------------
# One synthetic violation per rule → must go red
# ---------------------------------------------------------------------------


def test_rule_venv_pytest_is_detected(tmp_path: Path) -> None:
    _seed_minimal_tree(tmp_path)
    _write_skill(
        tmp_path,
        "---\nname: x\ndescription: x\n---\n"
        "Run with `venv/bin/python -m pytest tests/foo.py`.\n",
    )
    findings = hygiene.scan_tree(tmp_path)
    assert any(f.rule == "no-venv-pytest" for f in findings), findings
    assert _run("--root", str(tmp_path)).returncode == 1


def test_rule_pytest_timeout_is_detected(tmp_path: Path) -> None:
    _seed_minimal_tree(tmp_path)
    _write_prompt(
        tmp_path,
        "Use scripts/run_tests.sh, never `pytest --timeout=60 tests/`.\n",
    )
    findings = hygiene.scan_tree(tmp_path)
    assert any(f.rule == "no-pytest-timeout" for f in findings), findings


def test_rule_dead_path_is_detected(tmp_path: Path) -> None:
    _seed_minimal_tree(tmp_path)
    _write_skill(
        tmp_path,
        "---\nname: x\ndescription: x\n---\n"
        "Do not touch `web/package-lock.json`.\n",
    )
    findings = hygiene.scan_tree(tmp_path)
    dead = [f for f in findings if f.rule == "path-exists"]
    assert dead, findings
    assert any("web/package-lock.json" in f.message for f in dead)


def test_rule_non_executable_script_is_detected(tmp_path: Path) -> None:
    _seed_minimal_tree(tmp_path)
    script = tmp_path / "scripts" / "helper.sh"
    script.write_text("#!/bin/sh\necho hi\n", encoding="utf-8")
    # deliberately no +x
    script.chmod(stat.S_IRUSR | stat.S_IWUSR)
    _write_skill(
        tmp_path,
        "---\nname: x\ndescription: x\n---\n"
        "Run `scripts/helper.sh` after the change.\n",
    )
    findings = hygiene.scan_tree(tmp_path)
    assert any(
        f.rule == "script-executable" and "helper.sh" in f.message for f in findings
    ), findings


# ---------------------------------------------------------------------------
# False-positive classes from the brief §3 — must stay green
# ---------------------------------------------------------------------------


def test_false_positive_german_web_beruehrende(tmp_path: Path) -> None:
    """``web/-berührende`` must not be parsed as a path."""
    _seed_minimal_tree(tmp_path)
    _write_prompt(
        tmp_path,
        "Keine web/-berührende Änderungen ohne Planer-Freigabe.\n",
    )
    findings = [f for f in hygiene.scan_tree(tmp_path) if f.rule == "path-exists"]
    assert findings == []


def test_false_positive_german_control_anteil(tmp_path: Path) -> None:
    """``web/src/control-Anteil`` is a German compound, not a path."""
    _seed_minimal_tree(tmp_path)
    _write_prompt(
        tmp_path,
        "Der web/src/control-Anteil des Diffs muss klein bleiben.\n",
    )
    findings = [f for f in hygiene.scan_tree(tmp_path) if f.rule == "path-exists"]
    assert findings == []


def test_false_positive_package_glob_and_suffix(tmp_path: Path) -> None:
    """``web/package*.json`` and ``web/package.json-…`` must not fire."""
    _seed_minimal_tree(tmp_path)
    (tmp_path / "web" / "package.json").write_text("{}\n", encoding="utf-8")
    _write_prompt(
        tmp_path,
        "kein `web/package*.json`, keine web/package.json-Abhängigkeiten ändern.\n",
    )
    findings = [f for f in hygiene.scan_tree(tmp_path) if f.rule == "path-exists"]
    assert findings == []


def test_false_positive_gateway_dashboard_prose(tmp_path: Path) -> None:
    """Slash-joined service names are not a filesystem path."""
    _seed_minimal_tree(tmp_path)
    _write_skill(
        tmp_path,
        "---\nname: x\ndescription: x\n---\n"
        "NEVER restart gateway/dashboard while foreign edits sit uncommitted.\n",
    )
    findings = [f for f in hygiene.scan_tree(tmp_path) if f.rule == "path-exists"]
    assert findings == []


def test_false_positive_existing_directory_is_ok(tmp_path: Path) -> None:
    """Directory mentions pass via is_dir(), not only is_file()."""
    _seed_minimal_tree(tmp_path)
    _write_skill(
        tmp_path,
        "---\nname: x\ndescription: x\n---\n"
        "Work under `web/src/control` only.\n",
    )
    findings = [f for f in hygiene.scan_tree(tmp_path) if f.rule == "path-exists"]
    assert findings == []


def test_false_positive_absolute_and_home_paths_ignored(tmp_path: Path) -> None:
    _seed_minimal_tree(tmp_path)
    _write_skill(
        tmp_path,
        "---\nname: x\ndescription: x\n---\n"
        "See `/home/piet/vault/00-Canon/foo.md` and `~/.hermes/config.yaml`.\n",
    )
    findings = [f for f in hygiene.scan_tree(tmp_path) if f.rule == "path-exists"]
    assert findings == []


def test_false_positive_urls_ignored(tmp_path: Path) -> None:
    _seed_minimal_tree(tmp_path)
    _write_prompt(
        tmp_path,
        "Docs: https://example.com/web/package-lock.json is external.\n",
    )
    findings = [f for f in hygiene.scan_tree(tmp_path) if f.rule == "path-exists"]
    assert findings == []


def test_false_positive_placeholders_and_globs_ignored(tmp_path: Path) -> None:
    _seed_minimal_tree(tmp_path)
    _write_skill(
        tmp_path,
        "---\nname: x\ndescription: x\n---\n"
        "Bridge worktree (`.claude/worktrees/bridge-*`) and `<repo>/web/foo`.\n"
        "Also `scripts/$VAR/helper.sh` and `web/{{name}}/x.ts`.\n",
    )
    findings = [f for f in hygiene.scan_tree(tmp_path) if f.rule == "path-exists"]
    assert findings == []


# ---------------------------------------------------------------------------
# Runtime exception + --fix
# ---------------------------------------------------------------------------


def test_web_node_modules_is_excepted(tmp_path: Path) -> None:
    _seed_minimal_tree(tmp_path)
    # deliberately do NOT create web/node_modules
    _write_skill(
        tmp_path,
        "---\nname: x\ndescription: x\n---\n"
        "If `web/node_modules` is missing, run the gate once.\n",
    )
    findings = [f for f in hygiene.scan_tree(tmp_path) if f.rule == "path-exists"]
    assert findings == []
    assert "web/node_modules" in hygiene._PATH_EXCEPTION_MAP


def test_fix_rewrites_package_lock_and_rerun_is_green(tmp_path: Path) -> None:
    _seed_minimal_tree(tmp_path)
    path = _write_prompt(
        tmp_path,
        "Anti-scope: `web/package-lock.json` and also web/package-lock.json again.\n",
    )
    fixed = _run("--root", str(tmp_path), "--fix")
    assert fixed.returncode == 0, fixed.stdout + fixed.stderr
    text = path.read_text(encoding="utf-8")
    assert "web/package-lock.json" not in text
    assert text.count("package-lock.json") == 2
    assert "fixed" in fixed.stdout


def test_runtime_venv_python_c_is_not_venv_pytest(tmp_path: Path) -> None:
    """loop-schmiede style: venv/bin/python -c is runtime, not pytest."""
    _seed_minimal_tree(tmp_path)
    _write_prompt(
        tmp_path,
        "Smoke: `venv/bin/python -c 'import loops.runner'`.\n",
    )
    findings = [f for f in hygiene.scan_tree(tmp_path) if f.rule == "no-venv-pytest"]
    assert findings == []


def test_timeout_named_without_value_is_green(tmp_path: Path) -> None:
    _seed_minimal_tree(tmp_path)
    _write_skill(
        tmp_path,
        "---\nname: x\ndescription: x\n---\n"
        "Never pass pytest's `--timeout` flag (pytest-timeout is not installed).\n",
    )
    findings = [f for f in hygiene.scan_tree(tmp_path) if f.rule == "no-pytest-timeout"]
    assert findings == []


def test_finding_format_shape() -> None:
    finding = hygiene.Finding(
        Path("loops/packs/x/ROUND-PROMPT.md"),
        12,
        "path-exists",
        "repo-relative path does not exist: web/nope.json",
    )
    assert finding.format() == (
        "loops/packs/x/ROUND-PROMPT.md:12: path-exists: "
        "repo-relative path does not exist: web/nope.json"
    )


def test_executable_script_passes(tmp_path: Path) -> None:
    _seed_minimal_tree(tmp_path)
    script = tmp_path / "scripts" / "helper.sh"
    script.write_text("#!/bin/sh\n", encoding="utf-8")
    os.chmod(script, script.stat().st_mode | stat.S_IXUSR)
    _write_skill(
        tmp_path,
        "---\nname: x\ndescription: x\n---\nRun `scripts/helper.sh`.\n",
    )
    findings = [f for f in hygiene.scan_tree(tmp_path) if f.rule == "script-executable"]
    assert findings == []
