#!/usr/bin/env python3
"""Safely plan or apply retention cleanup for local Hermes artifacts.

The default mode is dry-run. Browser-cache cleanup is fail-closed unless installed
Playwright package metadata yields a complete, unambiguous set of revisions.
"""
from __future__ import annotations

import argparse
import ctypes
import fcntl
import json
import math
import os
import re
import shutil
import sqlite3
import stat
import sys
import time
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Iterator, Sequence

DEFAULT_OUTPUTS_ROOT = Path("/home/piet/.hermes/playwright-mcp-output")
DEFAULT_BROWSER_CACHE = Path("/home/piet/.cache/ms-playwright")
DEFAULT_KANBAN_DB = Path("/home/piet/.hermes/kanban.db")
DEFAULT_LOCK_FILE = Path("/home/piet/.hermes/retention-reap.lock")
DEFAULT_OUTPUT_DAYS = 14.0
DEFAULT_BACKUP_SETS = 3
DEFAULT_WORKTREE_CACHE_DAYS = 14.0
# Each root must contain worktrees directly.  These are deliberately a narrow,
# explicit allowlist: this reaper never discovers arbitrary repositories.
DEFAULT_WORKTREE_ROOTS = (
    Path("/home/piet/.hermes/hermes-agent/.worktrees/kanban"),
    Path("/home/piet/.codex/worktrees"),
)
_BROWSER_DIR = re.compile(r"^(?P<name>[A-Za-z][A-Za-z0-9_-]*)-(?P<revision>[0-9]+)$")
_PLAYWRIGHT_PACKAGES = {"@playwright/mcp", "playwright", "playwright-core"}
# These long-lived per-user daemons are non-dumpable under common Linux session
# configurations, so /proc/<pid>/cwd can be unreadable to the same UID. Their
# exact comm values are narrowly allowlisted because none runs worktree
# workloads from its cwd; every unknown same-user process remains fail-closed.
_UNREADABLE_CWD_NON_WORKER_PROCESSES = frozenset(
    {"systemd", "(sd-pam)", "gpg-agent", "ssh-agent", "gnome-keyring-d"}
)


@dataclass(frozen=True)
class DeleteAction:
    path: Path
    size: int
    category: str
    # Inode identity captured at plan time for worktree-dependency-cache
    # candidates. At deletion time the pinned leaf fd is fstat-verified against
    # these; any deviation (a swap between planning and execution) fails closed.
    # ``None`` for every other category and for manually built actions, which
    # therefore cannot pass the fd-identity gate.
    st_dev: int | None = None
    st_ino: int | None = None


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


def discover_package_roots(home: Path) -> tuple[list[Path], str | None]:
    """Find every Node and Python package root below home without entering package trees."""
    roots: set[Path] = set()
    errors: list[OSError] = []

    def record_error(error: OSError) -> None:
        # Unreadable service-owned trees cannot contain an installation active
        # for this user; other traversal failures still make discovery unsafe.
        if not isinstance(error, PermissionError):
            errors.append(error)

    try:
        for directory, names, _files in os.walk(home, onerror=record_error):
            path = Path(directory)
            if path.name == "node_modules":
                roots.add(path)
                names.clear()
                continue
            if path.name in {"site-packages", "dist-packages"}:
                roots.add(path)
                names.clear()
                continue
            names[:] = [name for name in names if name not in {".git", "__pycache__"}]
    except OSError as exc:
        errors.append(exc)
    if errors:
        return [], f"package discovery error: {errors[0]}"
    if not roots:
        return [], "no Node or Python package roots discovered"
    return sorted(roots), None


def _metadata_files(package_roots: Sequence[Path]) -> tuple[list[Path], str | None]:
    package_jsons: list[Path] = []
    python_browser_files: list[Path] = []
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
            python_browser_files.extend(root.glob("playwright/driver/package/browsers.json"))
        except OSError as exc:
            return [], f"package scan error: {exc}"
    if not package_jsons and not python_browser_files:
        return [], "no installed Playwright package metadata"
    return sorted(set(package_jsons + python_browser_files)), None


