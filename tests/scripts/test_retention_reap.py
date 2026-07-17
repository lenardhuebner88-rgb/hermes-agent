from __future__ import annotations

import errno
import json
import multiprocessing
import os
import shutil
import sqlite3
import time
from pathlib import Path

import pytest

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


def test_backup_directory_keeps_three_newest_regular_file_sets(tmp_path):
    base = tmp_path / "kanban.db"
    backup_root = tmp_path / "backups"
    backup_root.mkdir()
    backups = []
    for index in range(5):
        primary = backup_root / f"snapshot-{index}.db"
        wal = backup_root / f"snapshot-{index}.db-wal"
        for path in (primary, wal):
            path.write_text(str(index))
            os.utime(path, (NOW - index, NOW - index))
        backups.append((primary, wal))
    ignored_directory = backup_root / "directory-backup"
    ignored_directory.mkdir()
    ignored_symlink = backup_root / "symlink-backup"
    ignored_symlink.symlink_to(backups[-1][0])

    actions = rr.plan_backup_actions(base, backups_root=backup_root, keep_sets=3)

    assert {action.path for action in actions} == {path for group in backups[3:] for path in group}
    assert {action.category for action in actions} == {"hermes-backup"}
    assert ignored_directory not in {action.path for action in actions}
    assert ignored_symlink not in {action.path for action in actions}


def test_main_dry_run_includes_default_backups_directory_without_deleting(
    tmp_path, capsys, monkeypatch
):
    hermes_home = tmp_path / ".hermes"
    backups_root = hermes_home / "backups"
    backups_root.mkdir(parents=True)
    backups = []
    for index in range(5):
        path = backups_root / f"backup-{index}.zip"
        path.write_bytes(str(index).encode())
        os.utime(path, (NOW - index, NOW - index))
        backups.append(path)
    worktree_root = tmp_path / "worktrees"
    worktree_root.mkdir()

    monkeypatch.setattr(rr, "active_worktree_paths", lambda _db: (set(), "ok"))
    monkeypatch.setattr(rr, "DEFAULT_WORKTREE_ROOTS", (worktree_root,))
    rc = rr.main(
        [
            "--outputs-root", str(tmp_path / "outputs"),
            "--browser-cache", str(tmp_path / "browser"),
            "--package-root", str(tmp_path / "packages"),
            "--kanban-db", str(hermes_home / "kanban.db"),
            "--lock-file", str(tmp_path / "retention.lock"),
            "--worktree-root", str(worktree_root),
            "--now", str(NOW),
        ]
    )

    captured = capsys.readouterr()
    assert rc == 0
    assert all(path.exists() for path in backups)
    assert {line.split(" path=")[1].split(" size=")[0] for line in captured.out.splitlines()} == {
        str(path) for path in backups[3:]
    }
    assert all("category=hermes-backup" in line for line in captured.out.splitlines())


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


def _kanban_db_with_active_workspace(path: Path, workspace: Path) -> None:
    conn = sqlite3.connect(path)
    try:
        conn.execute("CREATE TABLE tasks (workspace_path TEXT, status TEXT)")
        conn.execute(
            "INSERT INTO tasks (workspace_path, status) VALUES (?, 'running')",
            (str(workspace),),
        )
        conn.commit()
    finally:
        conn.close()


def test_worktree_dependency_cache_plan_is_bounded_old_and_inactive_only(tmp_path):
    root = tmp_path / "worktrees"
    inactive = root / "inactive"
    kanban_active = root / "kanban-active"
    process_active = root / "process-active"
    fresh = root / "fresh"
    for worktree in (inactive, kanban_active, process_active, fresh):
        (worktree / "node_modules").mkdir(parents=True)
        (worktree / "node_modules" / "payload").write_bytes(b"cache-bytes")
        (worktree / "source.py").write_text("must never be a candidate")
    for worktree in (inactive, kanban_active, process_active):
        _age(worktree / "node_modules", 30)
    _age(fresh / "node_modules", 2)
    db = tmp_path / "kanban.db"
    _kanban_db_with_active_workspace(db, kanban_active)
    proc = tmp_path / "proc"
    (proc / "123").mkdir(parents=True)
    (proc / "123" / "cwd").symlink_to(process_active, target_is_directory=True)

    active, active_status = rr.active_worktree_paths(db, proc_root=proc)
    actions, status = rr.plan_worktree_cache_actions(
        [root], now=NOW, max_age_seconds=14 * DAY, active_worktrees=active
    )

    assert active_status == "ok"
    assert status == "ok"
    assert [(action.path, action.size, action.category) for action in actions] == [
        (inactive / "node_modules", len(b"cache-bytes"), "worktree-dependency-cache")
    ]
    assert all((worktree / "source.py").exists() for worktree in (inactive, kanban_active, process_active, fresh))


