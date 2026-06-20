"""Dispatcher-provisioned git worktrees + serialized chain integrator.

Worker isolation for kanban repo tasks (``kanban.worker_isolation: worktree``):

* **Provisioning** (claim time): a repo task (``dir``/``worktree`` whose
  resolved workspace is a git repo) gets a dispatcher-created worktree at
  ``<repo>/.worktrees/kanban/<root_task_id>`` on branch
  ``kanban/<root_task_id>``, branched from the repo's currently checked-out
  branch. Chain children land in the same worktree (idempotent per root).
  The base branch at first claim is frozen as the chain's merge target.

* **Integration** (completion time): when the LAST open task of a chain
  completes, the chain branch is merged ``--no-ff`` into the frozen merge
  target — serialized under a cross-process file lock, guarded by
  pre-checks (clean operation state, target still checked out, no overlap
  between the live checkout's dirty files and the branch diff), and a
  post-merge quick gate (ruff + affected pytest modules, tsc on ``web/``
  diffs). A red gate reverts the merge (``revert -m 1``). Any failed check
  PARKS the chain (caller blocks the task) instead of guessing. The
  integrator never pushes.

Layering: this module owns the git mechanics and the provisioning/
integration policy; task state transitions (done/blocked, run verdicts)
stay in ``kanban_db`` hook code. DB access here is limited to workspace
bookkeeping (``set_workspace_path``, provisioning/integration events,
receipt comments) via lazy imports so module import never cycles.
"""

from __future__ import annotations

import json
import logging
import os
import shutil
import sqlite3
import subprocess
import sys
import threading
import time
from pathlib import Path
from typing import Callable, Optional, Sequence

_log = logging.getLogger(__name__)

# Namespace for dispatcher-managed worktrees, relative to the repo root.
WORKTREES_DIRNAME = ".worktrees"
WORKTREES_NAMESPACE = "kanban"

# Untracked entries that are expected inside a provisioned worktree and in
# the live checkout, and must never count as "dirty" for overlap/clean
# checks: the worktree namespace itself and the node_modules symlinks the
# provisioner plants (never committed).
_IGNORED_DIRTY_PREFIXES = (
    f"{WORKTREES_DIRNAME}/",
)
_IGNORED_DIRTY_PATHS = (
    ".deliverable.md",
    "node_modules",
    "web/node_modules",
    ".venv",
)
# Tool/cache byproducts that are never commit content and never part of a
# branch diff. Gate runs themselves produce these (the verifier's
# `ruff`/`pytest` in the worktree writes __pycache__), so counting them as
# "uncommitted changes" would park every chain whose repo doesn't gitignore
# them — observed live in the 2026-06-11 E2E probe. `.playwright-mcp` is the
# Playwright-MCP visual-check output dir (console/network traces + page
# snapshots, and any screenshot written there): a UI-verification run inside
# the worktree drops `.playwright-mcp/console-*.log` + `page-*.yml`, which
# parked chain t_7567c379 on 2026-06-17 — same byproduct class as __pycache__.
_IGNORED_DIRTY_DIR_PARTS = frozenset({
    "__pycache__", ".pytest_cache", ".ruff_cache", ".mypy_cache", ".venv",
    ".playwright-mcp",
})
_IGNORED_DIRTY_SUFFIXES = (".pyc", ".pyo")


def _is_ignorable_dirty_path(path: str) -> bool:
    if path in _IGNORED_DIRTY_PATHS:
        return True
    if any(path.startswith(pref) for pref in _IGNORED_DIRTY_PREFIXES):
        return True
    if path.endswith(_IGNORED_DIRTY_SUFFIXES):
        return True
    parts = path.rstrip("/").split("/")
    return any(p in _IGNORED_DIRTY_DIR_PARTS for p in parts)

# node_modules and .venv locations symlinked into a fresh worktree so
# frontend gates work without an npm ci and Python tests can run without a
# second venv install.  The .venv symlink is removed by remove_worktree via
# the same loop — is_symlink()/unlink() so the real venv is never touched.
_NODE_MODULES_LINKS = ("node_modules", "web/node_modules", ".venv")

FO_REPO_PATH = Path("/home/piet/projects/family-organizer")
MERGED_GREEN = "MERGED_GREEN"
GREEN_CODE_NOT_RUNTIME_ACTIVATED = "GREEN_CODE_NOT_RUNTIME_ACTIVATED"
_RELEASE_GATE_COMMANDS = (
    "cd /home/piet/.hermes/hermes-agent/web",
    "npm run build",
    "test -f /home/piet/.hermes/hermes-agent/hermes_cli/web_dist/index.html",
    "curl -fsS http://127.0.0.1:9119/control >/dev/null",
)


def _is_fo_repo(repo_root: Path) -> bool:
    """True if repo_root is the Family-Organizer node repo. Path-equality
    first (cheap), then a package.json marker (scripts.build startswith
    'next build') so a moved/cloned checkout still matches. startswith (not
    ==) is robust to future flags like 'next build --turbo'. Any error -> False."""
    try:
        rr = Path(repo_root).resolve()
    except Exception:
        return False
    try:
        if rr == FO_REPO_PATH.resolve():
            return True
    except Exception:
        pass
    try:
        with open(rr / "package.json", "r", encoding="utf-8") as fh:
            data = json.load(fh)
        build = ((data.get("scripts") or {}).get("build") or "")
        return isinstance(build, str) and build.startswith("next build")
    except (OSError, ValueError, json.JSONDecodeError):
        return False


GIT_TIMEOUT_SECONDS = 120
MERGE_TIMEOUT_SECONDS = 300
# Must comfortably exceed a worst-case post-merge gate (ruff 300s +
# pytest 1200s + tsc 600s) so a second completer waits instead of parking
# on pure lock contention.
LOCK_TIMEOUT_SECONDS = 2400

# In-process serialization (the file lock below serializes across processes).
_PROCESS_LOCK = threading.Lock()


class WorktreeError(RuntimeError):
    """A provisioning/integration git step failed."""


class WorktreeTimeout(WorktreeError):
    """A git invocation exceeded its timeout (transient lock contention).

    Subclass of WorktreeError so existing ``except WorktreeError`` keeps
    working, but the dispatcher can isinstance-check it to re-queue instead
    of permanently blocking.
    """


def _integration_park_class(reason: str) -> str:
    """Classify why the serialized integrator parked a chain.

    ``transient`` reasons can self-clear and are safe for the future
    integration-retry lane. ``needs_orchestrator`` reasons require a focused
    fixer. Unknown reasons stay operator-owned as the conservative fallback.
    """
    text = (reason or "").strip()
    if text.startswith("integration parked:"):
        text = text.removeprefix("integration parked:").strip()

    transient_prefixes = (
        "live checkout has an operation in progress (",
        "checked-out branch ",
        "worktree has uncommitted changes but no commits to merge",
        "chain worktree has uncommitted changes:",
        "dirty files in live checkout overlap the branch diff:",
        "chain worktree missing before rebase",
    )
    if text.startswith(transient_prefixes):
        return "transient"

    needs_orchestrator_prefixes = (
        "merge conflict/failure (aborted):",
        "post-merge gate failed:",
    )
    if text.startswith(needs_orchestrator_prefixes):
        return "needs_orchestrator"

    return "needs_operator"


# ---------------------------------------------------------------------------
# Config / path predicates
# ---------------------------------------------------------------------------

def isolation_mode() -> str:
    """Resolve ``kanban.worker_isolation`` from the ROOT config.

    Same root-config-not-profile-config rationale as
    ``kanban_db._review_gate_config``: the dispatcher and every worker must
    agree on one source of truth. ``HERMES_KANBAN_WORKER_ISOLATION`` (env)
    wins over config — used by tests and one-off operator runs. Default:
    ``"off"`` (today's behavior, the planspec's reversibility guarantee).
    """
    env = (os.environ.get("HERMES_KANBAN_WORKER_ISOLATION") or "").strip().lower()
    if env:
        return env
    try:
        import yaml
        from hermes_constants import get_default_hermes_root

        cfg_path = get_default_hermes_root() / "config.yaml"
        if cfg_path.is_file():
            with open(cfg_path, "r", encoding="utf-8") as fh:
                root_cfg = yaml.safe_load(fh) or {}
            value = (root_cfg.get("kanban") or {}).get("worker_isolation")
            if isinstance(value, str) and value.strip():
                return value.strip().lower()
    except Exception:
        pass
    return "off"