def _revision_tokens(package_roots: Sequence[Path]) -> tuple[set[tuple[str, str]], str | None]:
    metadata_files, error = _metadata_files(package_roots)
    if error:
        return set(), error
    browser_files: set[Path] = set()
    for metadata_file in metadata_files:
        if metadata_file.name == "browsers.json":
            browser_files.add(metadata_file)
            continue
        package_dir = metadata_file.parent
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


def plan_browser_actions(
    cache: Path,
    package_roots: Sequence[Path],
    *,
    browsers_path_override: str | None = None,
) -> tuple[list[DeleteAction], str]:
    if not cache.is_dir():
        return [], "cache-missing"
    if browsers_path_override:
        if browsers_path_override == "0":
            return [], "fail-closed: PLAYWRIGHT_BROWSERS_PATH=0 uses per-package caches"
        override = Path(browsers_path_override).expanduser().resolve()
        if override != cache.expanduser().resolve():
            return [], f"fail-closed: PLAYWRIGHT_BROWSERS_PATH targets {override}, not {cache}"
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


def _backup_file_set_key(path: Path) -> str:
    name = path.name
    for suffix in ("-wal", "-shm"):
        if name.endswith(suffix):
            return name[: -len(suffix)]
    return name


def _plan_backup_file_sets(
    paths: Iterator[Path], *, keep_sets: int, category: str, key_for: Callable[[Path], str | None]
) -> list[DeleteAction]:
    groups: dict[str, list[Path]] = {}
    for path in paths:
        if path.is_symlink() or not path.is_file():
            continue
        key = key_for(path)
        if key is not None:
            groups.setdefault(key, []).append(path)
    ordered = sorted(
        groups.values(),
        key=lambda members: max(path.stat().st_mtime_ns for path in members),
        reverse=True,
    )
    return [
        DeleteAction(path, path.stat().st_size, category)
        for members in ordered[max(keep_sets, 0) :]
        for path in sorted(members)
    ]


def plan_backup_actions(
    base: Path, *, keep_sets: int, backups_root: Path | None = None
) -> list[DeleteAction]:
    """Keep the newest backup sets in each bounded Hermes backup location.

    The sibling ``kanban.db.bak*`` files and top-level regular files under
    ``~/.hermes/backups`` are independent retention pools, so activity in one
    cannot evict every recovery point from the other. Directories and symlinks
    in the general backup root are deliberately outside this file-only policy.
    """
    backups_root = base.parent / "backups" if backups_root is None else backups_root
    actions: list[DeleteAction] = []
    if base.parent.is_dir():
        actions.extend(
            _plan_backup_file_sets(
                base.parent.glob(base.name + ".bak*"),
                keep_sets=keep_sets,
                category="kanban-backup",
                key_for=lambda path: _backup_set_key(base, path),
            )
        )
    if backups_root.is_dir():
        actions.extend(
            _plan_backup_file_sets(
                backups_root.iterdir(),
                keep_sets=keep_sets,
                category="hermes-backup",
                key_for=_backup_file_set_key,
            )
        )
    return actions


def _normal_path(path: Path) -> Path:
    return path.expanduser().resolve(strict=False)


def _lexical_path(path: Path) -> Path:
    """Absolute, ``.``/``..``-collapsed path that does NOT follow symlinks.

    Unlike :func:`_normal_path`, a symlinked component stays lexically visible
    so allowlist matching and the downstream ``is_symlink`` guard can still see
    it instead of it being silently rewritten to the symlink target.
    """
    return Path(os.path.normpath(path.expanduser()))


