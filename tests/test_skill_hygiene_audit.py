import json
import sys
from pathlib import Path

import pytest

import skill_hygiene_audit as audit_module


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