def scratch_code_redirect(task, board: Optional[str] = None) -> Optional[Path]:
    """Isolation backstop for scratch CODE tasks.

    Scratch tasks bypass worktree provisioning entirely, but a code-role
    worker (``kanban.review_gate.code_roles``) routinely cd's into the
    project repo anyway and edits it UNISOLATED — that is how t_4cc0fe1b
    (2026-06-12) left an approved-but-uncommitted diff in the live checkout.
    When the board declares a ``default_workdir`` that is a git repo, treat
    the scratch task as a ``dir`` task on that repo so the normal worktree +
    merge-back pipeline applies. Non-code roles and boards without a
    ``default_workdir`` keep today's scratch behavior byte-identical.
    """
    from hermes_cli import kanban_db as kb

    assignee = (getattr(task, "assignee", None) or "").strip().lower()
    if not assignee:
        return None
    try:
        if assignee not in kb._review_gate_config()["code_roles"]:
            return None
    except Exception:
        return None

    # Tenant pins the repo: a family-organizer code task MUST land in the FO
    # checkout, never the board ``default_workdir`` (the Hermes repo). An
    # FO-backlog "copy to Fleet" commission arrives as scratch + coder with no
    # explicit workspace; without this it is silently redirected into
    # hermes-agent and blocks on missing FO files (t_8fbe701d, 2026-06-14 —
    # same leak the E1 guard closed for the dir/worktree door in
    # ``kanban_db.create_task``, here closed for the scratch door). If the FO
    # checkout is missing / not a git repo we return None (stay scratch) rather
    # than fall back to the Hermes repo — that fallback IS the bug.
    tenant = (getattr(task, "tenant", None) or "").strip().lower()
    if tenant == "family-organizer":
        if FO_REPO_PATH.is_dir() and repo_root_for(FO_REPO_PATH) is not None:
            return FO_REPO_PATH
        return None

    try:
        workdir = kb.read_board_metadata(board).get("default_workdir")
    except Exception:
        return None
    if not workdir:
        return None
    path = Path(str(workdir)).expanduser()
    if not path.is_dir() or repo_root_for(path) is None:
        return None
    return path


def split_provisioned_path(path) -> Optional[tuple[Path, str, Path]]:
    """``(repo_root, root_id, worktree_path)`` when *path* points INTO a
    dispatcher-provisioned worktree (``<repo>/.worktrees/kanban/<root_id>``,
    possibly a subdirectory of it), else ``None``."""
    if not path:
        return None
    parts = Path(path).parts
    for i in range(len(parts) - 2):
        if (
            parts[i] == WORKTREES_DIRNAME
            and parts[i + 1] == WORKTREES_NAMESPACE
        ):
            return (
                Path(*parts[:i]) if i else Path("."),
                parts[i + 2],
                Path(*parts[: i + 3]),
            )
    return None


def is_provisioned_path(path) -> bool:
    """True iff *path* points into a dispatcher-provisioned worktree."""
    return split_provisioned_path(path) is not None


def chain_branch(root_id: str) -> str:
    return f"{WORKTREES_NAMESPACE}/{root_id}"


# ---------------------------------------------------------------------------
# Git plumbing
# ---------------------------------------------------------------------------

def _git(
    repo: Path | str,
    *args: str,
    check: bool = True,
    timeout: int | None = None,
) -> str:
    # Read the timeout at call time so HERMES_WORKTREE_GIT_TIMEOUT (operator
    # tuning, tests) is honored — a default-arg bound at import time would
    # freeze it. Callers passing an explicit ``timeout`` (e.g.
    # MERGE_TIMEOUT_SECONDS) bypass the env, unchanged.
    if timeout is None:
        timeout = int(os.environ.get("HERMES_WORKTREE_GIT_TIMEOUT", GIT_TIMEOUT_SECONDS))
    try:
        proc = subprocess.run(  # noqa: S603 -- fixed argv, no shell
            ["git", "-C", str(repo), *args],
            capture_output=True,
            text=True,
            timeout=timeout,
        )
    except subprocess.TimeoutExpired as exc:
        raise WorktreeTimeout(
            f"git {' '.join(args[:3])}… timed out after {timeout}s in {repo}"
        ) from exc
    if check and proc.returncode != 0:
        raise WorktreeError(
            f"git {' '.join(args[:3])}… failed in {repo}: "
            f"{(proc.stderr or proc.stdout).strip()[:500]}"
        )
    return proc.stdout.strip()


def repo_root_for(path) -> Optional[Path]:
    """Toplevel of the git repo containing *path*, or None."""
    p = Path(path)
    if not p.is_dir():
        return None
    try:
        top = _git(p, "rev-parse", "--show-toplevel")
    except (WorktreeError, OSError, subprocess.SubprocessError):
        return None
    return Path(top) if top else None


def current_branch(repo: Path) -> str:
    """Checked-out branch of *repo*; raises on detached HEAD."""
    try:
        return _git(repo, "symbolic-ref", "--short", "HEAD")
    except WorktreeError as exc:
        raise WorktreeError(f"{repo} has a detached HEAD: {exc}") from exc


def _branch_exists(repo: Path, branch: str) -> bool:
    try:
        _git(repo, "rev-parse", "--verify", "--quiet", f"refs/heads/{branch}")
        return True
    except WorktreeError:
        return False


def _branch_is_ancestor(repo: Path, branch: str, target: str) -> bool:
    try:
        _git(repo, "merge-base", "--is-ancestor", branch, target)
        return True
    except WorktreeError:
        return False


def dirty_files(repo: Path) -> list[str]:
    """Porcelain status paths (incl. files inside untracked dirs), with the
    worktree namespace and the planted node_modules symlinks filtered out.

    Uses ``-z`` (NUL-separated) so paths with spaces/special chars arrive
    unquoted — the overlap check must compare exact paths."""
    out = _git(repo, "status", "--porcelain", "-uall", "-z")
    files: list[str] = []
    entries = out.split("\0")
    i = 0
    while i < len(entries):
        entry = entries[i]
        i += 1
        if len(entry) < 4:
            continue
        status, path = entry[:2], entry[3:]
        if status[0] in ("R", "C"):
            # Rename/copy: the ORIGIN path follows as its own NUL field;
            # `path` already is the destination. Skip the origin field.
            i += 1
        if _is_ignorable_dirty_path(path):
            continue
        files.append(path)
    return files


# ---------------------------------------------------------------------------
# Provisioning
# ---------------------------------------------------------------------------

def _reap_partial(repo_root: Path | str, wt: Path) -> None:
    """Reap a partial/locked worktree left behind by a failed ``worktree add``.

    A killed/timed-out ``worktree add`` can leave a registered,
    ``initializing``-locked worktree that plain ``prune`` won't reap. Force-
    remove it (the double ``--force`` overrides the lock), then prune the
    bookkeeping. Best-effort: both calls pass ``check=False`` so reaping never
    masks the original provisioning error the caller is about to re-raise.
    """
    _git(repo_root, "worktree", "remove", "--force", "--force", str(wt), check=False)
    _git(repo_root, "worktree", "prune", check=False)


def ensure_worktree(repo_root: Path, root_id: str) -> dict:
    """Create (or reuse) the chain worktree for *root_id*. Idempotent.

    Returns ``{"path", "branch", "base_branch", "created"}``. The branch is
    created from the repo's currently checked-out branch on first call;
    a pre-existing branch (e.g. the worktree was removed but the branch
    kept) is checked out as-is.
    """
    base = Path(repo_root) / WORKTREES_DIRNAME / WORKTREES_NAMESPACE
    base.mkdir(parents=True, exist_ok=True)
    wt = base / root_id
    branch = chain_branch(root_id)
    base_branch = current_branch(Path(repo_root))

    if (wt / ".git").exists():
        return {"path": wt, "branch": branch, "base_branch": base_branch,
                "created": False}
    if wt.exists():
        # Stale plain dir (e.g. resolve_workspace's mkdir after a cleanup):
        # only an EMPTY dir is safe to replace — anything else is not ours.
        try:
            wt.rmdir()
        except OSError as exc:
            raise WorktreeError(
                f"{wt} exists but is not a git worktree and not empty"
            ) from exc

    def _add() -> None:
        if _branch_exists(Path(repo_root), branch):
            _git(repo_root, "worktree", "add", str(wt), branch)
        else:
            _git(repo_root, "worktree", "add", str(wt), "-b", branch, base_branch)

    try:
        _add()
    except WorktreeTimeout:
        # Transient git-lock contention: reap the partial/locked worktree
        # and re-raise so the dispatcher classifies it as transient
        # (re-queue, not block — see kanban_db.dispatch_once). Caught BEFORE
        # the generic WorktreeError on purpose: a timeout must NOT fall into
        # the inline retry below, which would block the dispatcher for
        # another full timeout on the same contention.
        _reap_partial(repo_root, wt)
        raise
    except WorktreeError:
        # A removed-but-still-registered worktree blocks re-adding; prune
        # the bookkeeping once and retry.
        _git(repo_root, "worktree", "prune", check=False)
        try:
            _add()
        except WorktreeError:
            _reap_partial(repo_root, wt)
            raise

    # node_modules symlinks (untracked, never committed) so frontend gates
    # run inside the worktree without an npm ci.
    for rel in _NODE_MODULES_LINKS:
        src = Path(repo_root) / rel
        dst = wt / rel
        if src.is_dir() and not dst.exists() and dst.parent.is_dir():
            try:
                dst.symlink_to(src, target_is_directory=True)
            except OSError:
                _log.warning("could not symlink %s into worktree %s", rel, wt)

    return {"path": wt, "branch": branch, "base_branch": base_branch,
            "created": True}