def test_worktree_dependency_cache_plan_fails_closed_when_active_discovery_is_ambiguous(tmp_path):
    root = tmp_path / "worktrees"
    cache = root / "inactive" / "node_modules"
    cache.mkdir(parents=True)
    _age(cache, 30)
    malformed_db = tmp_path / "kanban.db"
    malformed_db.write_text("not sqlite")

    active, active_status = rr.active_worktree_paths(malformed_db, proc_root=tmp_path / "proc")

    assert active == set()
    assert active_status.startswith("fail-closed")


@pytest.mark.parametrize("process_name", ["gpg-agent", "ssh-agent", "gnome-keyring-d"])
def test_active_worktree_scan_skips_only_known_non_worker_daemons_with_unreadable_cwd(
    tmp_path, process_name
):
    db = tmp_path / "kanban.db"
    _kanban_db_with_active_workspace(db, tmp_path / "active")
    proc = tmp_path / "proc"
    process = proc / "123"
    process.mkdir(parents=True)
    (process / "cwd").write_text("not a symlink")
    (process / "comm").write_text(process_name)

    active, status = rr.active_worktree_paths(db, proc_root=proc)

    assert status == "ok"
    assert active == {(tmp_path / "active").resolve()}


def test_active_worktree_scan_still_fails_closed_for_unknown_process_with_unreadable_cwd(tmp_path):
    db = tmp_path / "kanban.db"
    _kanban_db_with_active_workspace(db, tmp_path / "active")
    proc = tmp_path / "proc"
    process = proc / "123"
    process.mkdir(parents=True)
    (process / "cwd").write_text("not a symlink")
    (process / "comm").write_text("unknown-worker")

    active, status = rr.active_worktree_paths(db, proc_root=proc)

    assert active == set()
    assert status.startswith("fail-closed: process cwd discovery failed")


def test_main_worktree_cache_dry_run_reports_exact_bytes_without_deleting(tmp_path, capsys, monkeypatch):
    root = tmp_path / "worktrees"
    cache = root / "inactive" / "node_modules"
    cache.mkdir(parents=True)
    (cache / "payload").write_bytes(b"12345")
    _age(cache, 30)

    monkeypatch.setattr(rr, "active_worktree_paths", lambda _db: (set(), "ok"))
    monkeypatch.setattr(rr, "DEFAULT_WORKTREE_ROOTS", (root,))
    rc = rr.main(
        [
            "--outputs-root", str(tmp_path / "outputs"),
            "--browser-cache", str(tmp_path / "browser"),
            "--kanban-db", str(tmp_path / "missing-kanban.db"),
            "--worktree-root", str(root),
            "--now", str(NOW),
        ]
    )

    captured = capsys.readouterr()
    assert rc == 0
    assert cache.exists()
    assert f"category=worktree-dependency-cache path={cache} size=5" in captured.out
    assert "worktree_status=ok" in captured.err


def test_allowlisted_worktree_root_is_accepted_lexically(tmp_path):
    real = tmp_path / "kanban-worktrees"
    real.mkdir()
    link = tmp_path / "link-to-worktrees"
    link.symlink_to(real, target_is_directory=True)

    # An exact allowlist entry is accepted in lexical form. A symlink alias that
    # *resolves into* a real allowlist root is NOT trusted: matching is lexical,
    # so the alias never equals the allowlist entry and the plan fails closed.
    exact, exact_status = rr.allowlisted_worktree_roots([real], allowlist=[real])
    aliased, aliased_status = rr.allowlisted_worktree_roots([link], allowlist=[real])

    assert exact_status == "ok" and exact == [real]
    assert aliased == [] and aliased_status.startswith("fail-closed")


def test_non_allowlisted_worktree_root_fails_closed(tmp_path):
    allowed = tmp_path / "allowed"
    rogue = tmp_path / "rogue"
    for path in (allowed, rogue):
        path.mkdir()

    roots, status = rr.allowlisted_worktree_roots([rogue], allowlist=[allowed])

    assert roots == []
    assert status.startswith("fail-closed: non-allowlisted worktree root")


