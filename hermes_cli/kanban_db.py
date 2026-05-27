"""SQLite-backed Kanban board for multi-profile, multi-project collaboration.

In a fresh install the board lives at ``<root>/kanban.db`` where
``<root>`` is the **shared Hermes root** (the parent of any active
profile). Profiles intentionally collapse onto a shared board: it IS
the cross-profile coordination primitive. A worker spawned with
``hermes -p <profile>`` joins the same board as the dispatcher that
claimed the task. The same applies to ``<root>/kanban/workspaces/`` and
``<root>/kanban/logs/``.

**Multiple boards (projects):** users can create additional boards to
separate unrelated streams of work (e.g. one per project / repo / domain).
Each board is a directory under ``<root>/kanban/boards/<slug>/`` with
its own ``kanban.db``, ``workspaces/``, and ``logs/``. All boards share
the profile's Hermes home but are otherwise isolated: a worker spawned
for a task on board ``atm10-server`` sees only that board's tasks,
cannot enumerate other boards, and its dispatcher ticks don't touch
other boards' DBs.

The first (and for single-project users, only) board is ``default``.
For back-compat its on-disk DB is ``<root>/kanban.db`` (not
``boards/default/kanban.db``), so installs that predate the boards
feature keep working with zero migration. See :func:`kanban_db_path`.

Board resolution order (highest precedence first, all optional):

* ``board=`` argument passed directly to :func:`connect` / :func:`init_db`
  (explicit — used by the CLI ``--board`` flag and the dashboard
  ``?board=...`` query param).
* ``HERMES_KANBAN_BOARD`` env var (used by the dispatcher to pin workers
  to the board their task lives on — workers cannot see other boards).
* ``HERMES_KANBAN_DB`` env var (pins the DB file path directly — legacy
  override still honoured; highest precedence when the file path itself
  is what the caller wants to force).
* ``<root>/kanban/current`` — a one-line text file holding the slug of
  the "currently selected" board. Written by ``hermes kanban boards
  switch <slug>``. When absent, the active board is ``default``.

In standard installs ``<root>`` is ``~/.hermes``. In Docker / custom
deployments where ``HERMES_HOME`` points outside ``~/.hermes`` (e.g.
``/opt/hermes``), ``<root>`` is ``HERMES_HOME``. Legacy env-var
overrides still work:

* ``HERMES_KANBAN_DB`` — pin the database file path directly.
* ``HERMES_KANBAN_WORKSPACES_ROOT`` — pin the workspaces root directly.
* ``HERMES_KANBAN_HOME`` — pin the umbrella root that anchors kanban
  paths. Useful for tests and unusual deployments.

The dispatcher injects ``HERMES_KANBAN_DB``,
``HERMES_KANBAN_WORKSPACES_ROOT``, and ``HERMES_KANBAN_BOARD`` into
worker subprocess env so workers converge on the exact DB the
dispatcher used to claim their task — even under unusual symlink or
Docker layouts.

Schema is intentionally small: tasks, task_links, task_comments,
task_events.  The ``workspace_kind`` field decouples coordination from git
worktrees so that research / ops / digital-twin workloads work alongside
coding workloads.  See ``docs/hermes-kanban-v1-spec.pdf`` for the full
design specification.

Concurrency strategy: WAL mode + ``BEGIN IMMEDIATE`` for write
transactions + compare-and-swap (CAS) updates on ``tasks.status`` and
``tasks.claim_lock``.  SQLite serializes writers via its WAL lock, so at
most one claimer can win any given task.  Losers observe zero affected
rows and move on -- no retry loops, no distributed-lock machinery.
The CAS coordination is **per-board** — each board is a separate DB,
so multi-board installs get the same atomicity guarantees without any
new locking.
"""

from __future__ import annotations

import contextlib
import copy
import json
import os
import re
import secrets
import shutil
import sqlite3
import subprocess
import sys
import threading
import logging
import tempfile
import time
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any, Iterable, Mapping, Optional

from toolsets import get_toolset_names

_log = logging.getLogger(__name__)
try:
    import yaml
except Exception:  # pragma: no cover - PyYAML is part of Hermes runtime deps
    yaml = None


# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

VALID_STATUSES = {"triage", "todo", "scheduled", "ready", "running", "blocked", "review", "done", "archived"}
VALID_INITIAL_STATUSES = {"running", "blocked"}
VALID_WORKSPACE_KINDS = {"scratch", "worktree", "dir"}
KNOWN_TOOLSET_NAMES = frozenset(name.casefold() for name in get_toolset_names())
KANBAN_REVIEW_LANES = ("FASTLANE_KANBAN", "STANDARD_REVIEW", "CRITICAL_REVIEW")
KANBAN_FASTLANE_DEFAULT = "FASTLANE_KANBAN"
_IS_WINDOWS = sys.platform == "win32"

# A running task's claim is valid for 15 minutes by default; after that the
# next dispatcher tick reclaims it. Workers that outlive this window should
# call ``heartbeat_claim(task_id)`` periodically. In practice most kanban
# workloads either finish within 15m, set a longer claim explicitly, or use
# ``HERMES_KANBAN_CLAIM_TTL_SECONDS`` to raise the default claim window for
# long single-call MCP workflows.
DEFAULT_CLAIM_TTL_SECONDS = 15 * 60


def _resolve_claim_ttl_seconds(ttl_seconds: Optional[int] = None) -> int:
    """Return the effective claim TTL, honoring the kanban env override.

    Explicit call-site values win. Otherwise a positive integer from
    ``HERMES_KANBAN_CLAIM_TTL_SECONDS`` overrides the built-in default.
    Invalid or non-positive env values fall back silently so existing
    installs keep working.
    """
    if ttl_seconds is not None:
        return max(1, int(ttl_seconds))

    raw = os.environ.get("HERMES_KANBAN_CLAIM_TTL_SECONDS", "").strip()
    if raw:
        try:
            parsed = int(raw)
        except ValueError:
            parsed = 0
        if parsed > 0:
            return parsed

    return DEFAULT_CLAIM_TTL_SECONDS


# Worker-context caps so build_worker_context() stays bounded on
# pathological boards (retry-heavy tasks, comment storms, giant
# summaries). Values chosen to fit a typical 100k-char LLM prompt with
# plenty of headroom. Each constant is tuned independently so users
# who need to relax one don't have to relax all of them.
_CTX_MAX_PRIOR_ATTEMPTS = 10      # most recent N prior runs shown in full
_CTX_MAX_COMMENTS       = 30      # most recent N comments shown in full
_CTX_MAX_FIELD_BYTES    = 4 * 1024   # 4 KB per summary/error/metadata/result
_CTX_MAX_BODY_BYTES     = 8 * 1024   # 8 KB per task.body (opening post)
_CTX_MAX_COMMENT_BYTES  = 2 * 1024   # 2 KB per comment


# ---------------------------------------------------------------------------
# Paths
# ---------------------------------------------------------------------------

DEFAULT_BOARD = "default"

# Slug validator: lowercase alphanumerics, digits, hyphens; 1–64 chars.
# Strict enough to stop traversal (`..`) and embedded path separators, loose
# enough that kebab-case names like ``atm10-server`` or ``hermes-agent``
# pass without fuss. Board names with display formatting (spaces, emoji)
# live in ``board.json``; the slug is just the directory name.
_BOARD_SLUG_RE = re.compile(r"^[a-z0-9][a-z0-9\-_]{0,63}$")


def _normalize_board_slug(slug: Optional[str]) -> Optional[str]:
    """Lowercase + strip a slug; validate; return ``None`` for empty."""
    if slug is None:
        return None
    s = str(slug).strip().lower()
    if not s:
        return None
    if not _BOARD_SLUG_RE.match(s):
        raise ValueError(
            f"invalid board slug {slug!r}: must be 1-64 chars, lowercase "
            f"alphanumerics / hyphens / underscores, not starting with '-' or '_'"
        )
    return s


def kanban_home() -> Path:
    """Return the shared Hermes root that anchors the kanban board.

    Resolution order:

    1. ``HERMES_KANBAN_HOME`` env var when set and non-empty (explicit
       override for tests and unusual deployments).
    2. ``get_default_hermes_root()``, which already returns ``<root>``
       when ``HERMES_HOME`` is ``<root>/profiles/<name>``, and returns
       ``HERMES_HOME`` directly for Docker / custom deployments.

    The kanban board is shared across profiles **by design** (see the
    module docstring). Resolving the kanban paths through the active
    profile's ``HERMES_HOME`` would silently fork the board per profile,
    which breaks the dispatcher / worker handoff.
    """
    override = os.environ.get("HERMES_KANBAN_HOME", "").strip()
    if override:
        return Path(override).expanduser()
    from hermes_constants import get_default_hermes_root
    return get_default_hermes_root()


def boards_root() -> Path:
    """Return ``<root>/kanban/boards`` — the parent of non-default board dirs.

    ``default`` is intentionally NOT under this directory — its DB lives at
    ``<root>/kanban.db`` for back-compat with pre-boards installs. This
    function returns the directory where *additional* named boards live,
    used by :func:`list_boards` to enumerate them.
    """
    return kanban_home() / "kanban" / "boards"


def current_board_path() -> Path:
    """Return the path to ``<root>/kanban/current``.

    One-line text file written by ``hermes kanban boards switch <slug>``
    to persist the user's board selection across CLI invocations. Absent
    by default (meaning: active board is ``default``).
    """
    return kanban_home() / "kanban" / "current"


def get_current_board() -> str:
    """Return the active board slug, honouring the resolution chain.

    Order (highest precedence first):

    1. ``HERMES_KANBAN_BOARD`` env var (set by the dispatcher on worker
       spawn, or manually for ad-hoc overrides).
    2. ``<root>/kanban/current`` on disk (set by ``hermes kanban boards
       switch``), but only when that board still exists.
    3. ``DEFAULT_BOARD`` (``"default"``).

    A malformed or stale slug at any step falls through to the next layer
    with a best-effort warning — the dispatcher must never crash because a
    user hand-edited a file or removed a board directory.
    """
    env = os.environ.get("HERMES_KANBAN_BOARD", "").strip()
    if env:
        try:
            normed = _normalize_board_slug(env)
            if normed and board_exists(normed):
                return normed
        except ValueError:
            pass
    try:
        f = current_board_path()
        if f.exists():
            val = f.read_text(encoding="utf-8").strip()
            if val:
                try:
                    normed = _normalize_board_slug(val)
                    if normed and board_exists(normed):
                        return normed
                except ValueError:
                    pass
    except OSError:
        pass
    return DEFAULT_BOARD


def set_current_board(slug: str) -> Path:
    """Persist ``slug`` as the active board. Returns the file written.

    Writes ``<root>/kanban/current``. The caller should validate the slug
    exists first (via :func:`board_exists`) — this function does not —
    so that ``hermes kanban boards switch <typo>`` returns an error
    instead of silently pointing at nothing.
    """
    normed = _normalize_board_slug(slug)
    if not normed:
        raise ValueError("board slug is required")
    path = current_board_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(normed + "\n", encoding="utf-8")
    return path


def clear_current_board() -> None:
    """Remove ``<root>/kanban/current`` so the active board reverts to ``default``."""
    try:
        current_board_path().unlink()
    except FileNotFoundError:
        pass


def board_dir(board: Optional[str] = None) -> Path:
    """Return the on-disk directory for ``board``.

    ``default`` is ``<root>/kanban/boards/default/`` **for metadata only**
    (board.json + workspaces/ + logs/). Its DB file stays at
    ``<root>/kanban.db`` for back-compat — see :func:`kanban_db_path`.

    All other boards live at ``<root>/kanban/boards/<slug>/`` with
    everything inside that directory including the ``kanban.db``.
    """
    slug = _normalize_board_slug(board) or DEFAULT_BOARD
    return boards_root() / slug


def board_exists(board: Optional[str] = None) -> bool:
    """Return True if the board has persisted metadata or a DB on disk.

    ``default`` is considered to always exist — its DB is created
    on first :func:`connect` and there's no way for it to be missing
    in a configuration where the kanban feature is usable at all.
    """
    slug = _normalize_board_slug(board) or DEFAULT_BOARD
    if slug == DEFAULT_BOARD:
        return True
    d = board_dir(slug)
    return (d / "board.json").exists() or (d / "kanban.db").exists()


def _sandbox_mode_enabled() -> bool:
    """Return True when ``HERMES_SANDBOX_MODE`` is set to a truthy value.

    Truthy values: ``1``, ``true``, ``yes``, ``on`` (case-insensitive).
    Anything else (including ``0`` / empty / unset) is False.

    When enabled, :func:`kanban_db_path` and :func:`workspaces_root`
    redirect to ephemeral per-``HERMES_HOME`` sandbox paths so that
    scripts running inside a worker (which inherit live
    ``HERMES_KANBAN_DB`` / ``HERMES_KANBAN_BOARD`` env vars from the
    dispatcher) do not accidentally write tasks/workspaces into the
    production board. See feedback memory
    ``hermes-worker-env-live-db-leak`` for the incident that motivated
    this knob.
    """
    raw = os.environ.get("HERMES_SANDBOX_MODE", "").strip().lower()
    return raw in {"1", "true", "yes", "on"}


def _sandbox_db_path(board: Optional[str] = None) -> Path:
    """Sandbox DB path for the current HERMES_HOME.

    Lives at ``<root>/.kanban-sandbox/<slug>.db`` (hidden directory so
    it sorts away from real kanban data, and distinct file per board
    so a "default" sandbox doesn't shadow other-board test runs).

    The inherited ``HERMES_KANBAN_BOARD`` env var is intentionally
    ignored when ``board`` is None — sandbox mode is opt-in isolation,
    so it must not silently pull in the live-board name. The default
    sandbox board is :data:`DEFAULT_BOARD`.
    """
    slug = _normalize_board_slug(board) or DEFAULT_BOARD
    sandbox_root = kanban_home() / ".kanban-sandbox"
    return sandbox_root / f"{slug}.db"


def _sandbox_workspaces_root() -> Path:
    """Sandbox workspaces root for the current HERMES_HOME."""
    return kanban_home() / ".kanban-sandbox" / "workspaces"


def kanban_db_path(board: Optional[str] = None) -> Path:
    """Return the path to the ``kanban.db`` for ``board``.

    Resolution (highest precedence first):

    0. ``HERMES_SANDBOX_MODE=1`` → ephemeral
       ``<root>/.kanban-sandbox/<slug>.db``. Wins over ``HERMES_KANBAN_DB``
       so scripts running inside a worker can opt out of the inherited
       live-board env vars without unsetting them. See
       :func:`_sandbox_mode_enabled`.
    1. ``HERMES_KANBAN_DB`` env var — pins the path directly. Honoured for
       back-compat and for the dispatcher→worker handoff (defense in
       depth: dispatcher injects this into worker env so workers are
       immune to any path-resolution disagreement).
    2. When ``board`` arg is None, the active board from
       :func:`get_current_board` is used.
    3. Board ``default`` → ``<root>/kanban.db`` (back-compat path).
       Other boards → ``<root>/kanban/boards/<slug>/kanban.db``.
    """
    if _sandbox_mode_enabled():
        return _sandbox_db_path(board)
    override = os.environ.get("HERMES_KANBAN_DB", "").strip()
    if override:
        return Path(override).expanduser()
    slug = _normalize_board_slug(board)
    if slug is None:
        slug = get_current_board()
    if slug == DEFAULT_BOARD:
        return kanban_home() / "kanban.db"
    return board_dir(slug) / "kanban.db"


def workspaces_root(board: Optional[str] = None) -> Path:
    """Return the directory under which ``scratch`` workspaces are created.

    Anchored per-board so workspaces don't leak between projects.
    ``HERMES_KANBAN_WORKSPACES_ROOT`` pins the path directly (highest
    precedence) — the dispatcher injects this into worker env.

    ``default`` keeps the legacy path ``<root>/kanban/workspaces/`` so
    that existing scratch workspaces from before the boards feature are
    preserved. Other boards use ``<root>/kanban/boards/<slug>/workspaces/``.

    When ``HERMES_SANDBOX_MODE=1`` is set the workspaces root is
    redirected to ``<root>/.kanban-sandbox/workspaces/`` so that
    sandboxed scripts don't pollute the production workspaces tree.
    """
    if _sandbox_mode_enabled():
        return _sandbox_workspaces_root()
    override = os.environ.get("HERMES_KANBAN_WORKSPACES_ROOT", "").strip()
    if override:
        return Path(override).expanduser()
    slug = _normalize_board_slug(board)
    if slug is None:
        slug = get_current_board()
    if slug == DEFAULT_BOARD:
        return kanban_home() / "kanban" / "workspaces"
    return board_dir(slug) / "workspaces"


def worker_logs_dir(board: Optional[str] = None) -> Path:
    """Return the directory under which per-task worker logs are written.

    ``default`` keeps the legacy path ``<root>/kanban/logs/``. Other
    boards use ``<root>/kanban/boards/<slug>/logs/``. Logs follow the
    board — makes ``hermes kanban log`` unambiguous even when multiple
    boards have tasks with the same id.
    """
    slug = _normalize_board_slug(board)
    if slug is None:
        slug = get_current_board()
    if slug == DEFAULT_BOARD:
        return kanban_home() / "kanban" / "logs"
    return board_dir(slug) / "logs"


def board_metadata_path(board: Optional[str] = None) -> Path:
    """Return the path to ``board.json`` for ``board``.

    Stores display metadata (display name, description, icon, color,
    created_at). The on-disk slug is the canonical identity; this file
    is purely for presentation in the CLI / dashboard.
    """
    slug = _normalize_board_slug(board) or DEFAULT_BOARD
    return board_dir(slug) / "board.json"


def _default_board_display_name(slug: str) -> str:
    """Turn a slug into a reasonable default display name.

    ``atm10-server`` → ``Atm10 Server``. Users can override via
    ``board.json`` but the default should look presentable in the
    dashboard without any follow-up editing.
    """
    return " ".join(part.capitalize() for part in slug.replace("_", "-").split("-") if part) or slug


def read_board_metadata(board: Optional[str] = None) -> dict:
    """Return ``board.json`` contents (or synthesized defaults).

    Never raises — a missing / malformed ``board.json`` falls back to a
    synthesised entry so the dashboard always has something to render.
    Includes the canonical ``slug`` and ``db_path`` so the caller
    doesn't need to reconstruct them.
    """
    slug = _normalize_board_slug(board) or DEFAULT_BOARD
    meta: dict[str, Any] = {
        "slug": slug,
        "name": _default_board_display_name(slug),
        "description": "",
        "icon": "",
        "color": "",
        "default_workdir": None,
        "created_at": None,
        "archived": False,
    }
    try:
        p = board_metadata_path(slug)
        if p.exists():
            raw = json.loads(p.read_text(encoding="utf-8"))
            if isinstance(raw, dict):
                # Never let the metadata file claim a different slug than
                # its directory — trust the filesystem.
                raw["slug"] = slug
                meta.update(raw)
    except (OSError, json.JSONDecodeError):
        pass
    meta["db_path"] = str(kanban_db_path(slug))
    return meta


def write_board_metadata(
    board: Optional[str],
    *,
    name: Optional[str] = None,
    description: Optional[str] = None,
    icon: Optional[str] = None,
    color: Optional[str] = None,
    archived: Optional[bool] = None,
    default_workdir: Optional[str] = None,
) -> dict:
    """Create / update ``board.json`` for ``board``.

    Preserves any existing fields not mentioned in the call. Sets
    ``created_at`` on first write. Returns the resulting metadata dict.
    """
    slug = _normalize_board_slug(board) or DEFAULT_BOARD
    meta = read_board_metadata(slug)
    # Preserve existing DB-derived fields — they get re-computed each
    # read but shouldn't be written into board.json.
    meta.pop("db_path", None)
    if name is not None:
        meta["name"] = str(name).strip() or _default_board_display_name(slug)
    if description is not None:
        meta["description"] = str(description)
    if icon is not None:
        meta["icon"] = str(icon)
    if color is not None:
        meta["color"] = str(color)
    if archived is not None:
        meta["archived"] = bool(archived)
    if default_workdir is not None:
        meta["default_workdir"] = str(default_workdir) if default_workdir else None
    if not meta.get("created_at"):
        meta["created_at"] = int(time.time())
    path = board_metadata_path(slug)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(meta, indent=2, ensure_ascii=False) + "\n",
        encoding="utf-8",
    )
    meta["db_path"] = str(kanban_db_path(slug))
    return meta


def create_board(
    slug: str,
    *,
    name: Optional[str] = None,
    description: Optional[str] = None,
    icon: Optional[str] = None,
    color: Optional[str] = None,
    default_workdir: Optional[str] = None,
) -> dict:
    """Create a new board directory + DB + metadata. Idempotent.

    Returns the resulting metadata. Raises :class:`ValueError` for a
    malformed slug; returns the existing metadata (not an error) if the
    board already exists — matching ``mkdir -p`` semantics.
    """
    normed = _normalize_board_slug(slug)
    if not normed:
        raise ValueError("board slug is required")
    meta = write_board_metadata(
        normed,
        name=name,
        description=description,
        icon=icon,
        color=color,
        default_workdir=default_workdir,
    )
    # Touch the DB so list_boards() sees it immediately.
    init_db(board=normed)
    return meta


def list_boards(*, include_archived: bool = True) -> list[dict]:
    """Enumerate all boards that exist on disk.

    Always includes ``default`` (even when the ``boards/default/``
    metadata dir doesn't exist, because its DB is at the legacy path).
    Other boards are discovered by scanning ``boards/`` for subdirectories
    that either contain a ``kanban.db`` or a ``board.json``.

    Returns a list of metadata dicts, sorted with ``default`` first and
    the rest alphabetically.
    """
    entries: list[dict] = []
    seen: set[str] = set()

    # Default board is always first.
    entries.append(read_board_metadata(DEFAULT_BOARD))
    seen.add(DEFAULT_BOARD)

    root = boards_root()
    if root.is_dir():
        for child in sorted(root.iterdir(), key=lambda p: p.name.lower()):
            if not child.is_dir():
                continue
            slug = child.name
            # Keep slug normalisation soft for discovery — but skip dirs
            # that don't parse as valid slugs so we don't surface junk.
            try:
                normed = _normalize_board_slug(slug)
            except ValueError:
                continue
            if not normed or normed in seen:
                continue
            has_db = (child / "kanban.db").exists()
            has_meta = (child / "board.json").exists()
            if not (has_db or has_meta):
                continue
            meta = read_board_metadata(normed)
            if meta.get("archived") and not include_archived:
                continue
            entries.append(meta)
            seen.add(normed)
    return entries


def remove_board(slug: str, *, archive: bool = True) -> dict:
    """Remove or archive a board.

    ``archive=True`` (default) moves the board's directory to
    ``<root>/kanban/boards/_archived/<slug>-<timestamp>/`` so the data
    is recoverable. ``archive=False`` deletes the directory outright.

    The ``default`` board cannot be removed — raises :class:`ValueError`.
    Returns a summary dict describing what happened (``{"slug", "action",
    "new_path"}``).
    """
    normed = _normalize_board_slug(slug)
    if not normed:
        raise ValueError("board slug is required")
    if normed == DEFAULT_BOARD:
        raise ValueError("the 'default' board cannot be removed")
    d = board_dir(normed)
    if not d.exists():
        raise ValueError(f"board {normed!r} does not exist")

    # If the user removed the currently-active board, revert to default.
    if get_current_board() == normed:
        clear_current_board()

    # A concurrent connect(board=normed) after the rename/delete recreates
    # an empty sqlite file via mkdir(exist_ok=True); the cache entry must be
    # dropped first so the schema init pass re-runs on that fresh file.
    _INITIALIZED_PATHS.discard(str((d / "kanban.db").resolve()))

    if archive:
        archive_root = boards_root() / "_archived"
        archive_root.mkdir(parents=True, exist_ok=True)
        ts = int(time.time())
        target = archive_root / f"{normed}-{ts}"
        # Avoid collision on rapid double-archives.
        suffix = 1
        while target.exists():
            target = archive_root / f"{normed}-{ts}-{suffix}"
            suffix += 1
        d.rename(target)
        return {"slug": normed, "action": "archived", "new_path": str(target)}
    else:
        import shutil
        shutil.rmtree(d)
        return {"slug": normed, "action": "deleted", "new_path": ""}


# ---------------------------------------------------------------------------
# Data classes
# ---------------------------------------------------------------------------

@dataclass
class Task:
    """In-memory view of a row from the ``tasks`` table."""

    id: str
    title: str
    body: Optional[str]
    assignee: Optional[str]
    status: str
    priority: int
    created_by: Optional[str]
    created_at: int
    started_at: Optional[int]
    completed_at: Optional[int]
    workspace_kind: str
    workspace_path: Optional[str]
    claim_lock: Optional[str]
    claim_expires: Optional[int]
    tenant: Optional[str]
    branch_name: Optional[str] = None
    result: Optional[str] = None
    idempotency_key: Optional[str] = None
    # Unified non-success counter. Incremented on any of:
    #   * spawn failure (dispatcher couldn't launch the worker)
    #   * timed_out outcome (worker exceeded max_runtime_seconds)
    #   * crashed outcome (worker PID vanished)
    # Reset to 0 only on a successful completion. See
    # ``_record_task_failure`` for the circuit-breaker trip rule.
    # (Pre-rename column: ``spawn_failures``.)
    consecutive_failures: int = 0
    worker_pid: Optional[int] = None
    # Short excerpt of the last failure's error text (any outcome, not
    # just spawn). Pre-rename column: ``last_spawn_error``.
    last_failure_error: Optional[str] = None
    max_runtime_seconds: Optional[int] = None
    last_heartbeat_at: Optional[int] = None
    current_run_id: Optional[int] = None
    workflow_template_id: Optional[str] = None
    current_step_key: Optional[str] = None
    # Force-loaded skills for the worker on this task. Stored as a JSON
    # array of skill names. None/empty = no per-task preloaded skills;
    # dispatcher guidance is injected separately via KANBAN_GUIDANCE.
    skills: Optional[list] = None
    model_override: Optional[str] = None
    # Per-task override for the consecutive-failure circuit breaker.
    # The value is the failure count at which the breaker trips — e.g.
    # ``max_retries=1`` blocks on the first failure (zero retries),
    # ``max_retries=3`` blocks on the third (two retries allowed).
    # ``None`` (the common case) falls through to the dispatcher-level
    # ``kanban.failure_limit`` config, and then to ``DEFAULT_FAILURE_LIMIT``.
    # Name matches the ``--max-retries`` CLI flag on ``kanban create``.
    max_retries: Optional[int] = None
    # Originating chat/agent session id, when the task was created from
    # within an agent loop that propagated ``HERMES_SESSION_ID``. NULL for
    # tasks created from the CLI, the dashboard, or any path that doesn't
    # set the env var. Lets clients render a per-session board without
    # relying on tenant + time-window heuristics.
    session_id: Optional[str] = None

    @classmethod
    def from_row(cls, row: sqlite3.Row) -> "Task":
        keys = set(row.keys())
        # Parse skills JSON blob if present
        skills_value: Optional[list] = None
        if "skills" in keys and row["skills"]:
            try:
                parsed = json.loads(row["skills"])
                if isinstance(parsed, list):
                    skills_value = [str(s) for s in parsed if s]
            except Exception:
                skills_value = None
        return cls(
            id=row["id"],
            title=row["title"],
            body=row["body"],
            assignee=row["assignee"],
            status=row["status"],
            priority=row["priority"],
            created_by=row["created_by"],
            created_at=row["created_at"],
            started_at=row["started_at"],
            completed_at=row["completed_at"],
            workspace_kind=row["workspace_kind"],
            workspace_path=row["workspace_path"],
            branch_name=row["branch_name"] if "branch_name" in keys else None,
            claim_lock=row["claim_lock"],
            claim_expires=row["claim_expires"],
            tenant=row["tenant"] if "tenant" in keys else None,
            result=row["result"] if "result" in keys else None,
            idempotency_key=row["idempotency_key"] if "idempotency_key" in keys else None,
            consecutive_failures=(
                row["consecutive_failures"] if "consecutive_failures" in keys
                # Pre-migration fallback: ``_migrate_add_optional_columns`` always
                # adds ``consecutive_failures`` now, so this branch is only reachable
                # on a DB that was never opened since pre-#20410 code ran. Keep for
                # belt-and-suspenders safety; in practice it is dead code post-migration.
                else (row["spawn_failures"] if "spawn_failures" in keys else 0)
            ),
            worker_pid=row["worker_pid"] if "worker_pid" in keys else None,
            last_failure_error=(
                row["last_failure_error"] if "last_failure_error" in keys
                # Same belt-and-suspenders fallback as consecutive_failures above.
                else (row["last_spawn_error"] if "last_spawn_error" in keys else None)
            ),
            max_runtime_seconds=(
                row["max_runtime_seconds"] if "max_runtime_seconds" in keys else None
            ),
            last_heartbeat_at=(
                row["last_heartbeat_at"] if "last_heartbeat_at" in keys else None
            ),
            current_run_id=(
                row["current_run_id"] if "current_run_id" in keys else None
            ),
            workflow_template_id=(
                row["workflow_template_id"] if "workflow_template_id" in keys else None
            ),
            current_step_key=(
                row["current_step_key"] if "current_step_key" in keys else None
            ),
            skills=skills_value,
            model_override=row["model_override"] if "model_override" in keys and row["model_override"] else None,
            max_retries=(
                row["max_retries"] if "max_retries" in keys else None
            ),
            session_id=(
                row["session_id"] if "session_id" in keys else None
            ),
        )


@dataclass
class Run:
    """In-memory view of a ``task_runs`` row.

    A run is one attempt to execute a task — created on claim, closed
    on complete/block/crash/timeout/spawn_failure/reclaim. Multiple runs
    per task when retries happen. Carries the claim machinery, PID,
    heartbeat, and the structured handoff summary that downstream workers
    read via ``build_worker_context``.
    """

    id: int
    task_id: str
    profile: Optional[str]
    step_key: Optional[str]
    status: str
    claim_lock: Optional[str]
    claim_expires: Optional[int]
    worker_pid: Optional[int]
    max_runtime_seconds: Optional[int]
    last_heartbeat_at: Optional[int]
    started_at: int
    ended_at: Optional[int]
    outcome: Optional[str]
    summary: Optional[str]
    metadata: Optional[dict]
    error: Optional[str]

    @classmethod
    def from_row(cls, row: sqlite3.Row) -> "Run":
        try:
            meta = json.loads(row["metadata"]) if row["metadata"] else None
        except Exception:
            meta = None
        return cls(
            id=int(row["id"]),
            task_id=row["task_id"],
            profile=row["profile"],
            step_key=row["step_key"],
            status=row["status"],
            claim_lock=row["claim_lock"],
            claim_expires=row["claim_expires"],
            worker_pid=row["worker_pid"],
            max_runtime_seconds=row["max_runtime_seconds"],
            last_heartbeat_at=row["last_heartbeat_at"],
            started_at=int(row["started_at"]),
            ended_at=(int(row["ended_at"]) if row["ended_at"] is not None else None),
            outcome=row["outcome"],
            summary=row["summary"],
            metadata=meta,
            error=row["error"],
        )


@dataclass
class Comment:
    id: int
    task_id: str
    author: str
    body: str
    created_at: int


@dataclass
class Event:
    id: int
    task_id: str
    kind: str
    payload: Optional[dict]
    created_at: int
    run_id: Optional[int] = None


# ---------------------------------------------------------------------------
# Schema
# ---------------------------------------------------------------------------

SCHEMA_SQL = """
CREATE TABLE IF NOT EXISTS tasks (
    id                   TEXT PRIMARY KEY,
    title                TEXT NOT NULL,
    body                 TEXT,
    assignee             TEXT,
    status               TEXT NOT NULL,
    priority             INTEGER DEFAULT 0,
    created_by           TEXT,
    created_at           INTEGER NOT NULL,
    started_at           INTEGER,
    completed_at         INTEGER,
    workspace_kind       TEXT NOT NULL DEFAULT 'scratch',
    workspace_path       TEXT,
    branch_name          TEXT,
    claim_lock           TEXT,
    claim_expires        INTEGER,
    tenant               TEXT,
    result               TEXT,
    idempotency_key      TEXT,
    -- Unified consecutive-failure counter. Incremented on spawn
    -- failure, timeout, or crash; reset only on successful completion.
    -- The circuit breaker in _record_task_failure trips when this
    -- exceeds DEFAULT_FAILURE_LIMIT consecutive non-successes.
    consecutive_failures INTEGER NOT NULL DEFAULT 0,
    worker_pid           INTEGER,
    -- Short excerpt of the most recent failure's error text.
    last_failure_error   TEXT,
    max_runtime_seconds  INTEGER,
    last_heartbeat_at    INTEGER,
    -- Pointer into task_runs for the currently-active run (NULL if no
    -- run is in-flight). Denormalised for cheap reads.
    current_run_id       INTEGER,
    -- Forward-compat for v2 workflow routing. In v1 the kernel writes
    -- these when the task is opted into a template but otherwise ignores
    -- them; the dispatcher doesn't consult them for routing yet.
    workflow_template_id TEXT,
    current_step_key     TEXT,
    -- Force-loaded per-task skills, stored as JSON.
    -- NULL or empty array = no per-task preloaded skills.
    skills               TEXT,
    -- Per-task model override. When set, the dispatcher passes -m <model>
    -- to the worker, overriding the profile's default model. NULL = use
    -- the profile default.
    model_override       TEXT,
    -- Per-task override for the consecutive-failure circuit breaker.
    -- The value is the failure count at which the breaker trips — e.g.
    -- ``max_retries=1`` blocks on the first failure. NULL (the common
    -- case) falls through to the dispatcher-level ``kanban.failure_limit``
    -- config and then ``DEFAULT_FAILURE_LIMIT``.
    max_retries          INTEGER,
    -- Originating chat/agent session id when the task was created from
    -- inside an agent loop that propagated ``HERMES_SESSION_ID``. NULL
    -- for tasks created from the CLI, dashboard, or any path that doesn't
    -- set the env var. Indexed so per-session list queries stay cheap on
    -- larger boards.
    session_id           TEXT
);

CREATE TABLE IF NOT EXISTS task_links (
    parent_id  TEXT NOT NULL,
    child_id   TEXT NOT NULL,
    PRIMARY KEY (parent_id, child_id)
);

CREATE TABLE IF NOT EXISTS task_comments (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    task_id    TEXT NOT NULL,
    author     TEXT NOT NULL,
    body       TEXT NOT NULL,
    created_at INTEGER NOT NULL
);

CREATE TABLE IF NOT EXISTS task_events (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    task_id    TEXT NOT NULL,
    run_id     INTEGER,
    kind       TEXT NOT NULL,
    payload    TEXT,
    created_at INTEGER NOT NULL
);

-- Historical attempt record. Each time the dispatcher claims a task, a
-- new row is created here; claim state, PID, heartbeat, runtime cap,
-- and structured summary all live on the run, not the task. Multiple
-- rows per task id when the task was retried after crash/timeout/block.
-- v2 of the kanban schema will use ``step_key`` to drive per-stage
-- workflow routing; in v1 the column is nullable and unused (kernel
-- ignores it).
CREATE TABLE IF NOT EXISTS task_runs (
    id                  INTEGER PRIMARY KEY AUTOINCREMENT,
    task_id             TEXT NOT NULL,
    profile             TEXT,
    step_key            TEXT,
    status              TEXT NOT NULL,
    -- status: running | done | blocked | crashed | timed_out | failed | released
    claim_lock          TEXT,
    claim_expires       INTEGER,
    worker_pid          INTEGER,
    max_runtime_seconds INTEGER,
    last_heartbeat_at   INTEGER,
    started_at          INTEGER NOT NULL,
    ended_at            INTEGER,
    outcome             TEXT,
    -- outcome: completed | blocked | crashed | timed_out | spawn_failed |
    --          gave_up | reclaimed | (null while still running)
    summary             TEXT,
    metadata            TEXT,
    error               TEXT,
    worker_exit_kind    TEXT,
    worker_exit_code    INTEGER,
    worker_protocol_state TEXT,
    worker_failure_fingerprint TEXT
);

-- Subscription from a gateway source (platform + chat + thread) to a
-- task. The gateway's kanban-notifier watcher tails task_events and
-- pushes ``completed`` / ``blocked`` / ``spawn_auto_blocked`` events to
-- the original requester so human-in-the-loop workflows close the loop.
CREATE TABLE IF NOT EXISTS kanban_notify_subs (
    task_id       TEXT NOT NULL,
    platform      TEXT NOT NULL,
    chat_id       TEXT NOT NULL,
    thread_id     TEXT NOT NULL DEFAULT '',
    user_id       TEXT,
    notifier_profile TEXT,
    created_at    INTEGER NOT NULL,
    last_event_id INTEGER NOT NULL DEFAULT 0,
    PRIMARY KEY (task_id, platform, chat_id, thread_id)
);

CREATE INDEX IF NOT EXISTS idx_tasks_assignee_status ON tasks(assignee, status);
CREATE INDEX IF NOT EXISTS idx_tasks_status          ON tasks(status);
CREATE INDEX IF NOT EXISTS idx_links_child           ON task_links(child_id);
CREATE INDEX IF NOT EXISTS idx_links_parent          ON task_links(parent_id);
CREATE INDEX IF NOT EXISTS idx_comments_task         ON task_comments(task_id, created_at);
CREATE INDEX IF NOT EXISTS idx_events_task           ON task_events(task_id, created_at);
CREATE INDEX IF NOT EXISTS idx_runs_task             ON task_runs(task_id, started_at);
CREATE INDEX IF NOT EXISTS idx_runs_status           ON task_runs(status);
CREATE INDEX IF NOT EXISTS idx_notify_task           ON kanban_notify_subs(task_id);
"""


# ---------------------------------------------------------------------------
# Connection helpers
# ---------------------------------------------------------------------------

_INITIALIZED_PATHS: set[str] = set()
_INIT_LOCK = threading.RLock()
_SQLITE_HEADER = b"SQLite format 3\x00"


def _looks_like_tls_record_at(data: bytes, offset: int) -> bool:
    """Return True for a TLS record header at ``data[offset:]``."""
    if len(data) < offset + 5:
        return False
    content_type = data[offset]
    major = data[offset + 1]
    minor = data[offset + 2]
    length = int.from_bytes(data[offset + 3:offset + 5], "big")
    return (
        content_type in {0x14, 0x15, 0x16, 0x17}
        and major == 0x03
        and minor in {0x00, 0x01, 0x02, 0x03, 0x04}
        and 0 < length <= 18432
    )


def _validate_sqlite_header(path: Path) -> None:
    """Fail early with an actionable error for non-SQLite Kanban DB files.

    ``sqlite3.connect()`` creates missing and zero-byte files, so those are
    allowed. Existing non-empty files must have the SQLite header before we
    hand them to SQLite/WAL setup. This keeps corrupted page-0 failures from
    being collapsed into a generic PRAGMA error and lets the gateway's corrupt
    board handling identify the board by fingerprint.
    """
    try:
        stat = path.stat()
    except FileNotFoundError:
        return
    except OSError:
        return
    if stat.st_size == 0:
        return
    try:
        with path.open("rb") as handle:
            head = handle.read(64)
    except OSError:
        return
    if head.startswith(_SQLITE_HEADER):
        return
    signature = ""
    if head.startswith(b"SQLit") and _looks_like_tls_record_at(head, 5):
        signature = " (TLS record header detected at byte offset 5)"
    elif _looks_like_tls_record_at(head, 0):
        signature = " (TLS record header detected at byte offset 0)"
    raise sqlite3.DatabaseError(
        "file is not a database: invalid SQLite header for "
        f"{path}{signature}; first_32={head[:32].hex(' ')}"
    )


class KanbanDbCorruptError(RuntimeError):
    """Raised when an existing kanban DB file fails integrity checks.

    Fail-closed guard against silent recreation of a corrupt board file,
    which would otherwise destroy the user's tasks. Carries both the
    original path and the timestamped backup we made before refusing.
    """

    def __init__(self, db_path: Path, backup_path: Optional[Path], reason: str):
        self.db_path = db_path
        self.backup_path = backup_path
        self.reason = reason
        backup_str = str(backup_path) if backup_path is not None else "<backup failed>"
        super().__init__(
            f"Refusing to open corrupt kanban DB at {db_path}: {reason}. "
            f"Original preserved; backup at {backup_str}."
        )


def _backup_corrupt_db(path: Path) -> Optional[Path]:
    """Copy a corrupt DB (and its WAL/SHM sidecars) to a timestamped backup.

    Returns the backup path of the main DB file, or ``None`` if the copy
    itself failed (the caller still raises loudly in that case).

    Writes are confined to the original DB's parent directory. The
    backup basename is derived purely from ``path.name``, never from
    caller-supplied directory segments — no traversal is possible.
    """
    # Resolve once and pin the parent so subsequent path operations cannot
    # escape it. ``Path.resolve()`` collapses any ``..`` segments and
    # symlinks, and we only ever write inside ``parent``.
    resolved = path.resolve()
    parent = resolved.parent
    base_name = resolved.name  # basename only
    stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    candidate = parent / f"{base_name}.corrupt.{stamp}.bak"
    # Defensive: candidate must still be inside parent after construction.
    # f-string interpolation of ``base_name`` cannot escape ``parent``
    # because ``base_name`` is itself a resolved basename, but assert it
    # anyway so static analyzers can see the containment guarantee.
    if candidate.parent != parent:
        return None
    counter = 0
    while candidate.exists():
        counter += 1
        candidate = parent / f"{base_name}.corrupt.{stamp}.{counter}.bak"
        if candidate.parent != parent:
            return None
    try:
        shutil.copy2(resolved, candidate)
    except OSError:
        return None
    for suffix in ("-wal", "-shm"):
        sidecar = parent / (base_name + suffix)
        if sidecar.parent != parent or not sidecar.exists():
            continue
        try:
            sidecar_backup = parent / (candidate.name + suffix)
            if sidecar_backup.parent != parent:
                continue
            shutil.copy2(sidecar, sidecar_backup)
        except OSError:
            pass
    return candidate


def _guard_existing_db_is_healthy(path: Path) -> None:
    """Run ``PRAGMA integrity_check`` on an existing non-empty DB file.

    Opens the probe in read/write mode so SQLite can recover or
    checkpoint a healthy WAL/hot-journal DB before we declare it
    corrupt. If the file is malformed, copy it (and any WAL/SHM
    sidecars) to a timestamped backup and raise
    :class:`KanbanDbCorruptError` so callers cannot silently recreate
    the schema on top of a damaged DB.

    Transient lock/busy errors (``sqlite3.OperationalError``) are NOT
    treated as corruption; they propagate raw so the caller sees a
    normal lock failure and no spurious ``.corrupt`` backup is made.

    No-op for missing files, zero-byte files (treated as fresh), and
    paths already proven healthy this process (cache hit).

    Path-trust note: ``path`` arrives via :func:`connect`, which itself
    resolves it from an explicit ``db_path`` argument, the
    :func:`kanban_db_path` env-var chain, or the kanban-home default —
    all sources Hermes treats as user-controlled-but-trusted on the
    user's own machine. We additionally resolve the path here and
    confine all filesystem writes to its parent directory so any
    accidental ``..`` segments are collapsed before any I/O happens.
    """
    # Resolve before any I/O. ``Path.resolve()`` normalizes ``..`` and
    # symlinks, giving us a canonical path whose parent dir we can pin.
    try:
        resolved = path.resolve()
    except OSError:
        return
    try:
        if not resolved.exists() or resolved.stat().st_size == 0:
            return
    except OSError:
        return
    if str(resolved) in _INITIALIZED_PATHS:
        return
    reason: Optional[str] = None
    try:
        probe = sqlite3.connect(str(resolved), timeout=5, isolation_level=None)
        try:
            row = probe.execute("PRAGMA integrity_check").fetchone()
        finally:
            probe.close()
        if not row or (row[0] or "").lower() != "ok":
            reason = f"integrity_check returned {row[0] if row else '<no row>'!r}"
    except sqlite3.OperationalError:
        # Lock contention, busy, transient IO — not corruption. Let it propagate.
        raise
    except sqlite3.DatabaseError as exc:
        reason = f"sqlite refused to open file: {exc}"
    if reason is None:
        return
    backup = _backup_corrupt_db(resolved)
    raise KanbanDbCorruptError(resolved, backup, reason)


def connect(
    db_path: Optional[Path] = None,
    *,
    board: Optional[str] = None,
) -> sqlite3.Connection:
    """Open (and initialize if needed) the kanban DB.

    WAL mode is enabled on every connection; it's a no-op after the first
    time but keeps the code robust if the DB file is ever re-created.

    The first connection to a given path auto-runs :func:`init_db` so
    fresh installs and test harnesses that construct `connect()`
    directly don't have to remember a separate init step. Subsequent
    connections skip the schema check via a module-level path cache.

    Path resolution:

    * ``db_path`` explicit → used as-is (legacy callers, tests).
    * ``board`` explicit → resolves to that board's DB.
    * Neither → :func:`kanban_db_path` resolves via
      ``HERMES_KANBAN_DB`` env → ``HERMES_KANBAN_BOARD`` env →
      ``<root>/kanban/current`` → ``default``.
    """
    if db_path is not None:
        path = db_path
    else:
        path = kanban_db_path(board=board)
    path.parent.mkdir(parents=True, exist_ok=True)
    # Cheap byte-level check first — catches the #29507 TLS-overwrite shape
    # and other invalid-header cases without opening a sqlite connection.
    _validate_sqlite_header(path)
    # Full integrity probe — catches corruption past the header (malformed
    # pages, broken internal metadata). Cached per-path after first success
    # via _INITIALIZED_PATHS so it only runs once per process per path.
    _guard_existing_db_is_healthy(path)
    resolved = str(path.resolve())
    conn = sqlite3.connect(str(path), isolation_level=None, timeout=30)
    try:
        conn.row_factory = sqlite3.Row
        with _INIT_LOCK:
            # WAL activation can take an exclusive lock while SQLite creates the
            # sidecar files for a fresh database. Keep it in the same process-local
            # critical section as schema initialization so concurrent gateway
            # startup threads do not race before _INITIALIZED_PATHS is populated.
            # WAL doesn't work on network filesystems (NFS/SMB/FUSE). Shared helper
            # falls back to DELETE with one WARNING so kanban stays usable there.
            # See hermes_state._WAL_INCOMPAT_MARKERS for detection logic.
            from hermes_state import apply_wal_with_fallback
            apply_wal_with_fallback(conn, db_label=f"kanban.db ({path.name})")
            conn.execute("PRAGMA synchronous=NORMAL")
            conn.execute("PRAGMA foreign_keys=ON")
            needs_init = resolved not in _INITIALIZED_PATHS
            if needs_init:
                # Idempotent: runs CREATE TABLE IF NOT EXISTS + the additive
                # migrations. Cached so subsequent connect() calls in the same
                # process are cheap. The lock prevents same-process dispatcher
                # threads from racing through the additive ALTER TABLE pass with
                # stale PRAGMA snapshots during gateway startup.
                conn.executescript(SCHEMA_SQL)
                _migrate_add_optional_columns(conn)
                _INITIALIZED_PATHS.add(resolved)
    except Exception:
        conn.close()
        raise
    return conn


def init_db(
    db_path: Optional[Path] = None,
    *,
    board: Optional[str] = None,
) -> Path:
    """Create the schema if it doesn't exist; return the path used.

    Kept as a public entry point so CLI ``hermes kanban init`` and the
    daemon have something explicit to call. Unlike :func:`connect`'s
    first-time auto-init (which caches by path), ``init_db`` always
    re-runs the migration pass. Callers that know the on-disk schema
    may have drifted — tests that write legacy event kinds directly,
    external tools that upgrade an old DB file — can call this to
    force re-migration.
    """
    if db_path is not None:
        path = db_path
    else:
        path = kanban_db_path(board=board)
    path.parent.mkdir(parents=True, exist_ok=True)
    resolved = str(path.resolve())
    # Clear the cache entry so the underlying connect() re-runs the
    # schema + migration pass unconditionally.
    with _INIT_LOCK:
        _INITIALIZED_PATHS.discard(resolved)
    with contextlib.closing(connect(path)):
        pass
    return path


def _add_column_if_missing(
    conn: sqlite3.Connection, table: str, column: str, ddl: str
) -> bool:
    """Run ``ALTER TABLE <table> ADD COLUMN <ddl>``, idempotent across races.

    Returns ``True`` when the column was actually added by this call.
    Swallows ``duplicate column name`` errors so a concurrent connection
    that ran the same migration first does not crash the dispatcher tick
    (issue #21708).
    """
    try:
        conn.execute(f"ALTER TABLE {table} ADD COLUMN {ddl}")
        return True
    except sqlite3.OperationalError as exc:
        if "duplicate column name" in str(exc).lower():
            return False
        raise


def _migrate_add_optional_columns(conn: sqlite3.Connection) -> None:
    """Add columns that were introduced after v1 release to legacy DBs.

    Called by ``init_db`` so opening an old DB is always safe.
    """
    cols = {row["name"] for row in conn.execute("PRAGMA table_info(tasks)")}
    if "tenant" not in cols:
        _add_column_if_missing(conn, "tasks", "tenant", "tenant TEXT")
    if "result" not in cols:
        _add_column_if_missing(conn, "tasks", "result", "result TEXT")
    if "branch_name" not in cols:
        _add_column_if_missing(conn, "tasks", "branch_name", "branch_name TEXT")
    if "idempotency_key" not in cols:
        _add_column_if_missing(
            conn, "tasks", "idempotency_key", "idempotency_key TEXT"
        )
    # ``idx_tasks_idempotency`` is created unconditionally below alongside
    # the other additive-column indexes — see the block after the
    # legacy-column migration. Creating it here too would be redundant.

    # Refresh after early additive migrations above. Some existing DBs were
    # partially migrated in older releases and can already contain the later
    # columns (for example ``consecutive_failures``) even when this function's
    # initial snapshot did not. Re-snapshot here so the legacy-column migration
    # below is truly idempotent and never re-adds columns that already exist.
    cols = {row["name"] for row in conn.execute("PRAGMA table_info(tasks)")}

    # Legacy column migration: ``spawn_failures`` → ``consecutive_failures``
    # and ``last_spawn_error`` → ``last_failure_error``.
    #
    # Avoid ``ALTER TABLE ... RENAME COLUMN`` for two reasons:
    #   1. Primary: very old DBs may never have had ``spawn_failures`` at
    #      all, so RENAME raises OperationalError: no such column (the crash
    #      reported in issue #20842 after the #20410 update).
    #   2. Secondary: SQLite reparses the whole schema on any RENAME, which
    #      fails if related objects (views, triggers) reference the old name.
    #
    # ADD-first-then-copy is tolerant of both shapes and preserves
    # historical counter values when the legacy columns do exist.
    if "consecutive_failures" not in cols:
        added = _add_column_if_missing(
            conn,
            "tasks",
            "consecutive_failures",
            "consecutive_failures INTEGER NOT NULL DEFAULT 0",
        )
        if added and "spawn_failures" in cols:
            conn.execute(
                "UPDATE tasks SET consecutive_failures = COALESCE(spawn_failures, 0)"
            )
    if "worker_pid" not in cols:
        _add_column_if_missing(conn, "tasks", "worker_pid", "worker_pid INTEGER")
    if "last_failure_error" not in cols:
        added = _add_column_if_missing(
            conn, "tasks", "last_failure_error", "last_failure_error TEXT"
        )
        if added and "last_spawn_error" in cols:
            conn.execute(
                "UPDATE tasks SET last_failure_error = last_spawn_error"
            )
    if "max_runtime_seconds" not in cols:
        _add_column_if_missing(
            conn, "tasks", "max_runtime_seconds", "max_runtime_seconds INTEGER"
        )
    if "last_heartbeat_at" not in cols:
        _add_column_if_missing(
            conn, "tasks", "last_heartbeat_at", "last_heartbeat_at INTEGER"
        )
    if "current_run_id" not in cols:
        _add_column_if_missing(
            conn, "tasks", "current_run_id", "current_run_id INTEGER"
        )
    if "workflow_template_id" not in cols:
        _add_column_if_missing(
            conn, "tasks", "workflow_template_id", "workflow_template_id TEXT"
        )
    if "current_step_key" not in cols:
        _add_column_if_missing(
            conn, "tasks", "current_step_key", "current_step_key TEXT"
        )
    if "skills" not in cols:
        # JSON array of optional specialist skill names the dispatcher
        # force-loads into the worker. Kanban lifecycle guidance is injected
        # separately via KANBAN_GUIDANCE, so NULL is fine for existing rows.
        _add_column_if_missing(conn, "tasks", "skills", "skills TEXT")

    if "max_retries" not in cols:
        # Per-task override for the consecutive-failure circuit breaker.
        # NULL = fall through to the dispatcher-level ``kanban.failure_limit``
        # config, then ``DEFAULT_FAILURE_LIMIT``. Existing rows get NULL,
        # which is the correct default (they keep the global behaviour
        # they were getting before the column existed).
        _add_column_if_missing(conn, "tasks", "max_retries", "max_retries INTEGER")

    if "model_override" not in cols:
        conn.execute("ALTER TABLE tasks ADD COLUMN model_override TEXT")

    if "session_id" not in cols:
        # Originating agent/chat session id, populated when the task is
        # created from within an agent loop that propagated
        # ``HERMES_SESSION_ID`` (e.g. ACP). NULL on legacy rows and on any
        # creation path that doesn't set the env var (CLI, dashboard).
        _add_column_if_missing(
            conn, "tasks", "session_id", "session_id TEXT"
        )

    # Indexes over additive ``tasks`` columns must be created after the
    # columns exist. Keeping them in SCHEMA_SQL breaks legacy boards: SQLite
    # parses each statement in ``executescript`` against the live schema, so a
    # ``CREATE INDEX`` over a missing column aborts initialization before the
    # additive ``ALTER TABLE`` migrations below can run. Re-running them here
    # is cheap thanks to ``IF NOT EXISTS`` and stays correct on fresh DBs
    # (where the columns already exist from SCHEMA_SQL).
    conn.execute("CREATE INDEX IF NOT EXISTS idx_tasks_tenant ON tasks(tenant)")
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_tasks_idempotency ON tasks(idempotency_key)"
    )
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_tasks_session_id ON tasks(session_id)"
    )

    # task_events gained a run_id column; back-fill it as NULL for
    # historical events (they predate runs and can't be attributed).
    ev_cols = {row["name"] for row in conn.execute("PRAGMA table_info(task_events)")}
    if "run_id" not in ev_cols:
        _add_column_if_missing(conn, "task_events", "run_id", "run_id INTEGER")

    # Same ordering rule as the additive ``tasks`` indexes above: create the
    # index after the additive column migration so legacy ``task_events``
    # tables don't fail during SCHEMA_SQL execution before ``run_id`` exists.
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_events_run "
        "ON task_events(run_id, id)"
    )

    notify_table_exists = conn.execute(
        "SELECT name FROM sqlite_master WHERE type='table' AND name='kanban_notify_subs'"
    ).fetchone() is not None
    if notify_table_exists:
        notify_cols = {
            row["name"] for row in conn.execute("PRAGMA table_info(kanban_notify_subs)")
        }
        if "notifier_profile" not in notify_cols:
            _add_column_if_missing(
                conn, "kanban_notify_subs", "notifier_profile", "notifier_profile TEXT"
            )

    # One-shot backfill: any task that is 'running' before runs existed
    # had its claim_lock / claim_expires / worker_pid on the task row.
    # Synthesize a matching task_runs row so subsequent end-run / heartbeat
    # calls have something to write to. Wrapped in write_txn to serialize
    # against any concurrent dispatcher, and the per-row UPDATE uses
    # ``current_run_id IS NULL`` as a CAS guard so a racing claim can't
    # produce an orphaned row if it interleaves with the backfill pass.
    runs_exist = conn.execute(
        "SELECT name FROM sqlite_master WHERE type='table' AND name='task_runs'"
    ).fetchone() is not None
    if runs_exist:
        run_cols = {
            row["name"] for row in conn.execute("PRAGMA table_info(task_runs)")
        }
        if "worker_exit_kind" not in run_cols:
            _add_column_if_missing(
                conn, "task_runs", "worker_exit_kind", "worker_exit_kind TEXT"
            )
        if "worker_exit_code" not in run_cols:
            _add_column_if_missing(
                conn, "task_runs", "worker_exit_code", "worker_exit_code INTEGER"
            )
        if "worker_protocol_state" not in run_cols:
            _add_column_if_missing(
                conn, "task_runs", "worker_protocol_state", "worker_protocol_state TEXT"
            )
        if "worker_failure_fingerprint" not in run_cols:
            _add_column_if_missing(
                conn,
                "task_runs",
                "worker_failure_fingerprint",
                "worker_failure_fingerprint TEXT",
            )

        with write_txn(conn):
            inflight = conn.execute(
                "SELECT id, assignee, claim_lock, claim_expires, worker_pid, "
                "       max_runtime_seconds, last_heartbeat_at, started_at "
                "FROM tasks "
                "WHERE status = 'running' AND current_run_id IS NULL"
            ).fetchall()
            for row in inflight:
                started = row["started_at"] or int(time.time())
                cur = conn.execute(
                    """
                    INSERT INTO task_runs (
                        task_id, profile, status,
                        claim_lock, claim_expires, worker_pid,
                        max_runtime_seconds, last_heartbeat_at,
                        started_at, worker_exit_kind, worker_protocol_state
                    ) VALUES (?, ?, 'running', ?, ?, ?, ?, ?, ?, 'pending', 'pending')
                    """,
                    (
                        row["id"], row["assignee"], row["claim_lock"],
                        row["claim_expires"], row["worker_pid"],
                        row["max_runtime_seconds"], row["last_heartbeat_at"],
                        started,
                    ),
                )
                # CAS: only install the pointer if nothing else claimed
                # the task between our SELECT and here (shouldn't happen
                # under the write_txn, but belt-and-suspenders). If the
                # CAS fails we've got an orphan run_row — mark it
                # reclaimed so it doesn't look in-flight.
                upd = conn.execute(
                    "UPDATE tasks SET current_run_id = ? "
                    "WHERE id = ? AND current_run_id IS NULL",
                    (cur.lastrowid, row["id"]),
                )
                if upd.rowcount != 1:
                    conn.execute(
                        "UPDATE task_runs SET status = 'reclaimed', "
                        "    outcome = 'reclaimed', ended_at = ? "
                        "WHERE id = ?",
                        (int(time.time()), cur.lastrowid),
                    )

    # One-shot event-kind rename pass. The old names ("ready", "priority",
    # "spawn_auto_blocked") still worked but were awkward on the wire;
    # rename them in-place so existing DBs migrate cleanly. Fires once
    # per DB because after the UPDATE no rows match the old kinds.
    _EVENT_RENAMES = (
        # (old, new)
        ("ready",              "promoted"),
        ("priority",           "reprioritized"),
        ("spawn_auto_blocked", "gave_up"),
    )
    for old, new in _EVENT_RENAMES:
        conn.execute(
            "UPDATE task_events SET kind = ? WHERE kind = ?",
            (new, old),
        )


@contextlib.contextmanager
def write_txn(conn: sqlite3.Connection):
    """Context manager for an IMMEDIATE write transaction.

    Use for any multi-statement write (creating a task + link, claiming a
    task + recording an event, etc.).  A claim CAS inside this context is
    atomic -- at most one concurrent writer can succeed.
    """
    conn.execute("BEGIN IMMEDIATE")
    try:
        yield conn
    except Exception:
        conn.execute("ROLLBACK")
        raise
    else:
        conn.execute("COMMIT")


# ---------------------------------------------------------------------------
# ID generation
# ---------------------------------------------------------------------------

def _new_task_id() -> str:
    """Generate a short, URL-safe task id.

    4 hex bytes = ~4.3B possibilities. At 10k tasks the collision
    probability is ~1.2e-5; at 100k it's ~1.2e-3. Previously we used 2
    hex bytes (65k possibilities) which hit the birthday paradox hard:
    ~5% collision probability at 1k tasks, ~50% at 10k. Callers that
    care about idempotency should pass ``idempotency_key`` to
    :func:`create_task` rather than rely on id uniqueness.
    """
    return "t_" + secrets.token_hex(4)


def _claimer_id() -> str:
    """Return a ``host:pid`` string that identifies this claimer."""
    import socket
    try:
        host = socket.gethostname() or "unknown"
    except Exception:
        host = "unknown"
    return f"{host}:{os.getpid()}"


# ---------------------------------------------------------------------------
# Task creation / mutation
# ---------------------------------------------------------------------------

def _canonical_assignee(assignee: Optional[str]) -> Optional[str]:
    """Lowercase-assignee normalization for Kanban rows (dashboard/CLI parity)."""
    if assignee is None:
        return None
    from hermes_cli.profiles import normalize_profile_name

    return normalize_profile_name(assignee)


def _coordinator_control_plane_gate_audit(
    *,
    assignee: Optional[str],
    control_plane_gate: Optional[Mapping[str, Any]],
    internal_test_bypass_control_plane_gate: bool = False,
) -> Optional[dict[str, Any]]:
    """Validate Coordinator task creation at the DB/kernel boundary.

    Model-native tools and the CLI both end here. Keeping the target
    Hub->Reviewer->Coordinator guard in this kernel path prevents direct DB
    callers from bypassing the Reviewer verdict gate.
    """
    if str(assignee or "").strip().lower() != "coordinator":
        return None
    if internal_test_bypass_control_plane_gate:
        return None
    if not isinstance(control_plane_gate, Mapping):
        raise ValueError(
            "coordinator handoff requires control_plane_gate with "
            "hub_plan_spec, reviewer_metadata, coordinator_plan_spec, "
            "and mechanical_fields"
        )

    hub_plan_spec = control_plane_gate.get("hub_plan_spec")
    reviewer_metadata = control_plane_gate.get("reviewer_metadata")
    coordinator_plan_spec = control_plane_gate.get("coordinator_plan_spec")
    mechanical_fields = control_plane_gate.get("mechanical_fields") or []
    if not isinstance(hub_plan_spec, Mapping):
        raise ValueError("coordinator handoff blocked: hub_plan_spec must be an object")
    if reviewer_metadata is not None and not isinstance(reviewer_metadata, Mapping):
        raise ValueError("coordinator handoff blocked: reviewer_metadata must be an object")
    if not isinstance(coordinator_plan_spec, Mapping):
        raise ValueError("coordinator handoff blocked: coordinator_plan_spec must be an object")
    if isinstance(mechanical_fields, str):
        mechanical_fields = [mechanical_fields]
    if not isinstance(mechanical_fields, (list, tuple, set)):
        raise ValueError("coordinator handoff blocked: mechanical_fields must be a list")

    from hermes_cli.control_plane_gate import (
        SubstantiveCoordinatorChangeError,
        coordinator_gate_decision,
    )

    try:
        decision = coordinator_gate_decision(
            hub_plan_spec=hub_plan_spec,
            reviewer_metadata=reviewer_metadata,
            coordinator_plan_spec=coordinator_plan_spec,
            mechanical_fields=[str(item) for item in mechanical_fields],
        )
    except SubstantiveCoordinatorChangeError as exc:
        raise ValueError(f"coordinator handoff blocked: substantive_plan_change: {exc}") from exc

    if not decision.allowed:
        findings = "; ".join(decision.blocking_findings)
        raise ValueError(f"coordinator handoff blocked: {decision.reason}: {findings}")

    return {
        "reason": decision.reason,
        "blocking_findings": decision.blocking_findings,
        "mechanical_diffs": decision.mechanical_diffs,
    }


def create_task(
    conn: sqlite3.Connection,
    *,
    title: str,
    body: Optional[str] = None,
    assignee: Optional[str] = None,
    created_by: Optional[str] = None,
    workspace_kind: str = "scratch",
    workspace_path: Optional[str] = None,
    branch_name: Optional[str] = None,
    tenant: Optional[str] = None,
    priority: int = 0,
    parents: Iterable[str] = (),
    triage: bool = False,
    idempotency_key: Optional[str] = None,
    max_runtime_seconds: Optional[int] = None,
    skills: Optional[Iterable[str]] = None,
    max_retries: Optional[int] = None,
    initial_status: str = "running",
    session_id: Optional[str] = None,
    board: Optional[str] = None,
    control_plane_gate: Optional[Mapping[str, Any]] = None,
    internal_test_bypass_control_plane_gate: bool = False,
) -> str:
    """Create a new task and optionally link it under parent tasks.

    Returns the new task id.  Status is ``ready`` when there are no
    parents (or all parents already ``done``), otherwise ``todo``.
    If ``triage=True``, status is forced to ``triage`` regardless of
    parents — a specifier/triager is expected to promote the task to
    ``todo`` once the spec is fleshed out.

    If ``idempotency_key`` is provided and a non-archived task with the
    same key already exists, returns the existing task's id instead of
    creating a duplicate. Useful for retried webhooks / automation that
    should not double-write.

    ``max_runtime_seconds`` caps how long a worker may run before the
    dispatcher SIGTERMs (then SIGKILLs after a grace window) and
    re-queues the task. ``None`` means no cap (default).

    ``skills`` is an optional list of skill names to force-load into
    the worker when dispatched. Stored as JSON; the dispatcher passes
    each name to ``hermes --skills ...``. Use this to pin a task to a
    specialist skill (e.g. ``skills=["translation"]`` so the worker
    loads the translation skill regardless of the profile's default
    config).
    """
    assignee = _canonical_assignee(assignee)
    gate_audit = _coordinator_control_plane_gate_audit(
        assignee=assignee,
        control_plane_gate=control_plane_gate,
        internal_test_bypass_control_plane_gate=internal_test_bypass_control_plane_gate,
    )
    if not title or not title.strip():
        raise ValueError("title is required")
    if initial_status not in VALID_INITIAL_STATUSES:
        raise ValueError(
            f"initial_status must be one of {sorted(VALID_INITIAL_STATUSES)}"
        )
    if workspace_kind not in VALID_WORKSPACE_KINDS:
        raise ValueError(
            f"workspace_kind must be one of {sorted(VALID_WORKSPACE_KINDS)}, "
            f"got {workspace_kind!r}"
        )
    if branch_name is not None:
        branch_name = str(branch_name).strip() or None
    if branch_name and workspace_kind != "worktree":
        raise ValueError("branch_name is only valid for worktree workspaces")
    parents = tuple(p for p in parents if p)

    # Normalise + validate skills: strip whitespace, drop empties, dedupe
    # (preserving order). Refuse commas inside a single name so we don't
    # invisibly splatter a comma-joined string into one argv slot — the
    # `hermes --skills X,Y` comma syntax is handled in the dispatcher,
    # not here.
    skills_list: Optional[list[str]] = None
    if skills is not None:
        cleaned: list[str] = []
        seen: set[str] = set()
        # Collect all toolset-name confusions up front so the user sees the
        # whole list at once. Raising on the first hit is friendly when the
        # input has one mistake, but agents that confuse skills with toolsets
        # usually pass several at once (`skills=["web", "browser", "terminal"]`)
        # and serial-correcting one per failure round-trips wastes tokens.
        toolset_typos: list[str] = []
        for s in skills:
            if not s:
                continue
            name = str(s).strip()
            if not name:
                continue
            if "," in name:
                raise ValueError(
                    f"skill name cannot contain comma: {name!r} "
                    f"(pass a list of separate names instead of a comma-joined string)"
                )
            if name.casefold() in KNOWN_TOOLSET_NAMES:
                toolset_typos.append(name)
                continue
            if name in seen:
                continue
            seen.add(name)
            cleaned.append(name)
        if toolset_typos:
            quoted = ", ".join(repr(n) for n in toolset_typos)
            noun = "is a toolset name" if len(toolset_typos) == 1 else "are toolset names"
            raise ValueError(
                f"{quoted} {noun}, not skill name(s). "
                "Put toolsets in the assignee profile's `toolsets:` config "
                "instead of per-task skills. Skills are named skill bundles "
                "(e.g. `kanban-worker`, `blogwatcher`); toolsets are runtime "
                "capabilities (e.g. `web`, `browser`, `terminal`)."
            )
        skills_list = cleaned

    # Idempotency check — return the existing task instead of creating a
    # duplicate. Done BEFORE entering write_txn to keep the fast path fast
    # and to avoid holding a write lock during the lookup. Race is
    # acceptable: two concurrent creators with the same key might both
    # insert, at which point both rows exist but the next lookup stabilises.
    if idempotency_key:
        row = conn.execute(
            "SELECT id FROM tasks WHERE idempotency_key = ? "
            "AND status != 'archived' "
            "ORDER BY created_at DESC LIMIT 1",
            (idempotency_key,),
        ).fetchone()
        if row:
            return row["id"]

    now = int(time.time())

    # Resolve workspace_path from board-level default_workdir when the
    # caller did not specify one explicitly. Board defaults represent
    # persistent project checkouts, so only persistent workspace kinds may
    # inherit them. Scratch workspaces are auto-deleted on completion and
    # must stay under the per-board scratch root created by
    # ``resolve_workspace``; inheriting ``default_workdir`` for a scratch
    # task would point cleanup at the user's source tree (#28818). The
    # containment guard in ``_cleanup_workspace`` is the safety rail, but
    # we also stop the bad state from being created in the first place.
    if workspace_path is None and workspace_kind in {"dir", "worktree"}:
        board_slug = board if board else get_current_board()
        board_meta = read_board_metadata(board_slug)
        board_default = board_meta.get("default_workdir")
        if board_default:
            workspace_path = str(board_default)

    # Retry once on the extremely unlikely id collision.
    for attempt in range(2):
        task_id = _new_task_id()
        try:
            with write_txn(conn):
                # Determine task status from parent status, unless the caller
                # parks it directly in blocked for human-ops review or in
                # triage for a specifier.
                if initial_status == "blocked":
                    task_status = "blocked"
                    if parents:
                        missing = _find_missing_parents(conn, parents)
                        if missing:
                            raise ValueError(f"unknown parent task(s): {', '.join(missing)}")
                elif triage:
                    task_status = "triage"
                else:
                    task_status = "ready"
                    if parents:
                        missing = _find_missing_parents(conn, parents)
                        if missing:
                            raise ValueError(f"unknown parent task(s): {', '.join(missing)}")
                        # If any parent is not yet done, we're todo.
                        rows = conn.execute(
                            "SELECT status FROM tasks WHERE id IN "
                            "(" + ",".join("?" * len(parents)) + ")",
                            parents,
                        ).fetchall()
                        if any(r["status"] != "done" for r in rows):
                            task_status = "todo"
                # Even in triage mode we still need to validate parent ids
                # so the eventual link rows don't dangle.
                if triage and parents:
                    missing = _find_missing_parents(conn, parents)
                    if missing:
                        raise ValueError(f"unknown parent task(s): {', '.join(missing)}")

                conn.execute(
                    """
                    INSERT INTO tasks (
                        id, title, body, assignee, status, priority,
                        created_by, created_at, workspace_kind, workspace_path,
                        branch_name, tenant, idempotency_key, max_runtime_seconds,
                        skills, max_retries, session_id
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        task_id,
                        title.strip(),
                        body,
                        assignee,
                        task_status,
                        priority,
                        created_by,
                        now,
                        workspace_kind,
                        workspace_path,
                        branch_name,
                        tenant,
                        idempotency_key,
                        int(max_runtime_seconds) if max_runtime_seconds is not None else None,
                        json.dumps(skills_list) if skills_list is not None else None,
                        int(max_retries) if max_retries is not None else None,
                        session_id,
                    ),
                )
                for pid in parents:
                    conn.execute(
                        "INSERT OR IGNORE INTO task_links (parent_id, child_id) VALUES (?, ?)",
                        (pid, task_id),
                    )
                _append_event(
                    conn,
                    task_id,
                    "created",
                    {
                        "assignee": assignee,
                        "status": task_status,
                        "parents": list(parents),
                        "tenant": tenant,
                        "branch_name": branch_name,
                        "skills": list(skills_list) if skills_list else None,
                    },
                )
                if gate_audit is not None:
                    gate_body = json.dumps({"control_plane_gate": gate_audit}, sort_keys=True)
                    conn.execute(
                        "INSERT INTO task_comments (task_id, author, body, created_at) "
                        "VALUES (?, ?, ?, ?)",
                        (task_id, "control-plane-gate", gate_body, now),
                    )
                    _append_event(
                        conn,
                        task_id,
                        "control_plane_gate_passed",
                        gate_audit,
                    )
            return task_id
        except sqlite3.IntegrityError:
            if attempt == 1:
                raise
            # Retry with a fresh id.
            continue
    raise RuntimeError("unreachable")


def _find_missing_parents(conn: sqlite3.Connection, parents: Iterable[str]) -> list[str]:
    parents = list(parents)
    if not parents:
        return []
    placeholders = ",".join("?" * len(parents))
    rows = conn.execute(
        f"SELECT id FROM tasks WHERE id IN ({placeholders})",
        parents,
    ).fetchall()
    present = {r["id"] for r in rows}
    return [p for p in parents if p not in present]


def get_task(conn: sqlite3.Connection, task_id: str) -> Optional[Task]:
    row = conn.execute("SELECT * FROM tasks WHERE id = ?", (task_id,)).fetchone()
    return Task.from_row(row) if row else None


# Canonical sort-order mappings for ``hermes kanban list --sort``.
# Each value is a raw SQL fragment appended after ``ORDER BY``.
VALID_SORT_ORDERS: dict[str, str] = {
    "created": "created_at ASC, id ASC",
    "created-desc": "created_at DESC, id DESC",
    "priority": "priority DESC, created_at ASC",
    "priority-desc": "priority ASC, created_at ASC",
    "status": "status ASC, created_at ASC",
    "assignee": "assignee ASC, created_at ASC",
    "title": "title ASC, id ASC",
    "updated": "started_at DESC NULLS LAST, created_at DESC",
}


def list_tasks(
    conn: sqlite3.Connection,
    *,
    assignee: Optional[str] = None,
    status: Optional[str] = None,
    tenant: Optional[str] = None,
    session_id: Optional[str] = None,
    include_archived: bool = False,
    limit: Optional[int] = None,
    order_by: Optional[str] = None,
    workflow_template_id: Optional[str] = None,
    current_step_key: Optional[str] = None,
) -> list[Task]:
    query = "SELECT * FROM tasks WHERE 1=1"
    params: list[Any] = []
    if assignee is not None:
        query += " AND assignee = ?"
        params.append(_canonical_assignee(assignee))
    if status is not None:
        if status not in VALID_STATUSES:
            raise ValueError(f"status must be one of {sorted(VALID_STATUSES)}")
        query += " AND status = ?"
        params.append(status)
    if tenant is not None:
        query += " AND tenant = ?"
        params.append(tenant)
    if session_id is not None:
        query += " AND session_id = ?"
        params.append(session_id)
    if workflow_template_id is not None:
        query += " AND workflow_template_id = ?"
        params.append(workflow_template_id)
    if current_step_key is not None:
        query += " AND current_step_key = ?"
        params.append(current_step_key)
    if not include_archived and status != "archived":
        query += " AND status != 'archived'"
    if order_by is not None:
        order_by = order_by.strip().lower()
        if order_by not in VALID_SORT_ORDERS:
            raise ValueError(
                f"order_by must be one of {sorted(VALID_SORT_ORDERS.keys())}"
            )
        query += f" ORDER BY {VALID_SORT_ORDERS[order_by]}"
    else:
        query += " ORDER BY priority DESC, created_at ASC"
    if limit:
        query += f" LIMIT {int(limit)}"
    rows = conn.execute(query, params).fetchall()
    return [Task.from_row(r) for r in rows]


def assign_task(conn: sqlite3.Connection, task_id: str, profile: Optional[str]) -> bool:
    """Assign or reassign a task.  Returns True on success.

    Refuses to reassign a task that's currently running (claim_lock set).
    Reassign after the current run completes if needed.
    """
    profile = _canonical_assignee(profile)
    with write_txn(conn):
        row = conn.execute(
            "SELECT status, claim_lock, assignee FROM tasks WHERE id = ?", (task_id,)
        ).fetchone()
        if not row:
            return False
        if row["claim_lock"] is not None and row["status"] == "running":
            raise RuntimeError(
                f"cannot reassign {task_id}: currently running (claimed). "
                "Wait for completion or reclaim the stale lock first."
            )
        if row["assignee"] != profile:
            # The retry guard is scoped to the task/profile combination. A
            # human reassigning the task is an explicit recovery action, so the
            # new profile should not inherit the previous profile's streak.
            conn.execute(
                "UPDATE tasks SET assignee = ?, consecutive_failures = 0, "
                "last_failure_error = NULL WHERE id = ?",
                (profile, task_id),
            )
        else:
            conn.execute("UPDATE tasks SET assignee = ? WHERE id = ?", (profile, task_id))
        _append_event(conn, task_id, "assigned", {"assignee": profile})
        return True


# ---------------------------------------------------------------------------
# Links
# ---------------------------------------------------------------------------

def link_tasks(conn: sqlite3.Connection, parent_id: str, child_id: str) -> None:
    if parent_id == child_id:
        raise ValueError("a task cannot depend on itself")
    with write_txn(conn):
        missing = _find_missing_parents(conn, [parent_id, child_id])
        if missing:
            raise ValueError(f"unknown task(s): {', '.join(missing)}")
        if _would_cycle(conn, parent_id, child_id):
            raise ValueError(
                f"linking {parent_id} -> {child_id} would create a cycle"
            )
        conn.execute(
            "INSERT OR IGNORE INTO task_links (parent_id, child_id) VALUES (?, ?)",
            (parent_id, child_id),
        )
        # If child was ready but parent is not yet done, demote child to todo.
        parent_status = conn.execute(
            "SELECT status FROM tasks WHERE id = ?", (parent_id,)
        ).fetchone()["status"]
        if parent_status != "done":
            conn.execute(
                "UPDATE tasks SET status = 'todo' WHERE id = ? AND status = 'ready'",
                (child_id,),
            )
        _append_event(
            conn, child_id, "linked",
            {"parent": parent_id, "child": child_id},
        )


def _would_cycle(conn: sqlite3.Connection, parent_id: str, child_id: str) -> bool:
    """Return True if adding parent->child creates a cycle.

    A cycle exists iff ``parent_id`` is already a descendant of
    ``child_id`` via existing parent->child links.  We walk downward
    from ``child_id`` and check whether we reach ``parent_id``.
    """
    seen = set()
    stack = [child_id]
    while stack:
        node = stack.pop()
        if node == parent_id:
            return True
        if node in seen:
            continue
        seen.add(node)
        rows = conn.execute(
            "SELECT child_id FROM task_links WHERE parent_id = ?", (node,)
        ).fetchall()
        stack.extend(r["child_id"] for r in rows)
    return False


def unlink_tasks(conn: sqlite3.Connection, parent_id: str, child_id: str) -> bool:
    with write_txn(conn):
        cur = conn.execute(
            "DELETE FROM task_links WHERE parent_id = ? AND child_id = ?",
            (parent_id, child_id),
        )
        if cur.rowcount:
            _append_event(
                conn, child_id, "unlinked",
                {"parent": parent_id, "child": child_id},
            )
        removed = cur.rowcount > 0
    if removed:
        # Dependency edge removed — re-evaluate promotion eligibility for the
        # child immediately.  Matches the contract of complete_task and
        # unblock_task; without this the child stays stuck in todo until the
        # next dispatcher tick or a manual `hermes kanban recompute` (issue #22459).
        recompute_ready(conn)
    return removed


def parent_ids(conn: sqlite3.Connection, task_id: str) -> list[str]:
    rows = conn.execute(
        "SELECT parent_id FROM task_links WHERE child_id = ? ORDER BY parent_id",
        (task_id,),
    ).fetchall()
    return [r["parent_id"] for r in rows]


def child_ids(conn: sqlite3.Connection, task_id: str) -> list[str]:
    rows = conn.execute(
        "SELECT child_id FROM task_links WHERE parent_id = ? ORDER BY child_id",
        (task_id,),
    ).fetchall()
    return [r["child_id"] for r in rows]


_SUPERSEDING_REVIEW_REWIRED_EVENT = "superseding_review_rewired"
_NEEDS_REVISION_FIX_EVENT = "needs_revision_fix_task_ensured"


def rewire_superseding_review_parent(
    conn: sqlite3.Connection,
    *,
    source_task: str,
    old_review_task: str,
    new_review_task: str,
    reason: str,
) -> dict[str, Any]:
    """Replace a superseded review parent edge with the superseding review.

    This is deliberately explicit and auditable: no reviewer completion or
    dispatcher pass calls it automatically. The helper only rewires the
    dependency edge; it does not complete, unblock, or otherwise finalize the
    source task.
    """
    source_task = str(source_task).strip()
    old_review_task = str(old_review_task).strip()
    new_review_task = str(new_review_task).strip()
    reason = str(reason or "").strip()
    if not source_task or not old_review_task or not new_review_task:
        raise ValueError("source_task, old_review_task, and new_review_task are required")
    if old_review_task == new_review_task:
        raise ValueError("old_review_task and new_review_task must differ")
    if source_task in {old_review_task, new_review_task}:
        raise ValueError("review task cannot be the source task")
    missing = _find_missing_parents(conn, [source_task, old_review_task, new_review_task])
    if missing:
        raise ValueError(f"unknown task(s): {', '.join(missing)}")
    if _would_cycle(conn, new_review_task, source_task):
        raise ValueError(
            f"linking {new_review_task} -> {source_task} would create a cycle"
        )

    with write_txn(conn):
        old_cur = conn.execute(
            "DELETE FROM task_links WHERE parent_id = ? AND child_id = ?",
            (old_review_task, source_task),
        )
        old_parent_removed = old_cur.rowcount > 0
        already_new = conn.execute(
            "SELECT 1 FROM task_links WHERE parent_id = ? AND child_id = ? LIMIT 1",
            (new_review_task, source_task),
        ).fetchone()
        conn.execute(
            "INSERT OR IGNORE INTO task_links (parent_id, child_id) VALUES (?, ?)",
            (new_review_task, source_task),
        )
        new_parent_added = already_new is None
        parent_status = conn.execute(
            "SELECT status FROM tasks WHERE id = ?", (new_review_task,)
        ).fetchone()["status"]
        if parent_status != "done":
            conn.execute(
                "UPDATE tasks SET status = 'todo' WHERE id = ? AND status = 'ready'",
                (source_task,),
            )
        payload = {
            "source_task": source_task,
            "old_review_task": old_review_task,
            "new_review_task": new_review_task,
            "old_parent_removed": old_parent_removed,
            "new_parent_added": new_parent_added,
            "reason": reason,
        }
        _append_event(conn, source_task, _SUPERSEDING_REVIEW_REWIRED_EVENT, payload)
    recompute_ready(conn)
    return payload


def _needs_revision_fix_body(
    source: Task,
    review_task: str,
    reviewer_metadata: dict[str, Any],
    reason: str,
) -> str:
    findings = reviewer_metadata.get("blocking_findings") or []
    required = reviewer_metadata.get("required_verification") or []
    lines = [
        f"# Fix task for NEEDS_REVISION on {source.id}",
        "",
        "revision_chain:",
        f"  source_task: {source.id}",
        f"  review_task: {review_task}",
        "  verdict: NEEDS_REVISION",
        "  finalization_gate: explicit Coordinator/Admin finalization required",
        "",
        "Instruction:",
        "- Fix only the reviewer findings below.",
        "- After completion, request/create a new Reviewer task that supersedes the old review.",
        "- The original source remains blocked until an explicit finalization gate rewires/supersedes and finalizes it.",
        f"- Reason: {reason}",
        "",
        "blocking_findings:",
    ]
    lines.extend([f"  - {item}" for item in findings] or ["  - none provided"])
    lines.append("required_verification:")
    lines.extend([f"  - {item}" for item in required] or ["  - none provided"])
    lines.extend([
        "",
        "reviewer_metadata_json:",
        json.dumps(reviewer_metadata, indent=2, sort_keys=True, ensure_ascii=False),
    ])
    return "\n".join(lines)


def ensure_needs_revision_fix_task(
    conn: sqlite3.Connection,
    *,
    source_task: str,
    review_task: str,
    reviewer_metadata: dict[str, Any],
    reason: str,
) -> dict[str, Any]:
    """Create or return the deterministic fix task for a NEEDS_REVISION verdict.

    Idempotency is keyed by source+review. The source task is not unblocked or
    completed; the returned fix task is an independent follow-up that can later
    produce a new review and explicit superseding/finalization gate.
    """
    source_task = str(source_task).strip()
    review_task = str(review_task).strip()
    reason = str(reason or "").strip()
    if not isinstance(reviewer_metadata, dict):
        raise ValueError("reviewer_metadata must be an object")
    verdict = str(reviewer_metadata.get("verdict") or "").strip().upper()
    if verdict != "NEEDS_REVISION":
        raise ValueError("reviewer_metadata.verdict must be NEEDS_REVISION")
    missing = _find_missing_parents(conn, [source_task, review_task])
    if missing:
        raise ValueError(f"unknown task(s): {', '.join(missing)}")
    source = get_task(conn, source_task)
    if source is None:
        raise ValueError(f"unknown task(s): {source_task}")

    key = f"needs-revision-fix:{source_task}:{review_task}"
    existed = conn.execute(
        "SELECT id FROM tasks WHERE idempotency_key = ? AND status != 'archived' "
        "ORDER BY created_at DESC LIMIT 1",
        (key,),
    ).fetchone()
    fix_task = create_task(
        conn,
        title=f"Fix NEEDS_REVISION for {source_task}: {source.title}",
        body=_needs_revision_fix_body(source, review_task, reviewer_metadata, reason),
        assignee=source.assignee,
        created_by="kanban-review-chain",
        workspace_kind=source.workspace_kind,
        workspace_path=source.workspace_path,
        priority=source.priority,
        idempotency_key=key,
    )
    payload = {
        "source_task": source_task,
        "review_task": review_task,
        "fix_task": fix_task,
        "reason": reason,
    }
    if existed is None:
        with write_txn(conn):
            _append_event(
                conn,
                source_task,
                _NEEDS_REVISION_FIX_EVENT,
                {**payload, "created": True},
            )
    return payload


def parent_results(conn: sqlite3.Connection, task_id: str) -> list[tuple[str, Optional[str]]]:
    """Return ``(parent_id, result)`` for every done parent of ``task_id``."""
    rows = conn.execute(
        """
        SELECT t.id AS id, t.result AS result
        FROM tasks t
        JOIN task_links l ON l.parent_id = t.id
        WHERE l.child_id = ? AND t.status = 'done'
        ORDER BY t.completed_at ASC
        """,
        (task_id,),
    ).fetchall()
    return [(r["id"], r["result"]) for r in rows]


# ---------------------------------------------------------------------------
# Comments & events
# ---------------------------------------------------------------------------

def add_comment(
    conn: sqlite3.Connection, task_id: str, author: str, body: str
) -> int:
    if not body or not body.strip():
        raise ValueError("comment body is required")
    if not author or not author.strip():
        raise ValueError("comment author is required")
    now = int(time.time())
    with write_txn(conn):
        if not conn.execute(
            "SELECT 1 FROM tasks WHERE id = ?", (task_id,)
        ).fetchone():
            raise ValueError(f"unknown task {task_id}")
        cur = conn.execute(
            "INSERT INTO task_comments (task_id, author, body, created_at) "
            "VALUES (?, ?, ?, ?)",
            (task_id, author.strip(), body.strip(), now),
        )
        _append_event(conn, task_id, "commented", {"author": author, "len": len(body)})
        return int(cur.lastrowid or 0)


def list_comments(conn: sqlite3.Connection, task_id: str) -> list[Comment]:
    rows = conn.execute(
        "SELECT * FROM task_comments WHERE task_id = ? ORDER BY created_at ASC",
        (task_id,),
    ).fetchall()
    return [
        Comment(
            id=r["id"],
            task_id=r["task_id"],
            author=r["author"],
            body=r["body"],
            created_at=r["created_at"],
        )
        for r in rows
    ]


def list_events(conn: sqlite3.Connection, task_id: str) -> list[Event]:
    rows = conn.execute(
        "SELECT * FROM task_events WHERE task_id = ? ORDER BY created_at ASC, id ASC",
        (task_id,),
    ).fetchall()
    out = []
    for r in rows:
        try:
            payload = json.loads(r["payload"]) if r["payload"] else None
        except Exception:
            payload = None
        out.append(
            Event(
                id=r["id"],
                task_id=r["task_id"],
                kind=r["kind"],
                payload=payload,
                created_at=r["created_at"],
                run_id=(int(r["run_id"]) if "run_id" in r.keys() and r["run_id"] is not None else None),
            )
        )
    return out


def _append_event(
    conn: sqlite3.Connection,
    task_id: str,
    kind: str,
    payload: Optional[dict] = None,
    *,
    run_id: Optional[int] = None,
) -> None:
    """Record an event row.  Called from within an already-open txn.

    ``run_id`` is optional: pass the current run id so UIs can group
    events by attempt. For events that aren't scoped to a single run
    (task created/edited/archived, dependency promotion) leave it None
    and the row carries NULL.
    """
    now = int(time.time())
    pl = json.dumps(payload, ensure_ascii=False) if payload else None
    conn.execute(
        "INSERT INTO task_events (task_id, run_id, kind, payload, created_at) "
        "VALUES (?, ?, ?, ?, ?)",
        (task_id, run_id, kind, pl, now),
    )


def _end_run(
    conn: sqlite3.Connection,
    task_id: str,
    *,
    outcome: str,
    summary: Optional[str] = None,
    error: Optional[str] = None,
    metadata: Optional[dict] = None,
    status: Optional[str] = None,
) -> Optional[int]:
    """Close the currently-active run for ``task_id`` and clear the pointer.

    ``outcome`` is the semantic result (completed / blocked / crashed /
    timed_out / spawn_failed / gave_up / reclaimed). ``status`` is the
    run-row status (usually just ``outcome``, but callers can pass it
    explicitly). Returns the closed run_id or ``None`` if no active run
    existed (e.g. a CLI user calling ``hermes kanban complete`` on a
    task that was never claimed).
    """
    now = int(time.time())
    row = conn.execute(
        "SELECT current_run_id FROM tasks WHERE id = ?", (task_id,),
    ).fetchone()
    if not row or not row["current_run_id"]:
        return None
    run_id = int(row["current_run_id"])
    conn.execute(
        """
        UPDATE task_runs
           SET status        = ?,
               outcome       = ?,
               summary       = ?,
               error         = ?,
               metadata      = ?,
               ended_at      = ?,
               claim_lock    = NULL,
               claim_expires = NULL
         WHERE id = ?
           AND ended_at IS NULL
        """,
        (
            status or outcome,
            outcome,
            summary,
            error,
            json.dumps(metadata, ensure_ascii=False) if metadata else None,
            now,
            run_id,
        ),
    )
    conn.execute(
        "UPDATE tasks SET current_run_id = NULL WHERE id = ?", (task_id,),
    )
    return run_id


def _current_run_id(conn: sqlite3.Connection, task_id: str) -> Optional[int]:
    row = conn.execute(
        "SELECT current_run_id FROM tasks WHERE id = ?", (task_id,),
    ).fetchone()
    return int(row["current_run_id"]) if row and row["current_run_id"] else None


def _terminal_event_payload(
    conn: sqlite3.Connection,
    task_id: str,
    run_id: Optional[int],
    *,
    outcome: str,
    summary: Optional[str] = None,
    error: Optional[str] = None,
    extra: Optional[dict] = None,
) -> dict:
    """Build a self-contained terminal event payload.

    Terminal state is also persisted on ``task_runs``; duplicating the
    small operator-facing subset here lets dashboards/notifiers reconstruct
    lifecycle state from ``task_events`` without a second table join.
    """
    payload: dict[str, Any] = {"outcome": outcome}
    if run_id is not None:
        payload["run_id"] = int(run_id)
        run = conn.execute(
            "SELECT profile, status, outcome, summary, error, ended_at "
            "FROM task_runs WHERE id = ?",
            (int(run_id),),
        ).fetchone()
        if run:
            payload.update(
                {
                    "profile": run["profile"],
                    "status": run["status"],
                    "outcome": run["outcome"] or outcome,
                    "ended_at": run["ended_at"],
                }
            )
            if summary is None:
                summary = run["summary"]
            if error is None:
                error = run["error"]
    else:
        row = conn.execute(
            "SELECT assignee FROM tasks WHERE id = ?", (task_id,),
        ).fetchone()
        if row:
            payload["profile"] = row["assignee"]
    if summary:
        payload["summary"] = str(summary).strip().splitlines()[0][:400]
    if error:
        payload["error"] = str(error)[:500]
    if extra:
        payload.update(extra)
    return payload


def _synthesize_ended_run(
    conn: sqlite3.Connection,
    task_id: str,
    *,
    outcome: str,
    summary: Optional[str] = None,
    error: Optional[str] = None,
    metadata: Optional[dict] = None,
) -> int:
    """Insert a zero-duration, already-closed run row.

    Used when a terminal transition happens on a task that was never
    claimed (CLI user calling ``hermes kanban complete <ready-task>
    --summary X``, or dashboard "mark done" on a ready task). Without
    this, the handoff fields (summary / metadata / error) would be
    silently dropped: ``_end_run`` is a no-op because there's no
    current run.

    The synthetic run has ``started_at == ended_at == now`` so it
    shows up in attempt history as "instant" and doesn't skew elapsed
    stats. Caller is responsible for leaving ``current_run_id`` NULL
    (or for clearing it elsewhere in the same txn) since this
    function does NOT touch the tasks row.
    """
    now = int(time.time())
    trow = conn.execute(
        "SELECT assignee, current_step_key FROM tasks WHERE id = ?",
        (task_id,),
    ).fetchone()
    profile = trow["assignee"] if trow else None
    step_key = trow["current_step_key"] if trow else None
    cur = conn.execute(
        """
        INSERT INTO task_runs (
            task_id, profile, step_key,
            status, outcome,
            summary, error, metadata,
            started_at, ended_at,
            worker_exit_kind, worker_protocol_state
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'pending', 'pending')
        """,
        (
            task_id, profile, step_key,
            outcome, outcome,
            summary, error,
            json.dumps(metadata, ensure_ascii=False) if metadata else None,
            now, now,
        ),
    )
    return int(cur.lastrowid or 0)


# ---------------------------------------------------------------------------
# Dependency resolution (todo -> ready)
# ---------------------------------------------------------------------------

def _has_sticky_block(conn: sqlite3.Connection, task_id: str) -> bool:
    """Return True when ``task_id`` is sticky-blocked by an explicit
    worker/operator ``kanban_block`` call (#28712).

    A ``blocked`` status can come from two very different sources:

    * **Worker- or operator-initiated** — a worker called
      ``kanban_block(reason="review-required: ...")`` (or somebody ran
      ``hermes kanban block <id>``).  This is a deliberate handoff that
      should stay blocked until an operator unblocks it.  The block tool
      emits a ``"blocked"`` event row in ``task_events``.

    * **Circuit-breaker** — ``_record_task_failure`` tripped after
      repeated crashes / spawn failures / timeouts.  This emits
      ``"gave_up"``, *not* ``"blocked"``, and is meant to recover
      automatically once the underlying conditions change (e.g. parents
      finish, transient infra error clears).

    The cheapest signal that distinguishes the two is the most recent
    ``"blocked"`` / ``"unblocked"`` event for the task.  If the most
    recent one is ``"blocked"`` (or there is a ``"blocked"`` event and
    no ``"unblocked"`` event has fired since), the task is sticky and
    ``recompute_ready`` must *not* auto-promote it.

    Returns ``False`` when there is no such event at all (e.g. the task
    was set to ``status='blocked'`` by the circuit breaker or by direct
    DB manipulation) — preserves the pre-#28712 auto-recover semantics
    for that path.
    """
    row = conn.execute(
        "SELECT kind FROM task_events "
        "WHERE task_id = ? AND kind IN ('blocked', 'unblocked') "
        "ORDER BY id DESC LIMIT 1",
        (task_id,),
    ).fetchone()
    return bool(row) and row["kind"] == "blocked"


def recompute_ready(conn: sqlite3.Connection) -> int:
    """Promote ``todo`` tasks to ``ready`` when all parents are ``done`` or ``archived``.

    Returns the number of tasks promoted.  Safe to call inside or outside
    an existing transaction; it opens its own IMMEDIATE txn.

    ``blocked`` tasks are also considered for promotion (so a task
    blocked purely by a parent dependency unblocks itself when the
    parent completes), *except* when the most recent block event was a
    worker-initiated ``kanban_block`` — those stay blocked until an
    explicit ``kanban_unblock`` (#28712).  Without that guard, a
    ``review-required`` handoff would auto-respawn, the fresh worker
    would find nothing to do, exit cleanly, get recorded as a protocol
    violation, and the cycle would repeat indefinitely.
    """
    promoted = 0
    with write_txn(conn):
        todo_rows = conn.execute(
            "SELECT id, status FROM tasks WHERE status IN ('todo', 'blocked')"
        ).fetchall()
        for row in todo_rows:
            task_id = row["id"]
            cur_status = row["status"]
            if cur_status == "blocked" and _has_sticky_block(conn, task_id):
                # Worker / operator asked for human review — do not
                # silently auto-recover.  ``unblock_task`` is the only
                # legitimate exit (it emits ``"unblocked"`` which flips
                # this predicate back).
                continue
            parents = conn.execute(
                "SELECT t.status FROM tasks t "
                "JOIN task_links l ON l.parent_id = t.id "
                "WHERE l.child_id = ?",
                (task_id,),
            ).fetchall()
            if all(p["status"] in ("done", "archived") for p in parents):
                # Blocked tasks also get their failure counters reset —
                # this is effectively an auto-unblock (circuit-breaker
                # recovery; worker-initiated blocks are skipped above).
                if cur_status == "blocked":
                    conn.execute(
                        "UPDATE tasks SET status = 'ready', "
                        "consecutive_failures = 0, last_failure_error = NULL "
                        "WHERE id = ? AND status = 'blocked'",
                        (task_id,),
                    )
                else:
                    conn.execute(
                        "UPDATE tasks SET status = 'ready' WHERE id = ? AND status = 'todo'",
                        (task_id,),
                    )
                _append_event(conn, task_id, "promoted", None)
                promoted += 1
    return promoted


# ---------------------------------------------------------------------------
# Claim / complete / block
# ---------------------------------------------------------------------------

def claim_task(
    conn: sqlite3.Connection,
    task_id: str,
    *,
    ttl_seconds: Optional[int] = None,
    claimer: Optional[str] = None,
) -> Optional[Task]:
    """Atomically transition ``ready -> running``.

    Returns the claimed ``Task`` on success, ``None`` if the task was
    already claimed (or is not in ``ready`` status).
    """
    now = int(time.time())
    lock = claimer or _claimer_id()
    expires = now + _resolve_claim_ttl_seconds(ttl_seconds)
    with write_txn(conn):
        # Structural invariant: never transition ready -> running while any
        # parent is not yet 'done'. This is the single enforcement point
        # regardless of which writer (create_task, link_tasks, unblock_task,
        # release_stale_claims, manual SQL) set status='ready'. If a racy
        # writer promoted a task with undone parents, demote it back to
        # 'todo' here — recompute_ready will re-promote when the parents
        # actually finish. See RCA at
        # kanban/boards/cookai/workspaces/t_a6acd07d/root-cause.md.
        undone = conn.execute(
            "SELECT 1 FROM task_links l "
            "JOIN tasks p ON p.id = l.parent_id "
            "WHERE l.child_id = ? AND p.status NOT IN ('done', 'archived') LIMIT 1",
            (task_id,),
        ).fetchone()
        if undone:
            conn.execute(
                "UPDATE tasks SET status = 'todo' "
                "WHERE id = ? AND status = 'ready'",
                (task_id,),
            )
            _append_event(
                conn, task_id, "claim_rejected",
                {"reason": "parents_not_done"},
            )
            return None
        # Defensive: if a prior run somehow leaked (invariant violation from
        # an unknown code path), close it as 'reclaimed' so we don't strand
        # it when the CAS resets the pointer below. No-op when the invariant
        # holds (the common case).
        stale = conn.execute(
            "SELECT current_run_id FROM tasks WHERE id = ? AND status = 'ready'",
            (task_id,),
        ).fetchone()
        if stale and stale["current_run_id"]:
            conn.execute(
                """
                UPDATE task_runs
                   SET status = 'reclaimed', outcome = 'reclaimed',
                       summary = COALESCE(summary, 'invariant recovery on re-claim'),
                       ended_at = ?,
                       claim_lock = NULL, claim_expires = NULL, worker_pid = NULL
                 WHERE id = ? AND ended_at IS NULL
                """,
                (now, int(stale["current_run_id"])),
            )
        cur = conn.execute(
            """
            UPDATE tasks
               SET status        = 'running',
                   claim_lock    = ?,
                   claim_expires = ?,
                   started_at    = COALESCE(started_at, ?)
             WHERE id = ?
               AND status = 'ready'
               AND claim_lock IS NULL
            """,
            (lock, expires, now, task_id),
        )
        if cur.rowcount != 1:
            return None
        # Look up the current task row so we can populate the run with
        # its assignee / step / runtime cap.
        trow = conn.execute(
            "SELECT assignee, max_runtime_seconds, current_step_key "
            "FROM tasks WHERE id = ?",
            (task_id,),
        ).fetchone()
        run_cur = conn.execute(
            """
            INSERT INTO task_runs (
                task_id, profile, step_key, status,
                claim_lock, claim_expires, max_runtime_seconds,
                started_at, worker_exit_kind, worker_protocol_state
            ) VALUES (?, ?, ?, 'running', ?, ?, ?, ?, 'pending', 'pending')
            """,
            (
                task_id,
                trow["assignee"] if trow else None,
                trow["current_step_key"] if trow else None,
                lock,
                expires,
                trow["max_runtime_seconds"] if trow else None,
                now,
            ),
        )
        run_id = run_cur.lastrowid
        conn.execute(
            "UPDATE tasks SET current_run_id = ? WHERE id = ?",
            (run_id, task_id),
        )
        _append_event(
            conn, task_id, "claimed",
            {"lock": lock, "expires": expires, "run_id": run_id},
            run_id=run_id,
        )
        return get_task(conn, task_id)


def claim_review_task(
    conn: sqlite3.Connection,
    task_id: str,
    *,
    ttl_seconds: Optional[int] = None,
    claimer: Optional[str] = None,
) -> Optional[Task]:
    """Atomically transition ``review -> running``.

    Returns the claimed ``Task`` on success, ``None`` if the task was
    already claimed (or is not in ``review`` status).

    Unlike ``claim_task`` (which handles ``ready -> running``), this
    does NOT check parent dependencies — the task already passed that
    gate on its original ``todo -> ready -> running`` transition.

    Creates a new run entry so the review agent's lifecycle is tracked
    independently from the original worker run.
    """
    now = int(time.time())
    lock = claimer or _claimer_id()
    expires = now + _resolve_claim_ttl_seconds(ttl_seconds)
    with write_txn(conn):
        cur = conn.execute(
            """
            UPDATE tasks
               SET status        = 'running',
                   claim_lock    = ?,
                   claim_expires = ?,
                   started_at    = COALESCE(started_at, ?)
             WHERE id = ?
               AND status = 'review'
               AND claim_lock IS NULL
            """,
            (lock, expires, now, task_id),
        )
        if cur.rowcount != 1:
            return None
        trow = conn.execute(
            "SELECT assignee, max_runtime_seconds, current_step_key "
            "FROM tasks WHERE id = ?",
            (task_id,),
        ).fetchone()
        run_cur = conn.execute(
            """
            INSERT INTO task_runs (
                task_id, profile, step_key, status,
                claim_lock, claim_expires, max_runtime_seconds,
                started_at, worker_exit_kind, worker_protocol_state
            ) VALUES (?, ?, ?, 'running', ?, ?, ?, ?, 'pending', 'pending')
            """,
            (
                task_id,
                trow["assignee"] if trow else None,
                trow["current_step_key"] if trow else None,
                lock,
                expires,
                trow["max_runtime_seconds"] if trow else None,
                now,
            ),
        )
        run_id = run_cur.lastrowid
        conn.execute(
            "UPDATE tasks SET current_run_id = ? WHERE id = ?",
            (run_id, task_id),
        )
        _append_event(
            conn, task_id, "claimed",
            {"lock": lock, "expires": expires, "run_id": run_id,
             "source_status": "review"},
            run_id=run_id,
        )
        return get_task(conn, task_id)


def heartbeat_claim(
    conn: sqlite3.Connection,
    task_id: str,
    *,
    ttl_seconds: Optional[int] = None,
    claimer: Optional[str] = None,
) -> bool:
    """Extend a running claim.  Returns True if we still own it.

    Workers that know they'll exceed 15 minutes should call this every
    few minutes to keep ownership.
    """
    expires = int(time.time()) + _resolve_claim_ttl_seconds(ttl_seconds)
    lock = claimer or _claimer_id()
    with write_txn(conn):
        cur = conn.execute(
            "UPDATE tasks SET claim_expires = ? "
            "WHERE id = ? AND status = 'running' AND claim_lock = ?",
            (expires, task_id, lock),
        )
        if cur.rowcount == 1:
            run_id = _current_run_id(conn, task_id)
            if run_id is not None:
                conn.execute(
                    "UPDATE task_runs SET claim_expires = ? WHERE id = ?",
                    (expires, run_id),
                )
            return True
        return False


def release_stale_claims(
    conn: sqlite3.Connection,
    *,
    signal_fn=None,
) -> int:
    """Reset any ``running`` task whose claim has expired.

    A stale-by-TTL claim whose host-local worker PID is still alive is
    *extended* (with a ``claim_extended`` event) instead of being
    reclaimed. Reclaiming a live worker mid-flight produces the spawn-
    then-immediately-reclaim loop seen on slow models that spend longer
    than ``DEFAULT_CLAIM_TTL_SECONDS`` inside a single tool-free LLM
    call (#23025): no tool calls means no ``kanban_heartbeat``, even
    though the subprocess is healthy. ``enforce_max_runtime`` and
    ``detect_crashed_workers`` remain the upper bounds for genuinely
    wedged or dead workers.

    Returns the number of stale claims actually reclaimed (live-pid
    extensions don't count). Safe to call often.
    """
    now = int(time.time())
    reclaimed = 0
    host_prefix = f"{_claimer_id().split(':', 1)[0]}:"
    stale = conn.execute(
        "SELECT id, claim_lock, worker_pid, claim_expires, last_heartbeat_at "
        "FROM tasks "
        "WHERE status = 'running' AND claim_expires IS NOT NULL "
        "  AND claim_expires < ?",
        (now,),
    ).fetchall()
    for row in stale:
        lock = row["claim_lock"] or ""
        host_local = lock.startswith(host_prefix)
        if host_local and row["worker_pid"] and _pid_alive(row["worker_pid"]):
            new_expires = now + _resolve_claim_ttl_seconds()
            with write_txn(conn):
                cur = conn.execute(
                    "UPDATE tasks SET claim_expires = ? "
                    "WHERE id = ? AND status = 'running' "
                    "  AND claim_lock IS ? "
                    "  AND claim_expires IS NOT NULL "
                    "  AND claim_expires < ?",
                    (new_expires, row["id"], row["claim_lock"], now),
                )
                if cur.rowcount != 1:
                    continue
                run_id = _current_run_id(conn, row["id"])
                if run_id is not None:
                    conn.execute(
                        "UPDATE task_runs SET claim_expires = ? WHERE id = ?",
                        (new_expires, run_id),
                    )
                _append_event(
                    conn, row["id"], "claim_extended",
                    {
                        "reason": "pid_alive",
                        "worker_pid": int(row["worker_pid"]),
                        "claim_lock": row["claim_lock"],
                        "claim_expires_was": int(row["claim_expires"]),
                        "claim_expires_now": new_expires,
                        "last_heartbeat_at": (
                            int(row["last_heartbeat_at"])
                            if row["last_heartbeat_at"] is not None
                            else None
                        ),
                    },
                    run_id=run_id,
                )
            continue

        termination = _terminate_reclaimed_worker(
            row["worker_pid"], row["claim_lock"], signal_fn=signal_fn,
        )
        with write_txn(conn):
            cur = conn.execute(
                "UPDATE tasks SET status = 'ready', claim_lock = NULL, "
                "claim_expires = NULL, worker_pid = NULL "
                "WHERE id = ? AND status = 'running' AND claim_lock IS ? "
                "AND claim_expires IS NOT NULL AND claim_expires < ?",
                (row["id"], row["claim_lock"], now),
            )
            if cur.rowcount != 1:
                continue
            run_id = _end_run(
                conn, row["id"],
                outcome="reclaimed", status="reclaimed",
                error=f"stale_lock={row['claim_lock']}",
                metadata=termination,
            )
            payload = {
                "stale_lock": row["claim_lock"],
                "worker_pid": (
                    int(row["worker_pid"])
                    if row["worker_pid"] is not None else None
                ),
                "claim_expires": int(row["claim_expires"]),
                "last_heartbeat_at": (
                    int(row["last_heartbeat_at"])
                    if row["last_heartbeat_at"] is not None else None
                ),
                "now": now,
                "host_local": host_local,
            }
            payload.update(termination)
            _append_event(
                conn, row["id"], "reclaimed",
                payload,
                run_id=run_id,
            )
            reclaimed += 1
    return reclaimed


def reclaim_task(
    conn: sqlite3.Connection,
    task_id: str,
    *,
    reason: Optional[str] = None,
    signal_fn=None,
) -> bool:
    """Operator-driven reclaim: release the claim and reset to ``ready``.

    Unlike :func:`release_stale_claims` which only acts on tasks whose
    ``claim_expires`` has passed, this function reclaims immediately
    regardless of TTL. Intended for the dashboard/CLI recovery flow
    when an operator wants to abort a running worker without waiting
    for the TTL to expire (e.g. after seeing a hallucination warning).

    Returns True if a reclaim happened, False if the task isn't in a
    reclaimable state (not running, or doesn't exist).
    """
    row = conn.execute(
        "SELECT status, claim_lock, worker_pid FROM tasks WHERE id = ?",
        (task_id,),
    ).fetchone()
    if not row:
        return False
    if row["status"] != "running" and row["claim_lock"] is None:
        # Nothing to reclaim — already ready / blocked / done.
        return False
    prev_lock = row["claim_lock"]
    termination = _terminate_reclaimed_worker(
        row["worker_pid"], prev_lock, signal_fn=signal_fn,
    )
    with write_txn(conn):
        cur = conn.execute(
            "UPDATE tasks SET status = 'ready', claim_lock = NULL, "
            "claim_expires = NULL, worker_pid = NULL "
            "WHERE id = ? AND status IN ('running', 'ready', 'blocked') "
            "AND claim_lock IS ?",
            (task_id, prev_lock),
        )
        if cur.rowcount != 1:
            return False
        run_id = _end_run(
            conn, task_id,
            outcome="reclaimed", status="reclaimed",
            error=(
                f"manual_reclaim: {reason}" if reason
                else f"manual_reclaim lock={prev_lock}"
            ),
            metadata=termination,
        )
        payload = {
            "manual": True,
            "reason": reason,
            "prev_lock": prev_lock,
        }
        payload.update(termination)
        _append_event(
            conn, task_id, "reclaimed",
            payload,
            run_id=run_id,
        )
    # Operator intervention — they've looked at the task, so the
    # consecutive-failures counter is now stale. Give the next retry
    # a fresh budget. (_clear_failure_counter opens its own write_txn,
    # so it runs after the enclosing one commits.)
    _clear_failure_counter(conn, task_id)
    return True


def reassign_task(
    conn: sqlite3.Connection,
    task_id: str,
    profile: Optional[str],
    *,
    reclaim_first: bool = False,
    reason: Optional[str] = None,
) -> bool:
    """Reassign a task, optionally reclaiming a stuck running worker first.

    This is the recovery path for "this profile's model is broken, try
    a different one". If ``reclaim_first`` is True, any active claim is
    released (via :func:`reclaim_task`) before the reassign happens;
    otherwise the function refuses to reassign a currently-running task
    and returns False (caller can retry with ``reclaim_first=True``).

    Returns True if the reassign landed. ``profile`` may be ``None`` to
    unassign entirely.
    """
    if reclaim_first:
        # Safe to call even if nothing to reclaim.
        reclaim_task(conn, task_id, reason=reason or "reassign")
    # assign_task handles its own txn + the still-running guard.
    try:
        return assign_task(conn, task_id, profile)
    except RuntimeError:
        # Task is still running and reclaim_first was False; caller
        # needs to decide whether to retry with reclaim.
        return False


def _normalize_created_card_claims(claimed_ids: Iterable[str]) -> list[str]:
    """Return non-empty claimed card ids, deduped while preserving order."""
    claimed = [str(x).strip() for x in (claimed_ids or []) if str(x).strip()]
    seen: set[str] = set()
    ordered: list[str] = []
    for cid in claimed:
        if cid not in seen:
            seen.add(cid)
            ordered.append(cid)
    return ordered


def _verify_created_cards(
    conn: sqlite3.Connection,
    completing_task_id: str,
    claimed_ids: Iterable[str],
) -> tuple[list[str], list[str]]:
    """Partition ``claimed_ids`` into (verified, phantom).

    A card is "verified" iff a row exists in ``tasks`` AND at least one
    of the following holds:

    * ``created_by`` matches the completing task's ``assignee`` profile
      (the common case: worker A spawns a card via ``kanban_create``,
      which stamps ``created_by=A``).
    * ``created_by`` matches the completing task's id (edge case where
      a worker passed its own task id as the ``created_by`` value).
    * The card is linked as a ``task_links.child`` of the completing
      task — i.e. the worker explicitly called ``kanban_create`` with
      ``parents=[<current_task>]``. This accepts cards created through
      the dashboard/CLI by a different principal but then attached to
      the completing task by the worker.

    ``phantom`` returns ids that either don't exist at all, or exist
    but don't satisfy any of the three trust conditions. The caller
    decides what to do with each bucket; this helper never mutates.
    """
    ordered = _normalize_created_card_claims(claimed_ids)
    if not ordered:
        return [], []

    row = conn.execute(
        "SELECT assignee FROM tasks WHERE id = ?", (completing_task_id,),
    ).fetchone()
    if row is None:
        # Completing task not found — nothing resolves.
        return [], ordered
    completing_assignee = row["assignee"]

    # Batch-fetch existence + created_by in one query.
    placeholders = ",".join(["?"] * len(ordered))
    rows = conn.execute(
        f"SELECT id, created_by FROM tasks WHERE id IN ({placeholders})",
        tuple(ordered),
    ).fetchall()
    found = {r["id"]: r["created_by"] for r in rows}

    # Pull the set of cards linked as children of the completing task.
    # Cheap: one query, indexed on parent_id.
    linked_children: set[str] = set(child_ids(conn, completing_task_id))

    verified: list[str] = []
    phantom: list[str] = []
    for cid in ordered:
        created_by = found.get(cid)
        if created_by is None:
            phantom.append(cid)
            continue
        # Accept if any of the three trust conditions holds.
        if completing_assignee and created_by == completing_assignee:
            verified.append(cid)
        elif created_by == completing_task_id:
            verified.append(cid)
        elif cid in linked_children:
            verified.append(cid)
        else:
            phantom.append(cid)
    return verified, phantom


def validate_created_cards(
    conn: sqlite3.Connection,
    completing_task_id: str,
    claimed_ids: Iterable[str],
) -> dict[str, Any]:
    """Dry-run the ``created_cards`` completion gate without mutating state.

    This is the public, side-effect-free preflight counterpart to the
    ``complete_task(..., created_cards=...)`` gate. Keep completion wired
    through this helper so workers can validate claims before risking a
    terminal handoff and both paths cannot drift.
    """
    claimed = _normalize_created_card_claims(claimed_ids)
    verified, phantom = _verify_created_cards(conn, completing_task_id, claimed)
    return {
        "ok": not phantom,
        "task_id": completing_task_id,
        "claimed_cards": claimed,
        "verified_cards": verified,
        "phantom_cards": phantom,
    }


# Task-id pattern used both by ``kanban_create`` (``t_<12 hex>``) and
# ``_new_task_id`` below. Kept permissive on length for forward compat:
# accept 8+ hex chars after the ``t_`` prefix.
_TASK_ID_PROSE_RE = re.compile(r"\bt_[a-f0-9]{8,}\b")


def _scan_prose_for_phantom_ids(
    conn: sqlite3.Connection,
    text: str,
) -> list[str]:
    """Regex-scan free-form text for ``t_<hex>`` references; return the
    ones that don't exist in ``tasks``.

    Used as a non-blocking advisory check on completion summaries. An
    empty return means "no suspicious references found" — either the
    text had no IDs at all, or every ID it mentioned resolves to a real
    task. Duplicates are deduped.
    """
    if not text:
        return []
    matches = _TASK_ID_PROSE_RE.findall(text)
    if not matches:
        return []
    # Dedupe preserving order.
    seen: set[str] = set()
    unique: list[str] = []
    for m in matches:
        if m not in seen:
            seen.add(m)
            unique.append(m)
    placeholders = ",".join(["?"] * len(unique))
    rows = conn.execute(
        f"SELECT id FROM tasks WHERE id IN ({placeholders})",
        tuple(unique),
    ).fetchall()
    existing = {r["id"] for r in rows}
    return [m for m in unique if m not in existing]


class HallucinatedCardsError(ValueError):
    """Raised by ``complete_task`` when ``created_cards`` contains ids
    that don't exist or weren't created by the completing worker.

    The phantom list is attached as ``.phantom`` for callers that want
    structured access. Kept as ``ValueError`` subclass so existing
    tool-error handlers treat it as a recoverable user error.
    """

    def __init__(self, phantom: list[str], completing_task_id: str):
        self.phantom = list(phantom)
        self.completing_task_id = completing_task_id
        super().__init__(
            f"completion blocked: claimed created_cards that do not exist "
            f"or were not created by this worker: {', '.join(phantom)}"
        )


class ScopeAttestationError(ValueError):
    """Raised when a task that requires scope attestation is completed
    without the structured safety metadata downstream verifiers rely on.
    """

    def __init__(self, missing: list[str], task_id: str):
        self.missing = list(missing)
        self.task_id = task_id
        super().__init__(
            "completion blocked: scope attestation metadata is incomplete "
            f"for {task_id}: {', '.join(missing)}"
        )


_KANBAN_POLICY_KEYS = {"completion_policy", "scope_contract", "review_lane"}
_KNOWN_SCOPE_ALLOWED_TOOLS = {
    "kanban_show",
    "kanban_complete",
    "kanban_block",
    "kanban_comment",
    "kanban_heartbeat",
    "kanban_create",
    "kanban_link",
    "kanban_run_workspace_command",
    "web_search",
    "web_extract",
    "read_file",
    "search_files",
    "write_file",
    "patch",
    "terminal",
    "process",
    "skill_view",
    "skills_list",
    "todo",
    "memory",
    "session_search",
}
_BROAD_SCOPE_ALLOWED_TOOL_MARKERS = {
    "*",
    "all",
    "any",
    "tools",
    "all_tools",
    # Toolset/category names are not valid scope-contract tool names.  The
    # contract must name model-native tools exactly; otherwise the dispatcher
    # cannot distinguish a narrow tool allowlist from a broad toolset request.
    "terminal",
    "file",
    "kanban",
    "mcp",
    "delegation",
    "code_execution",
    "memory",
    "clarify",
    "web",
    "browser",
}
_FORBIDDEN_SCOPE_ALLOWED_TOOL_MARKERS = {
    "openclaw",
    "mission_control",
    "telegram",
    "discord_admin",
    "config_write",
    "auth",
    "secrets",
    "cron_write",
}
_REQUIRED_SCOPE_FORBIDDEN_SYSTEMS = {
    "openclaw": "OpenClaw",
    "atlas": "Atlas",
    "mission-control": "Mission-Control",
    "telegram": "Telegram",
}
_REQUIRED_SCOPE_LIFECYCLE_TOOLS = [
    "kanban_show",
    "kanban_complete",
    "kanban_block",
]


def _truthy_policy_value(value: Any) -> bool:
    """Return True for explicit YAML truthy policy values only."""
    if isinstance(value, bool):
        return value
    if isinstance(value, (int, float)):
        return value != 0
    if isinstance(value, str):
        return value.strip().lower() in {"true", "yes", "1", "ok", "pass", "passed"}
    return bool(value)


def _safe_yaml_mapping(text: str) -> Optional[dict[str, Any]]:
    """Parse ``text`` as YAML and return a mapping, or ``None``.

    Kanban task bodies are Markdown with optional YAML snippets. Parser errors
    are treated as "no structured policy found" so malformed prose cannot crash
    dispatcher/completion paths or accidentally satisfy a policy gate.
    """
    if yaml is None:
        return None
    try:
        loaded = yaml.safe_load(text)
    except Exception:
        return None
    return loaded if isinstance(loaded, dict) else None


def _extract_frontmatter_block(text: str) -> Optional[str]:
    lines = text.splitlines()
    if not lines or lines[0].strip() != "---":
        return None
    for idx, line in enumerate(lines[1:], start=1):
        if line.strip() in {"---", "..."}:
            return "\n".join(lines[1:idx])
    return None


def _extract_fenced_yaml_blocks(text: str) -> list[str]:
    blocks: list[str] = []
    in_yaml = False
    buf: list[str] = []
    fence_re = re.compile(r"^\s*```\s*(yaml|yml)?\s*$", re.IGNORECASE)
    for line in text.splitlines():
        match = fence_re.match(line)
        if match:
            if in_yaml:
                blocks.append("\n".join(buf))
                buf = []
                in_yaml = False
            elif (match.group(1) or "").lower() in {"yaml", "yml"}:
                in_yaml = True
                buf = []
            continue
        if in_yaml:
            buf.append(line)
    return blocks


def _extract_top_level_yaml_snippets(text: str) -> list[str]:
    """Extract compact top-level policy snippets from Markdown task bodies.

    This is a controlled compatibility path for existing unfenced task specs:
    it only considers lines where ``completion_policy:`` or ``scope_contract:``
    starts at column 0, then keeps the following indented/blank/comment lines.
    Inline prose such as "mention completion_policy: require..." is ignored.
    """
    snippets: list[str] = []
    lines = text.splitlines()
    i = 0
    while i < len(lines):
        line = lines[i]
        if any(line.startswith(f"{key}:") for key in _KANBAN_POLICY_KEYS):
            buf = [line]
            i += 1
            while i < len(lines):
                nxt = lines[i]
                if nxt and not nxt.startswith((" ", "\t", "#")):
                    break
                buf.append(nxt)
                i += 1
            snippets.append("\n".join(buf))
            continue
        i += 1
    return snippets


def _iter_task_policy_mappings(body: Optional[str]) -> list[dict[str, Any]]:
    """Return parser-backed policy mappings found in a Markdown task body."""
    text = body or ""
    candidates: list[str] = []
    frontmatter = _extract_frontmatter_block(text)
    if frontmatter:
        candidates.append(frontmatter)
    candidates.extend(_extract_fenced_yaml_blocks(text))
    whole = _safe_yaml_mapping(text)
    if whole is not None:
        candidates.append(text)
    candidates.extend(_extract_top_level_yaml_snippets(text))

    mappings: list[dict[str, Any]] = []
    seen: set[str] = set()
    for candidate in candidates:
        stripped = candidate.strip()
        if not stripped or stripped in seen:
            continue
        seen.add(stripped)
        mapping = _safe_yaml_mapping(stripped)
        if not isinstance(mapping, dict):
            continue
        if _KANBAN_POLICY_KEYS.intersection(mapping):
            mappings.append(mapping)
    return mappings


def _body_requires_scope_attestation(body: Optional[str]) -> bool:
    """Return True for structured ``completion_policy.require_scope_attestation``.

    Supported forms are YAML frontmatter, fenced YAML blocks, pure YAML bodies,
    and top-level unfenced YAML snippets. Arbitrary prose/inline strings are not
    treated as policy declarations.
    """
    for mapping in _iter_task_policy_mappings(body):
        policy = mapping.get("completion_policy")
        if isinstance(policy, dict) and _truthy_policy_value(policy.get("require_scope_attestation")):
            return True
    return False


def _completion_policy_required_tool_evidence(body: Optional[str]) -> list[dict[str, Any]]:
    """Return structured ``completion_policy.required_tool_evidence`` entries."""
    selected: list[dict[str, Any]] = []
    for mapping in _iter_task_policy_mappings(body):
        policy = mapping.get("completion_policy")
        if not isinstance(policy, dict):
            continue
        raw = policy.get("required_tool_evidence")
        if not isinstance(raw, list):
            continue
        entries: list[dict[str, Any]] = []
        for item in raw:
            if not isinstance(item, dict):
                continue
            tool = str(item.get("tool") or "").strip()
            path = str(item.get("path") or "").strip()
            target = str(item.get("target") or "").strip()
            if not tool:
                continue
            entry = {
                "tool": tool,
                "path": path,
                "target": target,
                "same_worker_session": _truthy_policy_value(item.get("same_worker_session")),
            }
            entries.append(entry)
        if entries:
            selected = entries
    return selected


def _normalize_required_evidence_path(path: str) -> str:
    value = str(path or "").strip()
    if value != "/":
        value = value.rstrip("/")
    return value


def _profile_state_db_path(profile: Optional[str]) -> Optional[Path]:
    if not profile:
        return None
    try:
        from hermes_constants import get_default_hermes_root, get_hermes_home

        root = get_default_hermes_root()
        home = get_hermes_home()
    except Exception:
        root = Path(os.environ.get("HERMES_HOME") or Path.home() / ".hermes")
        home = root
    profile_name = str(profile).strip()
    if not profile_name or "/" in profile_name or "\\" in profile_name:
        return None
    profile_path = Path(profile_name)
    if profile_path.is_absolute() or profile_path.name != profile_name or profile_name in {".", ".."}:
        return None
    if home.parent.name == "profiles" and home.name == profile_name:
        return home / "state.db"
    return root / "profiles" / profile_name / "state.db"


def _json_object(value: Any) -> Optional[dict[str, Any]]:
    if isinstance(value, dict):
        return value
    if isinstance(value, str):
        try:
            parsed = json.loads(value)
        except Exception:
            return None
        return parsed if isinstance(parsed, dict) else None
    return None


def _tool_call_arguments(call: Mapping[str, Any]) -> dict[str, Any]:
    function = call.get("function") if isinstance(call, Mapping) else None
    if not isinstance(function, Mapping):
        return {}
    args = function.get("arguments")
    parsed = _json_object(args)
    return parsed or {}


def _validate_required_tool_evidence(
    *,
    conn: sqlite3.Connection,
    task_id: str,
    metadata: Optional[dict],
    required: list[dict[str, Any]],
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    """Validate required tool evidence against same worker session history."""
    if not required:
        return [], []
    missing: list[dict[str, Any]] = []
    verified: list[dict[str, Any]] = []
    if not isinstance(metadata, dict):
        return ([{"reason": "metadata object is required", "required": item} for item in required], [])
    worker_session_id = str(metadata.get("worker_session_id") or "").strip()
    if not worker_session_id:
        return ([{"reason": "worker_session_id missing", "required": item} for item in required], [])

    row = conn.execute("SELECT assignee FROM tasks WHERE id = ?", (task_id,)).fetchone()
    profile = row["assignee"] if row is not None and "assignee" in row.keys() else None
    state_db = _profile_state_db_path(profile)
    if state_db is None or not state_db.exists():
        return ([{"reason": "profile state.db missing", "required": item} for item in required], [])

    state: Optional[sqlite3.Connection] = None
    try:
        state = sqlite3.connect(str(state_db))
        state.row_factory = sqlite3.Row
        rows = state.execute(
            """
            SELECT id, role, content, tool_call_id, tool_calls, tool_name
              FROM messages
             WHERE session_id = ?
             ORDER BY id ASC
            """,
            (worker_session_id,),
        ).fetchall()
    except Exception as exc:
        return ([{"reason": f"profile state.db unreadable: {exc}", "required": item} for item in required], [])
    finally:
        if state is not None:
            state.close()

    calls: dict[str, dict[str, Any]] = {}
    for msg in rows:
        raw_calls = msg["tool_calls"]
        if not raw_calls:
            continue
        try:
            parsed_calls = json.loads(raw_calls)
        except Exception:
            continue
        if isinstance(parsed_calls, dict):
            parsed_calls = [parsed_calls]
        if not isinstance(parsed_calls, list):
            continue
        for call in parsed_calls:
            if not isinstance(call, dict):
                continue
            call_id = str(call.get("id") or "").strip()
            function = call.get("function") if isinstance(call.get("function"), dict) else {}
            if not call_id or not function:
                continue
            calls[call_id] = {
                "tool": str(function.get("name") or "").strip(),
                "args": _tool_call_arguments(call),
            }

    responses: dict[str, sqlite3.Row] = {}
    for msg in rows:
        if msg["role"] == "tool" and msg["tool_call_id"]:
            responses[str(msg["tool_call_id"])] = msg

    for item in required:
        want_tool = str(item.get("tool") or "").strip()
        want_path = _normalize_required_evidence_path(str(item.get("path") or ""))
        want_target = str(item.get("target") or "").strip()
        matched = False
        error_reason: Optional[str] = None
        for call_id, call in calls.items():
            if call.get("tool") != want_tool:
                continue
            args = call.get("args") or {}
            got_path = _normalize_required_evidence_path(str(args.get("path") or ""))
            got_target = str(args.get("target") or "").strip()
            if want_path and got_path != want_path:
                continue
            if want_target and got_target != want_target:
                continue
            response = responses.get(call_id)
            if response is None:
                error_reason = "matching tool response missing"
                continue
            content_obj = _json_object(response["content"])
            if isinstance(content_obj, dict) and content_obj.get("error"):
                error_reason = "matching tool response contains error"
                continue
            verified.append({
                "tool": want_tool,
                "path": item.get("path"),
                "target": want_target or None,
                "worker_session_id": worker_session_id,
                "tool_call_id": call_id,
            })
            matched = True
            break
        if not matched:
            missing.append({
                "reason": error_reason or "matching same-worker-session tool call missing",
                "required": item,
                "worker_session_id": worker_session_id,
            })
    return missing, verified


def _selected_policy_value(body: Optional[str], key: str) -> Any:
    """Return the last parser-backed policy value for ``key`` in a task body."""
    selected = None
    for mapping in _iter_task_policy_mappings(body):
        if key in mapping:
            selected = mapping.get(key)
    return selected


def _review_lane_string_list(value: Any) -> list[str]:
    if value is None:
        return []
    if isinstance(value, str):
        return [value]
    if isinstance(value, (list, tuple, set)):
        return [str(item) for item in value if str(item).strip()]
    return []


def _normalized_review_lane(value: Any) -> Optional[str]:
    if value is None:
        return None
    lane = str(value).strip().upper().replace("-", "_").replace(" ", "_")
    aliases = {
        "FASTLANE": "FASTLANE_KANBAN",
        "KANBAN_FASTLANE": "FASTLANE_KANBAN",
        "STANDARD": "STANDARD_REVIEW",
        "CRITICAL": "CRITICAL_REVIEW",
    }
    lane = aliases.get(lane, lane)
    return lane if lane in KANBAN_REVIEW_LANES else None


def _explicit_manual_review_pipeline(body: Optional[str]) -> bool:
    """Return True when task policy opts out of dispatcher Reviewer-B auto-creation.

    STANDARD_REVIEW supports exactly one review path: either Coordinator/Planner
    creates an explicit/manual reviewer pipeline, or the dispatcher creates the
    default Reviewer-B child. These structured policy keys make the manual path
    explicit for taskgraphs that do not pre-create the reviewer child.
    """
    review_pipeline = _selected_policy_value(body, "review_pipeline")
    if isinstance(review_pipeline, str) and review_pipeline.strip().casefold() in {
        "manual",
        "manual_review",
        "manual-review",
        "manual_reviewer",
        "manual-reviewer",
    }:
        return True

    auto_reviewer_b = _selected_policy_value(body, "auto_reviewer_b")
    if isinstance(auto_reviewer_b, bool):
        return not auto_reviewer_b
    if isinstance(auto_reviewer_b, (int, float)):
        return auto_reviewer_b == 0
    if isinstance(auto_reviewer_b, str):
        return auto_reviewer_b.strip().casefold() in {"false", "no", "0", "off", "disabled"}

    return False


def _without_scope_negative_declarations(text: str, contract: dict[str, Any]) -> str:
    """Remove negative scope declarations before free-text risk matching.

    The lane classifier should not escalate a low-risk task merely because its
    scope contract lists critical systems under forbidden/anti-scope fields.
    Positive declarations such as allowed_systems are handled separately.
    """
    sanitized = text
    for key in ("forbidden_systems", "forbidden_paths", "anti_scope"):
        for item in _review_lane_string_list(contract.get(key)):
            value = str(item).strip().casefold()
            if value:
                sanitized = sanitized.replace(value, "")
                sanitized = sanitized.replace(_normalize_forbidden_system_name(value), "")
    return sanitized


def classify_kanban_review_lane(
    *,
    title: Optional[str] = None,
    body: Optional[str] = None,
    changed_paths: Optional[Iterable[str]] = None,
    requested_lane: Optional[str] = None,
) -> dict[str, Any]:
    """Classify the cheapest safe Kanban review lane for a task."""
    text_parts = [title or "", body or ""]
    paths = [str(p) for p in (changed_paths or []) if str(p).strip()]
    text_haystack = "\n".join(text_parts).casefold()
    contract = _selected_scope_contract(body) or {}
    lane_value = _selected_policy_value(body, "review_lane")
    explicit_lane = _normalized_review_lane(requested_lane) or _normalized_review_lane(lane_value)
    allowed_systems = {
        _normalize_forbidden_system_name(item)
        for item in _review_lane_string_list(contract.get("allowed_systems"))
    }

    critical_terms = {
        "gateway-runtime", "dispatcher-runtime-activation", "live-profile-config-mutation",
        "openclaw", "mission-control", "atlas", "telegram", "systemd",
        "cron-runtime", "secrets-credentials", "restart", "deploy", "runtime-activation",
        "profile-config", "config-mutation", "broad-db", "state-operation",
    }
    critical_text_markers = {
        "gateway-runtime", "dispatcher-runtime-activation", "live-profile-config-mutation",
        "runtime-activation", "profile-config", "config-mutation", "broad-db",
        "state-operation", "secrets-credentials", "systemd", "cron-runtime",
        "openclaw-gateway", "mission-control", "atlas executor", "atlas call",
        "restart atlas", "telegram bot", "telegram notification", "systemctl restart",
        "restart service", "restart .service", ".service restart", "deploy runtime",
    }
    critical_path_markers = (
        "/.hermes/auth.json", "/.hermes/.env", "/.hermes/profiles/", "/.openclaw/",
        "systemd/", "cron/", "gateway/", "plugins/kanban/systemd/",
    )
    standard_terms = {
        "dispatcher", "lifecycle", "task-lifecycle", "completion-semantics",
        "review-required", "recompute-ready", "task-links", "claim-task",
        "block-task", "complete-task", "kanban-db-semantics",
    }

    reasons: list[str] = []
    triggers: list[str] = []
    critical_text_haystack = _without_scope_negative_declarations(text_haystack, contract)
    critical_text_hit = any(marker in critical_text_haystack for marker in critical_text_markers)
    if allowed_systems.intersection(critical_terms) or critical_text_hit:
        triggers.append("critical_allowed_system_or_text")
    if any(marker in p.casefold() for p in paths for marker in critical_path_markers):
        triggers.append("critical_changed_path")
    if explicit_lane == "CRITICAL_REVIEW":
        triggers.append("explicit_critical_review")

    if triggers:
        lane = "CRITICAL_REVIEW"
        reasons.append("critical trigger requires Reviewer-A + Reviewer-B and explicit activation Go")
    else:
        standard_hit = any(term in text_haystack for term in standard_terms) or explicit_lane == "STANDARD_REVIEW"
        if standard_hit:
            lane = "STANDARD_REVIEW"
            if explicit_lane == "STANDARD_REVIEW":
                triggers.append("explicit_standard_review")
                reasons.append("explicit/policy STANDARD_REVIEW request requires one Reviewer-B")
            else:
                reasons.append("lifecycle/dispatcher/non-trivial Kanban semantics require one Reviewer-B")
        else:
            lane = KANBAN_FASTLANE_DEFAULT
            reasons.append("low-risk Kanban-only/default path: Hub plan check + Coder + Hub/Coordinator evidence check")

    if explicit_lane and KANBAN_REVIEW_LANES.index(explicit_lane) > KANBAN_REVIEW_LANES.index(lane):
        lane = explicit_lane
        triggers.append(f"explicit_{explicit_lane.lower()}")

    risk_map = {"FASTLANE_KANBAN": "low", "STANDARD_REVIEW": "medium", "CRITICAL_REVIEW": "high"}
    risk = risk_map.get(lane, "low")
    return {
        "lane": lane,
        "risk": risk,
        "reviewer_a_required": lane == "CRITICAL_REVIEW",
        "reviewer_b_required": lane in {"STANDARD_REVIEW", "CRITICAL_REVIEW"},
        "hub_coordinator_evidence_check_required": lane == "FASTLANE_KANBAN",
        "fastlane_default": lane == "FASTLANE_KANBAN",
        "reasons": reasons,
        "escalation_triggers": triggers,
    }


def _metadata_truthy(metadata: Optional[dict], key: str) -> bool:
    if not isinstance(metadata, dict) or key not in metadata:
        return False
    value = metadata.get(key)
    if isinstance(value, bool):
        return value
    if isinstance(value, (int, float)):
        return value != 0
    if isinstance(value, str):
        return value.strip().lower() in {"true", "yes", "1", "ok", "pass", "passed"}
    return bool(value)


def _metadata_int(metadata: Optional[dict], key: str) -> Optional[int]:
    if not isinstance(metadata, dict) or key not in metadata:
        return None
    value = metadata.get(key)
    if isinstance(value, bool):
        return int(value)
    if isinstance(value, int):
        return value
    if isinstance(value, str):
        try:
            return int(value.strip())
        except ValueError:
            return None
    return None


def _latest_dispatch_preflight_effective_toolsets(
    conn: sqlite3.Connection,
    task_id: str,
) -> Optional[list[str]]:
    """Return the latest dispatcher-recorded effective tool schema for a task."""
    row = conn.execute(
        """
        SELECT payload FROM task_events
         WHERE task_id = ? AND kind = 'dispatch_preflight_passed'
         ORDER BY id DESC LIMIT 1
        """,
        (task_id,),
    ).fetchone()
    if not row or not row["payload"]:
        return None
    try:
        payload = json.loads(row["payload"])
    except Exception:
        return None
    effective = payload.get("effective_toolsets") if isinstance(payload, dict) else None
    if not isinstance(effective, list):
        return None
    return [str(item) for item in effective]


def _scope_contract_version(body: Optional[str]) -> Optional[int]:
    contract = _selected_scope_contract(body)
    if not isinstance(contract, dict):
        return None
    try:
        return int(contract.get("version"))
    except (TypeError, ValueError):
        return None


def kanban_completion_template(conn: sqlite3.Connection, task_id: str) -> dict[str, Any]:
    """Return a deterministic no-mutation completion metadata skeleton.

    The template is intentionally conservative: it includes the required
    scope-attestation fields and the latest dispatcher effective tool list when
    available, otherwise it falls back to the task body's
    ``scope_contract.allowed_tools`` and emits a diagnostic. It never writes to
    the board.
    """
    row = conn.execute(
        "SELECT id, body, assignee FROM tasks WHERE id = ?",
        (task_id,),
    ).fetchone()
    if row is None:
        return {
            "ok": False,
            "task_id": task_id,
            "mutated": False,
            "diagnostic": {
                "code": "task_not_found",
                "message": f"Task not found: {task_id}",
            },
            "metadata": None,
            "effective_toolsets_source": "unavailable",
        }

    body = row["body"]
    assignee = row["assignee"] if "assignee" in row.keys() else None
    version = _scope_contract_version(body) or 2
    scope_contract_read = _selected_scope_contract(body) is not None
    preflight_effective = _latest_dispatch_preflight_effective_toolsets(conn, task_id)
    contract_effective = _scope_contract_allowed_tools(body)
    diagnostic: Optional[dict[str, str]] = None
    effective_source = "unavailable"
    effective_toolsets: Optional[list[str]] = None

    if preflight_effective is not None:
        # F-2026-05-17-02: the dispatch event records the unfiltered runtime
        # tool list (worker has access via kanban-effective override). For the
        # metadata-record path we apply the profile filter so the drift
        # detector compares apples-to-apples with the profile's declared
        # surface and does not re-surface dispatcher-overridden tools.
        try:
            filtered_preflight = _filter_tool_names_by_profile_disabled(
                preflight_effective,
                profile=assignee,
            )
        except Exception:
            filtered_preflight = list(preflight_effective)
        if filtered_preflight and filtered_preflight != preflight_effective:
            effective_toolsets = filtered_preflight
            effective_source = "filtered_preflight_by_profile"
        elif filtered_preflight:
            effective_toolsets = filtered_preflight
            effective_source = "dispatch_preflight_passed"
        else:
            # All preflight tools were profile-disabled. Keep the raw preflight
            # list so validation can still match the event payload, but stamp
            # a marker so auditors know the filter produced an empty set.
            effective_toolsets = list(preflight_effective)
            effective_source = "fallback_preflight_all_profile_disabled"
    elif contract_effective is not None:
        raw_contract = [str(item) for item in contract_effective]
        # Finding F-2026-05-17-02 fallback path: no dispatch_preflight_passed
        # event found — run the contract through the profile filter directly.
        try:
            filtered_contract = _filter_tool_names_by_profile_disabled(
                raw_contract,
                profile=assignee,
            )
        except Exception:
            filtered_contract = list(raw_contract)
        if filtered_contract and filtered_contract != raw_contract:
            effective_toolsets = filtered_contract
            effective_source = "filtered_contract_by_profile"
        else:
            effective_toolsets = raw_contract
            # Preserve backwards-compatible source label when the filter was a
            # no-op (no profile, no overlap, or filter unavailable). Existing
            # consumers compare against "scope_contract.allowed_tools".
            effective_source = "scope_contract.allowed_tools"
        diagnostic = {
            "code": "dispatch_preflight_missing",
            "message": (
                "No dispatch_preflight_passed event found; effective_toolsets "
                "fell back to scope_contract.allowed_tools."
            ),
        }
    else:
        diagnostic = {
            "code": "effective_toolsets_unavailable",
            "message": (
                "No dispatch_preflight_passed event or "
                "scope_contract.allowed_tools found; fill effective_toolsets "
                "manually if this completion requires it."
            ),
        }

    metadata: dict[str, Any] = {
        "report_contract_version": 1,
        "scope_contract_read": scope_contract_read,
        "scope_contract_version": version,
        "scope_attestation": True,
        "forbidden_actions_taken": 0,
        "verification_evidence": [],
        "receipt_path": None,
    }
    if effective_toolsets is not None:
        metadata["effective_toolsets"] = effective_toolsets
    # F-2026-05-17-02: stamp a source marker on metadata when the filter path
    # was traversed, so auditors and the drift detector can tell whether the
    # list is profile-aligned. Values added by F-02:
    # ``filtered_preflight_by_profile`` (filtered runtime list — clean)
    # ``filtered_contract_by_profile`` (no-preflight fallback, filtered)
    # ``fallback_preflight_all_profile_disabled`` (profile disabled everything)
    # Legacy values (``dispatch_preflight_passed`` / ``scope_contract.allowed_tools``)
    # are left out of metadata to preserve the pre-patch on-disk shape.
    if effective_source in {
        "filtered_preflight_by_profile",
        "filtered_contract_by_profile",
        "fallback_preflight_all_profile_disabled",
    }:
        metadata["effective_toolsets_source"] = effective_source
    metadata.update(
        {
            "changed_files": [],
            "tests": [],
            "commands": [],
            "non_actions": [],
            "residual_risk": "",
            "commit_hash": "not_committed",
        }
    )

    return {
        "ok": True,
        "task_id": task_id,
        "mutated": False,
        "requires_scope_attestation": _body_requires_scope_attestation(body),
        "diagnostic": diagnostic,
        "metadata": metadata,
        "effective_toolsets_source": effective_source,
    }


def _validate_required_scope_attestation(
    metadata: Optional[dict],
    body: Optional[str] = None,
    *,
    conn: Optional[sqlite3.Connection] = None,
    task_id: Optional[str] = None,
) -> list[str]:
    """Return missing/invalid fields for a scope-gated completion."""
    missing: list[str] = []
    if not isinstance(metadata, dict):
        return ["metadata object is required"]
    version = _metadata_int(metadata, "scope_contract_version")
    if version is None or version < 2:
        missing.append("scope_contract_version >= 2")
    forbidden = _metadata_int(metadata, "forbidden_actions_taken")
    if forbidden is None:
        missing.append("forbidden_actions_taken = 0")
    elif forbidden != 0:
        missing.append("forbidden_actions_taken must be 0")
    if not _metadata_truthy(metadata, "scope_attestation"):
        missing.append("scope_attestation = true")
    expected_effective = _scope_contract_allowed_tools(body)
    assignee_for_filter: Optional[str] = None
    if conn is not None and task_id:
        expected_effective = (
            _latest_dispatch_preflight_effective_toolsets(conn, task_id)
            or expected_effective
        )
        try:
            row = conn.execute(
                "SELECT assignee FROM tasks WHERE id = ?",
                (task_id,),
            ).fetchone()
            if row is not None:
                assignee_for_filter = row["assignee"] if "assignee" in row.keys() else None
        except Exception:
            assignee_for_filter = None
    # F-2026-05-17-02: align expected with the kanban_completion_template path
    # — both apply the profile filter so worker-submitted metadata that excludes
    # profile-disabled tools is treated as valid (not a mismatch).
    if expected_effective is not None and assignee_for_filter:
        try:
            filtered_expected = _filter_tool_names_by_profile_disabled(
                [str(item) for item in expected_effective],
                profile=assignee_for_filter,
            )
            if filtered_expected:
                expected_effective = filtered_expected
        except Exception:
            pass
    if expected_effective is not None:
        effective = metadata.get("effective_toolsets")
        if not isinstance(effective, list) or not effective:
            missing.append("effective_toolsets list")
        elif [str(item) for item in effective] != [str(item) for item in expected_effective]:
            missing.append("effective_toolsets mismatch")
    return missing


def complete_task(
    conn: sqlite3.Connection,
    task_id: str,
    *,
    result: Optional[str] = None,
    summary: Optional[str] = None,
    metadata: Optional[dict] = None,
    created_cards: Optional[Iterable[str]] = None,
    expected_run_id: Optional[int] = None,
) -> bool:
    """Transition ``running|ready -> done`` and record ``result``.

    Accepts a task that is merely ``ready`` too, so a manual CLI
    completion (``hermes kanban complete <id>``) works without requiring
    a claim/start/complete sequence.

    ``summary`` and ``metadata`` are stored on the closing run (if any)
    and surfaced to downstream children via :func:`build_worker_context`.
    When ``summary`` is omitted we fall back to ``result`` so single-run
    callers do not have to pass both. ``metadata`` is a free-form dict
    (e.g. ``{"changed_files": [...], "tests_run": [...]}``) — workers
    are encouraged to use it for structured handoff facts.

    ``created_cards`` is an optional list of task ids the completing
    worker claims to have created. Each id is verified against
    ``tasks.created_by``. If any id is phantom (does not exist or was
    not created by this worker's assignee profile), completion is blocked
    with a ``HallucinatedCardsError`` and a
    ``completion_blocked_hallucination`` event is emitted so the rejected
    attempt is auditable. When all ids verify, they are recorded on the
    ``completed`` event payload.

    After a successful completion, ``summary`` and ``result`` are scanned
    for prose references like ``t_deadbeefcafe`` that do not resolve.
    Any suspected phantom references are recorded as a
    ``suspected_hallucinated_references`` event. This pass is advisory
    and never blocks.
    """
    now = int(time.time())

    task_row = conn.execute(
        "SELECT body FROM tasks WHERE id = ?",
        (task_id,),
    ).fetchone()
    requires_scope_attestation = bool(
        task_row and _body_requires_scope_attestation(task_row["body"])
    )
    task_body = task_row["body"] if task_row else None
    if requires_scope_attestation:
        missing_scope = _validate_required_scope_attestation(
            metadata,
            task_body,
            conn=conn,
            task_id=task_id,
        )
        if missing_scope:
            with write_txn(conn):
                _append_event(
                    conn,
                    task_id,
                    "completion_blocked_scope_attestation",
                    {
                        "missing": missing_scope,
                        "summary_preview": (
                            (summary or result or "").strip().splitlines()[0][:200]
                            if (summary or result)
                            else None
                        ),
                    },
                )
            raise ScopeAttestationError(missing_scope, task_id)

        required_tool_evidence = _completion_policy_required_tool_evidence(task_body)
        if required_tool_evidence:
            missing_evidence, verified_evidence = _validate_required_tool_evidence(
                conn=conn,
                task_id=task_id,
                metadata=metadata,
                required=required_tool_evidence,
            )
            if missing_evidence:
                worker_session_id = (
                    str(metadata.get("worker_session_id") or "").strip()
                    if isinstance(metadata, dict)
                    else ""
                )
                with write_txn(conn):
                    _append_event(
                        conn,
                        task_id,
                        "completion_blocked_missing_tool_evidence",
                        {
                            "missing": missing_evidence,
                            "required_tool_evidence": required_tool_evidence,
                            "worker_session_id": worker_session_id or None,
                            "summary_preview": (
                                (summary or result or "").strip().splitlines()[0][:200]
                                if (summary or result)
                                else None
                            ),
                        },
                    )
                missing_labels = [
                    f"required_tool_evidence: {item.get('reason', 'missing')}"
                    for item in missing_evidence
                ]
                raise ScopeAttestationError(missing_labels, task_id)
            with write_txn(conn):
                _append_event(
                    conn,
                    task_id,
                    "completion_required_tool_evidence_verified",
                    {
                        "verified_tool_evidence": verified_evidence,
                        "required_tool_evidence": required_tool_evidence,
                        "worker_session_id": (
                            str(metadata.get("worker_session_id") or "").strip()
                            if isinstance(metadata, dict)
                            else None
                        ),
                    },
                )

    # Gate: verify created_cards BEFORE the main write txn. A rejected
    # completion still needs an auditable event, so we emit it in a
    # tiny dedicated txn, then raise. The caller is responsible for
    # surfacing HallucinatedCardsError to the worker; this function
    # never mutates task state on a phantom-card rejection.
    if created_cards:
        card_validation = validate_created_cards(conn, task_id, created_cards)
        verified_cards = card_validation["verified_cards"]
        phantom_cards = card_validation["phantom_cards"]
        if phantom_cards:
            with write_txn(conn):
                _append_event(
                    conn, task_id, "completion_blocked_hallucination",
                    {
                        "phantom_cards": phantom_cards,
                        "verified_cards": verified_cards,
                        "summary_preview": (
                            (summary or result or "").strip().splitlines()[0][:200]
                            if (summary or result)
                            else None
                        ),
                    },
                )
            raise HallucinatedCardsError(phantom_cards, task_id)
    else:
        verified_cards = []

    with write_txn(conn):
        if expected_run_id is None:
            cur = conn.execute(
                """
                UPDATE tasks
                   SET status       = 'done',
                       result       = ?,
                       completed_at = ?,
                       claim_lock   = NULL,
                       claim_expires= NULL,
                       worker_pid   = NULL
                 WHERE id = ?
                   AND status IN ('running', 'ready', 'blocked')
                """,
                (result, now, task_id),
            )
        else:
            cur = conn.execute(
                """
                UPDATE tasks
                   SET status       = 'done',
                       result       = ?,
                       completed_at = ?,
                       claim_lock   = NULL,
                       claim_expires= NULL,
                       worker_pid   = NULL
                 WHERE id = ?
                   AND status IN ('running', 'ready', 'blocked')
                   AND current_run_id = ?
                """,
                (result, now, task_id, int(expected_run_id)),
            )
        if cur.rowcount != 1:
            return False
        run_id = _end_run(
            conn, task_id,
            outcome="completed", status="done",
            summary=summary if summary is not None else result,
            metadata=metadata,
        )
        # If complete_task was called on a never-claimed task (ready or
        # blocked → done with no run in flight), synthesize a
        # zero-duration run so the handoff fields are persisted in
        # attempt history instead of silently lost.
        if run_id is None and (summary or metadata or result):
            run_id = _synthesize_ended_run(
                conn, task_id,
                outcome="completed",
                summary=summary if summary is not None else result,
                metadata=metadata,
            )
        # Carry the handoff summary in the event payload so gateway
        # notifiers and dashboard WS consumers can render it without a
        # second SQL round-trip. First line only, 400 char cap — the
        # full summary stays on the run row.
        ev_summary = (summary if summary is not None else result) or ""
        ev_summary = ev_summary.strip().splitlines()[0][:400] if ev_summary else ""
        completed_payload = _terminal_event_payload(
            conn,
            task_id,
            run_id,
            outcome="completed",
            summary=ev_summary or None,
            extra={"result_len": len(result) if result else 0},
        )
        if verified_cards:
            completed_payload["verified_cards"] = verified_cards
        # Carry artifact paths in the event payload so the gateway
        # notifier can upload them as native attachments alongside the
        # completion message. Workers pass these via
        # ``kanban_complete(artifacts=[...])`` which stashes the list in
        # ``metadata["artifacts"]`` — we promote it onto the event so
        # consumers don't have to fetch the run row to find it.
        if isinstance(metadata, dict):
            md_artifacts = metadata.get("artifacts")
            if isinstance(md_artifacts, (list, tuple)):
                cleaned_artifacts = [
                    str(p).strip() for p in md_artifacts if isinstance(p, str) and str(p).strip()
                ]
                if cleaned_artifacts:
                    completed_payload["artifacts"] = cleaned_artifacts
        _append_event(
            conn, task_id, "completed",
            completed_payload,
            run_id=run_id,
        )
    # Prose-scan the summary + result for t_<hex> references that do
    # not resolve. Advisory — does not block the completion. Runs in
    # its own txn so the completion itself is already durable by the
    # time we emit the warning.
    scan_text = " ".join(filter(None, [summary, result]))
    if scan_text:
        phantom_refs = _scan_prose_for_phantom_ids(conn, scan_text)
        # Drop any phantom refs that were already flagged as verified
        # above (shouldn't happen — verified means they exist — but
        # belt-and-suspenders).
        phantom_refs = [p for p in phantom_refs if p not in set(verified_cards)]
        if phantom_refs:
            with write_txn(conn):
                _append_event(
                    conn, task_id, "suspected_hallucinated_references",
                    {
                        "phantom_refs": phantom_refs,
                        "source": "completion_summary",
                    },
                    run_id=run_id,
                )
    # Successful completion — wipe the consecutive-failures counter.
    # Failure history stays on the event log for audit; the counter
    # just tracks "is there a current pathology the breaker should
    # care about", and a success resets that question.
    _clear_failure_counter(conn, task_id)
    _terminalize_legacy_reviewer_children_for_finalization(conn, task_id, metadata)
    # Recompute ready status for dependents (separate txn so children see done).
    recompute_ready(conn)
    # Clean up the scratch workspace and any stale tmux session for the worker.
    _cleanup_workspace(conn, task_id)
    return True


# ---------------------------------------------------------------------------
# Workspace / tmux cleanup
# ---------------------------------------------------------------------------

def _is_managed_scratch_path(p: Path) -> bool:
    """Return True iff *p* is a strict descendant of a kanban-managed scratch root.

    A managed root is exclusively a ``workspaces/`` directory — never the
    broader kanban home, a board root, or sibling subtrees like ``logs/`` or
    ``boards/<slug>/`` itself. Allowed roots:

    * ``HERMES_KANBAN_WORKSPACES_ROOT`` when set (worker-side override
      injected by the dispatcher).
    * ``<kanban_home>/kanban/workspaces`` — legacy default-board scratch root.
    * ``<kanban_home>/kanban/boards/<slug>/workspaces`` for each board slug
      that currently exists on disk.

    The check requires strict descendancy: a path equal to one of these
    roots is NOT managed (deleting the workspaces root would wipe every
    task's scratch dir at once), and a path that resolves to ``<kanban_home>
    /kanban`` itself, ``<kanban_home>/kanban/logs``, or
    ``<kanban_home>/kanban/boards/<slug>`` is rejected because those
    subtrees hold Hermes' own DB, metadata, and logs, not task workspaces.

    Used by :func:`_cleanup_workspace` to refuse to ``shutil.rmtree`` paths
    outside Hermes-managed storage. A board ``default_workdir`` pointing at a
    real source tree can otherwise pair with ``workspace_kind='scratch'`` and
    cause task completion to delete user data (#28818).
    """
    try:
        p_abs = p.resolve(strict=False)
    except OSError:
        return False
    roots: list[Path] = []
    override = os.environ.get("HERMES_KANBAN_WORKSPACES_ROOT", "").strip()
    if override:
        try:
            roots.append(Path(override).expanduser().resolve(strict=False))
        except OSError:
            pass
    try:
        home = kanban_home()
    except OSError:
        home = None
    if home is not None:
        try:
            roots.append((home / "kanban" / "workspaces").resolve(strict=False))
        except OSError:
            pass
        try:
            boards_parent = (home / "kanban" / "boards").resolve(strict=False)
        except OSError:
            boards_parent = None
        if boards_parent is not None:
            try:
                entries = list(boards_parent.iterdir())
            except OSError:
                entries = []
            for entry in entries:
                try:
                    if not entry.is_dir():
                        continue
                except OSError:
                    continue
                try:
                    roots.append((entry / "workspaces").resolve(strict=False))
                except OSError:
                    continue
    for root in roots:
        if p_abs == root:
            continue
        try:
            if p_abs.is_relative_to(root):
                return True
        except ValueError:
            continue
    return False


def _preserve_scratch_artifacts(
    conn: sqlite3.Connection,
    task_id: str,
    workspace_path: Path,
) -> list[str]:
    """Copy artifacts under ``workspace_path`` to a persistent location
    BEFORE the workspace dir gets removed.

    Reads the latest run's ``metadata.artifacts[]`` for ``task_id``;
    for each absolute path that lives underneath ``workspace_path``,
    copies it to ``~/.hermes/reports/by-task/<task_id>/<basename>``.
    Best-effort: any per-file failure is logged at debug level and
    skipped, never raising — preservation MUST NOT block completion.

    Empty/missing ``artifacts[]`` is the no-op default. This matches
    the existing per-run metadata channel workers already write, so
    opt-in artifact preservation is a small additive contract.

    Returns the list of basenames that were preserved (for tests +
    logging). Empty list means nothing to do.
    """
    try:
        runs = list_runs(conn, task_id, include_active=False)
    except Exception as exc:
        _log.debug("preserve-artifacts %s: list_runs failed: %s", task_id, exc)
        return []
    if not runs:
        return []
    last = runs[-1]
    if not isinstance(last.metadata, dict):
        return []
    raw = last.metadata.get("artifacts")
    if not isinstance(raw, list) or not raw:
        return []

    try:
        workspace_resolved = workspace_path.resolve()
    except OSError as exc:
        _log.debug("preserve-artifacts %s: resolve workspace failed: %s",
                   task_id, exc)
        return []

    dest_root = kanban_home() / "reports" / "by-task" / task_id
    preserved: list[str] = []
    import shutil
    for entry in raw:
        if not isinstance(entry, str) or not entry:
            continue
        src = Path(entry).expanduser()
        if not src.is_absolute():
            # Skip relative paths — the contract is "absolute path
            # inside the workspace". Non-absolute entries are typically
            # informational (e.g. "test_foo.py" or a label) and have no
            # filesystem meaning here.
            continue
        try:
            src_resolved = src.resolve()
        except OSError as exc:
            _log.debug("preserve-artifacts %s: resolve %s failed: %s",
                       task_id, src, exc)
            continue
        # Containment: only copy artifacts that sit under the scratch
        # workspace. Other paths the worker named (e.g. files it wrote
        # to ~/.hermes/reports/ already, or system files) are either
        # already persistent or out of scope for auto-copy.
        try:
            src_resolved.relative_to(workspace_resolved)
        except ValueError:
            continue
        if not src_resolved.exists():
            _log.debug("preserve-artifacts %s: declared artifact missing: %s",
                       task_id, src_resolved)
            continue
        try:
            dest_root.mkdir(parents=True, exist_ok=True)
            dest = dest_root / src_resolved.name
            if src_resolved.is_dir():
                shutil.copytree(src_resolved, dest, dirs_exist_ok=True)
            else:
                shutil.copy2(src_resolved, dest)
            preserved.append(src_resolved.name)
        except (OSError, shutil.Error) as exc:
            _log.warning(
                "preserve-artifacts %s: copy %s -> %s failed: %s",
                task_id, src_resolved, dest_root, exc,
            )
            continue
    if preserved:
        _log.info(
            "preserve-artifacts %s: copied %d artifact(s) to %s before "
            "scratch cleanup: %s",
            task_id, len(preserved), dest_root, preserved,
        )
    return preserved


def _cleanup_workspace(conn: sqlite3.Connection, task_id: str) -> None:
    """Remove a task's scratch workspace dir and kill its stale tmux session.

    Called from :func:`complete_task` after the DB transaction commits.
    Best-effort — any error is swallowed so cleanup never blocks task completion.
    Only ``scratch`` workspaces are removed; ``worktree`` and ``dir`` workspaces
    are intentionally preserved.

    Before ``shutil.rmtree`` the scratch dir, runs.metadata.artifacts[]
    paths underneath it are auto-copied to
    ``~/.hermes/reports/by-task/<task_id>/<basename>`` via
    :func:`_preserve_scratch_artifacts`. Workers opt in by writing the
    absolute paths into the existing per-run ``metadata.artifacts``
    array before calling ``kanban_complete``.
    """
    try:
        row = conn.execute(
            "SELECT workspace_kind, workspace_path FROM tasks WHERE id = ?",
            (task_id,),
        ).fetchone()
        if not row:
            return
        kind: Optional[str] = row["workspace_kind"]
        path: Optional[str] = row["workspace_path"]
        if kind != "scratch" or not path:
            return
        import shutil
        wp = Path(path)
        if wp.is_dir():
            # Containment guard (#28818): a board's ``default_workdir`` can
            # pair ``workspace_kind='scratch'`` with a user-supplied path
            # pointing at a real source tree. Without this check, task
            # completion would unconditionally ``shutil.rmtree`` that path
            # and silently delete the user's source data.
            if _is_managed_scratch_path(wp):
                # Persist declared artifacts BEFORE rmtree.
                _preserve_scratch_artifacts(conn, task_id, wp)
                shutil.rmtree(wp, ignore_errors=True)
                _log.debug("Removed scratch workspace: %s", wp)
            else:
                _log.warning(
                    "Refusing to remove out-of-scratch workspace for task %s: %s "
                    "(workspace_kind='scratch' but path is outside any "
                    "kanban-managed workspaces root)",
                    task_id, wp,
                )
        # Also kill the tmux session for the worker that owned this task,
        # if the tmux session is now dead (worker process exited).
        _cleanup_worker_tmux(conn, task_id)
    except Exception:
        pass  # best-effort — never block completion


def _cleanup_worker_tmux(conn: sqlite3.Connection, task_id: str) -> None:
    """Kill the tmux session associated with a task's assignee, if dead."""
    try:
        row = conn.execute(
            "SELECT assignee FROM tasks WHERE id = ?", (task_id,)
        ).fetchone()
        if not row or not row["assignee"]:
            return
        assignee: str = row["assignee"]
        # Workers named swarm1-12 use tmux sessions named swarm-swarm1 etc.
        session = f"swarm-{assignee}"
        # Check if session exists and pane is dead before killing
        out = subprocess.run(
            ["tmux", "list-panes", "-t", session, "-F", "#{pane_dead}"],
            capture_output=True, text=True, timeout=5,
        )
        if out.stdout.strip() == "1":
            subprocess.run(
                ["tmux", "kill-session", "-t", session],
                capture_output=True, timeout=5,
            )
            _log.debug("Killed stale tmux session: %s", session)
    except Exception:
        pass  # best-effort — never block completion


# ---------------------------------------------------------------------------
# First-use tip for scratch workspaces
# ---------------------------------------------------------------------------
#
# Scratch workspaces are intentionally ephemeral — ``_cleanup_workspace``
# removes them as soon as ``complete_task`` runs.  New users often don't
# realize that and lose worker output (community report, May 2026).  The
# behavior is right; the lack of warning is the bug.
#
# On the FIRST scratch workspace materialization across the whole install
# we:
#   1. Log a warning line on the dispatcher logger.
#   2. Append a ``tip_scratch_workspace`` event on the task so it's visible
#      via ``hermes kanban show <id>`` and the dashboard.
#   3. Touch a sentinel file under ``kanban_home() / '.scratch_tip_shown'``
#      so we don't repeat the tip — once you know, you know.
#
# Scope is per-install, not per-board: a user creating a second board
# already learned the lesson on board #1.

_SCRATCH_TIP_SENTINEL_NAME = ".scratch_tip_shown"

_SCRATCH_TIP_MESSAGE = (
    "scratch workspaces are ephemeral — they're deleted when the task "
    "completes. Use --workspace worktree: (git worktree) or "
    "--workspace dir:/abs/path (existing dir) to preserve worker output."
)


def _scratch_tip_sentinel_path() -> Path:
    """Path to the per-install scratch-workspace-tip sentinel file."""
    return kanban_home() / _SCRATCH_TIP_SENTINEL_NAME


def _scratch_tip_shown() -> bool:
    """True iff the scratch-workspace tip has already been emitted on this
    install. Best-effort — any error means we re-emit, which is the safer
    failure mode for a help message."""
    try:
        return _scratch_tip_sentinel_path().exists()
    except OSError:
        return False


def _mark_scratch_tip_shown() -> None:
    """Touch the sentinel so future scratch workspaces stay silent.

    Best-effort: a failure here just means the tip might appear once more,
    which is preferable to crashing dispatch over a help message.
    """
    try:
        path = _scratch_tip_sentinel_path()
        path.parent.mkdir(parents=True, exist_ok=True)
        path.touch(exist_ok=True)
    except OSError:
        pass


def _maybe_emit_scratch_tip(
    conn: sqlite3.Connection,
    task_id: str,
    workspace_kind: Optional[str],
) -> None:
    """Emit the first-use scratch-workspace tip exactly once per install.

    Called from the dispatcher right after a scratch workspace is
    materialized. No-op for ``worktree`` / ``dir`` workspaces (they're
    preserved by design) and no-op after the sentinel exists.
    """
    if (workspace_kind or "scratch") != "scratch":
        return
    if _scratch_tip_shown():
        return
    try:
        _log.warning("kanban: %s (task %s)", _SCRATCH_TIP_MESSAGE, task_id)
        with write_txn(conn):
            _append_event(
                conn, task_id, "tip_scratch_workspace",
                {"message": _SCRATCH_TIP_MESSAGE},
            )
    except Exception:
        # Best-effort — never block the spawn loop over a help message.
        pass
    finally:
        _mark_scratch_tip_shown()


def edit_completed_task_result(
    conn: sqlite3.Connection,
    task_id: str,
    *,
    result: str,
    summary: Optional[str] = None,
    metadata: Optional[dict] = None,
) -> bool:
    """Backfill the user-visible result for an already completed task."""
    handoff_summary = summary if summary is not None else result
    with write_txn(conn):
        row = conn.execute(
            "SELECT status FROM tasks WHERE id = ?", (task_id,),
        ).fetchone()
        if not row or row["status"] != "done":
            return False
        conn.execute(
            "UPDATE tasks SET result = ? WHERE id = ?",
            (result, task_id),
        )
        run = conn.execute(
            """
            SELECT id FROM task_runs
             WHERE task_id = ?
               AND outcome = 'completed'
             ORDER BY COALESCE(ended_at, started_at, 0) DESC, id DESC
             LIMIT 1
            """,
            (task_id,),
        ).fetchone()
        run_id = int(run["id"]) if run else None
        if run_id is None:
            run_id = _synthesize_ended_run(
                conn, task_id,
                outcome="completed",
                summary=handoff_summary,
                metadata=metadata,
            )
        else:
            conn.execute(
                "UPDATE task_runs SET summary = ? WHERE id = ?",
                (handoff_summary, run_id),
            )
            if metadata is not None:
                conn.execute(
                    "UPDATE task_runs SET metadata = ? WHERE id = ?",
                    (json.dumps(metadata, ensure_ascii=False), run_id),
                )
        ev_summary = (
            handoff_summary.strip().splitlines()[0][:400]
            if handoff_summary else ""
        )
        _append_event(
            conn, task_id, "edited",
            {
                "fields": (
                    ["result", "summary"]
                    + (["metadata"] if metadata is not None else [])
                ),
                "result_len": len(result) if result else 0,
                "summary": ev_summary or None,
            },
            run_id=run_id,
        )
    return True


def block_task(
    conn: sqlite3.Connection,
    task_id: str,
    *,
    reason: Optional[str] = None,
    block_type: Optional[str] = None,
    expected_run_id: Optional[int] = None,
    context_comment_id: Optional[int] = None,
) -> bool:
    """Transition an active or queued task to ``blocked``.

    Worker-owned blocks pass ``expected_run_id`` and are restricted to the
    live ``running|ready`` task/run pair. Administrative/manual blocks without
    an expected run may also block ``triage`` and ``todo`` tasks so superseded
    fixtures can be parked without direct SQLite surgery.
    """
    with write_txn(conn):
        if expected_run_id is None:
            cur = conn.execute(
                """
                UPDATE tasks
                   SET status       = 'blocked',
                       claim_lock   = NULL,
                       claim_expires= NULL,
                       worker_pid   = NULL
                 WHERE id = ?
                   AND status IN ('running', 'ready', 'todo', 'triage')
                """,
                (task_id,),
            )
        else:
            cur = conn.execute(
                """
                UPDATE tasks
                   SET status       = 'blocked',
                       claim_lock   = NULL,
                       claim_expires= NULL,
                       worker_pid   = NULL
                 WHERE id = ?
                   AND status IN ('running', 'ready')
                   AND current_run_id = ?
                """,
                (task_id, int(expected_run_id)),
            )
        if cur.rowcount != 1:
            return False
        run_id = _end_run(
            conn, task_id,
            outcome="blocked", status="blocked",
            summary=reason,
        )
        # Synthesize a run when blocking a never-claimed task so the
        # reason is preserved in attempt history.
        if run_id is None and reason:
            run_id = _synthesize_ended_run(
                conn, task_id,
                outcome="blocked",
                summary=reason,
            )
        normalized_block_type = _normalize_block_type_marker(block_type)
        if normalized_block_type is None and _is_iteration_budget_guard_reason(reason):
            normalized_block_type = _ITERATION_BUDGET_BLOCK_TYPE
        block_payload_extra: dict[str, Any] = {"reason": reason}
        if normalized_block_type:
            block_payload_extra["block_type"] = normalized_block_type
        if context_comment_id is not None:
            comment = conn.execute(
                "SELECT body FROM task_comments WHERE id = ? AND task_id = ?",
                (int(context_comment_id), task_id),
            ).fetchone()
            if comment:
                block_payload_extra["context_comment_id"] = int(context_comment_id)
                snippet = (comment["body"] or "").strip().replace("\n", " ")[:200]
                if snippet:
                    block_payload_extra["context_snippet"] = snippet
        block_payload = _terminal_event_payload(
            conn,
            task_id,
            run_id,
            outcome="blocked",
            summary=reason,
            extra=block_payload_extra,
        )
        _append_event(conn, task_id, "blocked", block_payload, run_id=run_id)
        return True



def promote_task(
    conn: sqlite3.Connection,
    task_id: str,
    *,
    actor: str,
    reason: Optional[str] = None,
    force: bool = False,
    dry_run: bool = False,
) -> tuple[bool, Optional[str]]:
    """Manually promote a `todo` or `blocked` task to `ready`.

    Mirrors the automatic promotion done by ``recompute_ready`` but
    drives it from a deliberate operator action with an audit-trail
    entry. Refuses to promote if any parent dep is not in a terminal
    state (`done`/`archived`) unless ``force=True``. Does NOT change
    assignee or claim state. Returns ``(True, None)`` on success and
    ``(False, reason)`` if refused. ``dry_run=True`` validates the
    promotion would succeed without mutating state.
    """
    row = conn.execute(
        "SELECT status FROM tasks WHERE id = ?", (task_id,)
    ).fetchone()
    if row is None:
        return False, f"task {task_id} not found"

    cur_status = row["status"]
    if cur_status not in ("todo", "blocked"):
        return False, (
            f"task {task_id} is {cur_status!r}; promote only applies to "
            f"'todo' or 'blocked'"
        )

    if not force:
        parents = conn.execute(
            "SELECT t.id, t.status FROM tasks t "
            "JOIN task_links l ON l.parent_id = t.id "
            "WHERE l.child_id = ?",
            (task_id,),
        ).fetchall()
        unsatisfied = [
            p["id"] for p in parents
            if p["status"] not in ("done", "archived")
        ]
        if unsatisfied:
            return False, (
                f"unsatisfied parent dependencies: "
                f"{', '.join(unsatisfied)} (use --force to override)"
            )

    if dry_run:
        return True, None

    with write_txn(conn):
        upd = conn.execute(
            "UPDATE tasks SET status = 'ready' "
            "WHERE id = ? AND status IN ('todo', 'blocked')",
            (task_id,),
        )
        if upd.rowcount != 1:
            return False, f"task {task_id} status changed during promotion"
        _append_event(
            conn,
            task_id,
            "promoted_manual",
            {"actor": actor, "reason": reason, "forced": force},
        )

    return True, None


def unblock_task(conn: sqlite3.Connection, task_id: str) -> bool:
    """Transition ``blocked``/``scheduled`` -> ready or todo.

    Defensively closes any stale ``current_run_id`` pointer before flipping
    status. In the common path (``block_task`` closed the run already) this
    is a no-op. If a future or external write left the pointer dangling,
    the leaked run is closed as ``reclaimed`` inside the same txn so the
    runs invariant (``current_run_id IS NULL`` ⇔ run row in terminal
    state) holds for the rest of this function's lifetime.
    """
    now = int(time.time())
    with write_txn(conn):
        stale = conn.execute(
            "SELECT current_run_id FROM tasks WHERE id = ? AND status IN ('blocked', 'scheduled')",
            (task_id,),
        ).fetchone()
        if stale and stale["current_run_id"]:
            conn.execute(
                """
                UPDATE task_runs
                   SET status = 'reclaimed', outcome = 'reclaimed',
                       summary = COALESCE(summary, 'invariant recovery on unblock'),
                       ended_at = ?,
                       claim_lock = NULL, claim_expires = NULL, worker_pid = NULL
                 WHERE id = ? AND ended_at IS NULL
                """,
                (now, int(stale["current_run_id"])),
            )
        # Re-gate on parent completion before flipping 'blocked' back to
        # 'ready'. Unconditionally setting status='ready' here bypasses the
        # parent-completion invariant (the dispatcher trusts that column);
        # if parents are still in progress the task must wait in 'todo'
        # until recompute_ready picks it up. RCA: Bug 2 at
        # kanban/boards/cookai/workspaces/t_a6acd07d/root-cause.md.
        undone_parents = conn.execute(
            "SELECT 1 FROM task_links l "
            "JOIN tasks p ON p.id = l.parent_id "
            "WHERE l.child_id = ? AND p.status != 'done' LIMIT 1",
            (task_id,),
        ).fetchone()
        new_status = "todo" if undone_parents else "ready"
        cur = conn.execute(
            "UPDATE tasks SET status = ?, current_run_id = NULL, "
            "consecutive_failures = 0, last_failure_error = NULL "
            "WHERE id = ? AND status IN ('blocked', 'scheduled')",
            (new_status, task_id),
        )
        if cur.rowcount != 1:
            return False
        _append_event(
            conn, task_id, "unblocked",
            {"status": new_status} if new_status != "ready" else None,
        )
        return True


def specify_triage_task(
    conn: sqlite3.Connection,
    task_id: str,
    *,
    title: Optional[str] = None,
    body: Optional[str] = None,
    assignee: Optional[str] = None,
    author: Optional[str] = None,
) -> bool:
    """Flesh out a triage task and promote it to ``todo``.

    Atomically updates ``title`` / ``body`` / ``assignee`` (when provided)
    and transitions ``status: triage -> todo`` in a single write txn. Returns
    False when the task is missing or not in the ``triage`` column — callers
    should surface that as "nothing to specify" rather than an error.

    ``todo`` (not ``ready``) is the correct landing column: ``recompute_ready``
    promotes parent-free / parent-done todos to ``ready`` on the next
    dispatcher tick, which keeps the normal parent-gating behaviour intact
    for specified tasks that happen to have open parents.

    ``author`` is recorded on an audit comment only when at least one of
    ``title`` / ``body`` / ``assignee`` actually changed — avoids noisy
    comment spam for status-only promotions.
    """
    if title is not None and not title.strip():
        raise ValueError("title cannot be blank")
    assignee = _canonical_assignee(assignee)
    with write_txn(conn):
        existing = conn.execute(
            "SELECT title, body, assignee FROM tasks WHERE id = ? AND status = 'triage'",
            (task_id,),
        ).fetchone()
        if existing is None:
            return False
        sets: list[str] = ["status = 'todo'"]
        params: list[Any] = []
        changed_fields: list[str] = []
        if title is not None and title.strip() != (existing["title"] or ""):
            sets.append("title = ?")
            params.append(title.strip())
            changed_fields.append("title")
        if body is not None and (body or "") != (existing["body"] or ""):
            sets.append("body = ?")
            params.append(body)
            changed_fields.append("body")
        if assignee is not None and assignee != (existing["assignee"] or None):
            sets.append("assignee = ?")
            params.append(assignee)
            changed_fields.append("assignee")
        params.append(task_id)
        cur = conn.execute(
            f"UPDATE tasks SET {', '.join(sets)} "
            f"WHERE id = ? AND status = 'triage'",
            tuple(params),
        )
        if cur.rowcount != 1:
            return False
        if changed_fields and author and author.strip():
            # Inline INSERT (rather than ``add_comment``) because we're
            # already inside this function's write_txn — nested BEGIN
            # IMMEDIATE would raise OperationalError. We also skip the
            # 'commented' event that ``add_comment`` emits, since the
            # 'specified' event below already records the change.
            conn.execute(
                "INSERT INTO task_comments (task_id, author, body, created_at) "
                "VALUES (?, ?, ?, ?)",
                (
                    task_id,
                    author.strip(),
                    "Specified — updated "
                    + ", ".join(changed_fields)
                    + " and promoted to todo.",
                    int(time.time()),
                ),
            )
        _append_event(
            conn,
            task_id,
            "specified",
            {"changed_fields": changed_fields} if changed_fields else None,
        )
    # Outside the write_txn above, so we don't nest BEGIN IMMEDIATE — the
    # ready-promotion pass opens its own IMMEDIATE txn. This runs the same
    # logic the dispatcher would on its next tick, so a specified task
    # with no open parents flips straight to 'ready' here instead of
    # idling in 'todo' until the next sweep.
    recompute_ready(conn)
    return True


def decompose_triage_task(
    conn: sqlite3.Connection,
    task_id: str,
    *,
    root_assignee: Optional[str],
    children: list[dict],
    author: Optional[str] = None,
    auto_promote: bool = True,
) -> Optional[list[str]]:
    """Fan a triage task out into child tasks and promote the root to ``todo``.

    The root task stays alive and becomes the parent of every child —
    when all children reach ``done``, the root promotes to ``ready`` and
    its assignee (typically the orchestrator profile) wakes back up to
    judge completion or spawn more work.

    ``children`` is a list of dicts, each shaped like::

        {
            "title": "...",
            "body": "...",                     # optional
            "assignee": "profile-name",        # optional, None -> default fallback
            "parents": [0, 2],                 # indices into this same children list
        }

    Returns the list of created child task ids (in input order) on
    success. Returns ``None`` when:
      - The root task does not exist
      - The root task is not in ``triage``
      - A cycle would result (caller built a bad graph)

    Validation of titles/assignees happens inside the same write_txn as
    the inserts so a malformed entry aborts the whole decomposition
    cleanly (no orphan children).
    """
    if not children:
        return None
    if root_assignee is not None:
        root_assignee = _canonical_assignee(root_assignee)

    # Pre-validate the children list shape outside the txn. Cheap checks
    # that don't need DB access. Bad input aborts before we touch the DB.
    for idx, child in enumerate(children):
        if not isinstance(child, dict):
            raise ValueError(f"child[{idx}] is not a dict")
        title = child.get("title")
        if not isinstance(title, str) or not title.strip():
            raise ValueError(f"child[{idx}].title is required")
        parents_idx = child.get("parents") or []
        if not isinstance(parents_idx, list):
            raise ValueError(f"child[{idx}].parents must be a list")
        for p in parents_idx:
            if not isinstance(p, int) or p < 0 or p >= len(children):
                raise ValueError(
                    f"child[{idx}].parents[{p}] is not a valid index into children"
                )
            if p == idx:
                raise ValueError(f"child[{idx}] cannot list itself as a parent")

    # Detect cycles in the sibling parent graph (Kahn's topological sort).
    # link_tasks() calls _would_cycle() for every new edge; here we check
    # the entire sibling graph before touching the DB.  A cycle silently
    # deadlocks every involved child in 'todo' because recompute_ready()
    # can never promote them.
    _in_deg = [0] * len(children)
    _adj: list[list[int]] = [[] for _ in range(len(children))]
    for _i, _c in enumerate(children):
        for _p in (_c.get("parents") or []):
            _adj[_p].append(_i)
            _in_deg[_i] += 1
    _queue = [_i for _i in range(len(children)) if _in_deg[_i] == 0]
    _seen = 0
    while _queue:
        _node = _queue.pop()
        _seen += 1
        for _nb in _adj[_node]:
            _in_deg[_nb] -= 1
            if _in_deg[_nb] == 0:
                _queue.append(_nb)
    if _seen != len(children):
        raise ValueError("cyclic dependency detected in decomposed children list")

    # We do the full decomposition in a SINGLE write_txn so it's
    # atomic: either every child is created AND the root flips to
    # ``todo``, or nothing changes. We deliberately do NOT call any
    # kb helper that opens its own write_txn (create_task, link_tasks,
    # add_comment) from inside this block — see architecture.md
    # write_txn pitfalls. Instead we inline the INSERTs and
    # _append_event calls.
    now = int(time.time())
    child_ids: list[str] = []
    with write_txn(conn):
        root_row = conn.execute(
            "SELECT id, status, tenant FROM tasks WHERE id = ?", (task_id,)
        ).fetchone()
        if root_row is None:
            return None
        if root_row["status"] != "triage":
            return None
        tenant = root_row["tenant"]

        # Create children. Status is 'todo' regardless of parents — we
        # link them under the root AFTER creation so the dispatcher
        # sees a coherent state, and recompute_ready() at the end
        # promotes parent-free children to 'ready'.
        for idx, child in enumerate(children):
            new_id = _new_task_id()
            title = child["title"].strip()
            body = child.get("body")
            assignee = _canonical_assignee(child.get("assignee"))
            conn.execute(
                "INSERT INTO tasks "
                "(id, title, body, assignee, status, workspace_kind, "
                " tenant, created_at, created_by) "
                "VALUES (?, ?, ?, ?, 'todo', 'scratch', ?, ?, ?)",
                (
                    new_id,
                    title,
                    body if isinstance(body, str) else None,
                    assignee,
                    tenant,
                    now,
                    (author or "decomposer"),
                ),
            )
            _append_event(
                conn, new_id, "created",
                {"by": author or "decomposer", "from_decompose_of": task_id},
            )
            child_ids.append(new_id)

        # Link children to their sibling parents (within the decomposed graph).
        for idx, child in enumerate(children):
            for p_idx in child.get("parents") or []:
                parent_id = child_ids[p_idx]
                child_id = child_ids[idx]
                conn.execute(
                    "INSERT OR IGNORE INTO task_links (parent_id, child_id) "
                    "VALUES (?, ?)",
                    (parent_id, child_id),
                )
                _append_event(
                    conn, child_id, "linked",
                    {"parent": parent_id, "child": child_id},
                )

        # Link the ROOT task as a child of every leaf child — i.e. the
        # root waits for the whole graph. Simpler than computing leaves:
        # link root under every child. Cycle-free because the root is
        # only ever a child here, never a parent of children.
        for cid in child_ids:
            conn.execute(
                "INSERT OR IGNORE INTO task_links (parent_id, child_id) "
                "VALUES (?, ?)",
                (cid, task_id),
            )

        # Flip the root: triage -> todo, set assignee to the orchestrator.
        sets = ["status = 'todo'"]
        params: list[Any] = []
        if root_assignee is not None:
            sets.append("assignee = ?")
            params.append(root_assignee)
        params.append(task_id)
        conn.execute(
            f"UPDATE tasks SET {', '.join(sets)} WHERE id = ?",
            tuple(params),
        )

        # Audit comment + event on the root so the timeline shows the fan-out.
        if author and author.strip():
            conn.execute(
                "INSERT INTO task_comments (task_id, author, body, created_at) "
                "VALUES (?, ?, ?, ?)",
                (
                    task_id,
                    author.strip(),
                    "Decomposed into "
                    + ", ".join(child_ids)
                    + ". Root will wake when all children complete.",
                    now,
                ),
            )
        _append_event(
            conn, task_id, "decomposed",
            {
                "child_ids": child_ids,
                "root_assignee": root_assignee,
            },
        )

    # Outside the write_txn: promote parent-free children to 'ready'
    # so the dispatcher picks them up on its next tick. Same pattern
    # specify_triage_task uses.  When auto_promote is False children
    # stay in 'todo' until the user manually promotes them — useful
    # for manual-review-first workflows.
    if auto_promote:
        recompute_ready(conn)
    return child_ids


def archive_task(conn: sqlite3.Connection, task_id: str) -> bool:
    with write_txn(conn):
        cur = conn.execute(
            "UPDATE tasks SET status = 'archived', "
            "    claim_lock = NULL, claim_expires = NULL, worker_pid = NULL "
            "WHERE id = ? AND status != 'archived'",
            (task_id,),
        )
        if cur.rowcount != 1:
            return False
        # If archive happened while a run was still in flight (e.g. user
        # archived a running task from the dashboard), close that run with
        # outcome='reclaimed' so attempt history isn't orphaned.
        run_id = _end_run(
            conn, task_id,
            outcome="reclaimed", status="reclaimed",
            summary="task archived with run still active",
        )
        _append_event(conn, task_id, "archived", None, run_id=run_id)
    # ``archived`` parents no longer block children, same as ``done``.
    # Promote newly-unblocked dependents immediately instead of waiting
    # for a later dispatcher tick.
    recompute_ready(conn)
    return True


def delete_archived_task(conn: sqlite3.Connection, task_id: str) -> bool:
    """Permanently remove an already-archived task and its related rows.

    Safety guard: only archived tasks can be deleted. Active / blocked / done
    tasks must be explicitly archived first so accidental data loss requires a
    second deliberate action.
    """
    with write_txn(conn):
        row = conn.execute(
            "SELECT status FROM tasks WHERE id = ?",
            (task_id,),
        ).fetchone()
        if not row or row["status"] != "archived":
            return False
        conn.execute(
            "DELETE FROM task_links WHERE parent_id = ? OR child_id = ?",
            (task_id, task_id),
        )
        conn.execute("DELETE FROM task_comments WHERE task_id = ?", (task_id,))
        conn.execute("DELETE FROM task_events WHERE task_id = ?", (task_id,))
        conn.execute("DELETE FROM task_runs WHERE task_id = ?", (task_id,))
        conn.execute("DELETE FROM kanban_notify_subs WHERE task_id = ?", (task_id,))
        cur = conn.execute("DELETE FROM tasks WHERE id = ?", (task_id,))
        return cur.rowcount == 1


def delete_task(conn: sqlite3.Connection, task_id: str) -> bool:
    """Hard-delete a task and cascade to all related rows.

    Because the schema does not use ``ON DELETE CASCADE`` foreign keys,
    we explicitly delete from child tables first, then the task row.
    This keeps the operation atomic (single ``write_txn``).

    Returns ``True`` if the task existed and was deleted, ``False``
    if the task was not found.
    """
    with write_txn(conn):
        cur = conn.execute("DELETE FROM tasks WHERE id = ?", (task_id,))
        if cur.rowcount != 1:
            return False
        conn.execute("DELETE FROM task_links WHERE parent_id = ? OR child_id = ?", (task_id, task_id))
        conn.execute("DELETE FROM task_comments WHERE task_id = ?", (task_id,))
        conn.execute("DELETE FROM task_events WHERE task_id = ?", (task_id,))
        conn.execute("DELETE FROM task_runs WHERE task_id = ?", (task_id,))
        conn.execute("DELETE FROM kanban_notify_subs WHERE task_id = ?", (task_id,))
    recompute_ready(conn)
    return True


# ---------------------------------------------------------------------------
# Workspace resolution
# ---------------------------------------------------------------------------

def resolve_workspace(task: Task, *, board: Optional[str] = None) -> Path:
    """Resolve (and create if needed) the workspace for a task.

    - ``scratch``: a fresh dir under ``<board-root>/workspaces/<id>/``,
      where ``<board-root>`` is the active board's root. The path is the
      same for the dispatcher and every profile worker, so handoff is
      path-stable.
    - ``dir:<path>``: the path stored in ``workspace_path``.  Created
      if missing.  MUST be absolute — relative paths are rejected to
      prevent confused-deputy traversal where ``../../../tmp/attacker``
      resolves against the dispatcher's CWD instead of a meaningful
      root.  Users who want a kanban-root-relative workspace should
      compute the absolute path themselves.
    - ``worktree``: a git worktree at ``workspace_path``.  Not created
      automatically in v1 -- the kanban-worker skill documents
      ``git worktree add`` as a worker-side step.  Returns the intended path.

    Persist the resolved path back to the task row via ``set_workspace_path``
    so subsequent runs reuse the same directory.
    """
    kind = task.workspace_kind or "scratch"
    if kind == "scratch":
        if task.workspace_path:
            # Legacy scratch tasks that were set to an explicit path get the
            # same absolute-path guard as dir: — consistent with the
            # threat model.
            p = Path(task.workspace_path).expanduser()
            if not p.is_absolute():
                raise ValueError(
                    f"task {task.id} has non-absolute workspace_path "
                    f"{task.workspace_path!r}; workspace paths must be absolute"
                )
        else:
            p = workspaces_root(board=board) / task.id
        p.mkdir(parents=True, exist_ok=True)
        return p
    if kind == "dir":
        if not task.workspace_path:
            raise ValueError(
                f"task {task.id} has workspace_kind=dir but no workspace_path"
            )
        p = Path(task.workspace_path).expanduser()
        if not p.is_absolute():
            raise ValueError(
                f"task {task.id} has non-absolute workspace_path "
                f"{task.workspace_path!r}; use an absolute path "
                f"(relative paths are ambiguous against the dispatcher's CWD)"
            )
        p.mkdir(parents=True, exist_ok=True)
        return p
    if kind == "worktree":
        if not task.workspace_path:
            # Default: .worktrees/<id>/ under CWD.  Worker skill creates it.
            return Path.cwd() / ".worktrees" / task.id
        p = Path(task.workspace_path).expanduser()
        if not p.is_absolute():
            raise ValueError(
                f"task {task.id} has non-absolute worktree path "
                f"{task.workspace_path!r}; use an absolute path"
            )
        return p
    raise ValueError(f"unknown workspace_kind: {kind}")


def set_workspace_path(
    conn: sqlite3.Connection, task_id: str, path: Path | str
) -> None:
    with write_txn(conn):
        conn.execute(
            "UPDATE tasks SET workspace_path = ? WHERE id = ?",
            (str(path), task_id),
        )


# ---------------------------------------------------------------------------
def schedule_task(
    conn: sqlite3.Connection,
    task_id: str,
    *,
    reason: Optional[str] = None,
    expected_run_id: Optional[int] = None,
) -> bool:
    """Park a task in ``scheduled`` so it is waiting on time, not human input.

    ``scheduled`` tasks are intentionally not dispatchable; an external cron,
    human action, or automation can later call ``unblock_task`` to re-gate them
    to ``ready`` (or ``todo`` if parents are still incomplete).
    """
    with write_txn(conn):
        params: list[Any] = [task_id]
        sql = """
            UPDATE tasks
               SET status       = 'scheduled',
                   claim_lock   = NULL,
                   claim_expires= NULL,
                   worker_pid   = NULL
             WHERE id = ?
               AND status IN ('todo', 'ready', 'running', 'blocked')
        """
        if expected_run_id is not None:
            sql += " AND current_run_id = ?"
            params.append(int(expected_run_id))
        cur = conn.execute(sql, params)
        if cur.rowcount != 1:
            return False
        run_id = _end_run(
            conn, task_id,
            outcome="scheduled", status="scheduled",
            summary=reason,
        )
        if run_id is None and reason:
            run_id = _synthesize_ended_run(
                conn, task_id,
                outcome="scheduled",
                summary=reason,
            )
        _append_event(conn, task_id, "scheduled", {"reason": reason}, run_id=run_id)
        return True


# Dispatcher (one-shot pass)
# ---------------------------------------------------------------------------

# After this many consecutive non-success attempts on a task/profile, the
# dispatcher stops retrying and parks the task in ``blocked`` with a reason so
# a human can investigate. Prevents retry storms when a worker repeatedly times
# out, crashes, or cannot spawn.
DEFAULT_FAILURE_LIMIT = 2
# Legacy alias — callers / tests still reference the old name.
DEFAULT_SPAWN_FAILURE_LIMIT = DEFAULT_FAILURE_LIMIT

# C2 Backoff-Sleep Enforcement (followup-2026-05-17 §2).
# Must stay aligned with manifest:
# ~/.hermes/policies/standing-approvals.yaml
# approvals.coordinator.auto_retry_worker.caps.backoff_sec.
_RETRY_AFTER_BACKOFF_SEC = (30, 120)
_RETRY_AFTER_TAG = "; retry_after="


def _retry_after_ts_from_error(err):
    """Parse the ``; retry_after=<unix-ts>`` suffix from a
    ``tasks.last_failure_error`` value. Returns the integer timestamp or
    ``None`` when absent / malformed."""
    if not err or _RETRY_AFTER_TAG not in err:
        return None
    suffix = err.rsplit(_RETRY_AFTER_TAG, 1)[1].strip()
    try:
        return int(suffix.split()[0])
    except (ValueError, IndexError):
        return None


def _stamp_retry_after(err, ts):
    """Append (or replace) ``; retry_after=<ts>`` on a
    ``last_failure_error`` string. Caps total length at 500 chars to
    match column constraints."""
    base = err or ""
    if _RETRY_AFTER_TAG in base:
        base = base.rsplit(_RETRY_AFTER_TAG, 1)[0]
    stamped = base.rstrip() + _RETRY_AFTER_TAG + str(int(ts))
    return stamped[:500]


def _backoff_sec_for_failure(failures):
    """Backoff seconds to wait before next claim, based on the new
    ``consecutive_failures`` count (1-based). Falls through to the last
    schedule entry once failures exceed the table length."""
    if failures <= 0:
        return 0
    idx = min(int(failures) - 1, len(_RETRY_AFTER_BACKOFF_SEC) - 1)
    return int(_RETRY_AFTER_BACKOFF_SEC[idx])

# Max bytes to keep in a single worker log file. The dispatcher truncates
# and rotates on spawn if the file is larger than this at spawn time.
DEFAULT_LOG_ROTATE_BYTES = 2 * 1024 * 1024   # 2 MiB
DEFAULT_LOG_BACKUP_COUNT = 1

# Keep a little wall-clock budget for the worker to observe a terminal timeout
# and call kanban_block/kanban_complete before max_runtime_seconds kills it.
KANBAN_TERMINAL_TIMEOUT_GRACE_SECONDS = 30

# ---------------------------------------------------------------------------
# Respawn guard constants
# ---------------------------------------------------------------------------

# Patterns in last_failure_error that indicate a quota / auth blocker.
# These errors won't resolve by retrying immediately — auto-block instead.
_RESPAWN_BLOCKER_RE = re.compile(
    r"\b(quota|rate[\s_\-]?limit|429|403|auth\w*|"
    r"unauthorized|forbidden|billing|subscription|"
    r"access[\s_]denied|permission[\s_]denied|"
    r"invalid[\s_]api[\s_]key)\b",
    re.IGNORECASE,
)

# Within this window a completed run counts as "recent proof"; don't re-spawn.
_RESPAWN_GUARD_SUCCESS_WINDOW = 3600  # 1 hour

# Within this window a GitHub PR URL in a comment blocks re-spawn.
_RESPAWN_GUARD_PR_WINDOW = 86400  # 24 hours

# Pattern matching a GitHub PR URL in task comments.
_RESPAWN_GUARD_PR_URL_RE = re.compile(
    r"https?://github\.com/[^/\s]+/[^/\s]+/pull/\d+",
    re.IGNORECASE,
)


@dataclass
class DispatchResult:
    """Outcome of a single ``dispatch`` pass."""

    reclaimed: int = 0
    promoted: int = 0
    spawned: list[tuple[str, str, str]] = field(default_factory=list)
    """List of ``(task_id, assignee, workspace_path)`` triples."""
    skipped_unassigned: list[str] = field(default_factory=list)
    """Ready task ids skipped because they have no assignee at all.
    Operator-actionable — usually a misfiled task waiting for routing."""
    skipped_nonspawnable: list[str] = field(default_factory=list)
    """Ready task ids skipped because their assignee names a control-plane
    lane (a Claude Code terminal like ``orion-cc``) rather than a Hermes
    profile. Expected steady-state on multi-lane setups; NOT an
    operator-actionable failure. Tracked separately so health telemetry
    can distinguish "real stuck" (nothing spawned but spawnable work
    available) from "correctly idle" (nothing spawnable in the queue)."""
    crashed: list[str] = field(default_factory=list)
    """Task ids reclaimed because their worker PID disappeared."""
    auto_blocked: list[str] = field(default_factory=list)
    """Task ids auto-blocked by the spawn-failure circuit breaker."""
    timed_out: list[str] = field(default_factory=list)
    """Task ids whose workers exceeded ``max_runtime_seconds``."""
    stale: list[str] = field(default_factory=list)
    """Task ids reclaimed because no progress (heartbeat) was seen
    within ``dispatch_stale_timeout_seconds``."""
    respawn_guarded: list[tuple[str, str]] = field(default_factory=list)
    """Tasks skipped by the respawn guard, as ``(task_id, reason)`` pairs.

    Reasons: ``"blocker_auth"`` (quota/auth error — also auto-blocked),
    ``"recent_success"`` (completed run within guard window),
    ``"active_pr"`` (GitHub PR URL in a recent comment)."""
    auto_continued: list[str] = field(default_factory=list)
    """Blocked task ids the dispatcher autonomously reopened after iteration-budget exhaustion."""
    continuation_capped: list[str] = field(default_factory=list)
    """Blocked task ids left parked because the iteration-budget continuation cap was reached."""
    preflight_blocked: list[str] = field(default_factory=list)
    """Task ids blocked before claim/spawn because they failed dispatcher preflight."""
    retry_deferred: list[str] = field(default_factory=list)
    """Task ids skipped this tick because their retry_after window is still in the future (C2 backoff)."""


def _selected_scope_contract(body: Optional[str]) -> Optional[dict]:
    """Return the policy contract for the current task body.

    Coordinator tasks may embed child/reviewer task templates before their own
    top-level ``scope_contract``. In that shape, the final structured contract
    is the current task's contract; earlier contracts belong to embedded child
    templates and must not narrow the spawned worker schema or completion
    attestation for the parent task.
    """
    selected: Optional[dict] = None
    for mapping in _iter_task_policy_mappings(body):
        contract = mapping.get("scope_contract")
        if isinstance(contract, dict):
            selected = contract
    return selected


def _task_has_scope_contract_v2(body: Optional[str]) -> bool:
    contract = _selected_scope_contract(body)
    if not isinstance(contract, dict):
        return False
    version = contract.get("version")
    try:
        return int(version) >= 2
    except (TypeError, ValueError):
        return False


def _scope_contract_allowed_tools(body: Optional[str]) -> Optional[list[str]]:
    """Return structured ``scope_contract.allowed_tools`` when present.

    The values are task-author declarations, not a replacement for profile or
    model-native tool isolation. Dispatcher preflight uses them as the narrow
    allowed surface and records the effective list for worker attestation.
    """
    contract = _selected_scope_contract(body)
    if not isinstance(contract, dict) or "allowed_tools" not in contract:
        return None
    allowed = contract.get("allowed_tools")
    if isinstance(allowed, str):
        return [allowed]
    if isinstance(allowed, (list, tuple)):
        return [str(item).strip() for item in allowed if str(item).strip()]
    return []


def _scope_contract_forbidden_systems(body: Optional[str]) -> Optional[list[str]]:
    """Return structured ``scope_contract.forbidden_systems`` when present."""
    contract = _selected_scope_contract(body)
    if not isinstance(contract, dict) or "forbidden_systems" not in contract:
        return None
    forbidden = contract.get("forbidden_systems")
    if isinstance(forbidden, str):
        return [forbidden]
    if isinstance(forbidden, (list, tuple)):
        return [str(item).strip() for item in forbidden if str(item).strip()]
    return []


def _normalize_forbidden_system_name(name: str) -> str:
    return re.sub(r"[\s_]+", "-", str(name).strip().lower())


def _validate_scope_forbidden_systems(body: Optional[str]) -> list[str]:
    """Validate core Hermes-only forbidden-system declarations."""
    declared = _scope_contract_forbidden_systems(body)
    if declared is None:
        declared_keys: set[str] = set()
    else:
        declared_keys = {_normalize_forbidden_system_name(item) for item in declared}
    missing = [
        canonical
        for key, canonical in _REQUIRED_SCOPE_FORBIDDEN_SYSTEMS.items()
        if key not in declared_keys
    ]
    if not missing:
        return []
    return [
        "scope_contract.forbidden_systems is missing required entries: "
        + ", ".join(missing)
    ]


def _validate_scope_allowed_tools(body: Optional[str]) -> tuple[list[str], list[str]]:
    """Validate ``scope_contract.allowed_tools`` for dispatcher preflight.

    Returns ``(effective_toolsets, problems)``. Missing, unknown, and obviously
    broad/forbidden declarations fail closed before spawn.
    """
    allowed = _scope_contract_allowed_tools(body)
    if allowed is None:
        return [], ["scope_contract.allowed_tools is required"]
    normalized: list[str] = []
    problems: list[str] = []
    seen: set[str] = set()
    for item in allowed:
        name = str(item).strip()
        if not name:
            continue
        key = name.lower()
        if key in seen:
            continue
        seen.add(key)
        if key in _BROAD_SCOPE_ALLOWED_TOOL_MARKERS:
            problems.append(f"allowed_tool is too broad: {name}")
            continue
        if key in _FORBIDDEN_SCOPE_ALLOWED_TOOL_MARKERS:
            problems.append(f"allowed_tool is forbidden for Hermes-only dispatch: {name}")
            continue
        if name not in _KNOWN_SCOPE_ALLOWED_TOOLS:
            problems.append(f"unknown allowed_tool: {name}")
            continue
        normalized.append(name)
    if not normalized and not problems:
        problems.append("scope_contract.allowed_tools must not be empty")
    return normalized, problems


@contextlib.contextmanager
def _temporary_env(updates: dict[str, Optional[str]]):
    """Temporarily patch ``os.environ`` and restore it exactly afterwards."""
    sentinel = object()
    previous = {key: os.environ.get(key, sentinel) for key in updates}
    try:
        for key, value in updates.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value
        yield
    finally:
        for key, value in previous.items():
            if value is sentinel:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value  # type: ignore[assignment]


def _filter_tool_names_by_profile_disabled(
    tool_names: list[str],
    profile: Optional[str],
) -> list[str]:
    """Drop tool names whose owning toolset is in ``profile.agent.disabled_toolsets``.

    Finding F-2026-05-17-02: the dispatcher's kanban-effective filter (model_tools.py)
    deliberately overrides profile-level disabled_toolsets so worker spawns can still
    use dispatcher-approved coordination tools. The recorded ``effective_toolsets``,
    however, is consumed by ``capability_drift_detector`` — recording dispatcher-
    overridden but profile-disabled tools causes recurring false-positive drift
    after every cron tick. This helper produces the metadata-recording subset that
    aligns drift detection with the worker's profile-declared surface.
    """
    if not tool_names:
        return []
    requested = [str(n).strip() for n in tool_names if str(n).strip()]
    if not profile or not requested:
        return requested
    disabled_toolset_names = _load_profile_disabled_toolsets(profile)
    if not disabled_toolset_names:
        return requested
    try:
        import tools.kanban_tools  # noqa: F401
        from toolsets import resolve_toolset, validate_toolset
    except Exception:
        return requested
    disabled_tool_names: set[str] = set()
    for ts_name in disabled_toolset_names:
        try:
            if validate_toolset(ts_name):
                disabled_tool_names.update(resolve_toolset(ts_name))
        except Exception:
            continue
    return [name for name in requested if name not in disabled_tool_names]


def _load_profile_disabled_toolsets(profile: Optional[str]) -> list[str]:
    """Return ``agent.disabled_toolsets`` for a profile, or ``[]`` if unavailable.

    Finding F-2026-05-17-02 (Reactor-Matrix v1): the resolver must honor the
    profile's disabled_toolsets so the recorded ``effective_toolsets`` matches
    what the worker can actually invoke at runtime, not the broader contract
    declaration.
    """
    if not profile:
        return []
    try:
        _canon, profile_cfg_path = _profile_config_path(profile)
    except Exception:
        return []
    try:
        if not profile_cfg_path.exists():
            return []
        cfg = _parse_yaml_mapping(
            profile_cfg_path.read_text(encoding="utf-8"),
            source=profile_cfg_path,
        )
    except Exception:
        return []
    agent_section = cfg.get("agent") if isinstance(cfg, dict) else None
    if not isinstance(agent_section, dict):
        return []
    raw = agent_section.get("disabled_toolsets") or []
    if not isinstance(raw, list):
        return []
    return [str(item).strip() for item in raw if str(item).strip()]


def _resolve_scope_runtime_tool_schema_names(
    allowed_toolsets: list[str],
    *,
    task_id: Optional[str] = None,
    profile: Optional[str] = None,
) -> list[str]:
    """Resolve the model-native worker schema for a scoped allowlist.

    ``scope_contract.allowed_tools`` is only the declared policy. The worker's
    usable lifecycle surface is the schema that ``model_tools`` can actually
    build in a Kanban task context after registry ``check_fn`` filtering. Use
    that same runtime path here so dispatcher preflight blocks before spawn if
    a declared lifecycle tool is not present in the real worker schema.

    When ``profile`` is provided, the profile's ``agent.disabled_toolsets`` is
    threaded into ``get_tool_definitions`` so disabled toolsets are filtered
    out of the resolved schema (matches the worker's actual runtime surface).
    """
    requested = [str(name).strip() for name in (allowed_toolsets or []) if str(name).strip()]
    if not requested:
        return []

    profile_disabled = _load_profile_disabled_toolsets(profile)

    try:
        # Importing the tool module is idempotent and ensures module-level
        # registry.register(...) calls have happened in import-order-sensitive
        # test and CLI contexts.
        import tools.kanban_tools  # noqa: F401
        from model_tools import _clear_tool_defs_cache, get_tool_definitions
        from tools.registry import invalidate_check_fn_cache
    except Exception:
        return []

    env_updates = {
        "HERMES_KANBAN_TASK": task_id or "__dispatch_preflight__",
        "HERMES_KANBAN_EFFECTIVE_TOOLSETS": json.dumps(requested),
    }
    schemas = []
    with _temporary_env(env_updates):
        invalidate_check_fn_cache()
        _clear_tool_defs_cache()
        try:
            schemas = get_tool_definitions(
                enabled_toolsets=None,
                disabled_toolsets=profile_disabled,
                quiet_mode=True,
            )
        finally:
            _clear_tool_defs_cache()
            invalidate_check_fn_cache()

    available = {
        str(schema.get("function", {}).get("name"))
        for schema in (schemas or [])
        if isinstance(schema, dict) and schema.get("function", {}).get("name")
    }
    return [name for name in requested if name in available]


def _validate_effective_scope_runtime_tools(
    effective_toolsets: list[str],
    *,
    declared_allowed_tools: Optional[list[str]] = None,
) -> list[str]:
    """Fail closed when a scoped worker would lack lifecycle Kanban tools."""
    resolved = [str(name).strip() for name in (effective_toolsets or []) if str(name).strip()]
    if not resolved:
        return ["runtime tool schema resolved empty after scope_contract.allowed_tools validation"]

    resolved_set = set(resolved)
    declared = [str(name).strip() for name in (declared_allowed_tools or []) if str(name).strip()]
    missing_declared = [name for name in declared if name not in resolved_set]
    if missing_declared:
        return [
            "runtime tool schema missing declared allowed tools: "
            + ", ".join(missing_declared)
        ]

    missing = [name for name in _REQUIRED_SCOPE_LIFECYCLE_TOOLS if name not in resolved_set]
    if missing:
        return [
            "runtime tool schema missing required terminal Kanban lifecycle tools: "
            + ", ".join(missing)
        ]
    return []


def _task_requires_dispatcher_scope_preflight(body: Optional[str]) -> bool:
    if _body_requires_scope_attestation(body):
        return True
    return any("scope_contract" in mapping for mapping in _iter_task_policy_mappings(body))


def _target_profile_home(profile: Optional[str]) -> Optional[str]:
    """Return the HERMES_HOME a spawned worker will use for ``profile``.

    The dispatcher runs under its own profile, but the worker will activate
    ``task.assignee``. Force-skill preflight must therefore resolve against the
    target profile home, not the dispatcher's currently imported skill context.
    """
    profile_name = str(profile or "").strip()
    if not profile_name:
        return None
    try:
        from hermes_cli.profiles import normalize_profile_name, resolve_profile_env

        return resolve_profile_env(normalize_profile_name(profile_name))
    except FileNotFoundError:
        return None
    except Exception:
        return None


def _skill_available_in_home(skill_name: str, hermes_home: str) -> bool:
    """Return True when ``skill_name`` has a SKILL.md under ``hermes_home``."""
    name = str(skill_name or "").strip()
    if not name:
        return False
    skills_root = Path(hermes_home) / "skills"
    if not skills_root.is_dir():
        return False

    try:
        direct = (skills_root / name / "SKILL.md").resolve()
        skills_root_resolved = skills_root.resolve()
        if direct.is_file() and skills_root_resolved in direct.parents:
            return True
    except (OSError, RuntimeError):
        pass

    try:
        for skill_md in skills_root.rglob("SKILL.md"):
            if skill_md.is_file() and skill_md.parent.name == name:
                return True
    except OSError:
        pass
    return False


def _validate_task_extra_skills(
    skills: Optional[list],
    *,
    profile: Optional[str] = None,
) -> list[str]:
    """Return force-loaded skills the target worker profile cannot resolve.

    This preflight prevents crash loops where the spawned process exits with
    ``Unknown skill(s): ...`` before it can block its own task. When ``profile``
    is provided, resolve against the profile home that ``hermes -p <profile>``
    will use; falling back to the dispatcher's current skill context would miss
    coordinator-only skills assigned to coder/reviewer workers.
    """
    requested = [str(s).strip() for s in (skills or []) if str(s).strip()]
    requested = [s for s in requested if s != "kanban-worker"]
    if not requested:
        return []

    if profile is not None:
        target_home = _target_profile_home(profile)
        if not target_home:
            # Fail closed: if the dispatcher cannot determine the target
            # profile's skill root, it cannot prove the worker CLI will start.
            return list(requested)
        return [s for s in requested if not _skill_available_in_home(s, target_home)]

    try:
        from agent.skill_commands import build_preloaded_skills_prompt
        _prompt, _loaded, missing = build_preloaded_skills_prompt(requested)
        return [str(s) for s in (missing or [])]
    except Exception as exc:
        return [f"skill preflight failed: {exc}"]


def _block_dispatch_preflight(
    conn: sqlite3.Connection,
    task_id: str,
    reason: str,
    *,
    kind: str = "dispatch_preflight_blocked",
    evidence: Optional[dict[str, Any]] = None,
) -> bool:
    now = int(time.time())
    with write_txn(conn):
        cur = conn.execute(
            "UPDATE tasks SET status = 'blocked', result = ?, completed_at = NULL, "
            "claim_lock = NULL, claim_expires = NULL, worker_pid = NULL, "
            "last_failure_error = ? WHERE id = ? AND status = 'ready'",
            (reason[:500], reason[:500], task_id),
        )
        if cur.rowcount != 1:
            return False
        payload = {"reason": reason[:500], "blocked_at": now, "task_id": task_id}
        if isinstance(evidence, dict):
            payload.update(evidence)
        _append_event(
            conn,
            task_id,
            kind,
            payload,
        )
        return True


DEFAULT_ITERATION_BUDGET_CONTINUATION_LIMIT = 1
_ITERATION_BUDGET_BLOCK_TYPE = "iteration_budget_exhausted"
_ITERATION_BUDGET_GUARD_REASON_RE = re.compile(
    r"^\s*iteration budget exhausted\s*\(\d+\s*/\s*\d+\)\s*[—-]\s*"
    r"task could not complete within the allowed iterations\s*$",
    re.IGNORECASE,
)
_ITERATION_BUDGET_AUTO_CONTINUE_EVENT = "dispatch_auto_continued_iteration_budget"
_ITERATION_BUDGET_CONTINUATION_CAPPED_EVENT = "dispatch_iteration_budget_continuation_capped"
_ITERATION_BUDGET_COMMENT_AUTHOR = "kanban-dispatcher"
_REVIEW_REQUIRED_BLOCK_RE = re.compile(r"^\s*review-required:\s*", re.IGNORECASE)
_REVIEW_REQUIRED_HANDOFF_EVENT = "dispatch_review_required_handoff_created"
_REVIEW_REQUIRED_COMMENT_AUTHOR = "kanban-dispatcher"
_REVIEW_REQUIRED_REVIEWER_ALLOWED_TOOLS = [
    "skill_view",
    "kanban_show",
    "read_file",
    "search_files",
    "kanban_run_workspace_command",
    "kanban_comment",
    "kanban_complete",
    "kanban_block",
]
_AUTO_REVIEWER_CHILD_EVENT = "dispatch_auto_reviewer_child_created"
_AUTO_REVIEWER_CHILD_SUPPRESSED_EVENT = "dispatch_auto_reviewer_child_suppressed"
_AUTO_REVIEWER_COMMENT_AUTHOR = "kanban-dispatcher"
_AUTO_REVIEWER_MAX_RUNTIME_SECONDS = 12 * 60
_AUTO_REVIEWER_MAX_RETRIES = 1
_SUPERSEDED_NOOP_OUTCOME = "superseded_noop"
_SUPERSEDED_NOOP_EVENT = "superseded_noop_terminalized"
_COORDINATOR_FINALIZATION_REASON = "coordinator_finalization_existing_approved_reviewer"


def _is_iteration_budget_guard_reason(summary: Optional[str]) -> bool:
    if not summary:
        return False
    return bool(_ITERATION_BUDGET_GUARD_REASON_RE.fullmatch(str(summary)))


def _normalize_block_type_marker(block_type: Optional[str]) -> Optional[str]:
    """Normalize optional machine guard markers for blocked payloads."""
    if block_type is None:
        return None
    normalized = str(block_type).strip().lower()
    if not normalized:
        return None
    return normalized


def _is_iteration_budget_block_type(block_type: Optional[str]) -> bool:
    """Return True only for the exact dispatcher-owned budget marker."""
    return _normalize_block_type_marker(block_type) == _ITERATION_BUDGET_BLOCK_TYPE


def _is_iteration_budget_exhausted(
    summary: Optional[str], blocked_payload: Optional[dict[str, Any]]
) -> bool:
    # Auto-resume must require both the canonical full reason and the exact
    # machine marker.  This intentionally rejects substring neighbors such as
    # "... allowed iterations; awaiting policy approval" even if they mention
    # the budget-guard text.
    if not _is_iteration_budget_guard_reason(summary):
        return False
    if not isinstance(blocked_payload, dict):
        return False
    return _is_iteration_budget_block_type(blocked_payload.get("block_type"))


def _is_review_required_block(summary: Optional[str]) -> bool:
    if not summary:
        return False
    return bool(_REVIEW_REQUIRED_BLOCK_RE.match(str(summary)))


def _blocked_event_payload_for_run(
    conn: sqlite3.Connection,
    task_id: str,
    run_id: int,
) -> dict[str, Any]:
    row = conn.execute(
        """
        SELECT payload FROM task_events
         WHERE task_id = ? AND kind = 'blocked' AND run_id = ?
         ORDER BY id DESC LIMIT 1
        """,
        (task_id, int(run_id)),
    ).fetchone()
    if not row or not row["payload"]:
        return {}
    try:
        payload = json.loads(row["payload"])
    except Exception:
        return {}
    return payload if isinstance(payload, dict) else {}


def _legacy_parent_gated_reviewer_children(conn: sqlite3.Connection, task_id: str) -> list[str]:
    rows = conn.execute(
        """
        SELECT t.id
          FROM task_links l
          JOIN tasks t ON t.id = l.child_id
         WHERE l.parent_id = ? AND LOWER(COALESCE(t.assignee, '')) = 'reviewer'
         ORDER BY t.created_at ASC, t.id ASC
        """,
        (task_id,),
    ).fetchall()
    return [str(r["id"]) for r in rows]


def _completion_metadata_suppresses_auto_review(
    metadata: Optional[dict],
) -> tuple[bool, Optional[str], Optional[str]]:
    """Return whether structured Coordinator/Admin metadata suppresses Reviewer-B.

    Suppression is intentionally metadata-only. Prose in summaries/results is
    ignored so a worker cannot accidentally suppress required review by wording
    its handoff like an approval.
    """
    if not isinstance(metadata, dict):
        return False, None, None
    approved_reviewer = str(metadata.get("approved_reviewer_task") or "").strip()
    if not approved_reviewer:
        return False, None, None
    required_markers = (
        metadata.get("review_finalized_by_coordinator") is True,
        metadata.get("suppress_auto_reviewer_b") is True,
        metadata.get("reviewer_redispatch_forbidden") is True,
        str(metadata.get("lifecycle_finalization") or "").strip()
        == "coordinator_admin_finalization",
    )
    if not all(required_markers):
        return False, None, approved_reviewer
    return True, _COORDINATOR_FINALIZATION_REASON, approved_reviewer


def _is_superseded_noop_task(conn: sqlite3.Connection, task_id: str) -> bool:
    run = latest_run(conn, task_id)
    return bool(
        run
        and run.outcome == "completed"
        and isinstance(run.metadata, dict)
        and run.metadata.get("lifecycle_outcome") == _SUPERSEDED_NOOP_OUTCOME
    )


def terminalize_superseded_noop(
    conn: sqlite3.Connection,
    task_id: str,
    *,
    reason: str,
    superseded_by: Optional[str] = None,
    expected_assignee: Optional[str] = "reviewer",
) -> dict[str, Any]:
    """Mark an inert artifact as terminal ``done`` with superseded/noop metadata.

    This is deliberately narrower than a new task status: it only terminalizes
    non-running artifact rows and records the semantic outcome in run metadata.
    """
    safe_reason = str(reason or "superseded noop").strip() or "superseded noop"
    superseding_task = str(superseded_by).strip() if superseded_by else None
    if _is_superseded_noop_task(conn, task_id):
        return {
            "ok": True,
            "task_id": task_id,
            "idempotent": True,
            "lifecycle_outcome": _SUPERSEDED_NOOP_OUTCOME,
            "superseded_by": superseding_task,
        }

    with write_txn(conn):
        row = conn.execute(
            "SELECT status, assignee FROM tasks WHERE id = ?",
            (task_id,),
        ).fetchone()
        if not row:
            return {"ok": False, "task_id": task_id, "error": "task_not_found"}
        previous_status = str(row["status"] or "")
        assignee = str(row["assignee"] or "").strip().lower()
        if expected_assignee is not None and assignee != expected_assignee.lower():
            return {
                "ok": False,
                "task_id": task_id,
                "error": "unexpected_assignee",
                "assignee": assignee,
            }
        if previous_status not in {"triage", "todo", "ready", "blocked"}:
            return {
                "ok": False,
                "task_id": task_id,
                "error": "unsupported_status",
                "previous_status": previous_status,
            }

        now = int(time.time())
        result = f"superseded/noop: {safe_reason}"
        metadata = {
            "lifecycle_outcome": _SUPERSEDED_NOOP_OUTCOME,
            "noop_reason": safe_reason,
        }
        if superseding_task:
            metadata["superseded_by"] = superseding_task
        conn.execute(
            """
            UPDATE tasks
               SET status       = 'done',
                   result       = ?,
                   completed_at = ?,
                   claim_lock   = NULL,
                   claim_expires= NULL,
                   worker_pid   = NULL
             WHERE id = ?
               AND status IN ('triage', 'todo', 'ready', 'blocked')
            """,
            (result, now, task_id),
        )
        run_id = _end_run(
            conn,
            task_id,
            outcome="completed",
            status="done",
            summary=result,
            metadata=metadata,
        )
        if run_id is None:
            run_id = _synthesize_ended_run(
                conn,
                task_id,
                outcome="completed",
                summary=result,
                metadata=metadata,
            )
        _append_event(
            conn,
            task_id,
            _SUPERSEDED_NOOP_EVENT,
            {
                "reason": safe_reason,
                "superseded_by": superseding_task,
                "previous_status": previous_status,
                "lifecycle_outcome": _SUPERSEDED_NOOP_OUTCOME,
            },
            run_id=run_id,
        )
    return {
        "ok": True,
        "task_id": task_id,
        "idempotent": False,
        "lifecycle_outcome": _SUPERSEDED_NOOP_OUTCOME,
        "superseded_by": superseding_task,
    }


def _terminalize_legacy_reviewer_children_for_finalization(
    conn: sqlite3.Connection,
    source_task_id: str,
    metadata: Optional[dict],
) -> list[str]:
    suppresses, reason, approved_reviewer = _completion_metadata_suppresses_auto_review(metadata)
    if not suppresses:
        return []
    terminalized: list[str] = []
    for child_id in _legacy_parent_gated_reviewer_children(conn, source_task_id):
        result = terminalize_superseded_noop(
            conn,
            child_id,
            reason=reason or _COORDINATOR_FINALIZATION_REASON,
            superseded_by=approved_reviewer,
            expected_assignee="reviewer",
        )
        if result.get("ok"):
            terminalized.append(child_id)
    return terminalized


def _review_required_reviewer_body(
    source_task: Task,
    *,
    source_run_id: int,
    reason: str,
    context_comment_id: Optional[int],
    legacy_reviewer_children: list[str],
) -> str:
    allowed_paths: list[str] = []
    if source_task.workspace_path:
        allowed_paths.append(f"{source_task.workspace_path.rstrip('/')}/**")
    lines = [
        f"# Review blocked handoff for {source_task.id}",
        "",
        "Independent verdict-only Reviewer task created by the dispatcher because the source worker blocked with `review-required:`.",
        "This task intentionally has NO parent edge to the blocked source so review can proceed without a deadlock.",
        "",
        "review_handoff:",
        f"  source_task: {source_task.id}",
        f"  source_run_id: {int(source_run_id)}",
        f"  source_assignee: {source_task.assignee or ''}",
        f"  source_workspace_kind: {source_task.workspace_kind}",
    ]
    if source_task.workspace_path:
        lines.append(f"  source_workspace_path: {source_task.workspace_path}")
    if context_comment_id is not None:
        lines.append(f"  context_comment_id: {int(context_comment_id)}")
    if legacy_reviewer_children:
        lines.append("  legacy_parent_gated_reviewer_children:")
        lines.extend([f"    - {child_id}" for child_id in legacy_reviewer_children])
    lines.extend([
        "",
        "Review instructions:",
        f"- blocked reason: {reason}",
        "- Read the source task body, runs, comments, and the referenced context comment before deciding.",
        "- Produce a verdict-only terminal result: APPROVED or NEEDS_REVISION.",
        "- Required metadata keys: scope_contract_read=true, scope_contract_version=2, scope_attestation=true, forbidden_actions_taken=0, verdict, blocking_findings, required_verification, evidence_audited, residual_risk, effective_toolsets.",
        "- source remains blocked pending explicit Coordinator/Admin finalization; there is no auto-completion of the source task from this reviewer verdict.",
        "",
        "scope_contract:",
        "  version: 2",
        "  assignee: reviewer",
        "  allowed_systems:",
        "    - hermes-agent",
        "    - hermes-kanban",
        "  allowed_tools:",
    ])
    lines.extend([f"    - {tool}" for tool in _REVIEW_REQUIRED_REVIEWER_ALLOWED_TOOLS])
    if allowed_paths:
        lines.append("  allowed_paths:")
        lines.extend([f"    - {path}" for path in allowed_paths])
    lines.extend([
        "  forbidden_systems:",
        "    - OpenClaw",
        "    - Atlas",
        "    - Mission-Control",
        "    - Telegram",
        "completion_policy:",
        "  require_scope_attestation: true",
    ])
    return "\n".join(lines)


def _review_required_source_comment(
    *,
    reviewer_task_id: str,
    source_run_id: int,
    legacy_reviewer_children: list[str],
) -> str:
    lines = [
        "dispatcher review-required handoff created",
        "",
        f"- reviewer_task_id: {reviewer_task_id}",
        f"- source_run_id: {int(source_run_id)}",
        "- source remains blocked pending explicit Coordinator/Admin finalization after reviewer verdict",
        "- no auto-completion of the blocked source task",
    ]
    if legacy_reviewer_children:
        lines.append(
            "- legacy parent-gated reviewer children left inert: "
            + ", ".join(legacy_reviewer_children)
        )
    return "\n".join(lines)


def _ensure_comment_once(
    conn: sqlite3.Connection,
    task_id: str,
    *,
    author: str,
    body: str,
) -> int:
    existing = conn.execute(
        """
        SELECT id FROM task_comments
         WHERE task_id = ? AND author = ? AND body = ?
         ORDER BY id DESC LIMIT 1
        """,
        (task_id, author, body.strip()),
    ).fetchone()
    if existing:
        return int(existing["id"])
    return add_comment(conn, task_id, author, body)


def _dispatch_review_required_handoffs(conn: sqlite3.Connection) -> list[str]:
    blocked_rows = conn.execute(
        "SELECT id FROM tasks WHERE status = 'blocked' ORDER BY priority DESC, created_at ASC"
    ).fetchall()
    created_or_promoted: list[str] = []
    for row in blocked_rows:
        source_task = get_task(conn, row["id"])
        if source_task is None:
            continue
        if str(source_task.assignee or "").strip().lower() == "reviewer":
            continue
        run = latest_run(conn, source_task.id)
        if run is None or run.outcome != "blocked" or run.ended_at is None:
            continue
        if not _is_review_required_block(run.summary):
            continue
        if _has_task_event_for_run(conn, source_task.id, _REVIEW_REQUIRED_HANDOFF_EVENT, run.id):
            continue

        blocked_payload = _blocked_event_payload_for_run(conn, source_task.id, run.id)
        context_comment_id = _metadata_int(blocked_payload, "context_comment_id")
        legacy_reviewer_children = _legacy_parent_gated_reviewer_children(conn, source_task.id)
        reviewer_body = _review_required_reviewer_body(
            source_task,
            source_run_id=run.id,
            reason=(run.summary or "review-required").strip(),
            context_comment_id=context_comment_id,
            legacy_reviewer_children=legacy_reviewer_children,
        )
        reviewer_task_id = create_task(
            conn,
            title=f"Review blocked handoff for {source_task.id}: {source_task.title}",
            body=reviewer_body,
            assignee="reviewer",
            created_by="dispatcher",
            workspace_kind=source_task.workspace_kind,
            workspace_path=source_task.workspace_path,
            priority=source_task.priority,
            idempotency_key=f"review-required:{source_task.id}:{run.id}",
        )
        comment_body = _review_required_source_comment(
            reviewer_task_id=reviewer_task_id,
            source_run_id=run.id,
            legacy_reviewer_children=legacy_reviewer_children,
        )
        _ensure_comment_once(
            conn,
            source_task.id,
            author=_REVIEW_REQUIRED_COMMENT_AUTHOR,
            body=comment_body,
        )
        with write_txn(conn):
            if _has_task_event_for_run(conn, source_task.id, _REVIEW_REQUIRED_HANDOFF_EVENT, run.id):
                continue
            _append_event(
                conn,
                source_task.id,
                _REVIEW_REQUIRED_HANDOFF_EVENT,
                {
                    "source_task": source_task.id,
                    "source_run_id": run.id,
                    "reviewer_task_id": reviewer_task_id,
                    "context_comment_id": context_comment_id,
                    "blocked_source_parent_edge": False,
                    "legacy_reviewer_children": legacy_reviewer_children,
                },
                run_id=run.id,
            )
        created_or_promoted.append(reviewer_task_id)
    return created_or_promoted


def _auto_reviewer_child_body(
    source_task: Task,
    *,
    source_run_id: int,
    lane: str,
    stage: str,
    completion_summary: str,
) -> str:
    lines = [
        f"# Review {source_task.id}: {source_task.title}",
        "",
        "Auto-created reviewer child after coder completion.",
        "",
        f"parent_task: {source_task.id}",
        f"parent_run: {int(source_run_id)}",
        f"review_lane: {lane}",
        f"review_stage: {stage}",
        f"completion_summary: {completion_summary}",
        "",
        "Scope:",
        "- Deliver verdict-only review feedback for the parent task output.",
        "- Do not mutate forbidden systems.",
    ]
    return "\n".join(lines)


def _dispatch_standard_review_children(conn: sqlite3.Connection) -> list[str]:
    """Create STANDARD_REVIEW reviewer-b children after coder completion.

    Scope intentionally excludes CRITICAL_REVIEW pre-coder Reviewer-A flow.
    """
    done_coder_rows = conn.execute(
        """
        SELECT id FROM tasks
         WHERE status = 'done' AND LOWER(COALESCE(assignee, '')) = 'coder'
         ORDER BY completed_at ASC, id ASC
        """
    ).fetchall()

    created_children: list[str] = []
    for row in done_coder_rows:
        source_task = get_task(conn, row["id"])
        if source_task is None:
            continue
        run = latest_run(conn, source_task.id)
        if run is None or run.outcome != "completed" or run.ended_at is None:
            continue
        if _has_task_event_for_run(conn, source_task.id, _AUTO_REVIEWER_CHILD_EVENT, run.id):
            continue
        if _has_task_event_for_run(conn, source_task.id, _AUTO_REVIEWER_CHILD_SUPPRESSED_EVENT, run.id):
            continue

        suppresses_review, suppression_reason, approved_reviewer_task = (
            _completion_metadata_suppresses_auto_review(run.metadata)
        )
        if suppresses_review:
            with write_txn(conn):
                if _has_task_event_for_run(conn, source_task.id, _AUTO_REVIEWER_CHILD_EVENT, run.id):
                    continue
                if _has_task_event_for_run(conn, source_task.id, _AUTO_REVIEWER_CHILD_SUPPRESSED_EVENT, run.id):
                    continue
                _append_event(
                    conn,
                    source_task.id,
                    _AUTO_REVIEWER_CHILD_SUPPRESSED_EVENT,
                    {
                        "source_task": source_task.id,
                        "source_run_id": run.id,
                        "reason": suppression_reason,
                        "approved_reviewer_task": approved_reviewer_task,
                        "review_lane": "STANDARD_REVIEW",
                        "review_stage": "reviewer_b",
                    },
                    run_id=run.id,
                )
            continue

        changed_paths: list[str] = []
        if isinstance(run.metadata, dict):
            raw_changed = run.metadata.get("changed_files")
            if isinstance(raw_changed, list):
                changed_paths = [str(p) for p in raw_changed if str(p).strip()]

        lane_result = classify_kanban_review_lane(
            title=source_task.title,
            body=source_task.body,
            changed_paths=changed_paths,
        )
        lane = str(lane_result.get("lane") or "").strip().upper()
        if lane != "STANDARD_REVIEW":
            continue

        manual_reviewer_children = _legacy_parent_gated_reviewer_children(conn, source_task.id)
        manual_pipeline = _explicit_manual_review_pipeline(source_task.body)
        if manual_reviewer_children or manual_pipeline:
            suppression_reason = (
                "manual_reviewer_child_present"
                if manual_reviewer_children
                else "manual_review_pipeline_opt_out"
            )
            with write_txn(conn):
                if _has_task_event_for_run(conn, source_task.id, _AUTO_REVIEWER_CHILD_EVENT, run.id):
                    continue
                if _has_task_event_for_run(conn, source_task.id, _AUTO_REVIEWER_CHILD_SUPPRESSED_EVENT, run.id):
                    continue
                _append_event(
                    conn,
                    source_task.id,
                    _AUTO_REVIEWER_CHILD_SUPPRESSED_EVENT,
                    {
                        "source_task": source_task.id,
                        "source_run_id": run.id,
                        "reason": suppression_reason,
                        "manual_reviewer_children": manual_reviewer_children,
                        "review_lane": "STANDARD_REVIEW",
                        "review_stage": "reviewer_b",
                    },
                    run_id=run.id,
                )
            continue

        completion_summary = (run.summary or source_task.result or "").strip()
        if not completion_summary:
            completion_summary = "(no completion summary provided)"
        completion_summary = completion_summary.splitlines()[0][:300]

        reviewer_title = f"Review {source_task.id}: {source_task.title}".strip()
        if len(reviewer_title) > 80:
            reviewer_title = reviewer_title[:80]

        reviewer_task_id = create_task(
            conn,
            title=reviewer_title,
            body=_auto_reviewer_child_body(
                source_task,
                source_run_id=run.id,
                lane="STANDARD_REVIEW",
                stage="reviewer_b",
                completion_summary=completion_summary,
            ),
            assignee="reviewer",
            created_by="dispatcher",
            workspace_kind=source_task.workspace_kind,
            workspace_path=source_task.workspace_path,
            priority=source_task.priority,
            parents=[source_task.id],
            idempotency_key=f"auto-reviewer:{source_task.id}:{run.id}",
            skills=["kanban-reviewer"],
            max_runtime_seconds=_AUTO_REVIEWER_MAX_RUNTIME_SECONDS,
            max_retries=_AUTO_REVIEWER_MAX_RETRIES,
        )

        with write_txn(conn):
            if _has_task_event_for_run(conn, source_task.id, _AUTO_REVIEWER_CHILD_EVENT, run.id):
                continue
            _append_event(
                conn,
                source_task.id,
                _AUTO_REVIEWER_CHILD_EVENT,
                {
                    "source_task": source_task.id,
                    "source_run_id": run.id,
                    "reviewer_task_id": reviewer_task_id,
                    "review_lane": "STANDARD_REVIEW",
                    "review_stage": "reviewer_b",
                },
                run_id=run.id,
            )
        created_children.append(reviewer_task_id)
    return created_children


def _task_has_undone_parents(conn: sqlite3.Connection, task_id: str) -> bool:
    return bool(
        conn.execute(
            "SELECT 1 FROM task_links l "
            "JOIN tasks p ON p.id = l.parent_id "
            "WHERE l.child_id = ? AND p.status != 'done' LIMIT 1",
            (task_id,),
        ).fetchone()
    )


def _has_task_event_for_run(
    conn: sqlite3.Connection,
    task_id: str,
    kind: str,
    run_id: Optional[int],
) -> bool:
    if run_id is None:
        return False
    return bool(
        conn.execute(
            "SELECT 1 FROM task_events WHERE task_id = ? AND kind = ? AND run_id = ? LIMIT 1",
            (task_id, kind, int(run_id)),
        ).fetchone()
    )


def _count_task_events(conn: sqlite3.Connection, task_id: str, kind: str) -> int:
    row = conn.execute(
        "SELECT COUNT(*) AS n FROM task_events WHERE task_id = ? AND kind = ?",
        (task_id, kind),
    ).fetchone()
    return int(row["n"] or 0) if row else 0


def _iteration_budget_continue_comment(run: Run, *, continuation_index: int, continuation_limit: int) -> str:
    summary = (run.summary or "Iteration budget exhausted").strip()
    return (
        "dispatcher auto-continuation checkpoint\n\n"
        f"- prior run id: {run.id}\n"
        f"- worker summary: {summary}\n"
        f"- continuation attempt: {continuation_index}/{continuation_limit}\n"
        "- next worker instruction: Resume from the previous worker checkpoint, read prior comments/runs before acting, and continue only the remaining work. If the iteration budget exhausts again, block with a concise Coordinator/Human handoff instead of restarting from scratch."
    )


def _iteration_budget_cap_comment(run: Run, *, continuation_limit: int) -> str:
    summary = (run.summary or "Iteration budget exhausted").strip()
    return (
        "dispatcher continuation cap reached\n\n"
        f"- prior run id: {run.id}\n"
        f"- worker summary: {summary}\n"
        f"- continuation limit: {continuation_limit}\n"
        "- next action: Coordinator/Human review required. Decide whether to raise the worker's iteration budget, split the task into smaller cards, or provide a tighter handoff before another retry."
    )


def _auto_continue_iteration_budget_blocks(
    conn: sqlite3.Connection,
    *,
    continuation_limit: int = DEFAULT_ITERATION_BUDGET_CONTINUATION_LIMIT,
) -> tuple[list[str], list[str]]:
    """Reopen eligible iteration-budget blocks exactly once by default."""
    if continuation_limit <= 0:
        return ([], [])

    blocked_rows = conn.execute(
        "SELECT id FROM tasks WHERE status = 'blocked' ORDER BY priority DESC, created_at ASC"
    ).fetchall()
    continued: list[str] = []
    capped: list[str] = []
    for row in blocked_rows:
        task_id = row["id"]
        run = latest_run(conn, task_id)
        if run is None or run.outcome != "blocked" or run.ended_at is None:
            continue
        blocked_payload = _blocked_event_payload_for_run(conn, task_id, run.id)
        if not _is_iteration_budget_exhausted(run.summary, blocked_payload):
            continue
        if _task_has_undone_parents(conn, task_id):
            continue

        prior_continuations = _count_task_events(
            conn, task_id, _ITERATION_BUDGET_AUTO_CONTINUE_EVENT
        )
        if prior_continuations >= continuation_limit:
            if _has_task_event_for_run(
                conn,
                task_id,
                _ITERATION_BUDGET_CONTINUATION_CAPPED_EVENT,
                run.id,
            ):
                continue
            comment_id = add_comment(
                conn,
                task_id,
                _ITERATION_BUDGET_COMMENT_AUTHOR,
                _iteration_budget_cap_comment(
                    run,
                    continuation_limit=continuation_limit,
                ),
            )
            _append_event(
                conn,
                task_id,
                _ITERATION_BUDGET_CONTINUATION_CAPPED_EVENT,
                {
                    "previous_run_id": run.id,
                    "continuation_limit": continuation_limit,
                    "checkpoint_comment_id": int(comment_id),
                    "summary": (run.summary or "")[:400] or None,
                    "gate_reason": "Coordinator/Human review required",
                },
                run_id=run.id,
            )
            capped.append(task_id)
            continue

        if _has_task_event_for_run(conn, task_id, _ITERATION_BUDGET_AUTO_CONTINUE_EVENT, run.id):
            continue
        comment_id = add_comment(
            conn,
            task_id,
            _ITERATION_BUDGET_COMMENT_AUTHOR,
            _iteration_budget_continue_comment(
                run,
                continuation_index=prior_continuations + 1,
                continuation_limit=continuation_limit,
            ),
        )
        _append_event(
            conn,
            task_id,
            _ITERATION_BUDGET_AUTO_CONTINUE_EVENT,
            {
                "previous_run_id": run.id,
                "continuation_index": prior_continuations + 1,
                "continuation_limit": continuation_limit,
                "checkpoint_comment_id": int(comment_id),
                "summary": (run.summary or "")[:400] or None,
            },
            run_id=run.id,
        )
        if unblock_task(conn, task_id):
            continued.append(task_id)
    return (continued, capped)


# Bounded registry of recently-reaped worker child exits, populated by the
# reap loop at the top of ``dispatch_once`` and consulted by
# ``detect_crashed_workers`` to classify a dead-pid task.
#
# Entry: ``pid -> (raw_wait_status, reaped_at_epoch)``. We keep raw status
# so both ``os.WIFEXITED`` / ``os.WEXITSTATUS`` and ``os.WIFSIGNALED`` can
# be consulted. Entries are trimmed by age (and total size cap as a
# belt-and-braces against unbounded growth on exotic platforms).
_RECENT_WORKER_EXIT_TTL_SECONDS = 600
_RECENT_WORKER_EXITS_MAX = 4096
_recent_worker_exits: "dict[int, tuple[int, float]]" = {}


def _protocol_state_for_run(row: sqlite3.Row) -> str:
    outcome = row["outcome"] if "outcome" in row.keys() else None
    status = row["status"] if "status" in row.keys() else None
    if outcome == "completed" or status in {"done", "completed"}:
        return "complete_emitted"
    if outcome == "blocked" or status == "blocked":
        return "block_emitted"
    if status == "running" and row["ended_at"] is None:
        return "silent"
    return "silent"


def _exit_taxonomy_from_wait_status(
    raw_status: int,
    protocol_state: str,
) -> tuple[str, Optional[int]]:
    try:
        if os.WIFEXITED(raw_status):
            code = os.WEXITSTATUS(raw_status)
            if code == 0:
                if protocol_state in {"complete_emitted", "block_emitted"}:
                    return ("clean_exit_complete", 0)
                return ("clean_exit_protocol_violation", 0)
            return ("nonzero_exit", code)
        if os.WIFSIGNALED(raw_status):
            return ("signaled", os.WTERMSIG(raw_status))
    except Exception:
        pass
    return ("pending", None)


def _worker_exit_error_text(kind: str, pid: int, code: Optional[int]) -> Optional[str]:
    if kind == "clean_exit_protocol_violation":
        return (
            "worker exited cleanly (rc=0) without calling "
            "kanban_complete or kanban_block — protocol violation"
        )
    if kind == "nonzero_exit":
        return f"pid {pid} exited with code {code}"
    if kind == "signaled":
        return f"pid {pid} killed by signal {code}"
    if kind == "pid_not_alive":
        return f"pid {pid} not alive"
    return None


def _record_worker_exit(
    pid: int,
    raw_status: int,
    conn: Optional[sqlite3.Connection] = None,
) -> None:
    """Record a reaped child's exit status for later classification.

    Called from the reap loop in ``dispatch_once``. Safe to call many
    times; duplicate pids overwrite (pids can cycle, latest wins). When
    a DB connection is supplied, also persists the structured exit
    taxonomy on the matching task_runs row at reap time.
    """
    if not pid or pid <= 0:
        return
    now = time.time()
    _recent_worker_exits[int(pid)] = (int(raw_status), now)
    if conn is not None:
        try:
            row = conn.execute(
                """
                SELECT id, status, outcome, ended_at
                  FROM task_runs
                 WHERE worker_pid = ?
                   AND COALESCE(worker_exit_kind, 'pending') = 'pending'
                 ORDER BY id DESC
                 LIMIT 1
                """,
                (int(pid),),
            ).fetchone()
            if row is not None:
                protocol_state = _protocol_state_for_run(row)
                exit_kind, exit_code = _exit_taxonomy_from_wait_status(
                    int(raw_status), protocol_state
                )
                error_text = _worker_exit_error_text(exit_kind, int(pid), exit_code)
                fingerprint = (
                    _error_fingerprint(error_text) if error_text else None
                )
                with write_txn(conn):
                    conn.execute(
                        """
                        UPDATE task_runs
                           SET worker_exit_kind = ?,
                               worker_exit_code = ?,
                               worker_protocol_state = ?,
                               worker_failure_fingerprint = ?
                         WHERE id = ?
                           AND COALESCE(worker_exit_kind, 'pending') = 'pending'
                        """,
                        (
                            exit_kind,
                            exit_code,
                            protocol_state,
                            fingerprint,
                            int(row["id"]),
                        ),
                    )
        except Exception:
            _log.warning("worker exit taxonomy persist failed", exc_info=True)
    # Age-based trim: drop entries older than the TTL.
    if len(_recent_worker_exits) > _RECENT_WORKER_EXITS_MAX // 2:
        cutoff = now - _RECENT_WORKER_EXIT_TTL_SECONDS
        for _pid in [p for p, (_s, t) in _recent_worker_exits.items() if t < cutoff]:
            _recent_worker_exits.pop(_pid, None)
    # Size cap as a final guard.
    if len(_recent_worker_exits) > _RECENT_WORKER_EXITS_MAX:
        # Drop oldest half.
        ordered = sorted(_recent_worker_exits.items(), key=lambda kv: kv[1][1])
        for _pid, _ in ordered[: len(ordered) // 2]:
            _recent_worker_exits.pop(_pid, None)


def _classify_worker_exit(pid: int) -> "tuple[str, Optional[int]]":
    """Classify a recently-reaped worker by pid.

    Returns ``(kind, code)`` where ``kind`` is one of:

    * ``"clean_exit"`` — ``WIFEXITED`` with ``WEXITSTATUS == 0``. When the
      task is still ``running`` in the DB, this is a protocol violation
      (worker exited without calling ``kanban_complete`` / ``kanban_block``)
      and should be auto-blocked immediately — retrying will just loop.
    * ``"nonzero_exit"`` — ``WIFEXITED`` with non-zero status. Real error.
    * ``"signaled"`` — ``WIFSIGNALED`` (OOM killer, SIGKILL, etc). Real crash.
    * ``"unknown"`` — pid was not in the reap registry (either reaped by
      something else, or died between reap tick and liveness check). Fall
      back to existing crashed-counter behavior.

    ``code`` is the exit status (for ``clean_exit`` / ``nonzero_exit``) or
    the signal number (for ``signaled``), or ``None`` for ``unknown``.
    """
    entry = _recent_worker_exits.get(int(pid))
    if entry is None:
        return ("unknown", None)
    raw, _ = entry
    try:
        if os.WIFEXITED(raw):
            code = os.WEXITSTATUS(raw)
            if code == 0:
                return ("clean_exit", 0)
            return ("nonzero_exit", code)
        if os.WIFSIGNALED(raw):
            return ("signaled", os.WTERMSIG(raw))
    except Exception:
        pass
    return ("unknown", None)


def _pid_alive(pid: Optional[int]) -> bool:
    """Return True if ``pid`` is still running on this host.

    Cross-platform: uses ``OpenProcess`` + ``WaitForSingleObject`` on
    Windows (via ``gateway.status._pid_exists``) and ``os.kill(pid, 0)``
    on POSIX. Returns False for falsy PIDs or on any OS error.

    **DO NOT** use ``os.kill(pid, 0)`` directly on Windows — Python's
    Windows ``os.kill`` treats ``sig=0`` as ``CTRL_C_EVENT`` (bpo-14484)
    and will broadcast it to the target's console group, potentially
    killing unrelated processes.

    **Zombie handling:** the existence check succeeds against zombie
    processes (post-exit, pre-reap) because the process table entry
    still exists. A worker that exits without being reaped by its
    parent would stay "alive" to the dispatcher forever. Dispatcher
    workers are started via ``start_new_session=True`` + intentional
    Popen handle abandonment, so init reaps them quickly — but during
    the window between exit and reap, we'd otherwise see stale "alive"
    signals. On Linux we peek at ``/proc/<pid>/status`` and treat
    ``State: Z`` as dead. On macOS we ask ``ps`` for the BSD ``stat``
    field and treat values containing ``Z`` as dead.
    """
    if not pid or pid <= 0:
        return False
    from gateway.status import _pid_exists
    if not _pid_exists(int(pid)):
        return False
    # Still here → process exists. Check for zombie on platforms
    # where we have a cheap, deterministic process-state probe.
    if sys.platform == "linux":
        try:
            with open(f"/proc/{int(pid)}/status", "r", encoding="utf-8") as f:
                for line in f:
                    if line.startswith("State:"):
                        # "State:\tZ (zombie)" → dead
                        if "Z" in line.split(":", 1)[1]:
                            return False
                        break
        except (FileNotFoundError, PermissionError, OSError):
            # proc entry gone → already reaped; treat as dead.
            # PermissionError shouldn't happen for our own children but
            # be defensive.
            pass
    elif sys.platform == "darwin":
        try:
            proc = subprocess.run(
                ["ps", "-o", "stat=", "-p", str(int(pid))],
                stdout=subprocess.PIPE,
                stderr=subprocess.DEVNULL,
                text=True,
                timeout=1,
                check=False,
            )
            if proc.returncode != 0:
                return False
            if "Z" in (proc.stdout or "").strip():
                return False
        except (OSError, subprocess.SubprocessError, TimeoutError):
            # If the secondary probe fails, keep the kill(0) answer.
            pass
    return True


def _terminate_reclaimed_worker(
    pid: Optional[int],
    claim_lock: Optional[str],
    *,
    signal_fn=None,
) -> dict[str, Any]:
    """Best-effort host-local worker termination for reclaim paths."""
    import signal

    info: dict[str, Any] = {
        "prev_pid": int(pid) if pid else None,
        "host_local": False,
        "termination_attempted": False,
        "terminated": False,
        "sigkill": False,
    }
    if not pid or pid <= 0 or not claim_lock:
        return info

    host_prefix = f"{_claimer_id().split(':', 1)[0]}:"
    if not str(claim_lock).startswith(host_prefix):
        return info
    info["host_local"] = True

    kill = signal_fn if signal_fn is not None else (
        os.kill if hasattr(os, "kill") else None
    )
    if kill is None:
        return info

    info["termination_attempted"] = True
    try:
        kill(int(pid), signal.SIGTERM)
    except (ProcessLookupError, OSError):
        return info

    for _ in range(10):
        if not _pid_alive(pid):
            info["terminated"] = True
            return info
        time.sleep(0.5)

    if _pid_alive(pid):
        try:
            # signal.SIGKILL doesn't exist on Windows; fall back to SIGTERM
            # (which maps to TerminateProcess via the stdlib shim).
            _sigkill = getattr(signal, "SIGKILL", signal.SIGTERM)
            kill(int(pid), _sigkill)
            info["sigkill"] = True
        except (ProcessLookupError, OSError):
            return info

    info["terminated"] = not _pid_alive(pid)
    return info


def heartbeat_worker(
    conn: sqlite3.Connection,
    task_id: str,
    *,
    note: Optional[str] = None,
    expected_run_id: Optional[int] = None,
) -> bool:
    """Record a ``heartbeat`` event + touch ``last_heartbeat_at``.

    Called by long-running workers as a liveness signal orthogonal to
    the PID check. A worker that forks a long-lived child (train loop,
    video encode, web crawl) can have its Python still alive while the
    actual work process is stuck; periodic heartbeats catch that.

    Returns True on success, False if the task is not in a state that
    should be heartbeating (not running, or claim expired).
    """
    now = int(time.time())
    with write_txn(conn):
        if expected_run_id is None:
            cur = conn.execute(
                "UPDATE tasks SET last_heartbeat_at = ? "
                "WHERE id = ? AND status = 'running'",
                (now, task_id),
            )
        else:
            cur = conn.execute(
                "UPDATE tasks SET last_heartbeat_at = ? "
                "WHERE id = ? AND status = 'running' AND current_run_id = ?",
                (now, task_id, int(expected_run_id)),
            )
        if cur.rowcount != 1:
            return False
        run_id = (
            int(expected_run_id)
            if expected_run_id is not None
            else _current_run_id(conn, task_id)
        )
        if run_id is not None:
            conn.execute(
                "UPDATE task_runs SET last_heartbeat_at = ? WHERE id = ?",
                (now, run_id),
            )
        _append_event(
            conn, task_id, "heartbeat",
            {"note": note} if note else None,
            run_id=run_id,
        )
    return True


def enforce_max_runtime(
    conn: sqlite3.Connection,
    *,
    signal_fn=None,
) -> list[str]:
    """Terminate workers whose per-task ``max_runtime_seconds`` has elapsed.

    Sends SIGTERM, waits a short grace window, then SIGKILL. Emits a
    ``timed_out`` event and drops the task back to ``ready`` so the next
    dispatcher tick re-spawns it — unless the spawn-failure circuit
    breaker has already given up, in which case the task stays blocked
    where ``_record_spawn_failure`` parked it.

    Runs host-local: only tasks claimed by this host are candidates
    (same reasoning as ``detect_crashed_workers``). ``signal_fn`` is a
    test hook; defaults to ``os.kill`` on POSIX.
    """
    import signal
    timed_out: list[str] = []
    now = int(time.time())
    host_prefix = f"{_claimer_id().split(':', 1)[0]}:"

    rows = conn.execute(
        "SELECT t.id, t.worker_pid, "
        "       COALESCE(r.started_at, t.started_at) AS active_started_at, "
        "       t.max_runtime_seconds, t.claim_lock "
        "FROM tasks t "
        "LEFT JOIN task_runs r ON r.id = t.current_run_id "
        "WHERE t.status = 'running' AND t.max_runtime_seconds IS NOT NULL "
        "  AND COALESCE(r.started_at, t.started_at) IS NOT NULL "
        "  AND t.worker_pid IS NOT NULL"
    ).fetchall()
    for row in rows:
        lock = row["claim_lock"] or ""
        if not lock.startswith(host_prefix):
            continue
        # Runtime is per attempt, not lifetime-of-task. ``tasks.started_at``
        # intentionally records the first time a task ever started, so retries
        # must be measured from the active task_runs row when present.
        elapsed = now - int(row["active_started_at"])
        if elapsed < int(row["max_runtime_seconds"]):
            continue

        pid = int(row["worker_pid"])
        tid = row["id"]
        # SIGTERM then SIGKILL. Keep it simple: 5 s grace. Workers that
        # want a cleaner shutdown can install their own SIGTERM handler
        # before the grace expires.
        killed = False
        kill = signal_fn if signal_fn is not None else (
            os.kill if hasattr(os, "kill") else None
        )
        if kill is not None:
            try:
                kill(pid, signal.SIGTERM)
            except (ProcessLookupError, OSError):
                pass
            # Short polling wait — no time.sleep on the write txn.
            for _ in range(10):
                if not _pid_alive(pid):
                    break
                time.sleep(0.5)
            if _pid_alive(pid):
                try:
                    # signal.SIGKILL doesn't exist on Windows.
                    _sigkill = getattr(signal, "SIGKILL", signal.SIGTERM)
                    kill(pid, _sigkill)
                    killed = True
                except (ProcessLookupError, OSError):
                    pass

        with write_txn(conn):
            cur = conn.execute(
                "UPDATE tasks SET status = 'ready', claim_lock = NULL, "
                "claim_expires = NULL, worker_pid = NULL, "
                "last_heartbeat_at = NULL "
                "WHERE id = ? AND status = 'running'",
                (tid,),
            )
            if cur.rowcount == 1:
                payload = {
                    "pid": pid,
                    "elapsed_seconds": int(elapsed),
                    "limit_seconds": int(row["max_runtime_seconds"]),
                    "sigkill": killed,
                }
                run_id = _end_run(
                    conn, tid,
                    outcome="timed_out", status="timed_out",
                    error=f"elapsed {int(elapsed)}s > limit {int(row['max_runtime_seconds'])}s",
                    metadata=payload,
                )
                timeout_payload = _terminal_event_payload(
                    conn,
                    tid,
                    run_id,
                    outcome="timed_out",
                    error=f"elapsed {int(elapsed)}s > limit {int(row['max_runtime_seconds'])}s",
                    extra=payload,
                )
                _append_event(
                    conn, tid, "timed_out", timeout_payload, run_id=run_id,
                )
                timed_out.append(tid)
        # Increment the unified failure counter. Outside the write_txn
        # above because ``_record_task_failure`` opens its own. If the
        # breaker trips, this flips the task ``ready → blocked`` and
        # emits a ``gave_up`` event on top of the ``timed_out`` we
        # already emitted.
        if cur.rowcount == 1:
            _record_task_failure(
                conn, tid,
                error=f"elapsed {int(elapsed)}s > limit {int(row['max_runtime_seconds'])}s",
                outcome="timed_out",
                release_claim=False,
                end_run=False,
                event_payload_extra={"pid": pid, "sigkill": killed},
            )
    return timed_out


# Dispatcher-side worker heartbeat cadence. Auto-heartbeats are emitted by
# the dispatcher process for spawned workers, so a long blocking tool call
# still updates liveness while the worker is busy.
WORKER_HEARTBEAT_SEC = 60
_STALE_HEARTBEAT_GAP_SECONDS = 2 * WORKER_HEARTBEAT_SEC


def _worker_heartbeat_interval_seconds() -> int:
    """Resolve the worker auto-heartbeat cadence from the process environment.

    Accepts two env-var names: the PlanSpec-documented rollback flag
    ``HERMES_WORKER_HEARTBEAT_SEC`` and the original implementation name
    ``HERMES_KANBAN_WORKER_HEARTBEAT_SECONDS``. The PlanSpec name takes
    precedence when both are set.

    Return value semantics: ``0`` disables the auto-heartbeat loop entirely
    (per the PlanSpec rollback contract); any positive int overrides the
    default interval; unset or unparseable values fall back to
    ``WORKER_HEARTBEAT_SEC``.
    """
    for env_name in (
        "HERMES_WORKER_HEARTBEAT_SEC",
        "HERMES_KANBAN_WORKER_HEARTBEAT_SECONDS",
    ):
        raw = os.environ.get(env_name, "").strip()
        if not raw:
            continue
        try:
            parsed = int(raw)
        except ValueError:
            return WORKER_HEARTBEAT_SEC
        if parsed < 0:
            return WORKER_HEARTBEAT_SEC
        return parsed
    return WORKER_HEARTBEAT_SEC


def detect_stale_running(
    conn: sqlite3.Connection,
    *,
    stale_timeout_seconds: int = 0,
    signal_fn=None,
) -> list[str]:
    """Reclaim ``running`` tasks that show no progress (heartbeat) within the
    staleness window.

    A task is considered stale when BOTH of these hold:

    1. It has been running for longer than ``stale_timeout_seconds``
       (measured from the active run's ``started_at``, falling back to
       ``tasks.started_at`` on older runs).
    2. Its ``last_heartbeat_at`` is older than
       ``_STALE_HEARTBEAT_GAP_SECONDS`` (or NULL — never sent a heartbeat).

    On reclaim the task is reset to ``ready``, the run is closed with
    ``outcome='stale'``, and the host-local worker (if still running) is
    terminated.

    Only considers ``status='running'`` tasks. Blocked tasks are never
    candidates.  Returns the list of reclaimed task IDs.

    ``stale_timeout_seconds=0`` disables the check entirely (returns ``[]``
    immediately).  ``signal_fn`` is a test hook; defaults to ``os.kill``
    on POSIX.
    """
    if stale_timeout_seconds <= 0:
        return []

    import signal as _signal_mod

    now = int(time.time())
    host_prefix = f"{_claimer_id().split(':', 1)[0]}:"
    reclaimed: list[str] = []

    rows = conn.execute(
        "SELECT t.id, t.worker_pid, t.last_heartbeat_at, t.claim_lock, "
        "       COALESCE(r.started_at, t.started_at) AS active_started_at "
        "FROM tasks t "
        "LEFT JOIN task_runs r ON r.id = t.current_run_id "
        "WHERE t.status = 'running'"
    ).fetchall()

    for row in rows:
        # Skip if no started_at (shouldn't happen for running, but be safe).
        if row["active_started_at"] is None:
            continue

        elapsed = now - int(row["active_started_at"])
        if elapsed < stale_timeout_seconds:
            continue  # not old enough to check

        last_hb = row["last_heartbeat_at"]
        hb_age = (now - int(last_hb)) if last_hb is not None else None
        if hb_age is not None and hb_age < _STALE_HEARTBEAT_GAP_SECONDS:
            run_id = _current_run_id(conn, row["id"])
            if run_id is not None and not _has_task_event_for_run(
                conn, row["id"], "live_long_op", run_id
            ):
                with write_txn(conn):
                    _append_event(
                        conn,
                        row["id"],
                        "live_long_op",
                        {
                            "elapsed_seconds": int(elapsed),
                            "last_heartbeat_at": int(last_hb),
                            "heartbeat_age_seconds": int(hb_age),
                            "heartbeat_window_seconds": _STALE_HEARTBEAT_GAP_SECONDS,
                            "timeout_seconds": stale_timeout_seconds,
                        },
                        run_id=run_id,
                    )
            continue  # recent heartbeat → still alive

        pid = row["worker_pid"]
        tid = row["id"]
        lock = row["claim_lock"] or ""

        # Terminate the worker if it's still host-local.
        termination = _terminate_reclaimed_worker(
            pid, lock, signal_fn=signal_fn,
        )

        with write_txn(conn):
            cur = conn.execute(
                "UPDATE tasks SET status = 'ready', claim_lock = NULL, "
                "claim_expires = NULL, worker_pid = NULL, "
                "last_heartbeat_at = NULL "
                "WHERE id = ? AND status = 'running'",
                (tid,),
            )
            if cur.rowcount != 1:
                continue

            payload = {
                "elapsed_seconds": int(elapsed),
                "last_heartbeat_at": (
                    int(last_hb) if last_hb is not None else None
                ),
                "heartbeat_age_seconds": (
                    int(hb_age) if hb_age is not None else None
                ),
                "timeout_seconds": stale_timeout_seconds,
                "pid": int(pid) if pid else None,
            }
            payload.update(termination)

            run_id = _end_run(
                conn, tid,
                outcome="stale", status="stale",
                error=(
                    f"no heartbeat for {int(hb_age)}s "
                    if hb_age is not None
                    else "no heartbeat ever"
                ) + f" after {int(elapsed)}s running",
                metadata=payload,
            )
            _append_event(
                conn, tid, "stale", payload, run_id=run_id,
            )
            reclaimed.append(tid)

        # Intentionally NOT calling _record_task_failure here. Stale reclaim
        # is dispatcher-side detection of an absent heartbeat; the task is
        # going straight back to ``ready`` for re-dispatch. Counting it as
        # a worker failure would let two legitimately-long-running tasks
        # (>4h without explicit heartbeat) trip the circuit breaker and
        # auto-block, even though no worker actually failed. The 'stale'
        # event already lives in task_events for auditability; that's the
        # right surface for "this happened" without conflating with the
        # spawn_failed / timed_out / crashed counters.

    return reclaimed


def set_max_runtime(
    conn: sqlite3.Connection,
    task_id: str,
    seconds: Optional[int],
) -> bool:
    """Set or clear the per-task max_runtime_seconds. Returns True on
    success."""
    with write_txn(conn):
        cur = conn.execute(
            "UPDATE tasks SET max_runtime_seconds = ? WHERE id = ?",
            (int(seconds) if seconds is not None else None, task_id),
        )
    return cur.rowcount == 1


def _error_fingerprint(error_text: str) -> str:
    """Normalize an error message for grouping identical failures.

    Strips host-specific details (PIDs, timestamps) so that errors
    with the same root cause produce the same fingerprint.
    """
    fp = re.sub(r'\bpid \d+\b', 'pid N', error_text[:80])
    fp = re.sub(r'\b\d{10,}\b', '<TS>', fp)
    return fp.lower().strip()


def detect_crashed_workers(conn: sqlite3.Connection) -> list[str]:
    """Reclaim ``running`` tasks whose worker PID is no longer alive.

    Appends a ``crashed`` event and drops the task back to ``ready``.
    Different from ``release_stale_claims``: this checks liveness
    immediately rather than waiting for the claim TTL.

    Only considers tasks claimed by *this host* — PIDs from other hosts
    are meaningless here. The host-local check is enough because
    ``_default_spawn`` always runs the worker on the same host as the
    dispatcher (the whole design is single-host).

    When the reap registry shows the worker exited cleanly (rc=0) but
    the task was still ``running`` in the DB, treat it as a protocol
    violation (worker answered conversationally without calling
    ``kanban_complete`` / ``kanban_block``) and trip the circuit breaker
    on the first occurrence — retrying a worker whose CLI keeps
    returning 0 without a terminal transition just loops forever.
    """
    crashed: list[str] = []
    # Per-crash details collected inside the main txn, used after it
    # closes to run ``_record_task_failure`` (which needs its own
    # write_txn so can't nest). ``protocol_violation`` flags the
    # clean-exit-but-still-running case so we can trip the breaker
    # immediately instead of incrementing by 1.
    crash_details: list[tuple[str, int, str, bool, str]] = []
    # (task_id, pid, claimer, protocol_violation, error_text)
    with write_txn(conn):
        rows = conn.execute(
            """
            SELECT
                t.id,
                t.worker_pid,
                t.claim_lock,
                t.current_run_id,
                r.worker_exit_kind,
                r.worker_exit_code,
                r.worker_protocol_state,
                r.worker_failure_fingerprint
            FROM tasks t
            LEFT JOIN task_runs r ON r.id = t.current_run_id
            WHERE t.status = 'running' AND t.worker_pid IS NOT NULL
            """
        ).fetchall()
        host_prefix = f"{_claimer_id().split(':', 1)[0]}:"
        for row in rows:
            # Only check liveness for claims owned by this host.
            lock = row["claim_lock"] or ""
            if not lock.startswith(host_prefix):
                continue
            if _pid_alive(row["worker_pid"]):
                continue

            pid = int(row["worker_pid"])
            persisted_kind = (
                row["worker_exit_kind"]
                if "worker_exit_kind" in row.keys()
                and row["worker_exit_kind"]
                and row["worker_exit_kind"] != "pending"
                else None
            )
            if persisted_kind:
                kind = str(persisted_kind)
                code = row["worker_exit_code"]
            else:
                legacy_kind, code = _classify_worker_exit(pid)
                if legacy_kind == "clean_exit":
                    kind = "clean_exit_protocol_violation"
                elif legacy_kind in {"nonzero_exit", "signaled"}:
                    kind = legacy_kind
                else:
                    kind = "pid_not_alive"
                if row["current_run_id"]:
                    protocol_state = (
                        "silent"
                        if kind == "clean_exit_protocol_violation"
                        else (
                            row["worker_protocol_state"]
                            if "worker_protocol_state" in row.keys()
                            and row["worker_protocol_state"]
                            else "pending"
                        )
                    )
                    failure_text = _worker_exit_error_text(kind, pid, code)
                    with contextlib.suppress(Exception):
                        conn.execute(
                            """
                            UPDATE task_runs
                               SET worker_exit_kind = ?,
                                   worker_exit_code = ?,
                                   worker_protocol_state = ?,
                                   worker_failure_fingerprint = ?
                             WHERE id = ?
                               AND worker_exit_kind IS NULL
                            """,
                            (
                                kind,
                                code,
                                protocol_state,
                                (
                                    _error_fingerprint(failure_text)
                                    if failure_text else None
                                ),
                                int(row["current_run_id"]),
                            ),
                        )

            if kind == "clean_exit_protocol_violation":
                # Worker subprocess returned 0 but its task is still
                # ``running`` in the DB — it exited without calling
                # ``kanban_complete`` / ``kanban_block``. Retrying won't
                # help.
                protocol_violation = True
                error_text = _worker_exit_error_text(kind, pid, code) or (
                    "worker exited cleanly (rc=0) without calling "
                    "kanban_complete or kanban_block — protocol violation"
                )
                event_kind = "protocol_violation"
                event_payload = {
                    "pid": pid,
                    "claimer": row["claim_lock"],
                    "exit_code": code,
                    "worker_exit_kind": kind,
                    "worker_protocol_state": row["worker_protocol_state"] or "silent",
                }
            else:
                protocol_violation = False
                if kind == "nonzero_exit":
                    error_text = f"pid {pid} exited with code {code}"
                elif kind == "signaled":
                    error_text = f"pid {pid} killed by signal {code}"
                else:
                    error_text = f"pid {pid} not alive"
                event_kind = "crashed"
                event_payload = {"pid": pid, "claimer": row["claim_lock"]}
                if kind:
                    event_payload["worker_exit_kind"] = kind
                if row["worker_protocol_state"]:
                    event_payload["worker_protocol_state"] = row["worker_protocol_state"]
                if code is not None and kind != "pid_not_alive":
                    event_payload["exit_kind"] = kind
                    event_payload["exit_code"] = code

            cur = conn.execute(
                "UPDATE tasks SET status = 'ready', claim_lock = NULL, "
                "claim_expires = NULL, worker_pid = NULL "
                "WHERE id = ? AND status = 'running'",
                (row["id"],),
            )
            if cur.rowcount == 1:
                run_id = _end_run(
                    conn, row["id"],
                    outcome="crashed", status="crashed",
                    error=error_text,
                    metadata=dict(event_payload),
                )
                crash_payload = _terminal_event_payload(
                    conn,
                    row["id"],
                    run_id,
                    outcome="crashed",
                    error=error_text,
                    extra=event_payload,
                )
                _append_event(
                    conn, row["id"], event_kind,
                    crash_payload,
                    run_id=run_id,
                )
                crashed.append(row["id"])
                crash_details.append(
                    (row["id"], pid, row["claim_lock"],
                     protocol_violation, error_text)
                )
    # Outside the main txn: increment the unified failure counter for
    # each crashed task. If the breaker trips, the task transitions
    # ready → blocked with a ``gave_up`` event on top of the ``crashed``
    # event we already emitted.
    #
    # Protocol-violation crashes force an immediate trip (failure_limit=1)
    # because clean-exit-without-transition is deterministic: the next
    # respawn will do exactly the same thing. Better to surface to a
    # human with a clear reason than to loop ``DEFAULT_FAILURE_LIMIT``
    # times first.
    auto_blocked: list[str] = []
    if crash_details:
        # Fingerprint errors to detect systemic failures.
        _fp_counts: dict[str, int] = {}
        for _, _, _, _, err_text in crash_details:
            fp = _error_fingerprint(err_text)
            _fp_counts[fp] = _fp_counts.get(fp, 0) + 1
        for tid, pid, claimer, protocol_violation, error_text in crash_details:
            fp = _error_fingerprint(error_text)
            is_systemic = (
                not protocol_violation
                and _fp_counts.get(fp, 0) >= 3
            )
            tripped = _record_task_failure(
                conn, tid,
                error=error_text,
                outcome="crashed",
                failure_limit=1 if (protocol_violation or is_systemic) else None,
                release_claim=False,
                end_run=False,
                event_payload_extra={"pid": pid, "claimer": claimer},
            )
            if tripped:
                auto_blocked.append(tid)
    # Stash auto-blocked ids on the function for the dispatch loop to pick up.
    # Keeps the public return type (``list[str]``) stable for direct callers
    # and tests that destructure the result; ``dispatch_once`` reads this
    # side-channel attribute to populate ``DispatchResult.auto_blocked``.
    detect_crashed_workers._last_auto_blocked = auto_blocked  # type: ignore[attr-defined]
    return crashed


def _record_task_failure(
    conn: sqlite3.Connection,
    task_id: str,
    error: str,
    *,
    outcome: str,
    failure_limit: int = None,
    release_claim: bool = False,
    end_run: bool = False,
    event_payload_extra: Optional[dict] = None,
) -> bool:
    """Record a non-success outcome (spawn_failed / crashed / timed_out)
    and maybe trip the circuit breaker.

    Unified replacement for the old spawn-only ``_record_spawn_failure``.
    Every path that ends a task with a non-success outcome funnels
    through here so the ``consecutive_failures`` counter and the
    auto-block threshold stay consistent.

    Returns True when the task was auto-blocked (counter reached
    ``failure_limit``), False when it was just updated in place.

    Modes:

    * ``release_claim=True, end_run=True`` — spawn-failure path.
      Caller has a running task with an open run; this transitions
      it back to ``ready`` (or ``blocked`` when the breaker trips),
      releases the claim, and closes the run with ``outcome=<outcome>``.

    * ``release_claim=False, end_run=False`` — timeout/crash path.
      Caller has ALREADY flipped the task to ``ready`` and closed the
      run with the appropriate outcome. This just increments the
      counter; if the breaker trips, the task is re-transitioned
      ``ready → blocked`` and a ``gave_up`` event is emitted.

    ``event_payload_extra`` merges into the ``gave_up`` event payload
    when the breaker trips, so callers can include outcome-specific
    context (e.g. pid on crash, elapsed on timeout).

    Resolution order for the effective threshold:
      1. per-task ``max_retries`` if set (nothing else overrides)
      2. caller-supplied ``failure_limit`` (gateway passes the config
         value from ``kanban.failure_limit``; tests pass fixed values)
      3. ``DEFAULT_FAILURE_LIMIT``
    """
    if failure_limit is None:
        failure_limit = DEFAULT_FAILURE_LIMIT
    blocked = False
    with write_txn(conn):
        row = conn.execute(
            "SELECT consecutive_failures, status, max_retries "
            "FROM tasks WHERE id = ?", (task_id,),
        ).fetchone()
        if row is None:
            return False
        failures = int(row["consecutive_failures"]) + 1
        cur_status = row["status"]

        # Per-task override wins over both caller-supplied and default
        # thresholds. None (the common case) falls through.
        task_override = (
            row["max_retries"] if "max_retries" in row.keys() else None
        )
        if task_override is not None:
            effective_limit = int(task_override)
            limit_source = "task"
        else:
            effective_limit = int(failure_limit)
            limit_source = "dispatcher"

        if failures >= effective_limit:
            # Trip the breaker.
            if release_claim:
                # Spawn path: still running, also clear claim state.
                conn.execute(
                    "UPDATE tasks SET status = 'blocked', claim_lock = NULL, "
                    "claim_expires = NULL, worker_pid = NULL, "
                    "consecutive_failures = ?, last_failure_error = ? "
                    "WHERE id = ? AND status IN ('running', 'ready')",
                    (failures, error[:500], task_id),
                )
            else:
                # Timeout/crash path: task is already at ``ready``
                # with claim cleared; just flip to blocked + update
                # counter fields.
                conn.execute(
                    "UPDATE tasks SET status = 'blocked', "
                    "consecutive_failures = ?, last_failure_error = ? "
                    "WHERE id = ? AND status IN ('ready', 'running')",
                    (failures, error[:500], task_id),
                )
            run_id = None
            if end_run:
                # Only the spawn path has an open run to close.
                run_id = _end_run(
                    conn, task_id,
                    outcome="gave_up", status="gave_up",
                    error=error[:500],
                    metadata={
                        "failures": failures,
                        "trigger_outcome": outcome,
                        "effective_limit": effective_limit,
                        "limit_source": limit_source,
                    },
                )
            payload = {
                "failures": failures,
                "effective_limit": effective_limit,
                "limit_source": limit_source,
                "error": error[:500],
                "trigger_outcome": outcome,
            }
            if event_payload_extra:
                payload.update(event_payload_extra)
            gave_up_payload = _terminal_event_payload(
                conn,
                task_id,
                run_id,
                outcome="gave_up",
                error=error[:500],
                extra=payload,
            )
            _append_event(
                conn, task_id, "gave_up", gave_up_payload, run_id=run_id,
            )
            blocked = True
        else:
            # Below threshold — task will retry. Stamp a backoff window
            # via ``last_failure_error`` suffix (``; retry_after=<ts>``)
            # so ``dispatch_once`` skips it until the window passes.
            retry_after_ts = int(time.time()) + _backoff_sec_for_failure(failures)
            stamped_error = _stamp_retry_after(error[:500], retry_after_ts)
            if release_claim:
                # Spawn path: transition running → ready + clear claim.
                conn.execute(
                    "UPDATE tasks SET status = 'ready', claim_lock = NULL, "
                    "claim_expires = NULL, worker_pid = NULL, "
                    "consecutive_failures = ?, last_failure_error = ? "
                    "WHERE id = ? AND status = 'running'",
                    (failures, stamped_error, task_id),
                )
            else:
                # Timeout/crash path: task is already at ``ready`` via
                # its own UPDATE. Just bookkeep the counter + last error.
                conn.execute(
                    "UPDATE tasks SET consecutive_failures = ?, "
                    "last_failure_error = ? WHERE id = ?",
                    (failures, stamped_error, task_id),
                )
            if end_run:
                # Spawn path: close the open run with outcome.
                run_id = _end_run(
                    conn, task_id,
                    outcome=outcome, status=outcome,
                    error=error[:500],
                    metadata={"failures": failures},
                )
                failure_payload = _terminal_event_payload(
                    conn,
                    task_id,
                    run_id,
                    outcome=outcome,
                    error=error[:500],
                    extra={"failures": failures},
                )
                _append_event(
                    conn, task_id, outcome,
                    failure_payload,
                    run_id=run_id,
                )
            # Timeout/crash path's caller already emitted its own event.
    return blocked


# Backward-compat alias. Old name is referenced from tests and possibly
# third-party callers. New code should call ``_record_task_failure``.
def _record_spawn_failure(
    conn: sqlite3.Connection,
    task_id: str,
    error: str,
    *,
    failure_limit: int = None,
) -> bool:
    return _record_task_failure(
        conn, task_id, error,
        outcome="spawn_failed",
        failure_limit=failure_limit,
        release_claim=True,
        end_run=True,
    )


def _gate_shadow_decisions_for_running(
    conn: sqlite3.Connection,
    *,
    now: int,
    stale_timeout_seconds: int,
    failure_limit: int,
) -> list:
    """Build read-only shadow decisions for currently running task attempts."""
    try:
        from hermes_cli.control_plane.gate_decisions import (
            TaskRunSnapshot,
            TaskSnapshot,
            decide_for_run,
        )
    except Exception:
        return []

    host_prefix = f"{_claimer_id().split(':', 1)[0]}:"
    rows = conn.execute(
        """
        SELECT
            t.id AS task_id,
            t.status AS task_status,
            t.claim_lock AS task_claim_lock,
            t.claim_expires AS task_claim_expires,
            t.worker_pid AS task_worker_pid,
            t.last_heartbeat_at AS task_last_heartbeat_at,
            t.started_at AS task_started_at,
            t.current_run_id AS task_current_run_id,
            t.max_runtime_seconds AS task_max_runtime_seconds,
            t.consecutive_failures AS task_consecutive_failures,
            t.max_retries AS task_max_retries,
            t.assignee AS task_assignee,
            t.completed_at AS task_completed_at,
            t.body AS task_body,
            r.id AS run_id,
            r.status AS run_status,
            r.claim_lock AS run_claim_lock,
            r.claim_expires AS run_claim_expires,
            r.worker_pid AS run_worker_pid,
            r.max_runtime_seconds AS run_max_runtime_seconds,
            r.last_heartbeat_at AS run_last_heartbeat_at,
            r.started_at AS run_started_at,
            r.ended_at AS run_ended_at,
            r.outcome AS run_outcome,
            r.summary AS run_summary,
            r.metadata AS run_metadata,
            r.error AS run_error
        FROM tasks t
        LEFT JOIN task_runs r ON r.id = t.current_run_id
        WHERE t.status = 'running'
          AND r.id IS NOT NULL
          AND r.status = 'running'
        ORDER BY t.id
        """
    ).fetchall()

    decisions = []
    for row in rows:
        lock = row["run_claim_lock"] or row["task_claim_lock"] or ""
        host_local = bool(lock.startswith(host_prefix))
        worker_pid = (
            row["run_worker_pid"]
            if row["run_worker_pid"] is not None
            else row["task_worker_pid"]
        )
        pid_alive = None
        exit_kind = None
        exit_code = None
        if host_local and worker_pid is not None:
            pid_alive = _pid_alive(int(worker_pid))
            if pid_alive is False:
                exit_kind, exit_code = _classify_worker_exit(int(worker_pid))

        metadata = None
        if row["run_metadata"]:
            try:
                metadata = json.loads(row["run_metadata"])
            except Exception:
                metadata = None

        task_snapshot = TaskSnapshot(
            id=row["task_id"],
            status=row["task_status"],
            claim_lock=row["task_claim_lock"],
            claim_expires=row["task_claim_expires"],
            worker_pid=row["task_worker_pid"],
            last_heartbeat_at=row["task_last_heartbeat_at"],
            started_at=row["task_started_at"],
            current_run_id=row["task_current_run_id"],
            max_runtime_seconds=row["task_max_runtime_seconds"],
            consecutive_failures=int(row["task_consecutive_failures"] or 0),
            max_retries=row["task_max_retries"],
            assignee=row["task_assignee"],
            completed_at=row["task_completed_at"],
            body=row["task_body"],
        )
        run_snapshot = TaskRunSnapshot(
            id=int(row["run_id"]),
            task_id=row["task_id"],
            status=row["run_status"],
            claim_lock=row["run_claim_lock"],
            claim_expires=row["run_claim_expires"],
            worker_pid=row["run_worker_pid"],
            max_runtime_seconds=row["run_max_runtime_seconds"],
            last_heartbeat_at=row["run_last_heartbeat_at"],
            started_at=row["run_started_at"],
            ended_at=row["run_ended_at"],
            outcome=row["run_outcome"],
            summary=row["run_summary"],
            metadata=metadata,
            pid_alive=pid_alive,
            exit_kind=exit_kind,
            exit_code=exit_code,
            host_local=host_local,
        )
        decisions.append(
            decide_for_run(
                run_snapshot,
                task_snapshot,
                now,
                stale_timeout_seconds=stale_timeout_seconds,
                failure_limit=failure_limit,
            )
        )
    return decisions


_GATE_DECISION_EVENT_ACTIONS = {
    "claim_extended": "extend_stale_claim",
    "reclaimed": "reclaim_stale",
    "stale": "reclaim_stale",
    "crashed": "classify_crash",
    "protocol_violation": "classify_crash",
    "timed_out": "enforce_timeout",
    "gave_up": "gave_up",
}

_GATE_DECISION_EVENT_PRIORITY = {
    "enforce_timeout": 50,
    "classify_crash": 40,
    "reclaim_stale": 30,
    "extend_stale_claim": 20,
    "gave_up": 10,
}


def _gate_actual_actions_since(
    conn: sqlite3.Connection,
    *,
    event_watermark: int,
    task_ids: list[str],
) -> dict[str, dict[str, Any]]:
    if not task_ids:
        return {}
    placeholders = ",".join("?" for _ in task_ids)
    rows = conn.execute(
        f"""
        SELECT task_id, run_id, kind, payload
          FROM task_events
         WHERE id > ?
           AND task_id IN ({placeholders})
           AND kind IN ({",".join("?" for _ in _GATE_DECISION_EVENT_ACTIONS)})
         ORDER BY id ASC
        """,
        [event_watermark, *task_ids, *_GATE_DECISION_EVENT_ACTIONS.keys()],
    ).fetchall()
    actual: dict[str, dict[str, Any]] = {}
    for row in rows:
        action = _GATE_DECISION_EVENT_ACTIONS.get(row["kind"])
        if not action:
            continue
        current = actual.get(row["task_id"])
        if current and _GATE_DECISION_EVENT_PRIORITY[current["action"]] > _GATE_DECISION_EVENT_PRIORITY[action]:
            continue
        payload = None
        if row["payload"]:
            try:
                payload = json.loads(row["payload"])
            except Exception:
                payload = None
        actual[row["task_id"]] = {
            "action": action,
            "event_kind": row["kind"],
            "run_id": row["run_id"],
            "payload": payload,
        }
    return actual


def _gate_shadow_matches_actual(shadow, actual: Optional[dict[str, Any]]) -> bool:
    if actual is None:
        return shadow.action in {"keep_running", "no_op"}
    if shadow.action == "reclaim_stale":
        return actual["action"] == "reclaim_stale"
    if shadow.action == "classify_crash":
        return actual["action"] == "classify_crash"
    if shadow.action == "enforce_timeout":
        return actual["action"] == "enforce_timeout"
    if shadow.action == "extend_stale_claim":
        return actual["action"] == "extend_stale_claim"
    return shadow.action == actual["action"]


def _emit_gate_decision_parity_divergences(
    conn: sqlite3.Connection,
    *,
    shadow_decisions: list,
    event_watermark: int,
) -> int:
    """Emit gate_decision_parity only for shadow/actual divergences."""
    if not shadow_decisions:
        return 0
    task_ids = [d.task_id for d in shadow_decisions]
    actual_by_task = _gate_actual_actions_since(
        conn,
        event_watermark=event_watermark,
        task_ids=task_ids,
    )
    divergences = [
        (decision, actual_by_task.get(decision.task_id))
        for decision in shadow_decisions
        if not _gate_shadow_matches_actual(decision, actual_by_task.get(decision.task_id))
    ]
    if not divergences:
        return 0

    emitted = 0
    with write_txn(conn):
        for decision, actual in divergences:
            _append_event(
                conn,
                decision.task_id,
                "gate_decision_parity",
                {
                    "run_id": decision.run_id,
                    "ticker_decision": actual,
                    "shadow_decision": decision.as_payload(),
                    "match": False,
                    "family": "running_lifecycle",
                },
                run_id=decision.run_id,
            )
            emitted += 1
    return emitted


def _set_worker_pid(conn: sqlite3.Connection, task_id: str, pid: int) -> None:
    """Record the spawned child's pid + emit a ``spawned`` event.

    The event's payload carries the pid so a human reading ``hermes kanban
    tail`` can correlate log lines with OS-level traces without opening
    the drawer.
    """
    with write_txn(conn):
        conn.execute(
            "UPDATE tasks SET worker_pid = ? WHERE id = ?",
            (int(pid), task_id),
        )
        run_id = _current_run_id(conn, task_id)
        if run_id is not None:
            conn.execute(
                "UPDATE task_runs SET worker_pid = ? WHERE id = ?",
                (int(pid), run_id),
            )
        _append_event(conn, task_id, "spawned", {"pid": int(pid)}, run_id=run_id)


def _clear_failure_counter(conn: sqlite3.Connection, task_id: str) -> None:
    """Reset the unified consecutive-failures counter.

    Called from ``complete_task`` on successful completion — a fresh
    success means the task + profile combination is working and any
    past failures are history. NOT called on spawn success anymore:
    a successful spawn proves the worker could start but says nothing
    about whether the run will succeed, so we need to let timeouts and
    crashes accumulate across spawn boundaries.
    """
    with write_txn(conn):
        conn.execute(
            "UPDATE tasks SET consecutive_failures = 0, "
            "last_failure_error = NULL WHERE id = ?",
            (task_id,),
        )


# Legacy alias for test-code and anything else that still imports it.
_clear_spawn_failures = _clear_failure_counter


def check_respawn_guard(conn: sqlite3.Connection, task_id: str) -> Optional[str]:
    """Return a guard reason if ``task_id`` should NOT be re-spawned, else None.

    Called per ready task in ``dispatch_once`` before any claim attempt.
    Returning a reason defers the spawn this tick; the task stays in
    ``ready`` and gets another chance on the next dispatcher tick.

    Checks in priority order:

    ``"blocker_auth"``
        The task's last failure error matches a quota / authentication
        pattern. Retrying immediately is unlikely to help (rate limits
        reset on a timer; auth needs human action), so we defer to the
        next tick. The existing ``consecutive_failures`` counter still
        trips the auto-block circuit breaker after ``failure_limit``
        consecutive failures, so a persistent auth error eventually
        blocks via the normal path — but a transient 429 gets a few
        ticks of recovery first.

    ``"recent_success"``
        A completed run exists within ``_RESPAWN_GUARD_SUCCESS_WINDOW``
        seconds.  Useful work already succeeded for this task; wait for
        human review rather than immediately re-spawning.

    ``"active_pr"``
        A GitHub PR URL appears in a recent task comment (within
        ``_RESPAWN_GUARD_PR_WINDOW`` seconds).  A prior worker already
        opened a PR; re-spawning risks a duplicate PR on the same task.

    Stale / dead claim locks are NOT a guard reason — they are handled
    by ``release_stale_claims`` and ``detect_crashed_workers`` which
    reset the task to ``ready`` only after verifying the lock is
    genuinely dead (no live PID on this host).
    """
    row = conn.execute(
        "SELECT last_failure_error FROM tasks WHERE id = ?",
        (task_id,),
    ).fetchone()
    if row is None:
        return None

    # 1. Quota / auth blocker: retrying immediately will not help.
    err = row["last_failure_error"]
    if err and _RESPAWN_BLOCKER_RE.search(err):
        return "blocker_auth"

    now = int(time.time())

    # 2. Completed run within guard window — proof of recent success.
    cutoff = now - _RESPAWN_GUARD_SUCCESS_WINDOW
    if conn.execute(
        "SELECT id FROM task_runs "
        "WHERE task_id = ? AND outcome = 'completed' AND ended_at >= ?",
        (task_id, cutoff),
    ).fetchone():
        return "recent_success"

    # 3. GitHub PR URL in a recent comment — prior worker already opened a PR.
    pr_cutoff = now - _RESPAWN_GUARD_PR_WINDOW
    for c in conn.execute(
        "SELECT body FROM task_comments WHERE task_id = ? AND created_at >= ?",
        (task_id, pr_cutoff),
    ).fetchall():
        if c["body"] and _RESPAWN_GUARD_PR_URL_RE.search(c["body"]):
            return "active_pr"

    return None


def has_spawnable_ready(conn: sqlite3.Connection) -> bool:
    """Return True iff there is at least one ready+assigned+unclaimed task
    whose assignee maps to a real Hermes profile.

    Used by the gateway- and CLI-embedded dispatchers' health telemetry to
    decide whether ``0 spawned`` is a "stuck" condition (real spawnable
    work waiting) or a "correctly idle" condition (only control-plane
    lanes like ``orion-cc`` / ``orion-research`` waiting on terminals
    that pull tasks via ``claim_task`` directly).

    Falls back to "any ready+assigned" if ``profile_exists`` is not
    importable (e.g. partial install) — preserves the old behavior so
    the warning still fires in degraded environments.
    """
    rows = conn.execute(
        "SELECT DISTINCT assignee FROM tasks "
        "WHERE status = 'ready' AND assignee IS NOT NULL "
        "    AND claim_lock IS NULL"
    ).fetchall()
    if not rows:
        return False
    try:
        from hermes_cli.profiles import profile_exists  # local import: avoids cycle
    except Exception:
        # Can't introspect — assume spawnable, preserve legacy behavior.
        return True
    for row in rows:
        if profile_exists(row["assignee"]):
            return True
    return False


def has_spawnable_review(conn: sqlite3.Connection) -> bool:
    """Return True iff there is at least one review+assigned+unclaimed task
    whose assignee maps to a real Hermes profile.

    Mirror of :func:`has_spawnable_ready` for the review column —
    used by the health telemetry to decide whether the dispatcher
    should have spawned a review agent.
    """
    rows = conn.execute(
        "SELECT DISTINCT assignee FROM tasks "
        "WHERE status = 'review' AND assignee IS NOT NULL "
        "    AND claim_lock IS NULL"
    ).fetchall()
    if not rows:
        return False
    try:
        from hermes_cli.profiles import profile_exists  # local import: avoids cycle
    except Exception:
        return True
    for row in rows:
        if profile_exists(row["assignee"]):
            return True
    return False


def dispatch_once(
    conn: sqlite3.Connection,
    *,
    spawn_fn=None,
    ttl_seconds: Optional[int] = None,
    dry_run: bool = False,
    max_spawn: Optional[int] = None,
    max_in_progress: Optional[int] = None,
    failure_limit: int = DEFAULT_SPAWN_FAILURE_LIMIT,
    stale_timeout_seconds: int = 0,
    board: Optional[str] = None,
) -> DispatchResult:
    """Run one dispatcher tick.

    Steps:
      1. Reclaim stale running tasks (TTL expired).
      2. Reclaim stale running tasks (no recent heartbeat).
      3. Reclaim crashed running tasks (host-local PID no longer alive).
      3. Promote todo -> ready where all parents are done.
      4. For each ready task with an assignee, atomically claim and call
         ``spawn_fn(task, workspace_path, board) -> Optional[int]``. The
         return value (if any) is recorded as ``worker_pid`` so subsequent
         ticks can detect crashes before the TTL expires.

    Spawn failures are counted per-task. After ``failure_limit`` consecutive
    failures the task is auto-blocked with the last error as its reason —
    prevents the dispatcher from thrashing forever on an unfixable task.

    ``max_spawn`` is a **live concurrency cap**, not a per-tick spawn budget:
    it counts tasks already in ``status='running'`` plus this tick's spawns
    against the limit. So ``max_spawn=4`` means "at most 4 workers running
    at any time across the whole board" — matching the gateway's stated
    intent ("limit concurrent kanban tasks"). With a per-tick interpretation
    a 60-second tick interval could grow concurrency by N every minute on a
    busy board and accumulate without bound.

    ``spawn_fn`` defaults to ``_default_spawn``. Tests pass a stub.
    ``board`` pins workspace/log/db resolution for this tick to a specific
    board. When omitted, the current-board resolution chain is used.
    """
    # Reap zombie children from previously spawned workers.
    # The gateway-embedded dispatcher is the parent of every worker spawned
    # via _default_spawn (start_new_session=True only detaches the
    # controlling tty, not the parent). Without an explicit waitpid, each
    # completed worker becomes a <defunct> entry that lingers until gateway
    # exit. WNOHANG keeps this non-blocking; ChildProcessError means no
    # children to reap. Bounded: at most one tick's worth of completions
    # can be in <defunct> at once.
    #
    # We also record the exit status keyed by pid, so
    # ``detect_crashed_workers`` can distinguish a worker that exited
    # cleanly without calling ``kanban_complete`` / ``kanban_block``
    # (protocol violation — auto-block) from a real crash (OOM killer,
    # SIGKILL, non-zero exit — existing counter behavior).
    #
    # Windows has no zombies / no os.WNOHANG — subprocess.Popen handles
    # are freed when the Python object is garbage-collected or .wait() is
    # called explicitly.  The kanban dispatcher discards the Popen handle
    # after spawn (``_default_spawn`` → abandon), so on Windows there's
    # nothing to reap here — skip the whole block.
    if os.name != "nt":
        try:
            while True:
                try:
                    _pid, _status = os.waitpid(-1, os.WNOHANG)
                except ChildProcessError:
                    break
                if _pid == 0:
                    break
                _record_worker_exit(_pid, _status, conn)
        except Exception:
            pass

    try:
        _gate_event_watermark = int(
            conn.execute("SELECT COALESCE(MAX(id), 0) FROM task_events").fetchone()[0]
        )
        _gate_shadow_decisions = _gate_shadow_decisions_for_running(
            conn,
            now=int(time.time()),
            stale_timeout_seconds=stale_timeout_seconds,
            failure_limit=failure_limit,
        )
    except Exception:
        _log.warning("gate decision shadow snapshot failed", exc_info=True)
        _gate_event_watermark = 0
        _gate_shadow_decisions = []

    result = DispatchResult()
    result.reclaimed = release_stale_claims(conn)
    result.stale = detect_stale_running(
        conn, stale_timeout_seconds=stale_timeout_seconds,
    )
    result.crashed = detect_crashed_workers(conn)
    # detect_crashed_workers stashes protocol-violation auto-blocks on
    # itself so the public list-return stays stable. Pull them into the
    # DispatchResult here so telemetry / tests see the trip.
    _crash_auto_blocked = getattr(
        detect_crashed_workers, "_last_auto_blocked", []
    )
    if _crash_auto_blocked:
        result.auto_blocked.extend(_crash_auto_blocked)
    result.timed_out = enforce_max_runtime(conn)
    if not dry_run:
        auto_continued, continuation_capped = _auto_continue_iteration_budget_blocks(conn)
        result.auto_continued.extend(auto_continued)
        result.continuation_capped.extend(continuation_capped)
        _dispatch_review_required_handoffs(conn)
        _dispatch_standard_review_children(conn)
    result.promoted = recompute_ready(conn)
    try:
        _emit_gate_decision_parity_divergences(
            conn,
            shadow_decisions=_gate_shadow_decisions,
            event_watermark=_gate_event_watermark,
        )
    except Exception:
        _log.warning("gate decision shadow parity failed", exc_info=True)

    # Count tasks already running so max_spawn enforces concurrency rather
    # than a per-tick spawn budget. See the docstring above for the full
    # rationale; the short version is that a 60-second tick interval with a
    # per-tick budget of N would grow concurrency by N every tick on a busy
    # board, since "running" tasks aren't reclaimed by completion alone —
    # they sit in status='running' until the worker calls
    # kanban_complete/kanban_block (or the dispatcher TTL-reclaims them).
    running_count = 0
    if max_spawn is not None:
        running_count = int(
            conn.execute(
                "SELECT COUNT(*) FROM tasks WHERE status = 'running'"
            ).fetchone()[0]
        )

    ready_rows = conn.execute(
        "SELECT * FROM tasks "
        "WHERE status = 'ready' AND claim_lock IS NULL "
        "ORDER BY priority DESC, created_at ASC"
    ).fetchall()
    # Honour kanban.max_in_progress: if the board already has enough running
    # tasks, skip spawning this tick so slow workers (local LLMs,
    # resource-constrained hosts) can finish what they have before more tasks
    # pile up and time out.
    if max_in_progress is not None and ready_rows:
        in_progress = conn.execute(
            "SELECT COUNT(*) FROM tasks WHERE status = 'running'"
        ).fetchone()[0]
        if in_progress >= max_in_progress:
            return result
        # Only spawn enough to reach the cap, respecting max_spawn too.
        remaining = max_in_progress - in_progress
        if max_spawn is None or max_spawn > remaining:
            max_spawn = remaining
    # C2 Backoff: skip tasks whose last_failure_error carries a
    # ``; retry_after=<ts>`` suffix that is still in the future. Tasks
    # without a stamp (the common case) pass through unaffected. Filter
    # in Python rather than SQL so the parsing stays in one place.
    _retry_now_ts = int(time.time())
    _retry_deferred = []
    _ready_filtered = []
    for _row in ready_rows:
        _ts = _retry_after_ts_from_error(_row["last_failure_error"])
        if _ts is not None and _ts > _retry_now_ts:
            _retry_deferred.append(_row["id"])
            continue
        _ready_filtered.append(_row)
    if _retry_deferred:
        result.retry_deferred = list(_retry_deferred)
    ready_rows = _ready_filtered
    spawned = 0
    for row in ready_rows:
        if max_spawn is not None and running_count + spawned >= max_spawn:
            break
        if not row["assignee"]:
            result.skipped_unassigned.append(row["id"])
            continue
        # Skip ready tasks whose assignee is not a real Hermes profile.
        # `_default_spawn` invokes ``hermes -p <assignee>`` which fails
        # with "Profile 'X' does not exist" when the assignee names a
        # control-plane lane (e.g. an interactive Claude Code terminal
        # like ``orion-cc`` / ``orion-research``) rather than a Hermes
        # profile. Those task lanes are pulled by terminals via
        # ``claim_task`` directly and should NEVER auto-spawn — the
        # subprocess would crash on startup, get reaped as a zombie,
        # the task would loop back to ``ready`` on next tick, and we'd
        # burn CPU forever (#kanban-dispatcher-crash-loop 2026-05-05).
        try:
            from hermes_cli.profiles import profile_exists  # local import: avoids cycle
        except Exception:
            profile_exists = None  # type: ignore[assignment]
        if profile_exists is not None and not profile_exists(row["assignee"]):
            # Distinguish autonomous-task-mistake from human-pulled lane:
            # tasks that carry a scope_contract are autonomous-spawned
            # workloads (planner output etc.). If the planner produced an
            # unknown assignee, the task must fail closed instead of
            # looping forever in the ready queue. Tasks WITHOUT a scope
            # contract are typically human-pulled lanes (e.g. orion-cc
            # Claude Code terminals) where skipping is intentional.
            if _task_requires_dispatcher_scope_preflight(row["body"]):
                reason = (
                    "dispatch preflight blocked task: assignee "
                    f"'{row['assignee']}' is not a known Hermes "
                    "profile (scope_contract v2 task)"
                )
                if _block_dispatch_preflight(
                    conn,
                    row["id"],
                    reason,
                    kind="dispatch_preflight_invalid_assignee",
                ):
                    result.preflight_blocked.append(row["id"])
                continue
            # Bucket separately from skipped_unassigned: the operator
            # cannot fix this by assigning a profile (the assignee IS the
            # intended owner — a terminal lane). Health telemetry uses
            # this distinction to suppress spurious "stuck" warnings on
            # multi-lane setups where the ready queue is steadily full
            # of human-pulled work.
            result.skipped_nonspawnable.append(row["id"])
            continue
        # Respawn guard: refuse to re-spawn when useful work is already
        # in-flight/recent, or when the last failure is a deterministic
        # blocker (quota / auth). The guard defers the spawn this tick so
        # the task gets a chance to clear (rate limits often reset in
        # seconds-to-minutes); the existing consecutive_failures counter
        # still trips the auto-block circuit breaker after failure_limit
        # consecutive failures, so a persistent auth error eventually
        # blocks via the normal path rather than on first occurrence.
        guard_reason = check_respawn_guard(conn, row["id"])
        if guard_reason is not None:
            result.respawn_guarded.append((row["id"], guard_reason))
            # Emit an event so operators can see why the task was
            # skipped when reading `hermes kanban tail` — without
            # this the task appears stuck in ready with no diagnosis.
            if not dry_run:
                with write_txn(conn):
                    _append_event(
                        conn, row["id"], "respawn_guarded",
                        {"reason": guard_reason},
                    )
            continue
        task_for_preflight = Task.from_row(row)
        if not dry_run:
            try:
                missing_skills = _validate_task_extra_skills(
                    task_for_preflight.skills,
                    profile=task_for_preflight.assignee,
                )
            except TypeError:
                # Back-compat for tests/embedders that monkeypatch the older
                # one-argument helper; the real helper accepts ``profile``.
                missing_skills = _validate_task_extra_skills(task_for_preflight.skills)
        else:
            missing_skills = []
        if missing_skills:
            reason = (
                "dispatch preflight blocked task: unknown force-loaded skill(s): "
                + ", ".join(missing_skills)
            )
            if _block_dispatch_preflight(
                conn, row["id"], reason, kind="dispatch_preflight_unknown_skills"
            ):
                result.preflight_blocked.append(row["id"])
            continue
        if not dry_run and _task_requires_dispatcher_scope_preflight(row["body"]):
            if not _task_has_scope_contract_v2(row["body"]):
                reason = (
                    "dispatch preflight blocked task: scope_contract version 2 "
                    "is required for worker tasks with scope/completion policy"
                )
                if _block_dispatch_preflight(
                    conn, row["id"], reason, kind="dispatch_preflight_missing_scope_contract"
                ):
                    result.preflight_blocked.append(row["id"])
                continue
            effective_toolsets, allowed_tool_errors = _validate_scope_allowed_tools(row["body"])
            if allowed_tool_errors:
                reason = (
                    "dispatch preflight blocked task: invalid scope_contract.allowed_tools: "
                    + "; ".join(allowed_tool_errors)
                )
                if _block_dispatch_preflight(
                    conn, row["id"], reason, kind="dispatch_preflight_invalid_allowed_tools"
                ):
                    result.preflight_blocked.append(row["id"])
                continue
            forbidden_system_errors = _validate_scope_forbidden_systems(row["body"])
            if forbidden_system_errors:
                reason = (
                    "dispatch preflight blocked task: invalid scope_contract.forbidden_systems: "
                    + "; ".join(forbidden_system_errors)
                )
                if _block_dispatch_preflight(
                    conn,
                    row["id"],
                    reason,
                    kind="dispatch_preflight_missing_forbidden_systems",
                ):
                    result.preflight_blocked.append(row["id"])
                continue
            runtime_effective_toolsets = _resolve_scope_runtime_tool_schema_names(
                effective_toolsets,
                task_id=row["id"],
            )
            runtime_tool_errors = _validate_effective_scope_runtime_tools(
                runtime_effective_toolsets,
                declared_allowed_tools=effective_toolsets,
            )
            if runtime_tool_errors:
                reason = (
                    "dispatch preflight blocked task: empty/incomplete effective runtime tools: "
                    + "; ".join(runtime_tool_errors)
                )
                evidence = {
                    "failure_reason": reason[:500],
                    "declared_allowed_tools": effective_toolsets,
                    "effective_toolsets": runtime_effective_toolsets,
                    "required_lifecycle_tools": list(_REQUIRED_SCOPE_LIFECYCLE_TOOLS),
                    "skills_requested": [
                        str(s).strip() for s in (task_for_preflight.skills or []) if str(s).strip()
                    ],
                    "skill_resolution": {
                        "status": "ok" if not missing_skills else "missing",
                        "missing": list(missing_skills),
                    },
                }
                if _block_dispatch_preflight(
                    conn,
                    row["id"],
                    reason,
                    kind="dispatch_preflight_empty_toolset",
                    evidence=evidence,
                ):
                    result.preflight_blocked.append(row["id"])
                continue
            _append_event(
                conn,
                row["id"],
                "dispatch_preflight_passed",
                {"effective_toolsets": runtime_effective_toolsets},
            )
        if dry_run:
            result.spawned.append((row["id"], row["assignee"], ""))
            continue
        claimed = claim_task(conn, row["id"], ttl_seconds=ttl_seconds)
        if claimed is None:
            continue
        try:
            workspace = resolve_workspace(claimed, board=board)
        except Exception as exc:
            auto = _record_spawn_failure(
                conn, claimed.id, f"workspace: {exc}",
                failure_limit=failure_limit,
            )
            if auto:
                result.auto_blocked.append(claimed.id)
            continue
        # Persist the resolved workspace path so the worker can cd there.
        set_workspace_path(conn, claimed.id, str(workspace))
        _maybe_emit_scratch_tip(conn, claimed.id, claimed.workspace_kind)
        _spawn = spawn_fn if spawn_fn is not None else _default_spawn
        try:
            # Back-compat: older spawn_fn signatures accept only
            # (task, workspace). Test stubs in the suite rely on that.
            # Introspect the callable and pass `board` only when supported.
            import inspect
            try:
                sig = inspect.signature(_spawn)
                if "board" in sig.parameters:
                    pid = _spawn(claimed, str(workspace), board=board)
                else:
                    pid = _spawn(claimed, str(workspace))
            except (TypeError, ValueError):
                pid = _spawn(claimed, str(workspace))
            if pid:
                _set_worker_pid(conn, claimed.id, int(pid))
            # NOTE: we intentionally do NOT reset consecutive_failures
            # here. A successful spawn proves the worker can start but
            # doesn't prove the run will succeed. Under unified
            # failure counting, resetting on spawn would let a task
            # that keeps timing out after spawn loop forever. The
            # counter is cleared only on successful completion (see
            # complete_task).
            result.spawned.append((claimed.id, claimed.assignee or "", str(workspace)))
            spawned += 1
        except Exception as exc:
            auto = _record_spawn_failure(
                conn, claimed.id, str(exc),
                failure_limit=failure_limit,
            )
            if auto:
                result.auto_blocked.append(claimed.id)

    # ---- review column dispatch ----
    # Review tasks are tasks that a worker moved to 'review' after
    # creating a PR.  The dispatcher spawns a review agent (loading
    # sdlc-review skill) that verifies the PR and either merges (→ done)
    # or rejects (→ back to running for the worker to fix).
    #
    # Same concurrency model as ready dispatch: review spawns count
    # against max_spawn alongside ready tasks, so the total number of
    # running workers stays bounded.
    review_rows = conn.execute(
        "SELECT id, assignee FROM tasks "
        "WHERE status = 'review' AND claim_lock IS NULL "
        "ORDER BY priority DESC, created_at ASC"
    ).fetchall()
    for row in review_rows:
        if max_spawn is not None and running_count + spawned >= max_spawn:
            break
        if not row["assignee"]:
            result.skipped_unassigned.append(row["id"])
            continue
        try:
            from hermes_cli.profiles import profile_exists
        except Exception:
            profile_exists = None  # type: ignore[assignment]
        if profile_exists is not None and not profile_exists(row["assignee"]):
            result.skipped_nonspawnable.append(row["id"])
            continue
        if dry_run:
            result.spawned.append((row["id"], row["assignee"], ""))
            continue
        claimed = claim_review_task(conn, row["id"], ttl_seconds=ttl_seconds)
        if claimed is None:
            continue
        try:
            workspace = resolve_workspace(claimed, board=board)
        except Exception as exc:
            auto = _record_spawn_failure(
                conn, claimed.id, f"workspace: {exc}",
                failure_limit=failure_limit,
            )
            if auto:
                result.auto_blocked.append(claimed.id)
            continue
        # Persist the resolved workspace path so the worker can cd there.
        set_workspace_path(conn, claimed.id, str(workspace))
        _maybe_emit_scratch_tip(conn, claimed.id, claimed.workspace_kind)
        # Force-load sdlc-review skill for review agents.  The
        # _default_spawn function already auto-loads kanban-worker, and
        # appends task.skills via --skills.  Setting task.skills here
        # means the review agent gets both kanban-worker (lifecycle)
        # and sdlc-review (review logic: AC verification, merge, etc.).
        claimed.skills = ["sdlc-review"]
        _spawn = spawn_fn if spawn_fn is not None else _default_spawn
        try:
            import inspect
            try:
                sig = inspect.signature(_spawn)
                if "board" in sig.parameters:
                    pid = _spawn(claimed, str(workspace), board=board)
                else:
                    pid = _spawn(claimed, str(workspace))
            except (TypeError, ValueError):
                pid = _spawn(claimed, str(workspace))
            if pid:
                _set_worker_pid(conn, claimed.id, int(pid))
            result.spawned.append((claimed.id, claimed.assignee or "", str(workspace)))
            spawned += 1
        except Exception as exc:
            auto = _record_spawn_failure(
                conn, claimed.id, str(exc),
                failure_limit=failure_limit,
            )
            if auto:
                result.auto_blocked.append(claimed.id)
    return result


def _positive_int(value: Any, default: int, *, minimum: int = 1) -> int:
    try:
        parsed = int(value)
    except (TypeError, ValueError):
        return default
    return parsed if parsed >= minimum else default


def worker_log_rotation_config(kanban_cfg: Optional[dict] = None) -> tuple[int, int]:
    """Return ``(rotate_bytes, backup_count)`` for worker log rotation.

    Defaults preserve the historical behavior: rotate at 2 MiB and keep one
    backup generation (``.log.1``). Operators with long-running workers can
    raise either value from ``config.yaml`` without changing dispatcher code.
    """
    if kanban_cfg is None:
        try:
            from hermes_cli.config import load_config

            kanban_cfg = (load_config().get("kanban") or {})
        except Exception:
            kanban_cfg = {}
    max_bytes = _positive_int(
        (kanban_cfg or {}).get("worker_log_rotate_bytes"),
        DEFAULT_LOG_ROTATE_BYTES,
        minimum=1,
    )
    backup_count = _positive_int(
        (kanban_cfg or {}).get("worker_log_backup_count"),
        DEFAULT_LOG_BACKUP_COUNT,
        minimum=0,
    )
    return max_bytes, backup_count


def _rotated_log_path(log_path: Path, generation: int) -> Path:
    return log_path.with_suffix(log_path.suffix + f".{generation}")


def _rotate_worker_log(
    log_path: Path,
    max_bytes: int,
    backup_count: int = DEFAULT_LOG_BACKUP_COUNT,
) -> None:
    """Rotate ``<log>`` when it exceeds ``max_bytes``.

    ``backup_count=1`` preserves the legacy single-generation behavior:
    ``<log>`` moves to ``<log>.1`` and any previous ``.1`` is replaced.
    Higher values shift older generations up to ``backup_count``.
    """
    try:
        if not log_path.exists():
            return
        if log_path.stat().st_size <= max_bytes:
            return
        backup_count = _positive_int(
            backup_count,
            DEFAULT_LOG_BACKUP_COUNT,
            minimum=0,
        )
        if backup_count == 0:
            log_path.unlink()
            return
        oldest = _rotated_log_path(log_path, backup_count)
        try:
            if oldest.exists():
                oldest.unlink()
        except OSError:
            pass
        for generation in range(backup_count - 1, 0, -1):
            src = _rotated_log_path(log_path, generation)
            if not src.exists():
                continue
            try:
                src.rename(_rotated_log_path(log_path, generation + 1))
            except OSError:
                pass
        log_path.rename(_rotated_log_path(log_path, 1))
    except OSError:
        pass


def _module_hermes_argv() -> list[str]:
    """Return the interpreter-bound Hermes CLI invocation."""
    # ``hermes_cli.main`` is the console-script target declared in
    # pyproject.toml, NOT a top-level ``hermes`` package — there is no
    # ``hermes`` package to import.
    return [sys.executable, "-m", "hermes_cli.main"]


def _absolute_hermes_path(path: str) -> str:
    """Return an absolute filesystem path for a resolved Hermes shim."""
    expanded = os.path.expanduser(path)
    return expanded if os.path.isabs(expanded) else os.path.abspath(expanded)


def _looks_like_path(value: str) -> bool:
    """Return true when a command override is an explicit path, not a name."""
    expanded = os.path.expanduser(value)
    return (
        expanded.startswith("~")
        or os.path.isabs(expanded)
        or bool(os.path.dirname(expanded))
        or "\\" in expanded
        or bool(re.match(r"^[A-Za-z]:", expanded))
    )


def _is_windows_batch_shim(path: str) -> bool:
    """Return true for Windows shell/batch shims that should not be argv[0]."""
    return path.lower().endswith((".cmd", ".bat"))


def _path_search_names(command: str) -> list[str]:
    """Return executable names to try for an unqualified command."""
    if not _IS_WINDOWS or os.path.splitext(command)[1]:
        return [command]
    raw = os.environ.get("PATHEXT") or ".COM;.EXE;.BAT;.CMD"
    exts = [ext for ext in raw.split(";") if ext]
    return [command + ext for ext in exts]


def _safe_which_no_cwd(command: str) -> Optional[str]:
    """Resolve a bare command from PATH without implicit current-dir search.

    ``shutil.which`` follows platform search behavior. On Windows that can
    include the current directory before PATH for bare names, which is not a
    safe dispatcher primitive. This resolver only considers explicit PATH
    entries and skips empty / ``.`` entries.
    """
    path_env = os.environ.get("PATH", "")
    for raw_dir in path_env.split(os.pathsep):
        if not raw_dir or raw_dir == ".":
            continue
        directory = os.path.expanduser(raw_dir)
        for name in _path_search_names(command):
            candidate = os.path.join(directory, name)
            if not os.path.isfile(candidate):
                continue
            if _IS_WINDOWS or os.access(candidate, os.X_OK):
                return candidate
    return None


def _hermes_path_argv(path: str) -> list[str]:
    """Return argv for a resolved Hermes executable path.

    Windows batch shims (`.cmd` / `.bat`) are not safe as argv[0] for
    worker launches because the argument vector includes task-derived
    values. Prefer the interpreter-bound module form whenever the resolved
    executable is only a shell shim.
    """
    if _IS_WINDOWS and _is_windows_batch_shim(path):
        return _module_hermes_argv()
    return [_absolute_hermes_path(path)]


def _emit_worker_auto_heartbeat(
    *,
    task_id: str,
    run_id: Optional[int],
    claim_lock: Optional[str],
    board: Optional[str],
) -> bool:
    with contextlib.closing(connect(board=board)) as hb_conn:
        if claim_lock:
            heartbeat_claim(hb_conn, task_id, claimer=claim_lock)
        return heartbeat_worker(
            hb_conn,
            task_id,
            note="auto worker heartbeat",
            expected_run_id=run_id,
        )


def _worker_heartbeat_loop(
    *,
    task_id: str,
    run_id: Optional[int],
    claim_lock: Optional[str],
    board: Optional[str],
    worker_pid: int,
    interval_seconds: float,
    stop_event: threading.Event,
    heartbeat_fn=None,
    pid_alive_fn=None,
) -> None:
    heartbeat_fn = heartbeat_fn or _emit_worker_auto_heartbeat
    pid_alive_fn = pid_alive_fn or _pid_alive
    while not stop_event.wait(interval_seconds):
        if not pid_alive_fn(worker_pid):
            return
        try:
            heartbeat_fn(
                task_id=task_id,
                run_id=run_id,
                claim_lock=claim_lock,
                board=board,
            )
        except Exception:
            _log.warning(
                "kanban worker auto-heartbeat failed for %s pid=%s",
                task_id,
                worker_pid,
                exc_info=True,
            )


def _start_worker_heartbeat_loop(
    *,
    task_id: str,
    run_id: Optional[int],
    claim_lock: Optional[str],
    board: Optional[str],
    worker_pid: int,
    interval_seconds: Optional[float] = None,
) -> Optional[threading.Thread]:
    resolved_interval = (
        float(interval_seconds)
        if interval_seconds is not None
        else float(_worker_heartbeat_interval_seconds())
    )
    if resolved_interval <= 0:
        _log.info(
            "kanban worker auto-heartbeat disabled for %s (interval=%s)",
            task_id,
            resolved_interval,
        )
        return None
    stop_event = threading.Event()
    thread = threading.Thread(
        target=_worker_heartbeat_loop,
        kwargs={
            "task_id": task_id,
            "run_id": run_id,
            "claim_lock": claim_lock,
            "board": board,
            "worker_pid": int(worker_pid),
            "interval_seconds": resolved_interval,
            "stop_event": stop_event,
        },
        name=f"kanban-heartbeat-{task_id}",
        daemon=True,
    )
    thread.start()
    return thread


def _resolve_hermes_argv() -> list[str]:
    """Resolve the ``hermes`` invocation as argv parts for ``Popen``.

    Tries in order:

    1. ``$HERMES_BIN`` — explicit operator override. Path-like values are
       normalized to absolute paths; bare command names keep normal PATH
       semantics and never prefer a same-directory file before ``PATH``.
    2. ``shutil.which("hermes")`` — the console-script shim, normalized to
       an absolute path. On Windows, ``which`` can return a relative
       ``.\\hermes.CMD`` when the current directory is on ``PATH``; directly
       launching batch shims is also unsafe with task-derived argv. The
       dispatcher therefore falls back to the interpreter-bound module form
       for implicit ``.cmd`` / ``.bat`` shims.
    3. ``sys.executable -m hermes_cli.main`` — fallback for setups where
       Hermes is launched from a venv and the ``hermes`` shim is not on
       the dispatcher's ``$PATH`` (cron, systemd ``User=`` services,
       launchd jobs, detached processes, etc.). Goes through the running
       interpreter so the result is independent of ``$PATH``.

    Mirrors ``gateway.run._resolve_hermes_bin`` for the same reason. Kept
    local (not imported from gateway) because ``hermes_cli`` sits below
    ``gateway`` in the dependency order.
    """
    import shutil

    env_bin = os.environ.get("HERMES_BIN", "").strip()
    if env_bin:
        if _looks_like_path(env_bin):
            return _hermes_path_argv(env_bin)
        resolved_env_bin = _safe_which_no_cwd(env_bin)
        if resolved_env_bin:
            return _hermes_path_argv(resolved_env_bin)
        return _module_hermes_argv()

    hermes_bin = _safe_which_no_cwd("hermes") if _IS_WINDOWS else shutil.which("hermes")
    if hermes_bin:
        return _hermes_path_argv(hermes_bin)
    return _module_hermes_argv()


def _kanban_worker_skill_available(hermes_home: Optional[str]) -> bool:
    """True if the bundled ``kanban-worker`` skill resolves for the home the
    spawned worker will run under.

    The dispatcher injects ``--skills kanban-worker`` into every worker. When
    the worker activates a profile (``hermes -p <name>``), its ``SKILLS_DIR``
    becomes ``<profile_home>/skills`` — which on many profiles does NOT contain
    the bundled skill (it ships in the *default* root home, not every
    profile-scoped skills dir). Preloading a missing skill is fatal at CLI
    startup (``ValueError: Unknown skill(s): kanban-worker``), aborting the
    worker before the agent loop runs. Gate the flag on actual resolvability;
    the kanban lifecycle contract is still injected via ``KANBAN_GUIDANCE``, so
    omitting the flag only drops the supplementary pattern library.
    """
    from pathlib import Path as _Path

    # An unset HERMES_HOME means the worker falls back to the default root
    # home (``~/.hermes``), which ships the bundled skill.
    base = _Path(hermes_home) if hermes_home else (_Path.home() / ".hermes")
    skills_root = base / "skills"
    if not skills_root.is_dir():
        return False
    # Canonical bundled location first (cheap), then a bounded scan for
    # profiles that have it nested elsewhere.
    if (skills_root / "devops" / "kanban-worker" / "SKILL.md").is_file():
        return True
    try:
        for skill_md in skills_root.rglob("kanban-worker/SKILL.md"):
            if skill_md.is_file():
                return True
    except OSError:
        pass
    return False


def _worker_terminal_timeout_env(
    max_runtime_seconds: Optional[int],
    current_timeout: Optional[str],
) -> Optional[str]:
    """Return a worker-scoped TERMINAL_TIMEOUT override, if needed.

    Kanban's ``max_runtime_seconds`` bounds the whole worker attempt. The
    terminal tool has its own default timeout via ``TERMINAL_TIMEOUT``; when
    the worker runtime is longer, raise only the child process default so a
    long command is not killed by the generic terminal default first.
    """
    if max_runtime_seconds is None:
        return None
    try:
        runtime = int(max_runtime_seconds)
    except (TypeError, ValueError):
        return None
    if runtime <= 0:
        return None

    desired = max(1, runtime - KANBAN_TERMINAL_TIMEOUT_GRACE_SECONDS)
    try:
        existing = int(str(current_timeout).strip()) if current_timeout else 0
    except (TypeError, ValueError):
        existing = 0
    if existing >= desired:
        return None
    return str(desired)


def _default_spawn(
    task: Task,
    workspace: str,
    *,
    board: Optional[str] = None,
) -> Optional[int]:
    """Fire-and-forget ``hermes -p <profile> chat -q ...`` subprocess.

    Returns the spawned child's PID so the dispatcher can detect crashes
    before the claim TTL expires. The child's completion is still observed
    via the ``complete`` / ``block`` transitions the worker writes itself;
    the PID check is a safety net for crashes, OOM kills, and Ctrl+C.

    ``board`` pins the child's kanban context to that board: the child's
    ``HERMES_KANBAN_DB`` / ``HERMES_KANBAN_BOARD`` / workspaces_root env
    vars all resolve to the same board the dispatcher claimed the task
    from. Workers cannot accidentally see other boards.
    """
    import subprocess
    if not task.assignee:
        raise ValueError(f"task {task.id} has no assignee")

    from hermes_cli.profiles import normalize_profile_name

    profile_arg = normalize_profile_name(task.assignee)

    prompt = f"work kanban task {task.id}"
    env = dict(os.environ)

    # Inject HERMES_HOME so the worker reads the profile-scoped config.yaml
    # (fallback_providers, toolsets, agent settings, etc.) instead of the root
    # config.  Without this, `env = dict(os.environ)` copies only the parent's
    # env, and when the child process starts `hermes -p <name>` the
    # _apply_profile_override() runs *before* hermes_constants is imported.
    # If HERMES_HOME is absent from the child's env, get_hermes_home() falls
    # back to Path.home() / ".hermes" (the DEFAULT profile root), ignoring the
    # profile-specific config entirely.  Fixes profile-scoped fallback_providers
    # being invisible to kanban workers.
    from hermes_cli.profiles import resolve_profile_env
    try:
        env["HERMES_HOME"] = resolve_profile_env(profile_arg)
    except FileNotFoundError:
        # Profile dir doesn't exist — defer resolution to the CLI's
        # _apply_profile_override() via HERMES_PROFILE (set below).
        # This only happens in test fixtures where the isolated
        # HERMES_HOME never had profiles created.
        pass
    if task.tenant:
        env["HERMES_TENANT"] = task.tenant
    env["HERMES_KANBAN_TASK"] = task.id
    env["HERMES_KANBAN_WORKSPACE"] = workspace
    if task.branch_name:
        env["HERMES_KANBAN_BRANCH"] = task.branch_name
    env["HERMES_KANBAN_WORKSPACE_KIND"] = task.workspace_kind or "scratch"
    if task.current_run_id is not None:
        env["HERMES_KANBAN_RUN_ID"] = str(task.current_run_id)
    if task.claim_lock:
        env["HERMES_KANBAN_CLAIM_LOCK"] = task.claim_lock
    terminal_timeout = _worker_terminal_timeout_env(
        task.max_runtime_seconds,
        env.get("TERMINAL_TIMEOUT"),
    )
    if terminal_timeout is not None:
        env["TERMINAL_TIMEOUT"] = terminal_timeout
    foreground_timeout = _worker_terminal_timeout_env(
        task.max_runtime_seconds,
        env.get("TERMINAL_MAX_FOREGROUND_TIMEOUT"),
    )
    if foreground_timeout is not None:
        env["TERMINAL_MAX_FOREGROUND_TIMEOUT"] = foreground_timeout
    # Pin the shared board + workspaces root the dispatcher resolved, so
    # that even when the worker activates a profile (`hermes -p <name>`
    # rewrites HERMES_HOME), its kanban paths still match the
    # dispatcher's. Belt-and-braces with the `get_default_hermes_root()`
    # resolution in `kanban_home()` — symmetric resolution is the norm,
    # but unusual symlink / Docker layouts are caught here too.
    env["HERMES_KANBAN_DB"] = str(kanban_db_path(board=board))
    env["HERMES_KANBAN_WORKSPACES_ROOT"] = str(workspaces_root(board=board))
    # Board slug — the final defense-in-depth pin. If the worker ever
    # resolves kanban paths without the DB / workspaces env vars, the
    # board slug still forces it to the right directory.
    resolved_board = _normalize_board_slug(board) or get_current_board()
    env["HERMES_KANBAN_BOARD"] = resolved_board
    # HERMES_PROFILE is the author the kanban_comment tool defaults to.
    # `hermes -p <assignee>` activates the profile, but the env var is
    # what the tool reads — set it explicitly here so comments are
    # attributed correctly regardless of how the child loads config.
    env["HERMES_PROFILE"] = profile_arg
    declared_toolsets, allowed_tool_errors = _validate_scope_allowed_tools(task.body)
    if not allowed_tool_errors and declared_toolsets:
        effective_toolsets = _resolve_scope_runtime_tool_schema_names(
            declared_toolsets,
            task_id=task.id,
        )
        if effective_toolsets:
            env["HERMES_KANBAN_EFFECTIVE_TOOLSETS"] = json.dumps(effective_toolsets)

    cmd = [
        *_resolve_hermes_argv(),
        "-p", profile_arg,
        # Worker subprocesses switch to a profile-scoped HERMES_HOME above,
        # so they see that profile's shell-hook allowlist instead of the
        # dispatcher's root allowlist. Pass --accept-hooks explicitly so
        # profile-local worker sessions still register configured hooks.
        "--accept-hooks",
    ]
    # Per-task force-loaded skills. Each name goes in its own
    # `--skills X` pair rather than a single comma-joined arg: the CLI
    # accepts both forms (action='append' + comma-split), but
    # per-name pairs are easier to read in `ps` output and avoid any
    # quoting ambiguity if a skill name ever contains unusual chars.
    # Do not force-load the old built-in `kanban-worker` skill here:
    # profile-scoped + global skill bundles can collide and make the
    # child exit before it can block its task. Mandatory Kanban lifecycle
    # guidance is injected through KANBAN_GUIDANCE instead.
    if task.skills:
        for sk in task.skills:
            if sk and sk != "kanban-worker":
                cmd.extend(["--skills", sk])
    if task.model_override:
        cmd.extend(["-m", task.model_override])
    cmd.extend([
        "chat",
        "-q", prompt,
    ])
    # Redirect output to a per-task log under <board-root>/logs/.
    # Anchored at the board root (not the shared kanban root), so
    # `hermes kanban log` on a specific board reads its own file and
    # logs don't collide across boards that happen to share task ids.
    log_dir = worker_logs_dir(board=board)
    log_dir.mkdir(parents=True, exist_ok=True)
    log_path = log_dir / f"{task.id}.log"
    rotate_bytes, backup_count = worker_log_rotation_config()
    _rotate_worker_log(log_path, rotate_bytes, backup_count)

    # Use 'a' so a re-run on unblock appends rather than overwrites.
    log_f = open(log_path, "ab")
    try:
        proc = subprocess.Popen(  # noqa: S603 -- argv is a fixed list built above
            cmd,
            cwd=workspace if os.path.isdir(workspace) else None,
            stdin=subprocess.DEVNULL,
            stdout=log_f,
            stderr=subprocess.STDOUT,
            env=env,
            start_new_session=True,
            creationflags=subprocess.CREATE_NO_WINDOW if _IS_WINDOWS else 0,
        )
    except FileNotFoundError:
        log_f.close()
        raise RuntimeError(
            "`hermes` executable not found on PATH. "
            "Install Hermes Agent or activate its venv before running the kanban dispatcher."
        )
    # NOTE: we intentionally do NOT close log_f here — we want Popen's
    # child process to keep writing after this function returns.  The
    # handle is kept alive by the child's inheritance.  The parent's
    # reference goes out of scope and is GC'd, but the OS-level FD stays
    # open in the child until the child exits.
    try:
        _start_worker_heartbeat_loop(
            task_id=task.id,
            run_id=task.current_run_id,
            claim_lock=task.claim_lock,
            board=resolved_board,
            worker_pid=int(proc.pid),
        )
    except Exception:
        _log.warning(
            "kanban worker auto-heartbeat loop failed to start for %s",
            task.id,
            exc_info=True,
        )
    return proc.pid


# ---------------------------------------------------------------------------
# Long-lived dispatcher daemon
# ---------------------------------------------------------------------------

def run_daemon(
    *,
    interval: float = 60.0,
    max_spawn: Optional[int] = None,
    failure_limit: int = DEFAULT_SPAWN_FAILURE_LIMIT,
    stop_event=None,
    on_tick=None,
) -> None:
    """Run the dispatcher in a loop until interrupted.

    Calls :func:`dispatch_once` every ``interval`` seconds. Exits cleanly
    on SIGINT / SIGTERM so ``hermes kanban daemon`` is systemd-friendly.
    ``stop_event`` (a :class:`threading.Event`) and ``on_tick`` (a
    callable receiving the :class:`DispatchResult`) are test hooks.
    """
    import signal
    import threading

    if stop_event is None:
        stop_event = threading.Event()

    def _handle(_signum, _frame):
        stop_event.set()

    # Install handlers only when running on the main thread — tests call
    # this inline from worker threads and signal() would raise there.
    if threading.current_thread() is threading.main_thread():
        for sig_name in ("SIGINT", "SIGTERM"):
            sig = getattr(signal, sig_name, None)
            if sig is not None:
                try:
                    signal.signal(sig, _handle)
                except (ValueError, OSError):
                    pass

    while not stop_event.is_set():
        try:
            with contextlib.closing(connect()) as conn:
                res = dispatch_once(
                    conn,
                    max_spawn=max_spawn,
                    failure_limit=failure_limit,
                )
            if on_tick is not None:
                try:
                    on_tick(res)
                except Exception:
                    pass
        except Exception:
            # Don't let any single tick kill the daemon.
            import traceback
            traceback.print_exc()
        stop_event.wait(timeout=interval)


# ---------------------------------------------------------------------------
# Worker context builder (what a spawned worker sees)
# ---------------------------------------------------------------------------

def build_worker_context(conn: sqlite3.Connection, task_id: str) -> str:
    """Return the full text a worker should read to understand its task.

    Order:
      1. Task title (mandatory).
      2. Task body (optional opening post, capped at 8 KB).
      3. Prior attempts on THIS task (most recent ``_CTX_MAX_PRIOR_ATTEMPTS``
         shown; older attempts collapsed into a one-line summary).
         Each attempt's ``summary`` / ``error`` / ``metadata`` capped at
         ``_CTX_MAX_FIELD_BYTES`` each.
      4. Structured handoff results of every done parent task. Prefers
         ``run.summary`` / ``run.metadata`` when the parent was executed
         via a run; falls back to ``task.result`` for older data. Same
         per-field cap.
      5. Cross-task role history for the assignee (most recent 5
         completed runs on other tasks).
      6. Comment thread (most recent ``_CTX_MAX_COMMENTS`` shown, older
         collapsed).

    All caps exist so worker prompts stay bounded even on pathological
    boards (retry-heavy tasks, comment storms). The per-field char cap
    prevents a single 1 MB summary from dominating context.
    """
    task = get_task(conn, task_id)
    if not task:
        raise ValueError(f"unknown task {task_id}")

    def _cap(s: Optional[str], limit: int = _CTX_MAX_FIELD_BYTES) -> str:
        """Truncate a string to `limit` chars with a visible ellipsis."""
        if not s:
            return ""
        s = s.strip()
        if len(s) <= limit:
            return s
        return s[:limit] + f"… [truncated, {len(s) - limit} chars omitted]"

    lines: list[str] = []
    lines.append(f"# Kanban task {task.id}: {task.title}")
    lines.append("")
    lines.append(f"Assignee: {task.assignee or '(unassigned)'}")
    lines.append(f"Status:   {task.status}")
    if task.tenant:
        lines.append(f"Tenant:   {task.tenant}")
    lines.append(f"Workspace: {task.workspace_kind} @ {task.workspace_path or '(unresolved)'}")
    if task.max_runtime_seconds is not None:
        terminal_timeout = _worker_terminal_timeout_env(
            task.max_runtime_seconds,
            os.environ.get("TERMINAL_TIMEOUT"),
        )
        effective_terminal_timeout = terminal_timeout or os.environ.get("TERMINAL_TIMEOUT")
        lines.append(f"Max runtime: {task.max_runtime_seconds}s")
        if effective_terminal_timeout:
            lines.append(f"Terminal timeout: {effective_terminal_timeout}s")
    if task.branch_name:
        lines.append(f"Branch:   {task.branch_name}")
    lines.append("")

    if task.body and task.body.strip():
        lines.append("## Body")
        lines.append(_cap(task.body, _CTX_MAX_BODY_BYTES))
        lines.append("")

    # Prior attempts — show closed runs so a retrying worker sees the
    # history. Skip the currently-active run (that's this worker).
    # Cap at _CTX_MAX_PRIOR_ATTEMPTS most-recent closed runs; older
    # attempts get collapsed into a one-line marker so the worker knows
    # more exist without bloating the prompt.
    all_prior = [r for r in list_runs(conn, task_id) if r.ended_at is not None]
    # list_runs returns ascending by started_at; "most recent" = last N
    if len(all_prior) > _CTX_MAX_PRIOR_ATTEMPTS:
        omitted = len(all_prior) - _CTX_MAX_PRIOR_ATTEMPTS
        shown = all_prior[-_CTX_MAX_PRIOR_ATTEMPTS:]
        first_shown_idx = omitted + 1
    else:
        omitted = 0
        shown = all_prior
        first_shown_idx = 1
    if shown:
        lines.append("## Prior attempts on this task")
        if omitted:
            lines.append(
                f"_({omitted} earlier attempt{'s' if omitted != 1 else ''} "
                f"omitted; showing most recent {len(shown)})_"
            )
        for offset, run in enumerate(shown):
            idx = first_shown_idx + offset
            ts = time.strftime("%Y-%m-%d %H:%M", time.localtime(run.started_at))
            profile = run.profile or "(unknown)"
            outcome = run.outcome or run.status
            lines.append(f"### Attempt {idx} — {outcome} ({profile}, {ts})")
            if run.summary and run.summary.strip():
                lines.append(_cap(run.summary))
            if run.error and run.error.strip():
                lines.append(f"_error_: {_cap(run.error)}")
            if run.metadata:
                try:
                    meta_str = json.dumps(run.metadata, ensure_ascii=False, sort_keys=True)
                    lines.append(f"_metadata_: `{_cap(meta_str)}`")
                except Exception:
                    pass
            lines.append("")

    # Parents: prefer the most-recent 'completed' run's summary + metadata,
    # fall back to ``task.result`` when no run rows exist (legacy DBs,
    # or tasks completed before the runs table landed).
    parent_rows = conn.execute(
        "SELECT parent_id FROM task_links WHERE child_id = ? ORDER BY parent_id",
        (task_id,),
    ).fetchall()
    parent_ids = [r["parent_id"] for r in parent_rows]

    if parent_ids:
        wrote_header = False
        for pid in parent_ids:
            pt = get_task(conn, pid)
            if not pt or pt.status != "done":
                continue
            runs = [r for r in list_runs(conn, pid) if r.outcome == "completed"]
            runs.sort(key=lambda r: r.started_at, reverse=True)
            run = runs[0] if runs else None

            if not wrote_header:
                lines.append("## Parent task results")
                wrote_header = True
            lines.append(f"### {pid}")

            body_lines: list[str] = []
            if run is not None and run.summary and run.summary.strip():
                body_lines.append(_cap(run.summary))
            elif pt.result:
                body_lines.append(_cap(pt.result))
            else:
                body_lines.append("(no result recorded)")

            if run is not None and run.metadata:
                try:
                    meta_str = json.dumps(run.metadata, ensure_ascii=False, sort_keys=True)
                    body_lines.append(f"_metadata_: `{_cap(meta_str)}`")
                except Exception:
                    pass
            lines.extend(body_lines)
            lines.append("")

    # Cross-task role history: what else has THIS assignee completed
    # recently? Gives the worker implicit continuity — "I'm the reviewer
    # and my last three reviews focused on security" — without forcing
    # the user to wire anything into SOUL.md / MEMORY.md. Bounded to the
    # most recent 5 completed runs, excluding this task so the retry
    # section above isn't duplicated. Safe on assignee=None (skipped).
    if task.assignee:
        role_rows = conn.execute(
            "SELECT t.id, t.title, r.summary, r.ended_at "
            "FROM task_runs r JOIN tasks t ON r.task_id = t.id "
            "WHERE r.profile = ? AND r.task_id != ? "
            "  AND r.outcome = 'completed' "
            "ORDER BY r.ended_at DESC LIMIT 5",
            (task.assignee, task_id),
        ).fetchall()
        if role_rows:
            lines.append(f"## Recent work by @{task.assignee}")
            for row in role_rows:
                ts = time.strftime(
                    "%Y-%m-%d %H:%M", time.localtime(int(row["ended_at"]))
                )
                s = (row["summary"] or "").strip().splitlines()
                first = s[0][:200] if s else "(no summary)"
                lines.append(f"- {row['id']} — {row['title']} ({ts}): {first}")
            lines.append("")

    # Comments: cap at the most-recent _CTX_MAX_COMMENTS so
    # comment-storm tasks don't blow out the worker's prompt. Older
    # comments summarised in a one-line marker like prior attempts.
    all_comments = list_comments(conn, task_id)
    if len(all_comments) > _CTX_MAX_COMMENTS:
        omitted_c = len(all_comments) - _CTX_MAX_COMMENTS
        shown_c = all_comments[-_CTX_MAX_COMMENTS:]
    else:
        omitted_c = 0
        shown_c = all_comments
    if shown_c:
        lines.append("## Comment thread")
        if omitted_c:
            lines.append(
                f"_({omitted_c} earlier comment{'s' if omitted_c != 1 else ''} "
                f"omitted; showing most recent {len(shown_c)})_"
            )
        for c in shown_c:
            ts = time.strftime("%Y-%m-%d %H:%M", time.localtime(c.created_at))
            # Render author with explicit "comment from worker" framing so
            # operator-controlled HERMES_PROFILE values like "hermes-system"
            # or "operator" can't be misread by the next worker as a system
            # directive above the (attacker-influenceable) comment body.
            # Defense-in-depth — the LLM-controlled author-forgery surface
            # was already closed in #22435. See #22452.
            safe_author = (c.author or "").replace("`", "")
            lines.append(f"comment from worker `{safe_author}` at {ts}:")
            lines.append(_cap(c.body, _CTX_MAX_COMMENT_BYTES))
            lines.append("")

    return "\n".join(lines).rstrip() + "\n"


# ---------------------------------------------------------------------------
# Stats + SLA helpers
# ---------------------------------------------------------------------------

def board_stats(conn: sqlite3.Connection) -> dict:
    """Per-status + per-assignee counts, plus the oldest ``ready`` age in
    seconds (the clearest staleness signal for a router or HUD).
    """
    by_status: dict[str, int] = {}
    for row in conn.execute(
        "SELECT status, COUNT(*) AS n FROM tasks "
        "WHERE status != 'archived' GROUP BY status"
    ):
        by_status[row["status"]] = int(row["n"])

    by_assignee: dict[str, dict[str, int]] = {}
    for row in conn.execute(
        "SELECT assignee, status, COUNT(*) AS n FROM tasks "
        "WHERE status != 'archived' AND assignee IS NOT NULL "
        "GROUP BY assignee, status"
    ):
        by_assignee.setdefault(row["assignee"], {})[row["status"]] = int(row["n"])

    oldest_row = conn.execute(
        "SELECT MIN(created_at) AS ts FROM tasks WHERE status = 'ready'"
    ).fetchone()
    now = int(time.time())
    oldest_ready_age = (
        (now - int(oldest_row["ts"]))
        if oldest_row and oldest_row["ts"] is not None else None
    )

    return {
        "by_status": by_status,
        "by_assignee": by_assignee,
        "oldest_ready_age_seconds": oldest_ready_age,
        "now": now,
    }


def _to_epoch(val) -> Optional[int]:
    """Normalise a timestamp to unix epoch seconds.

    Accepts ints (pass-through), numeric strings, and ISO-8601 strings.
    Returns ``None`` for ``None`` / empty values.
    """
    if val is None:
        return None
    if isinstance(val, int):
        return val
    if isinstance(val, float):
        return int(val)
    s = str(val).strip()
    if not s:
        return None
    try:
        return int(s)
    except ValueError:
        pass
    # ISO-8601 fallback (e.g. '2026-05-10T15:00:00Z')
    try:
        from datetime import datetime, timezone
        dt = datetime.fromisoformat(s.replace("Z", "+00:00"))
        return int(dt.timestamp())
    except (ValueError, OSError):
        return None


def task_age(task: Task) -> dict:
    """Return age metrics for a single task. All values are seconds or None."""
    now = int(time.time())
    _c = _to_epoch(task.created_at)
    _s = _to_epoch(task.started_at)
    _co = _to_epoch(task.completed_at)
    age_since_created = now - _c if _c is not None else None
    age_since_started = now - _s if _s is not None else None
    time_to_complete = (
        _co - (_s or _c) if _co is not None else None
    )
    return {
        "created_age_seconds": age_since_created,
        "started_age_seconds": age_since_started,
        "time_to_complete_seconds": time_to_complete,
    }


# ---------------------------------------------------------------------------
# Notification subscriptions (used by the gateway kanban-notifier)
# ---------------------------------------------------------------------------

def add_notify_sub(
    conn: sqlite3.Connection,
    *,
    task_id: str,
    platform: str,
    chat_id: str,
    thread_id: Optional[str] = None,
    user_id: Optional[str] = None,
    notifier_profile: Optional[str] = None,
) -> None:
    """Register a gateway source that wants terminal-state notifications
    for ``task_id``. Idempotent on (task, platform, chat, thread)."""
    now = int(time.time())
    with write_txn(conn):
        conn.execute(
            """
            INSERT OR IGNORE INTO kanban_notify_subs
                (task_id, platform, chat_id, thread_id, user_id, notifier_profile, created_at)
            VALUES (?, ?, ?, ?, ?, ?, ?)
            """,
            (task_id, platform, chat_id, thread_id or "", user_id, notifier_profile, now),
        )
        if notifier_profile:
            # Self-heal legacy rows that predate notifier ownership by
            # backfilling only when the existing value is unset.
            conn.execute(
                """
                UPDATE kanban_notify_subs
                   SET notifier_profile = ?
                 WHERE task_id = ? AND platform = ? AND chat_id = ? AND thread_id = ?
                   AND (notifier_profile IS NULL OR notifier_profile = '')
                """,
                (notifier_profile, task_id, platform, chat_id, thread_id or ""),
            )


def list_notify_subs(
    conn: sqlite3.Connection, task_id: Optional[str] = None,
) -> list[dict]:
    if task_id is not None:
        rows = conn.execute(
            "SELECT * FROM kanban_notify_subs WHERE task_id = ?", (task_id,),
        ).fetchall()
    else:
        rows = conn.execute("SELECT * FROM kanban_notify_subs").fetchall()
    return [dict(r) for r in rows]


def remove_notify_sub(
    conn: sqlite3.Connection,
    *,
    task_id: str,
    platform: str,
    chat_id: str,
    thread_id: Optional[str] = None,
) -> bool:
    with write_txn(conn):
        cur = conn.execute(
            "DELETE FROM kanban_notify_subs WHERE task_id = ? "
            "AND platform = ? AND chat_id = ? AND thread_id = ?",
            (task_id, platform, chat_id, thread_id or ""),
        )
    return cur.rowcount > 0


def unseen_events_for_sub(
    conn: sqlite3.Connection,
    *,
    task_id: str,
    platform: str,
    chat_id: str,
    thread_id: Optional[str] = None,
    kinds: Optional[Iterable[str]] = None,
) -> tuple[int, list[Event]]:
    """Return ``(new_cursor, events)`` for a given subscription.

    Only events with ``id > last_event_id`` are returned. The subscription's
    cursor is NOT advanced here; call :func:`advance_notify_cursor` after
    the gateway has successfully delivered the notifications.
    """
    row = conn.execute(
        "SELECT last_event_id FROM kanban_notify_subs "
        "WHERE task_id = ? AND platform = ? AND chat_id = ? AND thread_id = ?",
        (task_id, platform, chat_id, thread_id or ""),
    ).fetchone()
    if row is None:
        return 0, []
    cursor = int(row["last_event_id"])
    kind_list = list(kinds) if kinds else None
    q = (
        "SELECT * FROM task_events WHERE task_id = ? AND id > ? "
        + ("AND kind IN (" + ",".join("?" * len(kind_list)) + ") " if kind_list else "")
        + "ORDER BY id ASC"
    )
    params: list[Any] = [task_id, cursor]
    if kind_list:
        params.extend(kind_list)
    rows = conn.execute(q, params).fetchall()
    out: list[Event] = []
    max_id = cursor
    for r in rows:
        try:
            payload = json.loads(r["payload"]) if r["payload"] else None
        except Exception:
            payload = None
        out.append(Event(
            id=r["id"], task_id=r["task_id"], kind=r["kind"],
            payload=payload, created_at=r["created_at"],
            run_id=(int(r["run_id"]) if "run_id" in r.keys() and r["run_id"] is not None else None),
        ))
        max_id = max(max_id, int(r["id"]))
    return max_id, out


def claim_unseen_events_for_sub(
    conn: sqlite3.Connection,
    *,
    task_id: str,
    platform: str,
    chat_id: str,
    thread_id: Optional[str] = None,
    kinds: Optional[Iterable[str]] = None,
) -> tuple[int, int, list[Event]]:
    """Atomically claim unseen notification events for one subscription.

    Returns ``(old_cursor, new_cursor, events)``. When events are returned,
    ``kanban_notify_subs.last_event_id`` has already been advanced to
    ``new_cursor`` inside a ``BEGIN IMMEDIATE`` transaction. That makes the
    notifier's read/claim step single-owner across multiple gateway watcher
    processes pointed at the same board DB: concurrent watchers serialize on
    SQLite's writer lock, and only the first process sees and claims a given
    event range.

    Callers should send the claimed events, then either leave the cursor at
    ``new_cursor`` on success or call :func:`rewind_notify_cursor` if delivery
    failed before any terminal unsubscribe removed the row.
    """
    with write_txn(conn):
        row = conn.execute(
            "SELECT last_event_id FROM kanban_notify_subs "
            "WHERE task_id = ? AND platform = ? AND chat_id = ? AND thread_id = ?",
            (task_id, platform, chat_id, thread_id or ""),
        ).fetchone()
        if row is None:
            return 0, 0, []
        old_cursor = int(row["last_event_id"])
        new_cursor, events = unseen_events_for_sub(
            conn,
            task_id=task_id,
            platform=platform,
            chat_id=chat_id,
            thread_id=thread_id,
            kinds=kinds,
        )
        if not events:
            return old_cursor, old_cursor, []
        conn.execute(
            "UPDATE kanban_notify_subs SET last_event_id = ? "
            "WHERE task_id = ? AND platform = ? AND chat_id = ? AND thread_id = ? "
            "AND last_event_id = ?",
            (int(new_cursor), task_id, platform, chat_id, thread_id or "", int(old_cursor)),
        )
        return old_cursor, new_cursor, events


def advance_notify_cursor(
    conn: sqlite3.Connection,
    *,
    task_id: str,
    platform: str,
    chat_id: str,
    thread_id: Optional[str] = None,
    new_cursor: int,
) -> None:
    with write_txn(conn):
        conn.execute(
            "UPDATE kanban_notify_subs SET last_event_id = ? "
            "WHERE task_id = ? AND platform = ? AND chat_id = ? AND thread_id = ?",
            (int(new_cursor), task_id, platform, chat_id, thread_id or ""),
        )


def rewind_notify_cursor(
    conn: sqlite3.Connection,
    *,
    task_id: str,
    platform: str,
    chat_id: str,
    thread_id: Optional[str] = None,
    claimed_cursor: int,
    old_cursor: int,
) -> bool:
    """Undo a notification claim when delivery fails.

    The CAS guard only rewinds if no later notifier advanced the row after our
    claim. This keeps retry behavior for transient send failures without
    clobbering newer progress.
    """
    with write_txn(conn):
        cur = conn.execute(
            "UPDATE kanban_notify_subs SET last_event_id = ? "
            "WHERE task_id = ? AND platform = ? AND chat_id = ? AND thread_id = ? "
            "AND last_event_id = ?",
            (
                int(old_cursor), task_id, platform, chat_id, thread_id or "",
                int(claimed_cursor),
            ),
        )
    return cur.rowcount > 0


# ---------------------------------------------------------------------------
# Retention + garbage collection
# ---------------------------------------------------------------------------

def gc_events(
    conn: sqlite3.Connection, *, older_than_seconds: int = 30 * 24 * 3600,
) -> int:
    """Delete task_events rows older than ``older_than_seconds`` for tasks
    in a terminal state (``done`` or ``archived``). Returns the number of
    rows deleted. Running / ready / blocked tasks keep their full event
    history."""
    cutoff = int(time.time()) - int(older_than_seconds)
    with write_txn(conn):
        cur = conn.execute(
            "DELETE FROM task_events WHERE created_at < ? AND task_id IN "
            "(SELECT id FROM tasks WHERE status IN ('done', 'archived'))",
            (cutoff,),
        )
    return int(cur.rowcount or 0)


def gc_worker_logs(
    *, older_than_seconds: int = 30 * 24 * 3600,
    board: Optional[str] = None,
) -> int:
    """Delete worker log files older than ``older_than_seconds``. Returns
    the number of files removed. Kept separate from ``gc_events`` because
    log files live on disk, not in SQLite. Scoped to ``board`` (defaults
    to the active board) — per-board isolation means deleting logs from
    board A cannot touch board B's logs."""
    log_dir = worker_logs_dir(board=board)
    if not log_dir.exists():
        return 0
    cutoff = time.time() - older_than_seconds
    removed = 0
    for p in log_dir.iterdir():
        try:
            if p.is_file() and p.stat().st_mtime < cutoff:
                p.unlink()
                removed += 1
        except OSError:
            continue
    return removed


# ---------------------------------------------------------------------------
# Worker log accessor
# ---------------------------------------------------------------------------

def worker_log_path(task_id: str, *, board: Optional[str] = None) -> Path:
    """Return the path to a worker's log file. The file may not exist
    (task never spawned, or log already GC'd).

    When ``board`` is None, resolves via the active board (env var →
    current-board file → default). The dispatcher always passes the
    board explicitly to avoid any resolution ambiguity when multiple
    boards exist."""
    return worker_logs_dir(board=board) / f"{task_id}.log"


def read_worker_log(
    task_id: str, *, tail_bytes: Optional[int] = None,
    board: Optional[str] = None,
) -> Optional[str]:
    """Read the worker log for ``task_id``. Returns None if the file
    doesn't exist. If ``tail_bytes`` is set, only the last N bytes are
    returned (useful for the dashboard drawer which shouldn't page megabytes)."""
    path = worker_log_path(task_id, board=board)
    if not path.exists():
        return None
    try:
        if tail_bytes is None:
            return path.read_text(encoding="utf-8", errors="replace")
        size = path.stat().st_size
        with open(path, "rb") as f:
            if size > tail_bytes:
                f.seek(size - tail_bytes)
                # Skip a partial line if we tailed mid-line. But if the
                # window has no newline at all (one giant log line),
                # readline() would eat everything — in that case don't
                # skip and return the raw tail.
                probe = f.tell()
                partial = f.readline()
                if not partial.endswith(b"\n") and f.tell() >= size:
                    f.seek(probe)
            data = f.read()
        return data.decode("utf-8", errors="replace")
    except OSError:
        return None


# ---------------------------------------------------------------------------
# Assignee enumeration (known profiles + per-profile board stats)
# ---------------------------------------------------------------------------

def list_profiles_on_disk() -> list[str]:
    """Return the set of assignee/profile names discovered on disk.

    Includes:
    - named profiles under ``<default-root>/profiles/<name>/config.yaml``
    - the implicit ``default`` profile when the default Hermes root exists

    Reads profile paths directly so this module has no import dependency on
    ``hermes_cli.profiles`` (which pulls in a large chunk of the CLI startup
    path).
    """
    try:
        from hermes_constants import get_default_hermes_root
        default_root = get_default_hermes_root()
        profiles_dir = default_root / "profiles"
    except Exception:
        return []

    names: set[str] = set()
    if default_root.exists():
        names.add("default")

    if profiles_dir.is_dir():
        try:
            for entry in sorted(profiles_dir.iterdir()):
                if not entry.is_dir():
                    continue
                if (entry / "config.yaml").is_file():
                    names.add(entry.name)
        except OSError:
            pass

    return sorted(names)


# ---------------------------------------------------------------------------
# Transactional profile config mutation (review-required deadlock recovery)
# ---------------------------------------------------------------------------

PROFILE_MODEL_CONFIG_KEYS = frozenset({"model.default", "model.provider"})


def _nested_get(data: Mapping[str, Any], dotted_key: str) -> Any:
    cur: Any = data
    for part in dotted_key.split("."):
        if not isinstance(cur, Mapping) or part not in cur:
            return None
        cur = cur[part]
    return cur


def _nested_set(data: dict[str, Any], dotted_key: str, value: Any) -> None:
    cur: dict[str, Any] = data
    parts = dotted_key.split(".")
    for part in parts[:-1]:
        nxt = cur.get(part)
        if nxt is None:
            nxt = {}
            cur[part] = nxt
        if not isinstance(nxt, dict):
            raise ValueError(
                f"cannot set {dotted_key!r}: {part!r} exists but is not a mapping"
            )
        cur = nxt
    cur[parts[-1]] = value


def _flatten_config(data: Mapping[str, Any], prefix: str = "") -> dict[str, Any]:
    out: dict[str, Any] = {}
    for key, value in data.items():
        dotted = f"{prefix}.{key}" if prefix else str(key)
        if isinstance(value, Mapping):
            out.update(_flatten_config(value, dotted))
        else:
            out[dotted] = value
    return out


def _changed_config_keys(before: Mapping[str, Any], after: Mapping[str, Any]) -> list[str]:
    b = _flatten_config(before)
    a = _flatten_config(after)
    keys = set(b) | set(a)
    return sorted(k for k in keys if b.get(k) != a.get(k))


def _parse_yaml_mapping(text: str, *, source: Path) -> dict[str, Any]:
    if yaml is None:  # pragma: no cover - PyYAML is a runtime dependency
        raise RuntimeError("PyYAML is required to update profile config")
    try:
        loaded = yaml.safe_load(text) if text.strip() else {}
    except Exception as exc:
        raise ValueError(f"failed to parse YAML in {source}: {exc}") from exc
    if loaded is None:
        return {}
    if not isinstance(loaded, dict):
        raise ValueError(f"{source} must contain a YAML mapping at the top level")
    return loaded


def _atomic_write_text(path: Path, text: str) -> None:
    """Write text via temp-file + fsync + atomic replace in the same directory."""
    from utils import atomic_replace

    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp_path = tempfile.mkstemp(
        dir=str(path.parent), prefix=f".{path.name}.", suffix=".tmp"
    )
    tmp = Path(tmp_path)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            f.write(text)
            f.flush()
            os.fsync(f.fileno())
        atomic_replace(tmp, path)
        try:
            dir_fd = os.open(str(path.parent), os.O_DIRECTORY)
        except OSError:
            dir_fd = None
        if dir_fd is not None:
            try:
                os.fsync(dir_fd)
            finally:
                os.close(dir_fd)
    except BaseException:
        try:
            tmp.unlink(missing_ok=True)
        except OSError:
            pass
        raise


def _profile_config_path(profile: str) -> tuple[str, Path]:
    """Resolve and validate the config path for a named Hermes profile."""
    from hermes_cli.profiles import (
        get_profile_dir,
        normalize_profile_name,
        validate_profile_name,
    )

    canon = normalize_profile_name(profile)
    validate_profile_name(canon)
    profile_dir = get_profile_dir(canon)
    if canon != "default" and not profile_dir.is_dir():
        raise ValueError(
            f"profile {canon!r} does not exist; create it before updating model config"
        )

    config_path = profile_dir / "config.yaml"
    try:
        from hermes_constants import get_default_hermes_root

        root = get_default_hermes_root()
    except Exception as exc:  # pragma: no cover - defensive startup fallback
        raise ValueError(f"could not resolve Hermes profile root: {exc}") from exc
    expected = (
        root / "config.yaml"
        if canon == "default"
        else root / "profiles" / canon / "config.yaml"
    )
    if config_path != expected:
        raise ValueError(
            f"refusing to update unexpected profile config path {config_path}; "
            f"expected {expected}"
        )
    if config_path.is_symlink():
        raise ValueError(
            f"refusing to update symlinked profile config {config_path}; "
            "Kanban profile-model updates require an in-profile config.yaml"
        )
    return canon, config_path


def _transactional_update_profile_config(
    profile: str,
    updates: Mapping[str, Any],
    *,
    allowed_keys: frozenset[str],
    postcheck=None,
) -> dict[str, Any]:
    """Backup, parse, atomically rewrite, postcheck, and rollback on failure.

    This is intentionally kept narrow and private. Public Kanban callers use
    :func:`kanban_update_profile_model`, which can only touch
    ``model.default`` and ``model.provider``.
    """
    requested = {str(k): v for k, v in updates.items()}
    unknown = sorted(set(requested) - set(allowed_keys))
    if unknown:
        raise ValueError(
            "profile config update contains unsupported key(s): "
            + ", ".join(unknown)
            + f"; allowed keys: {', '.join(sorted(allowed_keys))}"
        )

    canon, config_path = _profile_config_path(profile)
    original_text = config_path.read_text(encoding="utf-8") if config_path.exists() else ""
    backup_path = config_path.with_name(
        f"{config_path.name}.bak.{int(time.time())}.{os.getpid()}.{secrets.token_hex(4)}"
    )
    _atomic_write_text(backup_path, original_text)

    pre_cfg = _parse_yaml_mapping(original_text, source=config_path)
    pre_values = {key: _nested_get(pre_cfg, key) for key in sorted(allowed_keys)}

    next_cfg = copy.deepcopy(pre_cfg)
    for key, value in requested.items():
        _nested_set(next_cfg, key, value)

    if yaml is None:  # pragma: no cover - guarded above, here for type checkers
        raise RuntimeError("PyYAML is required to update profile config")
    next_text = yaml.safe_dump(next_cfg, sort_keys=False, allow_unicode=True)

    rollback_status = "not_needed"
    wrote_candidate = False
    try:
        _atomic_write_text(config_path, next_text)
        wrote_candidate = True
        post_text = config_path.read_text(encoding="utf-8")
        post_cfg = _parse_yaml_mapping(post_text, source=config_path)
        for key, value in requested.items():
            actual = _nested_get(post_cfg, key)
            if actual != value:
                raise ValueError(
                    f"postcheck failed for {key}: expected {value!r}, got {actual!r}"
                )
        changed_keys = _changed_config_keys(pre_cfg, post_cfg)
        forbidden_changes = sorted(set(changed_keys) - set(allowed_keys))
        if forbidden_changes:
            raise ValueError(
                "postcheck detected forbidden config key change(s): "
                + ", ".join(forbidden_changes)
            )
        if postcheck is not None:
            postcheck(post_cfg)
    except Exception as exc:
        if wrote_candidate:
            try:
                _atomic_write_text(config_path, original_text)
                rollback_status = "rolled_back"
            except Exception as rollback_exc:  # pragma: no cover - catastrophic FS failure
                rollback_status = f"rollback_failed: {rollback_exc}"
        raise RuntimeError(
            f"profile config update failed for {canon}; {rollback_status}: {exc}"
        ) from exc

    post_values = {key: _nested_get(post_cfg, key) for key in sorted(allowed_keys)}
    return {
        "profile": canon,
        "changed_file": str(config_path),
        "backup_path": str(backup_path),
        "requested": requested,
        "allowed_keys": sorted(allowed_keys),
        "changed_keys": changed_keys,
        "pre_values": pre_values,
        "post_values": post_values,
        "parse_status": {"pre": "ok", "post": "ok"},
        "rollback_status": rollback_status,
        "atomic_write": "tempfile_fsync_replace",
        "non_actions": [
            "no_gateway_restart",
            "no_dispatcher_activation",
            "no_secret_or_env_file_read",
            "no_profile_other_than_target_mutated",
        ],
    }


def kanban_update_profile_model(
    profile: str,
    provider: str,
    model: str,
    *,
    _postcheck=None,
) -> dict[str, Any]:
    """Transactionally update ``model.provider`` and ``model.default``.

    The primitive is deliberately narrower than a generic YAML patcher: Kanban
    control-plane recovery only needs to switch the assignee profile's routing
    model/provider, and the narrow shape lets us prove only those two semantic
    keys changed before returning a receipt.
    """
    if not str(provider or "").strip():
        raise ValueError("provider is required")
    if not str(model or "").strip():
        raise ValueError("model is required")
    return _transactional_update_profile_config(
        profile,
        {
            "model.provider": str(provider).strip(),
            "model.default": str(model).strip(),
        },
        allowed_keys=PROFILE_MODEL_CONFIG_KEYS,
        postcheck=_postcheck,
    )


def known_assignees(conn: sqlite3.Connection) -> list[dict]:
    """Return every assignee name known to the board or on disk.

    Each entry is ``{"name": str, "on_disk": bool, "counts": {status: n}}``.
    A name is included when it's a configured profile on disk OR when
    any non-archived task has it as the assignee. Used by:

    - ``hermes kanban assignees`` for the terminal.
    - The dashboard assignee dropdown (so a fresh profile appears in
      the picker even before it's been given any task).
    - Router-profile heuristics ("who's overloaded?") without scanning
      the whole board.
    """
    on_disk = set(list_profiles_on_disk())

    # Count tasks per (assignee, status), excluding archived.
    counts: dict[str, dict[str, int]] = {}
    for row in conn.execute(
        "SELECT assignee, status, COUNT(*) AS n FROM tasks "
        "WHERE status != 'archived' AND assignee IS NOT NULL "
        "GROUP BY assignee, status"
    ):
        counts.setdefault(row["assignee"], {})[row["status"]] = int(row["n"])

    names = sorted(on_disk | set(counts.keys()))
    return [
        {
            "name": name,
            "on_disk": name in on_disk,
            "counts": counts.get(name, {}),
        }
        for name in names
    ]


# ---------------------------------------------------------------------------
# Runs (attempt history on a task)
# ---------------------------------------------------------------------------

def list_runs(
    conn: sqlite3.Connection,
    task_id: str,
    *,
    include_active: bool = True,
    state_type: Optional[str] = None,
    state_name: Optional[str] = None,
) -> list[Run]:
    """Return all runs for ``task_id`` in start order.

    ``include_active=True`` (default) includes the currently-running
    attempt if any. Set False to return only closed runs (useful for
    "how many prior attempts have there been?" checks).

    When ``state_type`` and ``state_name`` are set, restrict to rows
    where that column equals ``state_name`` (``state_type`` is
    ``status`` or ``outcome``). Both must be passed together.
    """
    if (state_type is None) ^ (state_name is None):
        raise ValueError("state_type and state_name must both be set or both omitted")
    if state_type is not None:
        if state_type not in ("status", "outcome"):
            raise ValueError("state_type must be 'status' or 'outcome'")
    q = "SELECT * FROM task_runs WHERE task_id = ?"
    params: list[Any] = [task_id]
    if not include_active:
        q += " AND ended_at IS NOT NULL"
    if state_type is not None:
        q += f" AND {state_type} = ?"
        params.append(state_name)
    q += " ORDER BY started_at ASC, id ASC"
    rows = conn.execute(q, params).fetchall()
    return [Run.from_row(r) for r in rows]


def get_run(conn: sqlite3.Connection, run_id: int) -> Optional[Run]:
    row = conn.execute(
        "SELECT * FROM task_runs WHERE id = ?", (int(run_id),),
    ).fetchone()
    return Run.from_row(row) if row else None


def active_run(conn: sqlite3.Connection, task_id: str) -> Optional[Run]:
    """Return the currently-open run for ``task_id`` (``ended_at IS NULL``)."""
    row = conn.execute(
        "SELECT * FROM task_runs WHERE task_id = ? AND ended_at IS NULL "
        "ORDER BY started_at DESC LIMIT 1",
        (task_id,),
    ).fetchone()
    return Run.from_row(row) if row else None


def latest_run(conn: sqlite3.Connection, task_id: str) -> Optional[Run]:
    """Return the most recent run regardless of outcome (active or closed)."""
    row = conn.execute(
        "SELECT * FROM task_runs WHERE task_id = ? "
        "ORDER BY started_at DESC, id DESC LIMIT 1",
        (task_id,),
    ).fetchone()
    return Run.from_row(row) if row else None


def latest_summary(conn: sqlite3.Connection, task_id: str) -> Optional[str]:
    """Return the latest non-null ``task_runs.summary`` for ``task_id``.

    The kanban-worker skill writes its handoff to ``task_runs.summary``
    via ``complete_task(summary=...)``; ``tasks.result`` is left empty
    unless the caller passes ``result=`` explicitly. Dashboards and CLI
    "show" views need this value to surface what a worker actually did
    — without it, ``tasks.result`` is NULL and the task looks like a
    no-op even when the run completed.

    Picks the most recent run by ``ended_at`` (falling back to ``id``
    for ties or unfinished rows). Returns None if no run has a summary.
    """
    row = conn.execute(
        "SELECT summary FROM task_runs "
        "WHERE task_id = ? AND summary IS NOT NULL AND summary != '' "
        "ORDER BY COALESCE(ended_at, started_at) DESC, id DESC LIMIT 1",
        (task_id,),
    ).fetchone()
    return row["summary"] if row else None


def latest_summaries(
    conn: sqlite3.Connection, task_ids: Iterable[str]
) -> dict[str, str]:
    """Batch-fetch latest non-null summaries for a list of task ids.

    Used by the dashboard board endpoint to attach ``latest_summary`` to
    every card in a single SQL query, avoiding the N+1 pattern of
    calling :func:`latest_summary` per task. Returns a dict mapping
    ``task_id`` → summary string, omitting tasks with no summary.

    Approach: a window function picks the newest non-null-summary row
    per ``task_id``; works against SQLite ≥ 3.25 (default on every
    supported platform).
    """
    ids = list(task_ids)
    if not ids:
        return {}
    placeholders = ",".join("?" for _ in ids)
    rows = conn.execute(
        f"""
        SELECT task_id, summary FROM (
            SELECT task_id, summary,
                   ROW_NUMBER() OVER (
                       PARTITION BY task_id
                       ORDER BY COALESCE(ended_at, started_at) DESC, id DESC
                   ) AS rn
              FROM task_runs
             WHERE task_id IN ({placeholders})
               AND summary IS NOT NULL AND summary != ''
        ) WHERE rn = 1
        """,
        ids,
    ).fetchall()
    return {r["task_id"]: r["summary"] for r in rows}