def chain_root_id(conn: sqlite3.Connection, task_id: str) -> str:
    """Walk ``task_links`` upward to the chain root (deterministic: smallest
    parent id at each level; cycle-safe)."""
    current = task_id
    seen = {current}
    while True:
        rows = conn.execute(
            "SELECT parent_id FROM task_links WHERE child_id = ? "
            "ORDER BY parent_id",
            (current,),
        ).fetchall()
        parents = [r["parent_id"] for r in rows if r["parent_id"] not in seen]
        if not parents:
            return current
        current = parents[0]
        seen.add(current)


def _chain_member_ids(conn: sqlite3.Connection, root_id: str) -> set[str]:
    """All task ids reachable from *root_id* via ``task_links`` (incl. the
    root itself). BFS over child links, cycle-safe."""
    ids = {root_id}
    queue = [root_id]
    while queue:
        current = queue.pop()
        rows = conn.execute(
            "SELECT child_id FROM task_links WHERE parent_id = ?", (current,)
        ).fetchall()
        for r in rows:
            child = r["child_id"]
            if child not in ids:
                ids.add(child)
                queue.append(child)
    return ids


def frozen_merge_target(conn: sqlite3.Connection, root_id: str) -> Optional[str]:
    """Merge target frozen at first claim (Entscheidung 3), from the root
    task's ``worktree_provisioned`` event."""
    try:
        row = conn.execute(
            "SELECT payload FROM task_events "
            "WHERE task_id = ? AND kind = 'worktree_provisioned' "
            "ORDER BY id ASC LIMIT 1",
            (root_id,),
        ).fetchone()
        if not row or not row["payload"]:
            return None
        payload = json.loads(row["payload"])
        target = payload.get("merge_target")
        return str(target) if target else None
    except (sqlite3.Error, ValueError, TypeError):
        return None


def _release_gate_subject(task_id: str, root_id: str) -> str:
    return task_id if task_id == root_id else f"{task_id}/{root_id}"


def _release_gate_title(task_id: str, root_id: str) -> str:
    return (
        "[Release-Gate] Dashboard build + runtime activation check for "
        f"{_release_gate_subject(task_id, root_id)}"
    )


def _release_gate_body(task_id: str, root_id: str, outcome: dict) -> str:
    commands = "\n".join(_RELEASE_GATE_COMMANDS)
    return (
        f"Source integration: {task_id}\n"
        f"Chain root: {root_id}\n"
        f"Code state: {MERGED_GREEN}\n"
        f"Release state: {GREEN_CODE_NOT_RUNTIME_ACTIVATED}\n"
        f"Merge commit: {outcome.get('merge_commit', '')}\n\n"
        "Parked by default. Do not execute until a release-gate GO explicitly "
        "includes dashboard build/runtime activation.\n\n"
        "Documented activation commands:\n"
        f"{commands}\n"
    )


def _release_gate_child_exists(
    conn: sqlite3.Connection, parent_id: str, title: str,
) -> Optional[str]:
    row = conn.execute(
        "SELECT t.id FROM tasks t "
        "JOIN task_links l ON l.child_id = t.id "
        "WHERE l.parent_id = ? AND t.title = ? AND t.status != 'archived' "
        "ORDER BY t.created_at DESC LIMIT 1",
        (parent_id, title),
    ).fetchone()
    return row["id"] if row else None


def _create_parked_release_gate_child(
    conn: sqlite3.Connection,
    task_id: str,
    root_id: str,
    outcome: dict,
) -> Optional[str]:
    title = _release_gate_title(task_id, root_id)
    existing = _release_gate_child_exists(conn, task_id, title)
    if existing:
        return existing

    from hermes_cli import kanban_db as kb

    child_id = kb.create_task(
        conn,
        title=title,
        body=_release_gate_body(task_id, root_id, outcome),
        assignee="verifier",
        created_by="integrator",
        parents=(task_id,),
        initial_status="blocked",
    )
    payload = {
        "state": GREEN_CODE_NOT_RUNTIME_ACTIVATED,
        "source_task": task_id,
        "root_id": root_id,
        "merge_commit": outcome.get("merge_commit"),
        "reason": "awaiting release-gate GO",
        "commands": list(_RELEASE_GATE_COMMANDS),
    }
    with kb.write_txn(conn):
        kb._append_event(conn, child_id, "release_gate_parked", payload)
        kb._append_event(conn, child_id, "blocked", payload)
        kb._append_event(
            conn,
            task_id,
            "release_gate_created",
            {"child_id": child_id, **payload},
        )
    return child_id


# ---------------------------------------------------------------------------
# Release-gate executor (R2 / P2-release-executor)
#
# The parked release-gate child above documents the activation commands but
# nothing runs them. This executor processes such a child end to end: it runs
# the gate in the LIVE checkout (the commands are hardcoded to that path), and
# on green reports success to the board. On RED it spawns a BOUNDED fixer on
# the ``coder-claude`` lane EXCLUSIVELY inside the chain worktree/branch — the
# fixer reads the gate error, fixes, and the gate is re-run. After the retry
# budget (``kanban.release_gate_fixer_max_retries``, default 2) it escalates to
# the operator. Hard boundary: the fixer never edits live-main; only the gate's
# build/smoke touch the live checkout. The event trail is purely additive
# (``release_gate_executed`` / ``release_gate_fix_attempt``).
# ---------------------------------------------------------------------------

# The release-gate commands are hardcoded to the live checkout, so the fixer's
# chain worktree is provisioned under the SAME repo (its ``.worktrees/kanban/``
# namespace), never the live working tree.
LIVE_CHECKOUT_ROOT = Path("/home/piet/.hermes/hermes-agent")
# npm build + loopback smoke can be slow; keep generous so a slow-but-green
# build is not misreported as red.
RELEASE_GATE_COMMAND_TIMEOUT = 1800
RELEASE_GATE_FIXER_TIMEOUT = 1800


class ReleaseGateError(RuntimeError):
    """A release-gate executor precondition failed (e.g. the task is not a
    release-gate child, or a hard isolation invariant would be violated)."""


def release_gate_fixer_max_retries() -> int:
    """Bounded fixer retry budget. ``HERMES_RELEASE_GATE_FIXER_MAX_RETRIES``
    (env) wins for tests/operator one-offs, then config
    ``kanban.release_gate_fixer_max_retries``, default 2. Clamped to >= 0.

    Same root-config-not-profile-config rationale as :func:`isolation_mode`."""
    env = (os.environ.get("HERMES_RELEASE_GATE_FIXER_MAX_RETRIES") or "").strip()
    if env:
        try:
            return max(0, int(env))
        except ValueError:
            pass
    try:
        import yaml
        from hermes_constants import get_default_hermes_root

        cfg_path = get_default_hermes_root() / "config.yaml"
        if cfg_path.is_file():
            with open(cfg_path, "r", encoding="utf-8") as fh:
                root_cfg = yaml.safe_load(fh) or {}
            value = (root_cfg.get("kanban") or {}).get(
                "release_gate_fixer_max_retries"
            )
            # bool is an int subclass — reject it explicitly.
            if isinstance(value, bool):
                value = None
            if isinstance(value, int) and value >= 0:
                return value
            if isinstance(value, str) and value.strip().lstrip("-").isdigit():
                return max(0, int(value.strip()))
    except Exception:
        pass
    return 2


def _release_gate_context(
    conn: sqlite3.Connection, task_id: str,
) -> Optional[dict]:
    """Read the chain context off the most recent ``release_gate_parked``
    event for *task_id*. Returns ``None`` when the task is not a release-gate
    child (no such event)."""
    row = conn.execute(
        "SELECT payload FROM task_events WHERE task_id = ? "
        "AND kind = 'release_gate_parked' ORDER BY id DESC LIMIT 1",
        (task_id,),
    ).fetchone()
    if not row or not row["payload"]:
        return None
    try:
        payload = json.loads(row["payload"])
    except (ValueError, TypeError):
        return None
    return {
        "root_id": payload.get("root_id") or task_id,
        "source_task": payload.get("source_task"),
        "merge_commit": payload.get("merge_commit"),
        "commands": payload.get("commands") or list(_RELEASE_GATE_COMMANDS),
    }


def _resolve_fixer_worktree(
    root_id: str, repo_root: Optional[Path] = None,
) -> tuple[Path, str]:
    """``(worktree_path, branch)`` for the chain fixer — always under
    ``<repo>/.worktrees/kanban/<root_id>`` on ``kanban/<root_id>``.

    Hard isolation invariant: the path MUST be a provisioned-worktree path
    (under the worktree namespace), never the live checkout root. A computed
    path that fails that check means the namespace structure is wrong — refuse
    rather than risk the fixer running against live-main."""
    if not root_id:
        raise ReleaseGateError("cannot resolve fixer worktree without a root id")
    repo_root = Path(repo_root or LIVE_CHECKOUT_ROOT)
    wt = repo_root / WORKTREES_DIRNAME / WORKTREES_NAMESPACE / root_id
    if split_provisioned_path(wt) is None or wt.resolve() == repo_root.resolve():
        raise ReleaseGateError(
            f"refusing fixer worktree {wt!r}: not isolated from live checkout"
        )
    return wt, chain_branch(root_id)


