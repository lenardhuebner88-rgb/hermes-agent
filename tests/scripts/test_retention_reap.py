from __future__ import annotations

import json
import multiprocessing
import os
import time
from pathlib import Path

from scripts import retention_reap as rr

DAY = 24 * 60 * 60
NOW = 2_000_000_000.0


def _age(path: Path, days: float) -> None:
    stamp = NOW - days * DAY
    os.utime(path, (stamp, stamp))


def _package_tree(root: Path, browsers: list[dict[str, object]] | None) -> None:
    package = root / "node_modules" / "playwright-core"
    package.mkdir(parents=True)
    (package / "package.json").write_text(json.dumps({"name": "playwright-core", "version": "1.2.3"}))
    if browsers is not None:
        (package / "browsers.json").write_text(json.dumps({"browsers": browsers}))


def _python_package_tree(root: Path, browsers: list[dict[str, object]]) -> Path:
    package = root / ".venv" / "lib" / "python3.12" / "site-packages" / "playwright" / "driver" / "package"
    package.mkdir(parents=True)
    (package / "browsers.json").write_text(json.dumps({"browsers": browsers}))
    return package


def test_outputs_only_regular_files_strictly_older_than_fourteen_days(tmp_path):
    root = tmp_path / "outputs"
    root.mkdir()
    old = root / "old.png"
    boundary = root / "boundary.png"
    fresh = root / "fresh.png"
    nested = root / "directory"
    for path in (old, boundary, fresh):
        path.write_bytes(b"123")
    nested.mkdir()
    old_inside = nested / "old-inside.txt"
    old_inside.write_text("remove file, keep directory")
    _age(old, 14 + 1 / DAY)
    _age(boundary, 14)
    _age(fresh, 2)
    _age(old_inside, 20)

    actions = rr.plan_output_actions(root, now=NOW, max_age_seconds=14 * DAY)

    assert [action.path for action in actions] == [old_inside, old]
    assert all(action.path != nested for action in actions)


def test_playwright_keeps_referenced_revisions_and_removes_only_unreferenced_revision_dirs(tmp_path):
    packages = tmp_path / "packages"
    _package_tree(
        packages,
        [
            {"name": "chromium", "revision": "1234"},
            {"name": "firefox", "revision": "567"},
        ],
    )
    cache = tmp_path / "ms-playwright"
    for name in ("chromium-1234", "chromium_headless_shell-1234", "firefox-567", "chromium-9999", "notes"):
        (cache / name).mkdir(parents=True)
        (cache / name / "payload").write_bytes(b"x")

    actions, status = rr.plan_browser_actions(cache, [packages])

    assert status == "ok"
    assert [action.path.name for action in actions] == ["chromium-9999"]


def test_playwright_missing_or_malformed_reference_fails_closed(tmp_path):
    cache = tmp_path / "cache"
    (cache / "chromium-9999").mkdir(parents=True)
    missing = tmp_path / "missing"
    malformed = tmp_path / "malformed"
    _package_tree(malformed, None)

    missing_actions, missing_status = rr.plan_browser_actions(cache, [missing])
    malformed_actions, malformed_status = rr.plan_browser_actions(cache, [malformed])

    assert missing_actions == [] and missing_status.startswith("fail-closed")
    assert malformed_actions == [] and malformed_status.startswith("fail-closed")


def test_multiple_valid_installations_are_combined_without_hardcoded_revisions(tmp_path):
    first = tmp_path / "first"
    second = tmp_path / "second"
    _package_tree(first, [{"name": "chromium", "revision": "111"}])
    _package_tree(second, [{"name": "chromium", "revision": "222"}])
    cache = tmp_path / "cache"
    for name in ("chromium-111", "chromium-222", "chromium-333"):
        (cache / name).mkdir(parents=True)

    actions, status = rr.plan_browser_actions(cache, [first, second])

    assert status == "ok"
    assert [action.path.name for action in actions] == ["chromium-333"]