def active_worktree_paths(kanban_db: Path, *, proc_root: Path = Path("/proc")) -> tuple[set[Path], str]:
    """Return non-terminal Kanban and current-process worktree paths.

    Any database or same-user process-discovery error is fail-closed so an
    uncertain dependency cache is never selected for deletion. The Kanban DB is
    always expected (a default path exists); an *absent* DB is treated as a
    fail-closed stop rather than silently proceeding on the process scan alone.
    Otherwise a typo, transient rename, or missing default would let a stale
    cache of a non-terminal (ready/blocked) task without a live process become
    deletable.
    """
    if not kanban_db.exists():
        return set(), f"fail-closed: Kanban DB missing: {kanban_db}"
    active: set[Path] = set()
    try:
        with sqlite3.connect(f"file:{kanban_db}?mode=ro", uri=True) as conn:
            rows = conn.execute(
                """
                SELECT workspace_path FROM tasks
                WHERE workspace_path IS NOT NULL
                  AND LOWER(status) NOT IN ('done', 'archived', 'cancelled')
                """
            )
            active.update(_normal_path(Path(row[0])) for row in rows if row[0])
    except (OSError, sqlite3.Error) as exc:
        return set(), f"fail-closed: Kanban active-worktree query failed: {exc}"

    if not proc_root.exists():
        return active, "ok"
    try:
        process_dirs = list(proc_root.iterdir())
    except OSError as exc:
        return set(), f"fail-closed: process discovery failed: {exc}"
    for process_dir in process_dirs:
        if not process_dir.name.isdigit():
            continue
        try:
            # Foreign processes can legitimately hide their cwd under /proc
            # policies; only this user's workers can use these worktrees.
            if process_dir.stat().st_uid != os.getuid():
                continue
            cwd = Path(os.readlink(process_dir / "cwd"))
        except FileNotFoundError:
            continue  # Process exited between directory listing and readlink.
        except OSError as exc:
            # Specific per-user session daemons can hide their cwd even from
            # their own UID under ptrace restrictions. They do not run worktree
            # workloads; every unknown unreadable same-user cwd still fails closed.
            try:
                process_name = (process_dir / "comm").read_text().strip()
            except OSError:
                process_name = ""
            if process_name in _UNREADABLE_CWD_NON_WORKER_PROCESSES:
                continue
            return set(), f"fail-closed: process cwd discovery failed: {exc}"
        active.add(_normal_path(cwd))
    return active, "ok"


def _is_within(path: Path, ancestor: Path) -> bool:
    try:
        path.relative_to(ancestor)
    except ValueError:
        return False
    return True


def _mountinfo_mount_points() -> set[Path]:
    """Return Linux mount points, including same-filesystem bind mounts.

    ``os.path.ismount`` does not reliably recognise bind mounts. Mountinfo
    records every mounted target independently of its device number, so it is
    authoritative for the safety check below. Malformed or unreadable mount
    tables are rejected by the caller rather than treated as no mounts.
    """
    mount_points: set[Path] = set()
    for line in Path("/proc/self/mountinfo").read_text(encoding="utf-8").splitlines():
        fields = line.split()
        try:
            separator = fields.index("-")
            encoded_mount_point = fields[4]
        except (IndexError, ValueError) as exc:
            raise ValueError(f"malformed mountinfo entry: {line!r}") from exc
        if separator < 6:
            raise ValueError(f"malformed mountinfo entry: {line!r}")
        mount_point = Path(re.sub(r"\\([0-7]{3})", lambda match: chr(int(match.group(1), 8)), encoded_mount_point))
        if not mount_point.is_absolute():
            raise ValueError(f"non-absolute mount point: {line!r}")
        mount_points.add(Path(os.path.normpath(mount_point)))
    if not mount_points:
        raise ValueError("mountinfo contained no mount points")
    return mount_points