def _default_release_gate_runner(
    commands: Optional[Sequence[str]] = None,
) -> tuple[bool, str]:
    """Run the release-gate commands as one shell sequence in the live
    checkout and return ``(ok, output_tail)``. The commands are a fixed,
    code-defined tuple (web build + artifact check + loopback smoke), joined
    with ``&&`` so the leading ``cd <web>`` carries to ``npm run build`` —
    no untrusted input reaches the shell."""
    cmds = list(commands or _RELEASE_GATE_COMMANDS)
    script = " && ".join(cmds)
    cwd = str(LIVE_CHECKOUT_ROOT) if LIVE_CHECKOUT_ROOT.is_dir() else None
    try:
        proc = subprocess.run(  # noqa: S602 -- fixed code-defined commands
            ["bash", "-c", script],
            cwd=cwd,
            capture_output=True,
            text=True,
            timeout=RELEASE_GATE_COMMAND_TIMEOUT,
        )
    except subprocess.TimeoutExpired:
        return False, (
            f"release-gate timed out after {RELEASE_GATE_COMMAND_TIMEOUT}s"
        )
    except (OSError, subprocess.SubprocessError) as exc:
        return False, f"release-gate command error: {exc}"
    ok = proc.returncode == 0
    tail = ((proc.stdout or "") + (proc.stderr or ""))[-4000:]
    return ok, tail


def _release_gate_fixer_prompt(
    *, gate_error: str, attempt: int, task_id: str, root_id: str,
) -> str:
    commands = "\n".join(_RELEASE_GATE_COMMANDS)
    return (
        "You are a bounded Hermes release-gate fixer running headless on the "
        "coder-claude lane. The dashboard release gate for chain "
        f"`{root_id}` is RED (fixer attempt {attempt}).\n\n"
        "Gate commands (run in the live checkout — do NOT edit there):\n"
        f"{commands}\n\n"
        "Most recent gate output:\n"
        f"{(gate_error or '')[-3000:]}\n\n"
        "Your job: read the error, find and fix the root cause in THIS git "
        "worktree, then verify locally by running `npm run build` inside this "
        "worktree's `web/` directory. Commit your fix on this branch.\n\n"
        "HARD RULES: work ONLY in this worktree/branch. NEVER edit, checkout, "
        "switch, push, or merge the live checkout or any other branch — "
        "integration happens outside your run after review. Make the minimal "
        "change that turns the gate green."
    )


def _spawn_release_gate_fixer_process(
    *, worktree: Path, branch: str, prompt: str, task_id: str, root_id: str,
) -> None:
    """Spawn the coder-claude fixer process, blocking until it finishes, with
    ``cwd`` pinned to the isolated *worktree*. Reuses the claude-CLI worker
    env contract (caged via ``HERMES_KANBAN_TASK``, no provider keys, web
    egress tools denied). Separated out so tests can assert isolation without
    spawning a real model."""
    import shutil

    cmd = [
        os.environ.get("HERMES_CLAUDE_BIN") or "claude",
        "-p", prompt,
        "--dangerously-skip-permissions",
        "--disallowedTools", "WebFetch,WebSearch",
        "--output-format", "json",
        "--settings", '{"enabledPlugins": {"memsearch@memsearch-plugins": false}}',
    ]
    try:
        from hermes_cli import kanban_db as kb

        lane = kb._active_lane_entry_for_profile("coder-claude")
        model = (lane or {}).get("model")
        if model:
            cmd.extend(["--model", model])
    except Exception:
        pass

    env = dict(os.environ)
    hermes_dir = os.path.dirname(
        shutil.which("hermes") or "/home/piet/.local/bin/hermes"
    )
    env["PATH"] = hermes_dir + os.pathsep + env.get("PATH", "")
    # Cage the fixer to its own task + worktree branch (guard-dangerous-ops
    # keys off HERMES_KANBAN_TASK; the worker may never push/merge).
    env["HERMES_KANBAN_TASK"] = task_id
    env["HERMES_KANBAN_WORKSPACE"] = str(worktree)
    env["HERMES_KANBAN_BRANCH"] = branch
    env["MEMSEARCH_NO_WATCH"] = "1"
    # The claude CLI runs on the subscription — strip any provider key so
    # billing never silently switches to the API.
    for key in ("ANTHROPIC_API_KEY", "OPENAI_API_KEY", "OPENROUTER_API_KEY"):
        env.pop(key, None)

    subprocess.run(  # noqa: S603 -- argv is a fixed list built above
        cmd,
        cwd=str(worktree),
        stdin=subprocess.DEVNULL,
        capture_output=True,
        text=True,
        env=env,
        timeout=RELEASE_GATE_FIXER_TIMEOUT,
    )


def _default_release_gate_fixer(
    *,
    worktree: Path,
    branch: str,
    gate_error: str,
    attempt: int,
    task_id: str,
    root_id: str,
    repo_root: Optional[Path] = None,
) -> None:
    """Default fixer: (re)create the isolated chain worktree and spawn a
    coder-claude fixer inside it. The chain worktree is removed after the
    merge that created the release-gate child, so ``ensure_worktree`` recreates
    it (idempotent) on the chain branch. The live checkout is never modified
    here — only the gate's build/smoke run there, via the gate runner."""
    repo_root = Path(repo_root or LIVE_CHECKOUT_ROOT)
    ensure_worktree(repo_root, root_id)
    prompt = _release_gate_fixer_prompt(
        gate_error=gate_error, attempt=attempt, task_id=task_id, root_id=root_id,
    )
    _spawn_release_gate_fixer_process(
        worktree=Path(worktree), branch=branch, prompt=prompt,
        task_id=task_id, root_id=root_id,
    )


def _record_release_gate_executed(
    conn: sqlite3.Connection, task_id: str, *,
    attempt: int, ok: bool, output: str, root_id: str,
    fixer_error: Optional[str] = None,
) -> None:
    from hermes_cli import kanban_db as kb

    payload = {
        "attempt": attempt,
        "ok": bool(ok),
        "root_id": root_id,
        "output_tail": (output or "")[-2000:],
        "commands": list(_RELEASE_GATE_COMMANDS),
    }
    if fixer_error:
        payload["fixer_error"] = fixer_error
    try:
        with kb.write_txn(conn):
            kb._append_event(conn, task_id, "release_gate_executed", payload)
    except Exception:
        _log.warning("could not record release_gate_executed for %s",
                     task_id, exc_info=True)


def _record_release_gate_fix_attempt(
    conn: sqlite3.Connection, task_id: str, *,
    attempt: int, gate_error: str, root_id: str, worktree: str, branch: str,
) -> None:
    from hermes_cli import kanban_db as kb

    payload = {
        "attempt": attempt,
        "root_id": root_id,
        "lane": "coder-claude",
        "worktree": worktree,
        "branch": branch,
        "gate_error_tail": (gate_error or "")[-2000:],
    }
    try:
        with kb.write_txn(conn):
            kb._append_event(conn, task_id, "release_gate_fix_attempt", payload)
    except Exception:
        _log.warning("could not record release_gate_fix_attempt for %s",
                     task_id, exc_info=True)


def _finish_release_gate_green(
    conn: sqlite3.Connection, task_id: str, root_id: str, fixer_attempts: int,
) -> None:
    from hermes_cli import kanban_db as kb

    note = (
        f"✅ Release-gate green for chain `{root_id}` after {fixer_attempts} "
        "bounded fixer attempt(s). Dashboard build + artifact check + "
        "loopback smoke passed in the live checkout."
    )
    try:
        kb.add_comment(conn, task_id, "verifier", note)
    except Exception:
        _log.debug("release-gate green comment failed", exc_info=True)
    # Transition the parked child to done. Public path first (unblock the
    # blocked child, then complete); deterministic UPDATE fallback so a green
    # gate always lands as done on the board.
    moved = False
    try:
        task = kb.get_task(conn, task_id)
        if task is not None and task.status == "blocked":
            kb.unblock_task(conn, task_id)
        moved = kb.complete_task(
            conn, task_id, result="release-gate green",
            summary=f"release gate green after {fixer_attempts} fixer attempt(s)",
        )
    except Exception:
        _log.debug("release-gate green public transition failed", exc_info=True)
    if not moved:
        try:
            with kb.write_txn(conn):
                # Only promote a still-open gate child — never resurrect a
                # concurrently archived/failed task.
                conn.execute(
                    "UPDATE tasks SET status = 'done' WHERE id = ? "
                    "AND status IN ('blocked', 'ready', 'todo', 'running')",
                    (task_id,),
                )
                kb._append_event(
                    conn, task_id, "release_gate_green",
                    {"root_id": root_id, "fixer_attempts": fixer_attempts},
                )
        except Exception:
            _log.warning("could not mark release-gate child %s done",
                         task_id, exc_info=True)


