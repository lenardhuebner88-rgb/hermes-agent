from __future__ import annotations

import importlib.util
import subprocess
from pathlib import Path

import pytest


ROOT = Path(__file__).resolve().parents[2]
SCRIPT_PATH = ROOT / "scripts" / "check_test_wallclock_expiry.py"


def _load_checker():
    spec = importlib.util.spec_from_file_location("check_test_wallclock_expiry", SCRIPT_PATH)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.fixture
def checker():
    return _load_checker()


def _write_product(root: Path) -> None:
    product = root / "hermes_cli" / "control_loops.py"
    product.parent.mkdir(parents=True)
    product.write_text(
        '''from datetime import datetime, timezone

LANDING_PREVIEW_STALE_SECONDS = 900


def _landing_preview_status(state):
    started = datetime.fromisoformat(str(state.get("started_at")))
    age = (datetime.now(timezone.utc) - started).total_seconds()
    return age <= LANDING_PREVIEW_STALE_SECONDS
''',
        encoding="utf-8",
    )


def _write_test(root: Path, source: str) -> Path:
    path = root / "tests" / "test_landing_preview.py"
    path.parent.mkdir(parents=True)
    path.write_text(source, encoding="utf-8")
    return path


def test_reports_the_original_landing_preview_timebomb(checker, tmp_path):
    _write_product(tmp_path)
    path = _write_test(
        tmp_path,
        '''import json
from datetime import datetime, timezone


def test_landing_preview_route_lifecycle(tmp_path, monkeypatch):
    def fake_spawn(root):
        started_at = datetime(2026, 7, 31, 5, 0, tzinfo=timezone.utc).isoformat()
        root.mkdir(parents=True, exist_ok=True)
        (root / "preview-state.json").write_text(
            json.dumps({"started_at": started_at}), encoding="utf-8"
        )
        return started_at
''',
    )

    hazards = checker.find_wallclock_expiry_hazards(tmp_path, [path])

    assert len(hazards) == 1
    assert hazards[0].test_name == "test_landing_preview_route_lifecycle"
    assert hazards[0].state_key == "started_at"
    assert hazards[0].line == 7


def test_allows_fixed_time_when_product_clock_is_injected(checker, tmp_path):
    _write_product(tmp_path)
    path = _write_test(
        tmp_path,
        '''import json
from datetime import datetime, timezone


def test_preview_at_known_time(tmp_path):
    now = datetime(2026, 7, 31, 5, 0, tzinfo=timezone.utc)
    (tmp_path / "state.json").write_text(
        json.dumps({"started_at": now.isoformat()}), encoding="utf-8"
    )
    assert _landing_preview_status(tmp_path, now=now)["running"] is True
''',
    )

    assert checker.find_wallclock_expiry_hazards(tmp_path, [path]) == []


def test_allows_wall_clock_derived_state(checker, tmp_path):
    _write_product(tmp_path)
    path = _write_test(
        tmp_path,
        '''import json
from datetime import datetime, timezone


def test_preview_now(tmp_path):
    started_at = datetime.now(timezone.utc).isoformat()
    (tmp_path / "state.json").write_text(
        json.dumps({"started_at": started_at}), encoding="utf-8"
    )
''',
    )

    assert checker.find_wallclock_expiry_hazards(tmp_path, [path]) == []


def test_ignores_fixed_timestamp_not_compared_with_wall_clock(checker, tmp_path):
    _write_product(tmp_path)
    path = _write_test(
        tmp_path,
        '''import json
from datetime import datetime, timezone


def test_preview_result(tmp_path):
    finished_at = datetime(2026, 7, 31, 5, 1, tzinfo=timezone.utc).isoformat()
    (tmp_path / "done.json").write_text(
        json.dumps({"finished_at": finished_at}), encoding="utf-8"
    )
''',
    )

    assert checker.find_wallclock_expiry_hazards(tmp_path, [path]) == []


def test_ignores_product_code_copied_into_foreign_worktree(checker, tmp_path):
    subprocess.run(["git", "init", "-q", str(tmp_path)], check=True)
    tracked = tmp_path / "hermes_cli" / "placeholder.py"
    tracked.parent.mkdir(parents=True)
    tracked.write_text("VALUE = 1\n", encoding="utf-8")
    subprocess.run(
        ["git", "-C", str(tmp_path), "add", "hermes_cli/placeholder.py"],
        check=True,
    )

    foreign_product = (
        tmp_path
        / ".worktrees"
        / "stale-branch"
        / "hermes_cli"
        / "control_loops.py"
    )
    foreign_product.parent.mkdir(parents=True)
    foreign_product.write_text(
        '''from datetime import datetime, timezone


def _landing_preview_status(state):
    started = datetime.fromisoformat(str(state.get("started_at")))
    return datetime.now(timezone.utc) > started
''',
        encoding="utf-8",
    )
    path = _write_test(
        tmp_path,
        '''import json
from datetime import datetime, timezone


def test_preview(tmp_path):
    started_at = datetime(2026, 7, 31, 5, 0, tzinfo=timezone.utc).isoformat()
    (tmp_path / "state.json").write_text(
        json.dumps({"started_at": started_at}), encoding="utf-8"
    )
''',
    )

    assert checker.find_wallclock_expiry_hazards(tmp_path, [path]) == []