def _contains_mount_or_volume(path: Path, *, boundary_root: Path | None = None) -> bool:
    """Return true unless a dependency cache can be proven to stay on one filesystem.

    ``shutil.rmtree`` descends into nested mounts, so checking only the cache
    directory itself is insufficient. When ``boundary_root`` is supplied,
    its full chain through ``path`` is safety-critical too: a same-filesystem
    bind mount at the allowlisted root or worktree would otherwise be invisible
    below the cache. Any mount boundary, device change, or traversal failure
    rejects the entire cache candidate.
    """
    try:
        root = boundary_root or path
        relative_path = path.relative_to(root)
        root_device = root.stat().st_dev
        mount_points = _mountinfo_mount_points()
        current = root
        for component in ("", *relative_path.parts):
            if component:
                current /= component
            if current in mount_points or current.is_symlink() or os.path.ismount(current):
                return True
            if current.stat().st_dev != root_device:
                return True
        def raise_walk_error(error: OSError) -> None:
            raise error

        for directory, directories, files in os.walk(path, followlinks=False, onerror=raise_walk_error):
            current = Path(directory)
            if current in mount_points or current.is_symlink() or os.path.ismount(current):
                return True
            if current.stat().st_dev != root_device:
                return True
            for name in directories + files:
                child = current / name
                if child in mount_points or child.is_symlink() or os.path.ismount(child):
                    return True
                if child.stat().st_dev != root_device:
                    return True
    except (OSError, ValueError):
        return True
    return False


def _matched_confine_root(path: Path, confine_roots: Sequence[Path]) -> Path | None:
    """Return the single confine root that strictly, lexically contains ``path``.

    Used to bound the deletion-time mount/volume check to the validated
    allowlisted root→cache chain, matching :func:`_confined_parent_fd`'s own
    root selection. A cache outside every confine root — or the fail-closed
    empty default — yields ``None`` so the caller refuses to delete.
    """
    try:
        lexical_path = _lexical_path(path)
    except (OSError, ValueError):
        return None
    for root in confine_roots:
        try:
            root_lex = _lexical_path(root)
        except (OSError, ValueError):
            continue
        if lexical_path != root_lex and _is_within(lexical_path, root_lex):
            return root
    return None


def allowlisted_worktree_roots(
    requested_roots: Sequence[Path],
    *,
    allowlist: Sequence[Path],
) -> tuple[list[Path], str]:
    """Keep only requested worktree roots that lexically match the allowlist.

    Matching is *lexical* (absolute, ``.``/``..``-collapsed, symlinks NOT
    followed) so a symlinked allowlist root can never be rewritten into an
    external target that then slips past the allowlist. Any of the following
    fails the whole worktree plan closed — zero candidates, an explicit status —
    rather than silently trusting the override:

    * a relative root (``.``, ``worktrees``, ``~/…``): rejected before any
      normalisation, without exception;
    * a root that does not lexically equal an allowlist entry;
    * a root whose symlink-resolved location differs from its lexical form,
      which flags a symlinked allowlist root *or* a symlinked parent component
      that would redirect the subtree outside the lexical allowlist.

    Accepted roots are returned in lexical form, so the symlink stays visible to
    ``plan_worktree_cache_actions``' own ``is_symlink`` guard. Missing but
    allowlisted roots are kept; the planner skips the ones that do not exist.
    """
    allowed = {_lexical_path(root) for root in allowlist}
    accepted: list[Path] = []
    seen: set[Path] = set()
    rejected: list[str] = []
    for root in requested_roots:
        if not root.is_absolute():
            rejected.append(f"{root} (not absolute)")
            continue
        lexical = _lexical_path(root)
        if lexical not in allowed:
            rejected.append(f"{root} -> {lexical} (not allowlisted)")
            continue
        if _normal_path(root) != lexical:
            rejected.append(f"{root} -> {_normal_path(root)} (symlink or path escape)")
            continue
        if lexical in seen:
            continue
        seen.add(lexical)
        accepted.append(lexical)
    if rejected:
        return [], "fail-closed: non-allowlisted worktree root(s): " + ", ".join(sorted(rejected))
    return accepted, "ok"


