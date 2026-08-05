from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

from loops.runner import PACKS_DIR, load_pack
from scripts.dead_code_candidates import StaticFinding, build_report, path_is_allowed


PACK_NAME = "dead-code-scout"
REPO_ROOT = Path(__file__).resolve().parents[2]
ALLOWLIST = REPO_ROOT / "scripts" / "dead_code_revier_allowlist.txt"
SCANNER = REPO_ROOT / "scripts" / "dead_code_candidates.py"


def _finding(path: str, line: int, name: str) -> StaticFinding:
    return StaticFinding(
        path=path,
        line=line,
        name=name,
        kind="function",
        confidence=60,
        static_evidence=f"{path}:{line}: unused function '{name}' (60% confidence)",
    )


def test_manifest_is_report_only_sweep_with_checked_in_scope():
    pack = load_pack(PACKS_DIR, PACK_NAME)

    assert pack.type == "sweep"
    assert list(pack.phases) == ["round"]
    assert pack.autoland is False
    assert pack.params["scanner"] == "scripts/dead_code_candidates.py"
    assert pack.params["allowlist"] == "scripts/dead_code_revier_allowlist.txt"
    assert pack.params["report"] == "dead-code-candidates.json"

    prompt = (PACKS_DIR / PACK_NAME / "ROUND-PROMPT.md").read_text(encoding="utf-8")
    assert "NICHTS loeschen" in prompt
    assert "zwei Belege" in prompt
    assert "python -m" in prompt
    assert "scripts/dead_code_revier_allowlist.txt" in prompt


def test_checked_in_allowlist_is_fork_only_and_excludes_known_upstream_web_files():
    entries = {
        line.strip()
        for line in ALLOWLIST.read_text(encoding="utf-8").splitlines()
        if line.strip() and not line.lstrip().startswith("#")
    }

    assert {"web/src/control/", "loops/", "scripts/dead_code_candidates.py"} <= entries
    assert "hermes_cli/" not in entries
    assert "scripts/" not in entries
    assert "hermes_cli/landing_loop.py" in entries
    for forbidden in (
        "web/src/App.tsx",
        "web/src/main.tsx",
        "web/index.html",
        "web/vite.config.ts",
    ):
        assert not path_is_allowed(forbidden, entries)


def test_report_keeps_only_allowlisted_findings_without_repository_references(
    tmp_path: Path,
):
    (tmp_path / "loops").mkdir()
    (tmp_path / "web" / "src").mkdir(parents=True)
    (tmp_path / "loops" / "unused.py").write_text(
        "def genuinely_unused():\n    return 1\n",
        encoding="utf-8",
    )
    (tmp_path / "web" / "src" / "App.tsx").write_text(
        "const upstreamOnly = 1;\n",
        encoding="utf-8",
    )
    allowlist = tmp_path / "allowlist.txt"
    allowlist.write_text("loops/\n", encoding="utf-8")

    report = build_report(
        repo_root=tmp_path,
        allowlist_path=allowlist,
        findings=[
            _finding("loops/unused.py", 1, "genuinely_unused"),
            _finding("web/src/App.tsx", 1, "upstreamOnly"),
        ],
    )

    assert [(item["path"], item["name"]) for item in report["candidates"]] == [
        ("loops/unused.py", "genuinely_unused")
    ]
    rejected = {(item["path"], item["reason"]) for item in report["rejected"]}
    assert ("web/src/App.tsx", "outside_allowlist") in rejected
    assert report["candidates"][0]["repo_search_evidence"]["matches"] == []


def test_python_m_reference_in_pack_yaml_rejects_static_finding(tmp_path: Path):
    (tmp_path / "loops").mkdir()
    (tmp_path / "loops" / "invoked_only.py").write_text(
        "def dead_entry():\n    return 1\n",
        encoding="utf-8",
    )
    (tmp_path / "pack.yaml").write_text(
        'command: "python -m loops.invoked_only"\n',
        encoding="utf-8",
    )
    allowlist = tmp_path / "allowlist.txt"
    allowlist.write_text("loops/\n", encoding="utf-8")

    report = build_report(
        repo_root=tmp_path,
        allowlist_path=allowlist,
        findings=[_finding("loops/invoked_only.py", 1, "dead_entry")],
    )

    assert report["candidates"] == []
    assert report["rejected"][0]["reason"] == "repository_reference"
    matches = report["rejected"][0]["repo_search_evidence"]["matches"]
    assert any(
        match["path"] == "pack.yaml" and match["needle"] == "loops.invoked_only"
        for match in matches
    )


def test_cli_runs_pinned_vulture_and_writes_machine_readable_report(tmp_path: Path):
    repo = tmp_path / "repo"
    (repo / "loops").mkdir(parents=True)
    (repo / "loops" / "sample.py").write_text(
        "def cli_unused_symbol():\n    return 1\n",
        encoding="utf-8",
    )
    allowlist = repo / "allowlist.txt"
    allowlist.write_text("loops/\n", encoding="utf-8")
    output = tmp_path / "report.json"

    result = subprocess.run(
        [
            sys.executable,
            str(SCANNER),
            "--repo-root",
            str(repo),
            "--allowlist",
            str(allowlist),
            "--output",
            str(output),
        ],
        cwd=REPO_ROOT,
        text=True,
        capture_output=True,
        check=False,
    )

    assert result.returncode == 0, result.stderr
    payload = json.loads(output.read_text(encoding="utf-8"))
    assert payload["static_tool"] == {"name": "vulture", "version": "2.14"}
    assert [(item["path"], item["name"]) for item in payload["candidates"]] == [
        ("loops/sample.py", "cli_unused_symbol")
    ]