def test_default_discovery_combines_python_driver_and_other_project_node_modules(tmp_path):
    home = tmp_path / "home"
    python_package = _python_package_tree(home / "python-project", [{"name": "chromium", "revision": "1223"}])
    node_project = home / "Family Organizer"
    _package_tree(node_project, [{"name": "firefox", "revision": "1490"}])

    roots, error = rr.discover_package_roots(home)
    tokens, token_error = rr._revision_tokens(roots)

    assert error is None
    assert token_error is None
    assert python_package.parent.parent.parent in roots
    assert node_project / "node_modules" in roots
    assert {("chromium", "1223"), ("firefox", "1490")} <= tokens


def test_browser_path_override_for_different_cache_fails_closed(tmp_path):
    packages = tmp_path / "packages"
    _package_tree(packages, [{"name": "chromium", "revision": "111"}])
    cache = tmp_path / "shared-cache"
    (cache / "chromium-999").mkdir(parents=True)

    actions, status = rr.plan_browser_actions(
        cache,
        [packages],
        browsers_path_override=str(tmp_path / "other-cache"),
    )

    assert actions == []
    assert status.startswith("fail-closed")


def test_one_ambiguous_installed_package_makes_browser_cleanup_fail_closed(tmp_path):
    valid = tmp_path / "valid"
    _package_tree(valid, [{"name": "chromium", "revision": "111"}])
    broken_package = tmp_path / "broken" / "node_modules" / "playwright-core"
    broken_package.mkdir(parents=True)
    (broken_package / "package.json").write_text("not-json")
    cache = tmp_path / "cache"
    (cache / "chromium-999").mkdir(parents=True)

    actions, status = rr.plan_browser_actions(cache, [valid, tmp_path / "broken"])

    assert actions == []
    assert status.startswith("fail-closed")


def test_backup_sidecars_are_grouped_and_three_newest_sets_remain(tmp_path):
    base = tmp_path / "kanban.db"
    sets = []
    for index in range(5):
        primary = tmp_path / f"kanban.db.bak-{index}"
        wal = tmp_path / f"kanban.db.bak-{index}-wal"
        shm = tmp_path / f"kanban.db.bak-{index}-shm"
        for path in (primary, wal, shm):
            path.write_text(str(index))
            os.utime(path, (NOW - index, NOW - index))
        sets.append((primary, wal, shm))

    actions = rr.plan_backup_actions(base, keep_sets=3)

    assert {action.path for action in actions} == {path for group in sets[3:] for path in group}


def test_dry_run_logs_path_and_size_without_deleting_then_apply_is_idempotent(tmp_path):
    victim = tmp_path / "victim"
    victim.write_bytes(b"12345")
    action = rr.DeleteAction(victim, 5, "output")
    logs: list[str] = []

    rr.execute_actions([action], apply=False, log=logs.append)
    assert victim.exists()
    assert str(victim) in logs[0] and "size=5" in logs[0] and "WOULD-DELETE" in logs[0]

    rr.execute_actions([action], apply=True, log=logs.append)
    rr.execute_actions([action], apply=True, log=logs.append)
    assert not victim.exists()
    assert any("DELETE" in line and str(victim) in line and "size=5" in line for line in logs)


def _hold_lock(path: str, ready: multiprocessing.Queue) -> None:
    with rr.exclusive_lock(Path(path)) as acquired:
        ready.put(acquired)
        time.sleep(1)


def test_process_lock_makes_parallel_run_exit_cleanly_without_deleting(tmp_path, capsys):
    lock = tmp_path / "retention.lock"
    output = tmp_path / "outputs"
    output.mkdir()
    victim = output / "old"
    victim.write_text("x")
    _age(victim, 20)
    ready: multiprocessing.Queue = multiprocessing.Queue()
    holder = multiprocessing.Process(target=_hold_lock, args=(str(lock), ready))
    holder.start()
    assert ready.get(timeout=2) is True
    try:
        rc = rr.main([
            "--apply", "--now", str(NOW), "--outputs-root", str(output),
            "--browser-cache", str(tmp_path / "no-cache"), "--package-root", str(tmp_path / "no-packages"),
            "--kanban-db", str(tmp_path / "kanban.db"), "--lock-file", str(lock),
        ])
    finally:
        holder.join(timeout=3)
    assert rc == 0
    assert victim.exists()
    assert "already running" in capsys.readouterr().err
