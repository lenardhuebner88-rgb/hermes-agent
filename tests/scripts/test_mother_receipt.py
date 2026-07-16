from __future__ import annotations

import importlib.util
from pathlib import Path

import pytest

SCRIPT = Path.home() / ".hermes" / "scripts" / "kanban-mother-receipt.py"

# The script under test lives in the operator's ~/.hermes/scripts/ and is not
# tracked in git; skip everywhere it is not installed instead of failing.
pytestmark = pytest.mark.skipif(
    not SCRIPT.is_file(), reason=f"operator script {SCRIPT} is not installed"
)


def _load_mother_receipt():
    spec = importlib.util.spec_from_file_location("kanban_mother_receipt_under_test", SCRIPT)
    assert spec is not None
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(module)
    return module


def test_mother_receipt_vault_write_creates_sanitized_file(monkeypatch, tmp_path):
    mod = _load_mother_receipt()
    monkeypatch.setattr(mod.Path, "home", lambda: tmp_path)

    written = mod.write_vault_receipt("# Receipt\nBody\n", "root/id:weird", "2026:06/25 10")

    expected_dir = tmp_path / "vault" / "03-Agents" / "Hermes" / "receipts" / "mother"
    expected_path = expected_dir / "mother-root_id_weird-2026_06_25_10.md"
    assert written == str(expected_path)
    assert expected_path.read_text(encoding="utf-8") == "# Receipt\nBody\n"


def test_mother_receipt_vault_write_fail_soft(monkeypatch, tmp_path, capsys):
    mod = _load_mother_receipt()
    monkeypatch.setattr(mod.Path, "home", lambda: tmp_path)

    def fail_write_text(self, content, encoding=None):
        raise PermissionError("permission denied")

    monkeypatch.setattr(mod.Path, "write_text", fail_write_text)

    assert mod.write_vault_receipt("content", "root", "ts") is None
    captured = capsys.readouterr()
    assert "[WARN] vault receipt write failed:" in captured.err
    assert "permission denied" in captured.err
