from pathlib import Path


def test_e2e_probe_sentinel_file_contains_expected_line() -> None:
    sentinel_path = Path(__file__).resolve().parents[1] / "docs" / "_e2e-probe.md"

    assert "E2E-PROBE-SENTINEL-2026-06-21" in sentinel_path.read_text(encoding="utf-8")