def plan_worktree_cache_actions(
    worktree_roots: Sequence[Path],
    *,
    now: float,
    max_age_seconds: float,
    active_worktrees: set[Path],
) -> tuple[list[DeleteAction], str]:
    """Plan only stale, real ``node_modules`` paths under explicit roots.

    The sole selectable paths are ``<root>/<worktree>/node_modules`` and
    ``<root>/<worktree>/web/node_modules``. Source, .git, volumes, symlinks,
    and unrecognised dependency layouts cannot become deletion candidates.
    """
    actions: list[DeleteAction] = []
    for root in worktree_roots:
        if not root.exists():
            continue
        if root.is_symlink() or not root.is_dir():
            return [], f"fail-closed: invalid worktree root {root}"
        try:
            worktrees = sorted(root.iterdir())
        except OSError as exc:
            return [], f"fail-closed: cannot enumerate worktree root {root}: {exc}"
        for worktree in worktrees:
            if worktree.is_symlink() or not worktree.is_dir():
                continue
            normal_worktree = _normal_path(worktree)
            if any(_is_within(active, normal_worktree) for active in active_worktrees):
                continue
            for cache in (worktree / "node_modules", worktree / "web" / "node_modules"):
                try:
                    if cache.is_symlink() or not cache.is_dir():
                        continue
                    if _contains_mount_or_volume(cache, boundary_root=root):
                        continue
                    cache_stat = cache.stat()
                    if now - cache_stat.st_mtime <= max_age_seconds:
                        continue
                except OSError:
                    continue
                actions.append(
                    DeleteAction(
                        cache,
                        _path_size(cache),
                        "worktree-dependency-cache",
                        st_dev=cache_stat.st_dev,
                        st_ino=cache_stat.st_ino,
                    )
                )
    return actions, "ok"


# openat2(2) resolve flags (Linux >= 5.6). At deletion time these make the
# kernel reject, atomically during path resolution, any component that is a
# mount point (RESOLVE_NO_XDEV covers same-filesystem bind mounts, which share a
# device number and are therefore invisible to an ``st_dev`` check) or a symlink
# (RESOLVE_NO_SYMLINKS). This closes the residual window between the final
# ``_contains_mount_or_volume`` check and the fd descent: a bind mount raced onto
# a worktree/web/cache ancestor after that check can no longer be traversed and
# pinned. The syscall is unavailable on kernels < 5.6, where every call raises
# ``ENOSYS`` and the caller fails closed rather than degrading to a mount-blind
# open.
_SYS_openat2 = 437  # generic syscall number; identical on x86-64 and arm64
_RESOLVE_NO_XDEV = 0x01
_RESOLVE_NO_SYMLINKS = 0x04
_RESOLVE_BENEATH = 0x08


class _OpenHow(ctypes.Structure):
    _fields_ = (
        ("flags", ctypes.c_uint64),
        ("mode", ctypes.c_uint64),
        ("resolve", ctypes.c_uint64),
    )


_libc = ctypes.CDLL(None, use_errno=True)
_libc.syscall.restype = ctypes.c_long


def _openat2(dir_fd: int, name: str, flags: int, resolve: int) -> int:
    """Open ``name`` relative to ``dir_fd`` via the ``openat2`` syscall.

    Any failure raises ``OSError`` — including ``EXDEV`` when ``resolve`` forbids
    the mount crossing, ``ELOOP`` for a symlink, and ``ENOSYS`` on kernels
    without the syscall — so callers fail closed on every ambiguity.
    """
    how = _OpenHow(
        flags=ctypes.c_uint64(flags),
        mode=ctypes.c_uint64(0),
        resolve=ctypes.c_uint64(resolve),
    )
    result = _libc.syscall(
        ctypes.c_long(_SYS_openat2),
        ctypes.c_int(dir_fd),
        ctypes.c_char_p(os.fsencode(name)),
        ctypes.byref(how),
        ctypes.c_size_t(ctypes.sizeof(how)),
    )
    if result < 0:
        err = ctypes.get_errno()
        raise OSError(err, os.strerror(err), name)
    return result