def _escalate_release_gate(
    conn: sqlite3.Connection, task_id: str, root_id: str, *,
    attempts: int, last_error: str,
) -> None:
    from hermes_cli import kanban_db as kb

    payload = {
        "task": {"id": task_id},
        "why_now": (
            f"Release gate for chain {root_id} still red after {attempts} "
            "bounded fixer attempt(s)"
        ),
        "attempts_already_made": attempts,
        "evidence": {
            "last_error": (last_error or "")[-2000:],
            "root_id": root_id,
        },
        "recommended_human_action": (
            "Inspect the chain worktree fix and the gate output; decide a "
            "manual fix or revert before the staged deploy. Council review is "
            "required on this release path."
        ),
        "blocked_action_boundary": list(getattr(kb, "OPERATOR_ONLY_ACTIONS", ())),
    }
    try:
        with kb.write_txn(conn):
            esc_event_id = kb._append_event(
                conn, task_id, kb.OPERATOR_ESCALATION_EVENT, payload,
            )
            # ESCALATION-INLINE-CLASSIFY-S1: pair a heiler_classification to the
            # escalation atomically, in the same txn, rather than leaving it for
            # the poll-driven classify_escalations_sweep. The class is derived
            # from the escalation's own persisted evidence via the exact same
            # deterministic function the sweep uses, so the inline classification
            # is byte-identical to a swept one — defense-in-depth, not a
            # duplicate, no guess (AC-2 documented ledger reference).
            h_class, h_ev = kb._classify_escalation_payload(payload)
            kb._append_event(
                conn, task_id, kb.HEILER_CLASSIFICATION_EVENT,
                kb._heiler_classification_payload(
                    heiler_class=h_class, evidence=h_ev,
                    source=kb.HEILER_SOURCE_RELEASE_GATE, blocked=True,
                    escalation_event_id=esc_event_id,
                ),
            )
    except Exception:
        _log.warning("could not record operator_escalation for %s",
                     task_id, exc_info=True)
    try:
        kb.add_comment(
            conn, task_id, "verifier",
            f"⛔ Release-gate still red after {attempts} bounded fixer "
            "attempt(s) → operator_escalation. The fixer worked only in the "
            "chain worktree; live-main was never edited.",
        )
    except Exception:
        _log.debug("release-gate escalation comment failed", exc_info=True)
    # Keep the child blocked (it already is). Re-block defensively if some
    # other path moved it.
    try:
        task = kb.get_task(conn, task_id)
        if task is not None and task.status not in ("blocked", "archived"):
            kb.block_task(conn, task_id, reason="release-gate persistent red")
    except Exception:
        _log.debug("release-gate re-block failed", exc_info=True)


def execute_release_gate(
    conn: sqlite3.Connection,
    task_id: str,
    *,
    gate_runner=None,
    fixer_runner=None,
    max_retries: Optional[int] = None,
    repo_root: Optional[Path] = None,
    board: Optional[str] = None,
) -> dict:
    """Process a parked release-gate child end to end.

    Runs the gate in the live checkout. On green: report success + mark the
    child done. On red: spawn up to *max_retries* bounded coder-claude fixers
    inside the chain worktree/branch (never live-main), re-running the gate
    after each. Persistent red → ``operator_escalation`` and the child stays
    blocked.

    ``gate_runner``/``fixer_runner`` are injectable seams (defaults wire to the
    live subprocess gate and the claude-CLI fixer). Returns a result dict with
    ``status`` (``"green"`` | ``"escalated"``) and ``fixer_attempts``.
    """
    ctx = _release_gate_context(conn, task_id)
    if ctx is None:
        raise ReleaseGateError(
            f"{task_id} is not a release-gate child "
            "(no release_gate_parked event)"
        )
    root_id = ctx["root_id"]
    if max_retries is None:
        max_retries = release_gate_fixer_max_retries()
    max_retries = max(0, int(max_retries))
    gate_runner = gate_runner or _default_release_gate_runner
    fixer_runner = fixer_runner or _default_release_gate_fixer
    repo_root = Path(repo_root or LIVE_CHECKOUT_ROOT)

    # Attempt 0: bare gate run.
    ok, output = gate_runner()
    _record_release_gate_executed(
        conn, task_id, attempt=0, ok=ok, output=output, root_id=root_id,
    )
    if ok:
        _finish_release_gate_green(conn, task_id, root_id, 0)
        return {"status": "green", "fixer_attempts": 0, "root_id": root_id}

    fixer_attempts = 0
    while fixer_attempts < max_retries:
        fixer_attempts += 1
        worktree, branch = _resolve_fixer_worktree(root_id, repo_root)
        _record_release_gate_fix_attempt(
            conn, task_id, attempt=fixer_attempts, gate_error=output,
            root_id=root_id, worktree=str(worktree), branch=branch,
        )
        fix_error = None
        try:
            fixer_runner(
                worktree=worktree, branch=branch, gate_error=output,
                attempt=fixer_attempts, task_id=task_id, root_id=root_id,
            )
        except Exception as exc:  # the fixer is best-effort; record + retry/escalate
            fix_error = f"{type(exc).__name__}: {exc}"
            _log.warning("release-gate fixer attempt %d failed for %s: %s",
                         fixer_attempts, task_id, fix_error, exc_info=True)
        # Re-run the gate after the fixer.
        ok, output = gate_runner()
        _record_release_gate_executed(
            conn, task_id, attempt=fixer_attempts, ok=ok, output=output,
            root_id=root_id, fixer_error=fix_error,
        )
        if ok:
            _finish_release_gate_green(conn, task_id, root_id, fixer_attempts)
            return {
                "status": "green",
                "fixer_attempts": fixer_attempts,
                "root_id": root_id,
            }

    _escalate_release_gate(
        conn, task_id, root_id, attempts=fixer_attempts, last_error=output,
    )
    return {
        "status": "escalated",
        "fixer_attempts": fixer_attempts,
        "root_id": root_id,
    }


def _terminal_status(status: str) -> bool:
    return status in {"done", "archived", "failed", "cancelled"}


def _pending_root_finalizer_id(
    conn: sqlite3.Connection,
    *,
    task_id: str,
    root_id: str,
    wt: Path,
    members: set[str],
) -> Optional[str]:
    ids = set(members)
    ids.add(root_id)
    placeholders = ",".join("?" for _ in ids)
    like = (
        str(wt).replace("\\", "\\\\").replace("%", r"\%").replace("_", r"\_")
        + "%"
    )
    if placeholders:
        rows = conn.execute(
            f"SELECT id, status FROM tasks WHERE id IN ({placeholders}) "
            "OR workspace_path LIKE ? ESCAPE '\\'",
            (*ids, like),
        ).fetchall()
    else:
        rows = conn.execute(
            "SELECT id, status FROM tasks WHERE workspace_path LIKE ? ESCAPE '\\'",
            (like,),
        ).fetchall()
    open_ids = [
        r["id"] for r in rows
        if r["id"] != task_id and not _terminal_status(r["status"])
    ]
    if len(open_ids) != 1:
        return None
    pending_id = open_ids[0]
    if pending_id == task_id:
        return None
    return pending_id


def _record_pending_root_finalizer(
    conn: sqlite3.Connection,
    *,
    pending_root_id: str,
    completed_task_id: str,
    root_id: str,
    branch: str,
) -> None:
    from hermes_cli import kanban_db as kb

    latest = conn.execute(
        "SELECT kind FROM task_events WHERE task_id = ? "
        "ORDER BY id DESC LIMIT 1",
        (pending_root_id,),
    ).fetchone()
    if latest is not None and latest["kind"] == "children_approved_pending_root_integration":
        return
    with kb.write_txn(conn):
        kb._append_event(
            conn,
            pending_root_id,
            "children_approved_pending_root_integration",
            {
                "completed_task_id": completed_task_id,
                "chain_root": root_id,
                "branch": branch,
                "reason": "all children approved; root finalizer pending",
            },
        )