def test_relative_worktree_root_fails_closed(tmp_path):
    allowed = tmp_path / "allowed"
    allowed.mkdir()

    roots, status = rr.allowlisted_worktree_roots([Path("worktrees")], allowlist=[allowed])

    assert roots == []
    assert status.startswith("fail-closed")


def test_symlinked_worktree_root_escaping_allowlist_fails_closed(tmp_path):
    allowed = tmp_path / "allowed"
    outside = tmp_path / "outside"
    for path in (allowed, outside):
        path.mkdir()
    escape = tmp_path / "escape"
    escape.symlink_to(outside, target_is_directory=True)

    # The symlink lives beside the allowlisted root but resolves outside it, so
    # it must never be trusted as a worktree root.
    roots, status = rr.allowlisted_worktree_roots([escape], allowlist=[allowed])

    assert roots == []
    assert status.startswith("fail-closed")


def test_symlinked_allowlist_root_to_external_root_fails_closed(tmp_path):
    external = tmp_path / "external"
    external.mkdir()
    allow_link = tmp_path / "codex-worktrees"
    allow_link.symlink_to(external, target_is_directory=True)

    # The allowlist entry itself is a symlink to an external root. Its lexical
    # form matches, but its resolved location differs, so the whole plan fails
    # closed and the symlink target is never planned.
    roots, status = rr.allowlisted_worktree_roots([allow_link], allowlist=[allow_link])

    assert roots == []
    assert status.startswith("fail-closed")


def test_main_symlinked_allowlist_root_plans_nothing(tmp_path, capsys, monkeypatch):
    # A stale, inactive cache lives under the symlink target. Because the sole
    # allowlist root is a symlink to that external directory, the plan must be
    # empty and the cache must survive.
    external = tmp_path / "external"
    cache = external / "inactive" / "node_modules"
    cache.mkdir(parents=True)
    (cache / "payload").write_bytes(b"12345")
    _age(cache, 30)
    allow_link = tmp_path / "codex-worktrees"
    allow_link.symlink_to(external, target_is_directory=True)

    monkeypatch.setattr(rr, "active_worktree_paths", lambda _db: (set(), "ok"))
    monkeypatch.setattr(rr, "DEFAULT_WORKTREE_ROOTS", (allow_link,))
    rc = rr.main(
        [
            "--outputs-root", str(tmp_path / "outputs"),
            "--browser-cache", str(tmp_path / "browser"),
            "--kanban-db", str(tmp_path / "missing-kanban.db"),
            "--worktree-root", str(allow_link),
            "--now", str(NOW),
        ]
    )

    captured = capsys.readouterr()
    assert rc == 0
    assert cache.exists()
    assert "worktree-dependency-cache" not in captured.out
    assert "worktree_status=fail-closed" in captured.err


def test_relative_dot_root_with_cwd_in_allowlist_fails_closed(tmp_path, monkeypatch):
    allowed = tmp_path / "kanban-worktrees"
    allowed.mkdir()
    # Reproduce the previously exploitable trigger: the process CWD is exactly an
    # allowlisted root. A relative ``.`` override must still be rejected without
    # exception before any normalisation, so it can never alias into the root.
    monkeypatch.chdir(allowed)

    roots, status = rr.allowlisted_worktree_roots([Path(".")], allowlist=[allowed])

    assert roots == []
    assert status.startswith("fail-closed")


def test_main_non_allowlisted_root_plans_nothing_and_fails_closed(tmp_path, capsys, monkeypatch):
    # A stale, inactive dependency cache under a root that is NOT allowlisted
    # (the default allowlist is untouched) must yield zero candidates.
    root = tmp_path / "elsewhere"
    cache = root / "inactive" / "node_modules"
    cache.mkdir(parents=True)
    (cache / "payload").write_bytes(b"12345")
    _age(cache, 30)
    monkeypatch.setattr(rr, "active_worktree_paths", lambda _db: (set(), "ok"))

    rc = rr.main(
        [
            "--outputs-root", str(tmp_path / "outputs"),
            "--browser-cache", str(tmp_path / "browser"),
            "--kanban-db", str(tmp_path / "missing-kanban.db"),
            "--worktree-root", str(root),
            "--now", str(NOW),
        ]
    )

    captured = capsys.readouterr()
    assert rc == 0
    assert cache.exists()
    assert "worktree-dependency-cache" not in captured.out
    assert "worktree_status=fail-closed: non-allowlisted worktree root" in captured.err