def _confined_parent_fd(leaf: Path, confine_roots: Sequence[Path]) -> tuple[int, str, int] | None:
    """Open and pin the leaf cache directory and its parent against races.

    At deletion time — after planning and the safety re-plan — the candidate is
    re-resolved and confined again, then the allowlisted root is opened with
    ``O_NOFOLLOW`` and every descendant component down to and including the leaf
    is opened with :func:`_openat2` under ``RESOLVE_NO_XDEV | RESOLVE_NO_SYMLINKS
    | RESOLVE_BENEATH``. The kernel therefore rejects, during resolution, both a
    symlink swapped into any component *after* planning (a leaf ``is_symlink()``
    check alone misses a swapped parent) and a mount — including a
    same-filesystem bind mount, invisible to ``st_dev`` — raced onto a
    worktree/web/cache ancestor after the final mount check.

    The leaf directory fd is opened under the same guarantee and **held open**,
    so the caller deletes exclusively through it without ever re-resolving the
    leaf by name (which would reopen a raced-in mount/symlink). The returned
    parent fd pins the leaf's parent, used only for the final ``rmdir`` of the
    now-empty leaf. The caller must close both fds.

    Returns ``(parent_fd, leaf_name, leaf_fd)`` only if the leaf is strictly
    inside an allowlisted root, its realpath still resolves within that root, and
    every component from the root to the leaf is a real (non-symlink, non-mount)
    directory. Any ambiguity — including ``openat2`` being unavailable — →
    ``None`` (skip).
    """
    try:
        lexical_leaf = _lexical_path(leaf)
        matched_root: Path | None = None
        for root in confine_roots:
            root_lex = _lexical_path(root)
            if lexical_leaf != root_lex and _is_within(lexical_leaf, root_lex):
                matched_root = root_lex
                break
        if matched_root is None:
            return None
        # Re-resolve the candidate now: it must still land within the allowlisted
        # root's real location, catching a parent component swapped to a symlink.
        real_root = _normal_path(matched_root)
        real_leaf = Path(os.path.realpath(leaf))
        if real_leaf != real_root and not _is_within(real_leaf, real_root):
            return None
        relative = lexical_leaf.relative_to(matched_root)
        if not relative.parts:
            return None
        # Opening the root with O_NOFOLLOW rejects a root that is itself a symlink.
        parent_fd = os.open(str(matched_root), os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
        try:
            resolve = _RESOLVE_NO_XDEV | _RESOLVE_NO_SYMLINKS | _RESOLVE_BENEATH
            descent_flags = os.O_RDONLY | os.O_DIRECTORY
            for component in relative.parts[:-1]:
                next_fd = _openat2(parent_fd, component, descent_flags, resolve)
                os.close(parent_fd)
                parent_fd = next_fd
            leaf_name = relative.parts[-1]
            # Open (and HOLD) the leaf under the same guarantee: O_DIRECTORY
            # rejects a non-directory, RESOLVE_NO_SYMLINKS a symlinked leaf,
            # RESOLVE_NO_XDEV a leaf that is itself a freshly raced-in mount.
            # Holding this fd is what lets the caller delete without ever
            # re-resolving the leaf name.
            leaf_fd = _openat2(parent_fd, leaf_name, descent_flags, resolve)
        except OSError:
            os.close(parent_fd)
            return None
        return parent_fd, leaf_name, leaf_fd
    except (OSError, ValueError):
        return None


def _fd_rmtree(dir_fd: int) -> None:
    """Delete the *contents* of the directory referenced by ``dir_fd``.

    Every operation is relative to a pinned fd: entries are listed from
    ``dir_fd``, subdirectories are re-opened with :func:`_openat2` under
    ``RESOLVE_NO_XDEV | RESOLVE_NO_SYMLINKS | RESOLVE_BENEATH`` (so a mount or
    symlink raced into any nested component is rejected atomically during
    resolution), and files are removed with fd-relative ``unlink``. No path is
    ever resolved by a name that could be redirected above the pinned fd. Any
    failure propagates as ``OSError`` so the caller fails closed.
    """
    resolve = _RESOLVE_NO_XDEV | _RESOLVE_NO_SYMLINKS | _RESOLVE_BENEATH
    for name in os.listdir(dir_fd):
        info = os.lstat(name, dir_fd=dir_fd)
        if stat.S_ISDIR(info.st_mode):
            child_fd = _openat2(dir_fd, name, os.O_RDONLY | os.O_DIRECTORY, resolve)
            try:
                _fd_rmtree(child_fd)
            finally:
                os.close(child_fd)
            os.rmdir(name, dir_fd=dir_fd)
        else:
            os.unlink(name, dir_fd=dir_fd)


def execute_actions(
    actions: Sequence[DeleteAction],
    *,
    apply: bool,
    log: Callable[[str], None],
    safe_to_delete: Callable[[DeleteAction], bool] | None = None,
    confine_roots: Sequence[Path] = (),
) -> None:
    verb = "DELETE" if apply else "WOULD-DELETE"
    for action in actions:
        pinned: tuple[int, str, int] | None = None
        if apply and action.category == "worktree-dependency-cache":
            # Determine the single validated allowlisted root that bounds this
            # cache. No unambiguous match (including the fail-closed empty
            # default) → refuse before any deletion.
            boundary_root = _matched_confine_root(action.path, confine_roots)
            if boundary_root is None:
                log(f"retention-reap: SKIP category={action.category} path={action.path} reason=confinement")
                continue
            if safe_to_delete is not None and not safe_to_delete(action):
                log(f"retention-reap: SKIP category={action.category} path={action.path} reason=safety-recheck")
                continue
            # Final mount/volume check across the FULL allowlisted root→cache
            # chain, run immediately before fd-confinement so a bind mount or
            # volume inserted at the root/worktree ancestor *after* the safety
            # re-plan — invisible below the cache — still fails the candidate closed.
            if _contains_mount_or_volume(action.path, boundary_root=boundary_root):
                log(f"retention-reap: SKIP category={action.category} path={action.path} reason=mount-or-volume")
                continue
            # Deletion-time confinement: atomically open+pin the whole root→leaf
            # chain (mount/symlink races rejected during resolution) and HOLD the
            # leaf fd, so the delete below never re-resolves the leaf name.
            pinned = _confined_parent_fd(action.path, confine_roots)
            if pinned is None:
                log(f"retention-reap: SKIP category={action.category} path={action.path} reason=confinement")
                continue
            parent_fd, _leaf_name, leaf_fd = pinned
            # fd-identity: the pinned leaf must still be the exact inode planned.
            # Any deviation — a swap between planning and now, an fstat failure,
            # or an uncapturable planned identity — fails closed. This is the
            # last gate before the log/delete, so no DELETE line is ever emitted
            # for a candidate that is not deleted.
            try:
                pinned_stat = os.fstat(leaf_fd)
            except OSError as exc:
                os.close(leaf_fd)
                os.close(parent_fd)
                log(f"retention-reap: SKIP category={action.category} path={action.path} reason=fd-identity:{exc}")
                continue
            if (
                action.st_dev is None
                or action.st_ino is None
                or pinned_stat.st_dev != action.st_dev
                or pinned_stat.st_ino != action.st_ino
            ):
                os.close(leaf_fd)
                os.close(parent_fd)
                log(f"retention-reap: SKIP category={action.category} path={action.path} reason=fd-identity")
                continue
        elif apply and safe_to_delete is not None and not safe_to_delete(action):
            log(f"retention-reap: SKIP category={action.category} path={action.path} reason=safety-recheck")
            continue
        log(f"retention-reap: {verb} category={action.category} path={action.path} size={action.size}")
        if not apply:
            continue
        if pinned is not None:
            parent_fd, leaf_name, leaf_fd = pinned
            try:
                # Delete the leaf's contents exclusively through the pinned,
                # identity-verified fd; then remove the now-empty leaf. A mount
                # or symlink raced onto the leaf name afterwards can only make
                # this final rmdir fail harmlessly (EBUSY/ENOTDIR/ENOTEMPTY) —
                # the bytes are already reclaimed through the fd and no foreign
                # tree is ever re-resolved.
                _fd_rmtree(leaf_fd)
                os.rmdir(leaf_name, dir_fd=parent_fd)
            except FileNotFoundError:
                pass
            except OSError as exc:
                log(f"retention-reap: SKIP category={action.category} path={action.path} reason=delete-error:{exc}")
            finally:
                os.close(leaf_fd)
                os.close(parent_fd)
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
    handle = path.open("a+", encoding="utf-8")
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


def _non_negative_days(value: str) -> float:
    """Parse a non-negative, finite number of days.

    A negative age (e.g. ``-1``) would make ``now - mtime > max_age_seconds``
    almost always true and delete *fresh* caches, so it is rejected at parse time
    rather than silently inverting the age threshold.
    """
    try:
        parsed = float(value)
    except ValueError as exc:
        raise argparse.ArgumentTypeError(f"not a number: {value!r}") from exc
    if not math.isfinite(parsed) or parsed < 0:
        raise argparse.ArgumentTypeError(f"must be a non-negative, finite number of days: {value!r}")
    return parsed


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
    parser.add_argument("--worktree-root", type=Path, action="append", dest="worktree_roots")
    parser.add_argument(
        "--worktree-cache-days", type=_non_negative_days, default=DEFAULT_WORKTREE_CACHE_DAYS
    )
    parser.add_argument("--now", type=float, default=None, help=argparse.SUPPRESS)
    args = parser.parse_args(argv)
    if args.package_roots is not None:
        roots = args.package_roots
        discovery_error = None
    else:
        roots, discovery_error = discover_package_roots(Path.home())
    now = time.time() if args.now is None else args.now

    with exclusive_lock(args.lock_file) as acquired:
        if not acquired:
            print("retention-reap: already running; skipping", file=sys.stderr)
            return 0
        output_actions = plan_output_actions(
            args.outputs_root, now=now, max_age_seconds=args.output_days * 24 * 60 * 60
        )
        if discovery_error:
            browser_actions, browser_status = [], f"fail-closed: {discovery_error}"
        else:
            browser_actions, browser_status = plan_browser_actions(
                args.browser_cache,
                roots,
                browsers_path_override=os.environ.get("PLAYWRIGHT_BROWSERS_PATH"),
            )
        backup_actions = plan_backup_actions(args.kanban_db, keep_sets=args.keep_backup_sets)
        requested_roots = args.worktree_roots or list(DEFAULT_WORKTREE_ROOTS)
        worktree_roots, allow_status = allowlisted_worktree_roots(
            requested_roots, allowlist=DEFAULT_WORKTREE_ROOTS
        )
        active_worktrees, active_status = active_worktree_paths(args.kanban_db)
        if allow_status != "ok":
            worktree_actions, worktree_status = [], allow_status
        elif active_status == "ok":
            worktree_actions, worktree_status = plan_worktree_cache_actions(
                worktree_roots,
                now=now,
                max_age_seconds=args.worktree_cache_days * 24 * 60 * 60,
                active_worktrees=active_worktrees,
            )
        else:
            worktree_actions, worktree_status = [], active_status
        actions = output_actions + browser_actions + backup_actions + worktree_actions

        def safe_to_delete(action: DeleteAction) -> bool:
            if action.category != "worktree-dependency-cache":
                return True
            refreshed_active, refreshed_status = active_worktree_paths(args.kanban_db)
            if refreshed_status != "ok":
                return False
            refreshed_actions, refreshed_plan_status = plan_worktree_cache_actions(
                worktree_roots,
                now=time.time(),
                max_age_seconds=args.worktree_cache_days * 24 * 60 * 60,
                active_worktrees=refreshed_active,
            )
            return refreshed_plan_status == "ok" and any(
                candidate.path == action.path for candidate in refreshed_actions
            )

        execute_actions(
            actions,
            apply=args.apply,
            log=lambda line: print(line, flush=True),
            safe_to_delete=safe_to_delete,
            confine_roots=worktree_roots,
        )
        mode = "apply" if args.apply else "dry-run"
        print(
            "retention-reap: done "
            f"mode={mode} actions={len(actions)} browser_status={browser_status} "
            f"worktree_status={worktree_status}",
            file=sys.stderr,
        )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