def provision_for_task(
    conn: sqlite3.Connection,
    task,
    resolved,
    *,
    board: Optional[str] = None,
) -> Path:
    """Claim-time provisioning hook (called by ``dispatch_once`` when
    ``worker_isolation == "worktree"``). Returns the workspace the worker
    should actually use.

    No-ops (returns *resolved* unchanged) for scratch tasks and for
    ``dir`` tasks whose path is not a git repo. Raises ``WorktreeError``
    on git failures — the dispatcher records a spawn failure, exactly like
    a ``resolve_workspace`` error.

    Exception to the scratch no-op: a scratch task assigned to a CODE role
    on a board whose ``default_workdir`` is a git repo gets redirected there
    (see :func:`scratch_code_redirect`) — those workers cd into the repo and
    edit it regardless of their scratch dir, so leaving them unprovisioned
    means unisolated writes to the live checkout.
    """
    from hermes_cli import kanban_db as kb

    kind = task.workspace_kind or "scratch"
    if kind not in ("dir", "worktree"):
        redirect = scratch_code_redirect(task, board)
        if redirect is None:
            return Path(resolved)
        resolved = redirect
        with kb.write_txn(conn):
            conn.execute(
                "UPDATE tasks SET workspace_kind = 'dir' WHERE id = ?",
                (task.id,),
            )
        task.workspace_kind = "dir"

    resolved = Path(resolved)
    provisioned = split_provisioned_path(resolved)
    if provisioned is not None:
        # Already provisioned (chain child / retry). Re-create if the
        # worktree vanished (e.g. removed after an earlier merge); the
        # task's (sub)path inside the worktree is preserved as-is.
        repo_root, root_id, wt = provisioned
        if not (wt / ".git").exists():
            ensure_worktree(repo_root, root_id)
        return resolved

    repo_root = repo_root_for(resolved)
    if repo_root is None:
        return resolved  # non-repo dir task: today's behavior, untouched

    root_id = chain_root_id(conn, task.id)
    info = ensure_worktree(repo_root, root_id)
    # A workspace pointing at a SUBDIRECTORY of the repo keeps its relative
    # part inside the worktree (e.g. <repo>/web → <worktree>/web).
    try:
        rel = resolved.resolve().relative_to(Path(repo_root).resolve())
    except ValueError:
        rel = Path(".")
    workspace = info["path"] / rel if str(rel) != "." else info["path"]
    kb.set_workspace_path(conn, task.id, str(workspace))
    with kb.write_txn(conn):
        # Freeze the merge target ONCE per chain (first claim wins).
        existing = conn.execute(
            "SELECT 1 FROM task_events "
            "WHERE task_id = ? AND kind = 'worktree_provisioned' LIMIT 1",
            (root_id,),
        ).fetchone()
        if not existing:
            kb._append_event(
                conn, root_id, "worktree_provisioned",
                {
                    "worktree": str(info["path"]),
                    "branch": info["branch"],
                    "merge_target": info["base_branch"],
                    "repo_root": str(repo_root),
                    "provisioned_for": task.id,
                },
            )
        # Stamp the task branch so the worker env carries
        # HERMES_KANBAN_BRANCH (existing _default_spawn mechanic).
        conn.execute(
            "UPDATE tasks SET branch_name = ? WHERE id = ?",
            (info["branch"], task.id),
        )
    return workspace


def note_dirty_worktree(conn: sqlite3.Connection, task_id: str, workspace) -> None:
    """Review-lane helper: surface uncommitted leftovers in a provisioned
    worktree to the verifier (grounds for REQUEST_CHANGES). Best-effort."""
    try:
        provisioned = split_provisioned_path(workspace)
        if provisioned is None:
            return
        _, _, wt = provisioned
        if not (wt / ".git").exists():
            return
        leftovers = dirty_files(wt)
        if not leftovers:
            return
        from hermes_cli import kanban_db as kb

        listing = ", ".join(sorted(leftovers)[:15])
        if len(leftovers) > 15:
            listing += f", … ({len(leftovers)} total)"
        kb.add_comment(
            conn, task_id, "integrator",
            "⚠️ Working tree is NOT clean after the worker run — uncommitted "
            f"changes: {listing}. The worker contract requires committing on "
            "the task branch when gates are green; uncommitted leftovers are "
            "grounds for REQUEST_CHANGES.",
        )
    except Exception:
        _log.debug("note_dirty_worktree failed for %s", task_id, exc_info=True)


# ---------------------------------------------------------------------------
# Integration (serialized merge into the frozen target)
# ---------------------------------------------------------------------------