def test_worktree_cache_never_selects_git_or_source_even_when_dirty(tmp_path):
    # Simulate a git-dirty, inactive worktree: a .git directory plus tracked and
    # uncommitted source files sit next to a stale node_modules. Only the cache
    # may be planned; git state and source survive an apply of the plan.
    root = tmp_path / "worktrees"
    worktree = root / "inactive"
    cache = worktree / "node_modules"
    cache.mkdir(parents=True)
    (cache / "payload").write_bytes(b"cache-bytes")
    git_dir = worktree / ".git"
    git_dir.mkdir()
    (git_dir / "config").write_text("[core]\n")
    (worktree / "committed.py").write_text("tracked source")
    (worktree / "uncommitted.py").write_text("dirty, uncommitted source")
    for path in (cache, git_dir, worktree):
        _age(path, 30)

    actions, status = rr.plan_worktree_cache_actions(
        [root], now=NOW, max_age_seconds=14 * DAY, active_worktrees=set()
    )
    rr.execute_actions(actions, apply=True, log=lambda _line: None, confine_roots=[root])

    assert status == "ok"
    assert [action.path for action in actions] == [cache]
    assert not cache.exists()  # the sole candidate was the cache
    assert (git_dir / "config").exists()
    assert (worktree / "committed.py").read_text() == "tracked source"
    assert (worktree / "uncommitted.py").read_text() == "dirty, uncommitted source"


def test_worktree_dependency_cache_mount_or_volume_is_never_planned_or_deleted(tmp_path, monkeypatch):
    root = tmp_path / "worktrees"
    cache = root / "inactive" / "node_modules"
    cache.mkdir(parents=True)
    (cache / "payload").write_bytes(b"cache-bytes")
    _age(cache, 30)
    monkeypatch.setattr(rr.os.path, "ismount", lambda path: Path(path) == cache)

    actions, status = rr.plan_worktree_cache_actions(
        [root], now=NOW, max_age_seconds=14 * DAY, active_worktrees=set()
    )
    logs: list[str] = []
    rr.execute_actions(
        [rr.DeleteAction(cache, len(b"cache-bytes"), "worktree-dependency-cache")],
        apply=True,
        log=logs.append,
        confine_roots=[root],
    )

    assert actions == []
    assert status == "ok"
    assert cache.exists()
    assert any("reason=mount-or-volume" in line for line in logs)


def test_worktree_dependency_cache_same_filesystem_bind_mount_is_never_planned_or_deleted(tmp_path, monkeypatch):
    root = tmp_path / "worktrees"
    cache = root / "inactive" / "node_modules"
    cache.mkdir(parents=True)
    (cache / "payload").write_bytes(b"cache-bytes")
    _age(cache, 30)
    monkeypatch.setattr(rr.os.path, "ismount", lambda _path: False)
    monkeypatch.setattr(rr, "_mountinfo_mount_points", lambda: {cache})

    actions, status = rr.plan_worktree_cache_actions(
        [root], now=NOW, max_age_seconds=14 * DAY, active_worktrees=set()
    )
    logs: list[str] = []
    rr.execute_actions(
        [rr.DeleteAction(cache, len(b"cache-bytes"), "worktree-dependency-cache")],
        apply=True,
        log=logs.append,
        confine_roots=[root],
    )

    assert actions == []
    assert status == "ok"
    assert cache.exists()
    assert any("reason=mount-or-volume" in line for line in logs)


def test_worktree_dependency_cache_ancestor_bind_mount_is_never_planned_or_deleted(tmp_path, monkeypatch):
    root = tmp_path / "worktrees"
    worktree = root / "inactive"
    cache = worktree / "node_modules"
    cache.mkdir(parents=True)
    (cache / "payload").write_bytes(b"cache-bytes")
    _age(cache, 30)
    monkeypatch.setattr(rr.os.path, "ismount", lambda _path: False)

    for mount_point in (root, worktree):
        monkeypatch.setattr(rr, "_mountinfo_mount_points", lambda: {mount_point})
        actions, status = rr.plan_worktree_cache_actions(
            [root], now=NOW, max_age_seconds=14 * DAY, active_worktrees=set()
        )

        assert actions == []
        assert status == "ok"

    monkeypatch.setattr(rr, "DEFAULT_WORKTREE_ROOTS", (root,))
    monkeypatch.setattr(rr, "active_worktree_paths", lambda _db: (set(), "ok"))
    rc = rr.main(
        [
            "--apply",
            "--outputs-root", str(tmp_path / "outputs"),
            "--browser-cache", str(tmp_path / "browser"),
            "--kanban-db", str(tmp_path / "missing-kanban.db"),
            "--lock-file", str(tmp_path / "retention.lock"),
            "--now", str(NOW),
        ]
    )

    assert rc == 0
    assert cache.exists()


