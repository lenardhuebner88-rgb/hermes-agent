"""Tests for scripts/check_kanban_lifecycle_anchors.py."""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

import pytest

from scripts import check_kanban_lifecycle_anchors

REPO_ROOT = Path(__file__).resolve().parents[2]
SCRIPT = REPO_ROOT / "scripts" / "check_kanban_lifecycle_anchors.py"
DOCUMENT = REPO_ROOT / "docs" / "kanban" / "LIFECYCLE.md"


def _run(document: Path = DOCUMENT, *args: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [sys.executable, str(SCRIPT), "--document", str(document), *args],
        cwd=REPO_ROOT,
        capture_output=True,
        text=True,
        check=False,
    )


def _fixture(
    tmp_path: Path,
    *,
    label: str = "dispatch_once",
    source_text: str = "def dispatch_once():\n    return None\n",
) -> tuple[Path, Path]:
    root = tmp_path / "repo"
    document = root / "docs" / "kanban" / "LIFECYCLE.md"
    source = root / "hermes_cli" / "kanban_db.py"
    document.parent.mkdir(parents=True)
    source.parent.mkdir(parents=True)
    source.write_text(source_text, encoding="utf-8")
    document.write_text(
        "# Fixture lifecycle map\n\n"
        f"[`{label}`](../../hermes_cli/kanban_db.py)\n",
        encoding="utf-8",
    )
    return document, source


def _banner_fixture(
    tmp_path: Path,
    *,
    title: str = "Schema",
) -> tuple[Path, Path]:
    root = tmp_path / "repo"
    document = root / "docs" / "kanban" / "LIFECYCLE.md"
    source = root / "hermes_cli" / "kanban_db.py"
    document.parent.mkdir(parents=True)
    source.parent.mkdir(parents=True)
    source.write_text(
        "# --------------------\n"
        f"# {title}\n"
        "# --------------------\n"
        f"{title} = object()\n",
        encoding="utf-8",
    )
    document.write_text(
        "# Fixture lifecycle map\n\n"
        "## Section index\n\n"
        "| banner title |\n"
        "|---|\n"
        f"| [{title}](../../hermes_cli/kanban_db.py) |\n\n"
        "## Details\n",
        encoding="utf-8",
    )
    return document, source


def test_committed_lifecycle_map_resolves_against_real_source() -> None:
    result = _run()

    assert result.returncode == 0, result.stderr
    _, anchors = check_kanban_lifecycle_anchors.parse_anchors(DOCUMENT)
    expected = len(anchors)
    assert expected == 145, f"lifecycle map coverage changed: {expected} anchors"
    assert f"OK: {expected} anchors resolved" in result.stdout, result.stdout


def test_symbol_anchor_survives_line_shift(tmp_path: Path) -> None:
    document, source = _fixture(tmp_path)
    source.write_text("\n" + source.read_text(encoding="utf-8"), encoding="utf-8")

    result = _run(document)

    assert result.returncode == 0, result.stderr


def test_banner_anchor_survives_line_shift(tmp_path: Path) -> None:
    document, source = _banner_fixture(tmp_path)
    source.write_text("\n" + source.read_text(encoding="utf-8"), encoding="utf-8")

    result = _run(document)

    assert result.returncode == 0, result.stderr


def test_renamed_symbol_fails_with_file_and_symbol(tmp_path: Path) -> None:
    document, source = _fixture(tmp_path)
    source.write_text(
        source.read_text(encoding="utf-8").replace(
            "dispatch_once", "dispatch_renamed"
        ),
        encoding="utf-8",
    )

    result = _run(document)

    assert result.returncode == 1
    assert "symbol 'dispatch_once' does not exist" in result.stderr
    assert "hermes_cli/kanban_db.py" in result.stderr


def test_identifier_shaped_banner_does_not_remap_to_symbol(
    tmp_path: Path,
) -> None:
    document, source = _banner_fixture(tmp_path)
    source.write_text(
        source.read_text(encoding="utf-8").replace("# Schema\n", "# Tables\n"),
        encoding="utf-8",
    )

    result = _run(document)

    assert result.returncode == 1
    assert "banner 'Schema' does not exist" in result.stderr
    assert "hermes_cli/kanban_db.py" in result.stderr


def test_symbol_mentioned_only_in_comment_or_string_does_not_resolve(
    tmp_path: Path,
) -> None:
    document, _source = _fixture(
        tmp_path,
        source_text=(
            "# dispatch_once is documented here\n"
            'MENTION = "dispatch_once"\n'
        ),
    )

    result = _run(document)

    assert result.returncode == 1
    assert "symbol 'dispatch_once' does not exist" in result.stderr


@pytest.mark.parametrize(
    "source_text",
    [
        "def target():\n    pass\n",
        "async def target():\n    pass\n",
        "class target:\n    pass\n",
        "target = object()\n",
        "target: object = object()\n",
    ],
)
def test_supported_top_level_symbol_kinds_resolve(
    tmp_path: Path,
    source_text: str,
) -> None:
    document, _source = _fixture(
        tmp_path,
        label="target",
        source_text=source_text,
    )

    result = _run(document)

    assert result.returncode == 0, result.stderr