def _acquire_file_lock(lock_path: Path, timeout: int = LOCK_TIMEOUT_SECONDS):
    """Cross-process exclusive lock (flock with O_EXCL fallback)."""
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    try:
        import fcntl

        fh = open(lock_path, "a+", encoding="utf-8")
        deadline = time.monotonic() + timeout
        while True:
            try:
                fcntl.flock(fh.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
                return fh
            except OSError:
                if time.monotonic() > deadline:
                    fh.close()
                    raise WorktreeError(
                        f"integrator lock {lock_path} not acquired in {timeout}s"
                    )
                time.sleep(1.0)
    except ImportError:
        # Non-POSIX fallback: exclusive-create spin lock.
        deadline = time.monotonic() + timeout
        while True:
            try:
                fd = os.open(str(lock_path) + ".x", os.O_CREAT | os.O_EXCL | os.O_WRONLY)
                return ("excl", fd, str(lock_path) + ".x")
            except FileExistsError:
                if time.monotonic() > deadline:
                    raise WorktreeError(
                        f"integrator lock {lock_path} not acquired in {timeout}s"
                    )
                time.sleep(1.0)


def _release_file_lock(handle) -> None:
    try:
        if isinstance(handle, tuple):
            _, fd, path = handle
            os.close(fd)
            os.unlink(path)
        else:
            handle.close()
    except Exception:
        pass


def remove_worktree(repo_root: Path, wt_path: Path, branch: str) -> None:
    """Remove a merged chain's worktree + branch. Best-effort, but the
    node_modules symlinks are unlinked first so ``worktree remove`` never
    sees them as content."""
    for rel in _NODE_MODULES_LINKS:
        link = Path(wt_path) / rel
        try:
            if link.is_symlink():
                link.unlink()
        except OSError:
            pass
    _git(repo_root, "worktree", "remove", str(wt_path), check=False)
    if Path(wt_path).exists():
        # Symlink edge cases can make `worktree remove` refuse; the branch
        # is merged at this point, so force is safe.
        _git(repo_root, "worktree", "remove", "--force", str(wt_path), check=False)
    _git(repo_root, "worktree", "prune", check=False)
    if Path(wt_path).exists():
        shutil.rmtree(wt_path, ignore_errors=True)
    # -d (not -D): only delete when actually merged.
    _git(repo_root, "branch", "-d", branch, check=False)
    # Tidy empty namespace dirs so a fully-drained repo has no .worktrees/
    # residue (best-effort: rmdir only succeeds on empty dirs).
    base = Path(repo_root) / WORKTREES_DIRNAME
    for d in (base / WORKTREES_NAMESPACE, base):
        try:
            d.rmdir()
        except OSError:
            break


def _affected_pytest_modules(repo_root: Path, changed_files: list[str]) -> list[str]:
    """Map a diff to existing pytest modules: changed test files run
    themselves; ``<pkg>/<name>.py`` runs ``tests/<pkg>/test_<name>.py``."""
    modules: set[str] = set()
    for f in changed_files:
        if not f.endswith(".py"):
            continue
        name = Path(f).name
        if f.startswith("tests/stress/") and name.startswith("test_"):
            # Stress scripts use their own @scenario registry / main(), not
            # pytest test functions. Feeding them to pytest returns exit 5
            # ("no tests ran") and falsely parks otherwise valid chains.
            continue
        if f.startswith("tests/") and name.startswith("test_"):
            if (repo_root / f).is_file():
                modules.add(f)
            continue
        rel_dir = str(Path(f).parent)
        candidate = Path("tests") / rel_dir / f"test_{name}"
        if (repo_root / candidate).is_file():
            modules.add(str(candidate))
    return sorted(modules)


def _resolve_node_bin(repo_root: Path, name: str) -> Optional[Path]:
    """Resolve a node executable tolerant of npm-workspace hoisting.

    A workspace's direct dependency bin may live in ``web/node_modules/.bin``
    OR be deduped (hoisted) into the ROOT ``node_modules/.bin`` when no other
    workspace pins a conflicting version (npm's default). Returning the first
    that exists keeps the non-hoisted layout working unchanged while also
    handling the hoisted layout — the cause of the 2026-06-20 burn-dashboard
    auto-revert, where ``typescript`` had hoisted to root and the hard-coded
    ``web/node_modules/.bin/tsc`` path no longer existed after ``npm ci``.
    """
    for base in (repo_root / "web", repo_root):
        cand = base / "node_modules" / ".bin" / name
        if cand.is_file():
            return cand
    return None


def default_quick_gate(repo_root: Path, changed_files: list[str]) -> tuple[bool, str]:
    """Post-merge quick gate (Entscheidung 5): ruff + affected pytest
    modules; when the diff touches ``web/``, run lint:control,
    ``tsc -b --noEmit``, and the control Vitest suite. ``npm run build`` intentionally
    stays out of this automatic merge path because it mutates generated
    dashboard assets and belongs to the parked post-merge release gate."""
    notes: list[str] = []

    def _run(label: str, cmd: list[str], cwd: Path, timeout: int) -> Optional[str]:
        try:
            proc = subprocess.run(  # noqa: S603 -- fixed argv
                cmd, cwd=str(cwd), capture_output=True, text=True, timeout=timeout,
            )
        except subprocess.TimeoutExpired:
            return f"{label}: TIMEOUT after {timeout}s"
        except FileNotFoundError:
            return f"{label}: command not found ({cmd[0]})"
        if proc.returncode != 0:
            tail = (proc.stdout + "\n" + proc.stderr).strip()[-2000:]
            return f"{label}: exit {proc.returncode}\n{tail}"
        notes.append(f"{label} ok")
        return None

    ruff_bin = shutil.which("ruff")
    # #3-C: run ruff only over the changed .py files, not the whole repo.
    # Uses the same diff source (changed_files) already computed by the caller
    # for the affected-pytest-modules logic — no extra git subprocess needed.
    # Fallback: if changed_files is empty/unavailable we get _changed_py==[]
    # and no .py files → skip ruff (non-Python diff). If somehow the file
    # list can't be trusted the caller should pass [] and the integrator can
    # always force a full gate manually.
    _changed_py: list[str] = [f for f in changed_files if f.endswith(".py")]
    if _changed_py:
        ruff_base = [ruff_bin, "check"] if ruff_bin else [sys.executable, "-m", "ruff", "check"]
        ruff_cmd = ruff_base + _changed_py + ["--extend-exclude", WORKTREES_DIRNAME]
        err = _run("ruff", ruff_cmd, repo_root, 300)
        if err:
            return False, err
    else:
        # No .py files in diff — skip ruff entirely (non-Python-only change).
        notes.append("ruff skipped (no .py files in diff)")

    modules = _affected_pytest_modules(repo_root, changed_files)
    if modules:
        err = _run(
            f"pytest[{len(modules)}]",
            [sys.executable, "-m", "pytest", "-q", "-p", "no:cacheprovider", *modules],
            repo_root, 1200,
        )
        if err:
            return False, err
    else:
        notes.append("pytest skipped (no affected test modules)")

    if any(f.startswith("web/") for f in changed_files):
        web_root = repo_root / "web"
        # Resolve tolerant of npm-workspace hoisting (bins may live in web/ OR
        # the hoisted ROOT node_modules/.bin). See _resolve_node_bin.
        tsc = _resolve_node_bin(repo_root, "tsc")
        vitest = _resolve_node_bin(repo_root, "vitest")
        npm_bin = shutil.which("npm") or "npm"
        npx_bin = shutil.which("npx") or "npx"

        err = _run("lint:control", [npm_bin, "run", "lint:control"], web_root, 600)
        if err:
            return False, err
        if tsc is None:
            # Fail closed: a web diff we cannot type-check is not "green".
            return False, "tsc: web/ in diff but tsc not found in web/ or root node_modules/.bin"
        err = _run("tsc -b", [str(tsc), "-b", "--noEmit"], web_root, 600)
        if err:
            return False, err
        if vitest is None:
            return False, (
                "vitest[control]: web/ in diff but "
                "vitest not found in web/ or root node_modules/.bin"
            )
        err = _run("vitest[control]", [npx_bin, "vitest", "run", "src/control"], web_root, 900)
        if err:
            return False, err

    return True, "; ".join(notes)


def fo_integration_gate(repo_root: Path, changed_files: list[str]) -> tuple[bool, str]:
    """FO post-merge integration gate: ONE `npm run build` (the heavy gate that
    runs once at integration; lint/backlog:check/test are the worker gate, D).
    ``changed_files`` is accepted for signature-compatibility with
    default_quick_gate but unused — the build is whole-repo.

    Self-heals a missing dependency tree first. The build runs in the LIVE FO
    checkout (``repo_root``), whose gitignored ``node_modules`` can be emptied
    out of band — a worker ``npm ci`` through the provisioner's node_modules
    symlink, a cleanup, or a fresh checkout. When ``next`` is absent, ``next
    build`` exits 127 and the integrator would REVERT already-approved work
    (t_8fbe701d, 2026-06-14). So if the ``next`` bin is missing we run ``npm
    ci`` once to restore deps before building, instead of failing the gate."""
    repo_root = Path(repo_root)
    npm_bin = shutil.which("npm") or "npm"
    notes: list[str] = []

    if not (repo_root / "node_modules" / ".bin" / "next").exists():
        try:
            ci = subprocess.run(  # noqa: S603 -- fixed argv
                [npm_bin, "ci"], cwd=str(repo_root),
                capture_output=True, text=True, timeout=900,
            )
        except subprocess.TimeoutExpired:
            return False, "npm ci (self-heal, node_modules was missing): TIMEOUT after 900s"
        except FileNotFoundError:
            return False, f"npm ci (self-heal): command not found ({npm_bin})"
        if ci.returncode != 0:
            tail = (ci.stdout + "\n" + ci.stderr).strip()[-2000:]
            return False, (
                "npm ci (self-heal, node_modules was missing): "
                f"exit {ci.returncode}\n{tail}"
            )
        notes.append("npm ci (self-healed missing node_modules)")

    try:
        proc = subprocess.run(  # noqa: S603 -- fixed argv
            [npm_bin, "run", "build"], cwd=str(repo_root),
            capture_output=True, text=True, timeout=900,
        )
    except subprocess.TimeoutExpired:
        return False, "build: TIMEOUT after 900s"
    except FileNotFoundError:
        return False, f"build: command not found ({npm_bin})"
    if proc.returncode != 0:
        tail = (proc.stdout + "\n" + proc.stderr).strip()[-2000:]
        return False, f"build: exit {proc.returncode}\n{tail}"
    notes.append("npm run build ok")
    return True, "; ".join(notes)


def integrate_chain(
    repo_root: Path,
    wt_path: Path,
    branch: str,
    merge_target: Optional[str],
    *,
    gate_runner: Optional[Callable[[Path, list[str]], tuple[bool, str]]] = None,
) -> dict:
    """Merge a finished chain branch into the live branch — THE single
    serialized integration point. Never pushes.

    Returns a dict with ``action`` ∈ ``merged`` (merge landed, gate green),
    ``clean`` (nothing to merge; worktree removed) or ``parked`` (a
    pre-check, the merge, or the post-merge gate failed — ``reason`` says
    why; the caller parks the task instead of guessing).
    """
    repo_root = Path(repo_root)
    wt_path = Path(wt_path)

    def parked(reason: str, **extra) -> dict:
        out = {"action": "parked", "reason": reason, "branch": branch}
        out.update(extra)
        return out

    # Lock lives in the repo's .git dir: never visible in `git status`,
    # never blocks the empty-namespace rmdir tidy in remove_worktree, and
    # exactly one location per repo regardless of worktree layout.
    try:
        lock_path = (
            Path(_git(repo_root, "rev-parse", "--absolute-git-dir"))
            / "hermes-kanban-integrator.lock"
        )
    except (WorktreeError, subprocess.SubprocessError, OSError):
        lock_path = (
            repo_root / WORKTREES_DIRNAME / WORKTREES_NAMESPACE / ".integrator.lock"
        )
    with _PROCESS_LOCK:
        try:
            lock = _acquire_file_lock(lock_path)
        except WorktreeError as exc:
            return parked(str(exc))
        try:
            if not _branch_exists(repo_root, branch):
                return {"action": "clean", "branch": branch,
                        "reason": "chain branch does not exist (nothing to merge)"}

            # (0) live checkout in a clean operation state + frozen target.
            try:
                git_dir = Path(_git(repo_root, "rev-parse", "--absolute-git-dir"))
            except WorktreeError as exc:
                return parked(f"cannot inspect live checkout: {exc}")
            for marker in ("MERGE_HEAD", "CHERRY_PICK_HEAD", "REVERT_HEAD",
                           "rebase-merge", "rebase-apply"):
                if (git_dir / marker).exists():
                    return parked(
                        f"live checkout has an operation in progress ({marker})"
                    )
            try:
                cur = current_branch(repo_root)
            except WorktreeError as exc:
                return parked(str(exc))
            if merge_target and cur != merge_target:
                return parked(
                    f"checked-out branch {cur!r} != frozen merge target "
                    f"{merge_target!r}"
                )

            ahead = _git(repo_root, "rev-list", "--count", f"{cur}..{branch}")
            if ahead == "0":
                if wt_path.exists() and (wt_path / ".git").exists() and dirty_files(wt_path):
                    return parked(
                        "worktree has uncommitted changes but no commits to merge"
                    )
                already_integrated = _branch_is_ancestor(repo_root, branch, cur)
                remove_worktree(repo_root, wt_path, branch)
                if already_integrated:
                    return {
                        "action": "clean",
                        "branch": branch,
                        "target": cur,
                        "already_integrated": True,
                        "reason": f"chain branch already reachable from {cur}",
                    }
                return {"action": "clean", "branch": branch,
                        "reason": "no commits on chain branch"}

            # The chain's own worktree must be fully committed.
            if wt_path.exists() and (wt_path / ".git").exists():
                leftovers = dirty_files(wt_path)
                if leftovers:
                    return parked(
                        "chain worktree has uncommitted changes: "
                        + ", ".join(sorted(leftovers)[:10])
                    )

            diff_files = [
                f for f in _git(
                    repo_root, "diff", "--name-only", f"{cur}...{branch}"
                ).splitlines() if f
            ]

            # (a) overlap of foreign dirty files with the branch diff.
            dirty = set(dirty_files(repo_root))
            overlap = sorted(dirty & set(diff_files))
            if overlap:
                return parked(
                    "dirty files in live checkout overlap the branch diff: "
                    + ", ".join(overlap[:10])
                )

            # (a2) Rebase the chain branch onto the live target HEAD inside its
            # OWN worktree (B1), so the following merge is FF/conflict-free.
            # Reuse `cur` (the frozen, already-validated merge target) — do NOT
            # git fetch: repo_root is a LOCAL checkout, `cur`/HEAD is the live
            # local tip, and this integrator never pushes. (If a chain branch
            # could legitimately diverge from a REMOTE, escalate — do not add a
            # network fetch here.)
            if not (wt_path.exists() and (wt_path / ".git").exists()):
                return parked("chain worktree missing before rebase")
            target_head = _git(repo_root, "rev-parse", cur)
            try:
                _git(wt_path, "rebase", target_head, timeout=MERGE_TIMEOUT_SECONDS)
            except (WorktreeError, subprocess.TimeoutExpired) as exc:
                # Conflict (or timeout): abort cleanly so the worktree returns to
                # its pre-rebase committed state, then signal rebase_conflict so
                # complete_task routes the task back to the coder (NOT a park).
                _git(wt_path, "rebase", "--abort", check=False)
                return {
                    "action": "rebase_conflict",
                    "branch": branch,
                    "reason": (
                        f"rebase of {branch} onto {cur} hit a conflict "
                        f"(aborted, returned to coder): {exc}"
                    ),
                    "target": cur,
                }
            # Successful rebase: fall through to the existing merge block. The
            # --no-ff merge stays (preserves the merge-commit audit trail).

            # (b) the merge itself; conflicts → abort + park.
            msg = f"kanban: merge {branch} (worker-isolation integrator)"
            try:
                _git(repo_root, "merge", "--no-ff", "--no-edit", "-m", msg,
                     branch, timeout=MERGE_TIMEOUT_SECONDS)
            except (WorktreeError, subprocess.TimeoutExpired) as exc:
                _git(repo_root, "merge", "--abort", check=False)
                return parked(f"merge conflict/failure (aborted): {exc}")
            merge_commit = _git(repo_root, "rev-parse", "HEAD")

            # Post-merge quick gate (Entscheidung 5); red → revert -m 1 + park.
            gate = gate_runner or (
                fo_integration_gate if _is_fo_repo(repo_root) else default_quick_gate
            )
            try:
                ok, detail = gate(repo_root, diff_files)
            except Exception as exc:  # a broken gate must not pass silently
                ok, detail = False, f"gate crashed: {exc}"
            if not ok:
                try:
                    _git(repo_root, "revert", "-m", "1", "--no-edit",
                         merge_commit, timeout=MERGE_TIMEOUT_SECONDS)
                    reverted = True
                except (WorktreeError, subprocess.TimeoutExpired) as exc:
                    _git(repo_root, "revert", "--abort", check=False)
                    reverted = False
                    detail += f" — AND REVERT FAILED: {exc}"
                return parked(
                    f"post-merge gate failed: {detail}",
                    merge_commit=merge_commit, reverted=reverted,
                )

            remove_worktree(repo_root, wt_path, branch)
            return {
                "action": "merged",
                "state": MERGED_GREEN,
                "merge_commit": merge_commit,
                "branch": branch,
                "target": cur,
                "gate": detail,
                "files": len(diff_files),
                "changed_files": diff_files,
            }
        finally:
            _release_file_lock(lock)


def maybe_integrate_on_complete(
    conn: sqlite3.Connection,
    task_id: str,
    *,
    gate_runner=None,
) -> Optional[dict]:
    """Completion hook (called by ``complete_task`` on the direct done
    path, i.e. after Verifier-APPROVED routing): when *task_id* is the last
    open task of a provisioned chain, integrate the chain.

    Returns ``None`` when not applicable (non-provisioned workspace),
    ``{"action": "deferred"}`` while chain siblings are still open, or the
    ``integrate_chain`` outcome (events + receipt comment already written).
    A ``parked`` outcome means the caller must NOT move the task to done.
    """
    row = conn.execute(
        "SELECT workspace_path FROM tasks WHERE id = ?", (task_id,)
    ).fetchone()
    if not row or not row["workspace_path"]:
        return None
    provisioned = split_provisioned_path(row["workspace_path"])
    if provisioned is None:
        return None
    repo_root, root_id, wt = provisioned

    from hermes_cli import kanban_db as kb

    # Chain-complete check via BOTH signals, conservatively OR-ed:
    # (a) task_links membership from the chain root — covers unclaimed
    #     children whose workspace_path still points at the repo root;
    # (b) same provisioned worktree path — covers tasks attached to the
    #     worktree outside the link graph (e.g. cloned fix tasks).
    members = _chain_member_ids(conn, root_id)
    members.discard(task_id)
    open_sibling = None
    if members:
        placeholders = ",".join("?" for _ in members)
        open_sibling = conn.execute(
            f"SELECT 1 FROM tasks WHERE id IN ({placeholders}) "
            "AND status NOT IN ('done', 'archived', 'failed', 'cancelled') "
            "LIMIT 1",
            tuple(members),
        ).fetchone()
    if open_sibling is None:
        like = (
            str(wt).replace("\\", "\\\\").replace("%", r"\%").replace("_", r"\_")
            + "%"
        )
        open_sibling = conn.execute(
            "SELECT 1 FROM tasks WHERE workspace_path LIKE ? ESCAPE '\\' "
            "AND id != ? "
            "AND status NOT IN ('done', 'archived', 'failed', 'cancelled') "
            "LIMIT 1",
            (like, task_id),
        ).fetchone()
    if open_sibling:
        pending_root_id = _pending_root_finalizer_id(
            conn, task_id=task_id, root_id=root_id, wt=wt, members=members,
        )
        if pending_root_id is not None:
            try:
                _record_pending_root_finalizer(
                    conn,
                    pending_root_id=pending_root_id,
                    completed_task_id=task_id,
                    root_id=root_id,
                    branch=chain_branch(root_id),
                )
            except Exception:
                _log.debug("pending-root-finalizer event failed", exc_info=True)
        return {"action": "deferred", "reason": "chain has open siblings"}

    target = frozen_merge_target(conn, root_id)
    branch = chain_branch(root_id)
    if not _branch_exists(repo_root, branch):
        outcome = {
            "action": "parked",
            "reason": f"missing branch evidence for root finalizer: {branch}",
            "branch": branch,
            "target": target,
        }
        try:
            with kb.write_txn(conn):
                kb._append_event(conn, task_id, "integration_parked", outcome)
        except Exception:
            _log.warning("could not record missing-branch event for %s", task_id,
                         exc_info=True)
        return outcome
    outcome = integrate_chain(
        repo_root, wt, branch, target, gate_runner=gate_runner,
    )

    try:
        with kb.write_txn(conn):
            kind = {
                "merged": "integration_merged",
                "clean": "integration_clean",
                "rebase_conflict": "integration_rebase_conflict",
            }.get(outcome["action"], "integration_parked")
            kb._append_event(conn, task_id, kind, outcome)
            if outcome["action"] == "merged":
                kb._append_event(
                    conn,
                    task_id,
                    "INTEGRATOR_VERIFIED",
                    {
                        "merge_commit": outcome.get("merge_commit"),
                        "gate": outcome.get("gate"),
                        "state": outcome.get("state"),
                    },
                )
    except Exception:
        _log.warning("could not record integration event for %s", task_id,
                     exc_info=True)
    if outcome["action"] == "merged":
        try:
            kb.add_comment(
                conn, task_id, "integrator",
                f"✅ Integrated: merged `{outcome['branch']}` into "
                f"`{outcome['target']}` as `{outcome['merge_commit'][:12]}` "
                f"(--no-ff, post-merge gate green: {outcome.get('gate', '')}). "
                "Worktree and branch removed. Not pushed.",
            )
        except Exception:
            _log.debug("integration receipt comment failed", exc_info=True)
        if any(f.startswith("web/") for f in outcome.get("changed_files", [])):
            try:
                _create_parked_release_gate_child(conn, task_id, root_id, outcome)
            except Exception:
                _log.warning(
                    "could not create parked release-gate child for %s",
                    task_id, exc_info=True,
                )
    elif outcome["action"] == "clean" and outcome.get("already_integrated"):
        try:
            kb.add_comment(
                conn, task_id, "integrator",
                f"✅ Integration clean: `{outcome['branch']}` is already "
                f"reachable from `{outcome.get('target', target)}`. "
                "Worktree and branch removed. Not pushed.",
            )
        except Exception:
            _log.debug("already-integrated receipt comment failed", exc_info=True)
    return outcome