def test_worktree_cache_ancestor_bind_mount_inserted_after_replan_is_refused(tmp_path, monkeypatch):
    # Regression for the final-check gap: the safety re-plan (safe_to_delete)
    # passes, THEN a same-filesystem bind mount races in at the worktree ANCESTOR
    # before rmtree. Because the ancestor mount is invisible below the cache, the
    # deletion-time mount/volume check must be bounded to the validated allowlisted
    # root→cache chain — otherwise the cache is deleted through the fresh mount.
    root = tmp_path / "worktrees"
    worktree = root / "inactive"
    cache = worktree / "node_modules"
    cache.mkdir(parents=True)
    (cache / "payload").write_bytes(b"cache-bytes")
    _age(cache, 30)

    monkeypatch.setattr(rr.os.path, "ismount", lambda _path: False)

    def replan_passes_then_ancestor_is_mounted(_action):
        # The re-plan saw a clean tree; the bind mount only materialises afterwards,
        # i.e. between the re-plan and the delete.
        monkeypatch.setattr(rr, "_mountinfo_mount_points", lambda: {worktree})
        return True

    logs: list[str] = []
    rr.execute_actions(
        [rr.DeleteAction(cache, len(b"cache-bytes"), "worktree-dependency-cache")],
        apply=True,
        log=logs.append,
        safe_to_delete=replan_passes_then_ancestor_is_mounted,
        confine_roots=[root],
    )

    assert cache.exists()
    assert any("reason=mount-or-volume" in line for line in logs)
    assert all("DELETE category=worktree-dependency-cache" not in line for line in logs)


def test_active_worktree_paths_missing_db_fails_closed(tmp_path):
    # An absent Kanban DB (typo/transient rename/missing default) must fail closed
    # rather than proceeding on the process scan alone, otherwise a stale cache of
    # a non-terminal task without a live process would become deletable.
    missing = tmp_path / "does-not-exist.db"
    proc = tmp_path / "proc"
    (proc / "999").mkdir(parents=True)
    (proc / "999" / "cwd").symlink_to(tmp_path, target_is_directory=True)

    active, status = rr.active_worktree_paths(missing, proc_root=proc)

    assert active == set()
    assert status.startswith("fail-closed")
    assert "missing" in status.lower()


def test_main_missing_kanban_db_plans_no_worktree_cache(tmp_path, capsys, monkeypatch):
    # End-to-end: with a missing DB and the REAL activity guard (not monkeypatched),
    # a stale, inactive cache under an allowlisted root yields zero candidates.
    root = tmp_path / "worktrees"
    cache = root / "inactive" / "node_modules"
    cache.mkdir(parents=True)
    (cache / "payload").write_bytes(b"12345")
    _age(cache, 30)
    monkeypatch.setattr(rr, "DEFAULT_WORKTREE_ROOTS", (root,))

    rc = rr.main(
        [
            "--outputs-root", str(tmp_path / "outputs"),
            "--browser-cache", str(tmp_path / "browser"),
            "--kanban-db", str(tmp_path / "missing-kanban.db"),
            "--worktree-root", str(root),
            "--now", str(NOW),
        ]
    )

    captured = capsys.readouterr()
    assert rc == 0
    assert cache.exists()
    assert "worktree-dependency-cache" not in captured.out
    assert "worktree_status=fail-closed" in captured.err
    assert "missing" in captured.err.lower()


@pytest.mark.parametrize("bad_value", ["-1", "-0.5", "nan", "-inf"])
def test_negative_or_non_finite_worktree_cache_days_is_rejected(bad_value):
    # A negative age would invert the threshold and delete fresh caches; reject at
    # parse time. argparse turns the ArgumentTypeError into SystemExit(2).
    with pytest.raises(SystemExit):
        rr.main(["--worktree-cache-days", bad_value, "--now", str(NOW)])


