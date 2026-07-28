import json
import re
import sys
from pathlib import Path

import pytest

import skill_hygiene_audit as audit_module

# Repo root: tests/ lives one level below the checkout root.
_REPO_ROOT = Path(__file__).resolve().parents[1]

# Hardcoded release-venv used as a pytest *runner* (copy-paste form agents execute).
# Matches `venv/bin/python -m pytest` and the split form with a trailing `\`,
# absolute `…/venv/bin/python` included. Does NOT match:
#   - `.venv/bin/python …` (test env; broader "no path at all" gate = later slice)
#   - runtime uses (`-c`, `-m hermes_cli.main`, `-m loops.runner`)
#   - prose that merely *mentions* the anti-pattern without a shell continuation
# Negative lookbehind: not preceded by `.` or word char.
_VENV_PYTEST_INVOCATION = re.compile(
    r"(?<![.\w])venv/bin/python\b"
    r"(?:"
    r"[^\n\\]*-m\s+pytest\b"  # same line
    r"|"
    r"[^\n]*\\\s*\n[^\n]*-m\s+pytest\b"  # backslash continuation
    r")",
    re.MULTILINE,
)


def _iter_prompt_and_skill_files() -> list[Path]:
    files: list[Path] = []
    packs = _REPO_ROOT / "loops" / "packs"
    if packs.is_dir():
        files.extend(sorted(packs.rglob("*PROMPT.md")))
    skills = _REPO_ROOT / ".claude" / "skills"
    if skills.is_dir():
        files.extend(sorted(skills.rglob("SKILL.md")))
    return files


def _find_venv_pytest_invocations(text: str) -> list[str]:
    """Return 'start_line: snippet' for each hardcoded venv→pytest invocation."""
    hits: list[str] = []
    for match in _VENV_PYTEST_INVOCATION.finditer(text):
        start_line = text.count("\n", 0, match.start()) + 1
        snippet = " ".join(match.group(0).split())
        if len(snippet) > 160:
            snippet = snippet[:157] + "..."
        hits.append(f"{start_line}: {snippet}")
    return hits


def test_packs_and_skills_do_not_hardcode_venv_for_pytest():
    """Real tree: no pack/skill may invoke pytest via a hardcoded venv path.

    Canonical runner is scripts/run_tests.sh (selects the interpreter).
    Anti-scope must stay green: runtime `venv/bin/python -c` / `-m hermes_cli`
    / `-m loops.runner`, and trap-prose that only *names* the anti-pattern.
    """
    scanned = _iter_prompt_and_skill_files()
    assert scanned, "expected pack PROMPT.md and skill SKILL.md files under repo root"

    offenders: list[str] = []
    for path in scanned:
        rel = path.relative_to(_REPO_ROOT)
        for hit in _find_venv_pytest_invocations(path.read_text(encoding="utf-8")):
            offenders.append(f"{rel}:{hit}")

    assert not offenders, (
        "hardcoded venv interpreter used for pytest — use scripts/run_tests.sh:\n"
        + "\n".join(offenders)
    )


# pytest-timeout is not installed; any `--timeout=<n>` / `--timeout <n>` aborts the run
# before a test starts. Match only the *invocation* form (flag + value). Trap-prose that
# names the flag without a value (e.g. "do not use pytest's `--timeout`") stays green.
_PYTEST_TIMEOUT_FLAG = re.compile(r"--timeout(?:=\S+|\s+\S+)")


def _find_pytest_timeout_flags(text: str) -> list[str]:
    """Return 'start_line: snippet' for each pytest --timeout=<value> style flag."""
    hits: list[str] = []
    for match in _PYTEST_TIMEOUT_FLAG.finditer(text):
        start_line = text.count("\n", 0, match.start()) + 1
        snippet = " ".join(match.group(0).split())
        if len(snippet) > 160:
            snippet = snippet[:157] + "..."
        hits.append(f"{start_line}: {snippet}")
    return hits


def test_packs_and_skills_do_not_pass_pytest_timeout_flag():
    """Real tree: no pack/skill may pass pytest's --timeout flag with a value.

    pytest-timeout is not installed; the flag aborts with unrecognized arguments.
    Per-file caps live in scripts/run_tests.sh (HERMES_TEST_FILE_TIMEOUT / --file-timeout).
    Trap-prose that only *names* the flag without a value must stay green.
    """
    scanned = _iter_prompt_and_skill_files()
    assert scanned, "expected pack PROMPT.md and skill SKILL.md files under repo root"

    offenders: list[str] = []
    for path in scanned:
        rel = path.relative_to(_REPO_ROOT)
        for hit in _find_pytest_timeout_flags(path.read_text(encoding="utf-8")):
            offenders.append(f"{rel}:{hit}")

    assert not offenders, (
        "pytest --timeout flag is not available (pytest-timeout not installed); "
        "use the runner per-file timeout instead:\n"
        + "\n".join(offenders)
    )


def _write_skill(root: Path, content: str) -> None:
    skill_dir = root / "audit-skill"
    skill_dir.mkdir(parents=True)
    (skill_dir / "SKILL.md").write_text(content, encoding="utf-8")


def test_redacts_matched_values_from_json_and_markdown(monkeypatch, tmp_path):
    root = tmp_path / "skills"
    api_key = "supersecretvalue123"
    email = "owner@example.test"
    _write_skill(
        root,
        f"""---
name: \"api_key: {api_key}\"
description: Contact {email}
---
See [private contact](contact-{email}.md).
""",
    )
    monkeypatch.setattr(audit_module, "roots", lambda: [("test", root)])

    output_dir = tmp_path / "output"
    output_dir.mkdir()
    results_path = output_dir / "results.json"
    report_path = output_dir / "report.md"
    monkeypatch.setattr(sys, "argv", ["audit", "--results", str(results_path), "--report", str(report_path)])

    audit_module.main()

    result = json.loads(results_path.read_text(encoding="utf-8"))
    serialized = json.dumps(result, ensure_ascii=False)
    report = report_path.read_text(encoding="utf-8")

    assert result["redacted_pattern_summary"]["secret_types"] == {"api_key_assignment": 1}
    assert result["redacted_pattern_summary"]["pii_types"] == {"email_address": 2}
    assert api_key not in serialized
    assert email not in serialized
    assert api_key not in report
    assert email not in report


def test_rejects_output_paths_inside_roots_or_each_other(monkeypatch, tmp_path):
    root = tmp_path / "skills"
    root.mkdir()
    outside = tmp_path / "outside"
    outside.mkdir()
    monkeypatch.setattr(audit_module, "roots", lambda: [("test", root)])

    in_root_results = root / "results.json"
    monkeypatch.setattr(
        sys,
        "argv",
        ["audit", "--results", str(in_root_results), "--report", str(outside / "report.md")],
    )
    with pytest.raises(ValueError, match="innerhalb einer Audit-Wurzel"):
        audit_module.main()
    assert not in_root_results.exists()

    same_output = outside / "same.json"
    monkeypatch.setattr(sys, "argv", ["audit", "--results", str(same_output), "--report", str(same_output)])
    with pytest.raises(ValueError, match="verschieden"):
        audit_module.main()
    assert not same_output.exists()

    audit_module.validate_output_paths(outside / "results.json", outside / "report.md", [("test", root)])
