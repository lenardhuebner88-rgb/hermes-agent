#!/usr/bin/env python3
"""Safely plan or apply retention cleanup for local Hermes artifacts.

The default mode is dry-run. Browser-cache cleanup is fail-closed unless installed
Playwright package metadata yields a complete, unambiguous set of revisions.
"""
from __future__ import annotations

import argparse
import fcntl
import json
import os
import re
import shutil
import sys
import time
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Iterator, Sequence

DEFAULT_OUTPUTS_ROOT = Path("/home/piet/.hermes/playwright-mcp-output")
DEFAULT_BROWSER_CACHE = Path("/home/piet/.cache/ms-playwright")
DEFAULT_PACKAGE_ROOTS = (
    Path("/home/piet/.npm/_npx"),
    Path("/home/piet/.hermes/hermes-agent/node_modules"),
)
DEFAULT_KANBAN_DB = Path("/home/piet/.hermes/kanban.db")
DEFAULT_LOCK_FILE = Path("/home/piet/.hermes/retention-reap.lock")
DEFAULT_OUTPUT_DAYS = 14.0
DEFAULT_BACKUP_SETS = 3
_BROWSER_DIR = re.compile(r"^(?P<name>[A-Za-z][A-Za-z0-9_-]*)-(?P<revision>[0-9]+)$")
_PLAYWRIGHT_PACKAGES = {"@playwright/mcp", "playwright", "playwright-core"}


@dataclass(frozen=True)
class DeleteAction:
    path: Path
    size: int
    category: str


def _path_size(path: Path) -> int:
    if path.is_symlink() or path.is_file():
        try:
            return path.lstat().st_size
        except FileNotFoundError:
            return 0
    total = 0
    try:
        for child in path.rglob("*"):
            if child.is_file() and not child.is_symlink():
                try:
                    total += child.stat().st_size
                except FileNotFoundError:
                    pass
    except FileNotFoundError:
        pass
    return total


def plan_output_actions(root: Path, *, now: float, max_age_seconds: float) -> list[DeleteAction]:
    """Select regular files whose mtime is strictly older than the cutoff."""
    if not root.is_dir():
        return []
    actions: list[DeleteAction] = []
    for path in sorted(root.rglob("*")):
        try:
            if path.is_symlink() or not path.is_file():
                continue
            stat = path.stat()
        except FileNotFoundError:
            continue
        if now - stat.st_mtime > max_age_seconds:
            actions.append(DeleteAction(path, stat.st_size, "output"))
    return actions


def _metadata_files(package_roots: Sequence[Path]) -> tuple[list[Path], str | None]:
    package_jsons: list[Path] = []
    for root in package_roots:
        if not root.exists():
            continue
        try:
            candidates = root.rglob("package.json")
            for candidate in candidates:
                package_dir = candidate.parent
                path_identifies_playwright = (
                    package_dir.name in {"playwright", "playwright-core"}
                    or (package_dir.name == "mcp" and package_dir.parent.name == "@playwright")
                )
                try:
                    data = json.loads(candidate.read_text())
                except (OSError, json.JSONDecodeError) as exc:
                    if path_identifies_playwright:
                        return [], f"invalid Playwright package metadata {candidate}: {exc}"
                    continue
                if data.get("name") in _PLAYWRIGHT_PACKAGES:
                    package_jsons.append(candidate)
        except OSError as exc:
            return [], f"package scan error: {exc}"
    if not package_jsons:
        return [], "no installed Playwright package metadata"
    return sorted(set(package_jsons)), None


def _revision_tokens(package_roots: Sequence[Path]) -> tuple[set[tuple[str, str]], str | None]:
    package_jsons, error = _metadata_files(package_roots)
    if error:
        return set(), error
    browser_files: set[Path] = set()
    for package_json in package_jsons:
        package_dir = package_json.parent
        candidates = [package_dir / "browsers.json"]
        if package_dir.name != "playwright-core":
            candidates.append(package_dir.parent / "playwright-core" / "browsers.json")
        browser_files.update(path for path in candidates if path.is_file())
    if not browser_files:
        return set(), "Playwright packages found but no browsers.json"

    tokens: set[tuple[str, str]] = set()
    for path in sorted(browser_files):
        try:
            data = json.loads(path.read_text())
            browsers = data["browsers"]
            if not isinstance(browsers, list) or not browsers:
                raise ValueError("empty browsers list")
            for browser in browsers:
                name = browser["name"]
                revision = str(browser["revision"])
                if not isinstance(name, str) or not name or not revision.isdigit():
                    raise ValueError("invalid browser record")
                tokens.add((name, revision))
        except (OSError, json.JSONDecodeError, KeyError, TypeError, ValueError) as exc:
            return set(), f"invalid {path}: {exc}"
    if not tokens:
        return set(), "no browser revisions resolved"
    return tokens, None