def test_worktree_cache_parent_symlink_swap_after_plan_is_not_deleted(tmp_path):
    # Plan a real cache, then swap its WORKTREE parent for a symlink pointing at an
    # attacker-controlled tree. A leaf is_symlink() check would miss the swapped
    # parent; deletion-time confinement + O_NOFOLLOW pinning must refuse to delete
    # through the symlink, leaving the external data untouched.
    root = tmp_path / "worktrees"
    worktree = root / "inactive"
    cache = worktree / "node_modules"
    cache.mkdir(parents=True)
    (cache / "payload").write_bytes(b"cache-bytes")
    _age(cache, 30)

    actions, status = rr.plan_worktree_cache_actions(
        [root], now=NOW, max_age_seconds=14 * DAY, active_worktrees=set()
    )
    assert status == "ok"
    assert [action.path for action in actions] == [cache]

    external = tmp_path / "external"
    external_cache = external / "node_modules"
    external_cache.mkdir(parents=True)
    (external_cache / "keep").write_text("must survive")
    shutil.rmtree(worktree)
    worktree.symlink_to(external, target_is_directory=True)

    logs: list[str] = []
    rr.execute_actions(actions, apply=True, log=logs.append, confine_roots=[root])

    assert external_cache.exists()
    assert (external_cache / "keep").read_text() == "must survive"
    # A swapped parent symlink is now caught fail-closed by the bounded root→cache
    # mount/volume check (it flags an ancestor symlink) before fd-confinement would;
    # either fail-closed reason proves the external tree is never deleted through it.
    assert any(
        "reason=mount-or-volume" in line or "reason=confinement" in line for line in logs
    )
    assert all("DELETE category=worktree-dependency-cache" not in line for line in logs)


def test_worktree_cache_without_confine_roots_is_never_deleted(tmp_path):
    # Fail-closed default: execute_actions cannot delete a worktree cache unless an
    # explicit allowlisted confine_roots is supplied.
    root = tmp_path / "worktrees"
    cache = root / "inactive" / "node_modules"
    cache.mkdir(parents=True)
    (cache / "payload").write_bytes(b"cache-bytes")
    _age(cache, 30)

    logs: list[str] = []
    rr.execute_actions(
        [rr.DeleteAction(cache, len(b"cache-bytes"), "worktree-dependency-cache")],
        apply=True,
        log=logs.append,
    )

    assert cache.exists()
    assert any("reason=confinement" in line for line in logs)


def _find_real_nested_mount() -> tuple[Path, str] | None:
    """Return (parent_dir, mount_component) for a real mount whose parent is openable.

    Used to exercise RESOLVE_NO_XDEV against an actual mount boundary without any
    privileges — the kernel already provides mounts like ``/dev/pts`` or
    ``/dev/shm`` on Linux.
    """
    try:
        entries = Path("/proc/self/mountinfo").read_text(encoding="utf-8").splitlines()
    except OSError:
        return None
    for line in entries:
        fields = line.split()
        if len(fields) < 5:
            continue
        mount_point = Path(fields[4])
        parent = mount_point.parent
        name = mount_point.name
        if not name or parent == mount_point:
            continue
        try:
            fd = os.open(str(parent), os.O_RDONLY | os.O_DIRECTORY)
        except OSError:
            continue
        os.close(fd)
        return parent, name
    return None


def _openat2_available() -> bool:
    try:
        fd = os.open("/dev", os.O_RDONLY | os.O_DIRECTORY)
    except OSError:
        return False
    try:
        os.close(rr._openat2(fd, ".", os.O_RDONLY | os.O_DIRECTORY, 0))
        return True
    except OSError as exc:
        return exc.errno != errno.ENOSYS
    finally:
        os.close(fd)


def test_openat2_no_xdev_rejects_a_real_mount_crossing():
    # Primitive proof (no mocking, no privileges): the exact syscall+flag the
    # deletion path relies on refuses to cross a real mount boundary, while a
    # plain resolve succeeds. If it caught nothing, the fail-closed descent would
    # silently traverse a raced-in bind mount.
    if not _openat2_available():
        pytest.skip("openat2 unavailable on this kernel")
    found = _find_real_nested_mount()
    if found is None:
        pytest.skip("no real nested mount with an openable parent")
    parent, name = found
    parent_fd = os.open(str(parent), os.O_RDONLY | os.O_DIRECTORY)
    try:
        # Plain resolve traverses the mount fine.
        os.close(rr._openat2(parent_fd, name, os.O_RDONLY | os.O_DIRECTORY, 0))
        # RESOLVE_NO_XDEV must refuse the crossing with EXDEV.
        with pytest.raises(OSError) as excinfo:
            rr._openat2(parent_fd, name, os.O_RDONLY | os.O_DIRECTORY, rr._RESOLVE_NO_XDEV)
        assert excinfo.value.errno == errno.EXDEV
    finally:
        os.close(parent_fd)


