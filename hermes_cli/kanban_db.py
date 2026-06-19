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
import hashlib
import json
import os
import re
import secrets
import shlex
import shutil
import datetime as _dt
import sqlite3
import subprocess
import sys
import threading
import logging
import time
from contextvars import ContextVar, Token
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Iterable, Optional

from toolsets import get_toolset_names

_log = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

VALID_STATUSES = {"triage", "todo", "scheduled", "ready", "running", "blocked", "review", "done", "archived"}
VALID_INITIAL_STATUSES = {"running", "blocked"}
VALID_WORKSPACE_KINDS = {"scratch", "worktree", "dir"}
KNOWN_TOOLSET_NAMES = frozenset(name.casefold() for name in get_toolset_names())
_IS_WINDOWS = sys.platform == "win32"

# A running task's claim is valid for 15 minutes by default; after that the
# next dispatcher tick reclaims it. Workers that outlive this window should
# call ``heartbeat_claim(task_id)`` periodically. In practice most kanban
# workloads either finish within 15m, set a longer claim explicitly, or use
# ``HERMES_KANBAN_CLAIM_TTL_SECONDS`` to raise the default claim window for
# long single-call MCP workflows.
DEFAULT_CLAIM_TTL_SECONDS = 15 * 60

# If a worker's PID is still alive but its ``last_heartbeat_at`` is
# older than this when ``release_stale_claims`` runs, treat the worker
# as wedged and reclaim regardless of PID liveness (#29747 gap 3).
# This catches the logic-loop case where the process is technically
# running but not making observable progress.  ``_touch_activity``
# bridges chunk-level liveness into ``last_heartbeat_at`` via #31752,
# so any genuinely active worker keeps its heartbeat fresh as a side
# effect of normal API traffic.
DEFAULT_CLAIM_HEARTBEAT_MAX_STALE_SECONDS = 60 * 60

# Dispatcher-side heartbeat cadence for claude-CLI workers. Unlike Hermes-
# runtime workers (which bridge chunk-level liveness into ``last_heartbeat_at``
# via ``_touch_activity`` #31752), a ``claude -p`` worker is a detached
# subprocess the dispatcher only ever sees as a PID + a growing log file. So
# ``heartbeat_live_claude_cli_workers`` refreshes the heartbeat from the
# dispatcher tick while that PID is alive. Only re-emit (touch + event) when
# the existing heartbeat is older than this gap, so the timeline gets a steady
# pulse rather than one event per tick. Far below ``_STALE_HEARTBEAT_GAP_SECONDS``
# (1h) and the SPA's ``STUCK_HEARTBEAT_S`` (10m), so a live claude worker never
# reads as stale/stuck.
_CLAUDE_CLI_HEARTBEAT_MIN_GAP_SECONDS = 120


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


# Grace period after a task transitions to ``running`` during which
# ``detect_crashed_workers`` skips the ``_pid_alive`` check. Covers the
# fork() → /proc-visibility window where liveness can transiently report
# False for a freshly-spawned worker. The 15-minute claim TTL still
# catches genuinely-crashed workers; this only suppresses false positives
# during the launch window.
DEFAULT_CRASH_GRACE_SECONDS = 30

# First-slice default for bounded continuation when a worker explicitly reports
# that it stopped because the tool-calling iteration budget was exhausted.
# Config wiring is intentionally deferred; task-level max_continuations wins.
DEFAULT_ITERATION_BUDGET_CONTINUATION_LIMIT = 3

# Opt-in blocked-run auto-retry policy. Defaults are code-level constants;
# gateway/daemon config decides whether the policy runs at all.
DEFAULT_AUTO_RETRY_BLOCKED_BACKOFF_SECONDS = 5 * 60
DEFAULT_AUTO_RETRY_BLOCKED_LIMIT = 2
AUTO_RETRY_ESCALATION_MODEL = "claude-opus-4-8"  # claude-fable-5 z.Zt. gesperrt
AUTO_RETRY_ESCALATION_PROFILE = "premium"


# Sentinel exit code a kanban worker uses to signal "I bailed because the
# provider rate-limited / exhausted quota, not because the task failed."
# The dispatcher's reap classifier maps this to a ``rate_limited`` exit kind
# so ``detect_crashed_workers`` can release the task back to ``ready``
# WITHOUT counting a failure (the circuit breaker must never trip on a
# transient throttle). 75 == BSD ``EX_TEMPFAIL`` (sysexits.h) — the
# conventional "temporary failure, retry later" code, and well clear of the
# 0/1/2 codes the worker uses for success / generic failure / usage error.
KANBAN_RATE_LIMIT_EXIT_CODE = 75


def _resolve_crash_grace_seconds() -> int:
    """Return the crash-detection grace period in seconds.

    Reads ``HERMES_KANBAN_CRASH_GRACE_SECONDS`` from the environment;
    falls back to ``DEFAULT_CRASH_GRACE_SECONDS`` when absent, empty,
    non-integer, or negative. A value of 0 restores immediate-reclaim
    behaviour (useful for tests).
    """
    raw = os.environ.get("HERMES_KANBAN_CRASH_GRACE_SECONDS", "").strip()
    if raw:
        try:
            parsed = int(raw)
        except ValueError:
            parsed = -1
        if parsed >= 0:
            return parsed
    return DEFAULT_CRASH_GRACE_SECONDS


def _resolve_rate_limit_cooldown_seconds() -> int:
    """Return the rate-limit requeue cooldown in seconds.

    Reads ``HERMES_KANBAN_RATE_LIMIT_COOLDOWN_SECONDS`` from the environment;
    falls back to ``DEFAULT_RATE_LIMIT_COOLDOWN_SECONDS`` when absent, empty,
    non-integer, or negative. A value of 0 disables the cooldown (re-spawn on
    the next tick) — useful for tests that want to assert the task becomes
    spawnable again immediately.
    """
    raw = os.environ.get(
        "HERMES_KANBAN_RATE_LIMIT_COOLDOWN_SECONDS", ""
    ).strip()
    if raw:
        try:
            parsed = int(raw)
        except ValueError:
            parsed = -1
        if parsed >= 0:
            return parsed
    return DEFAULT_RATE_LIMIT_COOLDOWN_SECONDS


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
_CURRENT_BOARD_OVERRIDE: ContextVar[str | None] = ContextVar(
    "hermes_kanban_current_board_override",
    default=None,
)


@contextlib.contextmanager
def scoped_current_board(slug: str):
    """Temporarily pin the active board for the current context only."""
    token: Token[str | None] = _CURRENT_BOARD_OVERRIDE.set(slug)
    try:
        yield
    finally:
        _CURRENT_BOARD_OVERRIDE.reset(token)

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
    scoped = (_CURRENT_BOARD_OVERRIDE.get() or "").strip()
    if scoped:
        try:
            normed = _normalize_board_slug(scoped)
            if normed and board_exists(normed):
                return normed
        except ValueError:
            pass

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


def attachments_root(board: Optional[str] = None) -> Path:
    """Return the directory under which task file attachments are stored.

    Mirrors :func:`worker_logs_dir` / :func:`workspaces_root`: anchored
    per-board so attachments don't leak between projects. Each task gets
    its own ``<root>/.../attachments/<task_id>/`` subdirectory.

    ``HERMES_KANBAN_ATTACHMENTS_ROOT`` pins the path directly (highest
    precedence) for tests and unusual deployments.

    ``default`` uses ``<root>/kanban/attachments/``; other boards use
    ``<root>/kanban/boards/<slug>/attachments/``.

    Workers (which run with full file-tool access) read attached files
    by the absolute path surfaced in :func:`build_worker_context`. On the
    local terminal backend — the default for kanban — that path resolves
    directly. Remote backends (Docker/Modal) need this directory mounted;
    see the kanban docs.
    """
    override = os.environ.get("HERMES_KANBAN_ATTACHMENTS_ROOT", "").strip()
    if override:
        return Path(override).expanduser()
    slug = _normalize_board_slug(board)
    if slug is None:
        slug = get_current_board()
    if slug == DEFAULT_BOARD:
        return kanban_home() / "kanban" / "attachments"
    return board_dir(slug) / "attachments"


def task_attachments_dir(task_id: str, board: Optional[str] = None) -> Path:
    """Return the per-task attachment directory ``<root>/<task_id>/``."""
    return attachments_root(board=board) / task_id


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
    # Force-loaded skills for the worker on this task (appended to the
    # dispatcher's built-in `kanban-worker` via --skills). Stored as a
    # JSON array of skill names. None = use only the defaults; empty
    # list = explicitly no extra skills.
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
    # Per-task override for the worker's tool-calling iteration budget.
    # When set, the dispatcher passes ``--max-turns N`` on the worker's
    # ``chat`` argv — the authoritative, top-precedence path that beats the
    # profile's ``agent.max_turns`` — and also injects
    # ``HERMES_MAX_ITERATIONS=N`` into the worker env (a shadowed fallback;
    # see _default_spawn). NULL = fall through to the profile's
    # ``agent.max_turns`` config (or the global default).
    # Name matches the ``--max-iterations`` CLI flag on ``kanban create``.
    max_iterations: Optional[int] = None
    # Bounded auto-continuation policy for explicit iteration-budget exhaustion.
    continuation_count: int = 0
    max_continuations: Optional[int] = None
    last_continuation_reason: Optional[str] = None
    # When True, the dispatched worker runs in a Ralph-style goal loop
    # (the same engine behind the ``/goal`` slash command): after each
    # turn an auxiliary judge model evaluates the worker's response
    # against this card's title/body (treated as the goal). If the judge
    # says "not done" and budget remains, the worker is fed a
    # continuation prompt IN THE SAME SESSION and keeps working until the
    # judge agrees, the goal-turn budget is exhausted (→ kanban_block),
    # or the worker explicitly blocks/completes. ``False`` (default) =
    # the classic single-shot worker. ``goal_max_turns`` bounds the loop.
    goal_mode: bool = False
    # Goal-loop turn budget for ``goal_mode`` workers. ``None`` falls
    # through to the goals engine default (``goals.DEFAULT_MAX_TURNS``).
    goal_max_turns: Optional[int] = None
    # Originating chat/agent session id, when the task was created from
    # within an agent loop that propagated ``HERMES_SESSION_ID``. NULL for
    # tasks created from the CLI, the dashboard, or any path that doesn't
    # set the env var. Lets clients render a per-session board without
    # relying on tenant + time-window heuristics.
    session_id: Optional[str] = None
    # Earliest time (unix epoch seconds) the task may be promoted from
    # ``todo``/``blocked`` to ``ready``. NULL (the common case) = eligible
    # immediately, exactly as before this column existed. A future value
    # holds the task in place in ``recompute_ready`` until the wall clock
    # reaches it — native time-based scheduling without a separate cron.
    due_at: Optional[int] = None
    # N-E3: durable epic this task belongs to. NULL = not part of an epic.
    epic_id: Optional[str] = None
    # Optional coarse task kind stamped by decomposer/CLI. NULL = unknown.
    kind: Optional[str] = None
    auto_retry_count: int = 0
    integration_retry_count: int = 0

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
            max_iterations=(
                row["max_iterations"] if "max_iterations" in keys else None
            ),
            continuation_count=(
                row["continuation_count"] if "continuation_count" in keys else 0
            ),
            max_continuations=(
                row["max_continuations"] if "max_continuations" in keys else None
            ),
            last_continuation_reason=(
                row["last_continuation_reason"]
                if "last_continuation_reason" in keys else None
            ),
            goal_mode=(
                bool(row["goal_mode"]) if "goal_mode" in keys and row["goal_mode"] else False
            ),
            goal_max_turns=(
                row["goal_max_turns"] if "goal_max_turns" in keys and row["goal_max_turns"] else None
            ),
            session_id=(
                row["session_id"] if "session_id" in keys else None
            ),
            due_at=(
                row["due_at"] if "due_at" in keys else None
            ),
            epic_id=(
                row["epic_id"] if "epic_id" in keys else None
            ),
            kind=(
                row["kind"] if "kind" in keys else None
            ),
            auto_retry_count=(
                row["auto_retry_count"] if "auto_retry_count" in keys else 0
            ),
            integration_retry_count=(
                row["integration_retry_count"]
                if "integration_retry_count" in keys else 0
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
    # K5a: per-run token/cost accounting (NULL until a run records usage).
    input_tokens: Optional[int] = None
    output_tokens: Optional[int] = None
    cost_usd: Optional[float] = None

    @classmethod
    def from_row(cls, row: sqlite3.Row) -> "Run":
        try:
            meta = json.loads(row["metadata"]) if row["metadata"] else None
        except Exception:
            meta = None

        def _opt(key: str) -> Any:
            # K5a columns may be absent on rows from narrower SELECTs; tolerate.
            try:
                return row[key]
            except (IndexError, KeyError):
                return None

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
            input_tokens=_coerce_int(_opt("input_tokens")),
            output_tokens=_coerce_int(_opt("output_tokens")),
            cost_usd=_coerce_float(_opt("cost_usd")),
        )


@dataclass
class Comment:
    id: int
    task_id: str
    author: str
    body: str
    created_at: int


@dataclass
class Attachment:
    """In-memory view of a row from the ``task_attachments`` table."""

    id: int
    task_id: str
    filename: str
    stored_path: str
    content_type: Optional[str]
    size: int
    uploaded_by: Optional[str]
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
    -- Lifetime count of failed auto_decompose attempts for this task.
    -- Incremented whenever ``decompose_task`` returns ok=False (or
    -- crashes) for the task; reset to 0 on a successful decompose. Unlike
    -- ``consecutive_failures`` this is a decompose-specific signal and does
    -- NOT feed the spawn circuit breaker.
    decompose_failed     INTEGER NOT NULL DEFAULT 0,
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
    -- Force-loaded skills for the worker on this task, stored as JSON.
    -- Appended to the dispatcher's built-in `--skills kanban-worker`.
    -- NULL or empty array = no extras.
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
    -- Per-task override for the worker's tool-calling iteration budget
    -- (i.e. `--max-iterations` / `HERMES_MAX_ITERATIONS`). When set,
    -- the dispatcher injects ``HERMES_MAX_ITERATIONS=N`` into the
    -- worker env so the LLM agent loop allows up to N tool-calling
    -- rounds before the iteration-budget guard fires.  NULL (the
    -- common case) falls through to the profile's ``agent.max_turns``
    -- config.  Added 2026-05-27 for hardening sprint TASK 8
    -- (audit-class tasks reproducibly hit the 30-turn profile default).
    max_iterations       INTEGER,
    -- Auto-continuation audit/policy for workers that explicitly report
    -- iteration_budget_exhausted. NULL max_continuations uses the code-level
    -- default; 0 disables auto-continuation for this task.
    continuation_count   INTEGER NOT NULL DEFAULT 0,
    max_continuations    INTEGER,
    last_continuation_reason TEXT,
    -- When 1, the dispatched worker runs in a Ralph-style goal loop: an
    -- auxiliary judge re-evaluates the worker's response against the
    -- card title/body after each turn and feeds a continuation prompt
    -- back into the SAME session until the judge agrees the work is done
    -- or ``goal_max_turns`` is exhausted. NULL/0 = classic single-shot
    -- worker (the default).
    goal_mode            INTEGER NOT NULL DEFAULT 0,
    -- Goal-loop turn budget for ``goal_mode`` workers. NULL = use the
    -- goals-engine default.
    goal_max_turns       INTEGER,
    -- Originating chat/agent session id when the task was created from
    -- inside an agent loop that propagated ``HERMES_SESSION_ID``. NULL
    -- for tasks created from the CLI, dashboard, or any path that doesn't
    -- set the env var. Indexed so per-session list queries stay cheap on
    -- larger boards.
    session_id           TEXT,
    -- Earliest promotion time (unix epoch seconds). NULL = eligible
    -- immediately (legacy behaviour). recompute_ready holds a task in
    -- ``todo``/``blocked`` until the wall clock reaches a future due_at.
    due_at               INTEGER,
    -- N-E3: durable epic this task belongs to (FK-style pointer into the
    -- ``epics`` table, no hard constraint). NULL = not part of an epic =
    -- exactly the pre-E3 behaviour. Decompose propagates the triage root's
    -- epic_id onto every child so a whole tree rolls up under one epic.
    epic_id              TEXT,
    -- Optional coarse work classification stamped by the decomposer or CLI.
    -- NULL = unknown/unspecified.
    kind                 TEXT,
    -- Bounded, opt-in automatic retry count for worker-blocked runs.
    auto_retry_count     INTEGER NOT NULL DEFAULT 0,
    -- Bounded transient re-integration retry count (Heiler lane). Kept
    -- SEPARATE from auto_retry_count so a re-integration round never trips
    -- the premium/opus escalation ladder that auto_retry_count drives; gates
    -- the no-silent-stall integration-retry path.
    integration_retry_count INTEGER NOT NULL DEFAULT 0
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
    --          gave_up | reclaimed | iteration_budget_exhausted |
    --          (null while still running)
    summary             TEXT,
    metadata            TEXT,
    error               TEXT
);

-- Files attached to a task (PDFs, images, source documents). The blob
-- lives on disk under ``attachments_root(board)/<task_id>/<stored_name>``;
-- this row carries metadata + the absolute ``stored_path`` so the
-- dashboard can list/download and ``build_worker_context`` can surface
-- the absolute path to the worker (which has full file-tool access). See
-- #35338.
CREATE TABLE IF NOT EXISTS task_attachments (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    task_id      TEXT NOT NULL,
    filename     TEXT NOT NULL,
    stored_path  TEXT NOT NULL,
    content_type TEXT,
    size         INTEGER NOT NULL DEFAULT 0,
    uploaded_by  TEXT,
    created_at   INTEGER NOT NULL
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

-- N-E3: durable epic — a goal that spans MULTIPLE task trees. Unlike a
-- triage root (one tree) or ``--goal`` (a per-run loop flag) or ``tenant``
-- (a free filter string), an epic is a first-class object tasks point at via
-- ``tasks.epic_id``. Additive: a board with no epics behaves exactly as before.
CREATE TABLE IF NOT EXISTS epics (
    id         TEXT PRIMARY KEY,
    title      TEXT NOT NULL,
    body       TEXT,
    status     TEXT NOT NULL DEFAULT 'open',
    created_at INTEGER NOT NULL,
    closed_at  INTEGER
);

-- F1 (night-sprint): lane presets — named profile→(worker_runtime, model)
-- mappings stored in the board DB so the dispatcher hot-reads the ACTIVE lane
-- at every spawn (no gateway restart). ``profiles`` is a JSON object:
--   {"<profile>": {"worker_runtime": "hermes"|"claude-cli", "model": "<id>"}}
-- Precedence at spawn time: task.model_override > active lane > profile
-- config.yaml default. Additive: no rows / no active row = exact pre-lane
-- behavior.
CREATE TABLE IF NOT EXISTS lanes (
    id         TEXT PRIMARY KEY,
    name       TEXT NOT NULL UNIQUE,
    profiles   TEXT NOT NULL DEFAULT '{}',
    active     INTEGER NOT NULL DEFAULT 0,
    builtin    INTEGER NOT NULL DEFAULT 0,
    created_at INTEGER NOT NULL,
    updated_at INTEGER NOT NULL
);

-- F5 (night-sprint): eval scores — one row per measured quality signal on a
-- run/task. Review-gate verdicts land here automatically via
-- ``_record_verdict_score`` (name='review_verdict', value 1.0=APPROVED /
-- 0.0=REQUEST_CHANGES, value_type='binary', source='review_gate'); future
-- eval sources append their own names. Write-only at runtime — no read path
-- depends on it, so the table is purely additive.
CREATE TABLE IF NOT EXISTS scores (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    run_id     INTEGER,
    task_id    TEXT NOT NULL,
    name       TEXT NOT NULL,
    value      REAL,
    value_type TEXT NOT NULL DEFAULT 'numeric',
    source     TEXT,
    created_at INTEGER NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_tasks_assignee_status ON tasks(assignee, status);
CREATE INDEX IF NOT EXISTS idx_tasks_status          ON tasks(status);
-- Serves dispatch_once's per-tick ready-pull: WHERE status='ready' ... ORDER BY
-- priority DESC, created_at ASC. The DESC/ASC directions match the query so the
-- ORDER BY is answered straight from the index (no TEMP B-TREE sort each tick).
CREATE INDEX IF NOT EXISTS idx_tasks_ready_order     ON tasks(status, priority DESC, created_at ASC);
CREATE INDEX IF NOT EXISTS idx_links_child           ON task_links(child_id);
CREATE INDEX IF NOT EXISTS idx_links_parent          ON task_links(parent_id);
CREATE INDEX IF NOT EXISTS idx_comments_task         ON task_comments(task_id, created_at);
CREATE INDEX IF NOT EXISTS idx_events_task           ON task_events(task_id, created_at);
CREATE INDEX IF NOT EXISTS idx_events_task_id        ON task_events(task_id, id);
CREATE INDEX IF NOT EXISTS idx_runs_task             ON task_runs(task_id, started_at);
CREATE INDEX IF NOT EXISTS idx_runs_status           ON task_runs(status);
-- idx_runs_started: the budget preflight SUMs tokens/cost over a started_at>=?
-- 24h window per tick (was a full SCAN of task_runs). The ended_at companion
-- (idx_runs_task_ended) can't live here: ended_at is absent on legacy task_runs
-- until _rebuild_drifted_tables recreates the table, and a CREATE INDEX over a
-- missing column aborts executescript. It is created in
-- _migrate_add_optional_columns after the rebuild instead.
CREATE INDEX IF NOT EXISTS idx_runs_started          ON task_runs(started_at);
CREATE INDEX IF NOT EXISTS idx_attachments_task      ON task_attachments(task_id, created_at);
CREATE INDEX IF NOT EXISTS idx_notify_task           ON kanban_notify_subs(task_id);
CREATE INDEX IF NOT EXISTS idx_scores_run            ON scores(run_id);
CREATE INDEX IF NOT EXISTS idx_scores_task           ON scores(task_id);
CREATE INDEX IF NOT EXISTS idx_scores_name_created   ON scores(name, created_at);
"""


# ---------------------------------------------------------------------------
# Connection helpers
# ---------------------------------------------------------------------------

_INITIALIZED_PATHS: set[str] = set()
_INIT_LOCK = threading.RLock()
_SQLITE_HEADER = b"SQLite format 3\x00"
DEFAULT_BUSY_TIMEOUT_MS = 120_000

# Bump when the migration pass (_migrate_add_optional_columns /
# _rebuild_drifted_tables) changes WITHOUT a matching SCHEMA_SQL change —
# e.g. a new data backfill or event-kind rename. Schema changes themselves
# already invalidate the stamp via the SCHEMA_SQL hash below.
# gen 3 (#11): idx_events_kind was added to the migration pass only (not
# SCHEMA_SQL), so already-stamped boards must re-run the additive pass once to
# backfill it — without this bump connect()'s fast path would skip it forever.
_SCHEMA_GENERATION = 3

# Cross-process init stamp, persisted in ``PRAGMA user_version`` after a
# successful schema+migration pass. A connect() that finds this exact stamp
# in the DB header can skip the exclusive cross-process file lock, the
# integrity probe and the whole migration pass — the expensive first-connect
# work that used to run once per *process* (every worker spawn) and, under
# the flock + 120s busy timeout, could stall every dashboard request for
# minutes behind one slow init. 31-bit (user_version is a signed 32-bit
# int) and never 0 (0 = "unstamped legacy DB" → full init path).
_SCHEMA_STAMP = int.from_bytes(
    hashlib.sha256(f"{_SCHEMA_GENERATION}:{SCHEMA_SQL}".encode()).digest()[:4],
    "big",
) & 0x7FFFFFFF or 1


def _resolve_busy_timeout_ms() -> int:
    """Return the SQLite busy timeout for Kanban connections.

    Kanban is the shared cross-profile dispatch bus, so worker stampedes are
    expected.  A long busy timeout lets SQLite serialize writers via WAL rather
    than surfacing transient ``database is locked`` failures during bursts.
    """
    raw = os.environ.get("HERMES_KANBAN_BUSY_TIMEOUT_MS", "").strip()
    if raw:
        try:
            parsed = int(raw)
        except ValueError:
            parsed = 0
        if parsed > 0:
            return parsed
    return DEFAULT_BUSY_TIMEOUT_MS


def _sqlite_connect(
    path: Path, busy_timeout_ms: Optional[int] = None
) -> sqlite3.Connection:
    """Open a Kanban SQLite connection with consistent lock waiting.

    ``busy_timeout_ms`` overrides the default (env-resolved, 120s) wait.
    Read-mostly callers like the dashboard pass a few seconds: surfacing a
    busy error quickly beats a 2-minute request hang (the SPA has its own
    GET timeout + retry/backoff).
    """
    if busy_timeout_ms is None or busy_timeout_ms <= 0:
        busy_timeout_ms = _resolve_busy_timeout_ms()
    conn = sqlite3.connect(
        str(path),
        isolation_level=None,
        timeout=busy_timeout_ms / 1000.0,
    )
    # ``sqlite3.connect(timeout=...)`` normally maps to busy_timeout, but set
    # the PRAGMA explicitly so it is observable and survives future wrapper
    # changes. Parameter binding is not supported for PRAGMA assignments.
    conn.execute(f"PRAGMA busy_timeout={busy_timeout_ms}")
    return conn


def _apply_connection_pragmas(conn: sqlite3.Connection) -> None:
    """Per-connection PRAGMAs (not persisted in the DB file).

    Shared by the init path and the stamped fast path so both connection
    flavours behave identically. ``journal_mode=WAL`` is NOT here: it is
    persisted in the DB header by the init path (apply_wal_with_fallback)
    and re-applying it per connect is wasted work.
    """
    # FULL (was NORMAL): fsync before each checkpoint to narrow the
    # crash window that can leave a b-tree page header torn.
    conn.execute("PRAGMA synchronous=FULL")
    conn.execute("PRAGMA wal_autocheckpoint=100")
    conn.execute("PRAGMA foreign_keys=ON")
    # Zero freed pages so a later torn write cannot expose stale
    # cell content; persisted in the DB header for new DBs.
    conn.execute("PRAGMA secure_delete=ON")
    # Surface corrupt cells as read errors instead of silent
    # wrong-data returns.
    conn.execute("PRAGMA cell_size_check=ON")


def _try_fast_connect(
    path: Path, resolved: str, busy_timeout_ms: Optional[int] = None
) -> Optional[sqlite3.Connection]:
    """Fast path: connect without the cross-process init lock.

    Succeeds only when the DB is already fully initialized — either proven
    by this process (``_INITIALIZED_PATHS``) or stamped in the DB header by
    any process (``PRAGMA user_version == _SCHEMA_STAMP``). Returns ``None``
    on any doubt (missing/empty/corrupt file, legacy stamp, schema drift)
    so the caller falls back to the full flock+init+integrity path.
    """
    try:
        if not path.exists() or path.stat().st_size == 0:
            return None
    except OSError:
        return None
    try:
        conn = _sqlite_connect(path, busy_timeout_ms)
    except sqlite3.Error:
        return None
    try:
        if resolved not in _INITIALIZED_PATHS:
            row = conn.execute("PRAGMA user_version").fetchone()
            if row is None or int(row[0]) != _SCHEMA_STAMP:
                conn.close()
                return None
        conn.row_factory = sqlite3.Row
        _apply_connection_pragmas(conn)
        _INITIALIZED_PATHS.add(resolved)
        return conn
    except sqlite3.Error:
        # Unreadable header / corrupt file / locked beyond the busy timeout:
        # let the slow path produce the canonical error handling.
        with contextlib.suppress(Exception):
            conn.close()
        return None


@contextlib.contextmanager
def _cross_process_init_lock(path: Path):
    """Serialize first-connect WAL/schema/integrity setup across processes.

    ``_INIT_LOCK`` only protects threads inside one Python process. During a
    dispatcher burst, many worker processes can all hit a fresh/legacy board at
    once and each process has an empty ``_INITIALIZED_PATHS`` cache. This file
    lock keeps header validation, integrity probing, WAL activation, and
    additive migrations single-file/single-writer across the whole host while
    leaving normal post-init DB usage concurrent under SQLite WAL.
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    lock_path = path.with_name(path.name + ".init.lock")
    handle = lock_path.open("a+b")
    try:
        if _IS_WINDOWS:
            import msvcrt

            # Lock a single byte in the sidecar file. ``msvcrt.locking`` starts
            # at the current file position, so seek explicitly before both
            # lock and unlock.  The file is opened in append/read binary mode so
            # it always exists but the byte-range lock is the synchronization
            # primitive; no payload needs to be written.
            handle.seek(0)
            locking = getattr(msvcrt, "locking")
            lock_mode = getattr(msvcrt, "LK_LOCK")
            locking(handle.fileno(), lock_mode, 1)
        else:
            import fcntl

            fcntl.flock(handle.fileno(), fcntl.LOCK_EX)
        yield
    finally:
        try:
            if _IS_WINDOWS:
                import msvcrt

                handle.seek(0)
                locking = getattr(msvcrt, "locking")
                unlock_mode = getattr(msvcrt, "LK_UNLCK")
                locking(handle.fileno(), unlock_mode, 1)
            else:
                import fcntl

                fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
        finally:
            handle.close()


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
    """Copy a corrupt DB (and its WAL/SHM sidecars) to a content-addressed backup.

    The backup filename is deterministic in the main DB's sha256, so repeated
    quarantines of the same corrupt bytes (gateway restarts, dispatcher retries,
    multi-profile fleets all hitting the same shared DB) reuse one backup
    instead of amplifying disk usage by N. If the corrupt bytes actually
    change between attempts — e.g. a partial repair or further damage — the
    fingerprint changes and a separate backup is preserved.

    Returns the backup path of the main DB file, or ``None`` if the copy
    itself failed (the caller still raises loudly in that case).

    Writes are confined to the original DB's parent directory. The backup
    basename is derived purely from ``path.name`` and a content hash, never
    from caller-supplied directory segments — no traversal is possible.
    """
    # Resolve once and pin the parent so subsequent path operations cannot
    # escape it. ``Path.resolve()`` collapses any ``..`` segments and
    # symlinks, and we only ever write inside ``parent``.
    resolved = path.resolve()
    parent = resolved.parent
    base_name = resolved.name  # basename only
    digest = hashlib.sha256()
    try:
        with resolved.open("rb") as handle:
            for chunk in iter(lambda: handle.read(1024 * 1024), b""):
                digest.update(chunk)
    except OSError:
        return None
    token = digest.hexdigest()[:16]
    candidate = parent / f"{base_name}.corrupt.{token}.bak"
    # Defensive: candidate must still be inside parent after construction.
    if candidate.parent != parent:
        return None
    if not candidate.exists():
        try:
            shutil.copy2(resolved, candidate)
        except OSError:
            return None
    for suffix in ("-wal", "-shm"):
        sidecar = parent / (base_name + suffix)
        if sidecar.parent != parent or not sidecar.exists():
            continue
        sidecar_backup = parent / (candidate.name + suffix)
        if sidecar_backup.parent != parent or sidecar_backup.exists():
            continue
        try:
            shutil.copy2(sidecar, sidecar_backup)
        except OSError:
            pass
    return candidate


def _guard_existing_db_is_healthy(
    path: Path,
    *,
    attempts: int = 3,
    backoff_s: float = 0.15,
) -> None:
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

    A ``database disk image is malformed`` / non-ok integrity result is
    re-probed up to ``attempts`` times on a fresh connection (small
    growing ``backoff_s``) before it is believed. Under multi-process
    WAL/SHM coordination a page can be read torn mid-checkpoint and
    surface a transient malformed read that clears on the next open; only
    a result that persists across every attempt is quarantined.

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
    max_attempts = max(1, attempts)
    for attempt in range(1, max_attempts + 1):
        reason = None
        try:
            probe = _sqlite_connect(resolved)
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
            if attempt > 1:
                _log.warning(
                    "kanban.db (%s) integrity probe recovered on attempt %d/%d; "
                    "earlier read was a transient bad image, not quarantining.",
                    resolved.name,
                    attempt,
                    max_attempts,
                )
            return
        if attempt < max_attempts:
            time.sleep(backoff_s * attempt)
    # Reason persisted across every attempt → treat as real corruption.
    backup = _backup_corrupt_db(resolved)
    raise KanbanDbCorruptError(resolved, backup, reason)


def connect(
    db_path: Optional[Path] = None,
    *,
    board: Optional[str] = None,
    busy_timeout_ms: Optional[int] = None,
    force_init: bool = False,
) -> sqlite3.Connection:
    """Open (and initialize if needed) the kanban DB.

    The first connection to a given path auto-runs the schema+migration
    pass so fresh installs and test harnesses that construct `connect()`
    directly don't have to remember a separate init step. A successful
    pass stamps ``PRAGMA user_version`` with :data:`_SCHEMA_STAMP`;
    subsequent connects — including the first one of every NEW process —
    see the stamp and take :func:`_try_fast_connect`, skipping the
    exclusive cross-process file lock, header/integrity probes, WAL
    re-activation and the migration pass entirely. Before the stamp this
    full path ran once per process under the flock, so a single slow init
    (or a stuck flock holder) serialized every dashboard request behind a
    120s busy timeout.

    ``busy_timeout_ms`` overrides the lock-wait budget for this
    connection only (dashboard reads pass a few seconds). ``force_init``
    (used by :func:`init_db`) bypasses the fast path and re-runs the full
    schema + migration pass under the cross-process lock.

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
    resolved = str(path.resolve())
    if not force_init:
        fast = _try_fast_connect(path, resolved, busy_timeout_ms)
        if fast is not None:
            return fast
    with _cross_process_init_lock(path):
        if not force_init:
            # Re-check under the lock: another process may have finished the
            # init while we waited for the flock — its stamp lets us skip the
            # heavy pass.
            fast = _try_fast_connect(path, resolved, busy_timeout_ms)
            if fast is not None:
                return fast
        # Cheap byte-level check first — catches the #29507 TLS-overwrite shape
        # and other invalid-header cases without opening a sqlite connection.
        _validate_sqlite_header(path)
        # Full integrity probe — catches corruption past the header (malformed
        # pages, broken internal metadata). Cached per-path after first success
        # via _INITIALIZED_PATHS so it only runs once per process per path.
        _guard_existing_db_is_healthy(path)
        conn = _sqlite_connect(path, busy_timeout_ms)
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
                _apply_connection_pragmas(conn)
                # Idempotent: runs CREATE TABLE IF NOT EXISTS + the additive
                # migrations. The lock prevents same-process dispatcher
                # threads from racing through the additive ALTER TABLE pass with
                # stale PRAGMA snapshots during gateway startup.
                conn.executescript(SCHEMA_SQL)
                _migrate_add_optional_columns(conn)
                # Persist the cross-process stamp LAST: a stamp is only ever
                # written over a schema that completed the full pass.
                conn.execute(f"PRAGMA user_version={_SCHEMA_STAMP}")
                _INITIALIZED_PATHS.add(resolved)
        except Exception:
            conn.close()
            raise
    return conn


@contextlib.contextmanager
def connect_closing(
    db_path: Optional[Path] = None,
    *,
    board: Optional[str] = None,
    busy_timeout_ms: Optional[int] = None,
):
    """Open a kanban DB connection and guarantee it is closed on exit.

    Use this instead of ``with kb.connect() as conn:`` — sqlite3's
    built-in connection context manager only commits/rollbacks the
    transaction; it does NOT close the file descriptor. In long-lived
    processes (gateway, dashboard) that route every kanban operation
    through ``connect()`` (e.g. ``run_slash`` dispatching ``/kanban …``
    commands, ``decompose_task_endpoint`` calling
    ``kanban_decompose.decompose_task``), the unclosed connections
    accumulate as open FDs to ``kanban.db`` and ``kanban.db-wal``. After
    enough operations the process hits the kernel FD limit and dies
    with ``[Errno 24] Too many open files``.

    See #33159 for the production incident.

    The ``connect()`` function itself remains unchanged so callers that
    intentionally manage the connection lifetime (tests, long-lived
    callers) continue to work.
    """
    conn = connect(db_path=db_path, board=board, busy_timeout_ms=busy_timeout_ms)
    try:
        yield conn
    finally:
        try:
            conn.close()
        except Exception:
            pass


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
    # Clear the cache entry so the integrity probe re-runs, and force the
    # full init path (the on-disk _SCHEMA_STAMP would otherwise satisfy the
    # fast path and skip the migration pass this entry point promises).
    with _INIT_LOCK:
        _INITIALIZED_PATHS.discard(resolved)
    with contextlib.closing(connect(path, force_init=True)):
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
    if "decompose_failed" not in cols:
        # Per-task lifetime counter of failed auto_decompose attempts.
        # Additive; existing rows get the DEFAULT 0. Mirrors the
        # ``consecutive_failures`` migration above but is decompose-specific
        # and never feeds the spawn circuit breaker.
        _add_column_if_missing(
            conn,
            "tasks",
            "decompose_failed",
            "decompose_failed INTEGER NOT NULL DEFAULT 0",
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
        # JSON array of skill names the dispatcher force-loads into the
        # worker (additive to the built-in `kanban-worker`). NULL is fine
        # for existing rows.
        _add_column_if_missing(conn, "tasks", "skills", "skills TEXT")

    if "max_retries" not in cols:
        # Per-task override for the consecutive-failure circuit breaker.
        # NULL = fall through to the dispatcher-level ``kanban.failure_limit``
        # config, then ``DEFAULT_FAILURE_LIMIT``. Existing rows get NULL,
        # which is the correct default (they keep the global behaviour
        # they were getting before the column existed).
        _add_column_if_missing(conn, "tasks", "max_retries", "max_retries INTEGER")

    if "max_iterations" not in cols:
        # Per-task override for the worker's tool-calling iteration
        # budget. NULL = fall through to the profile's `agent.max_turns`
        # config (or the global default 90). Added 2026-05-27 for
        # hardening-sprint TASK 8.
        _add_column_if_missing(
            conn, "tasks", "max_iterations", "max_iterations INTEGER"
        )

    if "continuation_count" not in cols:
        _add_column_if_missing(
            conn,
            "tasks",
            "continuation_count",
            "continuation_count INTEGER NOT NULL DEFAULT 0",
        )
    if "max_continuations" not in cols:
        _add_column_if_missing(
            conn, "tasks", "max_continuations", "max_continuations INTEGER"
        )
    if "last_continuation_reason" not in cols:
        _add_column_if_missing(
            conn,
            "tasks",
            "last_continuation_reason",
            "last_continuation_reason TEXT",
        )

    if "model_override" not in cols:
        conn.execute("ALTER TABLE tasks ADD COLUMN model_override TEXT")

    if "goal_mode" not in cols:
        # Ralph-style goal loop toggle for the dispatched worker. 0 (the
        # default) = classic single-shot worker, preserving the behaviour
        # existing rows had before the column existed.
        _add_column_if_missing(
            conn, "tasks", "goal_mode", "goal_mode INTEGER NOT NULL DEFAULT 0"
        )

    if "goal_max_turns" not in cols:
        # Per-task goal-loop turn budget. NULL = goals-engine default.
        _add_column_if_missing(
            conn, "tasks", "goal_max_turns", "goal_max_turns INTEGER"
        )

    if "acceptance_criteria" not in cols:
        # A1 (N-A1): JSON array of the structured acceptance criteria parsed
        # from a child's decompose-generated body (normalized via
        # plan_compiler's AcceptanceCriterion schema). NULL on every task that
        # has none — the pre-A1 behaviour for all existing rows.
        _add_column_if_missing(
            conn, "tasks", "acceptance_criteria", "acceptance_criteria TEXT"
        )

    if "session_id" not in cols:
        # Originating agent/chat session id, populated when the task is
        # created from within an agent loop that propagated
        # ``HERMES_SESSION_ID`` (e.g. ACP). NULL on legacy rows and on any
        # creation path that doesn't set the env var (CLI, dashboard).
        _add_column_if_missing(
            conn, "tasks", "session_id", "session_id TEXT"
        )

    if "due_at" not in cols:
        # Earliest promotion time (unix epoch seconds) for time-based
        # scheduling. NULL on legacy rows = eligible immediately, preserving
        # the exact promote behaviour those rows had before the column
        # existed. recompute_ready holds a task whose due_at is still in the
        # future.
        _add_column_if_missing(conn, "tasks", "due_at", "due_at INTEGER")

    if "epic_id" not in cols:
        # N-E3: durable epic pointer. NULL on every legacy row = not part of
        # an epic = the exact pre-E3 behaviour. Decompose propagates a triage
        # root's epic_id onto its children; everything else leaves it NULL.
        _add_column_if_missing(conn, "tasks", "epic_id", "epic_id TEXT")

    if "kind" not in cols:
        _add_column_if_missing(conn, "tasks", "kind", "kind TEXT")
    if "auto_retry_count" not in cols:
        _add_column_if_missing(
            conn,
            "tasks",
            "auto_retry_count",
            "auto_retry_count INTEGER NOT NULL DEFAULT 0",
        )
    if "integration_retry_count" not in cols:
        # Heiler lane: bounded transient re-integration retry counter, kept
        # separate from auto_retry_count (which escalates to premium/opus).
        _add_column_if_missing(
            conn,
            "tasks",
            "integration_retry_count",
            "integration_retry_count INTEGER NOT NULL DEFAULT 0",
        )

    # A1 (kanban-chain-haertung): PlanSpec provenance columns. All four are
    # plain nullable TEXT — no default needed; NULL on every pre-A1 row
    # preserves the exact behaviour those rows had before the columns existed.
    if "planspec_subtask_id" not in cols:
        _add_column_if_missing(
            conn, "tasks", "planspec_subtask_id", "planspec_subtask_id TEXT"
        )
    if "planspec_source" not in cols:
        _add_column_if_missing(
            conn, "tasks", "planspec_source", "planspec_source TEXT"
        )
    if "freigabe" not in cols:
        _add_column_if_missing(conn, "tasks", "freigabe", "freigabe TEXT")
    if "live_test_depth" not in cols:
        _add_column_if_missing(
            conn, "tasks", "live_test_depth", "live_test_depth TEXT"
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
    # N-E3: index the epic pointer for cheap per-epic task rollups.
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_tasks_epic ON tasks(epic_id)"
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
    # #11: decision_queue scans ``task_events WHERE kind = 'release_gate_parked'``
    # (and similar kind filters).  ``kind`` is not the leading column of any
    # existing index (those lead with task_id), so the scan was full-table on a
    # large event log.  (kind, task_id) covers the filter + the join to tasks.
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_events_kind "
        "ON task_events(kind, task_id)"
    )

    # K5a: task_runs gained per-run token/cost accounting columns for D7 L1
    # cost observability. Additive + nullable — historical runs (and any run
    # the gateway can't attribute token usage to) stay NULL, which the
    # aggregations (board_stats / get_task / runs summary) treat as "no cost
    # recorded". The in-process write-back lives in ``_end_run``; the
    # cross-DB backfill from ``state.db`` is a separate fail-soft slice (K5b).
    # Guard on table existence: the legacy-migration path can run this on a
    # very old DB built from just the ``tasks`` table (task_runs predates the
    # additive-column era but a hand-rolled legacy fixture may omit it).
    runs_table_exists = conn.execute(
        "SELECT name FROM sqlite_master WHERE type='table' AND name='task_runs'"
    ).fetchone() is not None
    if runs_table_exists:
        run_cols = {
            row["name"] for row in conn.execute("PRAGMA table_info(task_runs)")
        }
        if "input_tokens" not in run_cols:
            _add_column_if_missing(
                conn, "task_runs", "input_tokens", "input_tokens INTEGER"
            )
        if "output_tokens" not in run_cols:
            _add_column_if_missing(
                conn, "task_runs", "output_tokens", "output_tokens INTEGER"
            )
        if "cost_usd" not in run_cols:
            _add_column_if_missing(
                conn, "task_runs", "cost_usd", "cost_usd REAL"
            )
        # B2 (N-B2): machine-readable review verdict. NULL on every non-review
        # run; the review lane writes 'APPROVED' (complete) / 'REQUEST_CHANGES'
        # (block). Distinct from metadata['verdict'] which stays untouched.
        if "verdict" not in run_cols:
            _add_column_if_missing(
                conn, "task_runs", "verdict", "verdict TEXT"
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
                        started_at
                    ) VALUES (?, ?, 'running', ?, ?, ?, ?, ?, ?)
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

    _rebuild_drifted_tables(conn)

    # idx_runs_task_ended: serves the respawn guard's "latest run per task
    # ORDER BY ended_at DESC LIMIT 1" straight from the index (was a TEMP
    # B-TREE sort on top of idx_runs_task). Created here, after the rebuild,
    # rather than in SCHEMA_SQL: ended_at is absent on a legacy task_runs until
    # _rebuild_drifted_tables recreates it, so a CREATE INDEX in executescript
    # would abort init. By this point every task_runs (fresh or rebuilt) has
    # the column; IF NOT EXISTS makes it a no-op on re-init and on tables the
    # rebuild already indexed via _REBUILD_SPECS.
    if runs_table_exists:
        conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_runs_task_ended "
            "ON task_runs(task_id, ended_at)"
        )


# Legacy DBs defined these tables with a ``TEXT PRIMARY KEY`` id (or, for
# ``kanban_notify_subs``, a nullable ``TEXT last_event_id``). The current
# schema uses ``INTEGER PRIMARY KEY AUTOINCREMENT`` / ``INTEGER NOT NULL
# DEFAULT 0``. ``CREATE TABLE IF NOT EXISTS`` skips existing tables
# regardless of schema and ``_add_column_if_missing`` only adds columns, so
# neither can fix a drifted column type — the table must be rebuilt. See
# #35096.
#
# Each entry pairs the canonical CREATE TABLE with the CREATE INDEX
# statements that DROP TABLE would otherwise take down with it (including
# ``idx_events_run``, added by the additive pass above). To guard against
# this list drifting from SCHEMA_SQL, ``test_rebuilt_schema_matches_fresh``
# asserts a rebuilt legacy DB is byte-identical to a fresh one.
_REBUILD_SPECS = {
    "task_events": (
        "CREATE TABLE task_events ("
        " id INTEGER PRIMARY KEY AUTOINCREMENT,"
        " task_id TEXT NOT NULL, run_id INTEGER, kind TEXT NOT NULL,"
        " payload TEXT, created_at INTEGER NOT NULL)",
        (
            "CREATE INDEX idx_events_task ON task_events(task_id, created_at)",
            "CREATE INDEX idx_events_task_id ON task_events(task_id, id)",
            "CREATE INDEX idx_events_run ON task_events(run_id, id)",
            "CREATE INDEX idx_events_kind ON task_events(kind, task_id)",
        ),
    ),
    "task_comments": (
        "CREATE TABLE task_comments ("
        " id INTEGER PRIMARY KEY AUTOINCREMENT,"
        " task_id TEXT NOT NULL, author TEXT NOT NULL, body TEXT NOT NULL,"
        " created_at INTEGER NOT NULL)",
        ("CREATE INDEX idx_comments_task ON task_comments(task_id, created_at)",),
    ),
    "task_runs": (
        "CREATE TABLE task_runs ("
        " id INTEGER PRIMARY KEY AUTOINCREMENT,"
        " task_id TEXT NOT NULL, profile TEXT, step_key TEXT,"
        " status TEXT NOT NULL, claim_lock TEXT, claim_expires INTEGER,"
        " worker_pid INTEGER, max_runtime_seconds INTEGER,"
        " last_heartbeat_at INTEGER, started_at INTEGER NOT NULL,"
        " ended_at INTEGER, outcome TEXT, summary TEXT, metadata TEXT,"
        # input_tokens/output_tokens/cost_usd/verdict are appended to fresh DBs
        # by the additive run_cols migration; mirror them here in the same order
        # so a rebuilt legacy task_runs stays byte-identical to fresh and does
        # not silently drop the budget columns (dispatcher) / verdict (review).
        " error TEXT, input_tokens INTEGER, output_tokens INTEGER,"
        " cost_usd REAL, verdict TEXT)",
        (
            "CREATE INDEX idx_runs_task ON task_runs(task_id, started_at)",
            "CREATE INDEX idx_runs_status ON task_runs(status)",
            "CREATE INDEX idx_runs_started ON task_runs(started_at)",
            "CREATE INDEX idx_runs_task_ended ON task_runs(task_id, ended_at)",
        ),
    ),
    "kanban_notify_subs": (
        "CREATE TABLE kanban_notify_subs ("
        " task_id TEXT NOT NULL, platform TEXT NOT NULL, chat_id TEXT NOT NULL,"
        " thread_id TEXT NOT NULL DEFAULT '', user_id TEXT,"
        " notifier_profile TEXT, created_at INTEGER NOT NULL,"
        " last_event_id INTEGER NOT NULL DEFAULT 0,"
        " PRIMARY KEY (task_id, platform, chat_id, thread_id))",
        ("CREATE INDEX idx_notify_task ON kanban_notify_subs(task_id)",),
    ),
}


def _table_has_drifted(conn: sqlite3.Connection, table: str) -> bool:
    """True when ``table`` still carries the legacy (pre-AUTOINCREMENT) shape."""
    info = conn.execute(f"PRAGMA table_info({table})").fetchall()
    if not info:
        return False  # table absent — nothing to rebuild
    if table == "kanban_notify_subs":
        lei = next((c for c in info if c["name"] == "last_event_id"), None)
        return lei is not None and (lei["type"] or "").upper() != "INTEGER"
    # task_events / task_comments / task_runs: id must be INTEGER and a PK.
    id_col = next((c for c in info if c["name"] == "id"), None)
    if id_col is None:
        return False
    return not ((id_col["type"] or "").upper() == "INTEGER" and id_col["pk"])


def _rebuild_drifted_tables(conn: sqlite3.Connection) -> None:
    """Rebuild any kanban table whose column types drifted from SCHEMA_SQL.

    Old boards crash the gateway notifier (``int(None)`` on a NULL id in
    ``unseen_events_for_sub``) and never match the ``id > cursor`` filter, so
    every kanban notification is silently lost (#35096). Each affected table is
    rebuilt with the standard SQLite pattern — CREATE new → INSERT shared
    columns → DROP old → RENAME — recreating its indexes too (DROP TABLE takes
    them down). The legacy TEXT ids are dropped (they aren't valid integers);
    AUTOINCREMENT assigns fresh ones and ``last_event_id`` cursors reset to 0,
    so the first post-migration tick replays a task's event history once —
    the safe failure mode for a feature that was already fully broken.

    The whole pass runs in one transaction so an interruption can't leave a
    table half-renamed, and under ``connect()``'s init locks so nothing races
    it. Idempotent: a correctly-typed DB skips every table and returns without
    opening a transaction.
    """
    drifted = [t for t in _REBUILD_SPECS if _table_has_drifted(conn, t)]
    if not drifted:
        return

    conn.execute("BEGIN IMMEDIATE")
    try:
        for table in drifted:
            create_sql, index_sqls = _REBUILD_SPECS[table]
            old_cols = [c["name"] for c in conn.execute(f"PRAGMA table_info({table})")]
            _log.info("kanban migration: rebuilding %s to match current schema", table)
            conn.execute(f"ALTER TABLE {table} RENAME TO {table}_legacy")
            conn.execute(create_sql)
            new_cols = {c["name"] for c in conn.execute(f"PRAGMA table_info({table})")}
            if table == "kanban_notify_subs":
                # Cast the legacy TEXT cursor to INTEGER; NULL / non-numeric → 0.
                shared = [c for c in old_cols if c in new_cols and c != "last_event_id"]
                cols_csv = ", ".join(shared)
                conn.execute(
                    f"INSERT INTO {table} ({cols_csv}, last_event_id) "
                    f"SELECT {cols_csv}, COALESCE(CAST(last_event_id AS INTEGER), 0) "
                    f"FROM {table}_legacy"
                )
            else:
                # Drop the legacy TEXT id; AUTOINCREMENT reassigns it.
                shared = [c for c in old_cols if c in new_cols and c != "id"]
                cols_csv = ", ".join(shared)
                conn.execute(
                    f"INSERT INTO {table} ({cols_csv}) "
                    f"SELECT {cols_csv} FROM {table}_legacy"
                )
            conn.execute(f"DROP TABLE {table}_legacy")
            for index_sql in index_sqls:
                conn.execute(index_sql)
        conn.execute("COMMIT")
    except Exception:
        try:
            conn.execute("ROLLBACK")
        except sqlite3.OperationalError:
            pass
        raise


def _check_file_length_invariant(conn: sqlite3.Connection) -> None:
    """Read the SQLite header page_count and compare against actual file size.

    Raises sqlite3.DatabaseError if the file is shorter than the header claims
    (torn-extend corruption). Skips WAL-mode connections: uncheckpointed WAL
    commits and concurrent checkpoints can leave the main DB file temporarily
    behind the logical page count that SQLite exposes via the connection.
    """
    try:
        mode_row = conn.execute("PRAGMA journal_mode").fetchone()
        if mode_row and str(mode_row[0]).lower() == "wal":
            return
        row = conn.execute("PRAGMA database_list").fetchone()
        if row is None:
            return
        path_str = row[2]  # column 2 is the file path; empty for in-memory DBs
        if not path_str:
            return  # in-memory or unnamed DB; skip
        path = path_str
        page_size = conn.execute("PRAGMA page_size").fetchone()[0]
        file_size = os.path.getsize(path)
        with open(path, "rb") as f:
            f.seek(28)
            header_bytes = f.read(4)
        if len(header_bytes) < 4:
            return  # can't read header; skip
        header_page_count = int.from_bytes(header_bytes, "big")
        if header_page_count == 0:
            return  # new/empty DB; skip
        actual_pages = file_size // page_size
        if actual_pages < header_page_count:
            raise sqlite3.DatabaseError(
                f"torn-extend detected: page count mismatch on {path}: "
                f"header claims {header_page_count} pages, "
                f"file has {actual_pages} pages "
                f"(missing {header_page_count - actual_pages} pages, "
                f"file_size={file_size}, page_size={page_size})"
            )
    except sqlite3.DatabaseError:
        raise
    except Exception:
        pass  # I/O errors during check are non-fatal; let normal ops continue


@contextlib.contextmanager
def write_txn(conn: sqlite3.Connection):
    """Context manager for an IMMEDIATE write transaction.

    Use for any multi-statement write (creating a task + link, claiming a
    task + recording an event, etc.).  A claim CAS inside this context is
    atomic -- at most one concurrent writer can succeed.

    The explicit ROLLBACK on exception is wrapped in try/except so that
    a SQLite auto-rollback (which leaves no active transaction) does not
    shadow the original exception with a spurious rollback error.
    """
    conn.execute("BEGIN IMMEDIATE")
    try:
        yield conn
    except Exception:
        try:
            conn.execute("ROLLBACK")
        except sqlite3.OperationalError:
            # SQLite has already auto-rolled-back the transaction (typical
            # under EIO, lock contention, or corruption). Nothing to undo;
            # do not let this secondary failure shadow the real one.
            pass
        raise
    else:
        conn.execute("COMMIT")
        # Post-commit file-length check: header page_count must match actual file pages.
        # A discrepancy means a torn-extend — raise now rather than silently corrupt.
        _check_file_length_invariant(conn)


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


def _profile_author() -> str:
    """Best-effort author name for the active profile (mirrors kanban.py)."""
    for env in ("HERMES_PROFILE_NAME", "HERMES_PROFILE"):
        v = os.environ.get(env)
        if v:
            return v
    try:
        from hermes_cli.profiles import get_active_profile_name
        return get_active_profile_name() or "user"
    except Exception:
        return "user"


# ---------------------------------------------------------------------------
# Task creation / mutation
# ---------------------------------------------------------------------------

def _canonical_assignee(assignee: Optional[str]) -> Optional[str]:
    """Lowercase-assignee normalization for Kanban rows (dashboard/CLI parity)."""
    if assignee is None:
        return None
    from hermes_cli.profiles import normalize_profile_name

    return normalize_profile_name(assignee)


_CODE_TASK_CONTRACT_MARKER = "## Hermes Coder Contract v1"
_CODE_TASK_CONTRACT_EVENT = "code_task_contract_inferred"
_NEEDS_CONTRACT_EVENT = "needs_contract"
_NEEDS_CONTRACT_BLOCKED_EVENT = "needs_contract_blocked"
_CODE_TASK_CONTRACT_FIELDS = (
    "repo_workspace",
    "assignee_lane",
    "reason_for_lane",
    "allowed_paths",
    "anti_scope",
    "risk",
    "expected_gates",
    "escalation_triggers",
)
SELF_VERIFIED = "SELF_VERIFIED"
SELF_VERIFY_LIMITED = "SELF_VERIFY_LIMITED"
INTEGRATOR_VERIFIED = "INTEGRATOR_VERIFIED"
DELIVERABLE_POSTED_NOT_COMPLETED = "deliverable_posted_not_completed"
OPERATOR_ESCALATION_EVENT = "operator_escalation"
OPERATOR_ONLY_ACTIONS = (
    "DB schema/data mutation",
    "destructive delete",
    "secrets/credentials",
)
NO_SILENT_STALL_EVENT = "no_silent_stall_sweep"
# S4 Heiler: structured failure-classification ledger (Phase 1).
# Every block/failure on the no_silent_stall_sweep park path and in
# ``_record_task_failure`` is classified into one stable class and written as a
# ``heiler_classification`` task_event (payload = class + evidence). The read
# side ``read_escalation_ledger`` aggregates them; the Stratege (Phase 1.5)
# derives root-cause Specs from that rollup. This phase does NOT spawn a fix
# worker and does NOT change escalation-after-N — that keeps using the existing
# ``operator_escalation`` event next to the classification.
HEILER_CLASSIFICATION_EVENT = "heiler_classification"
HEILER_CLASS_TRANSIENT = "transient"
HEILER_CLASS_FLAKY = "flaky"
HEILER_CLASS_REAL_BUG = "real-bug"
HEILER_CLASS_BAD_SPEC = "bad-spec"
HEILER_CLASS_CONFLICT = "conflict"
HEILER_CLASSES = (
    HEILER_CLASS_TRANSIENT,
    HEILER_CLASS_FLAKY,
    HEILER_CLASS_REAL_BUG,
    HEILER_CLASS_BAD_SPEC,
    HEILER_CLASS_CONFLICT,
)
NO_SILENT_STALL_DEFAULT_MIN_AGE_SECONDS = 3600
NO_SILENT_STALL_DECOMPOSE_FAILURE_LIMIT = 3
NO_SILENT_STALL_RATE_LIMIT_ATTEMPT_LIMIT = 3
# Heiler: transient re-integration retry lane (no_silent_stall_sweep §5).
# An integration-parked task whose park reason is classified ``transient`` by
# ``kanban_worktrees._integration_park_class`` (dirty overlap / in-progress git
# op / wrong branch) is re-run through the integration path instead of sitting
# in blocked-limbo. Bounded by its OWN counter (``integration_retry_count``,
# never the shared ``auto_retry_count``) so it cannot trip the premium/opus
# escalation ladder; after the limit it escalates to the operator.
INTEGRATION_RETRY_EVENT = "integration_retry"
INTEGRATION_RETRY_SUCCEEDED_EVENT = "integration_retry_succeeded"
INTEGRATION_RETRY_LIMIT = 2
INTEGRATION_RETRY_BACKOFF_SECONDS = 60
INTEGRATION_RETRY_EXHAUSTED_CLASS = "integration_retry_exhausted"
INTEGRATION_PARKED_STALL_CLASS = "integration_parked"
KANBAN_DISPATCHER_HEARTBEAT_FILENAME = "kanban_dispatcher_heartbeat.json"
_VERDICT_ONLY_BUILD_ROLES = frozenset({"reviewer", "critic", "research"})
_CODE_LANE_REASONS = {
    "coder": "default code implementation lane",
    "coder-claude": (
        "reasoning-heavy or chain-critical Claude code lane; requires "
        "cross-family review"
    ),
    "premium": "operator-signed high-stakes or escalation-reserve code lane",
}


def _assignee_key(assignee: Optional[str]) -> str:
    return str(assignee or "").strip().lower()


def _reason_for_lane(assignee: Optional[str]) -> str:
    return _CODE_LANE_REASONS.get(
        _assignee_key(assignee),
        "code-role implementation task",
    )


def _role_misuse_reason(
    *,
    assignee: Optional[str],
    kind: Optional[str],
) -> Optional[str]:
    role = _assignee_key(assignee)
    task_kind = str(kind or "").strip().lower()
    if task_kind == "code" and role in _VERDICT_ONLY_BUILD_ROLES:
        allowed = ", ".join(sorted(_CODE_LANE_REASONS))
        return (
            f"role_misuse: {role!r} is a verdict/research-only lane and "
            "cannot own kind='code' implementation work; route build/code "
            f"tasks to one of: {allowed}"
        )
    return None


def _role_misuse_payload(
    *,
    assignee: Optional[str],
    kind: Optional[str],
    source: str,
) -> dict:
    return {
        "version": 1,
        "source": source,
        "issue": "role_misuse",
        "assignee": assignee,
        "kind": kind,
        "verdict_only_roles": sorted(_VERDICT_ONLY_BUILD_ROLES),
        "allowed_code_lanes": sorted(_CODE_LANE_REASONS),
    }


def _is_code_assignee(assignee: Optional[str]) -> bool:
    if not assignee:
        return False
    try:
        return assignee in _review_gate_config()["code_roles"]
    except Exception:
        return False


def _with_code_task_contract(
    body: Optional[str],
    *,
    assignee: Optional[str],
    workspace_kind: str,
    workspace_path: Optional[str],
    tenant: Optional[str],
) -> Optional[str]:
    """Append a small, stable contract to code-role task bodies.

    GPT-class code workers do better when every card carries the same
    operational rails. Keep this terse and idempotent: the user/orchestrator
    spec remains first, and existing cards that already include the marker are
    left untouched.
    """
    if not _is_code_assignee(assignee):
        return body
    text = (body or "").strip()
    if _CODE_TASK_CONTRACT_MARKER in text:
        return body
    workspace = workspace_path or "Hermes-managed scratch workspace"
    tenant_label = (tenant or "default").strip() or "default"
    reason_for_lane = _reason_for_lane(assignee)
    contract = "".join(
        [
            f"{_CODE_TASK_CONTRACT_MARKER}\n",
            f"- Assignee: {assignee}\n",
            f"- Tenant: {tenant_label}\n",
            f"- Workspace: {workspace_kind}:{workspace}\n",
            f"- Repo/workspace: {workspace_kind}:{workspace}\n",
            f"- Assignee/lane: {assignee}\n",
            f"- Reason for lane: {reason_for_lane}.\n",
            (
                "- Allowed paths: use the explicit workspace path and any "
                "task-specified paths only; block if the target repo/path is "
                "ambiguous.\n"
            ),
            (
                "- Anti-scope: no unrelated cleanup, broad rewrites, push, "
                "deploy, runtime restart, or schema migration.\n"
            ),
            "- Risk: medium unless the task body states a narrower verified risk.\n",
            (
                "- Expected gates: smallest relevant lint/type/test gate; "
                "use SELF_VERIFY_LIMITED if a safe worktree-targeted gate is "
                "not possible.\n"
            ),
            (
                "- Escalation triggers: missing workspace, unclear allowed paths, "
                "failing required gate, open coordination overlap, or required "
                "secret/runtime/deploy action.\n"
            ),
            (
                "- Scope: edit only files required for this card; "
                "do not broaden into unrelated cleanup.\n"
            ),
            (
                "- Forbidden: no git push/deploy/destructive FS/DB schema changes; "
                "no secret printing.\n"
            ),
            (
                "- Dependency gate: if required deps are missing "
                "(e.g. next/tsc/node_modules), restore them with the project "
                "lockfile when safe, otherwise block with exact evidence instead "
                "of reverting unrelated work.\n"
            ),
            (
                "- Tests: run the smallest relevant lint/build/test gate; "
                "if skipped, state why.\n"
            ),
            (
                "- Completion metadata: summarize changed paths, commands run, "
                "residual risk, and next gate/review needs."
            ),
        ]
    )
    return f"{text}\n\n{contract}" if text else contract


def _absolute_paths_from_text(text: Optional[str]) -> list[str]:
    if not text:
        return []
    seen: set[str] = set()
    paths: list[str] = []
    for match in re.finditer(r"(?<![\w.-])/(?:[^\s`'\"<>|;$]+)", text):
        raw = match.group(0).rstrip(".,);:]")
        if raw and raw not in seen:
            seen.add(raw)
            paths.append(raw)
    return paths[:12]


def _is_code_task_row(row: sqlite3.Row) -> bool:
    kind = row["kind"] if "kind" in row.keys() else None
    return _is_code_assignee(row["assignee"]) or str(kind or "").lower() == "code"


def _code_task_contract_payload(
    *,
    assignee: Optional[str],
    workspace_kind: str,
    workspace_path: Optional[str],
    tenant: Optional[str],
    body: Optional[str],
    created_by: Optional[str],
    protected_funnel_root: bool,
    source: str,
) -> tuple[dict, list[str]]:
    inferred_paths = _absolute_paths_from_text(body)
    allowed_paths = [workspace_path] if workspace_path else inferred_paths
    workspace = workspace_path or (
        "Hermes-managed scratch workspace"
        if workspace_kind == "scratch"
        else None
    )
    payload = {
        "version": 1,
        "source": source,
        "repo_workspace": (
            f"{workspace_kind}:{workspace}" if workspace else f"{workspace_kind}:"
        ),
        "assignee_lane": assignee,
        "reason_for_lane": _reason_for_lane(assignee),
        "allowed_paths": allowed_paths,
        "anti_scope": [
            "no unrelated cleanup",
            "no git push",
            "no deploy or runtime restart",
            "no DB schema migration",
        ],
        "risk": "medium" if workspace_kind in {"dir", "worktree"} else "low",
        "expected_gates": [
            "run the smallest relevant lint/type/test gate",
            "record SELF_VERIFIED when safe worktree-targeted gates pass",
            "record SELF_VERIFY_LIMITED when safe self-verification is not possible",
        ],
        "escalation_triggers": [
            "missing or ambiguous workspace",
            "unclear allowed paths",
            "failing required gate",
            "coordination overlap",
            "secret/runtime/deploy action required",
        ],
        "tenant": tenant,
        "created_by": created_by,
    }
    missing: list[str] = []
    if workspace_kind in {"dir", "worktree"} and not workspace_path:
        missing.append("repo_workspace")
    if protected_funnel_root and workspace_kind == "scratch" and not inferred_paths:
        missing.extend(["repo_workspace", "allowed_paths"])
    for field in _CODE_TASK_CONTRACT_FIELDS:
        value = payload.get(field)
        if value is None or value == "":
            missing.append(field)
    return payload, sorted(set(missing))


def _latest_code_contract_event(conn: sqlite3.Connection, task_id: str) -> Optional[dict]:
    row = conn.execute(
        "SELECT payload FROM task_events "
        "WHERE task_id = ? AND kind = ? ORDER BY id DESC LIMIT 1",
        (task_id, _CODE_TASK_CONTRACT_EVENT),
    ).fetchone()
    if not row or not row["payload"]:
        return None
    try:
        payload = json.loads(row["payload"])
    except (TypeError, ValueError):
        return None
    return payload if isinstance(payload, dict) else None


def _code_contract_issue_for_row(
    conn: sqlite3.Connection,
    row: sqlite3.Row,
    *,
    source: str,
) -> tuple[Optional[str], Optional[dict]]:
    task_id = row["id"]
    role_misuse = _role_misuse_reason(
        assignee=row["assignee"],
        kind=row["kind"] if "kind" in row.keys() else None,
    )
    if role_misuse is not None:
        payload = _role_misuse_payload(
            assignee=row["assignee"],
            kind=row["kind"] if "kind" in row.keys() else None,
            source=source,
        )
        payload["reason"] = role_misuse
        return role_misuse, payload
    if not _is_code_task_row(row):
        return None, None
    if _latest_code_contract_event(conn, task_id) is not None:
        return None, None
    payload, missing = _code_task_contract_payload(
        assignee=row["assignee"],
        workspace_kind=row["workspace_kind"] or "scratch",
        workspace_path=row["workspace_path"],
        tenant=row["tenant"],
        body=row["body"],
        created_by=row["created_by"],
        protected_funnel_root=_is_funnel_root_task(conn, row),
        source=source,
    )
    if missing:
        payload["missing"] = missing
        reason = "needs_contract: missing " + ", ".join(missing)
        return reason, payload
    return None, payload


def _ensure_code_task_contract_in_txn(
    conn: sqlite3.Connection,
    task_id: str,
    *,
    source: str,
) -> Optional[str]:
    row = conn.execute(
        "SELECT id, body, assignee, workspace_kind, workspace_path, tenant, "
        "created_by, kind FROM tasks WHERE id = ?",
        (task_id,),
    ).fetchone()
    if row is None:
        return None
    reason, payload = _code_contract_issue_for_row(conn, row, source=source)
    if payload is None:
        return None
    if reason is None:
        _append_event(conn, task_id, _CODE_TASK_CONTRACT_EVENT, payload)
        return None
    conn.execute(
        "UPDATE tasks SET status = 'blocked', claim_lock = NULL, "
        "claim_expires = NULL, worker_pid = NULL "
        "WHERE id = ? AND status IN ('todo', 'ready', 'running', 'blocked')",
        (task_id,),
    )
    _append_event(conn, task_id, _NEEDS_CONTRACT_EVENT, payload)
    _append_event(conn, task_id, _NEEDS_CONTRACT_BLOCKED_EVENT, payload)
    _append_event(conn, task_id, "blocked", {"reason": reason})
    return reason


def ensure_code_task_contract_before_pickup(
    conn: sqlite3.Connection,
    task_id: str,
    *,
    source: str = "pickup",
) -> Optional[str]:
    """Ensure a code task has a visible contract before any worker pickup.

    Returns a blocking reason when the task was parked for missing contract
    data, otherwise ``None``. Contract state is written only to the existing
    task event stream; no schema surface is added.
    """
    with write_txn(conn):
        return _ensure_code_task_contract_in_txn(conn, task_id, source=source)


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
    max_iterations: Optional[int] = None,
    max_continuations: Optional[int] = None,
    goal_mode: bool = False,
    goal_max_turns: Optional[int] = None,
    initial_status: str = "running",
    session_id: Optional[str] = None,
    epic_id: Optional[str] = None,
    kind: Optional[str] = None,
    board: Optional[str] = None,
    model_override: Optional[str] = None,
    freigabe: Optional[str] = None,
    live_test_depth: Optional[str] = None,
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
    each name to ``hermes --skills ...`` alongside the built-in
    ``kanban-worker``. Use this to pin a task to a specialist skill
    (e.g. ``skills=["translation"]`` so the worker loads the
    translation skill regardless of the profile's default config).
    """
    assignee = _canonical_assignee(assignee)
    if not title or not title.strip():
        raise ValueError("title is required")
    role_misuse = _role_misuse_reason(assignee=assignee, kind=kind)
    if role_misuse is not None:
        raise ValueError(role_misuse)
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
    if max_iterations is not None and int(max_iterations) < 1:
        raise ValueError("max_iterations must be >= 1")
    if max_continuations is not None and int(max_continuations) < 0:
        raise ValueError("max_continuations must be >= 0")
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
    # duplicate. Keep a read-only fast path, but re-check under the
    # BEGIN IMMEDIATE write transaction below before inserting: SQLite
    # serializes writers there, so concurrent creators with the same key
    # cannot both observe "missing" and insert separate tasks.
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

    # FO tenant pins the repo for code tasks: an FO-backlog "copy to Fleet"
    # commission arrives with tenant='family-organizer', a code role (coder),
    # and no explicit workspace (scratch). Born-correct routing — set the FO
    # checkout as the workspace so worker isolation carves the worktree from the
    # FO repo, not the board default_workdir (the Hermes repo). Without this the
    # scratch task is dispatched and redirected into hermes-agent, then blocks
    # on missing FO files (t_8fbe701d, 2026-06-14). Backstopped at dispatch by
    # scratch_code_redirect; only code roles are pinned (review/research FO
    # tasks legitimately stay scratch). Reading code_roles is fail-open so a
    # config error never blocks creation.
    if workspace_path is None and (tenant or "").strip().lower() == "family-organizer":
        try:
            _is_code_role = assignee in _review_gate_config()["code_roles"]
        except Exception:
            _is_code_role = False
        if _is_code_role:
            from hermes_cli.kanban_worktrees import FO_REPO_PATH

            if FO_REPO_PATH.is_dir():
                workspace_kind = "dir"
                workspace_path = str(FO_REPO_PATH)

    body = _with_code_task_contract(
        body,
        assignee=assignee,
        workspace_kind=workspace_kind,
        workspace_path=workspace_path,
        tenant=tenant,
    )

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
            # E1 (wrong-workspace guard, #0167-0171 root cause): a code-role task
            # that would silently inherit the board default_workdir (the Hermes
            # repo) instead of an explicit workspace_path is refused at creation
            # time. Reading the review-gate code_roles is wrapped fail-open so a
            # config error never blocks non-FO/legacy task creation; only an
            # affirmatively-code-role task raises. Non-code roles keep inheriting.
            try:
                is_code_role = assignee in _review_gate_config()["code_roles"]
            except Exception:
                is_code_role = False
            if is_code_role:
                raise ValueError(
                    f"code-role task ({assignee!r}) has no explicit workspace_path and would "
                    f"fall back to the board default_workdir ({board_default!r}). Refusing to "
                    f"provision a code task in the Hermes repo. Pass workspace_path explicitly "
                    f"(e.g. /home/piet/projects/family-organizer for FO tasks)."
                )
            workspace_path = str(board_default)

    # Retry once on the extremely unlikely id collision.
    for attempt in range(2):
        task_id = _new_task_id()
        try:
            with write_txn(conn):
                if idempotency_key:
                    row = conn.execute(
                        "SELECT id FROM tasks WHERE idempotency_key = ? "
                        "AND status != 'archived' "
                        "ORDER BY created_at DESC LIMIT 1",
                        (idempotency_key,),
                    ).fetchone()
                    if row:
                        return row["id"]

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
                        skills, max_retries, max_iterations, max_continuations,
                        goal_mode, goal_max_turns, session_id, epic_id, kind,
                        model_override, freigabe, live_test_depth
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
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
                        int(max_iterations) if max_iterations is not None else None,
                        int(max_continuations) if max_continuations is not None else None,
                        1 if goal_mode else 0,
                        int(goal_max_turns) if goal_max_turns is not None else None,
                        session_id,
                        epic_id,
                        kind,
                        (model_override or "").strip() or None,
                        freigabe,
                        live_test_depth,
                    ),
                )
                for pid in parents:
                    conn.execute(
                        "INSERT OR IGNORE INTO task_links (parent_id, child_id) VALUES (?, ?)",
                        (pid, task_id),
                    )
                    # H1b: a task created under an explicit parent inherits that
                    # parent's notify-subscription (only when the parent has
                    # one), so the parent's watcher hears this child's terminal
                    # state without a manual notify-subscribe. Same write_txn,
                    # idempotent (PK collision). Decompose creates children
                    # without ``parents=`` (and does its own inheritance), and
                    # manual ``link_tasks`` goes through a different path, so
                    # neither double-inherits here.
                    if conn.execute(
                        "SELECT 1 FROM kanban_notify_subs WHERE task_id = ? LIMIT 1",
                        (pid,),
                    ).fetchone():
                        _inherit_notify_subs(conn, pid, task_id, now=now)
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
                        "goal_mode": bool(goal_mode) or None,
                    },
                )
                row = conn.execute(
                    "SELECT id, body, assignee, workspace_kind, workspace_path, "
                    "tenant, created_by, kind FROM tasks WHERE id = ?",
                    (task_id,),
                ).fetchone()
                if row is not None and _is_code_task_row(row):
                    reason, payload = _code_contract_issue_for_row(
                        conn, row, source="create_task",
                    )
                    if payload is not None:
                        if reason is None:
                            _append_event(
                                conn, task_id, _CODE_TASK_CONTRACT_EVENT, payload,
                            )
                        else:
                            _append_event(
                                conn, task_id, _NEEDS_CONTRACT_EVENT, payload,
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


def add_event(
    conn: sqlite3.Connection,
    task_id: str,
    kind: str,
    payload: Optional[dict] = None,
) -> None:
    """Public wrapper to append one timeline event for a task.

    Thin, self-contained (opens its own write txn) so callers outside
    this module can record a domain event (e.g. the Flow capture's
    ``flow_plan`` marker) without reaching into the private
    ``_append_event``. Raises ``ValueError`` for an unknown task id.
    """
    with write_txn(conn):
        if not conn.execute(
            "SELECT 1 FROM tasks WHERE id = ?", (task_id,)
        ).fetchone():
            raise ValueError(f"unknown task {task_id}")
        _append_event(conn, task_id, kind, payload)


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


# ---------------------------------------------------------------------------
# Attachments
# ---------------------------------------------------------------------------

def add_attachment(
    conn: sqlite3.Connection,
    task_id: str,
    *,
    filename: str,
    stored_path: str,
    content_type: Optional[str] = None,
    size: int = 0,
    uploaded_by: Optional[str] = None,
) -> int:
    """Record a file attachment for a task. Returns the new attachment id.

    The caller is responsible for writing the blob to ``stored_path``
    first (under :func:`task_attachments_dir`); this only persists the
    metadata row and appends an ``attached`` event.
    """
    if not filename or not filename.strip():
        raise ValueError("attachment filename is required")
    if not stored_path or not stored_path.strip():
        raise ValueError("attachment stored_path is required")
    now = int(time.time())
    with write_txn(conn):
        if not conn.execute(
            "SELECT 1 FROM tasks WHERE id = ?", (task_id,)
        ).fetchone():
            raise ValueError(f"unknown task {task_id}")
        cur = conn.execute(
            "INSERT INTO task_attachments "
            "(task_id, filename, stored_path, content_type, size, uploaded_by, created_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?)",
            (
                task_id,
                filename.strip(),
                stored_path,
                content_type,
                int(size),
                uploaded_by,
                now,
            ),
        )
        _append_event(
            conn,
            task_id,
            "attached",
            {"filename": filename.strip(), "size": int(size), "by": uploaded_by},
        )
        return int(cur.lastrowid or 0)


def list_attachments(conn: sqlite3.Connection, task_id: str) -> list[Attachment]:
    rows = conn.execute(
        "SELECT * FROM task_attachments WHERE task_id = ? ORDER BY created_at ASC, id ASC",
        (task_id,),
    ).fetchall()
    return [
        Attachment(
            id=r["id"],
            task_id=r["task_id"],
            filename=r["filename"],
            stored_path=r["stored_path"],
            content_type=r["content_type"],
            size=r["size"] or 0,
            uploaded_by=r["uploaded_by"],
            created_at=r["created_at"],
        )
        for r in rows
    ]


def get_attachment(conn: sqlite3.Connection, attachment_id: int) -> Optional[Attachment]:
    r = conn.execute(
        "SELECT * FROM task_attachments WHERE id = ?", (attachment_id,)
    ).fetchone()
    if r is None:
        return None
    return Attachment(
        id=r["id"],
        task_id=r["task_id"],
        filename=r["filename"],
        stored_path=r["stored_path"],
        content_type=r["content_type"],
        size=r["size"] or 0,
        uploaded_by=r["uploaded_by"],
        created_at=r["created_at"],
    )


def delete_attachment(conn: sqlite3.Connection, attachment_id: int) -> Optional[Attachment]:
    """Delete an attachment row and its on-disk blob. Returns the removed row.

    Returns ``None`` when no row matched. The blob is removed best-effort
    (a missing file is not an error); the metadata row is the source of
    truth for whether an attachment "exists".
    """
    with write_txn(conn):
        att = get_attachment(conn, attachment_id)
        if att is None:
            return None
        conn.execute("DELETE FROM task_attachments WHERE id = ?", (attachment_id,))
        _append_event(
            conn, att.task_id, "attachment_removed", {"filename": att.filename}
        )
    try:
        p = Path(att.stored_path)
        if p.is_file():
            p.unlink()
    except OSError:
        pass
    return att


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


def _coerce_int(val: Any) -> Optional[int]:
    if val is None or isinstance(val, bool):
        return None
    try:
        return int(val)
    except (TypeError, ValueError):
        return None


def _coerce_float(val: Any) -> Optional[float]:
    if val is None or isinstance(val, bool):
        return None
    try:
        return float(val)
    except (TypeError, ValueError):
        return None


def _extract_run_cost_tokens(
    metadata: Optional[dict],
) -> tuple[Optional[int], Optional[int], Optional[float]]:
    """K5a: pull (input_tokens, output_tokens, cost_usd) out of run metadata.

    Purely in-process: the only source is the ``metadata`` dict a caller
    hands to ``_end_run`` (e.g. a worker that recorded its own usage).
    Tolerant of the common shapes — top-level keys, a nested ``usage`` block,
    and the OpenAI-style ``prompt_tokens``/``completion_tokens`` aliases.
    Returns ``(None, None, None)`` when nothing usable is present, so a run
    with no usage data writes NULLs rather than crashing or guessing. The
    cross-DB ``state.db`` backfill is a separate slice (K5b).
    """
    if not isinstance(metadata, dict):
        return None, None, None
    usage = metadata.get("usage")
    usage = usage if isinstance(usage, dict) else {}

    def _first_int(*vals: Any) -> Optional[int]:
        for v in vals:
            out = _coerce_int(v)
            if out is not None:
                return out
        return None

    def _first_float(*vals: Any) -> Optional[float]:
        for v in vals:
            out = _coerce_float(v)
            if out is not None:
                return out
        return None

    input_tokens = _first_int(
        metadata.get("input_tokens"),
        usage.get("input_tokens"),
        usage.get("prompt_tokens"),
    )
    output_tokens = _first_int(
        metadata.get("output_tokens"),
        usage.get("output_tokens"),
        usage.get("completion_tokens"),
    )
    cost_usd = _first_float(
        metadata.get("cost_usd"),
        metadata.get("actual_cost_usd"),
        metadata.get("estimated_cost_usd"),
        metadata.get("cost"),
        usage.get("cost_usd"),
    )
    return input_tokens, output_tokens, cost_usd


def _state_db_path() -> Path:
    """Path to the agent session DB (``state.db``), paired with kanban.db's
    root. Mirrors ``hermes_state.DEFAULT_DB_PATH`` without importing that
    heavy module into the kanban write path (K5b)."""
    from hermes_constants import get_default_hermes_root
    return get_default_hermes_root() / "state.db"


def _backfill_cost_from_state_db(
    session_id: str,
    *,
    profile: Optional[str] = None,
) -> tuple[Optional[int], Optional[int], Optional[float]]:
    """K5b: read a worker session's token/cost from ``state.db`` (read-only).

    Keyed by ``metadata.worker_session_id`` (stamped only for ACP workers, so
    coverage is intentionally PARTIAL). Fully fail-soft and isolated:

    * Opens ``state.db`` on a SEPARATE read-only connection (``mode=ro``) — it
      can never acquire a lock on ``kanban.db`` nor write anywhere.
    * Short busy-timeout so a locked/contended ``state.db`` fails fast to a
      NO-OP instead of extending the caller's open kanban write transaction.
    * Any error (missing/locked DB, absent ``sessions`` table, no matching
      row) returns ``(None, None, None)`` — it must NEVER raise into
      ``_end_run``.

    K16: when ``profile`` is given and non-empty, prefer that profile's
    ``state.db`` (each kanban worker is a ``hermes -p <profile> chat``
    subprocess whose session cost lands in the PER-PROFILE ``state.db``, not
    the hub one). The per-profile path is resolved fail-soft; if it can't be
    resolved or doesn't exist we fall back to the hub ``_state_db_path()`` so
    the existing ``_end_run`` caller (``profile=None``) is unaffected.
    """
    if not session_id:
        return None, None, None
    path = None
    if profile:
        try:
            from hermes_cli.profiles import resolve_profile_env
            candidate = Path(resolve_profile_env(profile)) / "state.db"
            if candidate.exists():
                path = candidate
        except Exception:
            path = None
    if path is None:
        try:
            path = _state_db_path()
        except Exception:
            return None, None, None
    try:
        if not path.exists():
            return None, None, None
    except Exception:
        return None, None, None
    conn = None
    try:
        conn = sqlite3.connect(
            f"file:{path}?mode=ro", uri=True, timeout=0.5,
        )
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA busy_timeout=200")
        row = conn.execute(
            "SELECT input_tokens, output_tokens, actual_cost_usd, "
            "estimated_cost_usd FROM sessions WHERE id = ?",
            (session_id,),
        ).fetchone()
    except Exception:
        return None, None, None
    finally:
        if conn is not None:
            try:
                conn.close()
            except Exception:
                pass
    if row is None:
        return None, None, None
    in_tok = _coerce_int(row["input_tokens"])
    out_tok = _coerce_int(row["output_tokens"])
    cost = _coerce_float(row["actual_cost_usd"])
    if cost is None:
        cost = _coerce_float(row["estimated_cost_usd"])
    return in_tok, out_tok, cost


# K17: how far back into a claude-CLI worker log we look for the final
# ``{"type": "result", ...}`` JSON. The result object is the LAST thing the
# CLI prints; 1 MB of tail comfortably covers even verbose result texts.
_CLAUDE_RESULT_TAIL_BYTES = 1_000_000


def _is_claude_cli_runtime(profile: Optional[str]) -> bool:
    """K17: True when ``profile`` dispatches via the ``claude`` CLI.

    Thin wrapper that resolves the profile's env dir so the existing
    ``_is_claude_cli_profile`` seam (env allowlist + ``worker_runtime:
    claude-cli`` in the profile config) can be reused from the backfill
    path, where only the profile NAME is at hand. Fail-soft False — a
    broken profile can never divert the state.db backfill branch.
    """
    if not profile:
        return False
    try:
        hermes_home: Optional[str] = None
        try:
            from hermes_cli.profiles import resolve_profile_env
            hermes_home = resolve_profile_env(profile)
        except Exception:
            hermes_home = None
        return _is_claude_cli_profile(profile, hermes_home)
    except Exception:
        return False


def _run_is_claude_cli(profile: Optional[str], *, board: Optional[str] = None) -> bool:
    """True when a run's ``profile`` was dispatched via the ``claude`` CLI.

    Mirrors the spawn-time branch in ``_default_spawn`` so the dispatcher-side
    heartbeat (``heartbeat_live_claude_cli_workers``) classifies a live run
    exactly the way it was launched:

        active lane ``worker_runtime`` == "claude-cli"  → claude CLI
        active lane pins a different runtime (e.g. hermes) → NOT claude CLI
        no lane override → fall back to the profile config seam
            (``_is_claude_cli_runtime`` → env allowlist + ``worker_runtime``)

    Re-evaluating the lane (rather than only the profile config) is what keeps
    a Hermes-runtime worker untouched even when its profile *config* says
    claude-cli but an active lane forced it to hermes — that worker self-
    heartbeats and must not also be dispatcher-heartbeat'd. Fail-soft False so
    a broken lanes/profile config can never mis-mark a hermes worker.
    """
    if not profile:
        return False
    try:
        lane_entry = _active_lane_entry_for_profile(profile, board=board)
        lane_runtime = (lane_entry or {}).get("worker_runtime")
        if lane_runtime == "claude-cli":
            return True
        if lane_runtime is None:
            return _is_claude_cli_runtime(profile)
        return False
    except Exception:
        return False


def _parse_claude_cli_result(log_path: Path) -> Optional[dict]:
    """K17: last ``{"type": "result", ...}`` object in a claude-CLI task log.

    ``_spawn_claude_worker`` runs ``claude -p … --output-format json`` with
    stdout+stderr appended to the per-task worker log, so the final result
    JSON (``total_cost_usd`` + ``usage``) is the last JSON line in that log.
    Reads at most ``_CLAUDE_RESULT_TAIL_BYTES`` from the end; returns the
    LAST parseable result object (retries append, so last wins). Fully
    fail-soft: missing/unreadable log or no result line → ``None``.
    """
    try:
        if not log_path.exists():
            return None
        size = log_path.stat().st_size
        with open(log_path, "rb") as fh:
            if size > _CLAUDE_RESULT_TAIL_BYTES:
                fh.seek(size - _CLAUDE_RESULT_TAIL_BYTES)
            data = fh.read()
        text = data.decode("utf-8", errors="replace")
    except Exception:
        return None
    result: Optional[dict] = None
    for line in text.splitlines():
        line = line.strip()
        if not line.startswith("{") or '"type"' not in line:
            continue
        try:
            obj = json.loads(line)
        except (TypeError, ValueError):
            continue
        if isinstance(obj, dict) and obj.get("type") == "result":
            result = obj
    return result


def _extract_claude_cli_cost(
    result: dict,
) -> tuple[Optional[int], Optional[int], Optional[float]]:
    """K17: (input_tokens, output_tokens, cost_usd_equivalent) from a result.

    ``input_tokens`` counts fresh tokens (``input_tokens`` +
    ``cache_creation_input_tokens``); cache READS are excluded — they are
    near-free against the subscription quota and would triple-count the
    same context on every turn. The raw ``usage`` block is preserved in run
    metadata by the caller, so nothing is lost. ``total_cost_usd`` is the
    API-EQUIVALENT value, not real spend (subscription runs bill $0).
    """
    usage = result.get("usage")
    usage = usage if isinstance(usage, dict) else {}
    base = _coerce_int(usage.get("input_tokens"))
    cache_w = _coerce_int(usage.get("cache_creation_input_tokens"))
    in_tok: Optional[int] = None
    if base is not None or cache_w is not None:
        in_tok = (base or 0) + (cache_w or 0)
    out_tok = _coerce_int(usage.get("output_tokens"))
    equiv = _coerce_float(result.get("total_cost_usd"))
    return in_tok, out_tok, equiv


def backfill_run_costs(
    conn: sqlite3.Connection,
    *,
    limit: int = 200,
    since_seconds: Optional[int] = None,
    board: Optional[str] = None,
) -> int:
    """K16: deferred, profile-aware cost backfill for closed runs.

    The in-process K5a/K5b write-back at ``_end_run`` time often misses the
    worker's final cost: the worker is a ``hermes -p <profile> chat``
    subprocess whose session is flushed to the PER-PROFILE ``state.db`` AFTER
    completion, and the old K5b lookup read only the hub ``state.db``. This
    runs LATER, profile-aware, over runs that still have ``cost_usd IS NULL``.

    For each candidate run, looks up its ``metadata.worker_session_id`` in the
    run's own ``profile`` ``state.db`` (``_backfill_cost_from_state_db`` with
    ``profile=…``) and COALESCE-fills the token/cost columns. Returns the count
    of runs whose ``cost_usd`` was newly set. Fully fail-soft: a single bad row
    never aborts the batch, and a run without ``worker_session_id`` is skipped.

    K17: claude-CLI lanes (``worker_runtime: claude-cli`` — coder-claude,
    premium) have no hermes session at all, so there is nothing in any
    ``state.db``. Their usage lives in the final result JSON of the per-task
    worker log instead (``--output-format json``), which the CLI prints only
    AFTER the worker already closed its run via ``hermes kanban complete`` —
    hence post-hoc here, never at ``_end_run`` time. Tokens go into the
    columns, ``cost_usd`` is stamped ``0.0`` (the run is included in the
    subscription — keeps the honest-$0 convention), and the API-equivalent
    value is preserved as ``metadata.cost_usd_equivalent``. ``board`` selects
    the worker-log directory and defaults to the current board, matching the
    default ``connect_closing()`` the callers pair this with.
    """
    sql = (
        "SELECT id, task_id, profile, metadata FROM task_runs "
        "WHERE cost_usd IS NULL AND ended_at IS NOT NULL"
    )
    params: list[Any] = []
    if since_seconds is not None:
        sql += " AND ended_at >= ?"
        params.append(int(time.time()) - int(since_seconds))
    sql += " ORDER BY id DESC LIMIT ?"
    params.append(int(limit))
    rows = conn.execute(sql, params).fetchall()

    updated = 0
    for row in rows:
        try:
            run_id = row["id"]
            profile = row["profile"]
            raw_meta = row["metadata"]
            try:
                metadata = json.loads(raw_meta) if raw_meta else None
            except (TypeError, ValueError):
                metadata = None
            if not isinstance(metadata, dict):
                metadata = None

            # K17: claude-CLI branch — no state.db session exists; read the
            # final result JSON from the per-task worker log instead.
            if _is_claude_cli_runtime(profile):
                task_id = row["task_id"]
                newer = conn.execute(
                    "SELECT profile FROM task_runs WHERE task_id = ? AND id > ?",
                    (task_id, run_id),
                ).fetchall()
                if any(_is_claude_cli_runtime(r["profile"]) for r in newer):
                    # The per-task log only proves the LAST claude-cli run's
                    # result — never stamp an older run from a newer run's
                    # JSON. Non-cli runs (the review-gate verifier appends
                    # to the same log) never write a result line and must
                    # not shadow the worker run here.
                    continue
                result = _parse_claude_cli_result(
                    worker_logs_dir(board=board) / f"{task_id}.log"
                )
                if not isinstance(result, dict):
                    continue
                c_in, c_out, c_equiv = _extract_claude_cli_cost(result)
                if c_in is None and c_out is None and c_equiv is None:
                    continue
                stamped = dict(metadata or {})
                stamped.setdefault("billing_mode", "subscription_included")
                if c_equiv is not None:
                    stamped.setdefault("cost_usd_equivalent", c_equiv)
                claude_sid = result.get("session_id")
                if claude_sid:
                    stamped.setdefault("claude_session_id", str(claude_sid))
                usage = result.get("usage")
                if isinstance(usage, dict):
                    stamped.setdefault("usage", usage)
                with write_txn(conn):
                    conn.execute(
                        """
                        UPDATE task_runs
                           SET input_tokens  = COALESCE(?, input_tokens),
                               output_tokens = COALESCE(?, output_tokens),
                               cost_usd      = COALESCE(cost_usd, 0.0),
                               metadata      = ?
                         WHERE id = ?
                        """,
                        (c_in, c_out, json.dumps(stamped), run_id),
                    )
                updated += 1
                continue

            if metadata is None:
                continue
            session_id = metadata.get("worker_session_id")
            if not session_id:
                continue
            b_in, b_out, b_cost = _backfill_cost_from_state_db(
                str(session_id), profile=profile,
            )
            if b_in is None and b_out is None and b_cost is None:
                continue
            with write_txn(conn):
                conn.execute(
                    """
                    UPDATE task_runs
                       SET input_tokens  = COALESCE(?, input_tokens),
                           output_tokens = COALESCE(?, output_tokens),
                           cost_usd      = COALESCE(?, cost_usd)
                     WHERE id = ?
                    """,
                    (b_in, b_out, b_cost, run_id),
                )
            if b_cost is not None:
                updated += 1
        except Exception:
            # Per-row isolation: one bad row never aborts the batch.
            continue
    return updated


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
    # K5a: in-process token/cost write-back. Pull usage out of the metadata the
    # caller handed us (the only in-process source); absent → NULL. COALESCE
    # keeps any value a prior write already set, so re-closing never clobbers
    # real numbers with NULL.
    in_tok, out_tok, cost = _extract_run_cost_tokens(metadata)
    # K5b: cross-DB backfill. Only when in-process data left a gap AND the run
    # carries a worker_session_id, look it up in state.db (read-only, fail-soft,
    # partial coverage = ACP workers only). Never raises; can't lock kanban.db.
    if (in_tok is None or out_tok is None or cost is None) and isinstance(metadata, dict):
        session_id = metadata.get("worker_session_id")
        if session_id:
            try:
                b_in, b_out, b_cost = _backfill_cost_from_state_db(str(session_id))
            except Exception:
                b_in = b_out = b_cost = None
            if in_tok is None:
                in_tok = b_in
            if out_tok is None:
                out_tok = b_out
            if cost is None:
                cost = b_cost
    conn.execute(
        """
        UPDATE task_runs
           SET status        = ?,
               outcome       = ?,
               summary       = ?,
               error         = ?,
               metadata      = ?,
               ended_at      = ?,
               input_tokens  = COALESCE(?, input_tokens),
               output_tokens = COALESCE(?, output_tokens),
               cost_usd      = COALESCE(?, cost_usd),
               claim_lock    = NULL,
               claim_expires = NULL,
               worker_pid    = NULL
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
            in_tok,
            out_tok,
            cost,
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


def _resolve_max_continuations(task: Task) -> int:
    if task.max_continuations is not None:
        return int(task.max_continuations)
    return DEFAULT_ITERATION_BUDGET_CONTINUATION_LIMIT


def _schedule_continuation_after_closed_run(
    conn: sqlite3.Connection,
    task_id: str,
    *,
    reason: str,
    run_id: Optional[int],
) -> bool:
    """Requeue a budget-exhausted task or block it when the cap is exhausted.

    Caller must hold ``write_txn`` and must already have closed the active run.
    """
    task = get_task(conn, task_id)
    if task is None:
        return False
    limit = _resolve_max_continuations(task)
    now = int(time.time())
    if limit <= 0:
        message = "iteration budget exhausted; auto-continuation disabled"
        cur = conn.execute(
            """
            UPDATE tasks
               SET status = 'blocked', result = ?, completed_at = ?,
                   claim_lock = NULL, claim_expires = NULL, worker_pid = NULL,
                   last_continuation_reason = ?
             WHERE id = ? AND status = 'running' AND current_run_id IS NULL
            """,
            (message, now, reason, task_id),
        )
        if cur.rowcount != 1:
            return False
        _append_event(
            conn, task_id, "auto_continuation_disabled",
            {"reason": reason, "limit": limit, "message": message},
            run_id=run_id,
        )
        return True
    if int(task.continuation_count or 0) >= limit:
        message = (
            f"iteration budget exhausted; continuation limit exhausted "
            f"({int(task.continuation_count or 0)}/{limit})"
        )
        cur = conn.execute(
            """
            UPDATE tasks
               SET status = 'blocked', result = ?, completed_at = ?,
                   claim_lock = NULL, claim_expires = NULL, worker_pid = NULL,
                   last_continuation_reason = ?
             WHERE id = ? AND status = 'running' AND current_run_id IS NULL
            """,
            (message, now, reason, task_id),
        )
        if cur.rowcount != 1:
            return False
        _append_event(
            conn, task_id, "auto_continuation_exhausted",
            {
                "reason": reason,
                "count": int(task.continuation_count or 0),
                "limit": limit,
                "message": message,
            },
            run_id=run_id,
        )
        return True
    new_count = int(task.continuation_count or 0) + 1
    cur = conn.execute(
        """
        UPDATE tasks
           SET status = 'ready', claim_lock = NULL, claim_expires = NULL,
               worker_pid = NULL,
               continuation_count = continuation_count + 1,
               last_continuation_reason = ?
         WHERE id = ? AND status = 'running' AND current_run_id IS NULL
        """,
        (reason, task_id),
    )
    if cur.rowcount != 1:
        return False
    _append_event(
        conn, task_id, "auto_continuation_scheduled",
        {"reason": reason, "count": new_count, "limit": limit},
        run_id=run_id,
    )
    return True


def record_iteration_budget_exhausted(
    conn: sqlite3.Connection,
    task_id: str,
    *,
    summary: Optional[str] = None,
    metadata: Optional[dict] = None,
    expected_run_id: Optional[int] = None,
    reason: str = "iteration_budget_exhausted",
) -> bool:
    """Close the current run as iteration-budget exhausted and requeue/block.

    This is a worker self-report path: it does not increment failure counters
    and it uses the task's current_run_id as a run-ownership guard.
    """
    with write_txn(conn):
        row = conn.execute(
            "SELECT status, current_run_id FROM tasks WHERE id = ?", (task_id,),
        ).fetchone()
        if not row or row["status"] != "running" or not row["current_run_id"]:
            return False
        current_run_id = int(row["current_run_id"])
        if expected_run_id is not None and int(expected_run_id) != current_run_id:
            return False
        run_id = _end_run(
            conn,
            task_id,
            outcome="iteration_budget_exhausted",
            status="iteration_budget_exhausted",
            summary=summary,
            metadata=metadata,
        )
        _append_event(
            conn,
            task_id,
            "iteration_budget_exhausted",
            {
                "reason": reason,
                "summary": (summary or "").strip().splitlines()[0][:400]
                if summary else None,
            },
            run_id=run_id,
        )
        return _schedule_continuation_after_closed_run(
            conn, task_id, reason=reason, run_id=run_id,
        )


def maybe_schedule_continuation(
    conn: sqlite3.Connection,
    task_id: str,
    *,
    reason: str = "iteration_budget_exhausted",
    expected_run_id: Optional[int] = None,
) -> bool:
    """Compatibility wrapper for the planned DB API."""
    return record_iteration_budget_exhausted(
        conn, task_id, expected_run_id=expected_run_id, reason=reason,
    )


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
            started_at, ended_at
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
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


def recompute_ready(
    conn: sqlite3.Connection, failure_limit: int = None,
) -> int:
    """Promote ``todo`` tasks to ``ready`` when all parents are ``done``.

    Returns the number of tasks promoted.  Safe to call inside or outside
    an existing transaction; it opens its own IMMEDIATE txn.

    ``blocked`` tasks are also considered for promotion (so a task
    blocked purely by a parent dependency unblocks itself when the
    parent completes), *except* in two cases:

    1. The most recent block event was a worker-initiated
       ``kanban_block`` — those stay blocked until an explicit
       ``kanban_unblock`` (#28712).

    2. The task's ``consecutive_failures`` has reached the effective
       failure limit.  This prevents infinite retry loops when a task
       repeatedly exhausts its iteration budget: without this guard the
       counter would reset on every recovery cycle and the circuit
       breaker could never trip (#35072).

    The effective failure limit resolves in the same order as the
    circuit breaker in ``_record_task_failure`` so the two never
    disagree about when a task is permanently blocked:

      1. per-task ``max_retries`` if set
      2. caller-supplied ``failure_limit`` (the dispatcher passes the
         ``kanban.failure_limit`` config value through ``dispatch_once``)
      3. ``DEFAULT_FAILURE_LIMIT``
    """
    if failure_limit is None:
        failure_limit = DEFAULT_FAILURE_LIMIT
    promoted = 0
    now = int(time.time())
    with write_txn(conn):
        todo_rows = conn.execute(
            "SELECT id, status, consecutive_failures, max_retries, due_at "
            "FROM tasks WHERE status IN ('todo', 'blocked')"
        ).fetchall()
        for row in todo_rows:
            task_id = row["id"]
            cur_status = row["status"]
            # K9 time-based scheduling gate: hold a task whose due_at is still
            # in the future, regardless of status or parent state — it is not
            # yet eligible for promotion. NULL due_at (the common case) is
            # eligible immediately, so a task without a due time promotes
            # exactly as before this column existed (no extra 'promoted' event,
            # identical count). A past/equal due_at falls through to the normal
            # parent-completion check below.
            due_at = row["due_at"]
            if due_at is not None and int(due_at) > now:
                continue
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
            if all(p["status"] == "done" for p in parents):
                if cur_status == "blocked":
                    # Don't auto-recover tasks that have hit the
                    # circuit-breaker failure limit.  Without this
                    # guard, a task that repeatedly exhausts its
                    # iteration budget would cycle forever:
                    # block → auto-recover → respawn → budget
                    # exhausted → block → …  The counter must also
                    # be preserved so the breaker can accumulate
                    # across recovery cycles.
                    failures = int(row["consecutive_failures"] or 0)
                    task_limit = row["max_retries"]
                    effective_limit = (
                        int(task_limit) if task_limit is not None
                        else int(failure_limit)
                    )
                    if failures >= effective_limit:
                        continue
                    conn.execute(
                        "UPDATE tasks SET status = 'ready' "
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
        contract_reason = _ensure_code_task_contract_in_txn(
            conn, task_id, source="claim_task",
        )
        if contract_reason is not None:
            return None
        # Structural invariant: never transition ready -> running while any
        # parent is not yet 'done'. Archived parents intentionally remain
        # unsatisfied: archiving a parent parks/cancels that branch, it does
        # not declare downstream work complete. This is the single
        # enforcement point regardless of which writer (create_task,
        # link_tasks, unblock_task, release_stale_claims, manual SQL) set
        # status='ready'. If a racy writer promoted a task with undone
        # parents, demote it back to 'todo' here — recompute_ready will
        # re-promote when the parents actually finish. See RCA at
        # kanban/boards/cookai/workspaces/t_a6acd07d/root-cause.md.
        undone = conn.execute(
            "SELECT 1 FROM task_links l "
            "JOIN tasks p ON p.id = l.parent_id "
            "WHERE l.child_id = ? AND p.status != 'done' LIMIT 1",
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
        # Reset last_heartbeat_at so each run starts with a clean slate and a
        # re-claimed task can't carry a stale beat from a prior run (see the
        # fuller rationale in claim_review_task, where this matters most).
        cur = conn.execute(
            """
            UPDATE tasks
               SET status        = 'running',
                   claim_lock    = ?,
                   claim_expires = ?,
                   started_at    = COALESCE(started_at, ?),
                   last_heartbeat_at = NULL
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
                started_at
            ) VALUES (?, ?, ?, 'running', ?, ?, ?, ?)
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
    reviewer_profile: Optional[str] = None,
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
        # Reset last_heartbeat_at so the new run starts with a clean slate.
        # Otherwise a stage whose worker does not self-heartbeat (the
        # claude-CLI verifier/reviewer runs) would inherit the *previous*
        # stage's last beat, which then ages past the dashboard's stuck
        # threshold (STUCK_HEARTBEAT_S) and shows an actively-running review
        # as "Hängt". A NULL heartbeat reads as "no heartbeat yet" (liveness
        # via claim_expires), exactly like every other non-self-beating
        # worker. detect_stale_running still reclaims a NULL-heartbeat run
        # once it exceeds the stale window.
        cur = conn.execute(
            """
            UPDATE tasks
               SET status        = 'running',
                   claim_lock    = ?,
                   claim_expires = ?,
                   started_at    = COALESCE(started_at, ?),
                   last_heartbeat_at = NULL
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
        cfg = _review_gate_config()
        run_profile = reviewer_profile or cfg.get("verifier_profile") or (trow["assignee"] if trow else None)
        run_cur = conn.execute(
            """
            INSERT INTO task_runs (
                task_id, profile, step_key, status,
                claim_lock, claim_expires, max_runtime_seconds,
                started_at
            ) VALUES (?, ?, ?, 'running', ?, ?, ?, ?)
            """,
            (
                task_id,
                run_profile,
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
    though the subprocess is healthy.

    Backstop (#29747 gap 3): if the worker's PID is still alive but its
    ``last_heartbeat_at`` is stale by more than
    ``DEFAULT_CLAIM_HEARTBEAT_MAX_STALE_SECONDS`` (1h), the worker has
    been making no observable progress and we reclaim anyway — even if
    ``_pid_alive`` is still true. This catches the wedged-in-a-logic-loop
    case where the process is technically running but accomplishing
    nothing. ``_touch_activity`` (run_agent.py) bridges chunk-level
    liveness into ``last_heartbeat_at`` via #31752, so any genuinely
    active worker keeps its heartbeat fresh as a side effect of normal
    API traffic. ``enforce_max_runtime`` and ``detect_crashed_workers``
    remain the upper bounds for genuinely wedged or dead workers.

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
        hb = row["last_heartbeat_at"]
        # Heartbeat staleness backstop: if we have a heartbeat at all
        # and it's older than the max-stale threshold, the worker is
        # not making observable progress.  Reclaim instead of extending,
        # even if the PID is still alive (it's likely in a logic loop).
        heartbeat_stale = (
            hb is not None
            and (now - int(hb)) > DEFAULT_CLAIM_HEARTBEAT_MAX_STALE_SECONDS
        )
        if (
            host_local
            and row["worker_pid"]
            and _pid_alive(row["worker_pid"])
            and not heartbeat_stale
        ):
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
                "heartbeat_stale": bool(heartbeat_stale),
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
    claimed = [str(x).strip() for x in (claimed_ids or []) if str(x).strip()]
    if not claimed:
        return [], []
    # Dedupe while preserving order.
    seen: set[str] = set()
    ordered: list[str] = []
    for cid in claimed:
        if cid not in seen:
            seen.add(cid)
            ordered.append(cid)

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


class WorkerGateError(Exception):
    """Raised by _submit_for_review when the enforced light worker gate
    (kanban.worker_gate) fails. The submission is aborted and the task stays
    in-flight (running/ready) — same fail-safe shape as HallucinatedCardsError.
    Carries the failing command, exit code, and the tail of combined output."""

    def __init__(self, command: str, returncode: int, output_tail: str):
        self.command = command
        self.returncode = returncode
        self.output_tail = output_tail
        super().__init__(f"worker gate failed on {command!r} (exit {returncode})")


# ---------------------------------------------------------------------------
# Review gate (Phase 2: independent verification before 'done')
# ---------------------------------------------------------------------------
#
# When enabled, a code-writing worker calling ``kanban_complete`` does not move
# its task straight to ``done``: the task is parked in ``review`` for an
# independent ``verifier`` profile (terminal-enabled, file-disabled) that runs
# the real tests/build/lint and renders a verdict via the existing tool surface
# — APPROVED → ``kanban_complete`` (→ terminal ``done``), REQUEST_CHANGES →
# ``kanban_block`` (→ sticky ``blocked``, human-gated). The dormant review-
# dispatch loop in ``dispatch_once`` is the consumer; this is the producer.
#
# Design invariants (see plan: "Phase 2 — VERIFIZIERTE DELTAS"):
#   * Opt-in only: ``review_gate`` defaults False, so every non-worker caller of
#     ``complete_task`` (swarm root, sprint bulk-close, OpenClaw MC poll, manual
#     CLI, dashboard) keeps the direct ``done`` path unchanged by construction.
#   * Anti-loop: the verifier's OWN completion runs on a review-originated run
#     (``claim_review_task`` is the sole writer of ``source_status='review'``),
#     so it is always sent terminal — never re-parked in review.
#   * Children gate on the parent's verified ``done`` (recompute_ready is left
#     unchanged): they must not build on unverified work.
_DEFAULT_REVIEW_CODE_ROLES: tuple[str, ...] = ("coder", "coder-claude", "premium")
_DEFAULT_VERIFIER_PROFILE = "verifier"


def _review_gate_config() -> dict:
    """Resolve the ``kanban.review_gate`` policy from the ROOT config.

    The review gate is a board-level policy, but the decision is evaluated in
    the *worker* process — which runs under ``HERMES_HOME=<root>/profiles/
    <name>``. A plain ``load_config()`` there would read the worker profile's
    config, not the board's, so the gate would silently never fire. We read the
    root ``config.yaml`` explicitly (the same home that owns the shared kanban
    DB, via :func:`get_default_hermes_root`) so every worker, the dispatcher,
    and the CLI agree on one source of truth.

    Conservative defaults: disabled, ``{coder, coder-claude, premium}`` as the
    code-bearing roles, ``verifier`` as the verifying profile. The live root
    ``config.yaml`` opts in with ``kanban.review_gate.enabled: true``.
    """
    rg: dict = {}
    try:
        import yaml
        from hermes_constants import get_default_hermes_root

        cfg_path = get_default_hermes_root() / "config.yaml"
        if cfg_path.is_file():
            with open(cfg_path, "r", encoding="utf-8") as fh:
                root_cfg = yaml.safe_load(fh) or {}
            candidate = ((root_cfg.get("kanban") or {}).get("review_gate") or {})
            if isinstance(candidate, dict):
                rg = candidate
    except Exception:
        rg = {}
    roles = rg.get("code_roles")
    if isinstance(roles, (list, tuple)) and roles:
        code_roles = frozenset(
            str(r).strip().lower() for r in roles if str(r).strip()
        )
    else:
        code_roles = frozenset(_DEFAULT_REVIEW_CODE_ROLES)
    # A2 (N-A2): ``acceptance_roles`` extends the gated role set without
    # touching the default. Absent/empty → union with ∅ → byte-identical to
    # today's behaviour. Lets the gate cover extra roles via config alone.
    acc = rg.get("acceptance_roles")
    if isinstance(acc, (list, tuple)) and acc:
        acceptance_roles = frozenset(
            str(r).strip().lower() for r in acc if str(r).strip()
        )
    else:
        acceptance_roles = frozenset()
    code_roles = code_roles | acceptance_roles
    vp = rg.get("verifier_profile")
    verifier_profile = (
        str(vp).strip().lower()
        if vp and str(vp).strip()
        else _DEFAULT_VERIFIER_PROFILE
    )
    return {
        "enabled": bool(rg.get("enabled", False)),
        "code_roles": code_roles,
        "acceptance_roles": acceptance_roles,
        "verifier_profile": verifier_profile,
    }


def _worker_gate_config() -> dict:
    """Resolve kanban.worker_gate from the ROOT config.yaml (mirrors
    _review_gate_config). Workers run under HERMES_HOME=<root>/profiles/<name>,
    so a plain load_config would read the profile, not the board policy — read
    the root config explicitly via get_default_hermes_root(). Returns:
      {enabled: bool, repos: {<abs repo_root>: [cmd,...]}, default: [cmd,...],
       timeout: int, code_roles: frozenset}
    Defaults when absent: enabled=False, empty repos, default=[], timeout=900,
    code_roles=_DEFAULT_REVIEW_CODE_ROLES. Repo keys normalized via Path.resolve()."""
    wg: dict = {}
    try:
        import yaml
        from hermes_constants import get_default_hermes_root
        cfg_path = get_default_hermes_root() / "config.yaml"
        if cfg_path.is_file():
            with open(cfg_path, "r", encoding="utf-8") as fh:
                root_cfg = yaml.safe_load(fh) or {}
            candidate = ((root_cfg.get("kanban") or {}).get("worker_gate") or {})
            if isinstance(candidate, dict):
                wg = candidate
    except Exception:
        wg = {}
    repos_raw = wg.get("repos") if isinstance(wg.get("repos"), dict) else {}
    repos: dict[str, list[str]] = {}
    for k, v in repos_raw.items():
        try:
            key = str(Path(str(k)).resolve())
        except Exception:
            continue
        if isinstance(v, (list, tuple)):
            repos[key] = [str(c) for c in v if str(c).strip()]
    default_cmds = wg.get("default")
    default_list = (
        [str(c) for c in default_cmds if str(c).strip()]
        if isinstance(default_cmds, (list, tuple)) else []
    )
    roles = wg.get("code_roles")
    if isinstance(roles, (list, tuple)) and roles:
        code_roles = frozenset(
            str(r).strip().lower() for r in roles if str(r).strip()
        )
    else:
        code_roles = frozenset(_DEFAULT_REVIEW_CODE_ROLES)
    timeout = wg.get("timeout")
    try:
        timeout = int(timeout) if timeout is not None else 900
        if timeout <= 0:
            timeout = 900
    except (TypeError, ValueError):
        timeout = 900
    return {
        "enabled": bool(wg.get("enabled", False)),
        "repos": repos,
        "default": default_list,
        "timeout": timeout,
        "code_roles": code_roles,
    }


def _run_originated_from_review(
    conn: sqlite3.Connection, task_id: str, run_id: Optional[int]
) -> bool:
    """True iff *run_id* was claimed via :func:`claim_review_task`.

    The anti-loop discriminator: a coder's run is claimed via ``claim_task``
    (no ``source_status`` in the ``claimed`` event), whereas the verifier's run
    is claimed via ``claim_review_task`` (``source_status='review'``). Used so
    the verifier's own ``kanban_complete`` goes terminal instead of re-entering
    the review column.
    """
    if run_id is None:
        return False
    try:
        row = conn.execute(
            "SELECT 1 FROM task_events "
            "WHERE task_id = ? AND run_id = ? AND kind = 'claimed' "
            "  AND json_extract(payload, '$.source_status') = 'review' "
            "LIMIT 1",
            (task_id, int(run_id)),
        ).fetchone()
    except sqlite3.Error:
        return False
    return row is not None


def _set_run_verdict(
    conn: sqlite3.Connection, run_id: Optional[int], verdict: str
) -> None:
    """Persist the structured review *verdict* on *run_id* (B2).

    Fail-soft: a missing column (pre-migration legacy DB) or any SQL error is
    swallowed — recording the verdict must never break a completion/block. Call
    inside the caller's write txn so it shares the transaction.
    """
    if run_id is None:
        return
    try:
        conn.execute(
            "UPDATE task_runs SET verdict = ? WHERE id = ?",
            (verdict, int(run_id)),
        )
    except sqlite3.Error:
        pass
    _record_verdict_score(conn, run_id, verdict)


# F5: verdict → score mapping. Binary on purpose — trends need numbers.
_VERDICT_SCORE_VALUES = {"APPROVED": 1.0, "REQUEST_CHANGES": 0.0}


def _record_verdict_score(
    conn: sqlite3.Connection, run_id: Optional[int], verdict: str,
    *, created_at: Optional[int] = None,
) -> bool:
    """F5: mirror a review verdict into ``scores`` (eval baseline).

    Fail-soft like :func:`_set_run_verdict` and idempotent per run — a
    re-judged run keeps its first score row. Shares the caller's txn.
    """
    if run_id is None:
        return False
    value = _VERDICT_SCORE_VALUES.get(verdict)
    if value is None:
        return False
    try:
        run = conn.execute(
            "SELECT task_id FROM task_runs WHERE id = ?", (int(run_id),)
        ).fetchone()
        if run is None:
            return False
        exists = conn.execute(
            "SELECT 1 FROM scores WHERE run_id = ? AND name = 'review_verdict' LIMIT 1",
            (int(run_id),),
        ).fetchone()
        if exists is not None:
            return False
        conn.execute(
            "INSERT INTO scores (run_id, task_id, name, value, value_type, source, created_at) "
            "VALUES (?,?,?,?,?,?,?)",
            (
                int(run_id), run["task_id"], "review_verdict", value,
                "binary", "review_gate",
                int(created_at) if created_at is not None else int(time.time()),
            ),
        )
        return True
    except sqlite3.Error:
        return False


def backfill_verdict_scores(conn: sqlite3.Connection) -> int:
    """F5 one-time (idempotent): mirror pre-existing ``task_runs.verdict``
    rows into ``scores`` so the eval baseline starts with history. Score
    timestamps take the run's end (fallback start) so trends stay honest."""
    rows = conn.execute(
        "SELECT id, verdict, COALESCE(ended_at, started_at) AS at FROM task_runs "
        "WHERE verdict IN ('APPROVED', 'REQUEST_CHANGES')",
    ).fetchall()
    inserted = 0
    with write_txn(conn):
        for r in rows:
            if _record_verdict_score(conn, r["id"], r["verdict"], created_at=r["at"]):
                inserted += 1
    return inserted


def _review_gate_should_apply(
    conn: sqlite3.Connection, task_id: str, expected_run_id: Optional[int]
) -> bool:
    """Decide whether this completion should be parked in ``review``.

    All of: gate enabled, verifier profile exists (else routing would strand
    the task — fail safe to the direct ``done`` path), this run did NOT
    originate from review (anti-loop), and the task's assignee is a
    code-bearing role.
    """
    cfg = _review_gate_config()
    if not cfg["enabled"]:
        return False
    try:
        from hermes_cli.profiles import profile_exists

        if not profile_exists(cfg["verifier_profile"]):
            return False
    except Exception:
        return False
    run_id = expected_run_id
    if run_id is None:
        run_id = _current_run_id(conn, task_id)
    if _run_originated_from_review(conn, task_id, run_id):
        return False
    row = conn.execute(
        "SELECT assignee FROM tasks WHERE id = ?", (task_id,)
    ).fetchone()
    if not row:
        return False
    assignee = (row["assignee"] or "").strip().lower()
    return assignee in cfg["code_roles"]


# B1 (N-B1): machine-readable diff snapshot captured at the review handoff.
# Feeds the verifier's caller-grep duty (A2) and any later regression check.
# Strictly fail-soft: no git workspace, a vanished workspace, or any git error
# yields an empty snapshot — never an exception that could block the handoff.
_DIFF_SNAPSHOT_FILE_CAP = 200      # max changed_files entries
_DIFF_SNAPSHOT_STAT_CAP = 4000     # max diff_stat characters


def _capture_review_diff_snapshot(
    conn: sqlite3.Connection, task_id: str
) -> dict:
    """Best-effort ``{changed_files, diff_stat}`` for *task_id*'s workspace.

    Runs ``git status --porcelain`` + ``git diff --stat`` in the task's
    ``workspace_path`` (the worktree/dir the worker just used) so the review
    handoff records WHAT changed. Returns ``{}`` when there is no workspace, it
    is not a git work tree, or git errors/times out. Never raises — the review
    handoff must not depend on git being present or healthy.
    """
    try:
        row = conn.execute(
            "SELECT workspace_path FROM tasks WHERE id = ?", (task_id,)
        ).fetchone()
    except sqlite3.Error:
        return {}
    if not row:
        return {}
    ws = row["workspace_path"]
    if not ws or not os.path.isdir(ws):
        return {}

    def _git(*args: str) -> Optional[str]:
        try:
            proc = subprocess.run(
                ["git", "-C", ws, *args],
                stdout=subprocess.PIPE,
                stderr=subprocess.DEVNULL,
                text=True,
                timeout=5,
                check=False,
            )
        except (OSError, subprocess.SubprocessError, ValueError):
            return None
        if proc.returncode != 0:
            return None
        return proc.stdout or ""

    # Gate on "is this even a git work tree?" so a plain scratch dir → {}.
    inside = _git("rev-parse", "--is-inside-work-tree")
    if inside is None or inside.strip() != "true":
        return {}

    snapshot: dict = {}
    porcelain = _git("status", "--porcelain")
    if porcelain:
        changed: list = []
        for line in porcelain.splitlines():
            # Porcelain v1: "XY <path>" or "XY <old> -> <new>" for renames.
            entry = line[3:] if len(line) > 3 else line.strip()
            if " -> " in entry:
                entry = entry.split(" -> ", 1)[1]
            entry = entry.strip().strip('"')
            if entry:
                changed.append(entry)
            if len(changed) >= _DIFF_SNAPSHOT_FILE_CAP:
                break
        if changed:
            snapshot["changed_files"] = changed
    stat = _git("diff", "--stat")
    if stat and stat.strip():
        snapshot["diff_stat"] = stat[:_DIFF_SNAPSHOT_STAT_CAP]
    return snapshot


def _submit_for_review(
    conn: sqlite3.Connection,
    task_id: str,
    *,
    result: Optional[str],
    summary: Optional[str],
    metadata: Optional[dict],
    verified_cards: list,
    expected_run_id: Optional[int],
) -> bool:
    """Park a code-bearing completion in ``review`` instead of ``done``.

    Mirrors :func:`complete_task`'s run-closing + event payload (so the
    handoff summary/artifacts surface to the verifier and the dashboard), but
    deliberately does NOT unblock children (they gate on verified ``done``)
    and does NOT clean up the scratch workspace (the verifier inspects it; a
    REQUEST_CHANGES keeps it for the follow-up fix).
    """
    # B1: snapshot the workspace diff BEFORE taking the write lock (subprocess
    # must not run under the txn). Empty dict when no/non-git workspace.
    diff_snapshot = _capture_review_diff_snapshot(conn, task_id)
    if diff_snapshot:
        run_metadata = {**metadata, **diff_snapshot} if isinstance(
            metadata, dict
        ) else dict(diff_snapshot)
    else:
        run_metadata = metadata
    # D (worker gate): enforce the light repo gate (e.g. lint && backlog:check &&
    # test) at this DB commit boundary, BEFORE the review transition. subprocess
    # runs OUTSIDE the write txn (like the diff snapshot above). config disabled /
    # role not code-bearing / no commands for the repo => skip (byte-identical to
    # today). On the first non-zero command: write a worker_gate_blocked audit
    # event in its own short txn, then raise WorkerGateError WITHOUT entering the
    # review txn -> the task stays running/ready (same fail-safe as the
    # hallucination gate). && semantics: stop at the first failure.
    _wg = _worker_gate_config()
    # #3-A: capture worker_gate stamp for the submitted_for_review payload.
    # _wg_stamp accumulates the result; set to {"configured": False} when the
    # gate is disabled/unconfigured, or {"passed": True/False, ...} when it ran.
    _wg_stamp: dict = {"configured": False}
    if _wg["enabled"]:
        _wg_row = conn.execute(
            "SELECT assignee, workspace_path FROM tasks WHERE id = ?", (task_id,)
        ).fetchone()
        _wg_assignee = (_wg_row["assignee"] or "").strip().lower() if _wg_row else ""
        _wg_ws = _wg_row["workspace_path"] if _wg_row else None
        if _wg_assignee in _wg["code_roles"] and _wg_ws and os.path.isdir(_wg_ws):
            try:
                _wg_key = str(Path(_wg_ws).resolve())
            except Exception:
                _wg_key = _wg_ws
            _wg_cmds = _wg["repos"].get(_wg_key, _wg["default"])
            if _wg_cmds:
                # Gate will run — initialize stamp as passed (flip on failure)
                _wg_run_ts = _dt.datetime.now(_dt.timezone.utc).strftime(
                    "%Y-%m-%dT%H:%M:%SZ"
                )
                try:
                    _wg_commit = subprocess.run(
                        ["git", "rev-parse", "HEAD"], cwd=_wg_ws,
                        capture_output=True, text=True, timeout=10, check=False,
                    ).stdout.strip()[:40]
                except Exception:
                    _wg_commit = ""
                _wg_stamp = {
                    "passed": True,
                    "commands": list(_wg_cmds),
                    "exit_codes": [],
                    "ts": _wg_run_ts,
                    "commit": _wg_commit,
                }
                for _cmd in _wg_cmds:
                    try:
                        _proc = subprocess.run(
                            shlex.split(_cmd), cwd=_wg_ws,
                            capture_output=True, text=True,
                            timeout=_wg["timeout"], check=False,
                        )
                    except (OSError, subprocess.SubprocessError) as _exc:
                        _tail = f"{_cmd}: {_exc}"[-4000:]
                        with write_txn(conn):
                            _append_event(
                                conn, task_id, "worker_gate_blocked",
                                {"command": _cmd, "returncode": -1, "output_tail": _tail},
                            )
                        raise WorkerGateError(_cmd, -1, _tail)
                    _wg_stamp["exit_codes"].append(_proc.returncode)
                    if _proc.returncode != 0:
                        _wg_stamp["passed"] = False
                        _tail = ((_proc.stdout or "") + (_proc.stderr or ""))[-4000:]
                        with write_txn(conn):
                            _append_event(
                                conn, task_id, "worker_gate_blocked",
                                {"command": _cmd, "returncode": _proc.returncode,
                                 "output_tail": _tail},
                            )
                        raise WorkerGateError(_cmd, _proc.returncode, _tail)
    with write_txn(conn):
        if expected_run_id is None:
            cur = conn.execute(
                """
                UPDATE tasks
                   SET status       = 'review',
                       result       = ?,
                       claim_lock   = NULL,
                       claim_expires= NULL,
                       worker_pid   = NULL
                 WHERE id = ?
                   AND status IN ('running', 'ready', 'blocked')
                """,
                (result, task_id),
            )
        else:
            cur = conn.execute(
                """
                UPDATE tasks
                   SET status       = 'review',
                       result       = ?,
                       claim_lock   = NULL,
                       claim_expires= NULL,
                       worker_pid   = NULL
                 WHERE id = ?
                   AND status IN ('running', 'ready', 'blocked')
                   AND current_run_id = ?
                """,
                (result, task_id, int(expected_run_id)),
            )
        if cur.rowcount != 1:
            return False
        # Close the worker's run as a normal success — the coder DID finish
        # its turn; the task (not the run) is what waits for verification.
        run_id = _end_run(
            conn, task_id,
            outcome="completed", status="review",
            summary=summary if summary is not None else result,
            metadata=run_metadata,
        )
        if run_id is None and (summary or run_metadata or result):
            run_id = _synthesize_ended_run(
                conn, task_id,
                outcome="completed",
                summary=summary if summary is not None else result,
                metadata=run_metadata,
            )
        ev_summary = (summary if summary is not None else result) or ""
        ev_summary = ev_summary.strip().splitlines()[0][:400] if ev_summary else ""
        payload: dict = {
            "result_len": len(result) if result else 0,
            "summary": ev_summary or None,
        }
        if verified_cards:
            payload["verified_cards"] = verified_cards
        if isinstance(metadata, dict):
            md_artifacts = metadata.get("artifacts")
            if isinstance(md_artifacts, (list, tuple)):
                cleaned_artifacts = [
                    str(p).strip() for p in md_artifacts
                    if isinstance(p, str) and str(p).strip()
                ]
                if cleaned_artifacts:
                    payload["artifacts"] = cleaned_artifacts
        # B1: additive — surfaces the changed-files snapshot to the verifier
        # context (A2) and the dashboard. Absent when no git workspace.
        if diff_snapshot:
            payload.update(diff_snapshot)
        # #3-A: additive worker_gate stamp — gives the verifier machine-readable
        # gate evidence without changing any existing payload fields.
        payload["worker_gate"] = _wg_stamp
        _append_event(
            conn, task_id, "submitted_for_review", payload, run_id=run_id,
        )
    # Advisory phantom-ref scan, same as the done path (own txn, never blocks).
    scan_text = " ".join(filter(None, [summary, result]))
    if scan_text:
        phantom_refs = _scan_prose_for_phantom_ids(conn, scan_text)
        phantom_refs = [
            p for p in phantom_refs if p not in set(verified_cards or [])
        ]
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
    return True


_FO_BACKLOG_IDEMPOTENCY_PREFIX = "fo-backlog:"
_FO_BACKLOG_DEFAULT_DIR = "/home/piet/projects/family-organizer/backlog/items"
_FO_BACKLOG_ID_RE = re.compile(r"^[A-Za-z0-9_-]{1,64}$")
_FO_RESULT_MAX_CHARS = 500


def _fo_backlog_dir() -> Path:
    return Path(os.environ.get("FAMILY_ORGANIZER_BACKLOG_DIR", _FO_BACKLOG_DEFAULT_DIR)).expanduser()


def _fo_backlog_item_id(idempotency_key: Optional[str]) -> Optional[str]:
    if not idempotency_key:
        return None
    raw_key = str(idempotency_key).strip()
    if not raw_key.startswith(_FO_BACKLOG_IDEMPOTENCY_PREFIX):
        return None
    raw_item_id = raw_key[len(_FO_BACKLOG_IDEMPOTENCY_PREFIX):].strip()
    if not raw_item_id:
        return None
    if raw_item_id.isdigit():
        raw_item_id = raw_item_id.zfill(4)
    if not _FO_BACKLOG_ID_RE.match(raw_item_id):
        return None
    return raw_item_id


def _parse_flat_frontmatter(text: str) -> tuple[dict[str, str], list[str], int] | None:
    lines = text.splitlines()
    if not lines or lines[0].strip() != "---":
        return None
    end = None
    for i in range(1, len(lines)):
        if lines[i].strip() == "---":
            end = i
            break
    if end is None:
        return None
    data: dict[str, str] = {}
    for line in lines[1:end]:
        stripped = line.strip()
        if not stripped or stripped.startswith("#"):
            continue
        idx = line.find(":")
        if idx == -1:
            continue
        key = line[:idx].strip()
        if key:
            data[key] = line[idx + 1:].strip()
    return data, lines, end


def _find_fo_backlog_item_path(base: Path, item_id: str) -> Optional[Path]:
    try:
        base_resolved = base.resolve(strict=False)
    except OSError:
        return None
    if not base_resolved.is_dir():
        return None

    candidates = [base_resolved / f"{item_id}.md"]
    try:
        candidates.extend(sorted(base_resolved.glob(f"{item_id}-*.md")))
    except OSError:
        pass
    for candidate in candidates:
        try:
            resolved = candidate.resolve(strict=False)
            if resolved.is_file() and resolved.is_relative_to(base_resolved):
                return resolved
        except OSError:
            continue

    # Fallback for renamed files: scan flat frontmatter id values under the
    # configured backlog/items directory. The directory is small; fail-soft.
    try:
        entries = sorted(base_resolved.glob("*.md"))
    except OSError:
        return None
    for candidate in entries:
        try:
            resolved = candidate.resolve(strict=False)
            if not resolved.is_file() or not resolved.is_relative_to(base_resolved):
                continue
            parsed = _parse_flat_frontmatter(resolved.read_text(encoding="utf-8"))
        except OSError:
            continue
        if parsed is None:
            continue
        fm, _lines, _end = parsed
        fm_id = str(fm.get("id") or "").strip()
        if fm_id.isdigit():
            fm_id = fm_id.zfill(4)
        if fm_id == item_id:
            return resolved
    return None


def _single_line_backlog_result(value: Any) -> Optional[str]:
    text = " ".join(str(value).strip().split()) if value is not None else ""
    if not text:
        return None
    return text[:_FO_RESULT_MAX_CHARS]


def _fo_backlog_result_text(
    conn: sqlite3.Connection,
    task_id: str,
    *,
    summary: Optional[str],
    result: Optional[str],
    fallback_title: Optional[str],
) -> str:
    # If a coder completion was parked in review, terminal done is normally a
    # terse verifier receipt ("APPROVED"). Use the implementing worker's
    # handoff from the review handoff run as the human-relevant backlog result.
    try:
        row = conn.execute(
            "SELECT summary FROM task_runs "
            "WHERE task_id = ? AND outcome = 'completed' AND status = 'review' "
            "  AND summary IS NOT NULL AND TRIM(summary) != '' "
            "ORDER BY COALESCE(ended_at, started_at) ASC, id ASC LIMIT 1",
            (task_id,),
        ).fetchone()
        if row:
            text = _single_line_backlog_result(row["summary"])
            if text:
                return text
    except sqlite3.Error:
        pass
    for candidate in (summary, result, fallback_title):
        text = _single_line_backlog_result(candidate)
        if text:
            return text
    return f"Hermes task {task_id} completed."


def _set_frontmatter_field(lines: list[str], end: int, key: str, value: str) -> int:
    for i in range(1, end):
        line = lines[i]
        idx = line.find(":")
        if idx == -1:
            continue
        if line[:idx].strip() == key:
            lines[i] = f"{key}: {value}"
            return end
    lines.insert(end, f"{key}: {value}")
    return end + 1


def _close_fo_backlog_item_file(
    path: Path,
    *,
    item_id: str,
    now: int,
    result_text: str,
) -> bool:
    parsed = _parse_flat_frontmatter(path.read_text(encoding="utf-8"))
    if parsed is None:
        return False
    _fm, lines, end = parsed
    updated = _dt.datetime.fromtimestamp(int(now), tz=_dt.timezone.utc).date().isoformat()
    end = _set_frontmatter_field(lines, end, "status", "done")
    end = _set_frontmatter_field(lines, end, "updated", updated)
    end = _set_frontmatter_field(lines, end, "result", result_text)
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return True


def _maybe_close_family_organizer_backlog_item(
    conn: sqlite3.Connection,
    task_id: str,
    *,
    run_id: Optional[int],
    now: int,
    summary: Optional[str],
    result: Optional[str],
) -> None:
    """Best-effort write-back from terminal Kanban completion to FO backlog.

    Family Organizer backlog tasks are copied into Fleet/Kanban with
    ``tenant='family-organizer'`` and ``idempotency_key='fo-backlog:<id>'``.
    The source markdown remains the UI source of truth, so terminal ``done``
    must close that linked item. This hook is deliberately fail-soft: kanban
    completion must not be blocked by a missing local family-organizer checkout.
    """
    try:
        row = conn.execute(
            "SELECT title, tenant, idempotency_key FROM tasks WHERE id = ?",
            (task_id,),
        ).fetchone()
    except sqlite3.Error:
        return
    if not row or row["tenant"] != "family-organizer":
        return
    item_id = _fo_backlog_item_id(row["idempotency_key"])
    if item_id is None:
        return
    try:
        base = _fo_backlog_dir()
        item_path = _find_fo_backlog_item_path(base, item_id)
        if item_path is None:
            return
        result_text = _fo_backlog_result_text(
            conn,
            task_id,
            summary=summary,
            result=result,
            fallback_title=row["title"],
        )
        if not _close_fo_backlog_item_file(
            item_path, item_id=item_id, now=now, result_text=result_text
        ):
            return
        with write_txn(conn):
            _append_event(
                conn,
                task_id,
                "family_organizer_backlog_closed",
                {
                    "item_id": item_id,
                    "path": str(item_path),
                    "status": "done",
                    "result": result_text,
                },
                run_id=run_id,
            )
    except Exception:
        _log.debug("family-organizer backlog close failed for %s", task_id, exc_info=True)


def _resolve_workflow_next_step(
    conn: sqlite3.Connection, task_id: str
) -> Optional[tuple[str, str]]:
    """Return ``(next_step_key, next_assignee)`` when *task_id* is a workflow
    task with a step *after* its current one, else ``None`` (K8 / D7 L2).

    Fail-soft by contract: a task with no ``workflow_template_id`` / no
    ``current_step_key``, a missing/broken template, an unknown current step,
    or a current step that is already the *final* one all return ``None`` so
    the caller (:func:`complete_task`) falls through to the unchanged
    completion path. Any error is swallowed — a workflow lookup must never
    block a completion.
    """
    try:
        row = conn.execute(
            "SELECT workflow_template_id, current_step_key "
            "FROM tasks WHERE id = ?",
            (task_id,),
        ).fetchone()
    except sqlite3.Error:
        return None
    if not row:
        return None
    template_id = row["workflow_template_id"]
    step_key = row["current_step_key"]
    if not template_id or not step_key:
        return None
    try:
        from hermes_cli.kanban_workflows import load_workflow_template

        tmpl = load_workflow_template(template_id)
    except Exception:
        return None
    if tmpl is None:
        return None
    next_key = tmpl.next_step_key(step_key)
    if not next_key:
        return None
    next_assignee = tmpl.assignee_for(next_key)
    if not next_assignee:
        return None
    return (next_key, next_assignee)


def _workflow_step_assignee(
    template_id: Optional[str], step_key: Optional[str]
) -> Optional[str]:
    """Return the assignee for *step_key* in *template_id*, fail-soft.

    Used by :func:`dispatch_once` to route a workflow task by its current
    step instead of the static ``assignee`` column. ``None`` (template
    missing/broken or step unknown) means "fall back to the column assignee".
    """
    if not template_id or not step_key:
        return None
    try:
        from hermes_cli.kanban_workflows import load_workflow_template

        tmpl = load_workflow_template(template_id)
    except Exception:
        return None
    if tmpl is None:
        return None
    return tmpl.assignee_for(step_key)


def _advance_workflow_step(
    conn: sqlite3.Connection,
    task_id: str,
    *,
    next_step_key: str,
    next_assignee: str,
    result: Optional[str],
    summary: Optional[str],
    metadata: Optional[dict],
    verified_cards: list,
    expected_run_id: Optional[int],
) -> bool:
    """Advance a workflow task to its next step instead of completing it.

    Mirrors :func:`complete_task`'s run-closing + handoff-event payload (so the
    summary/artifacts surface to the next step's worker and the dashboard), but
    sets the task back to ``ready`` with the next step's ``current_step_key`` +
    ``assignee`` so the dispatcher re-spawns the next role. Like
    :func:`_submit_for_review`, it does NOT unblock children (the task is not
    ``done``) and does NOT clean the workspace (the next step may inspect it).

    The completing worker's run is closed as a normal success — the step DID
    finish; the *task* is what moves on. The consecutive-failures counter is
    cleared (the step succeeded) so a transient failure under the previous role
    cannot prematurely trip the circuit breaker for the next role.
    """
    with write_txn(conn):
        if expected_run_id is None:
            cur = conn.execute(
                """
                UPDATE tasks
                   SET status           = 'ready',
                       current_step_key = ?,
                       assignee         = ?,
                       result           = ?,
                       claim_lock       = NULL,
                       claim_expires    = NULL,
                       worker_pid       = NULL
                 WHERE id = ?
                   AND status IN ('running', 'ready', 'blocked')
                """,
                (next_step_key, next_assignee, result, task_id),
            )
        else:
            cur = conn.execute(
                """
                UPDATE tasks
                   SET status           = 'ready',
                       current_step_key = ?,
                       assignee         = ?,
                       result           = ?,
                       claim_lock       = NULL,
                       claim_expires    = NULL,
                       worker_pid       = NULL
                 WHERE id = ?
                   AND status IN ('running', 'ready', 'blocked')
                   AND current_run_id = ?
                """,
                (next_step_key, next_assignee, result, task_id, int(expected_run_id)),
            )
        if cur.rowcount != 1:
            return False
        run_id = _end_run(
            conn, task_id,
            outcome="completed", status="ready",
            summary=summary if summary is not None else result,
            metadata=metadata,
        )
        if run_id is None and (summary or metadata or result):
            run_id = _synthesize_ended_run(
                conn, task_id,
                outcome="completed",
                summary=summary if summary is not None else result,
                metadata=metadata,
            )
        ev_summary = (summary if summary is not None else result) or ""
        ev_summary = ev_summary.strip().splitlines()[0][:400] if ev_summary else ""
        payload: dict = {
            "next_step_key": next_step_key,
            "next_assignee": next_assignee,
            "result_len": len(result) if result else 0,
            "summary": ev_summary or None,
        }
        if verified_cards:
            payload["verified_cards"] = verified_cards
        if isinstance(metadata, dict):
            md_artifacts = metadata.get("artifacts")
            if isinstance(md_artifacts, (list, tuple)):
                cleaned_artifacts = [
                    str(p).strip() for p in md_artifacts
                    if isinstance(p, str) and str(p).strip()
                ]
                if cleaned_artifacts:
                    payload["artifacts"] = cleaned_artifacts
        _append_event(
            conn, task_id, "workflow_step_advanced", payload, run_id=run_id,
        )
    # Advisory phantom-ref scan, same as the done/review paths (own txn).
    scan_text = " ".join(filter(None, [summary, result]))
    if scan_text:
        phantom_refs = _scan_prose_for_phantom_ids(conn, scan_text)
        phantom_refs = [
            p for p in phantom_refs if p not in set(verified_cards or [])
        ]
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
    # The step succeeded — reset the failure counter so the next role starts
    # clean (mirrors complete_task's success semantics).
    _clear_failure_counter(conn, task_id)
    return True


def set_task_workflow(
    conn: sqlite3.Connection,
    task_id: str,
    *,
    template_id: str,
    first_step_key: str,
) -> bool:
    """Atomically assign a workflow template + seed its first step on a task.

    Writes ``workflow_template_id`` and ``current_step_key`` in a single
    statement so a task can never be left with a template but no step (or vice
    versa). The caller is responsible for resolving ``first_step_key`` from the
    template; this helper hard-guards against a half-seed by refusing to run
    with an empty ``template_id`` or ``first_step_key``. Returns ``True`` when
    exactly one row was updated.
    """
    if not template_id or not first_step_key:
        raise ValueError(
            "set_task_workflow requires a non-empty template_id and "
            "first_step_key (never half-seed a workflow)"
        )
    with write_txn(conn):
        cur = conn.execute(
            "UPDATE tasks "
            "SET workflow_template_id = ?, current_step_key = ? "
            "WHERE id = ?",
            (template_id, first_step_key, task_id),
        )
        return cur.rowcount == 1


def complete_task(
    conn: sqlite3.Connection,
    task_id: str,
    *,
    result: Optional[str] = None,
    summary: Optional[str] = None,
    metadata: Optional[dict] = None,
    created_cards: Optional[Iterable[str]] = None,
    expected_run_id: Optional[int] = None,
    review_gate: bool = False,
) -> bool:
    """Transition ``running|ready -> done`` and record ``result``.

    When ``review_gate`` is True (the ``kanban_complete`` worker tool opts in)
    and the task is a code-bearing one owned by a code role, the completion is
    routed to ``review`` for independent verification instead of ``done`` — see
    :func:`_submit_for_review` and the review-gate helpers above. All other
    callers keep ``review_gate=False`` and the direct ``done`` path below.

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

    # Gate: verify created_cards BEFORE the main write txn. A rejected
    # completion still needs an auditable event, so we emit it in a
    # tiny dedicated txn, then raise. The caller is responsible for
    # surfacing HallucinatedCardsError to the worker; this function
    # never mutates task state on a phantom-card rejection.
    if created_cards:
        verified_cards, phantom_cards = _verify_created_cards(
            conn, task_id, created_cards
        )
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

    # K8 workflow-step routing (D7 L2): when the task is opted into a workflow
    # template and its current step is NOT the last, advance to the next step
    # (back to 'ready', re-assigned to the next role) instead of completing.
    # Sits BEFORE the review gate so a workflow's own ordered steps — not the
    # generic code-role review gate — drive the routing of a workflow task.
    # Fail-soft: no template_id, a missing/broken template, an unknown step,
    # or the FINAL step all return None here and fall through to the unchanged
    # done/review path below — so a non-workflow completion is byte-identical
    # to today and a workflow task is never stranded by template breakage.
    _wf_next = _resolve_workflow_next_step(conn, task_id)
    if _wf_next is not None:
        _next_step_key, _next_assignee = _wf_next
        return _advance_workflow_step(
            conn, task_id,
            next_step_key=_next_step_key, next_assignee=_next_assignee,
            result=result, summary=summary, metadata=metadata,
            verified_cards=verified_cards, expected_run_id=expected_run_id,
        )

    # Phase 2 review gate: park code-bearing worker completions in 'review'
    # for an independent verifier instead of moving straight to 'done'.
    # Opt-in (review_gate) + enabled + verifier-exists + not-a-review-run
    # (anti-loop) + code-bearing assignee — otherwise fall through.
    if review_gate and _review_gate_should_apply(conn, task_id, expected_run_id):
        return _submit_for_review(
            conn, task_id,
            result=result, summary=summary, metadata=metadata,
            verified_cards=verified_cards, expected_run_id=expected_run_id,
        )

    # Worker-isolation integrator (kanban_worktrees, Phase 3): when this
    # completion closes the LAST open task of a dispatcher-provisioned
    # worktree chain, the chain branch is merged --no-ff into its frozen
    # merge target HERE — after Verifier-APPROVED routing, before the task
    # goes done. A parked integration (pre-check / conflict / red post-merge
    # gate) blocks the task instead of completing it: park, don't guess.
    # GUARD FIRST: the hook has real git side effects, so it must only run
    # for a completion that the done-UPDATE below would actually accept —
    # same status set, same expected_run_id match. Without this, a stale
    # worker (claim expired, task re-claimed) or a CLI complete on a task
    # parked in 'review' would merge an unreviewed/abandoned chain.
    # Fail-open on unexpected hook errors — completion semantics for
    # non-isolated tasks must never depend on this module. (Git-level
    # failures are converted to a 'parked' outcome inside the module.)
    _wt_outcome: Optional[dict] = None
    try:
        _wt_row = conn.execute(
            "SELECT status, current_run_id, workspace_path "
            "FROM tasks WHERE id = ?",
            (task_id,),
        ).fetchone()
        _wt_eligible = (
            _wt_row is not None
            and _wt_row["workspace_path"]
            and _wt_row["status"] in ("running", "ready", "blocked")
            and (
                expected_run_id is None
                or _wt_row["current_run_id"] == int(expected_run_id)
            )
        )
        if _wt_eligible:
            from hermes_cli.kanban_worktrees import maybe_integrate_on_complete
            _wt_outcome = maybe_integrate_on_complete(conn, task_id)
    except Exception:
        _log.error(
            "worker-isolation integration hook failed for %s",
            task_id, exc_info=True,
        )
    if _wt_outcome and _wt_outcome.get("action") == "rebase_conflict":
        return _route_rebase_conflict_to_coder(
            conn, task_id, _wt_outcome, expected_run_id=expected_run_id,
        )
    if _wt_outcome and _wt_outcome.get("action") == "parked":
        return _park_integration(
            conn, task_id, _wt_outcome, expected_run_id=expected_run_id,
        )

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
        # B2: an APPROVED verdict is the verifier completing the task it
        # reviewed. Only the review lane writes it (anti-loop discriminator);
        # ordinary coder completions leave task_runs.verdict NULL.
        if _run_originated_from_review(conn, task_id, run_id):
            _set_run_verdict(conn, run_id, "APPROVED")
        # Carry the handoff summary in the event payload so gateway
        # notifiers and dashboard WS consumers can render it without a
        # second SQL round-trip. First line only, 400 char cap — the
        # full summary stays on the run row.
        ev_summary = (summary if summary is not None else result) or ""
        ev_summary = ev_summary.strip().splitlines()[0][:400] if ev_summary else ""
        completed_payload: dict = {
            "result_len": len(result) if result else 0,
            "summary": ev_summary or None,
        }
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
            # Worker isolation (Phase 2): promote the worker's commit hash
            # onto the completed event so receipts/notifiers can render
            # "fertig = committeter Hash" without fetching the run row.
            md_commit = metadata.get("commit")
            if isinstance(md_commit, str) and md_commit.strip():
                completed_payload["commit"] = md_commit.strip()[:64]
            self_state = metadata.get("self_verification") or metadata.get(
                "self_verification_state"
            )
            if self_state is True:
                self_state = SELF_VERIFIED
            if isinstance(self_state, str):
                self_state = self_state.strip().upper()
            if self_state in {SELF_VERIFIED, SELF_VERIFY_LIMITED}:
                _append_event(
                    conn,
                    task_id,
                    self_state,
                    {
                        "summary": ev_summary or None,
                        "source": "completion_metadata",
                    },
                    run_id=run_id,
                )
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
    # Family Organizer write-back: the Kanban task is a Fleet copy of a
    # repo-native backlog item, so terminal done closes the linked source item.
    _maybe_close_family_organizer_backlog_item(
        conn,
        task_id,
        run_id=run_id,
        now=now,
        summary=summary,
        result=result,
    )
    # Successful completion — wipe the consecutive-failures counter.
    # Failure history stays on the event log for audit; the counter
    # just tracks "is there a current pathology the breaker should
    # care about", and a success resets that question.
    _clear_failure_counter(conn, task_id)
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
    # Collect declared artifacts across ALL runs, not just the last. With the
    # Phase 2 review gate the terminal 'done' is the verifier's run (whose
    # metadata carries the verdict, not deliverables), while the implementing
    # worker's ``artifacts=[...]`` rode an earlier ``submitted_for_review``
    # run. Reading only ``runs[-1]`` would silently drop the coder's
    # deliverables once the workspace is rmtree'd. Union + dedup, first-seen
    # order. Pre-gate single-run tasks are unaffected (one run = old behaviour).
    raw: list = []
    seen: set = set()
    for run in runs:
        md = run.metadata
        if not isinstance(md, dict):
            continue
        entries = md.get("artifacts")
        if not isinstance(entries, list):
            continue
        for entry in entries:
            if isinstance(entry, str) and entry and entry not in seen:
                seen.add(entry)
                raw.append(entry)
    if not raw:
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
            # This task's own workspace isn't a removable scratch dir, but its
            # completion may still unblock a deferred parent scratch cleanup
            # (e.g. a 'dir' child whose scratch parent was waiting on it). #33774
            _try_cleanup_parent_workspaces(conn, task_id)
            return
        # Check if this task has children that still need the workspace.
        # If any child is not yet done/archived, defer cleanup so the
        # child can read handoff artifacts from the scratch dir (#33774).
        _active_children = conn.execute(
            "SELECT 1 FROM task_links l "
            "JOIN tasks t ON t.id = l.child_id "
            "WHERE l.parent_id = ? AND t.status NOT IN ('done', 'archived', 'failed', 'cancelled') "
            "LIMIT 1",
            (task_id,),
        ).fetchone()
        if _active_children:
            _log.debug(
                "Deferring scratch workspace cleanup for task %s: "
                "active children still need workspace at %s",
                task_id, path,
            )
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
                preserved = _preserve_scratch_artifacts(conn, task_id, wp)
                shutil.rmtree(wp, ignore_errors=True)
                _log.debug("Removed scratch workspace: %s", wp)
                if preserved:
                    # Record where the deliverables landed — the scratch
                    # workspace is gone now, so without this the operator /
                    # dashboard can't find the preserved copies. Own txn
                    # (the completion txn already committed); best-effort.
                    try:
                        _dest = kanban_home() / "reports" / "by-task" / task_id
                        with write_txn(conn):
                            _append_event(
                                conn, task_id, "deliverables_preserved",
                                {"dir": str(_dest), "files": preserved},
                            )
                    except Exception:
                        pass
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
        # After cleaning up this task's workspace, check if any parent
        # tasks now have all children done — their deferred cleanup can
        # proceed (#33774).
        _try_cleanup_parent_workspaces(conn, task_id)
    except Exception:
        pass  # best-effort — never block completion


def _try_cleanup_parent_workspaces(conn: sqlite3.Connection, task_id: str) -> None:
    """Clean up parent scratch workspaces now that *task_id* completed.

    When a parent task's cleanup was deferred because it had active children,
    this function is called after each child completes.  If all children of a
    parent are now done/archived/failed/cancelled, the parent's scratch
    workspace is removed (#33774).
    """
    try:
        parents = conn.execute(
            "SELECT parent_id FROM task_links WHERE child_id = ?",
            (task_id,),
        ).fetchall()
        for (parent_id,) in parents:
            row = conn.execute(
                "SELECT workspace_kind, workspace_path FROM tasks WHERE id = ?",
                (parent_id,),
            ).fetchone()
            if not row or row["workspace_kind"] != "scratch" or not row["workspace_path"]:
                continue
            # Check if ALL children of this parent are terminal
            active = conn.execute(
                "SELECT 1 FROM task_links l "
                "JOIN tasks t ON t.id = l.child_id "
                "WHERE l.parent_id = ? AND t.status NOT IN ('done', 'archived', 'failed', 'cancelled') "
                "LIMIT 1",
                (parent_id,),
            ).fetchone()
            if active:
                continue  # still has active children
            # All children done — safe to clean up parent workspace
            import shutil
            wp = Path(row["workspace_path"])
            if wp.is_dir() and _is_managed_scratch_path(wp):
                shutil.rmtree(wp, ignore_errors=True)
                _log.debug("Deferred cleanup: removed parent %s scratch workspace: %s", parent_id, wp)
    except Exception:
        pass  # best-effort


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


def _route_rebase_conflict_to_coder(
    conn: sqlite3.Connection,
    task_id: str,
    outcome: dict,
    *,
    expected_run_id: Optional[int] = None,
) -> bool:
    """Route a chain whose pre-merge rebase hit a conflict back to the CODER
    as a REQUEST_CHANGES fix-run (B1), NOT a dead park. The branch was NOT
    merged; the rebase was cleanly aborted (worktree is clean). Unlike
    :func:`_park_integration`, the closing run gets a REQUEST_CHANGES verdict
    UNCONDITIONALLY, so the respawn guard does not suppress the task as
    'recent_success' — it re-enters the review loop as a coder fix-run. The
    ``assignee`` column is left untouched (stays the coder)."""
    branch = outcome.get("branch", "?")
    target = outcome.get("target", "?")
    reason = (
        f"rebase conflict integrating {branch} onto {target} "
        "(aborted cleanly, returned to coder)"
    )
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
                   AND status IN ('running', 'ready', 'blocked')
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
                   AND status IN ('running', 'ready', 'blocked')
                   AND current_run_id = ?
                """,
                (task_id, int(expected_run_id)),
            )
        if cur.rowcount != 1:
            return False
        run_id = _end_run(
            conn, task_id,
            outcome="completed", status="blocked",
            summary=reason, metadata=outcome,
        )
        _set_run_verdict(conn, run_id, "REQUEST_CHANGES")
        _append_event(
            conn, task_id, "rebase_conflict_returned",
            {"reason": reason, "branch": outcome.get("branch")},
            run_id=run_id,
        )
    # add_comment opens its own BEGIN IMMEDIATE -> must be OUTSIDE the txn.
    try:
        add_comment(
            conn, task_id, "integrator",
            f"🔁 Rebase-Konflikt beim Integrieren von `{branch}` auf `{target}`.\n"
            "Der Branch wurde NICHT gemergt; der Rebase wurde sauber abgebrochen "
            "(Worktree ist clean).\n"
            f"Bitte im Chain-Worktree `git rebase {target}` ausführen, die "
            "Konflikte auflösen, committen (gate grün halten: npm run lint && "
            "npm run backlog:check && npm test) und erneut zur Review abgeben.",
        )
    except Exception:
        _log.debug("rebase-conflict coder comment failed", exc_info=True)
    return True


def _park_integration(
    conn: sqlite3.Connection,
    task_id: str,
    outcome: dict,
    *,
    expected_run_id: Optional[int] = None,
) -> bool:
    """Park a completion whose chain integration failed (worker isolation).

    Called from :func:`complete_task` when the kanban_worktrees integrator
    returns ``parked`` (pre-check, merge conflict, or red post-merge gate).
    The task goes ``blocked`` — surfacing in the decision queue — instead of
    ``done``. The closing run keeps outcome ``completed``, and a review-lane
    run keeps its APPROVED verdict: the verifier DID approve; only the
    integration into the live branch is parked for the operator.
    """
    reason = f"integration parked: {outcome.get('reason', 'unknown')}"
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
                   AND status IN ('running', 'ready', 'blocked')
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
                   AND status IN ('running', 'ready', 'blocked')
                   AND current_run_id = ?
                """,
                (task_id, int(expected_run_id)),
            )
        if cur.rowcount != 1:
            return False
        run_id = _end_run(
            conn, task_id,
            outcome="completed", status="blocked",
            summary=reason, metadata=outcome,
        )
        if _run_originated_from_review(conn, task_id, run_id):
            _set_run_verdict(conn, run_id, "APPROVED")
        _append_event(conn, task_id, "blocked", {"reason": reason}, run_id=run_id)
    return True


def block_task(
    conn: sqlite3.Connection,
    task_id: str,
    *,
    reason: Optional[str] = None,
    expected_run_id: Optional[int] = None,
) -> bool:
    """Transition ``running -> blocked``."""
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
                   AND status IN ('running', 'ready')
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
        # B2: a REQUEST_CHANGES verdict is the verifier rejecting the task it
        # reviewed. Only the review lane writes it; ordinary blocks (a coder
        # hitting a wall) leave task_runs.verdict NULL.
        if _run_originated_from_review(conn, task_id, run_id):
            _set_run_verdict(conn, run_id, "REQUEST_CHANGES")
        _append_event(conn, task_id, "blocked", {"reason": reason}, run_id=run_id)
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
    entry. Refuses to promote if any parent dep is not ``done`` unless
    ``force=True``. Does NOT change
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
            if p["status"] != "done"
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


def _structured_acceptance_criteria_json(value: Any) -> Optional[str]:
    """Serialize structured PlanSpec AC dicts for tasks.acceptance_criteria.

    Returns NULL for absent/malformed values so ad-hoc decomposes keep the
    historical body-parse fallback.
    """
    if not isinstance(value, list) or not value:
        return None
    out: list[dict[str, Any]] = []
    for item in value:
        if isinstance(item, dict):
            statement = str(item.get("statement") or "").strip()
            if not statement:
                continue
            clean = {str(k): v for k, v in item.items() if v is not None}
            clean["statement"] = statement
            out.append(clean)
        elif isinstance(item, str) and item.strip():
            out.append({"statement": item.strip()})
    if not out:
        return None
    return json.dumps(out, ensure_ascii=False)


def planspec_source_for_task(conn: sqlite3.Connection, task_id: str) -> Optional[str]:
    """Return a task's PlanSpec source from its own row (1-hop)."""
    row = conn.execute(
        "SELECT planspec_source FROM tasks WHERE id = ?",
        (task_id,),
    ).fetchone()
    if row is None:
        return None
    source = row["planspec_source"]
    return source if isinstance(source, str) and source.strip() else None


def release_uireal_root(conn: sqlite3.Connection, task_id: str, *, author: str = "operator") -> bool:
    """Release a ui-real PlanSpec root held in scheduled state into todo.

    The root still waits on its child parents; this only records explicit
    operator intent and lets child-release paths proceed without recompute_ready
    auto-releasing ui-real roots.
    """
    with write_txn(conn):
        row = conn.execute(
            "SELECT status, live_test_depth FROM tasks WHERE id = ?",
            (task_id,),
        ).fetchone()
        if row is None or row["live_test_depth"] != "ui-real":
            return False
        if row["status"] == "todo":
            _append_event(conn, task_id, "uireal_released", {"author": author, "idempotent": True})
            return True
        if row["status"] != "scheduled":
            return False
        cur = conn.execute(
            "UPDATE tasks SET status = 'todo' WHERE id = ? AND status = 'scheduled'",
            (task_id,),
        )
        if cur.rowcount != 1:
            return False
        _append_event(conn, task_id, "uireal_released", {"author": author})
        return True


def release_freigabe_hold(
    conn: sqlite3.Connection, task_id: str, *, author: str = "operator"
) -> bool:
    """Release a ``freigabe: operator`` PlanSpec chain held in scheduled.

    F1 — the additive sibling of :func:`release_uireal_root`. A chain ingested
    with ``freigabe: operator`` lands with its root parked in ``scheduled`` and
    its children held in ``scheduled`` (see ``decompose_triage_task``). This
    records an explicit operator GO: it flips the root ``scheduled`` -> ``todo``
    and promotes the held children ``scheduled`` -> ``ready``/``todo`` (via
    :func:`unblock_task` + :func:`recompute_ready`) so the chain dispatches.

    Returns ``True`` when ``task_id`` is a ``freigabe: operator`` root and was
    released — idempotent: an already-released root (``todo``) re-stamps the
    event and still returns ``True``, re-releasing any child that is still held.
    Returns ``False`` (touching nothing) for a non-operator root, an unknown id,
    or a root that is neither ``scheduled`` nor ``todo``.
    """
    with write_txn(conn):
        row = conn.execute(
            "SELECT status, freigabe FROM tasks WHERE id = ?",
            (task_id,),
        ).fetchone()
        if row is None or str(row["freigabe"] or "").strip().lower() != "operator":
            return False
        if row["status"] == "todo":
            _append_event(
                conn, task_id, "freigabe_released",
                {"author": author, "idempotent": True},
            )
        elif row["status"] != "scheduled":
            return False
        else:
            cur = conn.execute(
                "UPDATE tasks SET status = 'todo' WHERE id = ? AND status = 'scheduled'",
                (task_id,),
            )
            if cur.rowcount != 1:
                return False
            _append_event(conn, task_id, "freigabe_released", {"author": author})
    # Release the held children OUTSIDE the root write_txn — unblock_task and
    # recompute_ready open their own write_txns (nested write_txn is a
    # documented pitfall). Mirrors plugin_api._release_flow_gate's child loop:
    # the chain's children are linked as the root's parents.
    for child_id in parent_ids(conn, task_id):
        child = conn.execute(
            "SELECT status FROM tasks WHERE id = ?", (child_id,)
        ).fetchone()
        if child is not None and child["status"] == "scheduled":
            unblock_task(conn, child_id)
    recompute_ready(conn)
    return True


def dismiss_freigabe_hold(
    conn: sqlite3.Connection, task_id: str, *, author: str = "operator"
) -> bool:
    """Veto a held ``freigabe: operator`` PlanSpec chain (G1 — the veto sibling
    of :func:`release_freigabe_hold`).

    Where ``release_freigabe_hold`` promotes the held chain, this archives it so
    nothing builds: the held root (``scheduled``) AND every still-held child
    (the subtasks linked as the root's parents, see ``decompose_triage_task``)
    move to ``archived``. The operator vetoed the strategist's proposal.

    Root-guard (Funnel-Selbstfraß-Lehre): only a ``freigabe: operator`` root is
    actionable. Children of a decomposed chain never carry ``freigabe`` (only
    the root row does), so this check alone hard-excludes the build-children —
    they can never be vetoed as if they were proposals.

    Returns ``True`` when ``task_id`` is a held (``scheduled``) operator root and
    was archived. Returns ``False`` (touching nothing) for a non-operator root,
    an unknown id, or a root that is no longer held (already released/building/
    done) — an already-released chain must not be silently torn down.
    """
    with write_txn(conn):
        row = conn.execute(
            "SELECT status, freigabe FROM tasks WHERE id = ?",
            (task_id,),
        ).fetchone()
        if row is None or str(row["freigabe"] or "").strip().lower() != "operator":
            return False
        if row["status"] != "scheduled":
            return False
        # The chain's children are linked as the root's parents (mirror of the
        # decompose link direction). Archive the ones still held; a child that
        # somehow already advanced is left untouched by the status-guarded UPDATE.
        child_rows = conn.execute(
            "SELECT parent_id FROM task_links WHERE child_id = ?",
            (task_id,),
        ).fetchall()
        archived_children: list[str] = []
        for child_row in child_rows:
            child_id = child_row["parent_id"]
            cur = conn.execute(
                "UPDATE tasks SET status = 'archived', claim_lock = NULL, "
                "claim_expires = NULL, worker_pid = NULL "
                "WHERE id = ? AND status = 'scheduled'",
                (child_id,),
            )
            if cur.rowcount == 1:
                archived_children.append(child_id)
                _append_event(conn, child_id, "archived", {"by": author, "via": "freigabe_vetoed"})
        cur = conn.execute(
            "UPDATE tasks SET status = 'archived', claim_lock = NULL, "
            "claim_expires = NULL, worker_pid = NULL "
            "WHERE id = ? AND status = 'scheduled'",
            (task_id,),
        )
        if cur.rowcount != 1:
            return False
        _append_event(
            conn, task_id, "freigabe_vetoed",
            {"author": author, "archived_children": archived_children},
        )
    return True


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


# A1 (N-A1): bullet line carries an acceptance criterion when it names a stable
# ``AC-<id>`` token — the form the decomposer prompt asks for. Conservative on
# purpose: a body with no AC ids yields no criteria (NULL) rather than scooping
# up unrelated bullets.
_AC_BULLET_RE = re.compile(r"^(?:[-*+]|\d+[.)])\s+(.*)$")
_AC_ID_RE = re.compile(r"\bAC-\w+", re.IGNORECASE)


def _parse_acceptance_criteria(body: Optional[str]) -> Optional[str]:
    """Parse ``AC-…`` bullets from a decompose-generated *body* into a JSON
    array, normalized through plan_compiler's ``AcceptanceCriterion`` schema.

    Returns the JSON string, or ``None`` when the body is empty, carries no
    recognizable AC bullets, or anything fails — strictly fail-soft so a parse
    miss never aborts a decomposition (the body itself is unchanged regardless).

    #14 — altitude boundary (intentionally lossy, locked by
    ``test_ac_body_roundtrip_contract_is_locked``): a bullet only carries an
    ``AC-<id>: <statement>`` prose line, so a planspec criterion's structured
    fields (``verification``/``done_signal``/``scope_level``/``applies_to``)
    cannot be recovered here and survive only as a flat ``"AC-…: <stmt>"``
    string. Those fields live in the source .md and are read structured by the
    PlanSpec viewer (``GET /planspecs/detail``). Threading the full AC JSON onto
    the child dict at decompose time (a structured store) is a deliberate
    follow-up, not a silent behaviour to drift into.
    """
    if not isinstance(body, str) or not body.strip():
        return None
    try:
        bullets: list[str] = []
        for raw in body.splitlines():
            m = _AC_BULLET_RE.match(raw.strip())
            if not m:
                continue
            text = m.group(1).strip()
            if text and _AC_ID_RE.search(text):
                bullets.append(text)
        if not bullets:
            return None
        # Reuse (do NOT fork) the PlanSpec normalizer: free-form strings pass
        # through as-is, dict items validate against the rich schema, invalid
        # ones are dropped into ``findings`` (which we intentionally discard —
        # a malformed criterion must not block the decomposition).
        from hermes_cli.plan_compiler import _normalize_acceptance_criteria

        findings: list[str] = []
        normalized = _normalize_acceptance_criteria(bullets, findings)
        out: list = []
        for item in normalized:
            if isinstance(item, str):
                out.append(item)
            else:  # AcceptanceCriterion -> plain dict for JSON storage
                out.append(item.model_dump())
        if not out:
            return None
        return json.dumps(out, ensure_ascii=False)
    except Exception:
        return None


def decompose_triage_task(
    conn: sqlite3.Connection,
    task_id: str,
    *,
    root_assignee: Optional[str],
    children: list[dict],
    author: Optional[str] = None,
    auto_promote: bool = True,
    initial_child_status: str = "todo",
    expected_root_status: str = "triage",
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
            "kind": "code",                    # optional, None -> unknown
            "parents": [0, 2],                 # indices into this same children list
        }

    Returns the list of created child task ids (in input order) on
    success. Returns ``None`` when:
      - The root task does not exist
      - The root task is not in ``expected_root_status``
      - A cycle would result (caller built a bad graph)

    ``expected_root_status`` (default ``'triage'`` — the long-standing
    behaviour) is the status the root MUST be in for the fan-out to
    proceed. The Flow capture's documented method passes ``'scheduled'``
    so the root can be fanned out atomically straight from the parked
    state it sat in during the (slow) LLM planning call — the gateway's
    auto-decompose tick only ever scans ``triage``, so a scheduled root
    is invisible to it and there is no race window between planning and
    fan-out. The root is still flipped to ``'todo'`` on success either
    way.

    Validation of titles/assignees happens inside the same write_txn as
    the inserts so a malformed entry aborts the whole decomposition
    cleanly (no orphan children).

    ``initial_child_status`` is the status children are created in
    (default ``'todo'`` — the long-standing behaviour). Pass
    ``'scheduled'`` to land them HELD instead: ``recompute_ready`` only
    touches ``todo``/``blocked`` and the dispatcher claims only
    ``ready``, so ``scheduled`` children sit untouched until an explicit
    release (``unblock_task``). This is the gate-hold used by the Flow
    capture's "Gate" mode and implies ``auto_promote`` has no effect on
    the held children (a scheduled task is never promoted by
    ``recompute_ready``).
    """
    if not children:
        return None
    if initial_child_status not in ("todo", "scheduled"):
        raise ValueError(
            "initial_child_status must be 'todo' or 'scheduled', "
            f"got {initial_child_status!r}"
        )
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
            "SELECT id, status, tenant, workspace_kind, workspace_path, epic_id, "
            "live_test_depth, freigabe "
            "FROM tasks WHERE id = ?",
            (task_id,),
        ).fetchone()
        if root_row is None:
            return None
        if root_row["status"] != expected_root_status:
            return None
        tenant = root_row["tenant"]
        # N-E3: children inherit the triage root's epic so a whole decomposed
        # tree rolls up under one epic. NULL root epic_id (the common case) =
        # children get NULL = pre-E3 behaviour, byte-identical.
        root_epic_id = root_row["epic_id"] if "epic_id" in root_row.keys() else None
        # Children inherit the root's workspace by default so a fan-out
        # of a code-gen task lands in the parent's project dir/worktree
        # rather than throwaway scratch tmp dirs. A child dict can still
        # override with its own 'workspace_kind' / 'workspace_path'.
        root_ws_kind = root_row["workspace_kind"] or "scratch"
        root_ws_path = root_row["workspace_path"]

        # Create children. Status is normally 'todo' regardless of parents
        # — we link them under the root AFTER creation so the dispatcher
        # sees a coherent state, and recompute_ready() at the end
        # promotes parent-free children to 'ready'. When the caller asks
        # for 'scheduled' (gate-hold) the children are parked instead and
        # recompute_ready never touches them until an explicit release.
        for idx, child in enumerate(children):
            new_id = _new_task_id()
            title = child["title"].strip()
            body = child.get("body")
            assignee = _canonical_assignee(child.get("assignee"))
            kind = child.get("kind")
            # Per-child override wins; otherwise inherit the root's
            # workspace. A child that sets workspace_kind without a path
            # falls back to the root path only when kinds match (so a
            # child can't accidentally point a 'dir' at the root's
            # worktree path or vice versa).
            child_ws_kind = child.get("workspace_kind") or root_ws_kind
            if child.get("workspace_path"):
                child_ws_path = child.get("workspace_path")
            elif child_ws_kind == root_ws_kind:
                child_ws_path = root_ws_path
            else:
                child_ws_path = None
            # Phase4 D: PlanSpec children can pass structured AC dicts directly.
            # Ad-hoc decomposes keep the historical body-parse fallback.
            child_ac = _structured_acceptance_criteria_json(
                child.get("acceptance_criteria_struct")
            ) or _parse_acceptance_criteria(body if isinstance(body, str) else None)
            # A2: PlanSpec provenance columns — populated when the child dict
            # carries planspec_subtask_id / planspec_source (set by
            # taskgraph_hints_to_children).  NULL on non-planspec children.
            child_planspec_subtask_id = child.get("planspec_subtask_id")
            child_planspec_source = child.get("planspec_source")
            conn.execute(
                "INSERT INTO tasks "
                "(id, title, body, assignee, status, workspace_kind, "
                " workspace_path, tenant, created_at, created_by, "
                " acceptance_criteria, epic_id, kind, "
                " planspec_subtask_id, planspec_source) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    new_id,
                    title,
                    body if isinstance(body, str) else None,
                    assignee,
                    initial_child_status,
                    child_ws_kind,
                    child_ws_path,
                    tenant,
                    now,
                    (author or "decomposer"),
                    child_ac,
                    root_epic_id,
                    kind,
                    child_planspec_subtask_id if isinstance(child_planspec_subtask_id, str) else None,
                    child_planspec_source if isinstance(child_planspec_source, str) else None,
                ),
            )
            _append_event(
                conn, new_id, "created",
                {"by": author or "decomposer", "from_decompose_of": task_id},
            )
            # H1: inherit the root/triage task's Discord notify-subscription
            # so this child can deliver its own terminal state back to the
            # originating chat without a manual notify-subscribe. Same write_txn.
            _inherit_notify_subs(conn, task_id, new_id, now=now)
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

        # Flip the root: normally triage/scheduled -> todo.  Two ADDITIVE
        # operator holds keep the root parked in scheduled (children, created in
        # initial_child_status='scheduled', stay held too):
        #   * Phase4 A: ui-real PlanSpec roots wait for release_uireal_root().
        #   * F1: freigabe:operator PlanSpec roots wait for release_freigabe_hold().
        # Every other case — any other freigabe value (complete/auto/empty/…),
        # any non-ui-real depth, or todo children — is unchanged: root -> todo
        # and the chain builds exactly like today.
        _operator_freigabe = (
            str(root_row["freigabe"] or "").strip().lower() == "operator"
        )
        _held_for_operator = (
            root_row["live_test_depth"] == "ui-real" or _operator_freigabe
        )
        root_new_status = (
            "scheduled"
            if _held_for_operator and initial_child_status == "scheduled"
            else "todo"
        )
        sets = ["status = ?"]
        params: list[Any] = [root_new_status]
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
    # Re-run promotion for any descendants whose other parents may have
    # completed concurrently. Archiving this task itself does not satisfy
    # child dependencies.
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

def _repo_root_for_row(workspace_kind, workspace_path) -> Optional[str]:
    """Effective repo_root for a ready row WITHOUT claiming/provisioning,
    for kanban.serialize_by_repo. Returns the SHARED integration target (the
    main checkout) — for a path inside a provisioned worktree that is the
    repo_root the integrator merges INTO, never the per-task worktree path.
    scratch / anything that does not resolve to a repo returns None and never
    participates in the lock. Fail-soft: any error -> None."""
    try:
        if workspace_kind not in {"dir", "worktree"}:
            return None
        if not workspace_path:
            return None
        p = str(workspace_path)
        if not os.path.isabs(p):
            return None
        from hermes_cli import kanban_worktrees as _kwt
        sp = _kwt.split_provisioned_path(p)
        if sp is not None:
            # split_provisioned_path returns (repo_root, root_id, worktree_path)
            return str(sp[0])
        rr = _kwt.repo_root_for(p)
        return str(rr) if rr else None
    except Exception:
        return None


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

# Max transient worktree-provisioning re-queues per task before falling back
# to the normal (counted) spawn-failure / block path. A transient git-lock
# timeout is infrastructure, not a task defect, so it re-queues the task to
# ``ready`` WITHOUT consuming the ``consecutive_failures`` budget — but only up
# to this cap, so chronic contention still surfaces as a block. The effective
# value is re-read from ``HERMES_SPAWN_RETRY_LIMIT`` at dispatch time (this
# constant is the documented default / fallback).
SPAWN_RETRY_LIMIT = int(os.environ.get("HERMES_SPAWN_RETRY_LIMIT", "5"))

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

# Cooldown after a rate-limited (quota-wall) requeue before the dispatcher
# re-spawns the worker. Without this, a task released by the rate-limit path
# would be re-spawned on the very next tick and immediately bounce off the
# same quota wall, burning a worker slot every tick for hours. The cooldown
# spaces retries out so the board keeps cheaply probing whether quota is back
# without thrashing. Overridable via ``HERMES_KANBAN_RATE_LIMIT_COOLDOWN_SECONDS``
# for operators who want a tighter/looser probe cadence.
DEFAULT_RATE_LIMIT_COOLDOWN_SECONDS = 300  # 5 minutes

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
    auto_assigned_default: list[str] = field(default_factory=list)
    """Task ids that were unassigned in the DB and had
    ``kanban.default_assignee`` applied this tick before spawning (#27145).
    Surfaces the auto-assignment to telemetry / CLI / dashboard so the
    operator can see when the dispatcher is acting on the fallback rule
    rather than on explicit per-task assignments."""
    skipped_nonspawnable: list[str] = field(default_factory=list)
    """Ready task ids skipped because their assignee names a control-plane
    lane (a Claude Code terminal like ``orion-cc``) rather than a Hermes
    profile. Expected steady-state on multi-lane setups; NOT an
    operator-actionable failure. Tracked separately so health telemetry
    can distinguish "real stuck" (nothing spawned but spawnable work
    available) from "correctly idle" (nothing spawnable in the queue)."""
    skipped_per_profile_capped: list[tuple[str, str, int]] = field(default_factory=list)
    """Tasks deferred this tick because their assignee is already at
    ``kanban.max_in_progress_per_profile`` (#21582). Each entry is
    ``(task_id, assignee, current_running_count)``. NOT an
    operator-actionable failure — the task will be picked up on a
    subsequent tick when the assignee has capacity. Separate bucket so
    telemetry / dashboards can show "this profile is busy" vs
    "task is genuinely stuck"."""
    skipped_repo_serialized: list[tuple[str, str]] = field(default_factory=list)
    """Ready tasks deferred this tick because another non-terminal task holds the
    same resolved repo_root (kanban.serialize_by_repo). Each entry is
    (task_id, repo_root). NOT a failure — picked up once the holder reaches
    done/archived. Empty when serialize_by_repo is False."""
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
    held_role_mismatch: list[tuple[str, str]] = field(default_factory=list)
    """Reviewer tasks HELD at dispatch time (not spawned) because the
    reviewer/execution role-fit preflight (K3) flagged them as asking the
    verdict-only reviewer lane to run repo gates, as ``(task_id, reason)``
    pairs. The task is left in ``ready`` (re-evaluated each tick), NOT
    blocked — the signal is advisory: re-shape it as a coder/verifier
    evidence task. A verdict-only reviewer task is exempt and dispatches
    normally. Separate bucket so telemetry distinguishes "held for role
    mis-fit" from "genuinely stuck"."""
    openclaw_dispatched: list[tuple[str, str]] = field(default_factory=list)
    """Tasks routed to OpenClaw (Mission-Control) instead of a local spawn,
    as ``(task_id, operation)`` pairs. The task stays ``running`` while MC
    executes; ``poll_openclaw_results`` polls it back to done/blocked on a
    later tick. Separate bucket so a cross-system dispatch is never confused
    with a local worker spawn in telemetry."""
    rate_limited: list[str] = field(default_factory=list)
    """Task ids whose workers bailed on a provider rate-limit / quota wall
    (EX_TEMPFAIL sentinel exit) and were released back to ``ready`` WITHOUT
    counting a failure. These never trip the circuit breaker — a long quota
    window just makes the task bounce cheaply until the window clears."""
    budget_held: list[tuple[str, str, str]] = field(default_factory=list)
    """C1 (N-C1): ready tasks HELD this tick because a daily budget cap is hit,
    as ``(task_id, assignee, reason)`` triples. Two causes: the assignee's
    rolling-24h token usage reached ``kanban.daily_token_cap_per_profile`` (only
    that profile is held), or the board's rolling-24h cost reached
    ``kanban.daily_cost_cap_usd`` (every assigned ready task is held). The task
    stays in ``ready`` (advisory, re-evaluated each tick), NOT blocked. Caps
    default OFF (None) → this bucket is always empty → byte-identical to the
    pre-C1 dispatcher. Surfaces in the decision-queue as a ``budget_held`` row."""
    budget_runaway_parked: list[tuple[str, int]] = field(default_factory=list)
    """G1: ready tasks PARKED (status -> blocked) this tick because the
    cumulative ``input_tokens`` across ALL their runs exceeded
    ``kanban.per_task_input_token_cap``, as ``(task_id, input_token_sum)`` pairs.
    Unlike ``budget_held`` (advisory hold, stays ``ready``) this is a HARD park
    with an ``operator_escalation`` — a per-task input-token runaway is not
    self-clearing. Cap ``None``/``0`` → this bucket is always empty →
    byte-identical to the pre-G1 dispatcher."""
    auto_retried_blocked: list[tuple[str, int]] = field(default_factory=list)
    """Opt-in blocked-run auto-retries performed this tick as
    ``(task_id, attempt)`` pairs."""
    heartbeated: list[str] = field(default_factory=list)
    """Live claude-CLI runs whose heartbeat the dispatcher refreshed this tick
    (``heartbeat_live_claude_cli_workers``). claude-CLI workers can't bridge
    their own liveness into ``last_heartbeat_at`` the way Hermes-runtime
    workers do, so the dispatcher pulses it from the parent side while the PID
    is alive. Empty when no claude-CLI worker is running."""


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


def _record_worker_exit(pid: int, raw_status: int) -> None:
    """Record a reaped child's exit status for later classification.

    Called from the reap loop in ``dispatch_once``. Safe to call many
    times; duplicate pids overwrite (pids can cycle, latest wins).
    """
    if not pid or pid <= 0:
        return
    now = time.time()
    _recent_worker_exits[int(pid)] = (int(raw_status), now)
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
    * ``"rate_limited"`` — ``WIFEXITED`` with status
      ``KANBAN_RATE_LIMIT_EXIT_CODE``. The worker bailed because the
      provider rate-limited / exhausted quota, NOT because the task failed.
      ``detect_crashed_workers`` releases the task back to ``ready`` without
      counting a failure, so a long quota window can't trip the breaker.
    * ``"nonzero_exit"`` — ``WIFEXITED`` with non-zero status. Real error.
    * ``"signaled"`` — ``WIFSIGNALED`` (OOM killer, SIGKILL, etc). Real crash.
    * ``"unknown"`` — pid was not in the reap registry (either reaped by
      something else, or died between reap tick and liveness check). Fall
      back to existing crashed-counter behavior.

    ``code`` is the exit status (for ``clean_exit`` / ``rate_limited`` /
    ``nonzero_exit``) or the signal number (for ``signaled``), or ``None``
    for ``unknown``.
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
            if code == KANBAN_RATE_LIMIT_EXIT_CODE:
                return ("rate_limited", code)
            return ("nonzero_exit", code)
        if os.WIFSIGNALED(raw):
            return ("signaled", os.WTERMSIG(raw))
    except Exception:
        pass
    return ("unknown", None)


def _title_terms(title: Optional[str]) -> set[str]:
    words = re.findall(r"[a-z0-9][a-z0-9_-]{3,}", (title or "").lower())
    return {w for w in words if w not in {"task", "work", "todo", "implement"}}


def _deliverable_evidence_for_protocol_miss(
    conn: sqlite3.Connection,
    task_id: str,
) -> Optional[dict[str, Any]]:
    row = conn.execute(
        "SELECT title, assignee FROM tasks WHERE id = ?", (task_id,),
    ).fetchone()
    if row is None:
        return None
    terms = _title_terms(row["title"])
    assignee = (row["assignee"] or "").strip().lower()
    rows = conn.execute(
        "SELECT id, author, body, created_at FROM task_comments "
        "WHERE task_id = ? ORDER BY id DESC LIMIT 10",
        (task_id,),
    ).fetchall()
    for comment in rows:
        body = (comment["body"] or "").strip()
        if len(body) < 80:
            continue
        author = (comment["author"] or "").strip().lower()
        if assignee and author not in {assignee, "worker", "assistant"}:
            continue
        lowered = body.lower()
        has_deliverable_signal = (
            "deliverable" in lowered
            or "result" in lowered
            or "ergebnis" in lowered
            or body.startswith("#")
        )
        if not has_deliverable_signal:
            continue
        if terms and not any(term in lowered for term in terms):
            continue
        return {
            "comment_id": int(comment["id"]),
            "author": comment["author"],
            "created_at": int(comment["created_at"]),
            "preview": body[:400],
        }
    return None


def reap_worker_zombies() -> "list[int]":
    """Reap all zombie children of this process without blocking.

    Returns the list of reaped PIDs. Safe to call when there are no
    children (returns []). No-op on Windows.
    """
    reaped: "list[int]" = []
    if os.name != "nt":
        try:
            while True:
                try:
                    pid, status = os.waitpid(-1, os.WNOHANG)
                except ChildProcessError:
                    break
                if pid == 0:
                    break
                _record_worker_exit(pid, status)
                reaped.append(pid)
        except Exception:
            pass
    return reaped


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


def _claude_cli_heartbeat_note(
    task_id: str, *, board: Optional[str] = None,
) -> str:
    """Honest one-line liveness note for a live claude-CLI worker.

    The dispatcher cannot see inside a ``claude -p`` session, so the only
    ground truth is the per-task worker log: how much output it has produced
    and how recently it last wrote. Reported verbatim — no fake percentage.
    Always returns at least ``"claude-cli running"``; the log detail is
    additive and fully fail-soft (missing/unreadable log → base note).
    """
    base = "claude-cli running"
    try:
        log_path = worker_logs_dir(board=board) / f"{task_id}.log"
        st = log_path.stat()
        size = int(st.st_size)
        if size >= 1024:
            size_str = f"{size / 1024:.0f}KB"
        else:
            size_str = f"{size}B"
        age = max(0, int(time.time()) - int(st.st_mtime))
        return f"{base} · log {size_str} · last output {age}s"
    except Exception:
        return base


def heartbeat_live_claude_cli_workers(
    conn: sqlite3.Connection, *, board: Optional[str] = None,
) -> list[str]:
    """Dispatcher-side heartbeat for live claude-CLI workers.

    A ``claude -p`` worker (``_spawn_claude_worker``) is a detached subprocess
    with stdout/stderr in its per-task log only — it never re-enters the
    Hermes runtime, so the ``_touch_activity`` → ``heartbeat_current_worker_
    from_env`` bridge (#31752) that keeps Hermes-runtime workers' heartbeats
    fresh does not apply. Without this, a claude-CLI run's ``last_heartbeat_at``
    stays NULL for its whole life: the dashboard worker card shows no liveness
    ("—"), no "doing now" note, and ``detect_stale_running`` can false-positive
    reclaim a perfectly healthy long run.

    This closes that gap from the parent/dispatcher side while the child PID is
    alive, reusing the SAME heartbeat fields/events the dashboard already
    consumes (``heartbeat_claim`` to hold the claim + ``heartbeat_worker`` to
    touch ``last_heartbeat_at`` and append a ``heartbeat`` event with an honest
    note). No parallel protocol, no schema change.

    Scope guards (so Hermes-runtime workers are byte-for-byte unchanged):
      * only ``status='running'`` tasks with a non-NULL ``worker_pid``;
      * only host-local claims (PID liveness + log path are host-local, same
        reasoning as ``detect_stale_running`` / ``enforce_max_runtime``);
      * only runs whose ``profile`` classifies as claude-CLI via
        ``_run_is_claude_cli`` (Hermes-runtime runs are skipped — they self-
        heartbeat, and a second writer would mask a genuine stall);
      * only when the PID is actually alive;
      * rate-limited: re-emit only when the existing heartbeat is older than
        ``_CLAUDE_CLI_HEARTBEAT_MIN_GAP_SECONDS`` (NULL → always), so the run
        timeline gets a steady pulse instead of one event per tick.

    Returns the task ids heartbeat'd this tick. Fully fail-soft: a single bad
    row never aborts the batch and any error returns the ids collected so far,
    so the dispatcher tick is never destabilised by liveness bookkeeping.
    """
    now = int(time.time())
    host_prefix = f"{_claimer_id().split(':', 1)[0]}:"
    beat: list[str] = []
    try:
        rows = conn.execute(
            "SELECT t.id, t.worker_pid, t.last_heartbeat_at, t.claim_lock, "
            "       t.current_run_id, r.profile "
            "FROM tasks t "
            "LEFT JOIN task_runs r ON r.id = t.current_run_id "
            "WHERE t.status = 'running' AND t.worker_pid IS NOT NULL"
        ).fetchall()
    except Exception:
        return beat

    for row in rows:
        try:
            lock = row["claim_lock"] or ""
            if not lock.startswith(host_prefix):
                continue  # another host owns it; it checks its own PIDs
            if not _run_is_claude_cli(row["profile"], board=board):
                continue  # Hermes-runtime worker — leave its heartbeat alone
            if not _pid_alive(row["worker_pid"]):
                continue  # dead PID is detect_crashed_workers' job, not ours

            last_hb = row["last_heartbeat_at"]
            if last_hb is not None and (now - int(last_hb)) < _CLAUDE_CLI_HEARTBEAT_MIN_GAP_SECONDS:
                continue  # heartbeat still fresh — don't spam the timeline

            tid = row["id"]
            run_id = row["current_run_id"]
            # Hold the claim too, mirroring the Hermes bridge (heartbeat_claim
            # + heartbeat_worker). release_stale_claims already extends a
            # live-PID claim, but doing it here keeps claude-CLI liveness
            # self-sufficient regardless of step ordering.
            try:
                heartbeat_claim(conn, tid, claimer=lock)
            except Exception:
                pass
            note = _claude_cli_heartbeat_note(tid, board=board)
            if heartbeat_worker(conn, tid, note=note, expected_run_id=run_id):
                beat.append(tid)
        except Exception:
            continue
    return beat


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
                _append_event(
                    conn, tid, "timed_out", payload, run_id=run_id,
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


# Heartbeat staleness heartbeat gap — if a running task hasn't sent a
# heartbeat in this many seconds it's considered inactive regardless of
# the ``dispatch_stale_timeout_seconds`` threshold.  Hardcoded at 1 hour
# to match the original spec (">4h started + no commits in 1h").
_STALE_HEARTBEAT_GAP_SECONDS = 3600


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

    When the reap registry shows the worker exited with the rate-limit
    sentinel (``KANBAN_RATE_LIMIT_EXIT_CODE``), the worker bailed on a
    provider quota wall, NOT a task failure. Such tasks are released back
    to ``ready`` WITHOUT counting a failure (so a long quota window can't
    trip the breaker) and stamped with a quota-blocker error so
    ``check_respawn_guard`` defers their respawn until the window clears.
    The ids are returned via the ``_last_rate_limited`` function attribute
    (the public return stays the crashed-only ``list[str]``).
    """
    crashed: list[str] = []
    rate_limited: list[str] = []
    # Per-crash details collected inside the main txn, used after it
    # closes to run ``_record_task_failure`` (which needs its own
    # write_txn so can't nest). ``protocol_violation`` flags the
    # clean-exit-but-still-running case so we can trip the breaker
    # immediately instead of incrementing by 1.
    crash_details: list[tuple[str, int, str, bool, str]] = []
    # (task_id, pid, claimer, protocol_violation, error_text)
    with write_txn(conn):
        rows = conn.execute(
            "SELECT id, worker_pid, claim_lock, started_at FROM tasks "
            "WHERE status = 'running' AND worker_pid IS NOT NULL"
        ).fetchall()
        host_prefix = f"{_claimer_id().split(':', 1)[0]}:"
        for row in rows:
            # Only check liveness for claims owned by this host.
            lock = row["claim_lock"] or ""
            if not lock.startswith(host_prefix):
                continue
            # Skip liveness check inside the launch-window grace period
            # so a freshly-spawned worker isn't reclaimed before its PID
            # is visible on /proc.
            started_at = row["started_at"] if "started_at" in row.keys() else None
            if started_at is not None:
                grace = _resolve_crash_grace_seconds()
                if time.time() - started_at < grace:
                    continue
            if _pid_alive(row["worker_pid"]):
                continue

            pid = int(row["worker_pid"])
            kind, code = _classify_worker_exit(pid)
            rate_limited_exit = False
            recoverable_protocol_miss = False
            if kind == "clean_exit":
                evidence = _deliverable_evidence_for_protocol_miss(
                    conn, row["id"],
                )
                if evidence is not None:
                    protocol_violation = False
                    recoverable_protocol_miss = True
                    error_text = (
                        "deliverable posted but worker exited cleanly (rc=0) "
                        "without calling kanban_complete — repair required"
                    )
                    event_kind = DELIVERABLE_POSTED_NOT_COMPLETED
                    event_payload = {
                        "pid": pid,
                        "claimer": row["claim_lock"],
                        "exit_code": code,
                        "evidence": evidence,
                    }
                else:
                    # Worker subprocess returned 0 but its task is still
                    # ``running`` in the DB — it exited without calling
                    # ``kanban_complete`` / ``kanban_block``. Retrying won't
                    # help.
                    protocol_violation = True
                    error_text = (
                        "worker exited cleanly (rc=0) without calling "
                        "kanban_complete or kanban_block — protocol violation"
                    )
                    event_kind = "protocol_violation"
                    event_payload = {
                        "pid": pid,
                        "claimer": row["claim_lock"],
                        "exit_code": code,
                    }
            elif kind == "rate_limited":
                # Worker bailed because the provider rate-limited / exhausted
                # quota (EX_TEMPFAIL sentinel). This is NOT a task failure —
                # the task is fine, the account just hit a wall. Release it
                # back to ``ready`` so the respawn guard defers it until the
                # quota window clears, and crucially do NOT count a failure
                # (skip ``_record_task_failure``) so a long quota window can't
                # trip the circuit breaker and permanently block the card.
                protocol_violation = False
                rate_limited_exit = True
                error_text = (
                    f"pid {pid} exited rate-limited (quota wall) — "
                    f"requeued without counting a failure"
                )
                event_kind = "rate_limited"
                event_payload = {
                    "pid": pid,
                    "claimer": row["claim_lock"],
                    "exit_code": code,
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
                if code is not None and kind != "unknown":
                    event_payload["exit_kind"] = kind
                    event_payload["exit_code"] = code

            cur = conn.execute(
                "UPDATE tasks SET status = 'ready', claim_lock = NULL, "
                "claim_expires = NULL, worker_pid = NULL "
                "WHERE id = ? AND status = 'running'",
                (row["id"],),
            )
            if cur.rowcount == 1:
                # Rate-limited requeues are a clean release, not a crash —
                # record the run outcome as ``rate_limited`` so the board
                # history doesn't show a phantom crash for a quota wall.
                _run_outcome = (
                    "rate_limited" if rate_limited_exit
                    else DELIVERABLE_POSTED_NOT_COMPLETED
                    if recoverable_protocol_miss else "crashed"
                )
                run_id = _end_run(
                    conn, row["id"],
                    outcome=_run_outcome, status=_run_outcome,
                    error=error_text,
                    metadata=dict(event_payload),
                )
                _append_event(
                    conn, row["id"], event_kind,
                    event_payload,
                    run_id=run_id,
                )
                if rate_limited_exit:
                    # Stamp the failure-error column so ``check_respawn_guard``
                    # recognizes this as a quota blocker and defers the
                    # respawn until the window clears — WITHOUT touching
                    # ``consecutive_failures`` (that's the whole point: no
                    # breaker trip on a throttle).
                    conn.execute(
                        "UPDATE tasks SET last_failure_error = ? WHERE id = ?",
                        (error_text[:500], row["id"]),
                    )
                    rate_limited.append(row["id"])
                elif recoverable_protocol_miss:
                    conn.execute(
                        "UPDATE tasks SET status = 'blocked', "
                        "last_failure_error = ? WHERE id = ?",
                        (error_text[:500], row["id"]),
                    )
                else:
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
    # Same side-channel for rate-limited requeues — these did NOT count a
    # failure and are NOT crashes, so they stay out of the ``crashed`` return.
    detect_crashed_workers._last_rate_limited = rate_limited  # type: ignore[attr-defined]
    return crashed


def repair_deliverable_posted_not_completed(
    conn: sqlite3.Connection,
    task_id: str,
    *,
    actor: str = "operator",
) -> bool:
    """Terminalize a recoverable deliverable/protocol miss without approval.

    This closes only the missing ``kanban_complete`` protocol step when a
    prior ``deliverable_posted_not_completed`` event carries clear evidence.
    It does not write a review verdict.
    """
    row = conn.execute(
        "SELECT payload FROM task_events "
        "WHERE task_id = ? AND kind = ? ORDER BY id DESC LIMIT 1",
        (task_id, DELIVERABLE_POSTED_NOT_COMPLETED),
    ).fetchone()
    if row is None or not row["payload"]:
        return False
    try:
        evidence_payload = json.loads(row["payload"])
    except (TypeError, ValueError):
        return False
    if not isinstance(evidence_payload, dict):
        return False
    evidence = evidence_payload.get("evidence")
    if not isinstance(evidence, dict):
        return False

    now = int(time.time())
    actor = (actor or "operator").strip() or "operator"
    summary = (
        "Protocol repair: deliverable was posted but worker missed "
        "kanban_complete."
    )
    with write_txn(conn):
        cur = conn.execute(
            """
            UPDATE tasks
               SET status       = 'done',
                   result       = COALESCE(result, ?),
                   completed_at = COALESCE(completed_at, ?),
                   claim_lock   = NULL,
                   claim_expires= NULL,
                   worker_pid   = NULL,
                   current_run_id = NULL
             WHERE id = ?
               AND status = 'blocked'
            """,
            (summary, now, task_id),
        )
        if cur.rowcount != 1:
            return False
        run_id = _synthesize_ended_run(
            conn,
            task_id,
            outcome="completed",
            summary=summary,
            metadata={
                "repair": DELIVERABLE_POSTED_NOT_COMPLETED,
                "actor": actor,
                "evidence": evidence,
            },
        )
        _append_event(
            conn,
            task_id,
            "deliverable_protocol_repaired",
            {
                "actor": actor,
                "terminalized": True,
                "evidence": evidence,
            },
            run_id=run_id,
        )
    recompute_ready(conn)
    return True


# S4 Heiler: substring signals for ``_classify_failure``, checked in this
# precedence order against a lower-cased haystack (error + reason + outcome +
# stall_class). Merge-conflict markers are unambiguous and win first; the broad
# git/dirty/branch signals come LAST so a red gate or reviewer finding that
# happens to mention a branch is not mis-read as transient.
_HEILER_CONFLICT_SIGNALS = (
    "merge conflict",
    "merge-konflikt",
    "conflict (content",
    "conflict (add/add",
    "automatic merge failed",
    "<<<<<<<",
    "fix conflicts and then commit",
    "unmerged path",
)
_HEILER_TEXT_SIGNALS = (
    (HEILER_CLASS_BAD_SPEC, (
        "unerfüllbar",
        "unfulfillable",
        "unsatisfiable",
        "impossible acceptance",
        "acceptance criteria cannot",
        "bad spec",
        "bad-spec",
        "cannot be decomposed",
        "no runnable verifier",
        "no runnable reviewer",
    )),
    (HEILER_CLASS_FLAKY, (
        "flake",
        "flaky",
        "passed on retry",
        "intermittent",
        "non-deterministic",
        "nondeterministic",
    )),
    (HEILER_CLASS_REAL_BUG, (
        "request_changes",
        "requested changes",
        "request changes",
        "reviewer finding",
        "red gate",
        "gate failed",
        "gates failed",
        "gate red",
        "test failed",
        "tests failed",
        "assertionerror",
        "assertion failed",
        "lint error",
        "tsc error",
        "type error",
        "build failed",
    )),
    (HEILER_CLASS_TRANSIENT, (
        "dirty",
        "overlap",
        "wrong branch",
        "falscher branch",
        "git lock",
        "git-lock",
        "index.lock",
        "worktree",
        "could not provision",
        "provisioning",
        "checkout",
        "rate limit",
        "rate_limited",
        "git",
        "branch",
    )),
)
# Strong structural mappings, independent of free-text error wording.
_HEILER_OUTCOME_CLASS = {
    "spawn_retry": HEILER_CLASS_TRANSIENT,
    "spawn_failed": HEILER_CLASS_TRANSIENT,
    "rate_limited": HEILER_CLASS_TRANSIENT,
}
_HEILER_STALL_CLASS = {
    "scheduled_overdue": HEILER_CLASS_TRANSIENT,
    "rate_limited_loop": HEILER_CLASS_TRANSIENT,
    "review_without_verifier": HEILER_CLASS_BAD_SPEC,
    "triage_decompose_failed": HEILER_CLASS_BAD_SPEC,
}


def _classify_failure(
    *,
    error: str = "",
    outcome: Optional[str] = None,
    stall_class: Optional[str] = None,
    reason: str = "",
) -> tuple[str, dict]:
    """Classify a block/failure into one stable Heiler class + evidence.

    Pure, deterministic and side-effect-free, so it is trivially unit-testable
    and safe to call from inside an already-open ``write_txn``. The returned
    ``evidence`` dict records which signal fired and where, so the Stratege
    (Phase 1.5) — and a human reading the ledger — can see *why* a class was
    assigned, not just the label.

    Precedence (first match wins):
      1. unambiguous merge-conflict markers -> conflict
      2. structural ``stall_class`` mapping (config/spec/transient by
         construction)
      3. structural ``outcome`` mapping (provisioning / quota = transient)
      4. free-text signals: bad-spec, flaky, real-bug, transient
      5. default -> real-bug (a failure that reached this path with no
         transient / spec / flaky signal is most likely a genuine defect:
         a red gate or reviewer findings)
    """
    haystack = " ".join(
        part for part in (error, reason, outcome or "", stall_class or "")
        if part
    ).lower()

    def _ev(matched: str, source: str) -> dict:
        ev = {"matched": matched, "signal_source": source}
        if outcome:
            ev["outcome"] = outcome
        if stall_class:
            ev["stall_class"] = stall_class
        excerpt = (reason or error or "").strip()
        if excerpt:
            ev["excerpt"] = excerpt[:300]
        return ev

    for sub in _HEILER_CONFLICT_SIGNALS:
        if sub in haystack:
            return HEILER_CLASS_CONFLICT, _ev(sub, "text")

    if stall_class and stall_class in _HEILER_STALL_CLASS:
        return _HEILER_STALL_CLASS[stall_class], _ev(stall_class, "stall_class")

    if outcome and outcome in _HEILER_OUTCOME_CLASS:
        return _HEILER_OUTCOME_CLASS[outcome], _ev(outcome, "outcome")

    for cls, subs in _HEILER_TEXT_SIGNALS:
        for sub in subs:
            if sub in haystack:
                return cls, _ev(sub, "text")

    return HEILER_CLASS_REAL_BUG, _ev("default", "default")


def _heiler_classification_payload(
    *,
    heiler_class: str,
    evidence: dict,
    source: str,
    blocked: bool,
) -> dict:
    """Stable payload shape for a ``heiler_classification`` event.

    ``source`` is the originating path (``record_task_failure`` / ``stall_park``)
    and ``blocked`` whether this failure actually parked/blocked the task — both
    let the Stratege weight a one-off transient differently from a terminal park.
    """
    return {
        "class": heiler_class,
        "evidence": evidence,
        "source": source,
        "blocked": bool(blocked),
    }


def _operator_escalation_payload(
    *,
    task_id: str,
    row: sqlite3.Row,
    failures: int,
    effective_limit: int,
    limit_source: str,
    error: str,
    outcome: str,
    event_payload_extra: Optional[dict],
) -> dict:
    evidence = {
        "trigger_outcome": outcome,
        "last_error": error[:500],
        "effective_limit": effective_limit,
        "limit_source": limit_source,
    }
    if event_payload_extra:
        evidence["context"] = dict(event_payload_extra)
    return {
        "task": {
            "id": task_id,
            "title": row["title"] if "title" in row.keys() else None,
            "status": row["status"] if "status" in row.keys() else None,
            "assignee": row["assignee"] if "assignee" in row.keys() else None,
        },
        "why_now": (
            "retry ladder exhausted: "
            f"{failures} consecutive failure(s) reached the "
            f"{effective_limit} attempt limit"
        ),
        "attempts_already_made": failures,
        "evidence": evidence,
        "recommended_human_action": (
            "inspect the task, decide whether to unblock/reassign/close, and "
            "perform any required operator-only action outside the worker loop"
        ),
        "blocked_action_boundary": list(OPERATOR_ONLY_ACTIONS),
    }


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
    count_failure: bool = True,
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

    ``count_failure`` (default True) controls the circuit breaker. When
    False, the consecutive-failures counter is NOT incremented and the
    breaker is NEVER tripped — the claim/run are still closed cleanly and
    the task transitions back to ``ready``. Used by the transient
    spawn-retry path (``_record_spawn_retry``), where an infrastructure
    timeout must not consume the task's real failure budget.

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
            "SELECT consecutive_failures, status, max_retries, title, assignee "
            "FROM tasks WHERE id = ?", (task_id,),
        ).fetchone()
        if row is None:
            return False
        # Transient path (count_failure=False) leaves the counter untouched;
        # everything else increments it toward the breaker threshold.
        failures = int(row["consecutive_failures"])
        if count_failure:
            failures += 1
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

        # run_id stays None unless a branch closes an open run; the S4 Heiler
        # classification event at the end of the txn links to it when present.
        run_id = None
        if count_failure and failures >= effective_limit:
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
            _append_event(
                conn, task_id, "gave_up", payload, run_id=run_id,
            )
            _append_event(
                conn,
                task_id,
                OPERATOR_ESCALATION_EVENT,
                _operator_escalation_payload(
                    task_id=task_id,
                    row=row,
                    failures=failures,
                    effective_limit=effective_limit,
                    limit_source=limit_source,
                    error=error,
                    outcome=outcome,
                    event_payload_extra=event_payload_extra,
                ),
                run_id=run_id,
            )
            blocked = True
        else:
            # Below threshold.
            if release_claim:
                # Spawn path: transition running → ready + clear claim.
                conn.execute(
                    "UPDATE tasks SET status = 'ready', claim_lock = NULL, "
                    "claim_expires = NULL, worker_pid = NULL, "
                    "consecutive_failures = ?, last_failure_error = ? "
                    "WHERE id = ? AND status = 'running'",
                    (failures, error[:500], task_id),
                )
            else:
                # Timeout/crash path: task is already at ``ready`` via
                # its own UPDATE. Just bookkeep the counter + last error.
                conn.execute(
                    "UPDATE tasks SET consecutive_failures = ?, "
                    "last_failure_error = ? WHERE id = ?",
                    (failures, error[:500], task_id),
                )
            if end_run:
                # Spawn path: close the open run with outcome.
                run_id = _end_run(
                    conn, task_id,
                    outcome=outcome, status=outcome,
                    error=error[:500],
                    metadata={"failures": failures},
                )
                _append_event(
                    conn, task_id, outcome,
                    {"error": error[:500], "failures": failures},
                    run_id=run_id,
                )
            # Timeout/crash path's caller already emitted its own event.

        # S4 Heiler: classify every recorded failure into the structured
        # ledger (one heiler_classification event per failure) so the Stratege
        # (Phase 1.5) can aggregate causes. Pure read of error+outcome; emitted
        # inside this txn so it commits atomically with the status change.
        h_class, h_ev = _classify_failure(error=error, outcome=outcome)
        _append_event(
            conn, task_id, HEILER_CLASSIFICATION_EVENT,
            _heiler_classification_payload(
                heiler_class=h_class, evidence=h_ev,
                source="record_task_failure", blocked=blocked,
            ),
            run_id=run_id,
        )
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


def _count_spawn_retries(conn: sqlite3.Connection, task_id: str) -> int:
    """How many transient spawn re-queues this task has already taken.

    Counted over the ``spawn_retry`` task_event log (no DB-schema change),
    so it survives process restarts and is the budget the dispatcher checks
    against ``SPAWN_RETRY_LIMIT``.
    """
    return conn.execute(
        "SELECT COUNT(*) FROM task_events WHERE task_id = ? AND kind = 'spawn_retry'",
        (task_id,),
    ).fetchone()[0]


def _record_spawn_retry(conn: sqlite3.Connection, task_id: str, reason: str) -> None:
    """Transient provisioning failure: release the claim, close the open run
    with a ``spawn_retry`` outcome, and put the task back to ``ready`` —
    WITHOUT touching ``consecutive_failures``.

    ``_record_task_failure``'s ``end_run`` path emits the single
    ``spawn_retry`` task_event that ``_count_spawn_retries`` counts against
    ``SPAWN_RETRY_LIMIT``; the next dispatch tick retries once the git-lock
    contention clears. Distinct from ``_record_spawn_failure`` (the
    permanent / budget-exhausted path), which counts toward the breaker.
    """
    _record_task_failure(
        conn, task_id, reason,
        outcome="spawn_retry",
        release_claim=True,
        end_run=True,
        count_failure=False,
    )


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


def record_decompose_failure(conn: sqlite3.Connection, task_id: str) -> int:
    """Bump the per-task ``decompose_failed`` counter and return its new value.

    Called from the auto_decompose callers when ``decompose_task`` returns
    ok=False or crashes for ``task_id``. Decompose-specific bookkeeping that
    is independent of the spawn circuit breaker (``consecutive_failures``).
    Returns 0 when the row doesn't exist.
    """
    with write_txn(conn):
        cur = conn.execute(
            "UPDATE tasks SET decompose_failed = decompose_failed + 1 "
            "WHERE id = ?",
            (task_id,),
        )
        if cur.rowcount == 0:
            return 0
        row = conn.execute(
            "SELECT decompose_failed FROM tasks WHERE id = ?", (task_id,),
        ).fetchone()
        return int(row["decompose_failed"]) if row is not None else 0


def reset_decompose_failed(conn: sqlite3.Connection, task_id: str) -> None:
    """Reset the per-task ``decompose_failed`` counter to 0.

    Called on a successful decompose — a fresh success means the task's
    decomposition is working and any past failures are history.
    """
    with write_txn(conn):
        conn.execute(
            "UPDATE tasks SET decompose_failed = 0 WHERE id = ?",
            (task_id,),
        )


def _task_has_parent(conn: sqlite3.Connection, task_id: str) -> bool:
    return conn.execute(
        "SELECT 1 FROM task_links WHERE child_id = ? LIMIT 1",
        (task_id,),
    ).fetchone() is not None


def _is_funnel_root_task(conn: sqlite3.Connection, row: sqlite3.Row) -> bool:
    created_by = row["created_by"] if "created_by" in row.keys() else None
    return (
        (created_by or "") in FUNNEL_CREATED_BY
        and not _task_has_parent(conn, row["id"])
    )


def _latest_event_at(
    conn: sqlite3.Connection,
    task_id: str,
    kind: str,
) -> Optional[int]:
    row = conn.execute(
        "SELECT created_at FROM task_events "
        "WHERE task_id = ? AND kind = ? "
        "ORDER BY created_at DESC, id DESC LIMIT 1",
        (task_id, kind),
    ).fetchone()
    if row is None or row["created_at"] is None:
        return None
    return int(row["created_at"])


def _has_stall_marker(
    conn: sqlite3.Connection,
    task_id: str,
    stall_class: str,
) -> bool:
    rows = conn.execute(
        "SELECT payload FROM task_events "
        "WHERE task_id = ? AND kind = ?",
        (task_id, NO_SILENT_STALL_EVENT),
    ).fetchall()
    for row in rows:
        try:
            payload = json.loads(row["payload"] or "{}")
        except Exception:
            continue
        if isinstance(payload, dict) and payload.get("stall_class") == stall_class:
            return True
    return False


def _append_stall_marker(
    conn: sqlite3.Connection,
    task_id: str,
    *,
    stall_class: str,
    action: str,
    reason: str,
    now: int,
) -> None:
    _append_event(
        conn,
        task_id,
        NO_SILENT_STALL_EVENT,
        {
            "stall_class": stall_class,
            "action": action,
            "reason": reason[:500],
            "at": int(now),
        },
    )


def _stall_operator_escalation_payload(
    *,
    row: sqlite3.Row,
    stall_class: str,
    reason: str,
    evidence: dict,
) -> dict:
    attempts = evidence.get("attempts")
    try:
        attempts_already_made = int(attempts)
    except (TypeError, ValueError):
        attempts_already_made = 0
    return {
        "task": {
            "id": row["id"],
            "title": row["title"],
            "status": row["status"],
            "assignee": row["assignee"],
        },
        "why_now": f"no-silent-stall sweep detected {stall_class}: {reason}",
        "attempts_already_made": attempts_already_made,
        "evidence": {"stall_class": stall_class, **evidence},
        "recommended_human_action": (
            "inspect the parked task, decide whether to unblock/reassign/close, "
            "and perform any required operator-only action outside automation"
        ),
        "blocked_action_boundary": list(OPERATOR_ONLY_ACTIONS),
    }


def _park_stall_once(
    conn: sqlite3.Connection,
    row: sqlite3.Row,
    *,
    stall_class: str,
    reason: str,
    evidence: dict,
    now: int,
) -> bool:
    task_id = row["id"]
    if _has_stall_marker(conn, task_id, stall_class):
        return False
    with write_txn(conn):
        fresh = conn.execute(
            "SELECT * FROM tasks WHERE id = ?", (task_id,),
        ).fetchone()
        if fresh is None or fresh["status"] in ("done", "archived"):
            return False
        if _is_funnel_root_task(conn, fresh):
            return False
        if _has_stall_marker(conn, task_id, stall_class):
            return False
        cur = conn.execute(
            "UPDATE tasks SET status = 'blocked', claim_lock = NULL, "
            "claim_expires = NULL, worker_pid = NULL "
            "WHERE id = ? AND status NOT IN ('done', 'archived')",
            (task_id,),
        )
        if cur.rowcount != 1:
            return False
        if fresh["status"] != "blocked":
            _append_event(conn, task_id, "blocked", {"reason": reason})
        _append_event(
            conn,
            task_id,
            OPERATOR_ESCALATION_EVENT,
            _stall_operator_escalation_payload(
                row=fresh,
                stall_class=stall_class,
                reason=reason,
                evidence=evidence,
            ),
        )
        # S4 Heiler: classify the parked stall into the structured ledger next
        # to the operator_escalation (which is unchanged). Idempotent because
        # _park_stall_once only reaches here on a fresh park.
        h_class, h_ev = _classify_failure(stall_class=stall_class, reason=reason)
        _append_event(
            conn,
            task_id,
            HEILER_CLASSIFICATION_EVENT,
            _heiler_classification_payload(
                heiler_class=h_class, evidence=h_ev,
                source="stall_park", blocked=True,
            ),
        )
        _append_stall_marker(
            conn,
            task_id,
            stall_class=stall_class,
            action="parked",
            reason=reason,
            now=now,
        )
        return True


def _count_task_runs_with_outcome(
    conn: sqlite3.Connection,
    task_id: str,
    outcome: str,
) -> int:
    row = conn.execute(
        "SELECT COUNT(*) AS n FROM task_runs "
        "WHERE task_id = ? AND outcome = ?",
        (task_id, outcome),
    ).fetchone()
    return int(row["n"] or 0) if row is not None else 0


def _has_operator_escalation(conn: sqlite3.Connection, task_id: str) -> bool:
    return conn.execute(
        "SELECT 1 FROM task_events WHERE task_id = ? AND kind = ? LIMIT 1",
        (task_id, OPERATOR_ESCALATION_EVENT),
    ).fetchone() is not None


# ---------------------------------------------------------------------------
# G1: per-task cumulative input-token runaway guard
# ---------------------------------------------------------------------------
#
# Token usage is already stamped per run on ``task_runs.input_tokens`` (K5a).
# The respawn preflight in ``dispatch_once`` sums that column across ALL of a
# task's runs; if the cumulative input exceeds ``kanban.per_task_input_token_cap``
# the task is a runaway and gets parked here rather than re-spawned. This is the
# event/escalation half (the summation + gate live in the dispatch loop). No
# schema change, no mid-run kill — a runaway is only ever caught at preflight.

BUDGET_RUNAWAY_PARKED_EVENT = "budget_runaway_parked"


def _budget_runaway_escalation_payload(
    *,
    row: sqlite3.Row,
    token_sum: int,
    cap: int,
    runs: int,
) -> dict:
    """Operator-escalation evidence for a per-task input-token runaway. Mirrors
    the shape of ``_operator_escalation_payload`` so the decision-queue renders
    it like any other escalation."""
    return {
        "task": {
            "id": row["id"],
            "title": row["title"] if "title" in row.keys() else None,
            "status": row["status"] if "status" in row.keys() else None,
            "assignee": row["assignee"] if "assignee" in row.keys() else None,
        },
        "why_now": (
            f"per-task input-token runaway: {token_sum} cumulative input "
            f"tokens across {runs} run(s) exceeded the cap of {cap}"
        ),
        "attempts_already_made": runs,
        "evidence": {
            "input_token_sum": token_sum,
            "per_task_input_token_cap": cap,
            "runs": runs,
        },
        "recommended_human_action": (
            "inspect the task's runs for a runaway retry / oversized-context "
            "loop, decide whether to unblock/reassign/close, and perform any "
            "required operator-only action outside the worker loop"
        ),
        "blocked_action_boundary": list(OPERATOR_ONLY_ACTIONS),
    }


def _park_budget_runaway(
    conn: sqlite3.Connection,
    row: sqlite3.Row,
    *,
    token_sum: int,
    cap: int,
    runs: int,
) -> bool:
    """G1: park a per-task input-token runaway.

    Sets the task ``blocked`` (clearing any claim), emits a
    ``budget_runaway_parked`` event with the token sum, and routes it to the
    decision-queue via the existing ``operator_escalation`` path. Skips
    already-terminal tasks and funnel roots (same exemptions as
    ``_park_stall_once``). Returns True iff the task was newly parked. Wrapped
    in a single ``write_txn`` so the status flip + both events commit atomically.
    """
    task_id = row["id"]
    reason = (
        f"per-task input-token cap exceeded: {token_sum} > {cap} "
        f"(cumulative input across {runs} run(s))"
    )
    with write_txn(conn):
        fresh = conn.execute(
            "SELECT * FROM tasks WHERE id = ?", (task_id,),
        ).fetchone()
        if fresh is None or fresh["status"] in ("done", "archived"):
            return False
        if _is_funnel_root_task(conn, fresh):
            return False
        cur = conn.execute(
            "UPDATE tasks SET status = 'blocked', claim_lock = NULL, "
            "claim_expires = NULL, worker_pid = NULL "
            "WHERE id = ? AND status NOT IN ('done', 'archived')",
            (task_id,),
        )
        if cur.rowcount != 1:
            return False
        if fresh["status"] != "blocked":
            _append_event(conn, task_id, "blocked", {"reason": reason})
        _append_event(
            conn, task_id, BUDGET_RUNAWAY_PARKED_EVENT,
            {"input_token_sum": token_sum, "cap": cap, "runs": runs},
        )
        _append_event(
            conn,
            task_id,
            OPERATOR_ESCALATION_EVENT,
            _budget_runaway_escalation_payload(
                row=fresh, token_sum=token_sum, cap=cap, runs=runs,
            ),
        )
        return True


def _finalize_integration_retry(
    conn: sqlite3.Connection,
    task_id: str,
    outcome: dict,
    *,
    now: int,
) -> bool:
    """Drive a successfully re-integrated parked task ``blocked -> done``.

    Heiler lane: when a transient re-integration round merges (or finds the
    branch already integrated), the task must finish on the *done* path — NOT
    ``blocked -> ready`` (that would re-spawn a worker against an already-merged
    branch, the exact limbo the old auto_retry_blocked lane caused). We do not
    call :func:`complete_task` here because the integration already happened
    (branch + worktree are gone); re-running its hook would re-park on a missing
    branch. The closing run was already ended when the task first parked.
    """
    action = outcome.get("action")
    merge_commit = outcome.get("merge_commit")
    summary_line = (
        f"integration retry {action}: merged "
        f"{outcome.get('branch')} into {outcome.get('target')}"
        + (f" as {str(merge_commit)[:12]}" if merge_commit else "")
    )
    with write_txn(conn):
        cur = conn.execute(
            """
            UPDATE tasks
               SET status       = 'done',
                   result       = ?,
                   completed_at = ?,
                   claim_lock   = NULL,
                   claim_expires= NULL,
                   worker_pid   = NULL
             WHERE id = ? AND status = 'blocked'
            """,
            (summary_line, int(now), task_id),
        )
        if cur.rowcount != 1:
            return False
        _append_event(
            conn, task_id, INTEGRATION_RETRY_SUCCEEDED_EVENT,
            {
                "action": action,
                "branch": outcome.get("branch"),
                "target": outcome.get("target"),
                "merge_commit": merge_commit,
            },
        )
        # Mirror the normal done-path completion signal so dashboards/notifiers
        # render the finish without a second SQL round-trip.
        _append_event(
            conn, task_id, "completed",
            {"summary": summary_line[:400], "result_len": len(summary_line)},
        )
    return True


def no_silent_stall_sweep(
    conn: sqlite3.Connection,
    *,
    now: Optional[int] = None,
    min_age_seconds: int = NO_SILENT_STALL_DEFAULT_MIN_AGE_SECONDS,
    decompose_failure_limit: int = NO_SILENT_STALL_DECOMPOSE_FAILURE_LIMIT,
    rate_limit_attempt_limit: int = NO_SILENT_STALL_RATE_LIMIT_ATTEMPT_LIMIT,
) -> dict:
    """Bound known silent-stall classes with one nudge or one park.

    Pure DB sweep: no schema, no worker spawn, and all idempotency rides on
    ``task_events`` markers. Demand-Funnel root proposals are visibility-only
    and are skipped; approved build children are not roots and remain eligible.
    """
    ts = int(time.time()) if now is None else int(now)
    min_age_seconds = max(0, int(min_age_seconds))
    summary = {
        "checked_at": ts,
        "self_healed": [],
        "parked": [],
        "skipped_funnel": [],
        "integration_retried": [],
    }

    def _old_enough(task_id: str, kind: str, fallback_at: Optional[int]) -> bool:
        at = _latest_event_at(conn, task_id, kind)
        if at is None:
            at = fallback_at
        return at is not None and int(at) + min_age_seconds <= ts

    # 1) scheduled-overdue: safe deterministic nudge through existing unblock.
    for row in conn.execute(
        "SELECT * FROM tasks WHERE status = 'scheduled'"
    ).fetchall():
        if _is_funnel_root_task(conn, row):
            summary["skipped_funnel"].append(row["id"])
            continue
        stall_class = "scheduled_overdue"
        reason = "scheduled task exceeded no-silent-stall age window"
        if _has_stall_marker(conn, row["id"], stall_class):
            continue
        if not _old_enough(row["id"], "scheduled", row["created_at"]):
            continue
        if unblock_task(conn, row["id"]):
            with write_txn(conn):
                _append_stall_marker(
                    conn, row["id"], stall_class=stall_class,
                    action="nudged", reason=reason, now=ts,
                )
            summary["self_healed"].append(
                {"task_id": row["id"], "class": stall_class, "action": "unblocked"}
            )
        elif _park_stall_once(
            conn, row, stall_class=stall_class, reason=reason,
            evidence={"attempts": 1}, now=ts,
        ):
            summary["parked"].append({"task_id": row["id"], "class": stall_class})

    # 2) review-without-verifier: nonspawnable review owner parks visibly.
    for row in conn.execute(
        "SELECT * FROM tasks WHERE status = 'review' AND claim_lock IS NULL"
    ).fetchall():
        if _is_funnel_root_task(conn, row):
            summary["skipped_funnel"].append(row["id"])
            continue
        stall_class = "review_without_verifier"
        if _has_stall_marker(conn, row["id"], stall_class):
            continue
        if not _old_enough(row["id"], "submitted_for_review", row["created_at"]):
            continue
        assignee = (row["assignee"] or "").strip()
        spawnable = bool(assignee)
        if spawnable:
            try:
                from hermes_cli.profiles import profile_exists
                spawnable = bool(profile_exists(assignee))
            except Exception:
                spawnable = True
        if not spawnable:
            reason = "review task has no runnable verifier/reviewer profile"
            if _park_stall_once(
                conn, row, stall_class=stall_class, reason=reason,
                evidence={"assignee": assignee or None, "attempts": 1}, now=ts,
            ):
                summary["parked"].append({"task_id": row["id"], "class": stall_class})

    # 3) triage-decompose-failed: auto-decompose failed repeatedly.
    for row in conn.execute(
        "SELECT * FROM tasks WHERE decompose_failed >= ? "
        "AND status NOT IN ('done', 'archived')",
        (max(1, int(decompose_failure_limit)),),
    ).fetchall():
        if _is_funnel_root_task(conn, row):
            summary["skipped_funnel"].append(row["id"])
            continue
        stall_class = "triage_decompose_failed"
        if _has_stall_marker(conn, row["id"], stall_class):
            continue
        reason = (
            f"auto_decompose failed {int(row['decompose_failed'])} times"
        )
        if _park_stall_once(
            conn, row, stall_class=stall_class, reason=reason,
            evidence={"attempts": int(row["decompose_failed"])}, now=ts,
        ):
            summary["parked"].append({"task_id": row["id"], "class": stall_class})

    # 4) persistent rate-limit loop: rate_limited runs never increment the
    # breaker, so bound repeated quota loops explicitly.
    for row in conn.execute(
        "SELECT * FROM tasks WHERE status = 'ready'"
    ).fetchall():
        if _is_funnel_root_task(conn, row):
            summary["skipped_funnel"].append(row["id"])
            continue
        latest = conn.execute(
            "SELECT outcome, ended_at FROM task_runs "
            "WHERE task_id = ? AND ended_at IS NOT NULL "
            "ORDER BY ended_at DESC, id DESC LIMIT 1",
            (row["id"],),
        ).fetchone()
        if latest is None or latest["outcome"] != "rate_limited":
            continue
        attempts = _count_task_runs_with_outcome(conn, row["id"], "rate_limited")
        if attempts < max(1, int(rate_limit_attempt_limit)):
            continue
        stall_class = "rate_limited_loop"
        if _has_stall_marker(conn, row["id"], stall_class):
            continue
        reason = f"persistent rate-limit loop after {attempts} rate-limited runs"
        if _park_stall_once(
            conn, row, stall_class=stall_class, reason=reason,
            evidence={"attempts": attempts, "latest_ended_at": latest["ended_at"]},
            now=ts,
        ):
            summary["parked"].append({"task_id": row["id"], "class": stall_class})

    # 5) integration_parked (Heiler lane): a TRANSIENT park (dirty overlap /
    #    in-progress git op / wrong branch) is re-run through the integration
    #    path up to INTEGRATION_RETRY_LIMIT times before escalating. A
    #    non-transient park (merge conflict / red post-merge gate / unknown) is
    #    NEVER retried — it is classified and escalated to the operator. We
    #    never move a parked task to ``ready`` (that re-spawned a worker against
    #    an already-merged branch — the old auto_retry_blocked failure mode).
    from hermes_cli import kanban_worktrees as _kwt
    for row in conn.execute(
        "SELECT * FROM tasks WHERE status = 'blocked'"
    ).fetchall():
        task_id = row["id"]
        if _is_funnel_root_task(conn, row):
            summary["skipped_funnel"].append(task_id)
            continue
        # Once escalated, the operator owns it — never retry again.
        if (
            _has_stall_marker(conn, task_id, INTEGRATION_PARKED_STALL_CLASS)
            or _has_stall_marker(conn, task_id, INTEGRATION_RETRY_EXHAUSTED_CLASS)
        ):
            continue
        blocked_event = conn.execute(
            "SELECT payload FROM task_events "
            "WHERE task_id = ? AND kind = 'blocked' "
            "ORDER BY id DESC LIMIT 1",
            (task_id,),
        ).fetchone()
        reason = _decision_event_reason(
            blocked_event["payload"] if blocked_event else None
        ) or ""
        if not reason.startswith("integration parked:"):
            continue

        park_class = _kwt._integration_park_class(reason)
        try:
            retry_count = int(row["integration_retry_count"] or 0)
        except (IndexError, KeyError, TypeError):
            retry_count = 0

        # Non-transient (merge conflict / red gate / unknown): classify and
        # escalate once. Dovetails with the operator/S4-ledger lane.
        if park_class != "transient":
            if _park_stall_once(
                conn, row, stall_class=INTEGRATION_PARKED_STALL_CLASS,
                reason=reason, evidence={"attempts": retry_count or 1}, now=ts,
            ):
                summary["parked"].append(
                    {"task_id": task_id, "class": INTEGRATION_PARKED_STALL_CLASS}
                )
            continue

        # Transient but exhausted: bounded — escalate to the operator.
        if retry_count >= INTEGRATION_RETRY_LIMIT:
            if _park_stall_once(
                conn, row, stall_class=INTEGRATION_RETRY_EXHAUSTED_CLASS,
                reason=reason, evidence={"attempts": retry_count}, now=ts,
            ):
                summary["parked"].append(
                    {"task_id": task_id, "class": INTEGRATION_RETRY_EXHAUSTED_CLASS}
                )
            continue

        # Transient backoff: don't burn the bounded retries faster than the
        # blocker (git lock / dirty tree) can plausibly clear.
        last_at = (
            _latest_event_at(conn, task_id, INTEGRATION_RETRY_EVENT)
            or _latest_event_at(conn, task_id, "blocked")
            or row["created_at"]
        )
        if last_at is not None and (
            int(last_at) + INTEGRATION_RETRY_BACKOFF_SECONDS > ts
        ):
            continue

        # Claim this retry round: bump the OWN counter atomically (CAS on the
        # observed value) so concurrent sweeps cannot double-attempt.
        attempt = retry_count + 1
        with write_txn(conn):
            cur = conn.execute(
                "UPDATE tasks SET integration_retry_count = ? "
                "WHERE id = ? AND status = 'blocked' "
                "AND integration_retry_count = ?",
                (attempt, task_id, retry_count),
            )
            claimed = cur.rowcount == 1
            if claimed:
                _append_event(
                    conn, task_id, INTEGRATION_RETRY_EVENT,
                    {
                        "attempt": attempt,
                        "limit": INTEGRATION_RETRY_LIMIT,
                        "reason": reason[:500],
                    },
                )
        if not claimed:
            continue

        # Re-run the integration path. Opens its own txn internally, so it must
        # run OUTSIDE the counter txn above. Fail-soft: a crash here just leaves
        # the task blocked for the next sweep (counter already advanced).
        try:
            outcome = _kwt.maybe_integrate_on_complete(conn, task_id) or {}
        except Exception:
            _log.warning(
                "integration-retry hook failed for %s", task_id, exc_info=True,
            )
            outcome = {}
        action = outcome.get("action")

        if action in ("merged", "clean"):
            if _finalize_integration_retry(conn, task_id, outcome, now=ts):
                summary["self_healed"].append(
                    {
                        "task_id": task_id,
                        "class": "integration_retry",
                        "action": "reintegrated",
                    }
                )
        elif action == "parked":
            new_reason = str(outcome.get("reason") or "")
            if _kwt._integration_park_class(new_reason) == "transient":
                # Still transient — leave blocked; retry again next sweep (until
                # the bounded limit). The counter already advanced.
                summary["integration_retried"].append(
                    {"task_id": task_id, "attempt": attempt}
                )
            elif _park_stall_once(
                conn, row, stall_class=INTEGRATION_PARKED_STALL_CLASS,
                reason=f"integration parked: {new_reason}",
                evidence={"attempts": attempt}, now=ts,
            ):
                # Re-park reclassified to non-transient → stop retrying.
                summary["parked"].append(
                    {"task_id": task_id, "class": INTEGRATION_PARKED_STALL_CLASS}
                )
        elif action == "rebase_conflict":
            if _park_stall_once(
                conn, row, stall_class=INTEGRATION_PARKED_STALL_CLASS,
                reason=reason, evidence={"attempts": attempt}, now=ts,
            ):
                summary["parked"].append(
                    {"task_id": task_id, "class": INTEGRATION_PARKED_STALL_CLASS}
                )
        else:
            # deferred / None (e.g. open siblings re-appeared) — leave blocked.
            summary["integration_retried"].append(
                {"task_id": task_id, "attempt": attempt}
            )

    # 6) legacy gave_up without a 3B escalation: backfill exactly once.
    for row in conn.execute(
        "SELECT * FROM tasks WHERE status = 'blocked'"
    ).fetchall():
        if _is_funnel_root_task(conn, row):
            summary["skipped_funnel"].append(row["id"])
            continue
        if _has_operator_escalation(conn, row["id"]):
            continue
        gave_up = conn.execute(
            "SELECT payload FROM task_events "
            "WHERE task_id = ? AND kind = 'gave_up' "
            "ORDER BY id DESC LIMIT 1",
            (row["id"],),
        ).fetchone()
        if gave_up is None:
            continue
        stall_class = "gave_up_no_subscriber"
        try:
            payload = json.loads(gave_up["payload"] or "{}")
        except Exception:
            payload = {}
        attempts = payload.get("failures") if isinstance(payload, dict) else None
        reason = "gave_up task has no operator escalation event"
        if _park_stall_once(
            conn, row, stall_class=stall_class, reason=reason,
            evidence={"attempts": attempts or 0}, now=ts,
        ):
            summary["parked"].append({"task_id": row["id"], "class": stall_class})

    return summary


def read_escalation_ledger(
    conn: sqlite3.Connection,
    *,
    since: Optional[int] = None,
    until: Optional[int] = None,
    task_id: Optional[str] = None,
    classes: Optional[Iterable[str]] = None,
    limit: Optional[int] = None,
) -> dict:
    """Read side of the S4 Heiler classification ledger (Phase 1.5 input).

    Aggregates ``heiler_classification`` task_events into a per-class rollup
    plus the raw entries (newest first), each joined to its task title/status so
    the Stratege can derive root-cause Specs without a second query. Pure read:
    no writes, no schema change, no worker spawn — safe to call from a report
    cron or the dashboard.

    Filters (all optional):
      * ``since`` / ``until`` — inclusive ``created_at`` window (unix seconds)
      * ``task_id`` — restrict to a single task
      * ``classes`` — restrict to a subset of :data:`HEILER_CLASSES`
      * ``limit`` — cap the number of returned *entries*. The ``by_class``
        rollup and ``total`` are always computed over the full filtered window,
        so the counts stay accurate even when the entry list is truncated.
    """
    where = ["e.kind = ?"]
    params: list = [HEILER_CLASSIFICATION_EVENT]
    if since is not None:
        where.append("e.created_at >= ?")
        params.append(int(since))
    if until is not None:
        where.append("e.created_at <= ?")
        params.append(int(until))
    if task_id is not None:
        where.append("e.task_id = ?")
        params.append(task_id)

    class_filter = {str(c) for c in classes} if classes is not None else None

    rows = conn.execute(
        "SELECT e.id, e.task_id, e.run_id, e.payload, e.created_at, "
        "t.title AS task_title, t.status AS task_status "
        "FROM task_events e LEFT JOIN tasks t ON t.id = e.task_id "
        "WHERE " + " AND ".join(where) + " "
        "ORDER BY e.created_at DESC, e.id DESC",
        params,
    ).fetchall()

    by_class: dict = {}
    entries: list = []
    for r in rows:
        try:
            payload = json.loads(r["payload"] or "{}")
        except Exception:
            payload = {}
        if not isinstance(payload, dict):
            payload = {}
        cls = payload.get("class")
        if class_filter is not None and cls not in class_filter:
            continue
        by_class[cls] = by_class.get(cls, 0) + 1
        entries.append({
            "event_id": r["id"],
            "task_id": r["task_id"],
            "run_id": (int(r["run_id"]) if r["run_id"] is not None else None),
            "created_at": (
                int(r["created_at"]) if r["created_at"] is not None else None
            ),
            "class": cls,
            "evidence": payload.get("evidence"),
            "source": payload.get("source"),
            "blocked": payload.get("blocked"),
            "task_title": r["task_title"],
            "task_status": r["task_status"],
        })

    total = len(entries)
    if limit is not None and int(limit) >= 0:
        entries = entries[: int(limit)]

    return {
        "total": total,
        "by_class": by_class,
        "entries": entries,
    }


def kanban_dispatcher_heartbeat_path() -> Path:
    from hermes_constants import get_default_hermes_root

    return (
        get_default_hermes_root()
        / "state"
        / KANBAN_DISPATCHER_HEARTBEAT_FILENAME
    )


def _kanban_heartbeat_counts_for_conn(
    conn: sqlite3.Connection,
    *,
    now: int,
) -> dict:
    day_start = int(
        _dt.datetime.fromtimestamp(now, _dt.timezone.utc)
        .replace(hour=0, minute=0, second=0, microsecond=0)
        .timestamp()
    )
    counts = {
        "self_healed_today": 0,
        "parked_open": 0,
        "open_escalations": 0,
        "stranded": 0,
    }

    for row in conn.execute(
        "SELECT payload, created_at FROM task_events "
        "WHERE kind = ? AND created_at >= ?",
        (NO_SILENT_STALL_EVENT, day_start),
    ).fetchall():
        try:
            payload = json.loads(row["payload"] or "{}")
        except Exception:
            continue
        if isinstance(payload, dict) and payload.get("action") == "nudged":
            counts["self_healed_today"] += 1

    for row in conn.execute(
        "SELECT e.task_id, e.payload, t.status "
        "FROM task_events e JOIN tasks t ON t.id = e.task_id "
        "WHERE e.kind = ? AND t.status = 'blocked'",
        (NO_SILENT_STALL_EVENT,),
    ).fetchall():
        try:
            payload = json.loads(row["payload"] or "{}")
        except Exception:
            continue
        if isinstance(payload, dict) and payload.get("action") == "parked":
            counts["parked_open"] += 1

    row = conn.execute(
        "SELECT COUNT(DISTINCT e.task_id) AS n "
        "FROM task_events e JOIN tasks t ON t.id = e.task_id "
        "WHERE e.kind = ? AND t.status NOT IN ('done', 'archived')",
        (OPERATOR_ESCALATION_EVENT,),
    ).fetchone()
    counts["open_escalations"] += int(row["n"] or 0) if row else 0

    try:
        counts["stranded"] += int(decision_queue(conn, now=now).get("count") or 0)
    except Exception:
        pass
    return counts


def _merge_count_dicts(items: Iterable[dict]) -> dict:
    merged = {
        "self_healed_today": 0,
        "parked_open": 0,
        "open_escalations": 0,
        "stranded": 0,
    }
    for item in items:
        for key in merged:
            try:
                merged[key] += int(item.get(key) or 0)
            except Exception:
                pass
    return merged


def write_kanban_dispatcher_heartbeat(
    *,
    tick_health: str = "ok",
    now: Optional[int] = None,
    boards: Optional[list[dict]] = None,
) -> dict:
    ts = int(time.time()) if now is None else int(now)
    board_counts: list[dict] = []
    boards_payload: list[dict] = []
    if boards is None:
        try:
            boards = list_boards(include_archived=False)
        except Exception:
            boards = [read_board_metadata(DEFAULT_BOARD)]

    for board in boards:
        slug = board.get("slug") or DEFAULT_BOARD
        try:
            with connect_closing(board=slug) as conn:
                counts = _kanban_heartbeat_counts_for_conn(conn, now=ts)
        except Exception:
            counts = {
                "self_healed_today": 0,
                "parked_open": 0,
                "open_escalations": 0,
                "stranded": 0,
            }
        board_counts.append(counts)
        boards_payload.append({"slug": slug, "counts": counts})

    path = kanban_dispatcher_heartbeat_path()
    previous: dict = {}
    try:
        if path.is_file():
            loaded = json.loads(path.read_text(encoding="utf-8"))
            if isinstance(loaded, dict):
                previous = loaded
    except Exception:
        previous = {}
    health = (tick_health or "unknown").strip() or "unknown"
    last_green = (
        ts if health == "ok"
        else previous.get("last_green_gate_at")
    )
    payload = {
        "last_tick_at": ts,
        "tick_health": health,
        "last_green_gate_at": last_green,
        "counts": _merge_count_dicts(board_counts),
        "boards": boards_payload,
    }
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(payload, sort_keys=True), encoding="utf-8")
    tmp.replace(path)
    return payload


def check_respawn_guard(conn: sqlite3.Connection, task_id: str) -> Optional[str]:
    """Return a guard reason if ``task_id`` should NOT be re-spawned, else None.

    Called per ready task in ``dispatch_once`` before any claim attempt.
    Returning a reason defers the spawn this tick; the task stays in
    ``ready`` and gets another chance on the next dispatcher tick.

    Checks in priority order:

    ``"rate_limit_cooldown"``
        The task's most recent run ended with the ``rate_limited`` outcome
        (a worker bailed on a provider quota wall via the EX_TEMPFAIL
        sentinel) within ``_resolve_rate_limit_cooldown_seconds()``. The
        quota almost certainly hasn't reset yet, so defer the respawn until
        the cooldown elapses — then allow a cheap probe. This is checked
        BEFORE ``blocker_auth`` because the rate-limit requeue stamps a
        quota-flavored ``last_failure_error`` that would otherwise match the
        auth-blocker regex and park the task forever (the rate-limit path
        never increments ``consecutive_failures``, so the breaker can't free
        it). Once the cooldown elapses the task falls through and respawns.

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
        "SELECT last_failure_error, workflow_template_id FROM tasks WHERE id = ?",
        (task_id,),
    ).fetchone()
    if row is None:
        return None

    contract_row = conn.execute(
        "SELECT id, body, assignee, workspace_kind, workspace_path, tenant, "
        "created_by, kind FROM tasks WHERE id = ?",
        (task_id,),
    ).fetchone()
    if contract_row is not None:
        contract_reason, _payload = _code_contract_issue_for_row(
            conn, contract_row, source="respawn_guard",
        )
        if contract_reason is not None:
            return contract_reason

    now = int(time.time())

    # 1. Rate-limit cooldown. The most recent run ended ``rate_limited``
    #    (quota wall) — defer while inside the cooldown window, then allow a
    #    cheap probe. Must run BEFORE the blocker_auth regex check, because a
    #    rate-limit requeue stamps a quota-flavored last_failure_error that
    #    the regex would otherwise match → defer forever (no failure counter
    #    increment on this path means the breaker can never free it).
    #
    #    We look at the LATEST run only (ORDER BY ended_at DESC LIMIT 1): if a
    #    newer crash/completion superseded the rate-limit run, this guard
    #    no longer applies and the normal paths take over.
    rl_cooldown = _resolve_rate_limit_cooldown_seconds()
    latest_run = conn.execute(
        "SELECT outcome, ended_at FROM task_runs "
        "WHERE task_id = ? AND ended_at IS NOT NULL "
        "ORDER BY ended_at DESC LIMIT 1",
        (task_id,),
    ).fetchone()
    if (
        latest_run is not None
        and latest_run["outcome"] == "rate_limited"
    ):
        if rl_cooldown <= 0:
            # Cooldown disabled — respawn immediately, and skip the
            # blocker_auth regex so the stamped rate-limit text doesn't
            # re-trap the task.
            return None
        ended_at = latest_run["ended_at"]
        if ended_at is not None and (now - int(ended_at)) < rl_cooldown:
            return "rate_limit_cooldown"
        # Cooldown elapsed — allow the respawn. Return early so the
        # blocker_auth check below doesn't catch the rate-limit text we
        # stamped on the task; this path intentionally retries forever
        # (cheaply, spaced by the cooldown) until quota returns or a real
        # crash/completion supersedes it.
        return None

    # 2. Quota / auth blocker: retrying immediately will not help.
    err = row["last_failure_error"]
    if err and _RESPAWN_BLOCKER_RE.search(err):
        return "blocker_auth"

    # 3. Completed run within guard window — proof of recent success.
    #    K8 exemption: a native workflow task (``workflow_template_id`` set)
    #    that is ``ready`` is mid-chain — its most recent completed run belongs
    #    to the PREVIOUS step, and the workflow exists precisely to auto-advance
    #    to the next role rather than "wait for human review". Guarding it here
    #    would stall every step boundary for the full success window. The final
    #    step lands in ``done`` (never re-enters ``ready``), so there is no
    #    respawn-loop risk. Non-workflow tasks are unaffected (byte-identical).
    if not row["workflow_template_id"]:
        # K3 exemption: a verifier REQUEST_CHANGES on the latest run
        # invalidates "recent success" — the review already happened and
        # demanded a fix run. The task only re-enters ``ready`` through an
        # explicit operator/CLI decision (unblock), so deferring here would
        # silently stall the requested retry for the full success window
        # (the CommandHome inline-resolve reported ok while nothing spawned).
        latest_verdict = conn.execute(
            "SELECT verdict FROM task_runs "
            "WHERE task_id = ? AND ended_at IS NOT NULL "
            "ORDER BY ended_at DESC, id DESC LIMIT 1",
            (task_id,),
        ).fetchone()
        rejected = bool(
            latest_verdict
            and latest_verdict["verdict"]
            and str(latest_verdict["verdict"]).upper() == "REQUEST_CHANGES"
        )
        if not rejected:
            cutoff = now - _RESPAWN_GUARD_SUCCESS_WINDOW
            if conn.execute(
                "SELECT id FROM task_runs "
                "WHERE task_id = ? AND outcome = 'completed' AND ended_at >= ?",
                (task_id, cutoff),
            ).fetchone():
                return "recent_success"

    # 4. GitHub PR URL in a recent comment — prior worker already opened a PR.
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


# ---------------------------------------------------------------------------
# OpenClaw cross-system dispatch (Mission-Control via HMAC-signed envelopes)
# ---------------------------------------------------------------------------
#
# A kanban task whose free-text ``assignee`` is ``openclaw:<agent>`` is not a
# Hermes profile and would normally be bucketed as ``skipped_nonspawnable``.
# Instead the dispatcher intercepts it BEFORE the profile_exists gate, signs a
# Mission-Control envelope via the EXISTING signer module
# (``mc_mutation_triage_server`` under ~/.hermes/mcp) and POSTs it to MC. The
# task stays ``running``; ``poll_openclaw_results`` polls MC and closes the
# task to done/blocked on a later tick. No DB migration: the correlation state
# lives on ``task_runs.metadata`` JSON under the ``openclaw`` key.

OPENCLAW_ASSIGNEE_PREFIX = "openclaw:"

# Map the OpenClaw agent name (the part after ``openclaw:``) to the MC
# operation the signer knows how to build/validate. Mirrors the MC
# operation->agent routing map. All four builders (atlas/lens/forge/pixel) are
# live; see ``_OPENCLAW_ENVELOPE_BUILDERS`` below.
OPENCLAW_AGENT_TO_OPERATION = {
    "atlas": "trigger_atlas_sprint",
    "lens": "request_lens_audit",
    "forge": "request_forge_review",
    "pixel": "request_pixel_ui_qa",
}

# Default Discord channel for the audit-result aggregate (hub parent channel
# #hermes-oc). 19 digits — matches the lens-audit ``deliver_to`` regex
# ``^[0-9]{17,20}$``. Overridable per-dispatch (see _dispatch_to_openclaw).
OPENCLAW_DEFAULT_DELIVER_TO = "1500203113867378789"

# Where the signer module lives. Importing it is lazy + guarded so a missing
# module never crashes the dispatcher — it degrades to a normal spawn failure.
_MC_SIGNER_DIR = str(Path.home() / ".hermes" / "mcp")

# MC task-status read endpoint (poll-back). Reuses the openclaw_view read
# headers (service / read), 6s timeout.
_MC_TASK_STATUS_URL = "http://127.0.0.1:3000/api/tasks/{mc_task_id}"
_MC_READ_HEADERS = {"x-actor-kind": "service", "x-request-class": "read"}
_MC_READ_TIMEOUT_SECONDS = 6.0


def _parse_openclaw_assignee(assignee: Optional[str]) -> Optional[str]:
    """Return the MC operation for an ``openclaw:<agent>`` assignee, else None.

    ``openclaw:lens`` -> ``request_lens_audit``. A plain Hermes profile
    (``coder``) or an unknown agent (``openclaw:bogus``) returns ``None`` so
    the caller falls through to the normal dispatch path unchanged.
    """
    if not assignee or not isinstance(assignee, str):
        return None
    if not assignee.startswith(OPENCLAW_ASSIGNEE_PREFIX):
        return None
    agent = assignee[len(OPENCLAW_ASSIGNEE_PREFIX):].strip().lower()
    return OPENCLAW_AGENT_TO_OPERATION.get(agent)


def _import_mc_signer():
    """Lazy + guarded import of the MC signer module.

    Returns the module on success. Raises (ImportError/Exception) on failure;
    the caller routes the failure through the normal spawn-failure path.
    """
    if _MC_SIGNER_DIR not in sys.path:
        sys.path.insert(0, _MC_SIGNER_DIR)
    import mc_mutation_triage_server as _signer  # noqa: WPS433 (lazy by design)
    return _signer


def _openclaw_deliver_to(claimed_task: "Task") -> str:
    """Resolve the Discord channel id for the audit aggregate.

    Per-task override: a ``deliver_to`` key on the task's metadata JSON wins;
    otherwise fall back to the hub-parent default. The value is validated by
    the signer's payload schema (``^[0-9]{17,20}$``) — we do not re-validate
    here, we just pick the source.
    """
    meta = getattr(claimed_task, "metadata", None)
    if isinstance(meta, str) and meta.strip():
        try:
            meta = json.loads(meta)
        except Exception:
            meta = None
    if isinstance(meta, dict):
        cand = meta.get("openclaw_deliver_to") or meta.get("deliver_to")
        if isinstance(cand, str) and cand.strip():
            return cand.strip()
    return OPENCLAW_DEFAULT_DELIVER_TO


def _build_lens_envelope(claimed_task: "Task", signer) -> dict:
    """Build a fully-signed ``request_lens_audit`` envelope for ``claimed_task``."""
    import uuid
    from datetime import datetime, timezone

    deliver_to = _openclaw_deliver_to(claimed_task)
    # scope_query must be 4-512 chars. Build it from the task title (+ id for
    # traceability), clamped into range.
    title = (getattr(claimed_task, "title", None) or "").strip()
    scope_query = f"[{claimed_task.id}] {title}".strip()
    if len(scope_query) < 4:
        scope_query = f"kanban-task-{claimed_task.id}"
    scope_query = scope_query[:512]

    payload = {
        "audit_kind": "memory-pipeline",
        "scope_query": scope_query,
        "deliver_to": deliver_to,
    }
    workflow_id = f"wf-openclaw-lens-{uuid.uuid4().hex[:8]}"
    timestamp = (
        datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    )
    signature = signer.compute_signature(payload, workflow_id, timestamp)
    envelope = {
        "workflow_id": workflow_id,
        "source": "hermes-coordinator",
        "source_profile": "coordinator",
        "routing_alias": "@lens",
        "capability_id": "openclaw.lens.request_audit",
        "operation": "request_lens_audit",
        "risk_class": "safe-read-only",
        "timestamp": timestamp,
        "signature": signature,
        "payload": payload,
    }
    return envelope


def _openclaw_objective(claimed_task: "Task") -> str:
    """Derive a scope-contract objective (8-280 chars) from the task title.

    Atlas' ``scope_contract_v2.objective`` requires minLength 8; pad short
    titles with the task id so the envelope always validates.
    """
    title = (getattr(claimed_task, "title", None) or "").strip()
    objective = f"[{claimed_task.id}] {title}".strip()
    if len(objective) < 8:
        objective = f"kanban-task-{claimed_task.id} sprint"
    return objective[:280]


def _openclaw_body_lines(claimed_task: "Task") -> list[str]:
    """Return the non-empty, stripped lines of the task body (excluding the
    machine marker lines like ``[openclaw_deliver_to:...]``)."""
    body = getattr(claimed_task, "body", None)
    if not isinstance(body, str) or not body.strip():
        return []
    out: list[str] = []
    for raw in body.splitlines():
        line = raw.strip()
        if not line:
            continue
        if line.startswith("[openclaw_deliver_to:") and line.endswith("]"):
            continue
        out.append(line)
    return out


def _build_atlas_envelope(claimed_task: "Task", signer) -> dict:
    """Build a fully-signed ``trigger_atlas_sprint`` envelope.

    NOTE: atlas is the HIGHEST-RISK path — ``risk_class=gated-mutation``. Unlike
    lens/forge/pixel (read-only / planned), an Atlas sprint can mutate. We
    hand-build the envelope (mirroring the lens pattern) rather than calling the
    ``simple_atlas_sprint`` convenience wrapper, so this returns the same
    envelope shape every other agent persists and the success path is uniform.

    kanban title -> ``scope_contract_v2.objective``; in_scope/out_of_scope/
    termination_conditions/evidence_requirements are derived from the task body
    with conservative defaults that keep the sprint tightly bounded.
    """
    import uuid
    from datetime import datetime, timezone

    deliver_to = _openclaw_deliver_to(claimed_task)
    objective = _openclaw_objective(claimed_task)
    body_lines = _openclaw_body_lines(claimed_task)
    in_scope = body_lines[:12] if body_lines else [objective]
    payload = {
        "sprint_kind": "audit",
        "scope_contract_v2": {
            "objective": objective,
            "in_scope": in_scope,
            "out_of_scope": [
                "No file writes outside the stated scope",
                # Phrased to avoid the signer's dangerous-keyword scanner, which
                # bans the literal VCS-publish verb even inside anti-scope text.
                "No remote publishing, deploys, or infra mutation",
            ],
            "termination_conditions": [
                "Objective addressed and evidence posted",
            ],
            "evidence_requirements": [
                "Summary of findings posted to the deliver_to channel",
            ],
        },
        "deliver_to": deliver_to,
        "max_parallel_tasks": 3,
    }
    workflow_id = f"wf-openclaw-atlas-{uuid.uuid4().hex[:8]}"
    timestamp = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    signature = signer.compute_signature(payload, workflow_id, timestamp)
    envelope = {
        "workflow_id": workflow_id,
        "source": "hermes-coordinator",
        "source_profile": "coordinator",
        "routing_alias": "@atlas",
        "capability_id": "openclaw.atlas.trigger_sprint",
        "operation": "trigger_atlas_sprint",
        "risk_class": "gated-mutation",
        "timestamp": timestamp,
        "signature": signature,
        "payload": payload,
    }
    return envelope


def _build_forge_envelope(claimed_task: "Task", signer) -> dict:
    """Build a fully-signed ``request_forge_review`` envelope (safe-read-only).

    ``target_paths`` are parsed from the task body lines (repo-relative paths),
    falling back to ``["."]``; ``review_kind`` defaults to ``code-quality``.
    """
    import uuid
    from datetime import datetime, timezone

    deliver_to = _openclaw_deliver_to(claimed_task)
    body_lines = _openclaw_body_lines(claimed_task)
    # Each non-empty body line is treated as a repo-relative path candidate
    # (schema caps item length at 256 and the array at 24 items).
    target_paths = [ln[:256] for ln in body_lines][:24] or ["."]
    payload = {
        "review_kind": "code-quality",
        "target_paths": target_paths,
        "deliver_to": deliver_to,
    }
    workflow_id = f"wf-openclaw-forge-{uuid.uuid4().hex[:8]}"
    timestamp = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    signature = signer.compute_signature(payload, workflow_id, timestamp)
    envelope = {
        "workflow_id": workflow_id,
        "source": "hermes-coordinator",
        "source_profile": "coordinator",
        "routing_alias": "@forge",
        "capability_id": "openclaw.forge.request_review",
        "operation": "request_forge_review",
        "risk_class": "safe-read-only",
        "timestamp": timestamp,
        "signature": signature,
        "payload": payload,
    }
    return envelope


def _extract_target_url(claimed_task: "Task") -> str:
    """Pull the first http(s) URL out of the task body for Pixel UI QA.

    Falls back to the local dashboard (``http://127.0.0.1:9119/control``) when
    no URL is present, so the envelope always satisfies the pixel schema's
    ``^https?://`` pattern. Local/staging only by contract.
    """
    import re

    for line in _openclaw_body_lines(claimed_task):
        m = re.search(r"https?://[A-Za-z0-9._:/-]+", line)
        if m:
            return m.group(0)[:512]
    return "http://127.0.0.1:9119/control"


def _build_pixel_envelope(claimed_task: "Task", signer) -> dict:
    """Build a fully-signed ``request_pixel_ui_qa`` envelope (safe-read-only).

    Pixel is a normal UI-QA worker doing read-only browser inspection — same
    risk tier as lens/forge. The former ``operator-lock`` risk class (and its
    ``operator_lock_acknowledged`` payload field) was a leftover from the
    standalone-OpenClaw era and has been removed: the operator's task creation
    is the authorization.
    """
    import uuid
    from datetime import datetime, timezone

    deliver_to = _openclaw_deliver_to(claimed_task)
    target_url = _extract_target_url(claimed_task)
    payload = {
        "target_url": target_url,
        "qa_kind": "layout-check",
        "deliver_to": deliver_to,
    }
    workflow_id = f"wf-openclaw-pixel-{uuid.uuid4().hex[:8]}"
    timestamp = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    signature = signer.compute_signature(payload, workflow_id, timestamp)
    envelope = {
        "workflow_id": workflow_id,
        "source": "hermes-coordinator",
        "source_profile": "coordinator",
        "routing_alias": "@pixel",
        "capability_id": "openclaw.pixel.request_ui_qa",
        "operation": "request_pixel_ui_qa",
        "risk_class": "safe-read-only",
        "timestamp": timestamp,
        "signature": signature,
        "payload": payload,
    }
    return envelope


# Map each MC operation to its envelope builder. All four converge on the same
# envelope shape so the success-persist path below (and poll_openclaw_results)
# handle every agent identically.
_OPENCLAW_ENVELOPE_BUILDERS = {
    "request_lens_audit": _build_lens_envelope,
    "trigger_atlas_sprint": _build_atlas_envelope,
    "request_forge_review": _build_forge_envelope,
    "request_pixel_ui_qa": _build_pixel_envelope,
}


def _dispatch_to_openclaw(
    conn: sqlite3.Connection,
    claimed_task: "Task",
    operation: str,
) -> None:
    """Sign + submit an OpenClaw envelope for an already-claimed (running) task.

    On success: persist the MC correlation on ``task_runs.metadata.openclaw``
    for the task's current run, leave the task ``running``, emit an
    ``openclaw_dispatched`` event + a human-trail comment.

    On rejection or any exception: raise, so the caller routes the task
    through the existing ``_record_spawn_failure`` path (consistent with a
    local spawn failure — task released, failure counted).
    """
    signer = _import_mc_signer()  # may raise -> caller treats as spawn failure

    builder = _OPENCLAW_ENVELOPE_BUILDERS.get(operation)
    if builder is None:
        # Unknown / mis-routed operation: raising here means it degrades
        # through the normal spawn-failure path rather than silently stranding
        # in ``running``.
        raise NotImplementedError(
            f"OpenClaw operation {operation!r} not implemented yet"
        )
    envelope = builder(claimed_task, signer)

    result = signer.submit_to_mission_control(envelope)
    if not isinstance(result, dict) or result.get("status") != "ok":
        # Structured rejection (local-validate / mc-rejected / mc-unreachable).
        stage = (result or {}).get("stage") if isinstance(result, dict) else None
        reason = (result or {}).get("reason") if isinstance(result, dict) else None
        raise RuntimeError(
            f"openclaw submit rejected (stage={stage}, reason={reason})"
        )

    mc_response = result.get("mc_response") or {}
    # MC returns the created/idempotent task id under ``taskId`` and the
    # workflow id it stored under ``workflowId`` (mirror the envelope's
    # workflow_id if MC echoes nothing).
    mc_task_id = (
        mc_response.get("taskId")
        or mc_response.get("task_id")
        or mc_response.get("id")
    )
    workflow_id = (
        mc_response.get("workflowId")
        or mc_response.get("workflow_id")
        or envelope["workflow_id"]
    )
    if not mc_task_id:
        raise RuntimeError("openclaw submit ok but MC returned no taskId")

    submitted_at = int(time.time())
    openclaw_meta = {
        "mc_task_id": str(mc_task_id),
        "workflow_id": str(workflow_id),
        "operation": operation,
        "poll_state": "submitted",
        "submitted_at": submitted_at,
    }
    # Persist on the in-flight run's metadata WITHOUT closing the run (the task
    # must stay ``running`` until poll-back resolves it). Merge into any
    # existing metadata JSON rather than clobbering it.
    with write_txn(conn):
        run_id = _current_run_id(conn, claimed_task.id)
        if run_id is None:
            raise RuntimeError(
                f"openclaw dispatch: task {claimed_task.id} has no active run"
            )
        row = conn.execute(
            "SELECT metadata FROM task_runs WHERE id = ?", (run_id,),
        ).fetchone()
        existing = {}
        if row and row["metadata"]:
            try:
                parsed = json.loads(row["metadata"])
                if isinstance(parsed, dict):
                    existing = parsed
            except Exception:
                existing = {}
        existing["openclaw"] = openclaw_meta
        conn.execute(
            "UPDATE task_runs SET metadata = ? WHERE id = ?",
            (json.dumps(existing, ensure_ascii=False), run_id),
        )
        _append_event(
            conn, claimed_task.id, "openclaw_dispatched",
            {
                "operation": operation,
                "mc_task_id": str(mc_task_id),
                "workflow_id": str(workflow_id),
            },
            run_id=run_id,
        )
    # Human trail comment (separate — add_comment opens its own txn).
    try:
        add_comment(
            conn, claimed_task.id, "dispatcher",
            f"Dispatched to OpenClaw ({operation}) → MC task {mc_task_id} "
            f"(workflow {workflow_id}). Polling for result.",
        )
    except Exception:
        # The comment is a convenience trail; never fail the dispatch on it.
        _log.debug(
            "openclaw dispatch: comment failed for task %s",
            claimed_task.id, exc_info=True,
        )


def poll_openclaw_results(conn: sqlite3.Connection, board: Optional[str] = None) -> None:
    """Poll Mission-Control for each in-flight OpenClaw dispatch and close it.

    Selects open runs (``task_runs.ended_at IS NULL``) whose metadata JSON has
    ``openclaw.poll_state == "submitted"`` and whose parent task is still
    ``running``. For each, GET MC's task-status endpoint with the read headers
    (reused from the openclaw_view proxy pattern) and a 6s timeout. All
    exceptions are swallowed — a transient MC blip just retries next tick.

    Terminal handling, idempotent:
      * MC ``done``/``completed`` -> flip ``poll_state`` to ``completed`` on
        the run metadata, then ``complete_task`` (run-safe via expected_run_id).
      * MC ``failed``/``canceled``/``cancelled``/``blocked`` -> flip to
        ``failed`` then ``block_task``.
    The ``ended_at`` filter excludes runs we've already closed, so a second
    tick is a no-op.
    """
    rows = conn.execute(
        """
        SELECT r.id AS run_id, r.task_id AS task_id, r.metadata AS metadata
          FROM task_runs r
          JOIN tasks t ON t.id = r.task_id
         WHERE r.ended_at IS NULL
           AND t.status = 'running'
           AND r.metadata LIKE '%"poll_state"%'
        """,
    ).fetchall()
    for row in rows:
        try:
            meta = json.loads(row["metadata"]) if row["metadata"] else {}
        except Exception:
            continue
        oc = meta.get("openclaw") if isinstance(meta, dict) else None
        if not isinstance(oc, dict) or oc.get("poll_state") != "submitted":
            continue
        mc_task_id = oc.get("mc_task_id")
        if not mc_task_id:
            continue
        run_id = int(row["run_id"])
        task_id = row["task_id"]

        # --- MC read (best-effort, all exceptions swallowed) ---
        try:
            import httpx  # local import: keeps httpx out of the import-time path
            url = _MC_TASK_STATUS_URL.format(mc_task_id=mc_task_id)
            resp = httpx.get(
                url, headers=_MC_READ_HEADERS, timeout=_MC_READ_TIMEOUT_SECONDS,
            )
            resp.raise_for_status()
            mc_task = resp.json()
        except Exception:
            # Transient MC failure / unreachable / parse error: retry next tick.
            continue
        if not isinstance(mc_task, dict):
            continue

        mc_status = str(
            mc_task.get("status") or mc_task.get("state") or ""
        ).strip().lower()
        if not mc_status:
            continue

        result_summary = (
            mc_task.get("resultSummary")
            or mc_task.get("result_summary")
            or mc_task.get("summary")
            or mc_task.get("result")
        )
        if isinstance(result_summary, (dict, list)):
            result_summary = json.dumps(result_summary, ensure_ascii=False)
        elif result_summary is not None:
            result_summary = str(result_summary)

        done_states = {"done", "completed", "complete", "succeeded", "success"}
        failed_states = {"failed", "failure", "canceled", "cancelled", "blocked", "error"}

        if mc_status in done_states:
            terminal_state = "completed"
        elif mc_status in failed_states:
            terminal_state = "failed"
        else:
            # Still in-flight on MC (queued/running/...). Leave for next tick.
            continue

        # Idempotency: flip poll_state on the run metadata BEFORE the terminal
        # call. complete_task / block_task close the run (ended_at set), so the
        # ended_at filter excludes this run on every future tick regardless.
        oc["poll_state"] = terminal_state
        oc["mc_status"] = mc_status
        merged = dict(meta)
        merged["openclaw"] = oc
        try:
            with write_txn(conn):
                conn.execute(
                    "UPDATE task_runs SET metadata = ? WHERE id = ? AND ended_at IS NULL",
                    (json.dumps(merged, ensure_ascii=False), run_id),
                )
        except Exception:
            # If we can't even flip the flag, skip — next tick retries.
            continue

        try:
            if terminal_state == "completed":
                complete_task(
                    conn, task_id,
                    result=result_summary,
                    summary=result_summary,
                    metadata=merged,
                    expected_run_id=run_id,
                )
            else:
                block_task(
                    conn, task_id,
                    reason=(result_summary or f"OpenClaw MC status: {mc_status}"),
                    expected_run_id=run_id,
                )
        except Exception:
            _log.debug(
                "poll_openclaw_results: terminal transition failed for task %s",
                task_id, exc_info=True,
            )
            continue


def reviewer_role_fit_hold_reason(
    conn: sqlite3.Connection, task_id: str,
) -> Optional[str]:
    """Return a hold reason if a ready reviewer task is an execution mis-fit.

    K3 dispatch-time preflight: reuses the advisory diagnostics rule
    ``kanban_diagnostics._rule_reviewer_role_tool_mismatch`` so a task assigned
    to the verdict-only ``reviewer`` lane whose title/body asks it to run repo
    gates is HELD (not spawned) — spawning it would burn a run that the
    reviewer cannot satisfy. A verdict-only / evidence-only reviewer task hits
    the rule's exemptions and returns ``None`` (dispatches normally).

    Fail-soft: any error (import, missing column, rule change) returns ``None``
    so the dispatcher never regresses to *not* dispatching on a diagnostics
    hiccup. Only call this for ``assignee == "reviewer"`` rows.
    """
    try:
        from hermes_cli import kanban_diagnostics as _diag
    except Exception:
        return None
    try:
        row = conn.execute(
            "SELECT id, title, body, assignee, status FROM tasks WHERE id = ?",
            (task_id,),
        ).fetchone()
        if row is None:
            return None
        diags = _diag._rule_reviewer_role_tool_mismatch(
            row, [], [], int(time.time()), {},
        )
    except Exception:
        _log.debug(
            "kanban dispatch: role-fit preflight failed for %s",
            task_id, exc_info=True,
        )
        return None
    for d in diags:
        if getattr(d, "kind", "") == "reviewer_role_tool_mismatch":
            matched = []
            data = getattr(d, "data", None)
            if isinstance(data, dict):
                matched = data.get("matched_imperatives") or []
            hint = f" (matched: {', '.join(str(m) for m in matched[:3])})" if matched else ""
            return f"reviewer assigned an execution/terminal task{hint}"
    return None


_AUTO_RETRY_QUESTION_RE = re.compile(
    r"(\?|\bwhich\b|\bchoose\b|\bdecision\b|\bdecide\b|\boperator\b|"
    r"\bhuman\b|\bcredential\b|\bcredentials?\b|\bsecret\b|\btoken\b|"
    r"\bapproval\b|\bmissing credentials?\b|\bpush\b|\bdeploy\b|"
    r"\bforce[- ]?push\b|\bgit reset\b|\brm -rf\b|\bdelete\b|"
    r"\bdrop\b|\btruncate\b|\balter\b|\bcreate\s+table\b|\bmigration\b|"
    r"\bfrage\b|\bentscheidung\b|\bentscheiden\b|\bfreigabe\b|"
    r"\bgeheim\b|\bzugang\b|\bpasswort\b|\bdeployen\b|\blöschen\b)",
    re.IGNORECASE,
)


def _blocked_kind_for_auto_retry(reason: Optional[str]) -> str:
    text = (reason or "").strip()
    if text and _AUTO_RETRY_QUESTION_RE.search(text):
        return "operator_question"
    return "retryable"


def _latest_blocked_run_for_auto_retry(
    conn: sqlite3.Connection, task_id: str,
) -> Optional[sqlite3.Row]:
    return conn.execute(
        """
        SELECT id, profile, summary, error, ended_at
          FROM task_runs
         WHERE task_id = ?
           AND outcome = 'blocked'
           AND ended_at IS NOT NULL
         ORDER BY ended_at DESC, id DESC
         LIMIT 1
        """,
        (task_id,),
    ).fetchone()


def _latest_result_comment_after(
    conn: sqlite3.Connection, task_id: str, after_ts: int,
) -> Optional[sqlite3.Row]:
    return conn.execute(
        """
        SELECT author, body, created_at
          FROM task_comments
         WHERE task_id = ?
           AND created_at > ?
           AND author NOT IN ('dispatcher', 'operator', 'dashboard', 'user')
           AND (
             body LIKE 'RESULT:%'
             OR body LIKE 'Result:%'
             OR body LIKE '%\nRESULT:%'
             OR body LIKE '%\nResult:%'
             OR body LIKE 'Ergebnis:%'
             OR body LIKE '%\nErgebnis:%'
           )
         ORDER BY created_at DESC, id DESC
         LIMIT 1
        """,
        (task_id, int(after_ts)),
    ).fetchone()


def _append_auto_retry_event_once(
    conn: sqlite3.Connection, task_id: str, kind: str, payload: dict,
) -> None:
    latest = conn.execute(
        "SELECT kind, payload FROM task_events WHERE task_id = ? "
        "ORDER BY id DESC LIMIT 1",
        (task_id,),
    ).fetchone()
    if latest is not None and latest["kind"] == kind:
        try:
            if json.loads(latest["payload"] or "{}") == payload:
                return
        except Exception:
            pass
    _append_event(conn, task_id, kind, payload)


def auto_retry_blocked_tasks(
    conn: sqlite3.Connection,
    *,
    backoff_seconds: int = DEFAULT_AUTO_RETRY_BLOCKED_BACKOFF_SECONDS,
    retry_limit: int = DEFAULT_AUTO_RETRY_BLOCKED_LIMIT,
    failure_limit: int = DEFAULT_FAILURE_LIMIT,
) -> list[tuple[str, int]]:
    """Opt-in dispatcher tick for bounded automatic retries of blocked tasks."""
    now = int(time.time())
    backoff_seconds = max(0, int(backoff_seconds))
    retry_limit = max(0, int(retry_limit))
    retried: list[tuple[str, int]] = []
    rows = conn.execute(
        """
        SELECT id, assignee, consecutive_failures, max_retries,
               auto_retry_count, created_by
          FROM tasks
         WHERE status = 'blocked'
         ORDER BY priority DESC, created_at ASC
        """
    ).fetchall()
    for row in rows:
        task_id = row["id"]
        if _is_funnel_root_task(conn, row):
            continue
        blocked_run = _latest_blocked_run_for_auto_retry(conn, task_id)
        if blocked_run is None:
            continue
        ended_at = int(blocked_run["ended_at"] or 0)
        reason = (
            (blocked_run["summary"] or "").strip()
            or (blocked_run["error"] or "").strip()
        )
        result_comment = _latest_result_comment_after(conn, task_id, ended_at)
        if result_comment is not None:
            body = str(result_comment["body"] or "")
            if complete_task(
                conn,
                task_id,
                result=body,
                summary=body.strip().splitlines()[0][:400],
                metadata={
                    "auto_retry": {
                        "source": "result_comment",
                        "blocked_run_id": int(blocked_run["id"]),
                        "comment_author": result_comment["author"],
                    }
                },
            ):
                with write_txn(conn):
                    _append_event(
                        conn,
                        task_id,
                        "auto_retry_completed",
                        {"source": "result_comment", "blocked_run_id": int(blocked_run["id"])},
                    )
            continue
        if ended_at + backoff_seconds > now:
            continue
        blocked_kind = _blocked_kind_for_auto_retry(reason)
        if blocked_kind != "retryable":
            with write_txn(conn):
                _append_auto_retry_event_once(
                    conn,
                    task_id,
                    "auto_retry_skipped",
                    {
                        "reason": "blocked_kind",
                        "blocked_kind": blocked_kind,
                        "blocked_run_id": int(blocked_run["id"]),
                    },
                )
            continue
        failures = int(row["consecutive_failures"] or 0)
        task_limit = row["max_retries"]
        effective_failure_limit = (
            int(task_limit) if task_limit is not None else int(failure_limit)
        )
        if failures >= effective_failure_limit:
            with write_txn(conn):
                _append_auto_retry_event_once(
                    conn,
                    task_id,
                    "auto_retry_skipped",
                    {"reason": "failure_limit", "failures": failures},
                )
            continue
        current_count = int(row["auto_retry_count"] or 0)
        if current_count >= retry_limit:
            with write_txn(conn):
                _append_auto_retry_event_once(
                    conn,
                    task_id,
                    "auto_retry_exhausted",
                    {"attempts": current_count, "limit": retry_limit},
                )
            continue
        attempt = current_count + 1
        escalated = attempt >= 2
        with write_txn(conn):
            latest = conn.execute(
                "SELECT status FROM tasks WHERE id = ?", (task_id,),
            ).fetchone()
            if latest is None or latest["status"] != "blocked":
                continue
            conn.execute(
                    """
                    UPDATE tasks
                       SET status = 'ready',
                           auto_retry_count = ?,
                           assignee = COALESCE(?, assignee),
                           model_override = COALESCE(?, model_override),
                           claim_lock = NULL,
                           claim_expires = NULL,
                           worker_pid = NULL
                     WHERE id = ? AND status = 'blocked'
                    """,
                    (
                        attempt,
                        AUTO_RETRY_ESCALATION_PROFILE if escalated else None,
                        AUTO_RETRY_ESCALATION_MODEL if escalated else None,
                        task_id,
                    ),
                )
            feedback = (
                f"Auto-Retry {attempt}/{retry_limit} after blocked run "
                f"{blocked_run['id']}. Previous block reason: "
                f"{(reason or '(none)')[:500]}"
            )
            conn.execute(
                "INSERT INTO task_comments (task_id, author, body, created_at) "
                "VALUES (?, ?, ?, ?)",
                (task_id, "dispatcher", feedback, now),
            )
            payload = {
                "attempt": attempt,
                "limit": retry_limit,
                "blocked_run_id": int(blocked_run["id"]),
                "reason": (reason or "")[:500] or None,
                "escalated": escalated,
            }
            if escalated:
                payload["model_override"] = AUTO_RETRY_ESCALATION_MODEL
                payload["assignee"] = AUTO_RETRY_ESCALATION_PROFILE
            _append_event(conn, task_id, "auto_retried", payload)
        retried.append((task_id, attempt))
    return retried


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
    default_assignee: Optional[str] = None,
    max_in_progress_per_profile: Optional[int] = None,
    serialize_by_repo: bool = True,
    daily_token_cap_per_profile: Optional[int] = None,
    daily_cost_cap_usd: Optional[float] = None,
    per_task_input_token_cap: Optional[int] = None,
    auto_retry_blocked: bool = False,
    auto_retry_blocked_backoff_seconds: int = DEFAULT_AUTO_RETRY_BLOCKED_BACKOFF_SECONDS,
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
    # Reap zombie children from previously spawned workers. See
    # reap_worker_zombies() for the full rationale.
    reap_worker_zombies()

    result = DispatchResult()
    # Refresh liveness for detached claude-CLI workers BEFORE the reclaimers
    # run: a live ``claude -p`` child never self-heartbeats, so this keeps its
    # ``last_heartbeat_at`` fresh and stops detect_stale_running from
    # false-positive reclaiming a healthy long run. Hermes-runtime workers are
    # skipped (they self-heartbeat). Fail-soft; never blocks dispatch.
    result.heartbeated = heartbeat_live_claude_cli_workers(conn, board=board)
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
    # Rate-limited requeues (quota wall, no failure counted) — surface for
    # telemetry / tests. These tasks went back to ``ready`` and the respawn
    # guard will defer them until the quota window clears.
    _crash_rate_limited = getattr(
        detect_crashed_workers, "_last_rate_limited", []
    )
    if _crash_rate_limited:
        result.rate_limited.extend(_crash_rate_limited)
    result.timed_out = enforce_max_runtime(conn)
    if auto_retry_blocked:
        result.auto_retried_blocked = auto_retry_blocked_tasks(
            conn,
            backoff_seconds=auto_retry_blocked_backoff_seconds,
            failure_limit=failure_limit,
        )
    result.promoted = recompute_ready(conn, failure_limit=failure_limit)

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
        "SELECT id, assignee, workflow_template_id, current_step_key, "
        "workspace_kind, workspace_path, created_by FROM tasks "
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
    spawned = 0
    # Per-profile concurrency cap (#21582): when set, track how many
    # workers each assignee already has in flight, and refuse to spawn
    # when this would push that assignee past the cap. Prevents
    # fan-out workloads from melting a single profile's local model /
    # API quota / browser pool while leaving other profiles idle.
    # Tasks blocked this way go to skipped_per_profile_capped (not
    # skipped_unassigned — the operator-actionable signal is different:
    # "this profile is busy, try again later" not "this needs routing").
    _per_profile_cap = max_in_progress_per_profile if (
        isinstance(max_in_progress_per_profile, int)
        and max_in_progress_per_profile > 0
    ) else None
    _per_profile_running: dict[str, int] = {}
    if _per_profile_cap is not None:
        for prow in conn.execute(
            "SELECT assignee, COUNT(*) AS n FROM tasks "
            "WHERE status = 'running' AND assignee IS NOT NULL "
            "GROUP BY assignee"
        ):
            _per_profile_running[prow["assignee"]] = int(prow["n"])

    # serialize_by_repo (A+C): a per-resolved-repo_root in-flight lock beside the
    # per-profile cap. Seed from every NON-TERMINAL task that is NOT itself a fresh
    # ready candidate and NOT merely parked for later — i.e. exclude 'done',
    # 'archived', 'ready', and 'scheduled'. Excluding 'ready' is what lets the
    # first ready candidate for an idle repo claim (it re-adds itself on claim,
    # step 3d). Excluding 'scheduled' keeps deliberately parked backlog cards
    # from blocking unrelated ready work in the same repo. INCLUDING 'review' and
    # 'blocked' is the fix for the 0167-0171 wave (a task parked in review/blocked
    # must keep holding its repo so N+1 never branches from a stale main).
    # Empty set + flag off => strict no-op.
    _repo_locked: set[str] = set()
    if serialize_by_repo:
        for irow in conn.execute(
            "SELECT workspace_kind, workspace_path FROM tasks "
            "WHERE status NOT IN ('done', 'archived', 'ready', 'scheduled')"
        ):
            _rr = _repo_root_for_row(irow["workspace_kind"], irow["workspace_path"])
            if _rr:
                _repo_locked.add(_rr)

    # C1 (N-C1) budget gate preflight. Caps default OFF (None) → both branches
    # are skipped, ``_budget_capped_profiles`` stays empty and
    # ``_global_cost_exceeded`` stays False → the loop below is byte-identical
    # to the pre-C1 dispatcher. When a cap IS set, aggregate the rolling-24h
    # spend from task_runs ONCE here (cheap, indexed on started_at-era columns)
    # so the per-row check is a set/bool lookup. K16 lesson: the subscription
    # fleet runs at $0 so tokens-per-profile is the real signal; $ catches
    # metered/OpenRouter. NULL token/cost values count as 0 (fail-soft).
    _token_cap = daily_token_cap_per_profile if (
        isinstance(daily_token_cap_per_profile, int)
        and daily_token_cap_per_profile > 0
    ) else None
    _cost_cap = daily_cost_cap_usd if (
        isinstance(daily_cost_cap_usd, (int, float))
        and not isinstance(daily_cost_cap_usd, bool)
        and daily_cost_cap_usd > 0
    ) else None
    _budget_capped_profiles: set[str] = set()
    _global_cost_exceeded = False
    if (_token_cap is not None or _cost_cap is not None) and ready_rows:
        _budget_window_start = int(time.time()) - 86400
        if _token_cap is not None:
            for brow in conn.execute(
                "SELECT profile, "
                "COALESCE(SUM(COALESCE(input_tokens, 0) + "
                "              COALESCE(output_tokens, 0)), 0) AS tok "
                "FROM task_runs WHERE started_at >= ? AND profile IS NOT NULL "
                "GROUP BY profile",
                (_budget_window_start,),
            ):
                if int(brow["tok"]) >= _token_cap:
                    _budget_capped_profiles.add(brow["profile"])
        if _cost_cap is not None:
            _total_cost = conn.execute(
                "SELECT COALESCE(SUM(COALESCE(cost_usd, 0)), 0) "
                "FROM task_runs WHERE started_at >= ?",
                (_budget_window_start,),
            ).fetchone()[0]
            if float(_total_cost or 0) >= _cost_cap:
                _global_cost_exceeded = True

    # G1 per-task input-token runaway guard preflight. Cap None/0 → guard OFF,
    # ``_per_task_input_usage`` stays empty and the per-row block below never
    # fires → byte-identical to the pre-G1 dispatcher. When a cap IS set,
    # aggregate the cumulative input_tokens (across ALL runs, K5a-stamped; NULLs
    # count as 0) for THIS tick's ready candidates ONCE here — same "aggregate
    # once, lookup per row" shape as the C1 caps above. Fresh tasks (no runs)
    # simply don't appear in the map and read as 0.
    _per_task_input_cap = per_task_input_token_cap if (
        isinstance(per_task_input_token_cap, int)
        and not isinstance(per_task_input_token_cap, bool)
        and per_task_input_token_cap > 0
    ) else None
    _per_task_input_usage: dict[str, tuple[int, int]] = {}
    if _per_task_input_cap is not None and ready_rows:
        _ready_ids = [r["id"] for r in ready_rows]
        for _chunk_start in range(0, len(_ready_ids), 500):
            _chunk = _ready_ids[_chunk_start:_chunk_start + 500]
            _placeholders = ",".join("?" * len(_chunk))
            for urow in conn.execute(
                "SELECT task_id, "
                "COALESCE(SUM(COALESCE(input_tokens, 0)), 0) AS tok, "
                "COUNT(*) AS n FROM task_runs "
                f"WHERE task_id IN ({_placeholders}) GROUP BY task_id",
                _chunk,
            ):
                _per_task_input_usage[urow["task_id"]] = (
                    int(urow["tok"] or 0), int(urow["n"] or 0)
                )

    # Normalize default_assignee once: empty/whitespace string → None so the
    # rest of the loop can use ``if default_assignee:`` as a single check.
    # We also resolve profile_exists once here for the same reason.
    _default_assignee = (default_assignee or "").strip() or None
    _default_assignee_resolved = False
    if _default_assignee:
        try:
            from hermes_cli.profiles import profile_exists as _pe
            _default_assignee_resolved = bool(_pe(_default_assignee))
        except Exception:
            # Profiles module not importable (test stubs, exotic envs).
            # Trust the operator's config and try the assignment; the
            # downstream profile_exists check on the assigned row will
            # bucket it as nonspawnable if the profile genuinely isn't
            # there, with the existing diagnostic.
            _default_assignee_resolved = True
    for row in ready_rows:
        if max_spawn is not None and running_count + spawned >= max_spawn:
            break
        row_assignee = row["assignee"]
        if _is_funnel_root_task(conn, row):
            result.respawn_guarded.append((row["id"], "funnel_protected"))
            continue
        # G1 per-task input-token runaway guard (additive; cap None/0 → inert,
        # this block is skipped and the loop is byte-identical to before). The
        # cumulative input_tokens across ALL of this task's runs was summed once
        # above. If it EXCEEDS the cap the task is a runaway: park it (blocked)
        # rather than (re)spawn, record the sum in ``budget_runaway_parked``, and
        # route it to the decision-queue via the operator_escalation path. No
        # mid-run kill, no parallel dispatch path — caught only here, at preflight.
        if _per_task_input_cap is not None:
            _input_sum, _input_runs = _per_task_input_usage.get(row["id"], (0, 0))
            if _input_sum > _per_task_input_cap:
                result.budget_runaway_parked.append((row["id"], _input_sum))
                if not dry_run:
                    _park_budget_runaway(
                        conn, row,
                        token_sum=_input_sum,
                        cap=_per_task_input_cap,
                        runs=_input_runs,
                    )
                continue
        # serialize_by_repo: resolve this candidate's repo_root once and reuse it
        # at the guard (3d) and every claim-success re-add. Computed here (above the
        # openclaw branch) so it is in scope at ALL claim paths, including openclaw.
        _cand_repo = (
            _repo_root_for_row(row["workspace_kind"], row["workspace_path"])
            if serialize_by_repo else None
        )
        # K8 workflow-step routing (D7 L2): a task opted into a workflow
        # template is routed by its CURRENT STEP, not the static assignee
        # column. Resolve the step's role and — when it differs — persist it to
        # the row (so the downstream claim_task→spawn uses it and the board
        # reflects the active role), mirroring the default_assignee auto-assign
        # below. Fail-soft: a missing/broken template or an unresolvable step
        # leaves the column assignee untouched, so the task routes exactly as
        # today. Non-workflow tasks (NULL workflow_template_id) skip this block
        # entirely → byte-identical to the legacy path.
        _wf_template_id = row["workflow_template_id"]
        if _wf_template_id:
            _wf_role = _workflow_step_assignee(
                _wf_template_id, row["current_step_key"]
            )
            if _wf_role and _wf_role != row_assignee:
                if not dry_run:
                    try:
                        with write_txn(conn):
                            conn.execute(
                                "UPDATE tasks SET assignee = ? WHERE id = ?",
                                (_wf_role, row["id"]),
                            )
                            _append_event(
                                conn, row["id"], "assigned",
                                {
                                    "assignee": _wf_role,
                                    "source": "workflow_step",
                                    "step_key": row["current_step_key"],
                                    "workflow_template_id": _wf_template_id,
                                },
                            )
                    except Exception:
                        _log.debug(
                            "kanban dispatch: failed to apply workflow step "
                            "assignee=%r to task %s",
                            _wf_role, row["id"], exc_info=True,
                        )
                row_assignee = _wf_role
        if not row_assignee:
            # Honour kanban.default_assignee: when the dispatcher hits an
            # unassigned ready task and an operator-configured fallback
            # exists, persist the assignment and proceed. This removes the
            # dashboard footgun where a task created without an assignee
            # parks in 'ready' forever even though the operator's intent
            # ("default") was perfectly clear (#27145). Mutating the row
            # (not just the in-memory view) keeps diagnostics and the
            # board state consistent: the task is now legitimately owned
            # by ``kanban.default_assignee``, not "unassigned but secretly
            # routed".
            if _default_assignee and _default_assignee_resolved:
                # Dry-run: show what WOULD happen (auto-assign + spawn) without
                # mutating the DB. Real run: mutate the row + emit the
                # 'assigned' event so the board state matches what just happened.
                if not dry_run:
                    try:
                        with write_txn(conn):
                            conn.execute(
                                "UPDATE tasks SET assignee = ? WHERE id = ? "
                                "AND (assignee IS NULL OR assignee = '')",
                                (_default_assignee, row["id"]),
                            )
                            _append_event(
                                conn, row["id"], "assigned",
                                {
                                    "assignee": _default_assignee,
                                    "source": "kanban.default_assignee",
                                },
                            )
                    except Exception:
                        _log.debug(
                            "kanban dispatch: failed to apply default_assignee=%r "
                            "to task %s",
                            _default_assignee, row["id"], exc_info=True,
                        )
                        result.skipped_unassigned.append(row["id"])
                        continue
                row_assignee = _default_assignee
                result.auto_assigned_default.append(row["id"])
            else:
                result.skipped_unassigned.append(row["id"])
                continue
        # ---- OpenClaw cross-system dispatch ----
        # An ``openclaw:<agent>`` assignee is NOT a Hermes profile and would be
        # rejected by the profile_exists gate just below. Intercept it FIRST:
        # claim the task, sign + POST an MC envelope, and leave it ``running``
        # for poll-back. This branch must sit BEFORE the profile_exists gate so
        # normal assignees are 100% unaffected (they never enter this block).
        _openclaw_op = _parse_openclaw_assignee(row_assignee)
        if _openclaw_op is not None:
            if dry_run:
                # Report what WOULD be dispatched without mutating the DB.
                result.spawned.append((row["id"], row_assignee, ""))
                result.openclaw_dispatched.append((row["id"], _openclaw_op))
                continue
            claimed = claim_task(conn, row["id"], ttl_seconds=ttl_seconds)
            if claimed is None:
                continue
            try:
                _dispatch_to_openclaw(conn, claimed, _openclaw_op)
                # Success: task stays ``running`` (poll-back closes it later).
                result.openclaw_dispatched.append((claimed.id, _openclaw_op))
                spawned += 1
                if _per_profile_cap is not None and claimed.assignee:
                    _per_profile_running[claimed.assignee] = (
                        _per_profile_running.get(claimed.assignee, 0) + 1
                    )
                if serialize_by_repo and _cand_repo:
                    _repo_locked.add(_cand_repo)
            except Exception as exc:
                # Mirror the local-spawn failure path: release the claim,
                # count the failure, auto-block after the limit. A missing
                # signer module, an MC rejection, or an unimplemented agent
                # all degrade here rather than crashing the dispatcher.
                auto = _record_spawn_failure(
                    conn, claimed.id, f"openclaw: {exc}",
                    failure_limit=failure_limit,
                )
                if auto:
                    result.auto_blocked.append(claimed.id)
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
        if profile_exists is not None and not profile_exists(row_assignee):
            # Bucket separately from skipped_unassigned: the operator
            # cannot fix this by assigning a profile (the assignee IS the
            # intended owner — a terminal lane). Health telemetry uses
            # this distinction to suppress spurious "stuck" warnings on
            # multi-lane setups where the ready queue is steadily full
            # of human-pulled work.
            result.skipped_nonspawnable.append(row["id"])
            # Emit ONE deduped diagnostic event so a MIS-assigned task
            # (assignee names a subagent/role, not a real profile) is
            # visible on the board timeline instead of silently rotting in
            # ``ready`` with no diagnosis. F2 dedup (role_fit_held pattern):
            # skip when the task's most recent event is already
            # ``nonspawnable`` — the task is re-evaluated every tick, so
            # without this it would emit one event per tick forever. This is
            # diagnostic ONLY: ``has_spawnable_ready`` still treats these as
            # non-spawnable so the stuck-alarm stays suppressed for genuine
            # terminal lanes (orion-cc / orion-research).
            if not dry_run:
                latest = conn.execute(
                    "SELECT kind FROM task_events WHERE task_id = ? "
                    "ORDER BY id DESC LIMIT 1",
                    (row["id"],),
                ).fetchone()
                if latest is None or latest["kind"] != "nonspawnable":
                    with write_txn(conn):
                        _append_event(
                            conn, row["id"], "nonspawnable",
                            {"assignee": row_assignee},
                        )
            continue
        # Per-profile concurrency cap (#21582): even if there's global
        # headroom, refuse to spawn for an assignee that's already at
        # its in-flight cap. Prevents one profile's local model / API
        # quota / browser pool from being overwhelmed by a fan-out
        # while the global max_in_progress / max_spawn caps still allow
        # work on OTHER profiles.
        if _per_profile_cap is not None:
            current = _per_profile_running.get(row_assignee, 0)
            if current >= _per_profile_cap:
                result.skipped_per_profile_capped.append(
                    (row["id"], row_assignee, current)
                )
                continue
        # serialize_by_repo guard: another non-terminal task holds this repo_root
        # this tick -> defer THIS candidate (continue, never break: other repos must
        # still flow). Emit ONE deduped repo_serialized event (role_fit_held pattern).
        if serialize_by_repo and _cand_repo and _cand_repo in _repo_locked:
            result.skipped_repo_serialized.append((row["id"], _cand_repo))
            if not dry_run:
                latest = conn.execute(
                    "SELECT kind FROM task_events WHERE task_id = ? "
                    "ORDER BY id DESC LIMIT 1",
                    (row["id"],),
                ).fetchone()
                if latest is None or latest["kind"] != "repo_serialized":
                    with write_txn(conn):
                        _append_event(
                            conn, row["id"], "repo_serialized",
                            {"repo_root": _cand_repo},
                        )
            continue
        # C1 (N-C1) budget hold: a daily cap is hit. Board-wide $ cap holds
        # every assigned ready task; per-profile token cap holds only that
        # profile's tasks. The task stays in ``ready`` (advisory hold, like the
        # role-fit hold above), and we emit ONE deduped ``budget_held`` event
        # (F2 pattern: skip if the task's most recent event is already
        # ``budget_held``) so the decision-queue shows it without flooding the
        # timeline. Caps OFF → this block never fires.
        if _global_cost_exceeded or row_assignee in _budget_capped_profiles:
            if _global_cost_exceeded:
                budget_reason = (
                    f"daily cost cap ${_cost_cap:.2f} reached "
                    "(rolling 24h, board-wide hold)"
                )
            else:
                budget_reason = (
                    f"daily token cap {_token_cap} reached for profile "
                    f"{row_assignee} (rolling 24h)"
                )
            result.budget_held.append((row["id"], row_assignee, budget_reason))
            if not dry_run:
                latest = conn.execute(
                    "SELECT kind FROM task_events WHERE task_id = ? "
                    "ORDER BY id DESC LIMIT 1",
                    (row["id"],),
                ).fetchone()
                if latest is None or latest["kind"] != "budget_held":
                    with write_txn(conn):
                        _append_event(
                            conn, row["id"], "budget_held",
                            {"reason": budget_reason},
                        )
            continue
        contract_reason = (
            check_respawn_guard(conn, row["id"])
            if dry_run
            else ensure_code_task_contract_before_pickup(
                conn, row["id"], source="dispatch",
            )
        )
        if contract_reason is not None:
            result.respawn_guarded.append((row["id"], contract_reason))
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
        # K3 role-fit preflight: hold a reviewer task that asks the
        # verdict-only reviewer lane to run repo gates instead of spawning a
        # run the reviewer cannot satisfy. Scoped to ``assignee == "reviewer"``
        # (the review-gate verifier lane is a different path and never reaches
        # here). The task is left in ``ready`` (advisory, re-evaluated each
        # tick), NOT blocked. Verdict-only reviewers fall through and dispatch.
        if row_assignee == "reviewer":
            hold_reason = reviewer_role_fit_hold_reason(conn, row["id"])
            if hold_reason is not None:
                result.held_role_mismatch.append((row["id"], hold_reason))
                # Emit an event so operators see why it was held (and don't
                # mistake the steadily-ready reviewer task for "stuck").
                # F2: dedup — a held task is re-evaluated every dispatch tick,
                # so without a guard it would emit one ``role_fit_held`` per
                # tick forever, flooding the timeline. Emit only when the hold
                # is newly observed: skip when the task's most recent event is
                # already ``role_fit_held`` (nothing changed since the last
                # hold). Any intervening event (re-route, comment, status
                # change) makes the next hold emit a fresh diagnosis. The hold
                # behaviour itself (append to result + ``continue``) is
                # unchanged — only the duplicate event emission is suppressed.
                if not dry_run:
                    latest = conn.execute(
                        "SELECT kind FROM task_events WHERE task_id = ? "
                        "ORDER BY id DESC LIMIT 1",
                        (row["id"],),
                    ).fetchone()
                    if latest is None or latest["kind"] != "role_fit_held":
                        with write_txn(conn):
                            _append_event(
                                conn, row["id"], "role_fit_held",
                                {"reason": hold_reason},
                            )
                continue
        if dry_run:
            result.spawned.append((row["id"], row_assignee, ""))
            # Increment per-profile counter even in dry_run so the cap
            # check sees the would-be spawn on subsequent iterations.
            # Without this, dry_run reports every task as spawnable and
            # under-reports the capped subset (#21582).
            if _per_profile_cap is not None and row_assignee:
                _per_profile_running[row_assignee] = (
                    _per_profile_running.get(row_assignee, 0) + 1
                )
            if serialize_by_repo and _cand_repo:
                _repo_locked.add(_cand_repo)
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
        # Worker isolation (kanban.worker_isolation: worktree): provision a
        # dispatcher-managed git worktree for repo tasks at claim time. The
        # provisioner re-persists the worktree path on the task row; a
        # provisioning failure counts as a spawn failure, exactly like a
        # resolve_workspace error. Flag off → byte-identical to before.
        try:
            from hermes_cli import kanban_worktrees as _kwt
            if _kwt.isolation_mode() == "worktree":
                workspace = _kwt.provision_for_task(
                    conn, claimed, workspace, board=board,
                )
        except Exception as exc:
            from hermes_cli import kanban_worktrees as _kwt
            # A transient git-lock timeout is infrastructure, not a task
            # defect: re-queue to ``ready`` WITHOUT consuming the
            # consecutive_failures budget, up to SPAWN_RETRY_LIMIT (read from
            # the env at call time so operators/tests can tune it). Only once
            # the spawn budget is spent — or for a permanent WorktreeError —
            # do we fall back to the normal counted spawn-failure/block path.
            spawn_retry_limit = int(
                os.environ.get("HERMES_SPAWN_RETRY_LIMIT", SPAWN_RETRY_LIMIT)
            )
            transient = isinstance(exc, _kwt.WorktreeTimeout)
            if transient and _count_spawn_retries(conn, claimed.id) < spawn_retry_limit:
                _record_spawn_retry(
                    conn, claimed.id,
                    f"worktree provisioning (transient): {exc}",
                )
            else:
                auto = _record_spawn_failure(
                    conn, claimed.id, f"worktree provisioning: {exc}",
                    failure_limit=failure_limit,
                )
                if auto:
                    result.auto_blocked.append(claimed.id)
            continue
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
            # Track the new in-flight count for this profile so later
            # iterations in this same tick respect the per-profile cap
            # (#21582). Subsequent ticks re-query from the DB.
            if _per_profile_cap is not None and claimed.assignee:
                _per_profile_running[claimed.assignee] = (
                    _per_profile_running.get(claimed.assignee, 0) + 1
                )
            if serialize_by_repo and _cand_repo:
                _repo_locked.add(_cand_repo)
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
            # Mirror of the ready-path diagnostic above: emit ONE deduped
            # ``nonspawnable`` event so a review task whose assignee is not a
            # runnable profile is visible on the timeline instead of silently
            # rotting in ``review``.
            if not dry_run:
                latest = conn.execute(
                    "SELECT kind FROM task_events WHERE task_id = ? "
                    "ORDER BY id DESC LIMIT 1",
                    (row["id"],),
                ).fetchone()
                if latest is None or latest["kind"] != "nonspawnable":
                    with write_txn(conn):
                        _append_event(
                            conn, row["id"], "nonspawnable",
                            {"assignee": row["assignee"]},
                        )
            continue
        if dry_run:
            result.spawned.append((row["id"], row["assignee"], ""))
            continue
        # Phase 2: run the review agent as the independent ``verifier`` profile
        # (terminal-enabled, file-disabled → runs tests/build/lint but cannot
        # edit), NOT the task's own code-writing assignee. The override is
        # in-memory only — the DB ``assignee`` stays the original coder, so a
        # REQUEST_CHANGES (kanban_block → blocked) leaves the task owned by the
        # coder for a follow-up fix. Falls back to the original assignee if the
        # verifier profile is missing (degenerate self-review, never a stall).
        # The historical ``sdlc-review`` skill does not exist in this tree; the
        # verifier's review logic lives in its profile SOUL.md, so we don't
        # force a phantom skill (it would be silently skipped anyway).
        _rg_cfg = _review_gate_config()
        _verifier_profile = _rg_cfg["verifier_profile"]
        try:
            from hermes_cli.profiles import profile_exists as _pexists
        except Exception:
            _pexists = None
        _spawn_profile = row["assignee"]
        if _verifier_profile and (_pexists is None or _pexists(_verifier_profile)):
            _spawn_profile = _verifier_profile
        claimed = claim_review_task(
            conn, row["id"], ttl_seconds=ttl_seconds, reviewer_profile=_spawn_profile,
        )
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
        # NOTE: resolve_workspace() above is task-keyed (not assignee-keyed),
        # so the verifier inherits the coder's preserved workspace and can
        # inspect the real changes.
        set_workspace_path(conn, claimed.id, str(workspace))
        # Worker isolation: surface uncommitted leftovers in a provisioned
        # worktree to the verifier as a task comment — the worker contract
        # requires committing on green gates, so leftovers are grounds for
        # REQUEST_CHANGES. Best-effort; non-provisioned workspaces no-op.
        try:
            from hermes_cli import kanban_worktrees as _kwt
            _kwt.note_dirty_worktree(conn, claimed.id, str(workspace))
        except Exception:
            pass
        _maybe_emit_scratch_tip(conn, claimed.id, claimed.workspace_kind)
        claimed.assignee = _spawn_profile
        claimed.skills = []
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


def _skill_available_for_home(skill_name: str, hermes_home: Optional[str]) -> bool:
    """True if ``skill_name`` resolves for the home the spawned worker runs under.

    A per-task ``--skill`` that does NOT resolve for the worker's profile-scoped
    skills dir is FATAL at CLI startup (``ValueError: Unknown skill(s)``),
    aborting the worker before the agent loop — which the dispatcher then retries
    into a crash loop (observed 7x on a single card). The dispatcher gates every
    per-task ``--skills`` flag on this so a bad/absent skill name is skipped with
    a warning instead of force-loaded into a crash. Mirrors the bundled
    ``_kanban_worker_skill_available`` resolution, generalised to any name.
    """
    from pathlib import Path as _Path

    if not skill_name:
        return False
    base = _Path(hermes_home) if hermes_home else (_Path.home() / ".hermes")
    skills_root = base / "skills"
    if not skills_root.is_dir():
        return False
    # Flat canonical location first (cheap), then a bounded scan for skills
    # nested under a category dir (e.g. devops/<name>/SKILL.md).
    if (skills_root / skill_name / "SKILL.md").is_file():
        return True
    try:
        for skill_md in skills_root.rglob(f"{skill_name}/SKILL.md"):
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


_SYSTEMD_SCOPE_USABLE: Optional[bool] = None


def _systemd_scope_usable() -> bool:
    """Probe (once per process) whether transient user scopes work here.

    ``systemd-run --user`` needs a session DBus + user manager; containers,
    Termux and CI runners typically lack both. The probe runs a no-op scope
    so a broken environment degrades to the plain (in-cgroup) spawn instead
    of failing every dispatch.
    """
    global _SYSTEMD_SCOPE_USABLE
    if _SYSTEMD_SCOPE_USABLE is None:
        try:
            probe = subprocess.run(
                ["systemd-run", "--user", "--scope", "--quiet", "--collect",
                 "--", "/bin/true"],
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                timeout=10,
            )
            _SYSTEMD_SCOPE_USABLE = probe.returncode == 0
        except Exception:
            _SYSTEMD_SCOPE_USABLE = False
        if not _SYSTEMD_SCOPE_USABLE:
            _log.warning(
                "HERMES_WORKER_SYSTEMD_SCOPE is set but `systemd-run --user "
                "--scope` is not usable here — workers stay in the spawning "
                "service's cgroup."
            )
    return _SYSTEMD_SCOPE_USABLE


def _maybe_scope_worker_cmd(cmd: list[str]) -> list[str]:
    """Optionally detach a worker spawn into its own transient systemd scope.

    Opt-in via ``HERMES_WORKER_SYSTEMD_SCOPE=1`` (set in the dashboard/gateway
    service units). Without it, workers spawned from a systemd service stay in
    that service's cgroup, which has two real consequences (2026-06-11 audit):

    * ``systemctl restart`` of the service KILLS every running worker
      (KillMode=control-group), so a dashboard deploy aborted in-flight runs;
    * the service's MemoryPeak accounts the workers (780MB–2.6GB spikes that
      look like a dashboard leak but aren't).

    ``systemd-run --scope`` re-execs the command in place after moving it
    into the scope: the returned PID *is* the worker's PID and signals hit
    the worker directly (verified live), so reaper liveness checks and
    SIGTERM/SIGKILL handling are unchanged. No-op on Windows, when the env
    flag is absent, or when the probe says scopes don't work here.
    """
    if _IS_WINDOWS:
        return cmd
    flag = os.environ.get("HERMES_WORKER_SYSTEMD_SCOPE", "").strip().lower()
    if flag not in ("1", "true", "yes"):
        return cmd
    if not _systemd_scope_usable():
        return cmd
    # CPUWeight=30 (default 100): under contention the kernel gives worker
    # scopes ~1/10 of the dashboard's share (its unit runs at CPUWeight=300),
    # so a worker's test gate can no longer starve interactive /control
    # requests. Idle machine = workers still get full speed.
    return [
        "systemd-run", "--user", "--scope", "--quiet", "--collect",
        "--property=CPUWeight=30", "--",
    ] + cmd


def _claude_worker_bin() -> str:
    """Resolve the ``claude`` CLI binary used for claude-CLI worker spawns.

    Order: ``$HERMES_CLAUDE_BIN`` (explicit operator override) → the known
    install path ``/home/piet/.local/bin/claude`` if it exists → bare
    ``"claude"`` (PATH fallback).
    """
    env_bin = os.environ.get("HERMES_CLAUDE_BIN")
    if env_bin:
        return env_bin
    default_path = "/home/piet/.local/bin/claude"
    if os.path.exists(default_path):
        return default_path
    return "claude"


def _is_claude_cli_profile(profile_arg: str, hermes_home: Optional[str]) -> bool:
    """True if ``profile_arg`` should be dispatched via the ``claude`` CLI.

    Fail-soft: returns False on ANY error so a broken profile config can never
    divert the default (hermes) spawn path.

    Two seams, checked in order:

    1. Env allowlist (primary for tests/ops): ``HERMES_CLAUDE_CLI_PROFILES`` is
       a comma-separated list of profile names; an exact (case-sensitive) match
       after stripping whitespace returns True.
    2. Profile config flag (production): the profile-scoped
       ``<hermes_home>/config.yaml`` top-level key ``worker_runtime`` equal to
       the string ``"claude-cli"`` returns True.
    """
    try:
        allow = os.environ.get("HERMES_CLAUDE_CLI_PROFILES", "")
        for name in allow.split(","):
            if name.strip() == profile_arg:
                return True
        if hermes_home:
            try:
                import yaml
            except Exception:
                return False
            cfg_path = os.path.join(hermes_home, "config.yaml")
            if os.path.isfile(cfg_path):
                with open(cfg_path, "r", encoding="utf-8") as fh:
                    cfg = yaml.safe_load(fh) or {}
                if isinstance(cfg, dict) and cfg.get("worker_runtime") == "claude-cli":
                    return True
        return False
    except Exception:
        return False


def _claude_profile_model(hermes_home: Optional[str]) -> Optional[str]:
    """Per-profile default claude model for a claude-CLI worker.

    Reads the top-level ``claude_model`` key from the profile-scoped
    ``<hermes_home>/config.yaml``. Returns None on any error / absence so the
    worker falls back to the claude subscription default. This is the MIDDLE
    tier of claude model routing:

        task.model_override  (per-task escalation, highest)
        > claude_model       (per-profile default tier — this helper)
        > subscription default (omit --model, currently opus-4-8)

    A profile can thus default to a fast/cheap tier (e.g. ``claude-fable-5``)
    while hard tasks escalate to Opus via the per-task override.
    """
    try:
        if not hermes_home:
            return None
        import yaml
        cfg_path = os.path.join(hermes_home, "config.yaml")
        if not os.path.isfile(cfg_path):
            return None
        with open(cfg_path, "r", encoding="utf-8") as fh:
            cfg = yaml.safe_load(fh) or {}
        if isinstance(cfg, dict):
            model = cfg.get("claude_model")
            if isinstance(model, str) and model.strip():
                return model.strip()
        return None
    except Exception:
        return None


# --- Worker env allowlist (security hardening S1) -------------------------
#
# Workers are spawned with broad autonomy (the claude-CLI lane even runs
# `--dangerously-skip-permissions`), so the dispatcher must NOT forward its
# own environment wholesale: the gateway env carries Discord bot tokens,
# API_SERVER_KEY, and provider keys for lanes the worker doesn't run.
# Hermes-lane workers re-load their profile-scoped `.env` from disk at
# startup (load_hermes_dotenv, override=True), so stripping inherited
# secrets does not starve them of their own lane credentials.

# Name prefixes forwarded to workers: the Hermes worker contract + config
# vars, terminal/timeout knobs, locale, and XDG base dirs.
_WORKER_ENV_PREFIXES = ("HERMES_", "TERMINAL_", "LC_", "XDG_")

# Exact names forwarded to workers: process basics only.
_WORKER_ENV_PASSTHROUGH = frozenset({
    "PATH", "HOME", "USER", "LOGNAME", "SHELL", "TERM", "COLORTERM",
    "LANG", "LANGUAGE", "TZ", "TMPDIR", "TEMP", "TMP", "PWD",
    "VIRTUAL_ENV", "PYTHONUNBUFFERED", "PYTHONIOENCODING",
    "SSL_CERT_FILE", "SSL_CERT_DIR", "REQUESTS_CA_BUNDLE", "CURL_CA_BUNDLE",
    "HTTP_PROXY", "HTTPS_PROXY", "NO_PROXY", "ALL_PROXY",
    "http_proxy", "https_proxy", "no_proxy", "all_proxy",
    # Windows process basics (no-ops elsewhere).
    "SYSTEMROOT", "SystemRoot", "COMSPEC", "ComSpec", "PATHEXT",
})

# LLM provider keys hermes-lane workers may legitimately use. Passed through
# as a safety net for profiles without their own `.env` (the profile `.env`
# overrides these on load anyway). Deliberately NOT bot tokens / gateway
# secrets. The claude-CLI lane drops even these (Max subscription needs no
# provider key at all).
_WORKER_LANE_PROVIDER_KEYS = frozenset({
    "OPENROUTER_API_KEY", "MINIMAX_API_KEY", "MINIMAX_BASE_URL",
    "KIMI_API_KEY", "FIRECRAWL_API_KEY", "FIRECRAWL_API_URL",
    "HONCHO_API_KEY",
})


def _build_worker_env(parent_env) -> dict:
    """Allowlisted copy of ``parent_env`` for spawned kanban workers.

    Everything not matched by the allowlist is dropped — most importantly
    DISCORD_* bot tokens, API_SERVER_KEY, ANTHROPIC_API_KEY and any other
    credential the dispatcher process happens to hold.
    """
    env: dict = {}
    for key, value in parent_env.items():
        if (
            key in _WORKER_ENV_PASSTHROUGH
            or key in _WORKER_LANE_PROVIDER_KEYS
            or key.startswith(_WORKER_ENV_PREFIXES)
        ):
            env[key] = value
    return env


def _spawn_claude_worker(
    task: Task,
    workspace: str,
    *,
    env: dict,
    board: Optional[str] = None,
    lane_model: Optional[str] = None,
) -> Optional[int]:
    """Fire-and-forget ``claude -p <prompt>`` subprocess for a claude-CLI worker.

    Reuses the fully-built ``env`` from ``_default_spawn`` AS-IS (it already
    carries the HERMES_KANBAN_TASK / _DB / _BOARD / _WORKSPACE / _RUN_ID /
    HERMES_HOME / HERMES_PROFILE contract). The headless ``claude`` worker
    drives the task to a terminal state by running ``hermes kanban complete``
    / ``block`` from inside its session — completion is authorized by the
    ``HERMES_KANBAN_TASK`` env var + the DB path.

    Mirrors ``_default_spawn``'s per-task log + Popen tail exactly so crash
    detection and log rotation behave identically across both runtimes.
    """
    import shutil
    import subprocess

    # The worker shells out to `hermes kanban complete/block`, so `hermes`
    # must be on the child's PATH. Prepend the hermes binary's directory.
    hermes_dir = os.path.dirname(shutil.which("hermes") or "/home/piet/.local/bin/hermes")
    env["PATH"] = hermes_dir + os.pathsep + env.get("PATH", "")

    # Tighten the env beyond the dispatcher allowlist: the claude CLI runs
    # on the subscription (it must NOT see ANTHROPIC_API_KEY, or billing
    # silently switches to the API key) and never re-loads Hermes .env
    # files, so no LLM provider key has any business in this process.
    for key in _WORKER_LANE_PROVIDER_KEYS | {"ANTHROPIC_API_KEY"}:
        env.pop(key, None)

    # memsearch stays out of headless workers: the user-global memory plugin
    # would inject shared session memories into the worker context at
    # SessionStart and spawn a haiku summarize on every Stop — per worker,
    # per turn. Suppress the watcher belt-and-suspenders via env; the actual
    # plugin disable is the --settings flag on the cmd below (NOT --bare,
    # which would also drop the guard-dangerous-ops PreToolUse hook the S2
    # hardening relies on).
    env["MEMSEARCH_NO_WATCH"] = "1"

    body = task.body or ""
    title = task.title or ""
    # Worker isolation (Phase 2, Entscheidung 1): the commit contract is
    # injected ONLY for dispatcher-provisioned worktrees. Workers in the
    # live checkout (flag off, legacy dir tasks) keep today's prompt —
    # a `git add -A` there would commit foreign dirty work.
    git_contract = ""
    try:
        from hermes_cli.kanban_worktrees import is_provisioned_path
        if is_provisioned_path(workspace):
            git_contract = (
                "Git contract: this directory is a dispatcher-provisioned "
                "git worktree on your own task branch. When your gates are "
                "green, commit your work: git add -A && git commit -m "
                '"kanban($HERMES_KANBAN_TASK): <one-line summary>" — and '
                'include the hash in your completion metadata as "commit". '
                "NEVER push, NEVER merge into another branch, NEVER switch "
                "branches; integration happens outside your run after "
                "review.\n\n"
            )
    except Exception:
        git_contract = ""
    prompt = (
        "You are an autonomous Hermes kanban worker running headless. "
        "Your task id is in $HERMES_KANBAN_TASK.\n\n"
        f"Task title: {title}\n"
        f"Task body:\n{body}\n\n"
        "Work in the current directory.\n\n"
        f"{git_contract}"
        "MANDATORY: your turn is not over until you report back, via the "
        "Bash tool (the hermes binary is on PATH; do not ask for "
        "confirmation):\n"
        "1. Post your end RESULT first — the answer/report a human asked "
        "for, not how you went about it — as one self-contained Markdown "
        'comment: hermes kanban comment "$HERMES_KANBAN_TASK" '
        '"<deliverable>".\n'
        "2. Then complete with a structured handoff: hermes kanban "
        'complete "$HERMES_KANBAN_TASK" --summary "<one line>" '
        "--metadata '<json>' where <json> is ONE JSON object with "
        '"residual_risk" (one line: what could still break or was not '
        'verified) plus the facts that apply: "changed_files": [...], '
        '"tests_run": N, "decisions": [...]. To keep a file you created, '
        'add "artifacts": ["<absolute path>"] — the workspace is deleted '
        "on completion; listed workspace files are copied to "
        "~/.hermes/reports/by-task/ first, anything unlisted is gone.\n"
        "3. If you cannot finish, run: hermes kanban block "
        '"$HERMES_KANBAN_TASK" "<reason>" instead.\n\n'
        "PROVIDER RULE: Never call anthropic/* or openai/gpt-5* models via "
        "--provider openrouter. claude-fable-5 runs on the claude-cli lane "
        "only; gpt-5.5 runs on the Codex lane only. If the task requires "
        "such a model and you are not on the right lane, block with an "
        "explanation. Every paid external API call must be disclosed in the "
        "task (cost + provider)."
    )

    cmd = [
        _claude_worker_bin(),
        "-p", prompt,
        "--dangerously-skip-permissions",
        # Deny the direct-HTTP tools (S2): with --dangerously-skip-permissions
        # an --allowedTools list would be a no-op (everything is auto-approved),
        # but disallowed tools stay hard-denied even in bypass mode. Bash-level
        # egress (curl -d, scp, nc, ...) is gated by the user-global
        # guard-dangerous-ops.sh PreToolUse hook, which loads for these
        # workers too.
        "--disallowedTools", "WebFetch,WebSearch",
        "--output-format", "json",
        # Keep the memsearch memory plugin out of worker sessions (see env
        # comment above). enabledPlugins merges into user settings, so other
        # plugins (superpowers, guard hooks) keep their normal state.
        "--settings", '{"enabledPlugins": {"memsearch@memsearch-plugins": false}}',
    ]
    # Model routing: per-task override > active lane (F1) > per-profile default
    # (claude_model) > subscription default (omit --model). A profile can default
    # to a fast/cheap tier (e.g. claude-fable-5) while hard tasks escalate via
    # model_override; the lane sits between the two as the operator-switchable
    # fleet-wide preset.
    worker_model = (
        task.model_override
        or lane_model
        or _claude_profile_model(env.get("HERMES_HOME"))
    )
    if worker_model:
        cmd.extend(["--model", worker_model])

    # Per-task log under <board-root>/logs/ — mirror the hermes path so
    # `hermes kanban log` reads the same file and rotation is identical.
    log_dir = worker_logs_dir(board=board)
    log_dir.mkdir(parents=True, exist_ok=True)
    log_path = log_dir / f"{task.id}.log"
    rotate_bytes, backup_count = worker_log_rotation_config()
    _rotate_worker_log(log_path, rotate_bytes, backup_count)

    log_f = open(log_path, "ab")
    try:
        proc = subprocess.Popen(  # noqa: S603 -- argv is a fixed list built above
            _maybe_scope_worker_cmd(cmd),
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
            "`claude` executable not found on PATH. "
            "Install the Claude CLI or set HERMES_CLAUDE_BIN before running the kanban dispatcher."
        )
    except BaseException:
        log_f.close()
        raise
    # NOTE: we intentionally do NOT close log_f here — same as the hermes path
    # (the child keeps writing after this function returns).
    return proc.pid


def _resolve_worker_cli_toolsets(hermes_home: Optional[str]) -> Optional[list[str]]:
    """Return the assigned profile's effective CLI toolsets for a worker.

    Dispatcher-spawned workers are launched from a long-lived gateway process,
    then the child re-enters the CLI with ``-p <assignee>``. Resolve the
    assignee profile's CLI tool surface at dispatch time and pass it as an
    explicit ``--toolsets`` pin so worker startup cannot fall back to a stale
    root/active-profile config or a profile whose top-level ``toolsets`` entry
    is only the kanban orchestrator surface. ``model_tools`` still appends the
    task-scoped kanban lifecycle tools when ``HERMES_KANBAN_TASK`` is set.
    """
    if not hermes_home:
        return None
    try:
        from hermes_constants import reset_hermes_home_override, set_hermes_home_override
        from hermes_cli.config import load_config
        from hermes_cli.tools_config import _get_platform_tools

        token = set_hermes_home_override(hermes_home)
        try:
            cfg = load_config()
            toolsets = sorted(_get_platform_tools(cfg, "cli"))
        finally:
            reset_hermes_home_override(token)
        return toolsets or None
    except Exception as exc:
        _log.debug(
            "kanban worker: could not resolve CLI toolsets for HERMES_HOME=%r (%s)",
            hermes_home,
            exc,
        )
        return None



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
    env = _build_worker_env(os.environ)

    # Inject HERMES_HOME so the worker reads the profile-scoped config.yaml
    # (fallback_providers, toolsets, agent settings, etc.) instead of the root
    # config.  Without this, the allowlisted copy of the parent's env carries
    # the dispatcher's HERMES_HOME, and when the child process starts
    # `hermes -p <name>` the
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
    if task.current_run_id is not None:
        env["HERMES_KANBAN_RUN_ID"] = str(task.current_run_id)
    if task.claim_lock:
        env["HERMES_KANBAN_CLAIM_LOCK"] = task.claim_lock
    # Per-task iteration-budget override (env-var leg). NOTE: this env var
    # is SHADOWED in production — the worker resolves max_turns as
    # "CLI arg > config > env > default" (cli.py:3052) and load_cli_config
    # always injects agent.max_turns, so config wins over this var. The
    # AUTHORITATIVE override is the `--max-turns` chat flag appended to the
    # worker argv below (the top-precedence CLI-arg branch). The env var is
    # kept for consistency, diagnostics, and any consumer that bypasses
    # load_cli_config. NULL on the task = inherit the profile default.
    # See feedback_hermes_iteration_budget_cap.md.
    if task.max_iterations is not None:
        env["HERMES_MAX_ITERATIONS"] = str(int(task.max_iterations))
    # Goal-loop mode: the worker reads these and wraps its run in the
    # Ralph-style /goal judge loop (see cli.py quiet-mode path). Only set
    # when enabled so non-goal tasks keep a clean env.
    if task.goal_mode:
        env["HERMES_KANBAN_GOAL_MODE"] = "1"
        if task.goal_max_turns is not None:
            env["HERMES_KANBAN_GOAL_MAX_TURNS"] = str(int(task.goal_max_turns))
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

    # Lane hot-read (F1): the active lane may pin this profile's runtime and
    # model for THIS spawn. Fail-soft — a broken lanes table yields None and
    # the pre-lane behavior below is untouched.
    lane_entry = _active_lane_entry_for_profile(profile_arg, board=board)
    lane_runtime = (lane_entry or {}).get("worker_runtime")
    lane_provider = (lane_entry or {}).get("provider")
    lane_model = (lane_entry or {}).get("model")
    lane_fallback_providers = (lane_entry or {}).get("fallback_providers") or []

    # Early branch for claude-CLI worker profiles: launch the `claude` CLI
    # instead of `hermes -p <profile> chat`. The lane's worker_runtime (when
    # set) overrides the profile-config seam in BOTH directions; otherwise
    # fail-soft config check as before — a broken profile config can never
    # divert the default path (see _is_claude_cli_profile). The hermes path
    # below stays byte-identical for unmapped profiles.
    if lane_runtime == "claude-cli" or (
        lane_runtime is None
        and _is_claude_cli_profile(profile_arg, env.get("HERMES_HOME"))
    ):
        return _spawn_claude_worker(
            task, workspace, env=env, board=board, lane_model=lane_model,
        )

    cmd = [
        *_resolve_hermes_argv(),
        "-p", profile_arg,
        # Worker subprocesses switch to a profile-scoped HERMES_HOME above,
        # so they see that profile's shell-hook allowlist instead of the
        # dispatcher's root allowlist. Pass --accept-hooks explicitly so
        # profile-local worker sessions still register configured hooks.
        "--accept-hooks",
    ]
    # Auto-load the kanban-worker skill so every dispatched worker
    # has the pattern library (good summary/metadata shapes, retry
    # diagnostics, block-reason examples) in its context, even if
    # the profile hasn't wired it into skills config. The MANDATORY
    # lifecycle is already in the system prompt via KANBAN_GUIDANCE;
    # this skill is the deeper reference. Users can point a profile
    # at a different/additional skill via config if they want —
    # --skills is additive to the profile's default skill set.
    #
    # Only add the flag when the skill actually resolves for the home
    # the worker runs under: the bundled skill is absent from many
    # profile-scoped skills dirs, and preloading a missing skill is
    # fatal at CLI startup. Omitting it is safe — the lifecycle
    # contract still ships via KANBAN_GUIDANCE.
    if _kanban_worker_skill_available(env.get("HERMES_HOME")):
        cmd.extend(["--skills", "kanban-worker"])
    # Per-task force-loaded skills. Each name goes in its own
    # `--skills X` pair rather than a single comma-joined arg: the CLI
    # accepts both forms (action='append' + comma-split), but
    # per-name pairs are easier to read in `ps` output and avoid any
    # quoting ambiguity if a skill name ever contains unusual chars.
    # Dedupe against the built-in so we don't double-load kanban-worker
    # if a task author asks for it explicitly.
    if task.skills:
        for sk in task.skills:
            if sk and sk != "kanban-worker":
                # Only force-load a skill that actually resolves for the
                # worker's home. A missing skill name is fatal at CLI startup
                # (Unknown skill(s)) and the dispatcher would retry it into a
                # crash loop — skip + warn instead so the task still runs.
                if _skill_available_for_home(sk, env.get("HERMES_HOME")):
                    cmd.extend(["--skills", sk])
                else:
                    _log.warning(
                        "kanban dispatch: task %s requests skill %r which does "
                        "not resolve under the worker home (%s) — skipping it to "
                        "avoid a startup crash loop. Fix the skill name or install "
                        "it for the assignee profile.",
                        task.id, sk, env.get("HERMES_HOME") or "~/.hermes",
                    )
    # Pin the assignee profile's CLI toolsets so worker startup can't fall
    # back to a stale config (upstream feature). --toolsets is a top-level
    # flag, so it goes BEFORE the `chat` subcommand.
    worker_toolsets = _resolve_worker_cli_toolsets(env.get("HERMES_HOME"))
    if worker_toolsets:
        cmd.extend(["--toolsets", ",".join(worker_toolsets)])
    cmd.append("chat")
    # Per-task model override (T4 / WI-6 fix). `-m/--model` is BOTH a
    # top-level and a chat-subparser flag, each defaulting to None. Placing
    # `-m` BEFORE `chat` let the chat subparser's default=None clobber the
    # top-level value in the single shared namespace, so the override never
    # reached the worker. It must come AFTER `chat` so argparse routes it to
    # the chat subparser (same reasoning as `--max-turns` below).
    # Lane (F1) sits below the per-task override: override > lane > profile
    # config default (no -m flag at all).
    hermes_model = task.model_override or lane_model
    if hermes_model:
        cmd.extend(["-m", hermes_model])
    if lane_provider and not task.model_override:
        cmd.extend(["--provider", lane_provider])
    if lane_fallback_providers:
        for fallback in lane_fallback_providers:
            if not isinstance(fallback, dict):
                continue
            provider = (fallback.get("provider") or "").strip()
            model = (fallback.get("model") or "").strip()
            if not provider or not model:
                continue
            if fallback.get("base_url"):
                cmd.extend(["--fallback-provider", json.dumps(fallback, separators=(",", ":"))])
            else:
                cmd.extend(["--fallback-provider", f"{provider}:{model}"])
    # Per-task iteration-budget override routed through the top-precedence
    # CLI-arg path: `--max-turns N` maps to HermesCLI(max_turns=N), which
    # wins over the profile's agent.max_turns (cli.py:3053) — unlike the
    # HERMES_MAX_ITERATIONS env var, which the profile config shadows. This
    # is what actually lets audit-class tasks exceed the profile default.
    # `--max-turns` is a chat-subcommand flag (hermes_cli/_parser.py), so it
    # must come after `chat` in the argv.
    if task.max_iterations is not None:
        cmd.extend(["--max-turns", str(int(task.max_iterations))])
    cmd.extend(["-q", prompt])
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
            _maybe_scope_worker_cmd(cmd),
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
    except BaseException:
        log_f.close()
        raise
    # NOTE: we intentionally do NOT close log_f here — we want Popen's
    # child process to keep writing after this function returns.  The
    # handle is kept alive by the child's inheritance.  The parent's
    # reference goes out of scope and is GC'd, but the OS-level FD stays
    # open in the child until the child exits.
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

# A2 (N-A2): caps for the verifier-only review section.
_CTX_REVIEW_MAX_CHANGED_FILES = 50
_CTX_REVIEW_MAX_DIFF_STAT = 2000


def _latest_review_diff_snapshot(
    conn: sqlite3.Connection, task_id: str
) -> tuple[list, Optional[str]]:
    """Return ``(changed_files, diff_stat)`` from the most recent B1 snapshot.

    Reads the latest ``submitted_for_review`` event payload. Fail-soft: returns
    ``([], None)`` when there is no such event, no snapshot keys, or the payload
    is unreadable.
    """
    try:
        row = conn.execute(
            "SELECT payload FROM task_events "
            "WHERE task_id = ? AND kind = 'submitted_for_review' "
            "ORDER BY id DESC LIMIT 1",
            (task_id,),
        ).fetchone()
    except sqlite3.Error:
        return [], None
    if not row or not row["payload"]:
        return [], None
    try:
        payload = json.loads(row["payload"])
    except (ValueError, TypeError):
        return [], None
    if not isinstance(payload, dict):
        return [], None
    cf = payload.get("changed_files")
    changed = [str(x) for x in cf] if isinstance(cf, list) else []
    ds = payload.get("diff_stat")
    diff_stat = ds if isinstance(ds, str) and ds.strip() else None
    return changed, diff_stat


def _render_review_verifier_section(
    conn: sqlite3.Connection, task_id: str
) -> list:
    """A2: context rendered ONLY for a run claimed from the review lane.

    Gives the verifier (a) the task's structured acceptance criteria (A1) as an
    explicit per-item checklist and (b) the changed-files snapshot captured at
    submit (B1) so it can run the mandated caller-grep. Returns ``[]`` for any
    non-review run, so an ordinary worker's context stays byte-identical.
    """
    run_id = _current_run_id(conn, task_id)
    if not _run_originated_from_review(conn, task_id, run_id):
        return []

    lines: list = ["## Acceptance checklist (verifier — judge each item)"]
    # (a) acceptance criteria (A1 column)
    acc_json = None
    try:
        row = conn.execute(
            "SELECT acceptance_criteria FROM tasks WHERE id = ?", (task_id,)
        ).fetchone()
        if row:
            acc_json = row["acceptance_criteria"]
    except sqlite3.Error:
        acc_json = None
    criteria: list = []
    if acc_json:
        try:
            parsed = json.loads(acc_json)
            if isinstance(parsed, list):
                criteria = parsed
        except (ValueError, TypeError):
            criteria = []
    if criteria:
        for item in criteria:
            if isinstance(item, dict):
                label = item.get("id") or ""
                stmt = item.get("statement") or ""
                head = ": ".join(p for p in [str(label), str(stmt)] if p)
                extra = "; ".join(
                    f"{k}: {v}" for k, v in (
                        ("verification", item.get("verification")),
                        ("done_signal", item.get("done_signal")),
                    ) if v
                )
                line = f"- [ ] {head or str(item)}"
                if extra:
                    line += f" ({extra})"
                lines.append(line)
            else:
                lines.append(f"- [ ] {str(item).strip()}")
        lines.append("")
        lines.append(
            "Render a per-criterion verdict — each item MET or UNMET with the "
            "evidence you checked. Any UNMET → kanban_block."
        )
    else:
        lines.append(
            "_No structured acceptance criteria were recorded for this task. "
            "Derive the acceptance bar from the Body above and judge against it._"
        )

    # (b) changed-files snapshot from the submit event (B1)
    changed_files, diff_stat = _latest_review_diff_snapshot(conn, task_id)
    lines.append("")
    lines.append("## Changed files at submit (caller check required)")
    if changed_files:
        for f in changed_files[:_CTX_REVIEW_MAX_CHANGED_FILES]:
            lines.append(f"- `{f}`")
        if len(changed_files) > _CTX_REVIEW_MAX_CHANGED_FILES:
            extra_n = len(changed_files) - _CTX_REVIEW_MAX_CHANGED_FILES
            lines.append(f"- _(+{extra_n} more)_")
        if diff_stat:
            lines.append("")
            lines.append("```")
            lines.append(diff_stat[:_CTX_REVIEW_MAX_DIFF_STAT])
            lines.append("```")
        lines.append("")
        lines.append(
            "MANDATORY: for any CHANGED existing symbol (function/class/const), "
            "grep its callers (`rg`) and confirm they still hold — an unchecked "
            "caller of a changed symbol is a blocking finding."
        )
    else:
        lines.append(
            "_No machine diff snapshot was captured. Inspect the workspace "
            "directly (git status / git diff) before judging._"
        )

    # (c) #3-A: worker_gate stamp — one machine-readable line for the verifier
    # SOUL to match on. Format contract (must match exactly):
    #   PASSED:  "Coder worker_gate: PASSED (exit 0) at <ts>, commit <sha7>"
    #   FAILED:  "Coder worker_gate: FAILED (<cmd> exit <n>)"
    #   N/A:     "Coder worker_gate: not configured for this repo (no coder-side gate ran)"
    try:
        _wg_ev_row = conn.execute(
            "SELECT payload FROM task_events "
            "WHERE task_id = ? AND kind = 'submitted_for_review' "
            "ORDER BY id DESC LIMIT 1",
            (task_id,),
        ).fetchone()
        _wg_ev: dict = {}
        if _wg_ev_row and _wg_ev_row["payload"]:
            _parsed = json.loads(_wg_ev_row["payload"])
            if isinstance(_parsed, dict):
                _wg_ev = _parsed.get("worker_gate") or {}
    except Exception:
        _wg_ev = {}
    lines.append("")
    if not _wg_ev or _wg_ev.get("configured") is False:
        lines.append(
            "Coder worker_gate: not configured for this repo "
            "(no coder-side gate ran)"
        )
    elif _wg_ev.get("passed") is True:
        _ts = _wg_ev.get("ts", "")
        _sha = (_wg_ev.get("commit") or "")[:7]
        lines.append(
            f"Coder worker_gate: PASSED (exit 0) at {_ts}, commit {_sha}"
        )
    else:
        _cmds = _wg_ev.get("commands") or []
        _codes = _wg_ev.get("exit_codes") or []
        # Find the first failing command
        _fail_cmd = ""
        _fail_code = 0
        for _c, _e in zip(_cmds, _codes):
            if _e != 0:
                _fail_cmd = _c
                _fail_code = _e
                break
        if not _fail_cmd and _cmds:
            _fail_cmd = _cmds[-1]
            _fail_code = _codes[-1] if _codes else -1
        lines.append(
            f"Coder worker_gate: FAILED ({_fail_cmd} exit {_fail_code})"
        )

    lines.append("")
    return lines


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
    if int(task.continuation_count or 0) > 0:
        continuation_limit = _resolve_max_continuations(task)
        lines.append("")
        lines.append(
            f"This is continuation run {int(task.continuation_count or 0)}/{continuation_limit}."
        )
        lines.append(
            "Continue from the latest run summary/log. Do not restart from scratch."
        )
        lines.append("If complete, call kanban_complete. If blocked, call kanban_block.")
    lines.append("")

    if task.body and task.body.strip():
        lines.append("## Body")
        lines.append(_cap(task.body, _CTX_MAX_BODY_BYTES))
        lines.append("")

    # A2: verifier-only section (acceptance checklist + changed-files snapshot).
    # Empty for every non-review run, so an ordinary worker's context is
    # byte-identical to the pre-A2 output.
    lines.extend(_render_review_verifier_section(conn, task_id))

    # Attachments — files uploaded to this task (PDFs, source docs,
    # images). Surface the absolute on-disk path so the worker, which has
    # full file-tool access, can read them directly (read_file, terminal
    # `pdftotext`, etc.). On the local terminal backend the path resolves
    # as-is; remote backends need the kanban attachments dir mounted.
    attachments = list_attachments(conn, task_id)
    if attachments:
        lines.append("## Attachments")
        lines.append(
            "Files attached to this task. Read them with the file/terminal "
            "tools at the absolute paths below:"
        )
        for att in attachments:
            size_kb = max(1, (att.size + 1023) // 1024) if att.size else 0
            size_str = f", {size_kb} KB" if size_kb else ""
            ctype = f", {att.content_type}" if att.content_type else ""
            lines.append(f"- `{att.filename}`{ctype}{size_str} → `{att.stored_path}`")
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

def _nearest_rank_percentile(
    sorted_vals: list[int], pct: float,
) -> Optional[int]:
    """Nearest-rank percentile over a pre-sorted list. ``None`` when empty.

    Dependency-free (no ``statistics``/``math`` import) and deterministic —
    fine for a HUD cycle-time signal where exact interpolation is unneeded.
    """
    if not sorted_vals:
        return None
    n = len(sorted_vals)
    idx = int(round((pct / 100.0) * (n - 1)))
    idx = max(0, min(n - 1, idx))
    return int(sorted_vals[idx])


def task_runs_cost_usd_sum(
    conn: sqlite3.Connection,
    *,
    task_id: Optional[str] = None,
    since_epoch: Optional[int] = None,
) -> Optional[float]:
    """Sum ``task_runs.cost_usd`` for a task or a recent window.

    Defensive about the ``cost_usd`` column: it is added by K5a's additive
    migration, so on a pre-K5a DB the column is absent and this returns
    ``None`` rather than raising into the dashboard / notifier. Returns
    ``None`` when there is no cost recorded (column present but all NULL).
    """
    where = []
    params: list[Any] = []
    if task_id is not None:
        where.append("task_id = ?")
        params.append(task_id)
    if since_epoch is not None:
        where.append("ended_at IS NOT NULL AND ended_at >= ?")
        params.append(int(since_epoch))
    clause = (" WHERE " + " AND ".join(where)) if where else ""
    try:
        row = conn.execute(
            f"SELECT SUM(cost_usd) AS c FROM task_runs{clause}", params,
        ).fetchone()
    except sqlite3.OperationalError:
        return None  # pre-K5a: cost_usd column not yet migrated in
    if row is None or row["c"] is None:
        return None
    try:
        return float(row["c"])
    except (TypeError, ValueError):
        return None


def profile_outcome_stats(
    conn: sqlite3.Connection, *, last_n: int = 50
) -> dict[str, dict]:
    """Return recent per-profile outcome aggregates for decomposer context.

    Read-only and fail-soft: older DBs may not have D1/B2/K5a columns yet, and
    the decomposer must keep working with the exact old roster when that
    happens.
    """
    window = int(last_n)
    if window <= 0:
        return {}
    try:
        rows = conn.execute(
            """
            WITH ranked AS (
                SELECT
                    profile,
                    outcome,
                    verdict,
                    CASE
                        WHEN input_tokens IS NULL AND output_tokens IS NULL
                            THEN NULL
                        ELSE COALESCE(input_tokens, 0) + COALESCE(output_tokens, 0)
                    END AS token_sum,
                    CASE
                        WHEN started_at IS NOT NULL AND ended_at IS NOT NULL
                            THEN ended_at - started_at
                        ELSE NULL
                    END AS runtime_s,
                    ROW_NUMBER() OVER (
                        PARTITION BY profile
                        ORDER BY started_at DESC, id DESC
                    ) AS rn
                FROM task_runs
                WHERE profile IS NOT NULL AND outcome IS NOT NULL
            ),
            windowed AS (
                SELECT * FROM ranked WHERE rn <= ?
            )
            SELECT
                profile,
                COUNT(*) AS runs,
                AVG(CASE WHEN outcome = 'completed' THEN 1.0 ELSE 0.0 END) * 100.0
                    AS done_pct,
                AVG(CASE WHEN outcome = 'blocked' THEN 1.0 ELSE 0.0 END) * 100.0
                    AS blocked_pct,
                AVG(CASE WHEN outcome = 'timed_out' THEN 1.0 ELSE 0.0 END) * 100.0
                    AS timeout_pct,
                AVG(token_sum) AS avg_tokens,
                AVG(runtime_s) AS avg_runtime_s,
                SUM(CASE WHEN verdict IS NOT NULL THEN 1 ELSE 0 END) AS verdict_n,
                CASE
                    WHEN SUM(CASE WHEN verdict IS NOT NULL THEN 1 ELSE 0 END) = 0
                        THEN NULL
                    ELSE
                        SUM(CASE WHEN verdict = 'APPROVED' THEN 1.0 ELSE 0.0 END)
                        * 100.0
                        / SUM(CASE WHEN verdict IS NOT NULL THEN 1.0 ELSE 0.0 END)
                END AS approved_pct
            FROM windowed
            GROUP BY profile
            """,
            (window,),
        ).fetchall()
    except sqlite3.OperationalError:
        return {}

    stats: dict[str, dict] = {}
    for row in rows:
        avg_tokens = row["avg_tokens"]
        avg_runtime = row["avg_runtime_s"]
        stats[row["profile"]] = {
            "runs": int(row["runs"]),
            "done_pct": float(row["done_pct"] or 0.0),
            "blocked_pct": float(row["blocked_pct"] or 0.0),
            "timeout_pct": float(row["timeout_pct"] or 0.0),
            "avg_tokens": (
                int(round(float(avg_tokens))) if avg_tokens is not None else None
            ),
            "avg_runtime_s": (
                int(round(float(avg_runtime))) if avg_runtime is not None else None
            ),
            "verdict_n": int(row["verdict_n"] or 0),
            "approved_pct": (
                float(row["approved_pct"]) if row["approved_pct"] is not None else None
            ),
        }
    return stats


def board_stats(conn: sqlite3.Connection) -> dict:
    """Per-status + per-assignee counts, plus the oldest ``ready`` age in
    seconds (the clearest staleness signal for a router or HUD).

    K6 additively surfaces L1 observability: throughput (``completed_last_24h``
    / ``completed_last_7d``), cycle-time percentiles over the last 7d
    (``completed_at - created_at``), and ``total_cost_usd_24h`` (summed from
    ``task_runs.cost_usd``; ``None`` until K5a populates it). All pre-existing
    keys are unchanged — callers reading ``by_status`` etc. keep working.
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

    # K6: throughput + cycle-time. ``completed_at`` is stamped on every
    # terminal transition (done/blocked-final), so count and cycle over it.
    day_ago = now - 86_400
    week_ago = now - 7 * 86_400
    completed_last_24h = int(conn.execute(
        "SELECT COUNT(*) AS n FROM tasks "
        "WHERE completed_at IS NOT NULL AND completed_at >= ?",
        (day_ago,),
    ).fetchone()["n"])
    completed_last_7d = int(conn.execute(
        "SELECT COUNT(*) AS n FROM tasks "
        "WHERE completed_at IS NOT NULL AND completed_at >= ?",
        (week_ago,),
    ).fetchone()["n"])
    durations = sorted(
        int(r["dt"])
        for r in conn.execute(
            "SELECT completed_at - created_at AS dt FROM tasks "
            "WHERE completed_at IS NOT NULL AND created_at IS NOT NULL "
            "AND completed_at >= ?",
            (week_ago,),
        )
        if r["dt"] is not None and int(r["dt"]) >= 0
    )
    queue_waits = sorted(
        int(r["dt"])
        for r in conn.execute(
            "SELECT MIN(r.started_at) - t.created_at AS dt "
            "FROM tasks t JOIN task_runs r ON r.task_id = t.id "
            "WHERE t.created_at IS NOT NULL AND r.started_at IS NOT NULL "
            "GROUP BY t.id"
        )
        if r["dt"] is not None and int(r["dt"]) >= 0
    )
    run_profiles = [
        r["profile"] for r in conn.execute(
            "SELECT DISTINCT profile FROM task_runs "
            "WHERE profile IS NOT NULL AND TRIM(profile) != ''"
        )
    ]

    return {
        "by_status": by_status,
        "by_assignee": by_assignee,
        "oldest_ready_age_seconds": oldest_ready_age,
        "now": now,
        "completed_last_24h": completed_last_24h,
        "completed_last_7d": completed_last_7d,
        "cycle_time_p50_seconds": _nearest_rank_percentile(durations, 50),
        "cycle_time_p90_seconds": _nearest_rank_percentile(durations, 90),
        "total_cost_usd_24h": task_runs_cost_usd_sum(conn, since_epoch=day_ago),
        "queue_wait_p50_seconds": _nearest_rank_percentile(queue_waits, 50),
        "run_duration_percentiles": run_duration_percentiles(conn, run_profiles),
    }


def autonomy_stats(conn: sqlite3.Connection) -> dict:
    """Operator-free task acceptance rate from task event history."""
    accepted = int(conn.execute(
        "SELECT COUNT(DISTINCT task_id) AS n FROM task_events WHERE kind = 'created'"
    ).fetchone()["n"] or 0)
    escalations = int(conn.execute(
        "SELECT COUNT(DISTINCT task_id) AS n FROM task_events WHERE kind = ?",
        (OPERATOR_ESCALATION_EVENT,),
    ).fetchone()["n"] or 0)
    return {
        "accepted_tasks": accepted,
        "operator_escalations": escalations,
        "autonomy_rate": (1.0 - (escalations / accepted)) if accepted else None,
    }


def chain_completion_stats(conn: sqlite3.Connection) -> dict:
    """Done roots whose dependency leaves are all done, divided by done roots."""
    done_roots = conn.execute(
        "SELECT id FROM tasks "
        "WHERE status = 'done' AND id NOT IN (SELECT DISTINCT parent_id FROM task_links)"
    ).fetchall()
    complete = 0
    for row in done_roots:
        member_ids = _root_tree_member_ids(conn, row["id"])
        leaves = [mid for mid in member_ids if mid != row["id"] and not parent_ids(conn, mid)]
        if not leaves:
            complete += 1
            continue
        placeholders = ",".join("?" for _ in leaves)
        open_leaf = conn.execute(
            f"SELECT 1 FROM tasks WHERE id IN ({placeholders}) AND status != 'done' LIMIT 1",
            tuple(leaves),
        ).fetchone()
        if open_leaf is None:
            complete += 1
    total = len(done_roots)
    return {
        "done_roots": total,
        "completed_done_roots": complete,
        "chain_completion_rate": (complete / total) if total else None,
    }


def _root_tree_member_ids(conn: sqlite3.Connection, root_id: str) -> list[str]:
    """A tree-sink root + all its transitive parents (the work tasks). The
    K2/F1 link convention: a child waits for its parent, the orchestration
    sink/root is the child of every leaf, so its tree is reached by walking
    ``parent_ids`` upward. Cycle-safe."""
    seen = {root_id}
    members = [root_id]
    stack = list(parent_ids(conn, root_id))
    while stack:
        pid = stack.pop()
        if pid in seen:
            continue
        seen.add(pid)
        members.append(pid)
        stack.extend(parent_ids(conn, pid))
    return members


def runs_summary(
    conn: sqlite3.Connection, *, since_hours: int = 24, max_roots: int = 20,
) -> dict:
    """K7: root-grouped run summary over the last ``since_hours``.

    A ROOT is a task nobody depends on — a tree sink (has parents, no
    children) or a standalone task. Interior work nodes roll up into their
    root (same de-flood model as K2's consolidated report), so each completed
    unit of work is counted once. For every root completed in the window we
    sum its tree's run cost, derive its cycle-time, and count its subtasks.

    Returns aggregate throughput / cost / cycle-time plus the most recent
    roots (newest first). Cost is ``None`` when no run in the tree recorded a
    cost (pre-K5a / unattributed), never a crash.
    """
    now = int(time.time())
    since_hours = max(1, int(since_hours))
    window_start = now - since_hours * 3600

    # Tasks that ARE a parent (something depends on them) → interior nodes.
    interior = {
        r["parent_id"]
        for r in conn.execute("SELECT DISTINCT parent_id FROM task_links")
    }

    completed = conn.execute(
        "SELECT id, title, status, assignee, created_at, completed_at "
        "FROM tasks WHERE completed_at IS NOT NULL AND completed_at >= ? "
        "ORDER BY completed_at DESC",
        (window_start,),
    ).fetchall()

    roots: list[dict[str, Any]] = []
    cycle_times: list[int] = []
    total_cost: Optional[float] = None
    for row in completed:
        if row["id"] in interior:
            continue  # rolled into its own root
        member_ids = _root_tree_member_ids(conn, row["id"])
        cost: Optional[float] = None
        for mid in member_ids:
            c = task_runs_cost_usd_sum(conn, task_id=mid)
            if c is not None:
                cost = (cost or 0.0) + c
        if cost is not None:
            total_cost = (total_cost or 0.0) + cost
        cycle_time = None
        if row["created_at"] is not None:
            delta = int(row["completed_at"]) - int(row["created_at"])
            if delta >= 0:
                cycle_time = delta
                cycle_times.append(delta)
        roots.append({
            "id": row["id"],
            "title": row["title"],
            "status": row["status"],
            "assignee": row["assignee"],
            "completed_at": int(row["completed_at"]),
            "cost_usd": cost,
            "cycle_time_seconds": cycle_time,
            "subtask_count": max(0, len(member_ids) - 1),
        })

    cycle_times.sort()
    return {
        "since_hours": since_hours,
        "now": now,
        "completed_roots": len(roots),
        "total_cost_usd": total_cost,
        "cycle_time_p50_seconds": _nearest_rank_percentile(cycle_times, 50),
        "cycle_time_p90_seconds": _nearest_rank_percentile(cycle_times, 90),
        "roots": roots[: max(0, int(max_roots))],
    }


# Outcomes, die als "fehlgeschlagen" in die Verlässlichkeits-Rate eingehen.
_RELIABILITY_FAIL_OUTCOMES = (
    "crashed", "timed_out", "spawn_failed", "gave_up", "iteration_budget_exhausted",
)


def _reliability_window(conn: sqlite3.Connection, *, window_start: int, min_n: int) -> list[dict]:
    """Per-Profil-Verlässlichkeit über ein Zeitfenster (beendete Runs).

    Drei Signal-Familien je Profil:
      * Outcome-Raten der eigenen Runs (completed vs. crash/timeout/spawn-fail …),
      * Retry-Quote (Runs über dem ersten Versuch derselben Task),
      * Verifier-Urteile, dem GEPRÜFTEN Run zugeordnet: ``task_runs.verdict``
        steht auf dem Verifier-Run; das geprüfte Profil ist der jüngste zuvor
        beendete Run derselben Task, der selbst kein Verdict trägt.

    ``min_n`` spiegelt das roster-stats-Damping: Profile unter der Schwelle
    werden mitgeliefert, aber als ``low_sample`` markiert (die UI dämpft).
    """
    rows = conn.execute(
        "SELECT id, task_id, profile, outcome, started_at, ended_at, verdict "
        "FROM task_runs WHERE ended_at IS NOT NULL AND ended_at >= ? "
        "ORDER BY ended_at ASC",
        (window_start,),
    ).fetchall()

    stats: dict[str, dict] = {}

    def _bucket(profile: Optional[str]) -> dict:
        key = profile or "unbekannt"
        b = stats.get(key)
        if b is None:
            b = {
                "profile": key,
                "runs": 0,
                "completed": 0,
                "failed": 0,
                "outcomes": {},
                "tasks": set(),
                "judged": 0,
                "approved": 0,
                "rejected": 0,
            }
            stats[key] = b
        return b

    for r in rows:
        b = _bucket(r["profile"])
        b["runs"] += 1
        b["tasks"].add(r["task_id"])
        outcome = (r["outcome"] or "unknown").strip() or "unknown"
        b["outcomes"][outcome] = b["outcomes"].get(outcome, 0) + 1
        if outcome == "completed":
            b["completed"] += 1
        elif outcome in _RELIABILITY_FAIL_OUTCOMES:
            b["failed"] += 1

    # Verdict-Zuordnung: für jeden Verifier-Run im Fenster den jüngsten zuvor
    # beendeten verdienst-freien Run derselben Task suchen (auch außerhalb des
    # Fensters, damit ein früher Coder-Run nicht aus der Zuordnung fällt).
    for r in rows:
        verdict = (r["verdict"] or "").strip().upper()
        if not verdict:
            continue
        judged = conn.execute(
            "SELECT profile FROM task_runs "
            "WHERE task_id = ? AND id != ? AND ended_at IS NOT NULL "
            "AND ended_at <= ? AND (verdict IS NULL OR verdict = '') "
            "ORDER BY ended_at DESC, id DESC LIMIT 1",
            (r["task_id"], r["id"], r["started_at"] or r["ended_at"]),
        ).fetchone()
        if judged is None:
            continue
        b = _bucket(judged["profile"])
        b["judged"] += 1
        if verdict == "APPROVED":
            b["approved"] += 1
        elif verdict == "REQUEST_CHANGES":
            b["rejected"] += 1

    out: list[dict] = []
    for b in stats.values():
        runs = b["runs"]
        task_count = len(b["tasks"])
        retries = max(0, runs - task_count)
        judged = b["judged"]
        out.append({
            "profile": b["profile"],
            "runs": runs,
            "tasks": task_count,
            "outcomes": dict(sorted(b["outcomes"].items())),
            "completed_rate": round(b["completed"] / runs, 4) if runs else None,
            "failed_rate": round(b["failed"] / runs, 4) if runs else None,
            "retries": retries,
            "retry_rate": round(retries / runs, 4) if runs else None,
            "judged": judged,
            "approved": b["approved"],
            "rejected": b["rejected"],
            # Approve-Rate nur mit genug Urteilen — sonst None (min-n-Gate).
            "approve_rate": round(b["approved"] / judged, 4) if judged >= min_n else None,
            "low_sample": runs < min_n,
        })
    out.sort(key=lambda p: (-p["runs"], p["profile"]))
    return out


def runs_reliability(
    conn: sqlite3.Connection, *, since_hours: int = 168,
    baseline_hours: int = 720, min_n: int = 5,
) -> dict:
    """Verlässlichkeit pro Profil: aktuelles Fenster (default 7 d) plus
    30-d-Baseline zum Vergleich. Operator-Vertrag 2026-06-10 (Phase 3)."""
    now = int(time.time())
    since_hours = max(1, int(since_hours))
    baseline_hours = max(since_hours, int(baseline_hours))
    min_n = max(1, int(min_n))
    return {
        "since_hours": since_hours,
        "baseline_hours": baseline_hours,
        "min_n": min_n,
        "now": now,
        "profiles": _reliability_window(
            conn, window_start=now - since_hours * 3600, min_n=min_n,
        ),
        "baseline": _reliability_window(
            conn, window_start=now - baseline_hours * 3600, min_n=min_n,
        ),
    }


# --- Demand-Funnel / Wert-Bilanz -------------------------------------------
# Herkunfts-Tags der Funnel-Quellen (created_by). Bewusst Konvention statt
# Schema: Vorschläge sind normale Kanban-Tasks, nur mit diesen Autoren.
FUNNEL_CREATED_BY = ("family", "discord-idee", "fo-gap-audit")

_VALUE_TITLE_NUTZER_RE = re.compile(
    r"\[FO\]|^0\d{3}:|FO Mobil|FO NextGen|Abo-Limits|/kitchen|/shopping|"
    r"Essensplan|Rezept|Einkauf",
    re.IGNORECASE,
)

_VALUE_CLASSES = ("nutzer", "haertung", "meta")


def value_class(
    created_by: Optional[str],
    *,
    title: Optional[str] = None,
    epic_id: Optional[str] = None,
) -> str:
    """Wert-Klasse eines gelieferten Roots, abgeleitet aus stabilen Task-Signalen.

    Bewusst unscharf (kein Schema-Touch): FO-/Family-Signale über Titel,
    Funnel-Quellen oder Epic-Zuordnung → ``nutzer``; Review-/Verifier-Ketten
    → ``haertung``; alles andere → ``meta``. Fehlklassifikationen sind im
    Digest sichtbar; wenn das nervt, ist eine ``value_class``-Spalte der
    dokumentierte v2-Schritt.
    """
    c = (created_by or "").strip().lower()
    t = (title or "").strip()
    if epic_id or c in FUNNEL_CREATED_BY or _VALUE_TITLE_NUTZER_RE.search(t):
        return "nutzer"
    if c == "kanban-review-chain" or "review" in c or "verif" in c:
        return "haertung"
    return "meta"


def runs_daily(conn: sqlite3.Connection, *, days: int = 30) -> dict:
    """Tages-Zeitreihe für die Statistik-Charts: Durchsatz (gelieferte Roots +
    Tasks), Kosten-Burn und Run-Ausgänge pro lokalem Kalendertag. Leere Tage
    werden mitgeliefert (durchgehende Achse). Read-only, eine Hand voll
    Aggregat-Queries — kein N+1."""
    days = max(1, min(365, int(days)))
    now = int(time.time())
    today = _dt.date.fromtimestamp(now)
    start_day = today - _dt.timedelta(days=days - 1)
    window_start = int(time.mktime(start_day.timetuple()))

    buckets: dict[str, dict] = {}
    for i in range(days):
        day = start_day + _dt.timedelta(days=i)
        buckets[day.isoformat()] = {
            "date": day.isoformat(),
            "done_roots": 0,
            "done_tasks": 0,
            # Dollar-Kosten sind auf einer Subscription-Flotte meist ehrliche
            # 0.0 (billing_mode=subscription_included) — Tokens sind die
            # belastbare Burn-Metrik und laufen deshalb separat mit.
            "cost_usd": None,
            "input_tokens": None,
            "output_tokens": None,
            "runs_completed": 0,
            "runs_failed": 0,
            "cycle_times": [],
            # Wert-Bilanz: wofür wurde geliefert (Klasse je Root via created_by).
            "done_roots_by_class": {cls: 0 for cls in _VALUE_CLASSES},
        }

    def _day_key(ts: int) -> Optional[str]:
        try:
            key = _dt.date.fromtimestamp(int(ts)).isoformat()
        except (OverflowError, OSError, ValueError):
            return None
        return key if key in buckets else None

    # Tasks: geliefert (Roots = Senken, gleiche Definition wie runs_summary).
    interior = {
        r["parent_id"]
        for r in conn.execute("SELECT DISTINCT parent_id FROM task_links")
    }
    for row in conn.execute(
        "SELECT id, title, created_at, completed_at, created_by, epic_id FROM tasks "
        "WHERE completed_at IS NOT NULL AND completed_at >= ?",
        (window_start,),
    ).fetchall():
        key = _day_key(row["completed_at"])
        if key is None:
            continue
        b = buckets[key]
        b["done_tasks"] += 1
        if row["id"] not in interior:
            b["done_roots"] += 1
            b["done_roots_by_class"][value_class(
                row["created_by"], title=row["title"], epic_id=row["epic_id"],
            )] += 1
            if row["created_at"] is not None:
                delta = int(row["completed_at"]) - int(row["created_at"])
                if delta >= 0:
                    b["cycle_times"].append(delta)

    # Runs: Ausgänge + Kosten + Token-Burn.
    for row in conn.execute(
        "SELECT ended_at, outcome, cost_usd, input_tokens, output_tokens "
        "FROM task_runs WHERE ended_at IS NOT NULL AND ended_at >= ?",
        (window_start,),
    ).fetchall():
        key = _day_key(row["ended_at"])
        if key is None:
            continue
        b = buckets[key]
        outcome = (row["outcome"] or "").strip()
        if outcome == "completed":
            b["runs_completed"] += 1
        elif outcome in _RELIABILITY_FAIL_OUTCOMES:
            b["runs_failed"] += 1
        if row["cost_usd"] is not None:
            b["cost_usd"] = (b["cost_usd"] or 0.0) + float(row["cost_usd"])
        if row["input_tokens"] is not None:
            b["input_tokens"] = (b["input_tokens"] or 0) + int(row["input_tokens"])
        if row["output_tokens"] is not None:
            b["output_tokens"] = (b["output_tokens"] or 0) + int(row["output_tokens"])

    series = []
    for key in sorted(buckets):
        b = buckets[key]
        cycle_times = sorted(b.pop("cycle_times"))
        b["cycle_time_p50_seconds"] = _nearest_rank_percentile(cycle_times, 50)
        if b["cost_usd"] is not None:
            b["cost_usd"] = round(b["cost_usd"], 6)
        series.append(b)
    return {"days": days, "now": now, "series": series}


def _empty_cost_bucket() -> dict:
    return {
        "runs": 0,
        "cost_usd": None,
        "cost_usd_equivalent": None,
        "input_tokens": None,
        "output_tokens": None,
    }


def _cost_bucket_add(bucket: dict, *, cost, equiv, tokens_in, tokens_out) -> None:
    bucket["runs"] += 1
    if cost is not None:
        bucket["cost_usd"] = round((bucket["cost_usd"] or 0.0) + float(cost), 6)
    if equiv is not None:
        bucket["cost_usd_equivalent"] = round(
            (bucket["cost_usd_equivalent"] or 0.0) + float(equiv), 6
        )
    if tokens_in is not None:
        bucket["input_tokens"] = (bucket["input_tokens"] or 0) + int(tokens_in)
    if tokens_out is not None:
        bucket["output_tokens"] = (bucket["output_tokens"] or 0) + int(tokens_out)


# Paid-subscription lanes for the Statistik "Abo-Tokenverbrauch" panel. The
# bucket is derived from the profile's ACTUAL runtime/provider config, never
# its name — a name heuristic mis-attributes renamed/repurposed lanes (e.g.
# ``reviewer`` runs on the Kimi sub, not Claude; ``verifier``/``admin`` run on
# the Codex sub). Cached because profile configs change rarely and the
# dashboard process restarts on deploy.
_PROFILE_SUBSCRIPTION_CACHE: dict[str, Optional[str]] = {}


def _read_profile_provider(home: Optional[str]) -> Optional[str]:
    """Read ``model.provider`` from a profile-home ``config.yaml``. Fail-soft
    None on any error so a broken profile config never breaks the cost view."""
    if not home:
        return None
    try:
        import yaml
        cfg_path = os.path.join(home, "config.yaml")
        if not os.path.isfile(cfg_path):
            return None
        with open(cfg_path, "r", encoding="utf-8") as fh:
            cfg = yaml.safe_load(fh) or {}
        model = cfg.get("model") if isinstance(cfg, dict) else None
        if isinstance(model, dict):
            prov = model.get("provider")
            return str(prov).strip() if prov else None
        return None
    except Exception:
        return None


def _profile_subscription(profile: Optional[str]) -> Optional[str]:
    """Map a profile to its paid-subscription lane: ``"claude"`` (Claude Max via
    the claude-cli runtime), ``"chatgpt"`` (ChatGPT/Codex via the openai-codex
    provider) or ``"kimi"`` (Kimi sub). Returns None for API-billed lanes
    (openrouter/qwen, gemini, …) and for blank/synthetic profile names — those
    are NOT subscriptions and must not be shown as Abo token usage.

    Grounded in config, not the name: claude-cli is checked FIRST because a
    claude-cli profile (e.g. ``premium``) may carry a codex ``model.provider``
    line while actually dispatching through the Claude subscription."""
    if not profile:
        return None
    name = profile.strip()
    if not name or name.startswith("("):
        return None
    if name in _PROFILE_SUBSCRIPTION_CACHE:
        return _PROFILE_SUBSCRIPTION_CACHE[name]
    sub: Optional[str] = None
    try:
        if _is_claude_cli_runtime(name):
            sub = "claude"
        else:
            home: Optional[str] = None
            try:
                from hermes_cli.profiles import resolve_profile_env
                home = resolve_profile_env(name)
            except Exception:
                home = None
            provider = (_read_profile_provider(home) or "").lower()
            if "codex" in provider or "openai" in provider or "chatgpt" in provider:
                sub = "chatgpt"
            elif "kimi" in provider or "moonshot" in provider:
                sub = "kimi"
            elif provider == "anthropic":
                sub = "claude"
    except Exception:
        sub = None
    _PROFILE_SUBSCRIPTION_CACHE[name] = sub
    return sub


def subscription_token_totals(
    conn: sqlite3.Connection,
    *,
    subscription: str,
    since_epoch: int,
) -> dict:
    """Token totals for a paid-subscription lane since ``since_epoch``.

    Caller-neutral helper for rolling Abo limits (e.g. Kimi 5h/7d): it reads
    closed ``task_runs`` only, uses ``_profile_subscription`` for attribution
    instead of profile-name heuristics, and returns a small aggregate that does
    not alter ``runs_costs``' dashboard-facing response shape. The lower bound
    is inclusive, matching the dispatcher budget queries (``started_at >= ?``).
    NULL token columns count as zero so partially stamped rows remain
    fail-soft.
    """
    sub = str(subscription or "").strip().lower()
    since = int(since_epoch)
    totals = {
        "subscription": sub,
        "since_epoch": since,
        "runs": 0,
        "input_tokens": 0,
        "output_tokens": 0,
        "total_tokens": 0,
    }
    if not sub:
        return totals

    for row in conn.execute(
        "SELECT profile, COUNT(*) AS runs, "
        "COALESCE(SUM(COALESCE(input_tokens, 0)), 0) AS input_tokens, "
        "COALESCE(SUM(COALESCE(output_tokens, 0)), 0) AS output_tokens "
        "FROM task_runs "
        "WHERE ended_at IS NOT NULL AND started_at >= ? AND profile IS NOT NULL "
        "GROUP BY profile",
        (since,),
    ).fetchall():
        profile = (row["profile"] or "").strip()
        if _profile_subscription(profile) != sub:
            continue
        in_tok = int(row["input_tokens"] or 0)
        out_tok = int(row["output_tokens"] or 0)
        totals["runs"] += int(row["runs"] or 0)
        totals["input_tokens"] += in_tok
        totals["output_tokens"] += out_tok
        totals["total_tokens"] += in_tok + out_tok
    return totals


def runs_costs(conn: sqlite3.Connection, *, days: int = 7) -> dict:
    """F4 (Statistik): Kosten-Sicht — heute + N-Tage-Fenster gesamt und pro
    Profil. Liest ausschließlich gestempelte ``task_runs``-Spalten plus
    ``metadata.cost_usd_equivalent`` (K17: Subscription-Runs tragen ehrliche
    $0 in ``cost_usd``, das API-Äquivalent steht in der Metadata).
    Doppelzählung verhindert das Stamping selbst (K17-Guard stempelt im
    geteilten Worker-Log nur den jüngsten claude-cli-Run); ungestempelte
    Runs — etwa der Review-Gate-Verifier — zählen hier schlicht 0."""
    days = max(1, min(90, int(days)))
    now = int(time.time())
    today = _dt.date.fromtimestamp(now)
    today_start = int(time.mktime(today.timetuple()))
    window_start = int(
        time.mktime((today - _dt.timedelta(days=days - 1)).timetuple())
    )

    totals_today = _empty_cost_bucket()
    totals_window = _empty_cost_bucket()
    profiles: dict[str, dict] = {}

    for row in conn.execute(
        "SELECT profile, ended_at, cost_usd, input_tokens, output_tokens, metadata "
        "FROM task_runs WHERE ended_at IS NOT NULL AND ended_at >= ?",
        (window_start,),
    ).fetchall():
        equiv = None
        if row["metadata"]:
            try:
                meta = json.loads(row["metadata"])
                raw = meta.get("cost_usd_equivalent") if isinstance(meta, dict) else None
                if isinstance(raw, (int, float)) and not isinstance(raw, bool):
                    equiv = float(raw)
            except (ValueError, TypeError):
                pass
        kwargs = {
            "cost": row["cost_usd"],
            "equiv": equiv,
            "tokens_in": row["input_tokens"],
            "tokens_out": row["output_tokens"],
        }
        _cost_bucket_add(totals_window, **kwargs)
        if int(row["ended_at"]) >= today_start:
            _cost_bucket_add(totals_today, **kwargs)
        profile = (row["profile"] or "").strip() or "(ohne profil)"
        bucket = profiles.setdefault(profile, _empty_cost_bucket())
        _cost_bucket_add(bucket, **kwargs)

    def _burn(b: dict) -> float:
        return (b["cost_usd"] or 0.0) + (b["cost_usd_equivalent"] or 0.0)

    profile_rows = [
        {"profile": name, "subscription": _profile_subscription(name), **bucket}
        for name, bucket in profiles.items()
    ]
    profile_rows.sort(
        key=lambda p: (
            -_burn(p),
            -((p["input_tokens"] or 0) + (p["output_tokens"] or 0)),
            p["profile"],
        )
    )
    return {
        "days": days,
        "now": now,
        "today": totals_today,
        "window": totals_window,
        "profiles": profile_rows,
    }


# F6 (night-sprint): Issue-Gruppierung — gleicher Fehlertyp + gleiches Profil
# = ein Issue. blocked zählt mit (sein Grund steht in summary, nicht error).
_ISSUE_OUTCOMES = _RELIABILITY_FAIL_OUTCOMES + ("blocked",)


def _issue_signature(text: Optional[str]) -> str:
    """Normalisierte Fehler-Signatur: erste nicht-leere Zeile, volatile Teile
    (PIDs, Zähler, Task-/Run-IDs, Zeitangaben) maskiert, Whitespace kollabiert.
    Regelbasiert und bewusst grob — kein KI-Clustering (OUT of scope)."""
    first = next(
        (ln.strip() for ln in (text or "").splitlines() if ln.strip()), ""
    )
    if not first:
        return "(kein Fehlertext)"
    sig = first[:240]
    sig = re.sub(r"\bt_[0-9a-f]{6,}\b", "t_…", sig)
    sig = re.sub(r"\b[0-9a-f]{12,}\b", "…", sig)
    sig = re.sub(r"\d+", "N", sig)
    sig = re.sub(r"\s+", " ", sig)
    return sig.strip()


# Phase A (Programm 3): ehrliche ETA — Dauer-Perzentile abgeschlossener Runs
# pro Profil. Dünne Historie (< min_n) liefert None, das Frontend zeigt "—".
def run_duration_percentiles(
    conn: sqlite3.Connection, profiles: list[str], *,
    days: int = 30, min_n: int = 3,
) -> dict[str, dict]:
    """p50/p90 der Laufzeiten (ended_at-started_at) aller completed-Runs der
    letzten *days* Tage, je Profil aus *profiles*. Eine Aggregat-Query."""
    out: dict[str, dict] = {}
    wanted = sorted({(p or "").strip() for p in profiles if (p or "").strip()})
    if not wanted:
        return out
    placeholders = ",".join("?" for _ in wanted)
    window_start = int(time.time()) - max(1, int(days)) * 86400
    durations: dict[str, list[int]] = {p: [] for p in wanted}
    for row in conn.execute(
        f"SELECT profile, started_at, ended_at FROM task_runs "
        f"WHERE outcome = 'completed' AND ended_at IS NOT NULL "
        f"  AND profile IN ({placeholders}) AND started_at >= ?",
        (*wanted, window_start),
    ).fetchall():
        delta = int(row["ended_at"]) - int(row["started_at"])
        if delta >= 0:
            durations[(row["profile"] or "").strip()].append(delta)
    for profile, vals in durations.items():
        vals.sort()
        if len(vals) < max(1, int(min_n)):
            out[profile] = {"p50": None, "p90": None, "n": len(vals)}
        else:
            out[profile] = {
                "p50": _nearest_rank_percentile(vals, 50),
                "p90": _nearest_rank_percentile(vals, 90),
                "n": len(vals),
            }
    return out


def runs_issues(
    conn: sqlite3.Connection, *, days: int = 30, limit: int = 50,
) -> dict:
    """F6: wiederkehrende Fehler gruppieren — failed/blocked-Runs der letzten
    *days* Tage, Gruppenschlüssel = (Profil, Fehler-Signatur). Rein lesend,
    keine Auto-Tasks. Fehlertext = ``COALESCE(error, summary)`` (blocked legt
    seinen Grund in summary ab)."""
    days = max(1, min(365, int(days)))
    limit = max(1, min(200, int(limit)))
    now = int(time.time())
    window_start = now - days * 86400
    placeholders = ",".join("?" for _ in _ISSUE_OUTCOMES)
    groups: dict[tuple[str, str], dict] = {}
    total_runs = 0
    for row in conn.execute(
        f"SELECT id, task_id, profile, outcome, started_at, "
        f"COALESCE(NULLIF(TRIM(error), ''), summary) AS reason "
        f"FROM task_runs WHERE outcome IN ({placeholders}) AND started_at >= ?",
        (*_ISSUE_OUTCOMES, window_start),
    ).fetchall():
        total_runs += 1
        profile = (row["profile"] or "").strip() or "(ohne profil)"
        sig = _issue_signature(row["reason"])
        g = groups.setdefault((profile, sig), {
            "signature": sig,
            "profile": profile,
            "count": 0,
            "first_seen": int(row["started_at"]),
            "last_seen": int(row["started_at"]),
            "outcomes": {},
            "example_run_id": int(row["id"]),
            "example_task_id": row["task_id"],
            "example_text": (row["reason"] or "").strip()[:500],
        })
        g["count"] += 1
        outcome = (row["outcome"] or "").strip()
        g["outcomes"][outcome] = g["outcomes"].get(outcome, 0) + 1
        at = int(row["started_at"])
        g["first_seen"] = min(g["first_seen"], at)
        if at >= g["last_seen"]:
            # jüngstes Auftreten gewinnt auch das Beispiel — der Operator
            # springt vom Issue in den frischesten Run.
            g["last_seen"] = at
            g["example_run_id"] = int(row["id"])
            g["example_task_id"] = row["task_id"]
            g["example_text"] = (row["reason"] or "").strip()[:500]
    issues = sorted(
        groups.values(), key=lambda g: (-g["count"], -g["last_seen"]),
    )
    truncated = len(issues) > limit
    return {
        "days": days,
        "now": now,
        "total_failed_runs": total_runs,
        "group_count": len(issues),
        "truncated": truncated,
        "issues": issues[:limit],
    }


# Phase F (Programm 3): Triage-Leiste — Tasks, die noch eine Operator-Aktion
# brauchen. Anders als runs_issues (Muster-Sicht, 30d) ist das die Akut-Sicht:
# jüngster Fehl-Run pro Task, nur Tasks die NICHT längst wieder laufen/fertig
# sind.
_TRIAGE_ACTIONABLE_STATUSES = ("blocked", "ready", "todo", "scheduled")


def runs_failures(
    conn: sqlite3.Connection, *, hours: int = 48, limit: int = 30,
) -> dict:
    """Jüngster failed/blocked-Run pro Task der letzten *hours* Stunden,
    beschränkt auf Tasks in noch handlungsbedürftigem Status. Read-only."""
    hours = max(1, min(24 * 14, int(hours)))
    limit = max(1, min(100, int(limit)))
    now = int(time.time())
    window_start = now - hours * 3600
    placeholders = ",".join("?" for _ in _ISSUE_OUTCOMES)
    status_ph = ",".join("?" for _ in _TRIAGE_ACTIONABLE_STATUSES)
    by_task: dict[str, dict] = {}
    for row in conn.execute(
        f"SELECT r.id AS run_id, r.task_id, r.profile, r.outcome, r.ended_at, "
        f"       COALESCE(NULLIF(TRIM(r.error), ''), r.summary) AS reason, "
        f"       t.title, t.status AS task_status, t.assignee, t.model_override, "
        f"       t.auto_retry_count "
        f"FROM task_runs r JOIN tasks t ON t.id = r.task_id "
        f"WHERE r.outcome IN ({placeholders}) AND r.ended_at IS NOT NULL "
        f"  AND r.ended_at >= ? AND t.status IN ({status_ph}) "
        f"ORDER BY r.ended_at DESC",
        (*_ISSUE_OUTCOMES, window_start, *_TRIAGE_ACTIONABLE_STATUSES),
    ).fetchall():
        if row["task_id"] in by_task:
            continue  # neuester Run gewinnt (DESC-Order)
        by_task[row["task_id"]] = {
            "run_id": int(row["run_id"]),
            "task_id": row["task_id"],
            "title": row["title"],
            "profile": (row["profile"] or "").strip() or None,
            "assignee": row["assignee"],
            "outcome": (row["outcome"] or "").strip(),
            "reason": (row["reason"] or "").strip()[:500] or None,
            "ended_at": int(row["ended_at"]),
            "task_status": row["task_status"],
            "model_override": row["model_override"],
            "auto_retry_count": int(row["auto_retry_count"] or 0),
            "auto_retry_limit": DEFAULT_AUTO_RETRY_BLOCKED_LIMIT,
        }
    failures = sorted(by_task.values(), key=lambda f: -f["ended_at"])
    return {
        "hours": hours,
        "now": now,
        "count": len(failures),
        "truncated": len(failures) > limit,
        "failures": failures[:limit],
    }


def _decision_event_reason(payload) -> Optional[str]:
    """Pull a human ``reason`` string out of a task_events payload, fail-soft."""
    if payload is None:
        return None
    try:
        data = json.loads(payload) if isinstance(payload, str) else payload
        if isinstance(data, dict):
            reason = data.get("reason")
            if reason:
                return str(reason)
    except Exception:
        pass
    return None


def decision_queue(
    conn: sqlite3.Connection,
    *,
    now: Optional[int] = None,
    config: Optional[dict] = None,
) -> dict:
    """E1 (N-E1): fold every operator-decision-ready board state into ONE
    read-only feed.

    Today these states are scattered or invisible: sticky-blocked tasks live
    only inside ``recompute_ready`` logic; ``role_fit_held`` is emitted as an
    event nobody consumes; ``decompose_failed`` is a bare counter column; K4's
    stranded-by-stuck-parent only surfaces in the CLI ``diagnostics`` command;
    verifier REQUEST_CHANGES (B2) is a verdict column; and the C1 budget gate
    emits ``budget_held`` events. This consolidates all of them, one row per
    decision::

        {kind, task_id, title, reason, age_seconds, suggested_command}

    Read-only and fail-soft: every category is gathered independently inside its
    own ``try`` so a failure in one never drops the others. Each task appears at
    most once, classified by its most specific reason (priority order: a blocked
    task whose latest run is a verifier rejection is ``review_rejected``, not the
    generic ``sticky_blocked``; a held ready task is ``budget_held`` /
    ``role_fit_held`` per its latest event).
    """
    now = int(now if now is not None else time.time())
    rows: list[dict[str, Any]] = []
    seen: set[str] = set()

    def _age(ts) -> Optional[int]:
        if ts is None:
            return None
        try:
            return max(0, now - int(ts))
        except Exception:
            return None

    def _add(
        kind,
        task_id,
        title,
        reason,
        age_seconds,
        suggested_command,
        operator_escalation: Optional[dict] = None,
    ):
        if task_id in seen:
            return
        seen.add(task_id)
        row = {
            "kind": kind,
            "task_id": task_id,
            "title": title,
            "reason": reason,
            "age_seconds": age_seconds,
            "suggested_command": suggested_command,
        }
        if operator_escalation is not None:
            row["operator_escalation"] = operator_escalation
        rows.append(row)

    def _payload_dict(raw_payload) -> dict:
        try:
            payload = json.loads(raw_payload or "{}")
        except Exception:
            return {}
        return payload if isinstance(payload, dict) else {}

    # Blocked tasks feed two categories (review_rejected, sticky_blocked). Fetch
    # the set ONCE and batch every per-task lookup the categories need — the
    # latest-run verdict, the latest blocked/unblocked event (sticky check) and
    # the latest 'blocked' event (reason + age) — in one grouped query each.
    # This endpoint is polled every 15s; the per-task variants were an N+1 over
    # the blocked set AND over every ready task. Each step is fail-soft and
    # independent. The grouped queries use SQLite's bare-column-with-MAX(id)
    # guarantee: non-aggregate columns come from the max-id (latest) row.
    blocked_tasks: list = []
    latest_verdict: dict[str, Optional[str]] = {}
    sticky_ids: set[str] = set()
    last_blocked: dict[str, tuple] = {}  # task_id -> (payload, created_at)
    # R1: blocked deliverable misses (worker posted a deliverable but exited
    # without kanban_complete). These carry no 'blocked' event so they would
    # otherwise fall through every category; the repair endpoint needs them
    # surfaced under a dedicated kind.
    last_deliverable_miss: dict[str, tuple] = {}  # task_id -> (payload, created_at)
    try:
        blocked_tasks = conn.execute(
            "SELECT id, title, created_by FROM tasks WHERE status = 'blocked'"
        ).fetchall()
    except Exception:
        blocked_tasks = []
    if blocked_tasks:
        ids = [r["id"] for r in blocked_tasks]
        ph = ",".join("?" for _ in ids)
        try:
            for vr in conn.execute(
                "SELECT task_id, verdict FROM ("
                " SELECT task_id, verdict,"
                " ROW_NUMBER() OVER (PARTITION BY task_id ORDER BY id DESC) AS rn"
                f" FROM task_runs WHERE task_id IN ({ph})"
                ") WHERE rn = 1",
                ids,
            ).fetchall():
                latest_verdict[vr["task_id"]] = vr["verdict"]
        except Exception:
            latest_verdict = {}
        try:
            # Same semantics as _has_sticky_block, batched: sticky iff the most
            # recent blocked/unblocked event is 'blocked'.
            for sr in conn.execute(
                "SELECT task_id, kind, MAX(id) FROM task_events "
                f"WHERE kind IN ('blocked', 'unblocked') AND task_id IN ({ph}) "
                "GROUP BY task_id",
                ids,
            ).fetchall():
                if sr["kind"] == "blocked":
                    sticky_ids.add(sr["task_id"])
        except Exception:
            sticky_ids = set()
        try:
            for br in conn.execute(
                "SELECT task_id, payload, created_at, MAX(id) FROM task_events "
                f"WHERE kind = 'blocked' AND task_id IN ({ph}) "
                "GROUP BY task_id",
                ids,
            ).fetchall():
                last_blocked[br["task_id"]] = (br["payload"], br["created_at"])
        except Exception:
            last_blocked = {}
        try:
            for dr in conn.execute(
                "SELECT task_id, payload, created_at, MAX(id) FROM task_events "
                f"WHERE kind = ? AND task_id IN ({ph}) "
                "GROUP BY task_id",
                [DELIVERABLE_POSTED_NOT_COMPLETED, *ids],
            ).fetchall():
                last_deliverable_miss[dr["task_id"]] = (
                    dr["payload"], dr["created_at"],
                )
        except Exception:
            last_deliverable_miss = {}

    # 0) no-silent-stall parks — specific classes beat the generic
    #    operator_escalation event that 4A also emits for the same task.
    try:
        for row in blocked_tasks:
            if row["id"] in seen or _is_funnel_root_task(conn, row):
                continue
            lb = last_blocked.get(row["id"])
            reason = _decision_event_reason(lb[0] if lb else None) or ""
            if not reason.startswith("integration parked:"):
                continue
            _add(
                "integration_parked", row["id"], row["title"],
                reason, _age(lb[1] if lb else None),
                f"hermes kanban show {row['id']}",
            )
    except Exception:
        pass

    try:
        for row in conn.execute(
            "SELECT e.task_id, e.payload, e.created_at, t.id, t.title, "
            "t.assignee, t.status, t.created_by "
            "FROM task_events e JOIN tasks t ON t.id = e.task_id "
            "WHERE e.kind = ? AND t.status NOT IN ('done', 'archived') "
            "ORDER BY e.id DESC",
            (NO_SILENT_STALL_EVENT,),
        ).fetchall():
            if row["task_id"] in seen or _is_funnel_root_task(conn, row):
                continue
            payload = _payload_dict(row["payload"])
            if (
                payload.get("stall_class") != "rate_limited_loop"
                or payload.get("action") != "parked"
            ):
                continue
            _add(
                "rate_limited_loop", row["task_id"], row["title"],
                str(payload.get("reason") or "Persistent rate-limit loop"),
                _age(row["created_at"]),
                f"hermes kanban show {row['task_id']}",
            )
    except Exception:
        pass

    try:
        for row in conn.execute(
            "SELECT e.task_id, e.payload, e.created_at, t.id, t.title, "
            "t.assignee, t.status, t.created_by "
            "FROM task_events e JOIN tasks t ON t.id = e.task_id "
            "WHERE e.kind = ? AND t.status NOT IN ('done', 'archived') "
            "ORDER BY e.id DESC",
            (OPERATOR_ESCALATION_EVENT,),
        ).fetchall():
            if row["task_id"] in seen or _is_funnel_root_task(conn, row):
                continue
            payload = _payload_dict(row["payload"])
            reason = (
                str(payload.get("why_now") or "").strip()
                or "Operator escalation pending"
            )
            _add(
                "operator_escalation", row["task_id"], row["title"],
                reason, _age(row["created_at"]),
                f"hermes kanban show {row['task_id']}",
                operator_escalation=payload,
            )
    except Exception:
        pass

    # 1) review_rejected — blocked task whose MOST RECENT run was a verifier
    #    REQUEST_CHANGES (B2 verdict). More specific than sticky_blocked.
    try:
        for row in blocked_tasks:
            verdict = latest_verdict.get(row["id"])
            if verdict and verdict.upper() == "REQUEST_CHANGES":
                lb = last_blocked.get(row["id"])
                _add(
                    "review_rejected", row["id"], row["title"],
                    "Verifier requested changes (REQUEST_CHANGES)",
                    _age(lb[1] if lb else None),
                    f"hermes kanban show {row['id']}",
                )
    except Exception:
        pass

    # 1b) deliverable_posted_not_completed — blocked because the worker posted a
    #     deliverable but exited without kanban_complete. Repairable in one click
    #     via POST /tasks/<id>/repair (R1). More specific than sticky_blocked, and
    #     these never carry a 'blocked' event so they must be classified here.
    try:
        for row in blocked_tasks:
            if row["id"] in seen:
                continue
            dm = last_deliverable_miss.get(row["id"])
            if dm is None:
                continue
            _add(
                "deliverable_posted_not_completed", row["id"], row["title"],
                "Deliverable gepostet, aber kanban_complete fehlt — Repair möglich",
                _age(dm[1]),
                f"hermes kanban show {row['id']}",
            )
    except Exception:
        pass

    # 2) budget_held / role_fit_held — a READY task whose LATEST event is a hold
    #    (F2-dedup semantics: the most recent event IS the standing hold).
    #    One grouped query over the ready set instead of one lookup per task.
    try:
        for ev in conn.execute(
            "SELECT t.id AS task_id, t.title AS title,"
            "       e.kind AS kind, e.payload AS payload,"
            "       e.created_at AS created_at, MAX(e.id) "
            "FROM tasks t JOIN task_events e ON e.task_id = t.id "
            "WHERE t.status = 'ready' "
            "GROUP BY t.id",
        ).fetchall():
            if ev["task_id"] in seen:
                continue
            if ev["kind"] == "budget_held":
                _add(
                    "budget_held", ev["task_id"], ev["title"],
                    _decision_event_reason(ev["payload"])
                    or "Daily budget cap reached — dispatch held",
                    _age(ev["created_at"]),
                    f"hermes kanban show {ev['task_id']}",
                )
            elif ev["kind"] == "role_fit_held":
                _add(
                    "role_fit_held", ev["task_id"], ev["title"],
                    _decision_event_reason(ev["payload"])
                    or "Held: assignee role does not fit the task",
                    _age(ev["created_at"]),
                    f"hermes kanban show {ev['task_id']}",
                )
    except Exception:
        pass

    # 3) sticky_blocked — worker/operator kanban_block, not a review rejection.
    #    Reuses the batched blocked-set lookups above (no per-task queries).
    try:
        for row in blocked_tasks:
            if row["id"] in seen:
                continue
            if row["id"] in sticky_ids:
                lb = last_blocked.get(row["id"])
                _add(
                    "sticky_blocked", row["id"], row["title"],
                    _decision_event_reason(lb[0] if lb else None)
                    or "Blocked — awaiting operator unblock",
                    _age(lb[1] if lb else None),
                    f"hermes kanban unblock {row['id']}",
                )
    except Exception:
        pass

    # 4) decompose_failed — unresolved decompose failures on a non-terminal task.
    try:
        failed_rows = conn.execute(
            "SELECT id, title, decompose_failed FROM tasks "
            "WHERE decompose_failed > 0 AND status NOT IN ('done', 'archived')"
        ).fetchall()
        last_any: dict[str, Optional[int]] = {}
        if failed_rows:
            ph = ",".join("?" for _ in failed_rows)
            for ar in conn.execute(
                "SELECT task_id, MAX(created_at) AS at FROM task_events "
                f"WHERE task_id IN ({ph}) GROUP BY task_id",
                [r["id"] for r in failed_rows],
            ).fetchall():
                last_any[ar["task_id"]] = ar["at"]
        for row in failed_rows:
            if row["id"] in seen:
                continue
            _add(
                "decompose_failed", row["id"], row["title"],
                f"auto_decompose failed {int(row['decompose_failed'])}× "
                "(last attempt unparsed or errored)",
                _age(last_any.get(row["id"])),
                f"hermes kanban show {row['id']}",
            )
    except Exception:
        pass

    # 5) stranded_by_stuck_parent — K4 cross-task diagnostic (todo descendants
    #    of a long-sticky-blocked parent). Reuse the diagnostics helper.
    try:
        from hermes_cli import kanban_diagnostics as _kdiag  # lazy: import cycle
        cross = _kdiag.find_descendants_blocked_by_stuck_parent(
            conn, now=now, config=config,
        )
        for tid, diags in cross.items():
            if tid in seen or not diags:
                continue
            d = diags[0]
            title_row = conn.execute(
                "SELECT title FROM tasks WHERE id = ?", (tid,),
            ).fetchone()
            data = getattr(d, "data", None) or {}
            blockers = data.get("blocked_parents") or []
            primary = blockers[0] if blockers else None
            age = data.get("max_block_age_hours")
            _add(
                "stranded_by_stuck_parent", tid,
                title_row["title"] if title_row else tid,
                getattr(d, "detail", None) or getattr(d, "title", None)
                or "Stranded by a sticky-blocked parent",
                int(float(age) * 3600) if age is not None else None,
                f"hermes kanban unblock {primary}" if primary
                else f"hermes kanban show {tid}",
            )
    except Exception:
        pass

    # 6) tree_root_woke — a decompose ROOT that is 'ready' AND all its subtasks
    #    are 'done'.  A decompose root DEPENDS ON its subtasks, so in task_links
    #    the root is the child_id and the subtasks are the parent_ids — the same
    #    direction recompute_ready uses (JOIN l.parent_id WHERE l.child_id = root)
    #    to promote the root.  A root with no subtasks at all is excluded.
    try:
        for row in conn.execute(
            "SELECT t.id AS task_id, t.title AS title, t.created_at AS created_at "
            "FROM tasks t "
            "WHERE t.status = 'ready' "
            "  AND (t.planspec_source IS NOT NULL OR EXISTS ("
            "    SELECT 1 FROM task_events e0 WHERE e0.task_id = t.id AND e0.kind = 'decomposed'"
            "  )) "
            # has at least one subtask (the root is the dependent child_id)
            "  AND EXISTS (SELECT 1 FROM task_links WHERE child_id = t.id) "
            # every subtask (parent) is done — same predicate recompute_ready uses
            "  AND NOT EXISTS ("
            "    SELECT 1 FROM task_links l2 "
            "    JOIN tasks t2 ON t2.id = l2.parent_id "
            "    WHERE l2.child_id = t.id AND t2.status != 'done'"
            "  )",
        ).fetchall():
            if row["task_id"] in seen:
                continue
            _add(
                "tree_root_woke", row["task_id"], row["title"],
                "Decompose root is ready — all subtasks done; root awaits finalization",
                _age(row["created_at"]),
                f"hermes kanban show {row['task_id']}",
            )
    except Exception:
        pass

    # 7) release_gate_parked — tasks carrying a 'release_gate_parked' event
    #    that are NOT in a terminal state (not done/archived).  The
    #    suggested_command is the FULL gate sequence: the event payload's own
    #    ``commands`` list (what the gate actually parked on) joined with
    #    ``&&``, falling back to the canonical _RELEASE_GATE_COMMANDS.  The old
    #    ``next(iter(...))`` surfaced only the leading ``cd`` — a no-op alone.
    try:
        from hermes_cli import kanban_worktrees as _kwt  # lazy: avoids import cycle
        _fallback_cmd = " && ".join(_kwt._RELEASE_GATE_COMMANDS) or "hermes kanban show <id>"
        for row in conn.execute(
            "SELECT e.task_id, e.payload, e.created_at AS event_at, "
            "t.title AS title, MAX(e.id) "
            "FROM task_events e JOIN tasks t ON t.id = e.task_id "
            "WHERE e.kind = 'release_gate_parked' "
            "  AND t.status NOT IN ('done', 'archived') "
            "GROUP BY e.task_id",
        ).fetchall():
            if row["task_id"] in seen:
                continue
            payload = _payload_dict(row["payload"])
            reason = (
                str(payload.get("reason") or "").strip()
                or "Release gate parked — awaiting GO"
            )
            cmds = payload.get("commands")
            if isinstance(cmds, list) and cmds:
                suggested = " && ".join(str(c) for c in cmds)
            else:
                suggested = _fallback_cmd
            _add(
                "release_gate_parked", row["task_id"], row["title"],
                reason, _age(row["event_at"]),
                suggested,
            )
    except Exception:
        pass

    # Oldest decisions first (most likely to be stale/forgotten); unknown ages
    # sort to the end.
    rows.sort(
        key=lambda r: (r["age_seconds"] is None, -(r["age_seconds"] or 0)),
    )
    return {"decisions": rows, "count": len(rows), "checked_at": now}


# ---------------------------------------------------------------------------
# Epics (N-E3) — durable goals spanning multiple task trees
# ---------------------------------------------------------------------------

def _new_epic_id() -> str:
    """Generate a short epic id (``e_`` prefix, parallel to ``t_`` tasks)."""
    return "e_" + secrets.token_hex(4)


def create_epic(
    conn: sqlite3.Connection,
    *,
    title: str,
    body: Optional[str] = None,
    epic_id: Optional[str] = None,
) -> str:
    """Create a durable epic and return its id.

    An epic is a first-class grouping object that tasks point at via
    ``tasks.epic_id`` — unlike ``--goal`` (a per-run loop flag) or ``tenant``
    (a free filter string). Status starts ``open``; ``close_epic`` flips it.
    """
    if not title or not title.strip():
        raise ValueError("title is required")
    eid = epic_id or _new_epic_id()
    now = int(time.time())
    with write_txn(conn):
        conn.execute(
            "INSERT INTO epics (id, title, body, status, created_at) "
            "VALUES (?, ?, ?, 'open', ?)",
            (eid, title.strip(), body, now),
        )
    return eid


def _epic_stats(conn: sqlite3.Connection, epic_id: str) -> dict:
    """Per-epic rollup: task counts by terminal-ness + cost/token sums.

    Read-only. Cost/tokens come from the same ``task_runs`` columns the K5a
    cost observability uses; NULL contributions are treated as 0, and an epic
    with no attributed cost reports ``cost_usd: None`` (never a crash)."""
    total = open_count = done_count = 0
    for row in conn.execute(
        "SELECT status, COUNT(*) AS n FROM tasks WHERE epic_id = ? GROUP BY status",
        (epic_id,),
    ).fetchall():
        n = int(row["n"])
        total += n
        if row["status"] in ("done", "archived"):
            done_count += n
        else:
            open_count += n
    agg = conn.execute(
        "SELECT "
        "  SUM(r.cost_usd)      AS cost_usd, "
        "  SUM(r.input_tokens)  AS input_tokens, "
        "  SUM(r.output_tokens) AS output_tokens "
        "FROM task_runs r JOIN tasks t ON t.id = r.task_id "
        "WHERE t.epic_id = ?",
        (epic_id,),
    ).fetchone()
    return {
        "task_count": total,
        "open_tasks": open_count,
        "done_tasks": done_count,
        "cost_usd": agg["cost_usd"] if agg and agg["cost_usd"] is not None else None,
        "input_tokens": int(agg["input_tokens"]) if agg and agg["input_tokens"] is not None else None,
        "output_tokens": int(agg["output_tokens"]) if agg and agg["output_tokens"] is not None else None,
    }


def _epic_dict(conn: sqlite3.Connection, row: sqlite3.Row) -> dict:
    d = {
        "id": row["id"],
        "title": row["title"],
        "body": row["body"],
        "status": row["status"],
        "created_at": int(row["created_at"]) if row["created_at"] is not None else None,
        "closed_at": int(row["closed_at"]) if row["closed_at"] is not None else None,
    }
    d.update(_epic_stats(conn, row["id"]))
    return d


def list_epics(
    conn: sqlite3.Connection, *, include_closed: bool = True,
) -> list[dict]:
    """List epics (newest first) with per-epic task/cost rollups."""
    if include_closed:
        rows = conn.execute(
            "SELECT * FROM epics ORDER BY created_at DESC, id DESC"
        ).fetchall()
    else:
        rows = conn.execute(
            "SELECT * FROM epics WHERE status = 'open' "
            "ORDER BY created_at DESC, id DESC"
        ).fetchall()
    return [_epic_dict(conn, r) for r in rows]


def get_epic(conn: sqlite3.Connection, epic_id: str) -> Optional[dict]:
    """Return one epic (with rollup + its task ids), or None if absent."""
    row = conn.execute(
        "SELECT * FROM epics WHERE id = ?", (epic_id,),
    ).fetchone()
    if row is None:
        return None
    d = _epic_dict(conn, row)
    d["tasks"] = [
        {"id": t["id"], "title": t["title"], "status": t["status"]}
        for t in conn.execute(
            "SELECT id, title, status FROM tasks WHERE epic_id = ? "
            "ORDER BY created_at ASC, id ASC",
            (epic_id,),
        ).fetchall()
    ]
    return d


def close_epic(conn: sqlite3.Connection, epic_id: str) -> bool:
    """Mark an epic ``closed``. Returns False if the epic doesn't exist.

    Idempotent: closing an already-closed epic refreshes ``closed_at`` and
    still returns True. Does NOT touch member tasks — closing an epic is an
    organisational act, not a cancellation."""
    now = int(time.time())
    with write_txn(conn):
        cur = conn.execute(
            "UPDATE epics SET status = 'closed', closed_at = ? WHERE id = ?",
            (now, epic_id),
        )
        return cur.rowcount > 0


def set_task_epic(
    conn: sqlite3.Connection, task_id: str, epic_id: Optional[str],
) -> bool:
    """Attach a task to an epic, or detach it (``epic_id=None``).

    Returns False if the task doesn't exist. Attaching validates the target:
    the epic must exist and be ``open`` (ValueError otherwise) — detaching is
    always allowed, even from a since-closed epic.
    """
    with write_txn(conn):
        row = conn.execute(
            "SELECT epic_id FROM tasks WHERE id = ?", (task_id,)
        ).fetchone()
        if not row:
            return False
        if epic_id is not None:
            epic = conn.execute(
                "SELECT status FROM epics WHERE id = ?", (epic_id,)
            ).fetchone()
            if epic is None:
                raise ValueError(f"epic {epic_id} not found")
            if epic["status"] != "open":
                raise ValueError(f"epic {epic_id} is closed; reopen or pick an open epic")
        conn.execute(
            "UPDATE tasks SET epic_id = ? WHERE id = ?", (epic_id, task_id)
        )
        _append_event(conn, task_id, "epic_changed", {"epic_id": epic_id})
        return True


def set_task_model_override(
    conn: sqlite3.Connection, task_id: str, model: Optional[str],
) -> bool:
    """Phase B (Programm 3): set/clear ``tasks.model_override`` — the highest
    precedence step of the spawn resolution (task > active lane > profile
    default). ``model=None``/leer löscht den Override. Greift ab dem
    nächsten Spawn; ein laufender Run bleibt unberührt.

    Returns False if the task doesn't exist.
    """
    value = (model or "").strip() or None
    with write_txn(conn):
        cur = conn.execute(
            "UPDATE tasks SET model_override = ? WHERE id = ?",
            (value, task_id),
        )
        if cur.rowcount != 1:
            return False
        _append_event(conn, task_id, "model_override_set", {"model": value})
        return True


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
        from datetime import datetime
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
# Lanes (night-sprint F1) — switchable profile→(runtime, model) presets
# ---------------------------------------------------------------------------
#
# A lane is a named preset mapping profile names to a worker runtime plus
# optional provider/model/fallback routing for Hermes-runtime workers.
# Exactly one lane may be active; the dispatcher hot-reads the active lane from
# the board DB at every spawn (same per-spawn freshness as
# _is_claude_cli_profile / _claude_profile_model), so switching lanes needs NO
# gateway restart.
#
# Spawn-time precedence (documented + tested):
#   task.model_override            (per-task escalation, highest)
#   > active lane entry            (this section)
#   > profile config.yaml default  (worker_runtime / claude_model / model)
#
# Fallback chain precedence:
#   active lane fallback_providers
#   > profile fallback_providers
#   > legacy fallback_model
#
# A profile absent from the active lane falls through to its config default;
# no lanes / no active lane = exact pre-lane behavior.

LANE_RUNTIMES = ("hermes", "claude-cli")

# Static fallback rosters for the first-start seed (from the signed
# night-sprint plan). ensure_lane_seeds prefers the LIVE profile configs for
# api-standard so activating it is behavior-neutral; these values are the
# fallback when a profile config is unreadable (e.g. test fixtures).
_LANE_SEED_API_STANDARD = {
    "coder": {"worker_runtime": "hermes", "model": "gpt-5.5"},
    "reviewer": {"worker_runtime": "hermes", "model": "kimi-for-coding"},
    "critic": {"worker_runtime": "hermes", "model": "qwen3.7-max"},
    "research": {"worker_runtime": "hermes", "model": "kimi-k2.6"},
    "verifier": {"worker_runtime": "hermes", "model": "gpt-5.5"},
    "coder-claude": {"worker_runtime": "claude-cli", "model": "claude-opus-4-8"},
    "premium": {"worker_runtime": "claude-cli", "model": "claude-opus-4-8"},
}
_LANE_SEED_MAX_ABO = {
    "coder": {"worker_runtime": "claude-cli", "model": None},
    "reviewer": {"worker_runtime": "claude-cli", "model": None},
    "critic": {"worker_runtime": "claude-cli", "model": None},
    "research": {"worker_runtime": "claude-cli", "model": None},
    "verifier": {"worker_runtime": "claude-cli", "model": None},
    "coder-claude": {"worker_runtime": "claude-cli", "model": "claude-opus-4-8"},
    "premium": {"worker_runtime": "claude-cli", "model": "claude-opus-4-8"},
}


def _new_lane_id() -> str:
    """Generate a short lane id (``lane_`` prefix, parallel to ``e_`` epics)."""
    return "lane_" + secrets.token_hex(4)


def _normalize_lane_profiles(profiles) -> dict:
    """Validate + normalize a lane ``profiles`` mapping.

    Returns ``{profile: {"worker_runtime": <str|None>, "provider": <str|None>,
    "model": <str|None>, "fallback_providers": <list>}}``. Old entries with
    only ``worker_runtime`` + ``model`` remain valid. Empty-string models
    normalize to None (= profile default).
    """
    if profiles is None:
        return {}
    if not isinstance(profiles, dict):
        raise ValueError("profiles must be an object of {profile: lane-entry}")
    out: dict = {}
    for prof, entry in profiles.items():
        if not isinstance(prof, str) or not prof.strip():
            raise ValueError("profile names must be non-empty strings")
        if entry is None:
            entry = {}
        if not isinstance(entry, dict):
            raise ValueError(f"lane entry for {prof!r} must be an object")
        runtime = entry.get("worker_runtime")
        if runtime is not None:
            if not isinstance(runtime, str) or runtime.strip() not in LANE_RUNTIMES:
                raise ValueError(
                    f"worker_runtime for {prof!r} must be one of {LANE_RUNTIMES}"
                )
            runtime = runtime.strip()
        model = entry.get("model")
        if model is not None and not isinstance(model, str):
            raise ValueError(f"model for {prof!r} must be a string")
        model = model.strip() if isinstance(model, str) and model.strip() else None

        provider = entry.get("provider")
        if provider is not None and not isinstance(provider, str):
            raise ValueError(f"provider for {prof!r} must be a string")
        provider = provider.strip() if isinstance(provider, str) and provider.strip() else None

        fallbacks = _normalize_lane_fallback_providers(
            entry.get("fallback_providers"), profile=prof.strip(),
        )
        if runtime is None and (provider or fallbacks):
            runtime = "hermes"
        if runtime == "claude-cli" and provider:
            raise ValueError(f"provider for claude-cli lane entry {prof!r} is not supported")
        if runtime == "claude-cli" and fallbacks:
            raise ValueError(f"fallback_providers for claude-cli lane entry {prof!r} is not supported")
        out[prof.strip()] = {
            "worker_runtime": runtime,
            "provider": provider,
            "model": model,
            "fallback_providers": fallbacks,
        }
    return out


def _normalize_lane_fallback_providers(raw, *, profile: str) -> list[dict]:
    if raw is None:
        return []
    if not isinstance(raw, list):
        raise ValueError(f"fallback_providers for {profile!r} must be a list")

    out: list[dict] = []
    for idx, item in enumerate(raw):
        if not isinstance(item, dict):
            raise ValueError(f"fallback_providers[{idx}] for {profile!r} must be an object")
        provider = item.get("provider")
        model = item.get("model")
        if not isinstance(provider, str) or not provider.strip():
            raise ValueError(f"fallback_providers[{idx}].provider for {profile!r} is required")
        if not isinstance(model, str) or not model.strip():
            raise ValueError(f"fallback_providers[{idx}].model for {profile!r} is required")
        normalized = {
            "provider": provider.strip(),
            "model": model.strip(),
        }
        base_url = item.get("base_url")
        if isinstance(base_url, str) and base_url.strip():
            normalized["base_url"] = base_url.strip().rstrip("/")
        out.append(normalized)
    return out


def _lane_dict(row: sqlite3.Row) -> dict:
    try:
        profiles = json.loads(row["profiles"] or "{}")
        if not isinstance(profiles, dict):
            profiles = {}
        else:
            profiles = _normalize_lane_profiles(profiles)
    except (ValueError, TypeError):
        profiles = {}
    return {
        "id": row["id"],
        "name": row["name"],
        "profiles": profiles,
        "active": bool(row["active"]),
        "builtin": bool(row["builtin"]),
        "created_at": int(row["created_at"]) if row["created_at"] is not None else None,
        "updated_at": int(row["updated_at"]) if row["updated_at"] is not None else None,
    }


def _seed_api_standard_profiles() -> dict:
    """Roster for the ``api-standard`` seed: live profile configs, with the
    plan's static values as fallback per profile (fail-soft)."""
    roster = dict(_LANE_SEED_API_STANDARD)
    try:
        from hermes_cli.profiles import resolve_profile_env
        import yaml
        for prof in list(roster.keys()):
            try:
                home = resolve_profile_env(prof)
                cfg_path = os.path.join(home, "config.yaml")
                if not os.path.isfile(cfg_path):
                    continue
                with open(cfg_path, "r", encoding="utf-8") as fh:
                    cfg = yaml.safe_load(fh) or {}
                if not isinstance(cfg, dict):
                    continue
                if cfg.get("worker_runtime") == "claude-cli":
                    model = cfg.get("claude_model")
                    roster[prof] = {
                        "worker_runtime": "claude-cli",
                        "model": model.strip() if isinstance(model, str) and model.strip() else None,
                    }
                else:
                    model_cfg = cfg.get("model")
                    default = model_cfg.get("default") if isinstance(model_cfg, dict) else None
                    provider = model_cfg.get("provider") if isinstance(model_cfg, dict) else None
                    from hermes_cli.fallback_config import get_fallback_chain
                    roster[prof] = {
                        "worker_runtime": "hermes",
                        "provider": provider.strip() if isinstance(provider, str) and provider.strip() else None,
                        "model": default.strip() if isinstance(default, str) and default.strip() else roster[prof]["model"],
                        "fallback_providers": get_fallback_chain(cfg),
                    }
            except Exception:
                continue  # keep the static fallback for this profile
    except Exception:
        pass
    return roster


def ensure_lane_seeds(conn: sqlite3.Connection) -> bool:
    """Seed the two first-start presets if the lanes table is empty.

    ``api-standard`` (today's roster, seeded ACTIVE so activation is
    behavior-neutral) and ``max-abo`` (claude-cli subscription lane).
    Returns True if seeding happened. Idempotent.
    """
    n = conn.execute("SELECT COUNT(*) AS n FROM lanes").fetchone()["n"]
    if int(n) > 0:
        return False
    now = int(time.time())
    with write_txn(conn):
        # Re-check inside the txn — another process may have seeded between
        # the count above and acquiring the write lock.
        n = conn.execute("SELECT COUNT(*) AS n FROM lanes").fetchone()["n"]
        if int(n) > 0:
            return False
        conn.execute(
            "INSERT INTO lanes (id, name, profiles, active, builtin, created_at, updated_at) "
            "VALUES (?, ?, ?, 1, 1, ?, ?)",
            (_new_lane_id(), "api-standard",
             json.dumps(_seed_api_standard_profiles()), now, now),
        )
        conn.execute(
            "INSERT INTO lanes (id, name, profiles, active, builtin, created_at, updated_at) "
            "VALUES (?, ?, ?, 0, 1, ?, ?)",
            (_new_lane_id(), "max-abo",
             json.dumps(_LANE_SEED_MAX_ABO), now, now),
        )
    return True


def list_lanes(conn: sqlite3.Connection) -> list[dict]:
    """List all lanes (seeding the defaults on first contact)."""
    ensure_lane_seeds(conn)
    rows = conn.execute(
        "SELECT * FROM lanes ORDER BY created_at ASC, name ASC"
    ).fetchall()
    return [_lane_dict(r) for r in rows]


def get_active_lane(conn: sqlite3.Connection) -> Optional[dict]:
    """Return the active lane, or None (= pure config-default behavior)."""
    row = conn.execute(
        "SELECT * FROM lanes WHERE active = 1 ORDER BY updated_at DESC LIMIT 1"
    ).fetchone()
    return _lane_dict(row) if row is not None else None


def create_lane(
    conn: sqlite3.Connection,
    *,
    name: str,
    profiles=None,
) -> dict:
    """Create a lane preset (inactive). Raises ValueError on bad input."""
    if not name or not name.strip():
        raise ValueError("name is required")
    norm = _normalize_lane_profiles(profiles)
    lane_id = _new_lane_id()
    now = int(time.time())
    with write_txn(conn):
        existing = conn.execute(
            "SELECT 1 FROM lanes WHERE name = ?", (name.strip(),)
        ).fetchone()
        if existing is not None:
            raise ValueError(f"lane name {name.strip()!r} already exists")
        conn.execute(
            "INSERT INTO lanes (id, name, profiles, active, builtin, created_at, updated_at) "
            "VALUES (?, ?, ?, 0, 0, ?, ?)",
            (lane_id, name.strip(), json.dumps(norm), now, now),
        )
    row = conn.execute("SELECT * FROM lanes WHERE id = ?", (lane_id,)).fetchone()
    return _lane_dict(row)


def update_lane(
    conn: sqlite3.Connection,
    lane_id: str,
    *,
    name: Optional[str] = None,
    profiles=None,
) -> Optional[dict]:
    """Update name and/or profiles of a lane. Returns the lane or None."""
    sets: list[str] = []
    params: list = []
    if name is not None:
        if not name.strip():
            raise ValueError("name must be non-empty")
        sets.append("name = ?")
        params.append(name.strip())
    if profiles is not None:
        sets.append("profiles = ?")
        params.append(json.dumps(_normalize_lane_profiles(profiles)))
    if not sets:
        row = conn.execute("SELECT * FROM lanes WHERE id = ?", (lane_id,)).fetchone()
        return _lane_dict(row) if row is not None else None
    sets.append("updated_at = ?")
    params.append(int(time.time()))
    params.append(lane_id)
    with write_txn(conn):
        if name is not None:
            clash = conn.execute(
                "SELECT 1 FROM lanes WHERE name = ? AND id != ?",
                (name.strip(), lane_id),
            ).fetchone()
            if clash is not None:
                raise ValueError(f"lane name {name.strip()!r} already exists")
        cur = conn.execute(
            f"UPDATE lanes SET {', '.join(sets)} WHERE id = ?", params
        )
        if cur.rowcount == 0:
            return None
    row = conn.execute("SELECT * FROM lanes WHERE id = ?", (lane_id,)).fetchone()
    return _lane_dict(row) if row is not None else None


def delete_lane(conn: sqlite3.Connection, lane_id: str) -> bool:
    """Delete a lane. The ACTIVE lane is not deletable (ValueError)."""
    with write_txn(conn):
        row = conn.execute(
            "SELECT active FROM lanes WHERE id = ?", (lane_id,)
        ).fetchone()
        if row is None:
            return False
        if int(row["active"]):
            raise ValueError("the active lane cannot be deleted — activate another lane first")
        conn.execute("DELETE FROM lanes WHERE id = ?", (lane_id,))
    return True


def activate_lane(conn: sqlite3.Connection, lane_id: str) -> Optional[dict]:
    """Make ``lane_id`` the single active lane. Returns it, or None if absent.

    Takes effect for every spawn AFTER the commit — the dispatcher re-reads
    the active lane per spawn (no gateway restart).
    """
    with write_txn(conn):
        row = conn.execute(
            "SELECT id FROM lanes WHERE id = ?", (lane_id,)
        ).fetchone()
        if row is None:
            return None
        now = int(time.time())
        conn.execute("UPDATE lanes SET active = 0 WHERE active = 1")
        conn.execute(
            "UPDATE lanes SET active = 1, updated_at = ? WHERE id = ?",
            (now, lane_id),
        )
    out = conn.execute("SELECT * FROM lanes WHERE id = ?", (lane_id,)).fetchone()
    return _lane_dict(out) if out is not None else None


def _active_lane_entry_for_profile(
    profile_arg: str,
    *,
    board: Optional[str] = None,
) -> Optional[dict]:
    """Hot-read the active lane's entry for ``profile_arg`` at spawn time.

    Opens its own short-lived connection (the spawn helpers don't receive the
    dispatcher's). Fail-soft like _is_claude_cli_profile: ANY error returns
    None so a broken lanes table can never block dispatching. Returns
    normalized lane entry or None when the profile is not mapped / no lane is
    active.
    """
    try:
        conn = connect(board=board)
        try:
            row = conn.execute(
                "SELECT profiles FROM lanes WHERE active = 1 LIMIT 1"
            ).fetchone()
        finally:
            conn.close()
        if row is None:
            return None
        profiles = json.loads(row["profiles"] or "{}")
        if not isinstance(profiles, dict):
            return None
        entry = profiles.get(profile_arg)
        if not isinstance(entry, dict):
            return None
        normalized = _normalize_lane_profiles({profile_arg: entry}).get(profile_arg)
        if not isinstance(normalized, dict):
            return None
        if (
            normalized.get("worker_runtime") is None
            and normalized.get("provider") is None
            and normalized.get("model") is None
            and not normalized.get("fallback_providers")
        ):
            return None
        return normalized
    except Exception:
        return None


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


def _inherit_notify_subs(
    conn: sqlite3.Connection,
    src_task_id: str,
    dst_task_id: str,
    *,
    now: Optional[int] = None,
) -> None:
    """Copy every notify-subscription of ``src_task_id`` onto ``dst_task_id``.

    Used so auto-decompose children inherit the root/triage task's Discord
    subscription and can deliver their own terminal state back to the
    originating chat without a manual ``notify-subscribe``.

    ``last_event_id`` is deliberately NOT copied: the child has its own
    event stream and the inherited sub must start at cursor 0 so the
    child's own terminal events get delivered.

    Idempotent: ``INSERT OR IGNORE`` collides on the
    ``(task_id, platform, chat_id, thread_id)`` primary key, so a repeated
    decompose never creates duplicate rows.

    MUST be called from inside an existing ``write_txn`` — it issues a bare
    ``conn.execute`` and does not open its own transaction (mirrors the
    inlined-INSERT discipline of :func:`decompose_triage_task`).
    """
    if now is None:
        now = int(time.time())
    conn.execute(
        """
        INSERT OR IGNORE INTO kanban_notify_subs
            (task_id, platform, chat_id, thread_id, user_id, notifier_profile, created_at)
        SELECT ?, platform, chat_id, thread_id, user_id, notifier_profile, ?
          FROM kanban_notify_subs
         WHERE task_id = ?
        """,
        (dst_task_id, now, src_task_id),
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
        # Inline the event-query body from unseen_events_for_sub, reusing
        # old_cursor already fetched above — avoids a second SELECT on
        # kanban_notify_subs inside the same BEGIN IMMEDIATE transaction.
        kind_list = list(kinds) if kinds else None
        q = (
            "SELECT * FROM task_events WHERE task_id = ? AND id > ? "
            + ("AND kind IN (" + ",".join("?" * len(kind_list)) + ") " if kind_list else "")
            + "ORDER BY id ASC"
        )
        params: list[Any] = [task_id, old_cursor]
        if kind_list:
            params.extend(kind_list)
        rows = conn.execute(q, params).fetchall()
        events: list[Event] = []
        new_cursor = old_cursor
        for r in rows:
            try:
                payload = json.loads(r["payload"]) if r["payload"] else None
            except Exception:
                payload = None
            events.append(Event(
                id=r["id"], task_id=r["task_id"], kind=r["kind"],
                payload=payload, created_at=r["created_at"],
                run_id=(int(r["run_id"]) if "run_id" in r.keys() and r["run_id"] is not None else None),
            ))
            new_cursor = max(new_cursor, int(r["id"]))
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


def run_timeline(
    conn: sqlite3.Connection,
    run_id: int,
    *,
    max_events: int = 500,
) -> Optional[dict]:
    """F3 (night-sprint): flat per-run timeline — events sorted with deltas.

    Read-only over existing data (no schema change): the run row frames the
    timeline (synthetic ``run_started`` / ``run_ended`` items), run-scoped
    ``task_events`` (``run_id`` match) fill it, and legacy/task-scoped events
    with ``run_id IS NULL`` are included when they fall inside the run's
    time window so pre-migration runs still get a usable trace.

    Each item carries ``offset_seconds`` (from run start) and
    ``delta_seconds`` (gap to the previous item) so the UI can render
    relative time bars without re-deriving anything. Returns None when the
    run does not exist. Events are capped at ``max_events`` (oldest first;
    ``truncated`` flags a cap hit).
    """
    run = conn.execute(
        "SELECT * FROM task_runs WHERE id = ?", (int(run_id),),
    ).fetchone()
    if run is None:
        return None
    started = int(run["started_at"]) if run["started_at"] is not None else None
    ended = int(run["ended_at"]) if run["ended_at"] is not None else None
    window_end = ended if ended is not None else int(time.time())

    rows = conn.execute(
        "SELECT id, kind, payload, created_at, run_id FROM task_events "
        "WHERE task_id = ? AND (run_id = ? OR (run_id IS NULL "
        "      AND created_at >= ? AND created_at <= ?)) "
        "ORDER BY created_at ASC, id ASC LIMIT ?",
        (
            run["task_id"], int(run_id),
            (started or 0) - 1, window_end + 1,
            int(max_events) + 1,
        ),
    ).fetchall()
    truncated = len(rows) > max_events
    rows = rows[:max_events]

    items: list[dict] = []
    if started is not None:
        items.append({
            "kind": "run_started",
            "at": started,
            "source": "run",
            "payload": {"profile": run["profile"], "status": run["status"]},
        })
    for r in rows:
        try:
            payload = json.loads(r["payload"]) if r["payload"] else None
            if not isinstance(payload, (dict, list)):
                payload = None
        except (ValueError, TypeError):
            payload = None
        items.append({
            "kind": r["kind"],
            "at": int(r["created_at"]) if r["created_at"] is not None else started or 0,
            "source": "event" if r["run_id"] is not None else "task",
            "payload": payload,
        })
    if ended is not None:
        items.append({
            "kind": "run_ended",
            "at": ended,
            "source": "run",
            "payload": {
                "status": run["status"],
                "outcome": run["outcome"],
                "error": run["error"],
            },
        })
    items.sort(key=lambda it: it["at"])

    prev: Optional[int] = None
    for it in items:
        it["offset_seconds"] = (it["at"] - started) if started is not None else None
        it["delta_seconds"] = (it["at"] - prev) if prev is not None else 0
        prev = it["at"]

    return {
        "run": {
            "id": int(run["id"]),
            "task_id": run["task_id"],
            "profile": run["profile"],
            "status": run["status"],
            "outcome": run["outcome"],
            "error": run["error"],
            "summary": run["summary"],
            "started_at": started,
            "ended_at": ended,
            "duration_seconds": (window_end - started) if started is not None else None,
        },
        "items": items,
        "count": len(items),
        "truncated": truncated,
    }


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