def _is_referenced_browser_dir(name: str, revision: str, tokens: set[tuple[str, str]]) -> bool:
    if (name, revision) in tokens:
        return True
    # Playwright installs Chromium's headless shell with Chromium's revision.
    if name == "chromium_headless_shell" and ("chromium", revision) in tokens:
        return True
    return False


def plan_browser_actions(cache: Path, package_roots: Sequence[Path]) -> tuple[list[DeleteAction], str]:
    if not cache.is_dir():
        return [], "cache-missing"
    tokens, error = _revision_tokens(package_roots)
    if error:
        return [], f"fail-closed: {error}"
    actions: list[DeleteAction] = []
    for path in sorted(cache.iterdir()):
        if path.is_symlink() or not path.is_dir():
            continue
        match = _BROWSER_DIR.fullmatch(path.name)
        if not match:
            continue
        if not _is_referenced_browser_dir(match.group("name"), match.group("revision"), tokens):
            actions.append(DeleteAction(path, _path_size(path), "browser"))
    return actions, "ok"


def _backup_set_key(base: Path, path: Path) -> str | None:
    prefix = base.name + ".bak"
    if not path.name.startswith(prefix):
        return None
    name = path.name
    for suffix in ("-wal", "-shm"):
        if name.endswith(suffix):
            name = name[: -len(suffix)]
            break
    return name


def plan_backup_actions(base: Path, *, keep_sets: int) -> list[DeleteAction]:
    groups: dict[str, list[Path]] = {}
    if not base.parent.is_dir():
        return []
    for path in base.parent.glob(base.name + ".bak*"):
        if not path.is_file() or path.is_symlink():
            continue
        key = _backup_set_key(base, path)
        if key is not None:
            groups.setdefault(key, []).append(path)
    ordered = sorted(
        groups.values(),
        key=lambda members: max(path.stat().st_mtime_ns for path in members),
        reverse=True,
    )
    return [
        DeleteAction(path, path.stat().st_size, "kanban-backup")
        for members in ordered[max(keep_sets, 0) :]
        for path in sorted(members)
    ]


def execute_actions(actions: Sequence[DeleteAction], *, apply: bool, log: Callable[[str], None]) -> None:
    verb = "DELETE" if apply else "WOULD-DELETE"
    for action in actions:
        log(f"retention-reap: {verb} category={action.category} path={action.path} size={action.size}")
        if not apply:
            continue
        try:
            if action.path.is_symlink() or action.path.is_file():
                action.path.unlink()
            elif action.path.is_dir():
                shutil.rmtree(action.path)
        except FileNotFoundError:
            pass


@contextmanager
def exclusive_lock(path: Path) -> Iterator[bool]:
    path.parent.mkdir(parents=True, exist_ok=True)
    handle = path.open("a+")
    acquired = False
    try:
        try:
            fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
            acquired = True
        except BlockingIOError:
            pass
        yield acquired
    finally:
        if acquired:
            fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
        handle.close()


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=(__doc__ or "Retention cleanup").splitlines()[0])
    parser.add_argument("--apply", action="store_true", help="delete planned paths (default: dry-run)")
    parser.add_argument("--outputs-root", type=Path, default=DEFAULT_OUTPUTS_ROOT)
    parser.add_argument("--browser-cache", type=Path, default=DEFAULT_BROWSER_CACHE)
    parser.add_argument("--package-root", type=Path, action="append", dest="package_roots")
    parser.add_argument("--kanban-db", type=Path, default=DEFAULT_KANBAN_DB)
    parser.add_argument("--lock-file", type=Path, default=DEFAULT_LOCK_FILE)
    parser.add_argument("--output-days", type=float, default=DEFAULT_OUTPUT_DAYS)
    parser.add_argument("--keep-backup-sets", type=int, default=DEFAULT_BACKUP_SETS)
    parser.add_argument("--now", type=float, default=None, help=argparse.SUPPRESS)
    args = parser.parse_args(argv)
    roots = args.package_roots if args.package_roots is not None else list(DEFAULT_PACKAGE_ROOTS)
    now = time.time() if args.now is None else args.now

    with exclusive_lock(args.lock_file) as acquired:
        if not acquired:
            print("retention-reap: already running; skipping", file=sys.stderr)
            return 0
        output_actions = plan_output_actions(
            args.outputs_root, now=now, max_age_seconds=args.output_days * 24 * 60 * 60
        )
        browser_actions, browser_status = plan_browser_actions(args.browser_cache, roots)
        backup_actions = plan_backup_actions(args.kanban_db, keep_sets=args.keep_backup_sets)
        actions = output_actions + browser_actions + backup_actions
        execute_actions(actions, apply=args.apply, log=lambda line: print(line, flush=True))
        mode = "apply" if args.apply else "dry-run"
        print(
            f"retention-reap: done mode={mode} actions={len(actions)} browser_status={browser_status}",
            file=sys.stderr,
        )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