def test_confined_descent_fails_closed_when_openat2_reports_mount_crossing(tmp_path, monkeypatch):
    # The final _contains_mount_or_volume check passes (the mount is not yet
    # visible), but the componentwise fd descent must still refuse when the kernel
    # reports a mount crossing (EXDEV) on the worktree ancestor — exactly what
    # RESOLVE_NO_XDEV yields for a same-filesystem bind mount raced in *after* the
    # check. Also asserts the descent actually requests RESOLVE_NO_XDEV.
    root = tmp_path / "worktrees"
    worktree = root / "inactive"
    cache = worktree / "node_modules"
    cache.mkdir(parents=True)
    (cache / "payload").write_bytes(b"cache-bytes")
    _age(cache, 30)

    monkeypatch.setattr(rr, "_contains_mount_or_volume", lambda *args, **kwargs: False)
    real_openat2 = rr._openat2
    seen_resolves: list[int] = []

    def openat2_mount_on_worktree(dir_fd, name, flags, resolve):
        seen_resolves.append(resolve)
        if name == "inactive":
            raise OSError(errno.EXDEV, os.strerror(errno.EXDEV), name)
        return real_openat2(dir_fd, name, flags, resolve)

    monkeypatch.setattr(rr, "_openat2", openat2_mount_on_worktree)

    logs: list[str] = []
    rr.execute_actions(
        [rr.DeleteAction(cache, len(b"cache-bytes"), "worktree-dependency-cache")],
        apply=True,
        log=logs.append,
        confine_roots=[root],
    )

    assert cache.exists()
    assert (cache / "payload").read_bytes() == b"cache-bytes"
    assert seen_resolves and all(resolve & rr._RESOLVE_NO_XDEV for resolve in seen_resolves)
    assert any("reason=confinement" in line for line in logs)
    assert all("DELETE category=worktree-dependency-cache" not in line for line in logs)


def test_real_mount_raced_onto_worktree_ancestor_after_check_is_not_deleted(tmp_path, monkeypatch):
    # End-to-end with a REAL mount (tmpfs /dev/shm) as the worktree ancestor and
    # only the *timing* simulated: _contains_mount_or_volume is stubbed to False
    # so the mount is invisible to the final check, and the real openat2 descent
    # must still refuse to delete through the real mount boundary. No privileges,
    # no mocking of openat2 itself.
    if not _openat2_available():
        pytest.skip("openat2 unavailable on this kernel")
    shm = Path("/dev/shm")
    if not (shm.is_dir() and any(line.split()[4] == "/dev/shm" for line in Path("/proc/self/mountinfo").read_text().splitlines())):
        pytest.skip("/dev/shm is not a mount here")
    try:
        dev_fd = os.open("/dev", os.O_RDONLY | os.O_DIRECTORY)
    except OSError:
        pytest.skip("/dev not openable")
    os.close(dev_fd)

    # confine_root = /dev, worktree = shm (the real mount), cache under it.
    workdir = shm / f"rr-race-{os.getpid()}"
    cache = workdir / "node_modules"
    try:
        cache.mkdir(parents=True)
        (cache / "payload").write_bytes(b"external-must-survive")
        _age(cache, 30)

        monkeypatch.setattr(rr, "_contains_mount_or_volume", lambda *args, **kwargs: False)

        logs: list[str] = []
        rr.execute_actions(
            [rr.DeleteAction(cache, len(b"external-must-survive"), "worktree-dependency-cache")],
            apply=True,
            log=logs.append,
            confine_roots=[Path("/dev")],
        )

        assert cache.exists()
        assert (cache / "payload").read_bytes() == b"external-must-survive"
        assert any("reason=confinement" in line for line in logs)
        assert all("DELETE category=worktree-dependency-cache" not in line for line in logs)
    finally:
        shutil.rmtree(workdir, ignore_errors=True)


