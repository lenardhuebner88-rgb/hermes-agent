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
from typing import Callable, Optional

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
    "node_modules",
    "web/node_modules",
)
# Tool/cache byproducts that are never commit content and never part of a
# branch diff. Gate runs themselves produce these (the verifier's
# `ruff`/`pytest` in the worktree writes __pycache__), so counting them as
# "uncommitted changes" would park every chain whose repo doesn't gitignore
# them — observed live in the 2026-06-11 E2E probe.
_IGNORED_DIRTY_DIR_PARTS = frozenset({
    "__pycache__", ".pytest_cache", ".ruff_cache", ".mypy_cache", ".venv",
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

# node_modules locations symlinked into a fresh worktree so frontend gates
# work without an npm ci (monorepo: .bin lives in the ROOT node_modules).
_NODE_MODULES_LINKS = ("node_modules", "web/node_modules")

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
    timeout: int = GIT_TIMEOUT_SECONDS,
) -> str:
    proc = subprocess.run(  # noqa: S603 -- fixed argv, no shell
        ["git", "-C", str(repo), *args],
        capture_output=True,
        text=True,
        timeout=timeout,
    )
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
    except WorktreeError:
        # A removed-but-still-registered worktree blocks re-adding; prune
        # the bookkeeping once and retry.
        _git(repo_root, "worktree", "prune", check=False)
        _add()

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


def default_quick_gate(repo_root: Path, changed_files: list[str]) -> tuple[bool, str]:
    """Post-merge quick gate (Entscheidung 5): ruff + affected pytest
    modules; tsc --noEmit only when the diff touches ``web/``."""
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
    ruff_cmd = [ruff_bin, "check", "."] if ruff_bin else [
        sys.executable, "-m", "ruff", "check", "."
    ]
    # Other chains' worktrees live under .worktrees/ in the SAME repo —
    # their in-progress state must never fail THIS chain's gate (observed
    # in the 2026-06-11 gate probe: ruff also flagged the worktree copy).
    ruff_cmd += ["--extend-exclude", WORKTREES_DIRNAME]
    err = _run("ruff", ruff_cmd, repo_root, 300)
    if err:
        return False, err

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
        tsc = repo_root / "web" / "node_modules" / ".bin" / "tsc"
        if not tsc.is_file():
            # Fail closed: a web diff we cannot type-check is not "green".
            return False, "tsc: web/ in diff but web/node_modules/.bin/tsc missing"
        err = _run("tsc", [str(tsc), "--noEmit"], repo_root / "web", 600)
        if err:
            return False, err

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
                remove_worktree(repo_root, wt_path, branch)
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
            gate = gate_runner or default_quick_gate
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
                "merge_commit": merge_commit,
                "branch": branch,
                "target": cur,
                "gate": detail,
                "files": len(diff_files),
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
        return {"action": "deferred", "reason": "chain has open siblings"}

    target = frozen_merge_target(conn, root_id)
    outcome = integrate_chain(
        repo_root, wt, chain_branch(root_id), target, gate_runner=gate_runner,
    )

    try:
        with kb.write_txn(conn):
            kind = {
                "merged": "integration_merged",
                "clean": "integration_clean",
            }.get(outcome["action"], "integration_parked")
            kb._append_event(conn, task_id, kind, outcome)
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
    return outcome