def test_worktree_cache_fd_identity_changed_between_plan_and_delete_is_refused(tmp_path):
    # Option-A apply fail-closed: the leaf inode is captured at plan time and the
    # pinned leaf fd is fstat-verified against it. Swap the cache directory for a
    # DIFFERENT real inode after planning (the safety re-plan only checks the
    # path, so it still passes) — the fd-identity mismatch must skip the delete,
    # never touch the replacement.
    root = tmp_path / "worktrees"
    worktree = root / "inactive"
    cache = worktree / "node_modules"
    cache.mkdir(parents=True)
    (cache / "payload").write_bytes(b"planned-cache")
    _age(cache, 30)

    actions, status = rr.plan_worktree_cache_actions(
        [root], now=NOW, max_age_seconds=14 * DAY, active_worktrees=set()
    )
    assert status == "ok"
    assert [action.path for action in actions] == [cache]
    assert actions[0].st_ino is not None

    # Replace the cache with a distinct directory (rename preserves the fresh
    # inode), so the leaf at the same path now has a different st_ino.
    replacement = worktree / "replacement"
    replacement.mkdir()
    (replacement / "fresh").write_bytes(b"fresh-must-survive")
    _age(replacement, 30)
    shutil.rmtree(cache)
    os.rename(replacement, cache)
    assert cache.stat().st_ino != actions[0].st_ino

    logs: list[str] = []
    rr.execute_actions(
        actions,
        apply=True,
        log=logs.append,
        safe_to_delete=lambda _action: True,  # re-plan finds the path → passes
        confine_roots=[root],
    )

    assert cache.exists()
    assert (cache / "fresh").read_bytes() == b"fresh-must-survive"
    assert any("reason=fd-identity" in line for line in logs)
    assert all("DELETE category=worktree-dependency-cache" not in line for line in logs)


def test_worktree_cache_leaf_symlink_swapped_after_plan_is_not_deleted(tmp_path, monkeypatch):
    # A same-path leaf swapped for a SYMLINK after planning. Even with the
    # ancestor-chain mount/volume check stubbed blind, the held-leaf openat2 open
    # under RESOLVE_NO_SYMLINKS must refuse the symlinked leaf, so the external
    # target is never deleted through it.
    root = tmp_path / "worktrees"
    worktree = root / "inactive"
    cache = worktree / "node_modules"
    cache.mkdir(parents=True)
    (cache / "payload").write_bytes(b"planned-cache")
    _age(cache, 30)

    actions, status = rr.plan_worktree_cache_actions(
        [root], now=NOW, max_age_seconds=14 * DAY, active_worktrees=set()
    )
    assert status == "ok"
    assert [action.path for action in actions] == [cache]

    external = tmp_path / "external"
    external_cache = external / "node_modules"
    external_cache.mkdir(parents=True)
    (external_cache / "keep").write_text("must survive")
    shutil.rmtree(cache)
    cache.symlink_to(external_cache, target_is_directory=True)

    # Blind the ancestor-chain check so the leaf fd guard is the sole line of
    # defence for this timing.
    monkeypatch.setattr(rr, "_contains_mount_or_volume", lambda *args, **kwargs: False)

    logs: list[str] = []
    rr.execute_actions(actions, apply=True, log=logs.append, confine_roots=[root])

    assert external_cache.exists()
    assert (external_cache / "keep").read_text() == "must survive"
    assert cache.is_symlink()
    assert any("reason=confinement" in line for line in logs)
    assert all("DELETE category=worktree-dependency-cache" not in line for line in logs)


def test_worktree_cache_normal_case_deletes_via_pinned_fd(tmp_path):
    # The unchanged, matching-identity normal case still deletes: nested contents
    # are removed through the pinned fd and the empty leaf is rmdir'd, while a
    # sibling source file in the same worktree is untouched.
    root = tmp_path / "worktrees"
    worktree = root / "inactive"
    cache = worktree / "node_modules"
    (cache / "pkg" / "sub").mkdir(parents=True)
    (cache / "pkg" / "sub" / "index.js").write_bytes(b"module.exports = 1\n")
    (cache / "top.txt").write_bytes(b"12345")
    (worktree / "source.py").write_text("keep me")
    for path in (cache, worktree):
        _age(path, 30)

    actions, status = rr.plan_worktree_cache_actions(
        [root], now=NOW, max_age_seconds=14 * DAY, active_worktrees=set()
    )
    assert status == "ok"
    assert [action.path for action in actions] == [cache]

    logs: list[str] = []
    rr.execute_actions(actions, apply=True, log=logs.append, confine_roots=[root])

    assert not cache.exists()
    assert (worktree / "source.py").read_text() == "keep me"
    assert any(
        "DELETE category=worktree-dependency-cache" in line and str(cache) in line
        for line in logs
    )
