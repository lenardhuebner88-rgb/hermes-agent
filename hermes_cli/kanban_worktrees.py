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

Module layout (section order)::

    Config / path predicates
    Git plumbing
    Provisioning + chain helpers
    Release-gate executor (context, visual gate, fixer, activation)
    Integration (locks, affected tests, quick gates, integrate_chain)
    Completion hook (maybe_integrate_on_complete)
"""

from __future__ import annotations

from datetime import datetime, timezone
import inspect
import json
import logging
import os
import re
import shlex
import shutil
import sqlite3
import subprocess
import sys
import threading
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Optional, Sequence

# Cap for the package-directory fallback in _affected_pytest_modules: if the
# package test directory contains more than this many test_*.py files, the
# fallback downgrades to no selection (nightly full suite remains the
# backstop). Must stay in sync with scripts/affected_tests.py.
#
# Calibrated (2026-07-16): tests/hermes_cli/=592, tests/gateway/=460,
# tests/tools/=318 files. 800 covers all current directories; anything
# larger would exceed the targeted-gate walltime budget.
_FALLBACK_MAX_TEST_FILES = 800


def _imports_changed_module(test_path: Path, module_import: str) -> bool:
    try:
        content = test_path.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError):
        return False

    package, _, module_name = module_import.rpartition(".")
    direct_import = rf"^\s*import\s+.*\b{re.escape(module_import)}\b"
    if re.search(direct_import, content, re.MULTILINE):
        return True
    submodule_from_import = rf"^\s*from\s+{re.escape(module_import)}\s+import\b"
    if re.search(submodule_from_import, content, re.MULTILINE):
        return True
    if package:
        package_import = rf"^\s*from\s+{re.escape(package)}\s+import\s+.*\b{re.escape(module_name)}\b"
        if re.search(package_import, content, re.MULTILINE):
            return True
    return False


def _feature_named_sibling_tests(repo_root: Path, rel_dir: str, source: Path) -> list[str]:
    module_import = str(source.with_suffix("")).replace("/", ".")
    # Feature tests also live directly at tests/ root (e.g.
    # tests/test_design_board_store.py for hermes_cli/design_board_store.py);
    # glob is non-recursive, so the root scan only matches those.
    test_dirs = [repo_root / "tests"]
    pkg_test_dir = Path("tests") / rel_dir
    absolute_pkg_test_dir = repo_root / pkg_test_dir
    if pkg_test_dir != Path("tests") and absolute_pkg_test_dir.is_dir():
        test_dirs.append(absolute_pkg_test_dir)
    return [
        str(path.relative_to(repo_root))
        for test_dir in test_dirs
        for path in sorted(test_dir.glob("test_*.py"))
        if _imports_changed_module(path, module_import)
    ]

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
    # Playwright storageState dir: holds session tokens / auth secrets.
    # Must NEVER be preserved to the Vault receipt tree (secret leak).
    # Ignored from dirty-file detection so it doesn't park the chain
    # either — it's a tool byproduct like node_modules.
    "playwright/.auth/",
    # Critic profile metadata: transient per-run output, not a visual/test
    # artifact. Without this ignore it matches _ARTIFACT_LIKE_SUFFIXES via
    # '.json' and parks the chain with ARTIFACT_POLICY_MISSING.
    ".critic_meta.json",
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
# them — observed live in the 2026-06-11 E2E probe. Visual-QA artifacts are
# handled separately by the integrator preserve/cleanup path below: they must
# be visible as dirty files so the chain can copy them to a durable receipt
# directory before removing the worktree.
#
# NOTE (belt-and-suspenders): `dirty_files()` runs `git status --porcelain`
# WITHOUT `--ignored`, so anything the repo gitignores is already invisible
# here. Every entry below is also in this repo's `.gitignore`, so the list only
# does real work for paths a repo does NOT gitignore. The residual gap is a NEW
# tool whose scratch dir isn't gitignored yet — that still parks a chain until
# the `.gitignore` (and, optionally, this list) catches up. A hard pre-submit
# "clean worktree" gate is deliberately NOT used: it would also block a worker
# that legitimately left genuine untracked work, which SHOULD park for review.
_IGNORED_DIRTY_DIR_PARTS = frozenset({
    "__pycache__", ".pytest_cache", ".ruff_cache", ".mypy_cache", ".venv",
})
_IGNORED_DIRTY_SUFFIXES = (".pyc", ".pyo")

_PRESERVABLE_ARTIFACT_PREFIXES = (
    ".playwright-mcp/",
    "playwright-report/",
    "test-results/",
    "visual-qa/",
    "artifacts/",
    # Common visual-QA output dir: bare `screenshots/` (Playwright/Cypress
    # screenshot dumps). Previously parked as DIRTY_WORKTREE; preserve to the
    # receipt tree instead.
    #
    # NOTE: `playwright/.auth/` is deliberately NOT here — it holds auth
    # storageState (session tokens). Preserving it would copy secrets into
    # the Vault receipt tree via shutil.copy2. It is ignored in dirty_files()
    # via _IGNORED_DIRTY_PREFIXES instead, so it neither parks nor preserves.
    "screenshots/",
)


def _resolve_chromium_shot() -> str:
    """Resolve the ``chromium-shot`` screenshot binary.

    The release-gate activation runs in a transient systemd unit whose PATH is
    an explicit allowlist that does NOT include ``~/bin`` (where chromium-shot
    lives), so a bare ``chromium-shot`` lookup raises FileNotFoundError, the
    visual gate reports RED, and a bounded fixer is spawned to chase a phantom
    CSS bug (observed 2026-07-12). Resolve PATH first, then the known ``~/bin``
    location — matching design_board_kanban's absolute-path convention — so the
    gate finds the tool regardless of the activation env's PATH."""
    return shutil.which("chromium-shot") or os.path.expanduser("~/bin/chromium-shot")


_ARTIFACT_LIKE_PREFIXES = _PRESERVABLE_ARTIFACT_PREFIXES + (
    "blob-report/",
    "coverage/",
    "htmlcov/",
)
_ARTIFACT_LIKE_SUFFIXES = (
    ".gif",
    ".html",
    ".jpg",
    ".jpeg",
    ".json",
    ".log",
    ".png",
    ".svg",
    ".txt",
    ".webm",
    ".zip",
)
_ARTIFACT_RECEIPTS_ROOT = Path(
    "/home/piet/vault/03-Agents/Hermes/receipts/artifacts"
)

DIRTY_WORKTREE_CLASS = "DIRTY_WORKTREE"
PRESERVABLE_ARTIFACTS_CLASS = "PRESERVABLE_ARTIFACTS"
ARTIFACT_POLICY_MISSING_CLASS = "ARTIFACT_POLICY_MISSING"
# Retained for classifying/retrying historical parked events created before
# gates moved out of the shared live checkout. New gates run in clean detached
# validation worktrees and no longer emit this class.
FOREIGN_DIRTY_CHECKOUT_CLASS = "foreign_dirty_checkout"


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


def _integration_gate_config() -> dict:
    """Read ``kanban.integration_gate`` from the root Hermes config."""
    raw: dict = {}
    try:
        import yaml
        from hermes_constants import get_default_hermes_root

        cfg_path = get_default_hermes_root() / "config.yaml"
        if cfg_path.is_file():
            with open(cfg_path, "r", encoding="utf-8") as fh:
                root_cfg = yaml.safe_load(fh) or {}
            candidate = (root_cfg.get("kanban") or {}).get("integration_gate") or {}
            if isinstance(candidate, dict):
                raw = candidate
    except Exception:
        raw = {}

    repos_raw = raw.get("repos") if isinstance(raw.get("repos"), dict) else {}
    repos: dict[str, list[str]] = {}
    for path, commands in repos_raw.items():
        if not isinstance(commands, (list, tuple)):
            continue
        try:
            key = str(Path(str(path)).resolve())
        except Exception:
            continue
        normalized = [str(command) for command in commands if str(command).strip()]
        if normalized:
            repos[key] = normalized

    try:
        timeout = int(raw.get("timeout", 900))
    except (TypeError, ValueError):
        timeout = 900
    if timeout <= 0:
        timeout = 900
    return {"repos": repos, "timeout": timeout}


def _configured_integration_gate(
    repo_root: Path,
    changed_files: list[str],
    *,
    commands: Sequence[str],
    timeout: int,
) -> tuple[bool, str]:
    """Run configured integration commands in the validation worktree."""
    del changed_files
    notes: list[str] = []
    for command in commands:
        argv = shlex.split(command)
        try:
            proc = subprocess.run(
                argv,
                cwd=str(repo_root),
                capture_output=True,
                text=True,
                timeout=timeout,
                check=False,
            )
        except subprocess.TimeoutExpired:
            return False, f"{command}: TIMEOUT after {timeout}s"
        except (OSError, subprocess.SubprocessError) as exc:
            return False, f"{command}: {exc}"
        if proc.returncode != 0:
            tail = ((proc.stdout or "") + "\n" + (proc.stderr or "")).strip()[-2000:]
            return False, f"{command}: exit {proc.returncode}\n{tail}"
        notes.append(f"{command} ok")
    return True, "; ".join(notes)


def _integration_gate_for_repo(
    repo_root: Path,
) -> Callable[[Path, list[str]], tuple[bool, str]]:
    """Select a configured repo gate, else preserve the existing heuristic."""
    config = _integration_gate_config()
    repo_key = str(Path(repo_root).resolve())
    commands = config["repos"].get(repo_key)
    if commands:
        timeout = config["timeout"]

        def configured_gate(
            worktree: Path, changed_files: list[str]
        ) -> tuple[bool, str]:
            return _configured_integration_gate(
                worktree,
                changed_files,
                commands=commands,
                timeout=timeout,
            )

        return configured_gate
    return fo_integration_gate if _is_fo_repo(repo_root) else default_quick_gate


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


RESOLVE_EXISTING_WORKSPACE = "resolve_existing"
MANAGED_WORKTREE_PROVISION = "managed_provision"
_WORKSPACE_MATERIALIZATION_MODES = frozenset(
    {RESOLVE_EXISTING_WORKSPACE, MANAGED_WORKTREE_PROVISION}
)


@dataclass(frozen=True)
class DispatchWorkspace:
    """One claim-time workspace selected by the canonical facade."""

    path: Path
    branch_name: Optional[str]
    mode: str


def materialize_dispatch_workspace(
    conn,
    task,
    *,
    mode: str,
    board: Optional[str],
    resolve_existing: Callable,
    resolve_managed_base: Callable,
) -> DispatchWorkspace:
    """Route claim-time workspace materialization through one explicit owner.

    ``resolve_existing`` is the upstream/default resolver. The managed mode
    resolves a non-materializing base first, then delegates provisioning to the
    isolation edge. Integration and release remain separate completion hooks.
    """
    if mode not in _WORKSPACE_MATERIALIZATION_MODES:
        raise ValueError(f"unknown workspace materialization mode: {mode!r}")
    if mode == RESOLVE_EXISTING_WORKSPACE:
        path, branch_name = resolve_existing(task, board=board)
        return DispatchWorkspace(Path(path), branch_name, mode)

    base = Path(resolve_managed_base(task, board=board))
    path = Path(provision_for_task(conn, task, base, board=board))
    branch_name: Optional[str] = None
    try:
        row = conn.execute(
            "SELECT branch_name FROM tasks WHERE id = ?", (task.id,)
        ).fetchone()
        if row is not None:
            branch_name = str(row["branch_name"] or "").strip() or None
    except Exception:
        branch_name = None
    return DispatchWorkspace(path, branch_name, mode)


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
        # P1 2026-07-05 (t_2fa852c6): a foreign session's dirty file in the
        # FIRST failing gate stage's scope is environment contamination, not
        # a defect in this chain's own diff — self-clears once the foreign
        # session commits/cleans up, so it belongs in the bounded
        # integration-retry lane (its own counter, never auto_retry_count/
        # consecutive_failures) rather than the needs_orchestrator
        # conflict-fixer lane below (which would dispatch a fixer to "repair"
        # code that was never broken — the chain-blame this park exists to
        # avoid).
        "foreign dirty checkout (",
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
    strip: bool = True,
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
    # ``strip=False`` for NUL-delimited output (``-z``): stripping would eat the
    # leading status-column space of the FIRST porcelain record (" M path"),
    # shifting the parse and dropping the first character of that path.
    return proc.stdout.strip() if strip else proc.stdout


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


def _first_parent_merges_reaching_branch(
    repo: Path, branch: str, target: str
) -> list[str]:
    """Merge commits on *target*'s first-parent history that merged *branch*.

    This is intentionally content-agnostic; callers use it only after branch is
    already known to be an ancestor of target.  A reverted merge is still an
    ancestry success, so we need the concrete merge commit(s) for follow-up
    tree/revert inspection.
    """
    merges = [
        line.strip()
        for line in _git(
            repo,
            "log",
            "--first-parent",
            "--merges",
            "--format=%H",
            target,
        ).splitlines()
        if line.strip()
    ]
    matches: list[str] = []
    for merge in merges:
        parents = _git(repo, "rev-list", "--parents", "-n", "1", merge).split()
        # ``parents`` = [merge, first-parent, merged-parent, ...].  The worker
        # integrator uses normal two-parent merge commits.
        for parent in parents[2:]:
            if _branch_is_ancestor(repo, branch, parent):
                matches.append(merge)
                break
    return matches


def _revert_commits_for_merge(repo: Path, merge_commit: str, target: str) -> list[str]:
    """Return commits on *target* that explicitly revert *merge_commit*."""
    return [
        line.strip()
        for line in _git(
            repo,
            "log",
            "--format=%H",
            f"--grep={merge_commit}",
            target,
        ).splitlines()
        if line.strip() and line.strip() != merge_commit
    ]


def _reverted_merged_ancestor(repo: Path, branch: str, target: str) -> Optional[str]:
    """Find a reverted merge parent whose reviewed patch is in *branch*."""
    # Merges at or before the branch/target merge-base cannot need recovery:
    # their matching reverts (if any) are already part of the worker branch.
    # Restricting the scan also avoids an O(history²) sequence of full-log
    # ``--grep`` searches for a fresh branch cut from a large repository.
    merge_base = _git(repo, "merge-base", branch, target)
    merges = _git(
        repo,
        "rev-list",
        "--first-parent",
        "--merges",
        f"{merge_base}..{target}",
    )
    for merge_commit in merges.splitlines():
        parents = _git(repo, "show", "-s", "--format=%P", merge_commit).split()
        if len(parents) < 2:
            continue
        merged_parent = parents[1]
        if not _branch_is_ancestor(repo, merged_parent, branch):
            continue
        reverts = _revert_commits_for_merge(repo, merge_commit, target)
        if not reverts or any(
            _branch_is_ancestor(repo, revert, branch) for revert in reverts
        ):
            continue
        changed = _changed_files_between(repo, parents[0], merged_parent)
        if changed and any(
            _git(repo, "rev-parse", f"{target}:{path}", check=False)
            != _git(repo, "rev-parse", f"{merged_parent}:{path}", check=False)
            for path in changed
        ):
            return merged_parent
    return None


def _changed_files_between(repo: Path, left: str, right: str) -> list[str]:
    return [
        f
        for f in _git(repo, "diff", "--name-only", left, right).splitlines()
        if f
    ]


def dirty_files(repo: Path) -> list[str]:
    """Porcelain status paths (incl. files inside untracked dirs), with the
    worktree namespace and the planted node_modules symlinks filtered out.

    Uses ``-z`` (NUL-separated) so paths with spaces/special chars arrive
    unquoted — the overlap check must compare exact paths. ``strip=False`` keeps
    the leading status-column space of the first record intact (see ``_git``)."""
    out = _git(repo, "status", "--porcelain", "-uall", "-z", strip=False)
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


def _artifact_receipt_timestamp() -> str:
    return datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")


def _normalize_dirty_path(path: str) -> str:
    return path.strip().lstrip("/")


def _is_preservable_artifact_path(path: str) -> bool:
    normalized = _normalize_dirty_path(path)
    if not normalized or ".." in Path(normalized).parts:
        return False
    return any(normalized.startswith(prefix) for prefix in _PRESERVABLE_ARTIFACT_PREFIXES)


def _looks_like_artifact_path(path: str) -> bool:
    normalized = _normalize_dirty_path(path)
    if not normalized or ".." in Path(normalized).parts:
        return False
    if _is_preservable_artifact_path(normalized):
        return True
    if any(normalized.startswith(prefix) for prefix in _ARTIFACT_LIKE_PREFIXES):
        return True
    return normalized.lower().endswith(_ARTIFACT_LIKE_SUFFIXES)


def _classify_dirty_paths(paths: Sequence[str]) -> str:
    dirty_paths = [path for path in paths if path]
    if dirty_paths and all(_is_preservable_artifact_path(path) for path in dirty_paths):
        return PRESERVABLE_ARTIFACTS_CLASS
    if dirty_paths and all(_looks_like_artifact_path(path) for path in dirty_paths):
        return ARTIFACT_POLICY_MISSING_CLASS
    return DIRTY_WORKTREE_CLASS


def _dirty_recovery_instruction(dirty_class: str) -> str:
    if dirty_class == PRESERVABLE_ARTIFACTS_CLASS:
        return (
            "Recovery: keep these visual/test artifacts under the approved "
            "artifact prefixes so the integrator can preserve them to the "
            "Vault receipt, or delete them before re-submitting if they are "
            "disposable."
        )
    if dirty_class == ARTIFACT_POLICY_MISSING_CLASS:
        prefixes = ", ".join(_PRESERVABLE_ARTIFACT_PREFIXES)
        return (
            "Recovery: artifact-like files are present outside the approved "
            f"preserve prefixes ({prefixes}). Move them under an approved "
            "prefix, extend the artifact policy in code/tests if this output "
            "type should be preserved, or delete them if disposable."
        )
    return (
        "Recovery: commit intentional source changes on the task branch or "
        "remove accidental leftovers, then re-submit the task."
    )


def _preserve_artifact_files(worktree: Path, task_id: str, paths: Sequence[str]) -> dict:
    """Copy Visual-QA artifacts out of a worktree, then remove only those files.

    The operation is intentionally two-phase: every file is copied first. Only
    after all copies succeed do we unlink the originals, so a partial copy
    failure parks the chain without deleting worker output.
    """
    dirty_paths = list(paths)
    if not dirty_paths:
        return {"destination": None, "file_count": 0, "paths": []}

    dest = _ARTIFACT_RECEIPTS_ROOT / f"{task_id}-{_artifact_receipt_timestamp()}"
    copied: list[tuple[Path, Path]] = []
    try:
        dest.mkdir(parents=True, exist_ok=False)
        for rel in dirty_paths:
            if not _is_preservable_artifact_path(rel):
                raise RuntimeError(f"non-preservable artifact path: {rel}")
            src = worktree / rel
            if not src.is_file():
                raise RuntimeError(f"artifact path is not a file: {rel}")
            target = dest / rel
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(src, target)
            copied.append((src, target))
        for src, _target in copied:
            src.unlink()
        # Remove now-empty artifact directories from deepest to shallowest;
        # ignore non-empty parents so sibling artifacts/sources are untouched.
        for rel in sorted(dirty_paths, key=lambda p: p.count("/"), reverse=True):
            parent = (worktree / rel).parent
            while parent != worktree:
                try:
                    parent.rmdir()
                except OSError:
                    break
                parent = parent.parent
    except Exception:
        raise
    return {"destination": str(dest), "file_count": len(copied), "paths": dirty_paths}


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

    # node_modules/.venv symlinks (untracked, never committed) let worker and
    # validation worktrees share the checkout's installed dependencies.
    _link_shared_dependencies(Path(repo_root), wt)

    return {"path": wt, "branch": branch, "base_branch": base_branch,
            "created": True}


def prepare_worker_base(
    worktree: Path | str,
    *,
    recorded_head: str,
    merge_target: str,
    task_id: str | None = None,
) -> dict[str, str]:
    """Fail closed on reused-worktree drift, then update a clean stale base.

    This runs after claim/provisioning but before the worker process starts.
    ``recorded_head`` is the HEAD captured by the current run before any worker
    edit.  Dirty state or an unexpected HEAD is never rewritten.  A clean
    branch whose target advanced is rebased, with an automatic abort on
    conflict so the dispatcher can block with the original worktree intact.

    When ``task_id`` is provided, known artifact-only dirt (scratch, receipts,
    screenshots, archived artifact dirs) is preserved to the receipts area and
    removed before the dirty check, so artefakt-only leftovers from a crashed
    predecessor do not park the chain.  Real source edits still park.
    """
    wt = Path(worktree)
    actual_head = _git(wt, "rev-parse", "HEAD")
    recorded = str(recorded_head or "").strip()
    if not recorded or actual_head != recorded:
        raise WorktreeError(
            "worktree HEAD does not match recorded pre-run HEAD "
            f"(recorded={recorded or 'missing'}, actual={actual_head})"
        )
    dirty = dirty_files(wt)
    if dirty:
        if task_id is None:
            raise WorktreeError(
                "worktree is dirty before worker edits; refusing automatic base "
                f"update ({', '.join(dirty[:8])})"
            )
        artifact_paths = [
            p for p in dirty
            if _classify_dirty_paths([p]) == PRESERVABLE_ARTIFACTS_CLASS
        ]
        if not artifact_paths:
            raise WorktreeError(
                "worktree is dirty before worker edits; refusing automatic base "
                f"update ({', '.join(dirty[:8])})"
            )
        receipt = _preserve_artifact_files(wt, task_id, artifact_paths)
        remaining = dirty_files(wt)
        if remaining:
            raise WorktreeError(
                "worktree is dirty before worker edits; refusing automatic base "
                f"update ({', '.join(remaining[:8])})"
            )
    target = str(merge_target or "").strip()
    if not target:
        raise WorktreeError("worktree merge target is missing")
    target_head = _git(wt, "rev-parse", target)
    if actual_head == target_head or _branch_is_ancestor(wt, target_head, actual_head):
        return {
            "action": "current",
            "previous_head": actual_head,
            "head": actual_head,
            "merge_target": target,
            "merge_target_head": target_head,
        }
    try:
        _git(wt, "rebase", target)
    except WorktreeError as exc:
        _git(wt, "rebase", "--abort", check=False)
        raise WorktreeError(
            f"clean stale worktree could not rebase onto {target}: {exc}"
        ) from exc
    new_head = _git(wt, "rev-parse", "HEAD")
    if dirty_files(wt):
        raise WorktreeError(
            "worktree became dirty while preparing the worker base"
        )
    return {
        "action": "rebased",
        "previous_head": actual_head,
        "head": new_head,
        "merge_target": target,
        "merge_target_head": target_head,
    }


def prepare_reused_task_worktree(
    conn: sqlite3.Connection,
    task,
    workspace: Path | str,
) -> Optional[dict[str, str]]:
    """Prepare a task retry's already-provisioned worktree before spawn.

    First-time chain provisioning is excluded: a sibling may legitimately
    inherit earlier slice commits and uses separate chain-tip semantics.  A
    task whose recorded workspace already pointed into the provisioned tree is
    a retry/continuation and has an exact pre-run HEAD suitable for this guard.
    """
    prior = split_provisioned_path(task.workspace_path)
    current = split_provisioned_path(workspace)
    if prior is None or current is None:
        return None
    prior_repo, prior_root_id, prior_wt = prior
    repo_root, root_id, wt = current
    if (
        prior_repo.resolve() != repo_root.resolve()
        or prior_root_id != root_id
        or prior_wt.resolve() != wt.resolve()
    ):
        raise WorktreeError(
            "provisioned worktree identity drifted before worker spawn"
        )
    row = conn.execute(
        "SELECT current_run_id FROM tasks WHERE id = ?",
        (task.id,),
    ).fetchone()
    run_id = int(row["current_run_id"]) if row and row["current_run_id"] else None
    if run_id is None:
        raise WorktreeError("current task run is missing before worker base preparation")
    run = conn.execute(
        "SELECT pre_run_commit_sha FROM task_runs WHERE id = ? AND task_id = ?",
        (run_id, task.id),
    ).fetchone()
    recorded_head = str(run["pre_run_commit_sha"] or "").strip() if run else ""
    event = conn.execute(
        "SELECT payload FROM task_events "
        "WHERE task_id = ? AND kind = 'worktree_provisioned' "
        "ORDER BY id ASC LIMIT 1",
        (root_id,),
    ).fetchone()
    merge_target = ""
    if event and event["payload"]:
        try:
            payload = json.loads(event["payload"])
            if isinstance(payload, dict):
                merge_target = str(payload.get("merge_target") or "").strip()
        except (TypeError, ValueError):
            pass
    result = prepare_worker_base(
        wt,
        recorded_head=recorded_head,
        merge_target=merge_target,
        task_id=task.id,
    )
    from hermes_cli import kanban_db as kb

    with kb.write_txn(conn):
        conn.execute(
            "UPDATE task_runs SET pre_run_commit_sha = ? WHERE id = ?",
            (result["head"], run_id),
        )
        kb._append_event(
            conn,
            task.id,
            "worker_base_prepared",
            {**result, "run_id": run_id},
            run_id=run_id,
        )
    return result


def chain_root_id(conn: sqlite3.Connection, task_id: str) -> str:
    """Walk ``task_links`` upward to the chain root (deterministic: smallest
    parent id at each level; cycle-safe)."""
    if _is_decompose_root(conn, task_id):
        return task_id
    decompose_root = _direct_decompose_root_for_child(conn, task_id)
    if decompose_root is not None:
        return decompose_root
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


def _is_decompose_root(conn: sqlite3.Connection, task_id: str) -> bool:
    row = conn.execute(
        "SELECT 1 FROM task_events "
        "WHERE task_id = ? AND kind = 'decomposed' LIMIT 1",
        (task_id,),
    ).fetchone()
    return row is not None


def _direct_decompose_root_for_child(
    conn: sqlite3.Connection,
    task_id: str,
) -> Optional[str]:
    """Return the decomposed root that waits on *task_id*, if any.

    Decompose links point from every subtask to the root
    (``task_links.parent_id = subtask``, ``child_id = root``), the opposite
    direction of ordinary parent→child dependency chains.  Worker-isolation
    still needs all subtasks to share the root's branch/worktree.
    """
    row = conn.execute(
        "SELECT l.child_id FROM task_links l "
        "WHERE l.parent_id = ? "
        "AND EXISTS ("
        "  SELECT 1 FROM task_events e "
        "  WHERE e.task_id = l.child_id AND e.kind = 'decomposed'"
        ") "
        "ORDER BY l.child_id LIMIT 1",
        (task_id,),
    ).fetchone()
    return row["child_id"] if row is not None else None


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
    if _is_decompose_root(conn, root_id):
        rows = conn.execute(
            "SELECT parent_id FROM task_links WHERE child_id = ?",
            (root_id,),
        ).fetchall()
        ids.update(r["parent_id"] for r in rows)
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
        outcome["release_gate_child_id"] = existing
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
    # The completion outbox persists this stable id in its release context so
    # closeout recovery observes this gate instead of starting a second deploy.
    outcome["release_gate_child_id"] = child_id
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
    # Creating the gate is a database-only operation.  The terminal task and its
    # closeout intent have not committed yet, so starting a deploy here would let
    # runtime activation escape a later completion rollback.  The durable
    # closeout worker calls ``start_parked_release_gate`` after commit.
    return child_id


def _spawn_gate_activation_logged(
    conn: sqlite3.Connection,
    child_id: str,
    payload: dict,
    *,
    mode: str,
) -> bool:
    """Launch the detached release-gate activation for *child_id* and record the
    additive event trail (``release_gate_auto_execute_started``, then on a launch
    failure ``release_gate_auto_execute_failed``).

    Returns ``True`` iff the detached unit actually launched (``systemd-run``
    accepted it); ``False`` on a launch failure. The caller keys the
    double-deploy mutual-exclusion flag on this, so a FAILED launch must return
    ``False`` — otherwise it would suppress the ``maybe_auto_release`` fallback
    and drop BOTH deploys.

    Shared by the operator-forced ``release_gate.mode: auto`` path and the AD-S2
    ``release.autonomous`` hook — both use EXACTLY the same detached activation
    (AC: no new deploy path). ``mode`` only labels the started event (``"auto"``
    vs ``"autonomous"``).

    AC-5: the detached transient unit runs the SAME ``execute_release_gate`` core
    the CLI runs (gate + real backend restart via deploy_dashboard.sh) and writes
    the child result itself. Detaching keeps the integration process from
    blocking on the deploy AND makes the activation immune to the restart it
    triggers (self-termination trap). Reverse-map the integration connection's
    DB to its board slug and also forward explicit Kanban path overrides so both
    named boards and custom/sandbox DBs resolve the same child."""
    from hermes_cli import kanban_db as kb

    try:
        with kb.write_txn(conn):
            kb._append_event(
                conn,
                child_id,
                "release_gate_auto_execute_started",
                {"mode": mode, **payload},
            )
        board = kb.board_slug_for_conn(conn)
        spawn = spawn_release_gate_activation(child_id, board=board)
        if not spawn.get("ok"):
            with kb.write_txn(conn):
                kb._append_event(
                    conn,
                    child_id,
                    "release_gate_auto_execute_failed",
                    {"error": spawn.get("detail"), **payload},
                )
            return False
        return True
    except Exception as exc:
        with kb.write_txn(conn):
            kb._append_event(
                conn,
                child_id,
                "release_gate_auto_execute_failed",
                {"error": str(exc), **payload},
            )
        return False


def maybe_auto_execute_gate(
    conn: sqlite3.Connection,
    child_id: str,
    *,
    source_task_id: str,
    root_id: str,
    payload: dict,
) -> bool:
    """AD-S2 hook, run at the point a ``release_gate_parked`` child is created.

    When the global ``release.autonomous`` kill-switch is ON **and** every
    auto_release guard passes (chain tier ceiling, root ``freigabe: complete``,
    ``effective_ui_impact`` != redesign, ``pause_on_red_streak``), the just-parked
    gate is auto-executed via the SAME detached activation the operator
    ``mode: auto`` path and the CLI/endpoint use — no new deploy path. Otherwise
    the child stays parked; a held decision is recorded as an additive
    ``release_gate_auto_execute_held`` event for the dashboard/audit trail,
    EXCEPT the kill-switch-off default which stays silent so
    ``release.autonomous: false`` is byte-exact today's behaviour (no new event).
    Fail-soft: a guard-evaluation error parks (never breaks integration).

    Returns True iff the detached activation was successfully launched."""
    from hermes_cli import auto_release
    from hermes_cli import kanban_db as kb

    try:
        # The source task is part of the chain but is linked as the release-gate
        # child's parent; union it in so its ui_impact/tier is always considered
        # even if the pure root→child BFS would not reach it.
        chain_ids = set(_chain_member_ids(conn, root_id)) | {root_id, source_task_id}
        decision = auto_release.evaluate_ad_hoc_release_guards(
            conn, root_id=root_id, chain_ids=chain_ids,
        )
    except Exception:
        _log.warning(
            "release-gate auto-exec guard evaluation failed for %s; parking",
            child_id, exc_info=True,
        )
        return False
    if decision.get("outcome") != "auto_execute":
        # held_kill_switch is the silent default: no event keeps
        # release.autonomous:false byte-identical to pre-AD-S2 behaviour.
        if decision.get("outcome") != "held_kill_switch":
            try:
                with kb.write_txn(conn):
                    kb._append_event(
                        conn,
                        child_id,
                        "release_gate_auto_execute_held",
                        {**decision, **payload},
                    )
            except Exception:
                _log.debug(
                    "could not record release_gate_auto_execute_held",
                    exc_info=True,
                )
        return False
    return _spawn_gate_activation_logged(conn, child_id, payload, mode="autonomous")


def start_parked_release_gate(
    conn: sqlite3.Connection,
    child_id: str,
) -> str:
    """Start one parked gate after its source completion committed.

    Returns ``started``, ``held``, or ``ambiguous``.  A failed
    ``systemd-run`` acknowledgement is ambiguous because the unit may have been
    accepted before the caller timed out; callers must never fall back to a
    second release path when a release-gate child exists.
    """

    row = conn.execute(
        "SELECT payload FROM task_events WHERE task_id = ? "
        "AND kind = 'release_gate_parked' ORDER BY id DESC LIMIT 1",
        (child_id,),
    ).fetchone()
    if row is None:
        return "held"
    try:
        payload = json.loads(row["payload"] or "{}")
    except (TypeError, ValueError, json.JSONDecodeError):
        payload = {}
    if not isinstance(payload, dict):
        payload = {}
    source_task_id = str(payload.get("source_task") or "").strip()
    root_id = str(payload.get("root_id") or source_task_id).strip()
    if not source_task_id or not root_id:
        return "held"

    if release_gate_mode() == "auto":
        started = _spawn_gate_activation_logged(
            conn, child_id, payload, mode="auto",
        )
    else:
        started = maybe_auto_execute_gate(
            conn,
            child_id,
            source_task_id=source_task_id,
            root_id=root_id,
            payload=payload,
        )
    if started:
        return "started"
    failed = conn.execute(
        "SELECT 1 FROM task_events WHERE task_id = ? "
        "AND kind = 'release_gate_auto_execute_failed' LIMIT 1",
        (child_id,),
    ).fetchone()
    return "ambiguous" if failed is not None else "held"


# ---------------------------------------------------------------------------
# Release-gate executor (R2 / P2-release-executor)
#
# The parked release-gate child above documents the activation commands but
# nothing runs them. This executor processes such a child end to end: it runs
# the gate at the recorded commit in a clean detached validation worktree, and
# on green reports success to the board. On RED it spawns a BOUNDED fixer on
# the ``premium`` lane EXCLUSIVELY inside the chain worktree/branch — the
# fixer reads the gate error, fixes, and the gate is re-run. After the retry
# budget (``kanban.release_gate_fixer_max_retries``, default 2) it escalates to
# the operator. Hard boundary: the fixer never edits live-main; validation build
# output stays in the detached worktree. The event trail is purely additive
# (``release_gate_executed`` / ``release_gate_fix_attempt``).
# ---------------------------------------------------------------------------

# Persisted release-gate commands use the canonical checkout path; the runner
# rebinds that prefix to the clean validation worktree. The fixer's chain
# worktree remains under the same repo's ``.worktrees/kanban/`` namespace.
LIVE_CHECKOUT_ROOT = Path("/home/piet/.hermes/hermes-agent")
# npm build + loopback smoke can be slow; keep generous so a slow-but-green
# build is not misreported as red.
RELEASE_GATE_COMMAND_TIMEOUT = 1800
RELEASE_GATE_FIXER_TIMEOUT = 1800

# Runtime activation (S1): a GREEN release gate must ACTIVATE the merged code, not
# merely prove it builds — build + backend restart + post-restart health, run
# through the canonical ``scripts/deploy_dashboard.sh`` (which also lays down the
# pre-deploy rollback anchor tag and polls ``/api/status`` after the restart, so a
# 200 + valid JSON proves the *Python* backend, not just the static SPA, is live).
# The activation is launched as a DETACHED transient unit (see
# :func:`spawn_release_gate_activation`) so the restart it triggers cannot kill the
# process that writes the child's terminal result — the self-termination trap.
DEPLOY_SCRIPT = LIVE_CHECKOUT_ROOT / "scripts" / "deploy_dashboard.sh"
DASHBOARD_SERVICE = "hermes-dashboard.service"
# lint + build + restart + health poll — generous, mirrors the gate build timeout.
RELEASE_GATE_ACTIVATION_TIMEOUT = 1800
# Time budget for the ``systemd-run`` launcher itself (it starts the transient
# unit and returns immediately; this only bounds the launch, not the activation).
RELEASE_GATE_SPAWN_TIMEOUT = 30


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


def release_gate_mode() -> str:
    """Configured release-gate execution mode: ``manual`` (default) or ``auto``.

    Reads the root Hermes config directly for the same profile-isolation reason
    as :func:`release_gate_fixer_max_retries`: release activation targets the
    live checkout and should not vary by worker profile.
    """
    try:
        import yaml
        from hermes_constants import get_default_hermes_root

        cfg_path = get_default_hermes_root() / "config.yaml"
        if cfg_path.is_file():
            with open(cfg_path, "r", encoding="utf-8") as fh:
                root_cfg = yaml.safe_load(fh) or {}
            mode = (((root_cfg.get("kanban") or {}).get("release_gate") or {}).get("mode") or "manual")
            mode = str(mode).strip().lower()
            if mode in {"manual", "auto"}:
                return mode
    except Exception:
        pass
    return "manual"


_VISUAL_GATE_IME_NOTE = "mobile-IME physically unverified"
_VISUAL_GATE_SCREENSHOTS_ROOT = Path("/tmp/hermes-visual-gate")
_VISUAL_GATE_URL = "http://127.0.0.1:9119/control"


class _VisualGateStaticServer:
    """Ephemeral loopback-only static server for the freshly built dashboard."""

    def __init__(self, web_dist: Path) -> None:
        self.web_dist = Path(web_dist)
        self._server = None
        self._thread: Optional[threading.Thread] = None

    def start(self) -> str:
        index = self.web_dist / "index.html"
        if not index.is_file():
            raise RuntimeError(f"web_dist missing index.html: {index}")

        from fastapi import FastAPI
        from fastapi.responses import FileResponse
        from fastapi.staticfiles import StaticFiles
        import uvicorn

        app = FastAPI(docs_url=None, redoc_url=None, openapi_url=None)
        assets = self.web_dist / "assets"
        if assets.is_dir():
            app.mount("/assets", StaticFiles(directory=assets), name="assets")

        @app.get("/{full_path:path}", include_in_schema=False)
        async def _serve_spa(full_path: str = "") -> FileResponse:
            requested = (self.web_dist / full_path).resolve()
            root = self.web_dist.resolve()
            if requested.is_file() and requested.is_relative_to(root):
                return FileResponse(requested)
            return FileResponse(index)

        config = uvicorn.Config(
            app,
            host="127.0.0.1",
            port=0,
            log_level="warning",
            access_log=False,
            lifespan="off",
        )
        server = uvicorn.Server(config)
        server.install_signal_handlers = lambda: None  # type: ignore[method-assign]
        self._server = server
        self._thread = threading.Thread(
            target=server.run,
            name="hermes-visual-gate-static-server",
            daemon=True,
        )
        self._thread.start()

        deadline = time.monotonic() + 10
        port: Optional[int] = None
        while time.monotonic() < deadline:
            servers = getattr(server, "servers", None) or []
            if servers:
                sockets = getattr(servers[0], "sockets", None) or []
                if sockets:
                    port = int(sockets[0].getsockname()[1])
                    break
            if self._thread and not self._thread.is_alive():
                break
            time.sleep(0.05)
        if port is None:
            self.stop()
            raise RuntimeError("uvicorn did not expose a loopback port within 10s")
        return f"http://127.0.0.1:{port}/control"

    def stop(self) -> None:
        server = self._server
        if server is not None:
            server.should_exit = True
        thread = self._thread
        if thread is not None and thread.is_alive():
            thread.join(timeout=5)


def visual_gate_enabled() -> bool:
    """Resolve ``kanban.visual_gate`` from root config, default off.

    ``HERMES_KANBAN_VISUAL_GATE`` wins for tests/operator one-offs. Default
    is deliberately ``False`` so existing worker gates keep today's behavior
    unless the operator opts in.
    """
    env = (os.environ.get("HERMES_KANBAN_VISUAL_GATE") or "").strip().lower()
    if env:
        return env in {"1", "true", "yes", "on"}
    try:
        import yaml
        from hermes_constants import get_default_hermes_root

        cfg_path = get_default_hermes_root() / "config.yaml"
        if cfg_path.is_file():
            with open(cfg_path, "r", encoding="utf-8") as fh:
                root_cfg = yaml.safe_load(fh) or {}
            value = (root_cfg.get("kanban") or {}).get("visual_gate")
            if isinstance(value, bool):
                return value
            if isinstance(value, str):
                normalized = value.strip().lower()
                if normalized:
                    return normalized in {"1", "true", "yes", "on"}
    except Exception:
        pass
    return False


def visual_gate_max_retries() -> int:
    """Bounded visual-gate fixer retry budget.

    ``HERMES_KANBAN_VISUAL_GATE_MAX_RETRIES`` wins over config
    ``kanban.visual_gate_max_retries``. Default 3, clamped to 0..5.
    """
    env = (os.environ.get("HERMES_KANBAN_VISUAL_GATE_MAX_RETRIES") or "").strip()
    if env:
        try:
            return min(5, max(0, int(env)))
        except ValueError:
            pass
    try:
        import yaml
        from hermes_constants import get_default_hermes_root

        cfg_path = get_default_hermes_root() / "config.yaml"
        if cfg_path.is_file():
            with open(cfg_path, "r", encoding="utf-8") as fh:
                root_cfg = yaml.safe_load(fh) or {}
            value = (root_cfg.get("kanban") or {}).get("visual_gate_max_retries")
            if isinstance(value, bool):
                value = None
            if isinstance(value, int):
                return min(5, max(0, value))
            if isinstance(value, str):
                stripped = value.strip()
                if stripped.lstrip("-").isdigit():
                    return min(5, max(0, int(stripped)))
    except Exception:
        pass
    return 3


# ---------------------------------------------------------------------------
# Visual gate (non-MCP dashboard screenshots + Playwright mobile check)
# ---------------------------------------------------------------------------

def _visual_gate_with_ime_note(detail: str) -> str:
    text = detail or "visual-gate failed"
    if _VISUAL_GATE_IME_NOTE in text:
        return text
    return f"{text}\nnotes: {_VISUAL_GATE_IME_NOTE}"


def _visual_gate_error(message: str, screenshot_paths: Sequence[Path]) -> str:
    paths = "\n".join(f"- {path}" for path in screenshot_paths)
    detail = f"visual-gate: {message}"
    if paths:
        detail = f"{detail}\nscreenshots:\n{paths}"
    return _visual_gate_with_ime_note(detail)


def _visual_gate_ensure_web_dist(
    repo_root: Path, screenshot_paths: list[Path],
) -> Optional[str]:
    """Build ``hermes_cli/web_dist`` if missing; return error or None."""
    web_dist = repo_root / "hermes_cli" / "web_dist"
    web_dist_index = web_dist / "index.html"
    if web_dist_index.is_file():
        return None
    try:
        build = subprocess.run(  # noqa: S603 -- fixed argv
            ["npm", "run", "build"],
            cwd=str(repo_root / "web"),
            capture_output=True,
            text=True,
            timeout=RELEASE_GATE_COMMAND_TIMEOUT,
        )
    except subprocess.TimeoutExpired:
        return _visual_gate_error(
            "frontend build for missing web_dist timed out after "
            f"{RELEASE_GATE_COMMAND_TIMEOUT}s",
            screenshot_paths,
        )
    except FileNotFoundError:
        return _visual_gate_error(
            "frontend build for missing web_dist failed: npm not found",
            screenshot_paths,
        )
    except (OSError, subprocess.SubprocessError) as exc:
        return _visual_gate_error(
            f"frontend build for missing web_dist failed: {exc}",
            screenshot_paths,
        )
    if build.returncode != 0:
        tail = ((build.stdout or "") + "\n" + (build.stderr or "")).strip()[-2000:]
        return _visual_gate_error(
            "frontend build for missing web_dist failed with exit "
            f"{build.returncode}\n{tail}",
            screenshot_paths,
        )
    if not web_dist_index.is_file():
        return _visual_gate_error(
            "frontend build completed but web_dist is still missing index.html: "
            f"{web_dist_index}",
            screenshot_paths,
        )
    return None


def _visual_gate_healthcheck(
    repo_root: Path, visual_gate_url: str, screenshot_paths: list[Path],
) -> Optional[str]:
    """curl the throwaway static server; return error or None."""
    try:
        health = subprocess.run(  # noqa: S603 -- fixed argv
            ["curl", "-fsS", visual_gate_url],
            cwd=str(repo_root),
            capture_output=True,
            text=True,
            timeout=10,
        )
    except subprocess.TimeoutExpired:
        return _visual_gate_error(
            f"dashboard unreachable: curl timed out for {visual_gate_url}",
            screenshot_paths,
        )
    except FileNotFoundError:
        return _visual_gate_error("dashboard unreachable: curl not found", screenshot_paths)
    except (OSError, subprocess.SubprocessError) as exc:
        return _visual_gate_error(f"dashboard unreachable: {exc}", screenshot_paths)
    if health.returncode != 0:
        tail = (health.stdout + "\n" + health.stderr).strip()[-1000:]
        return _visual_gate_error(
            f"dashboard unreachable: curl exit {health.returncode}\n{tail}",
            screenshot_paths,
        )
    return None


def _visual_gate_chromium_shots(
    repo_root: Path,
    visual_gate_url: str,
    desktop_path: Path,
    mobile_path: Path,
    screenshot_paths: list[Path],
) -> Optional[str]:
    """Capture desktop + mobile chromium screenshots; return error or None."""
    shots = (
        ("desktop", "1280,800", desktop_path),
        ("mobile", "390,844", mobile_path),
    )
    for label, size, path in shots:
        try:
            shot = subprocess.run(  # noqa: S603 -- fixed argv
                [
                    _resolve_chromium_shot(),
                    f"--screenshot={path}",
                    f"--window-size={size}",
                    "--virtual-time-budget=12000",
                    visual_gate_url,
                ],
                cwd=str(repo_root),
                capture_output=True,
                text=True,
                timeout=120,
            )
        except subprocess.TimeoutExpired:
            return _visual_gate_error(
                f"{label} chromium-shot timed out after 120s",
                screenshot_paths,
            )
        except FileNotFoundError:
            return _visual_gate_error("chromium-shot not found", screenshot_paths)
        except (OSError, subprocess.SubprocessError) as exc:
            return _visual_gate_error(
                f"{label} chromium-shot failed: {exc}", screenshot_paths,
            )
        if shot.returncode != 0:
            tail = (shot.stdout + "\n" + shot.stderr).strip()[-2000:]
            return _visual_gate_error(
                f"{label} chromium-shot exit {shot.returncode}\n{tail}",
                screenshot_paths,
            )
    return None


def _visual_gate_playwright_check(
    repo_root: Path,
    visual_gate_url: str,
    scripted_mobile_path: Path,
    screenshot_paths: list[Path],
) -> Optional[str]:
    """Run the mobile Playwright visual check; return error or None."""
    env = dict(os.environ)
    env["HERMES_VISUAL_GATE_SCREENSHOT"] = str(scripted_mobile_path)
    env["HERMES_VISUAL_GATE_URL"] = visual_gate_url
    try:
        node = subprocess.run(  # noqa: S603 -- fixed argv
            ["node", "scripts/visual_check_mobile.mjs"],
            cwd=str(repo_root),
            capture_output=True,
            text=True,
            timeout=120,
            env=env,
        )
    except subprocess.TimeoutExpired:
        return _visual_gate_error(
            "mobile Playwright check timed out after 120s", screenshot_paths,
        )
    except FileNotFoundError:
        return _visual_gate_error("node not found", screenshot_paths)
    except (OSError, subprocess.SubprocessError) as exc:
        return _visual_gate_error(f"mobile Playwright check failed: {exc}", screenshot_paths)

    stdout = (node.stdout or "").strip()
    try:
        payload = json.loads(stdout) if stdout else {}
    except json.JSONDecodeError:
        tail = ((node.stdout or "") + "\n" + (node.stderr or "")).strip()[-2000:]
        return _visual_gate_error(
            f"mobile Playwright check returned non-JSON output\n{tail}",
            screenshot_paths,
        )
    if payload.get("screenshotPath"):
        try:
            scripted_path = Path(str(payload["screenshotPath"]))
            if scripted_path not in screenshot_paths:
                screenshot_paths.append(scripted_path)
        except TypeError:
            pass
    if node.returncode != 0 or not payload.get("ok"):
        tail = json.dumps(payload, ensure_ascii=False, sort_keys=True)[-2000:]
        stderr = (node.stderr or "").strip()[-1000:]
        if stderr:
            tail = f"{tail}\nstderr:\n{stderr}"
        return _visual_gate_error(
            f"mobile Playwright check failed\n{tail}",
            screenshot_paths,
        )
    return None


def _run_visual_gate(repo_root: Path, screenshots_dir: Path) -> Optional[str]:
    """Run the non-MCP dashboard visual gate.

    Returns ``None`` on pass, otherwise an error tail beginning with
    ``visual-gate:`` and including screenshot paths. The helper serves the
    freshly built ``hermes_cli/web_dist`` through a throwaway auth-free
    loopback uvicorn instance and always tears it down before returning.
    """
    repo_root = Path(repo_root)
    run_id = (
        datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S%fZ")
        + f"-{os.getpid()}"
    )
    run_dir = Path(screenshots_dir) / run_id
    run_dir.mkdir(parents=True, exist_ok=True)
    desktop_path = run_dir / "desktop-1280x800.png"
    mobile_path = run_dir / "mobile-390x844.png"
    scripted_mobile_path = run_dir / "mobile-playwright.png"
    screenshot_paths = [desktop_path, mobile_path, scripted_mobile_path]

    err = _visual_gate_ensure_web_dist(repo_root, screenshot_paths)
    if err:
        return err

    web_dist = repo_root / "hermes_cli" / "web_dist"
    server = _VisualGateStaticServer(web_dist)
    try:
        try:
            visual_gate_url = server.start()
        except Exception as exc:
            return _visual_gate_error(
                f"dashboard static server failed: {exc}", screenshot_paths
            )

        err = _visual_gate_healthcheck(
            repo_root, visual_gate_url, screenshot_paths,
        )
        if err:
            return err

        err = _visual_gate_chromium_shots(
            repo_root, visual_gate_url, desktop_path, mobile_path,
            screenshot_paths,
        )
        if err:
            return err

        return _visual_gate_playwright_check(
            repo_root, visual_gate_url, scripted_mobile_path, screenshot_paths,
        )
    finally:
        server.stop()


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


def _self_heal_release_toolchain(root: Path) -> Optional[str]:
    """Install a private, lockfile-bound toolchain in a validation worktree."""
    for rel in ("node_modules", "web/node_modules"):
        private_modules = root / rel
        try:
            if private_modules.is_symlink():
                private_modules.unlink()
            private_modules.mkdir(parents=True, exist_ok=True)
        except OSError as exc:
            return (
                "release-toolchain: could not create private "
                f"{rel}: {exc}"
            )

    npm_bin = shutil.which("npm") or "npm"
    try:
        proc = subprocess.run(  # noqa: S603 -- fixed argv
            [npm_bin, "ci"],
            cwd=str(root),
            capture_output=True,
            text=True,
            timeout=RELEASE_GATE_COMMAND_TIMEOUT,
        )
    except subprocess.TimeoutExpired:
        return (
            "release-toolchain: npm ci timed out after "
            f"{RELEASE_GATE_COMMAND_TIMEOUT}s"
        )
    except (OSError, subprocess.SubprocessError) as exc:
        return f"release-toolchain: npm ci command error: {exc}"
    if proc.returncode != 0:
        tail = ((proc.stdout or "") + (proc.stderr or "")).strip()[-2000:]
        return f"release-toolchain: npm ci exit {proc.returncode}\n{tail}"
    return None


def _default_release_gate_runner(
    commands: Optional[Sequence[str]] = None,
    *,
    repo_root: Optional[Path] = None,
) -> tuple[bool, str]:
    """Run the release-gate commands in *repo_root* and return output.

    Production callers pass a detached validation worktree. The commands are a
    fixed, code-defined tuple (web build + artifact check), joined
    with ``&&`` so the leading ``cd <web>`` carries to ``npm run build`` —
    no untrusted input reaches the shell. Historical absolute live-checkout
    paths in the persisted command tuple are rebound to the validation root.
    Runtime health belongs to the later activation, which deploys this commit and
    checks ``/api/status``; probing the old live process here would not validate
    the detached commit.
    """
    root = Path(repo_root or LIVE_CHECKOUT_ROOT)
    if (root / "web" / "package.json").is_file() and _resolve_node_bin(
        root, "tsc"
    ) is None:
        if root.resolve() == LIVE_CHECKOUT_ROOT.resolve():
            return False, (
                "release-toolchain: refusing npm ci in the live checkout"
            )
        heal_error = _self_heal_release_toolchain(root)
        if heal_error:
            return False, heal_error
    cmds = list(commands or _RELEASE_GATE_COMMANDS)
    quoted_root = shlex.quote(str(root))
    cmds = [cmd.replace(str(LIVE_CHECKOUT_ROOT), quoted_root) for cmd in cmds]
    script = " && ".join(cmds)
    cwd = str(root) if root.is_dir() else None
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
    if ok and visual_gate_enabled():
        err = _run_visual_gate(root, _VISUAL_GATE_SCREENSHOTS_ROOT)
        if err:
            return False, err
        tail = "\n".join(part for part in (tail, _VISUAL_GATE_IME_NOTE) if part)
    return ok, tail


def _dashboard_service_pid() -> Optional[int]:
    """Current ``MainPID`` of the durable dashboard systemd unit, or ``None``.

    The pre/post activation PIDs are the AC-1 evidence that a genuine backend
    restart happened: a ``restart`` always forks a fresh process, so a
    changed, non-zero MainPID proves fresh Python is live (a bare rebuild would
    leave the PID untouched). Best-effort — any failure returns ``None`` and the
    caller falls back to the deploy script's own health verdict."""
    ctl = os.environ.get("HERMES_SYSTEMCTL_BIN") or "systemctl"
    try:
        proc = subprocess.run(  # noqa: S603 -- fixed argv
            [ctl, "--user", "show", "-p", "MainPID", "--value", DASHBOARD_SERVICE],
            capture_output=True,
            text=True,
            timeout=15,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    raw = (proc.stdout or "").strip()
    try:
        pid = int(raw)
    except ValueError:
        return None
    return pid or None


def _live_checkout_head() -> Optional[str]:
    """Return the exact commit currently checked out for runtime activation."""
    try:
        sha = _git(LIVE_CHECKOUT_ROOT, "rev-parse", "HEAD").strip().lower()
    except (WorktreeError, OSError, subprocess.SubprocessError):
        return None
    return sha if re.fullmatch(r"[0-9a-f]{40}", sha) else None


def _default_release_gate_activation() -> tuple[bool, str, dict]:
    """Perform the REAL runtime activation and return ``(ok, output_tail, meta)``.

    Captures the dashboard service PID, runs ``deploy_dashboard.sh`` (lint +
    build + pre-deploy anchor tag + ``systemctl --user restart`` + post-restart
    ``/api/status`` health), then captures the new PID.

    ``ok`` requires the deploy script to exit 0 (its own post-restart health
    gate) AND a present PID afterwards; when both pre- and post-PIDs are known it
    also requires them to differ (a genuinely new backend process — the restart
    took, AC-1/AC-2). ``meta`` carries the pre/post PIDs and the deploy exit code
    for the event trail. This is the injectable seam tests replace so no real
    restart runs under pytest."""
    pre_pid = _dashboard_service_pid()
    deployed_sha = _live_checkout_head()
    if deployed_sha is None:
        return (
            False,
            "activation: live checkout has no exact deployment commit",
            {"pre_pid": pre_pid, "deployed_sha": None, "running_sha": None},
        )
    if not DEPLOY_SCRIPT.is_file():
        return (
            False,
            f"activation: deploy script missing: {DEPLOY_SCRIPT}",
            {"pre_pid": pre_pid},
        )
    try:
        proc = subprocess.run(  # noqa: S603 -- fixed argv, code-defined path
            ["bash", str(DEPLOY_SCRIPT)],
            cwd=str(LIVE_CHECKOUT_ROOT),
            capture_output=True,
            text=True,
            timeout=RELEASE_GATE_ACTIVATION_TIMEOUT,
        )
    except subprocess.TimeoutExpired:
        return (
            False,
            f"activation: deploy timed out after {RELEASE_GATE_ACTIVATION_TIMEOUT}s",
            {"pre_pid": pre_pid},
        )
    except (OSError, subprocess.SubprocessError) as exc:
        return False, f"activation: deploy command error: {exc}", {"pre_pid": pre_pid}
    post_pid = _dashboard_service_pid()
    running_sha = _live_checkout_head()
    tail = ((proc.stdout or "") + (proc.stderr or ""))[-4000:]
    meta = {
        "pre_pid": pre_pid,
        "post_pid": post_pid,
        "deploy_exit": proc.returncode,
        "deployed_sha": deployed_sha,
        "running_sha": running_sha,
    }
    if proc.returncode != 0:
        return False, f"activation: deploy_dashboard.sh exit {proc.returncode}\n{tail}", meta
    if post_pid is None:
        return (
            False,
            f"activation: dashboard not running after restart (no MainPID)\n{tail}",
            meta,
        )
    if pre_pid is not None and post_pid == pre_pid:
        return (
            False,
            f"activation: dashboard PID unchanged ({post_pid}) — restart did not take\n{tail}",
            meta,
        )
    if running_sha is None or deployed_sha != running_sha:
        return (
            False,
            "activation: deployed and running commits do not match "
            f"({deployed_sha or 'unknown'} != {running_sha or 'unknown'})\n{tail}",
            meta,
        )
    return True, tail, meta


def _release_gate_fixer_prompt(
    *, gate_error: str, attempt: int, task_id: str, root_id: str,
) -> str:
    commands = "\n".join(_RELEASE_GATE_COMMANDS)
    visual_guidance = ""
    if (gate_error or "").startswith("visual-gate:"):
        screenshot_lines = "\n".join(
            line for line in (gate_error or "").splitlines()
            if ".png" in line or "screenshot" in line.lower()
        )
        visual_guidance = (
            "\n\nVisual-gate context:\n"
            "The failing artifacts are these screenshot paths from the gate "
            f"output:\n{screenshot_lines or '(none parsed)'}\n"
            "Fix mobile CSS/layout overflow first: the scripted criterion is "
            "`document.documentElement.scrollWidth <= window.innerWidth` after "
            "focus in a 390x844 touch viewport, with zero browser console "
            "errors. Treat this as a CSS/mobile rendering failure unless the "
            "captured console errors prove a runtime cause."
        )
    return (
        "You are a bounded Hermes release-gate fixer running headless on the "
        "premium lane. The dashboard release gate for chain "
        f"`{root_id}` is RED (fixer attempt {attempt}).\n\n"
        "Gate commands (run in the live checkout — do NOT edit there):\n"
        f"{commands}\n\n"
        "Most recent gate output:\n"
        f"{(gate_error or '')[-3000:]}"
        f"{visual_guidance}\n\n"
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
    """Spawn the premium fixer process, blocking until it finishes, with
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
        # Worker MCP isolation (idle-hang fix, disposition-di_109b5a17-S1): never
        # load external MCP servers (vault qmd, @playwright/mcp headless chromium,
        # claude.ai connectors). Their child processes keep the Node event loop
        # alive so ``claude -p`` cannot exit after its turn (the post-commit
        # ``ep_poll`` idle hang). The fixer drives its lifecycle via Bash →
        # ``hermes`` and edits via built-in tools, so MCP is pure dead weight.
        "--strict-mcp-config",
    ]
    try:
        from hermes_cli import kanban_db as kb

        lane = kb._active_lane_entry_for_profile("premium")
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
    premium fixer inside it. The chain worktree is removed after the
    merge that created the release-gate child, so ``ensure_worktree`` recreates
    it (idempotent) on the chain branch. The live checkout is never modified
    here; its commit reaches the live checkout only through ``integrate_chain``
    after clean branch validation."""
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
    phase: Optional[str] = None,
    validation_commit: Optional[str] = None,
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
    if phase:
        payload["phase"] = phase
    if validation_commit:
        payload["validation_commit"] = validation_commit
    try:
        with kb.write_txn(conn):
            kb._append_event(conn, task_id, "release_gate_executed", payload)
    except Exception:
        _log.warning("could not record release_gate_executed for %s",
                     task_id, exc_info=True)


def _invoke_release_gate_runner(runner, validation_root: Path) -> tuple[bool, str]:
    """Call an injected runner while preserving the historical zero-arg seam.

    New path-aware tests/runners may accept the clean validation root as their
    sole positional argument; existing ``lambda: (...)`` runners remain valid.
    """
    try:
        signature = inspect.signature(runner)
        accepts_root = any(
            parameter.kind in (
                inspect.Parameter.POSITIONAL_ONLY,
                inspect.Parameter.POSITIONAL_OR_KEYWORD,
                inspect.Parameter.VAR_POSITIONAL,
            )
            for parameter in signature.parameters.values()
        )
    except (TypeError, ValueError):
        accepts_root = False
    return runner(validation_root) if accepts_root else runner()


def _run_release_gate_at_commit(
    repo_root: Path,
    commit: Optional[str],
    gate: Callable[[Path, list[str]], tuple[bool, str]],
    *,
    allow_injected_legacy_fallback: bool,
) -> tuple[bool, str]:
    """Validate one release candidate commit in a clean detached worktree.

    The fallback exists only for old injected unit-test seams whose synthetic
    release event contains no real git commit. Production/default execution is
    fail-closed when the event commit cannot be resolved.
    """
    if commit:
        try:
            return _run_gate_in_validation_worktree(repo_root, commit, [], gate)
        except (WorktreeError, OSError, subprocess.SubprocessError) as exc:
            error = f"release validation commit unavailable ({commit}): {exc}"
    else:
        error = "release validation commit missing"
    if allow_injected_legacy_fallback:
        try:
            return gate(repo_root, [])
        except Exception as exc:
            return False, f"injected release gate crashed: {exc}"
    return False, error


def _record_release_gate_fix_attempt(
    conn: sqlite3.Connection, task_id: str, *,
    attempt: int, gate_error: str, root_id: str, worktree: str, branch: str,
) -> None:
    from hermes_cli import kanban_db as kb

    payload = {
        "attempt": attempt,
        "root_id": root_id,
        "lane": "premium",
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


def _record_release_gate_activation(
    conn: sqlite3.Connection, task_id: str, *,
    ok: bool, output: str, meta: Optional[dict], root_id: str,
) -> None:
    """Append the runtime-activation outcome (build + backend restart + health)
    to the child's event trail. ``release_gate_activated`` on success carries the
    pre/post dashboard PIDs (AC-1 evidence); ``release_gate_activation_failed``
    on a failed deploy/health carries the same PIDs plus the error tail."""
    from hermes_cli import kanban_db as kb

    meta = meta or {}
    payload = {
        "ok": bool(ok),
        "root_id": root_id,
        "pre_pid": meta.get("pre_pid"),
        "post_pid": meta.get("post_pid"),
        "deploy_exit": meta.get("deploy_exit"),
        "deployed_sha": meta.get("deployed_sha"),
        "running_sha": meta.get("running_sha"),
        "output_tail": (output or "")[-2000:],
    }
    kind = "release_gate_activated" if ok else "release_gate_activation_failed"
    try:
        with kb.write_txn(conn):
            kb._append_event(conn, task_id, kind, payload)
            deployed_sha = str(meta.get("deployed_sha") or "").strip().lower()
            running_sha = str(meta.get("running_sha") or "").strip().lower()
            exact_runtime = (
                bool(ok)
                and re.fullmatch(r"[0-9a-f]{40}", deployed_sha) is not None
                and deployed_sha == running_sha
            )
            if exact_runtime:
                existing = conn.execute(
                    "SELECT 1 FROM task_events WHERE task_id = ? "
                    "AND kind = 'deployment_verified' "
                    "AND json_extract(payload, '$.deployed_sha') = ? "
                    "AND json_extract(payload, '$.running_sha') = ? LIMIT 1",
                    (root_id, deployed_sha, running_sha),
                ).fetchone()
                if existing is None:
                    kb._append_event(
                        conn,
                        root_id,
                        "deployment_verified",
                        {
                            "deployed_sha": deployed_sha,
                            "running_sha": running_sha,
                            "release_gate_task_id": task_id,
                            "source": "release_gate_activation",
                        },
                    )
    except Exception:
        _log.warning("could not record %s for %s", kind, task_id, exc_info=True)


def _finish_release_gate_green(
    conn: sqlite3.Connection, task_id: str, root_id: str, fixer_attempts: int,
    *, activation: Optional[dict] = None,
) -> None:
    from hermes_cli import kanban_db as kb

    if activation is not None:
        pre = activation.get("pre_pid")
        post = activation.get("post_pid")
        activation_line = (
            "Runtime activation succeeded: backend restarted via "
            f"deploy_dashboard.sh (:9119 PID {pre} → {post}), post-restart health "
            "passed."
        )
    else:
        activation_line = (
            "Dashboard build + artifact check + loopback smoke passed in the "
            "live checkout."
        )
    note = (
        f"✅ Release-gate green for chain `{root_id}` after {fixer_attempts} "
        f"bounded fixer attempt(s). {activation_line}"
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


# ESCALATION-RELEASE-GATE-ERROR-CONTEXT-S1: sentinels the release-gate runner
# emits when it could not COMPLETE a clean gate run (the gate never produced a
# pass/fail verdict — an operational fault, not a candidate defect). Matched as a
# prefix on our own code-defined output strings (``_default_release_gate_runner``
# timeout / OSError branches), so parsing them is robust, not free-text guessing.
_RELEASE_GATE_INFRA_SENTINELS = (
    "release-gate timed out",
    "release-gate command error",
    # Runtime-activation operational faults (S1): a deploy that could not even
    # run to a verdict (launcher/subprocess error, wall-clock timeout, missing
    # script) is infrastructure → transient. A deploy that RAN and failed its
    # health gate ("activation: deploy_dashboard.sh exit N" / "dashboard not
    # running" / "PID unchanged") is a genuine blocking defect and stays
    # ``release_gate_red`` (real-bug).
    "activation: deploy timed out",
    "activation: deploy command error",
    "activation: deploy script missing",
)


def _release_gate_trigger_outcome(last_error: str) -> str:
    """Structural failure-MODE label for a persistent-red release-gate escalation.

    The gate output text (build log, ``visual-gate: …`` message, subprocess
    sentinel, or empty) rarely contains one of the Heiler free-text signals, so a
    release-gate escalation used to fall through to ``unclassified`` and starve
    the Stratege's ``by_class`` input. This derives a structural ``trigger_outcome``
    from the failure mode instead of the exact wording:

    * a gate the runner could not complete (our own timeout / command-error
      sentinels) is infrastructure → mapped to ``transient``;
    * any other red gate (build/artifact/smoke/visual failure, incl. empty
      output) is a genuine blocking defect on the release candidate → ``real-bug``.

    Read by :func:`kanban_db._classify_escalation_payload` and mapped by the WEAK
    outcome fallback in ``kanban_db`` — WEAK so a genuine free-text signal in the
    gate output still classifies first (no over-claiming, AC-2). Pure/deterministic.
    """
    low = (last_error or "").lstrip().lower()
    for sentinel in _RELEASE_GATE_INFRA_SENTINELS:
        if low.startswith(sentinel):
            return "release_gate_infra"
    return "release_gate_red"


def _escalate_release_gate(
    conn: sqlite3.Connection, task_id: str, root_id: str, *,
    attempts: int, last_error: str, phase: str = "gate",
) -> None:
    from hermes_cli import kanban_db as kb

    # ``phase`` distinguishes a persistent-RED gate (the merged code never built/
    # smoked green) from a failed runtime ACTIVATION (code was green but the
    # backend restart / post-restart health failed). Both escalate to the
    # operator and keep the child blocked; only the human-facing wording and the
    # re-block reason differ. Default ``"gate"`` keeps the pre-S1 path byte-identical.
    if phase == "activation":
        why_now = (
            f"Runtime activation for chain {root_id} failed (backend restart / "
            "post-restart health) after the code gate was green"
        )
        comment = (
            f"⛔ Release-gate code was green but runtime activation failed → "
            "operator_escalation. The pre-deploy rollback anchor is in place; "
            "inspect the deploy/health output and roll back or re-activate."
        )
        block_reason = "release-gate activation failed"
    else:
        why_now = (
            f"Release gate for chain {root_id} still red after {attempts} "
            "bounded fixer attempt(s)"
        )
        comment = (
            f"⛔ Release-gate still red after {attempts} bounded fixer "
            "attempt(s) → operator_escalation. The fixer worked only in the "
            "chain worktree; live-main was never edited."
        )
        block_reason = "release-gate persistent red"

    payload = {
        "task": {"id": task_id},
        "why_now": why_now,
        "attempts_already_made": attempts,
        "evidence": {
            "last_error": (last_error or "")[-2000:],
            # ESCALATION-RELEASE-GATE-ERROR-CONTEXT-S1: structural failure-mode
            # context so a blind (opaque/visual-gate/empty) red gate classifies
            # into its real cause class instead of ``unclassified``. Pure context
            # enrichment — the block/escalation decision below is unchanged (AC-2).
            "trigger_outcome": _release_gate_trigger_outcome(last_error),
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
        kb.add_comment(conn, task_id, "verifier", comment)
    except Exception:
        _log.debug("release-gate escalation comment failed", exc_info=True)
    # Keep the child blocked (it already is). Re-block defensively if some
    # other path moved it.
    try:
        task = kb.get_task(conn, task_id)
        if task is not None and task.status not in ("blocked", "archived"):
            kb.block_task(conn, task_id, reason=block_reason)
    except Exception:
        _log.debug("release-gate re-block failed", exc_info=True)


def _activate_and_finalize(
    conn: sqlite3.Connection,
    task_id: str,
    root_id: str,
    fixer_attempts: int,
    activation_runner,
) -> dict:
    """Green CODE → REAL runtime activation → finish (done) or escalate.

    The gate proved the merged code builds/smokes; this step makes it *live* —
    build + backend restart + post-restart health via ``deploy_dashboard.sh``.
    On activation success the child is marked done (with the pre→post PID as
    evidence); on activation failure the child escalates to the operator and
    stays blocked (the deploy script's pre-deploy anchor tag is the rollback
    handle). Returns the ``execute_release_gate`` result dict."""
    act_ok, act_output, act_meta = activation_runner()
    _record_release_gate_activation(
        conn, task_id, ok=act_ok, output=act_output, meta=act_meta, root_id=root_id,
    )
    if act_ok:
        _finish_release_gate_green(
            conn, task_id, root_id, fixer_attempts, activation=act_meta,
        )
        return {
            "ok": True,
            "status": "green",
            "fixer_attempts": fixer_attempts,
            "root_id": root_id,
            "activation": act_meta,
        }
    _escalate_release_gate(
        conn, task_id, root_id, attempts=fixer_attempts,
        last_error=act_output, phase="activation",
    )
    return {
        "ok": False,
        "status": "escalated",
        "fixer_attempts": fixer_attempts,
        "root_id": root_id,
        "activation": act_meta,
    }


def _release_gate_retry_budget(
    output: str, *, explicit_max_retries: bool, max_retries: int,
) -> int:
    if not explicit_max_retries and (output or "").startswith("visual-gate:"):
        return visual_gate_max_retries()
    return max_retries


def _bind_release_gate_runner(injected_gate_runner, commands: list):
    """Build the gate callable used by validation worktrees and fixer cycles."""

    def release_gate(
        validation_root: Path, _changed_files: list[str],
    ) -> tuple[bool, str]:
        if injected_gate_runner is not None:
            return _invoke_release_gate_runner(
                injected_gate_runner, validation_root,
            )
        return _default_release_gate_runner(
            commands, repo_root=validation_root,
        )

    return release_gate


def _activate_if_still_current(
    conn: sqlite3.Connection,
    task_id: str,
    root_id: str,
    validated_commit: Optional[str],
    attempts: int,
    activation_runner,
    repo_root: Path,
) -> dict:
    """Never deploy a different live commit than the one just validated."""
    if not validated_commit:
        return _activate_and_finalize(
            conn, task_id, root_id, attempts, activation_runner,
        )

    guard_error = None
    lock_path = _integrator_lock_path(repo_root)
    with _PROCESS_LOCK:
        try:
            lock = _acquire_file_lock(lock_path)
        except WorktreeError as exc:
            guard_error = f"release activation lock failed: {exc}"
        else:
            try:
                try:
                    expected = _git(
                        repo_root,
                        "rev-parse",
                        f"{validated_commit}^{{commit}}",
                    )
                    live_head = _git(repo_root, "rev-parse", "HEAD")
                except WorktreeError as exc:
                    guard_error = (
                        f"release activation HEAD guard failed: {exc}"
                    )
                else:
                    guard_error = (
                        "release activation refused: live HEAD advanced from "
                        f"validated {expected} to {live_head}"
                        if expected != live_head
                        else None
                    )
                if not guard_error:
                    return _activate_and_finalize(
                        conn,
                        task_id,
                        root_id,
                        attempts,
                        activation_runner,
                    )
            finally:
                _release_file_lock(lock)

    _escalate_release_gate(
        conn,
        task_id,
        root_id,
        attempts=attempts,
        last_error=guard_error or "release activation guard failed",
    )
    return {
        "ok": False,
        "status": "escalated",
        "fixer_attempts": attempts,
        "root_id": root_id,
    }


def _run_release_gate_fixer_cycle(
    conn: sqlite3.Connection,
    task_id: str,
    root_id: str,
    repo_root: Path,
    *,
    release_gate,
    fixer_runner,
    activation_runner,
    injected_gate_runner,
    fixer_attempts: int,
    output: str,
) -> tuple[Optional[dict], str]:
    """One fixer attempt. Returns (result_or_None, next_output).

    When *result* is not None the caller must return it; otherwise continue the
    retry loop with the updated *output*.
    """
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
    branch_commit = None
    if (worktree / ".git").exists() and _branch_exists(repo_root, branch):
        try:
            branch_commit = _git(repo_root, "rev-parse", f"{branch}^{{commit}}")
        except WorktreeError:
            branch_commit = None

    if branch_commit is None and injected_gate_runner is not None:
        # Compatibility for existing isolated executor tests whose fake
        # fixer intentionally creates no git branch. Production fixers are
        # required to leave a real commit and never use this path.
        ok, output = release_gate(repo_root, [])
        phase = "injected_legacy"
    elif branch_commit is None:
        ok, output = False, (
            "release-gate fixer produced no integratable branch commit"
        )
        phase = "fixer_branch"
    else:
        ok, output = _run_release_gate_at_commit(
            repo_root,
            branch_commit,
            release_gate,
            allow_injected_legacy_fallback=False,
        )
        phase = "fixer_branch"
    _record_release_gate_executed(
        conn, task_id, attempt=fixer_attempts, ok=ok, output=output,
        root_id=root_id, fixer_error=fix_error, phase=phase,
        validation_commit=branch_commit,
    )
    if ok and branch_commit is None:
        # Synthetic legacy test seam only: there is no real ref to guard.
        return (
            _activate_and_finalize(
                conn, task_id, root_id, fixer_attempts, activation_runner,
            ),
            output,
        )
    if not ok:
        return None, output

    merged_gate_result: list[tuple[bool, str, str]] = []

    def merged_release_gate(
        validation_root: Path, changed_files: list[str],
    ) -> tuple[bool, str]:
        result = release_gate(validation_root, changed_files)
        merged_gate_result.append(
            (result[0], result[1], _git(validation_root, "rev-parse", "HEAD"))
        )
        return result

    try:
        target = current_branch(repo_root)
        integration = integrate_chain(
            repo_root,
            worktree,
            branch,
            target,
            gate_runner=merged_release_gate,
        )
    except Exception as exc:
        integration = {
            "action": "parked",
            "reason": f"release-gate fixer integration crashed: {exc}",
        }

    if merged_gate_result:
        merged_ok, merged_output, merged_commit = merged_gate_result[-1]
        _record_release_gate_executed(
            conn,
            task_id,
            attempt=fixer_attempts,
            ok=merged_ok,
            output=merged_output,
            root_id=root_id,
            phase="integrated_fixer_commit",
            validation_commit=merged_commit,
        )

    if integration.get("action") == "merged":
        return (
            _activate_if_still_current(
                conn, task_id, root_id,
                integration.get("merge_commit"), fixer_attempts,
                activation_runner, repo_root,
            ),
            output,
        )
    if integration.get("action") == "clean":
        live_commit = _git(repo_root, "rev-parse", "HEAD")
        if live_commit == branch_commit:
            return (
                _activate_if_still_current(
                    conn, task_id, root_id,
                    live_commit, fixer_attempts,
                    activation_runner, repo_root,
                ),
                output,
            )
    output = (
        "release-gate fixer integration failed: "
        + str(integration.get("reason") or integration.get("action"))
    )
    return None, output


def execute_release_gate(
    conn: sqlite3.Connection,
    task_id: str,
    *,
    gate_runner=None,
    fixer_runner=None,
    activation_runner=None,
    max_retries: Optional[int] = None,
    repo_root: Optional[Path] = None,
) -> dict:
    """Process a parked release-gate child end to end.

    Runs every gate at an exact commit in a clean detached validation worktree.
    On red: spawn up to *max_retries* bounded premium fixers inside the chain
    worktree/branch (never live-main). A green fixer branch is integrated through
    the serialized integrator and revalidated at the resulting live merge commit;
    activation can never consume an unmerged fixer commit. Persistent red or a
    failed integration → ``operator_escalation`` and the child stays blocked.

    On green CODE it does NOT stop at "builds" — it performs the REAL runtime
    activation (build + backend restart + post-restart health via
    ``deploy_dashboard.sh``); only after the restart lands and health passes is
    the child marked done. A failed activation escalates to the operator (the
    child stays blocked) — a green build is never silently reported as activated.

    This function is the SHARED activation core: the CLI runs it directly and the
    endpoint / auto-mode run it inside a detached transient unit (see
    :func:`spawn_release_gate_activation`) so the restart cannot kill the writer.

    ``gate_runner``/``fixer_runner``/``activation_runner`` are injectable seams
    (defaults wire to the detached-worktree subprocess gate, the claude-CLI
    fixer, and the ``deploy_dashboard.sh`` restart). Returns a result dict with ``status``
    (``"green"`` | ``"escalated"``) and ``fixer_attempts``.
    """
    ctx = _release_gate_context(conn, task_id)
    if ctx is None:
        raise ReleaseGateError(
            f"{task_id} is not a release-gate child "
            "(no release_gate_parked event)"
        )
    root_id = ctx["root_id"]
    explicit_max_retries = max_retries is not None
    if max_retries is None:
        max_retries = release_gate_fixer_max_retries()
    max_retries = max(0, int(max_retries))
    injected_gate_runner = gate_runner
    fixer_runner = fixer_runner or _default_release_gate_fixer
    activation_runner = activation_runner or _default_release_gate_activation
    repo_root = Path(repo_root or LIVE_CHECKOUT_ROOT)

    release_gate = _bind_release_gate_runner(
        injected_gate_runner, ctx["commands"],
    )

    # Attempt 0: validate the integration commit recorded when this release
    # child was created. Synthetic old unit-test seams may not carry a real ref;
    # default production execution never takes that compatibility fallback.
    ok, output = _run_release_gate_at_commit(
        repo_root,
        ctx.get("merge_commit"),
        release_gate,
        allow_injected_legacy_fallback=injected_gate_runner is not None,
    )
    _record_release_gate_executed(
        conn, task_id, attempt=0, ok=ok, output=output, root_id=root_id,
        phase="merged_commit", validation_commit=ctx.get("merge_commit"),
    )
    if ok:
        guarded_commit = ctx.get("merge_commit")
        if injected_gate_runner is not None and guarded_commit:
            try:
                _git(repo_root, "rev-parse", f"{guarded_commit}^{{commit}}")
            except WorktreeError:
                # Synthetic legacy test seam: the injected gate ran directly
                # because its fixture carries no real repository commit.
                guarded_commit = None
        return _activate_if_still_current(
            conn, task_id, root_id, guarded_commit, 0,
            activation_runner, repo_root,
        )

    fixer_attempts = 0
    while fixer_attempts < _release_gate_retry_budget(
        output,
        explicit_max_retries=explicit_max_retries,
        max_retries=max_retries,
    ):
        fixer_attempts += 1
        done, output = _run_release_gate_fixer_cycle(
            conn, task_id, root_id, repo_root,
            release_gate=release_gate,
            fixer_runner=fixer_runner,
            activation_runner=activation_runner,
            injected_gate_runner=injected_gate_runner,
            fixer_attempts=fixer_attempts,
            output=output,
        )
        if done is not None:
            return done

    _escalate_release_gate(
        conn, task_id, root_id, attempts=fixer_attempts, last_error=output,
    )
    return {
        "ok": False,
        "status": "escalated",
        "fixer_attempts": fixer_attempts,
        "root_id": root_id,
    }


def _release_gate_activation_unit(task_id: str) -> str:
    """Stable, sanitized transient-unit name for a task's activation. Doubles as
    a dedup guard: a second trigger while one is running fails to start (systemd:
    unit already exists) instead of racing two backend restarts."""
    safe = re.sub(r"[^A-Za-z0-9_.-]", "_", str(task_id))
    return f"hermes-release-gate-{safe}"


def spawn_release_gate_activation(
    task_id: str,
    board: Optional[str] = None,
    *,
    runner=None,
    hermes_bin: Optional[str] = None,
) -> dict:
    """Launch the release-gate activation as a DETACHED systemd transient unit.

    The activation restarts the dashboard backend. Run synchronously inside the
    dashboard request (``release_gate_endpoint``) the ``systemctl restart`` would
    kill the very process that must write the child's terminal result BEFORE it
    runs — the self-termination trap. ``systemd-run --user`` places the run in its
    OWN transient unit/cgroup, so it outlives both the request and the restart and
    writes the child done/escalated itself (the CLI process has kanban.db access).

    A bare ``setsid``/``Popen`` child would NOT survive: it stays in the dashboard
    unit's cgroup, and systemd's default ``KillMode=control-group`` reaps the whole
    cgroup on restart. The transient unit — not a double-fork — is the mechanism.

    The detached unit runs ``hermes kanban release-gate <task_id> --json`` which is
    the SAME :func:`execute_release_gate` core the CLI runs directly (AC-5). Returns
    ``{"ok", "unit", "detail"}``. ``runner`` is an injectable seam (defaults to
    ``subprocess.run``) so tests assert the argv without launching a unit."""
    bin_path = (
        hermes_bin
        or os.environ.get("HERMES_BIN")
        or shutil.which("hermes")
        or "hermes"
    )
    systemd_run = os.environ.get("HERMES_SYSTEMD_RUN_BIN") or "systemd-run"
    unit = _release_gate_activation_unit(task_id)

    # systemd --user transient units start from the user manager's environment,
    # not the caller's, so pass through the vars the CLI + deploy_dashboard.sh
    # need: an augmented PATH (npm/node/git/curl/systemctl/python3 must resolve),
    # the user-bus handles so ``systemctl --user restart`` works inside the unit,
    # and HERMES_HOME when the caller pins a non-default runtime root.
    path_prefix = [
        os.path.expanduser("~/.local/bin"),
        # ~/bin holds chromium-shot (the visual gate's screenshot tool) and other
        # operator scripts; without it the gate's tool lookup fails in the unit.
        os.path.expanduser("~/bin"),
        str(LIVE_CHECKOUT_ROOT / "venv" / "bin"),
        "/usr/local/bin", "/usr/bin", "/bin",
    ]
    existing_path = os.environ.get("PATH", "")
    merged_path = ":".join(path_prefix + ([existing_path] if existing_path else []))
    setenv = [f"--setenv=PATH={merged_path}"]
    for key in (
        "HERMES_HOME",
        "HERMES_KANBAN_HOME",
        "HERMES_KANBAN_DB",
        "XDG_RUNTIME_DIR",
        "DBUS_SESSION_BUS_ADDRESS",
    ):
        val = os.environ.get(key)
        if val:
            setenv.append(f"--setenv={key}={val}")

    # ``--board`` is a GLOBAL kanban option and argparse only accepts it BEFORE
    # the ``release-gate`` subcommand — appending it after the subcommand fails
    # with "unrecognized arguments". Build the CLI portion with the board slug in
    # front of the subcommand.
    cli_args = [bin_path, "kanban"]
    if board:
        cli_args += ["--board", str(board)]
    # --inline: the detached unit must run the gate core directly, NOT spawn a
    # second transient unit (which would deadlock: systemd-run waiting on a
    # nested systemd-run waiting on the restart it triggers).
    cli_args += ["release-gate", str(task_id), "--inline", "--json"]
    argv = [
        systemd_run, "--user", "--collect",
        f"--unit={unit}",
        f"--description=Hermes release-gate activation {task_id}",
        *setenv,
        *cli_args,
    ]

    run = runner or subprocess.run
    try:
        proc = run(  # noqa: S603 -- argv is a fixed list built above
            argv, capture_output=True, text=True,
            timeout=RELEASE_GATE_SPAWN_TIMEOUT,
        )
    except (OSError, subprocess.SubprocessError) as exc:
        return {"ok": False, "unit": unit, "detail": f"activation spawn failed: {exc}"}
    if proc.returncode == 0:
        return {
            "ok": True,
            "unit": unit,
            "detail": (
                "runtime activation started (detached); watch the release-gate "
                "task for green/escalation"
            ),
        }
    err = ((proc.stderr or "") + (proc.stdout or "")).strip()[-500:]
    return {
        "ok": False,
        "unit": unit,
        "detail": f"could not start activation unit (exit {proc.returncode}): {err}",
    }


def _terminal_status(status: str) -> bool:
    return status in {"done", "archived", "failed", "cancelled"}


_REAL_COMPLETION_STATUSES = frozenset({"done", "running", "ready", "blocked"})


def _is_real_completion_status(status: Optional[str]) -> bool:
    """Fail CLOSED: a status counts as real completion evidence only if it is
    one of the EXACT statuses that can legally reach this guard.

    Narrowed (cross-family review finding, 2026-07-17 pass 3): this used to
    accept every ``kanban_db.VALID_STATUSES`` member except ``archived`` — far
    wider than what can actually appear here. The finalizer hook
    (``maybe_integrate_on_complete`` -> ``_auto_complete_decompose_root``)
    runs from INSIDE ``complete_task``, BEFORE the completing task's own
    ``done`` write lands — so ``completed_task_id``'s row is read mid-flight.
    ``complete_task``'s worker-isolation guard (``_wt_eligible``) only ever
    invokes the hook for a task whose status is ``running``, ``ready``, or
    ``blocked`` at that point; the commitless path
    (``_direct_complete_decompose_root``) only ever passes already-``done``
    siblings (``finalize_decompose_root_at_dispatch``'s ``children_pending``
    guard requires every child to be ``done`` first). So exactly these four
    statuses are real completion evidence — every other
    ``kanban_db.VALID_STATUSES`` member (``triage``/``todo``/``scheduled``/
    ``review``/``archived``), a missing row (``None``), or a blank/unknown
    string means the root parks defensively instead of completing on
    evidence that could never legitimately reach here."""
    text = str(status or "").strip()
    return text in _REAL_COMPLETION_STATUSES


def _decompose_root_has_real_child_completion(
    conn: sqlite3.Connection,
    root_id: str,
    *,
    completed_task_id: Optional[str] = None,
    children: Optional[list[tuple[str, str]]] = None,
) -> bool:
    """True iff there is real evidence that a child actually did the work,
    rather than the chain simply running out of open (non-terminal) members.

    ``archive_task`` (a SUPERSEDED block) removes a child from ``task_links``
    and from every open/pending-sibling check, so a chain whose every child
    was only superseded-archived can otherwise look "no siblings left open"
    and get auto-completed with zero real work landed (live incident
    t_ecd5cf42, 2026-07-17). Guards ``_auto_complete_decompose_root``
    (``completed_task_id`` — the task whose completion drove this call) and
    ``_direct_complete_decompose_root`` (``children`` — the terminal-status
    list the caller already gathered) against exactly that: refuse when the
    only evidence on hand is an ``archived`` status, a missing row, or an
    unrecognized status; accept a genuine ``done`` (or any other currently
    valid in-flight status, e.g. a still-running completion) as real."""
    if completed_task_id is not None:
        row = conn.execute(
            "SELECT status FROM tasks WHERE id = ?", (completed_task_id,),
        ).fetchone()
        status = row["status"] if row is not None else None
        return _is_real_completion_status(status)
    if children is not None:
        return any(_is_real_completion_status(status) for _cid, status in children)
    return False


def _block_decompose_root_no_real_completion(
    conn: sqlite3.Connection, *, root_id: str,
) -> None:
    """Park a decompose root instead of auto-completing it when none of its
    chain's subtasks ever reached a real ``done`` (the whole chain is
    archived/cancelled/failed) — mirrors :func:`_block_decompose_root`'s
    shape, distinct reason so the operator sees exactly why."""
    from hermes_cli import kanban_db as kb

    reason = (
        "auto-complete verweigert: kein Kind erfolgreich (alle archived/failed) "
        "— Operator pruefen"
    )
    try:
        with kb.write_txn(conn):
            cur = conn.execute(
                "UPDATE tasks SET status = 'blocked', claim_lock = NULL, "
                "claim_expires = NULL, worker_pid = NULL "
                "WHERE id = ? AND status IN ('todo', 'ready', 'running', 'blocked')",
                (root_id,),
            )
            if cur.rowcount == 1:
                kb._append_event(
                    conn,
                    root_id,
                    "blocked",
                    {
                        "reason": reason,
                        "source": "decompose_root_finalizer",
                    },
                )
    except Exception:
        _log.warning(
            "could not block decompose root %s after no-real-completion guard",
            root_id, exc_info=True,
        )


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


def _auto_complete_decompose_root(
    conn: sqlite3.Connection,
    *,
    root_id: str,
    completed_task_id: str,
    outcome: dict,
) -> None:
    from hermes_cli import kanban_db as kb

    if not _decompose_root_has_real_child_completion(
        conn, root_id, completed_task_id=completed_task_id,
    ):
        _block_decompose_root_no_real_completion(conn, root_id=root_id)
        return

    now = int(time.time())
    summary = (
        "auto-completed decomposed root after all children completed and "
        f"`{outcome.get('branch', chain_branch(root_id))}` integrated"
    )
    stamp_after_commit = False
    with kb.write_txn(conn):
        cur = conn.execute(
            "UPDATE tasks "
            "SET status = 'done', result = ?, completed_at = ?, "
            "    claim_lock = NULL, claim_expires = NULL, worker_pid = NULL "
            "WHERE id = ? AND status IN ('todo', 'ready', 'running', 'blocked')",
            (summary, now, root_id),
        )
        if cur.rowcount != 1:
            return
        stamp_after_commit = True
        run_id = kb._end_run(
            conn,
            root_id,
            outcome="completed",
            status="done",
            summary=summary,
            metadata={
                "auto_completed_by": "decompose_root_finalizer",
                "completed_by": completed_task_id,
                "integration_action": outcome.get("action"),
                "merge_commit": outcome.get("merge_commit"),
            },
        )
        # Mirror child-side delivery witnesses onto the decompose root so
        # project_strategist_outcomes (root_id only) can mark the lever shipped.
        # Same event shapes as _record_integration_events_and_receipts.
        merge_commit = str(outcome.get("merge_commit") or "").strip()
        if (
            outcome.get("action") == "merged"
            and re.fullmatch(r"[0-9a-fA-F]{40}", merge_commit) is not None
        ):
            kb._append_event(conn, root_id, "integration_merged", outcome)
            kb._append_event(
                conn,
                root_id,
                "INTEGRATOR_VERIFIED",
                {
                    "merge_commit": outcome.get("merge_commit"),
                    "gate": outcome.get("gate"),
                    "state": outcome.get("state"),
                },
            )
        payload = {
            "completed_by": completed_task_id,
            "integration_action": outcome.get("action"),
            "branch": outcome.get("branch"),
            "merge_commit": outcome.get("merge_commit"),
        }
        kb._append_event(conn, root_id, "decompose_root_auto_completed", payload)
        kb._append_event(
            conn,
            root_id,
            "completed",
            {"result_len": len(summary), "summary": summary},
            run_id=run_id,
        )
    if stamp_after_commit:
        kb._stamp_strategist_lever_outcome_shipped(root_id, shipped_at=now)


def _direct_complete_decompose_root(
    conn: sqlite3.Connection,
    *,
    root_id: str,
    children: list[tuple[str, str]],
) -> None:
    """Finalize a COMMITLESS decompose root directly (AC-2b).

    Used when the chain left no provisioned worktree branch to integrate — all
    children ran (and committed, if at all) outside a chain worktree, so there
    is nothing for ``integrate_chain`` to merge. ``children`` is the list of
    ``(child_id, status)`` — all terminal-``done`` per the caller's contract —
    recorded as completion evidence. Emits the same
    ``decompose_root_auto_completed`` + ``completed`` events as the integrated
    path so downstream telemetry is uniform, plus an integrator comment listing
    the children as evidence.
    """
    from hermes_cli import kanban_db as kb

    if not _decompose_root_has_real_child_completion(
        conn, root_id, children=children,
    ):
        _block_decompose_root_no_real_completion(conn, root_id=root_id)
        return

    now = int(time.time())
    branch = chain_branch(root_id)
    child_ids = [cid for cid, _ in children]
    summary = (
        f"auto-completed decomposed root: all {len(children)} child task(s) "
        f"terminal-done, commitless chain (no `{branch}` branch to integrate)"
    )
    stamp_after_commit = False
    with kb.write_txn(conn):
        cur = conn.execute(
            "UPDATE tasks "
            "SET status = 'done', result = ?, completed_at = ?, "
            "    claim_lock = NULL, claim_expires = NULL, worker_pid = NULL "
            "WHERE id = ? AND status IN ('todo', 'ready', 'running', 'blocked')",
            (summary, now, root_id),
        )
        if cur.rowcount != 1:
            return
        stamp_after_commit = True
        run_id = kb._end_run(
            conn,
            root_id,
            outcome="completed",
            status="done",
            summary=summary,
            metadata={
                "auto_completed_by": "decompose_root_finalizer_commitless",
                "children": child_ids,
            },
        )
        kb._append_event(
            conn,
            root_id,
            "decompose_root_auto_completed",
            {
                "completed_by": "dispatch_finalizer",
                "integration_action": "commitless",
                "branch": branch,
                "children": child_ids,
            },
        )
        kb._append_event(
            conn,
            root_id,
            "completed",
            {"result_len": len(summary), "summary": summary},
            run_id=run_id,
        )
    if stamp_after_commit:
        kb._stamp_strategist_lever_outcome_shipped(root_id, shipped_at=now)
    # Evidence comment is written OUTSIDE the txn above — add_comment opens its
    # own write_txn (nesting would fail).
    evidence = "\n".join(f"- `{cid}` — {status}" for cid, status in children)
    try:
        kb.add_comment(
            conn,
            root_id,
            "integrator",
            "✅ Decompose-root finalized without integration (commitless chain — "
            "the children left no chain worktree branch to merge). Children "
            f"(all terminal):\n{evidence}",
        )
    except Exception:
        _log.debug("commitless finalize receipt comment failed", exc_info=True)


def _block_decompose_root(
    conn: sqlite3.Connection,
    *,
    root_id: str,
    reason: str,
    outcome: Optional[dict],
) -> None:
    """Park a decompose root whose integration did NOT complete (missing branch
    evidence / red post-merge gate / conflict). Moving it out of ``ready`` stops
    it re-entering the dispatch loop every tick, and surfaces it to the operator
    on the board — never silently completed (AC-5 spirit: a chain with lost or
    unmergeable commits must be parked, not auto-closed)."""
    from hermes_cli import kanban_db as kb

    try:
        with kb.write_txn(conn):
            cur = conn.execute(
                "UPDATE tasks SET status = 'blocked', claim_lock = NULL, "
                "claim_expires = NULL, worker_pid = NULL "
                "WHERE id = ? AND status IN ('todo', 'ready', 'running')",
                (root_id,),
            )
            if cur.rowcount == 1:
                kb._append_event(
                    conn,
                    root_id,
                    "blocked",
                    {
                        "reason": f"decompose-root finalize: {reason}",
                        "source": "decompose_root_finalizer",
                        "integration_action": (outcome or {}).get("action"),
                    },
                )
    except Exception:
        _log.warning(
            "could not block decompose root %s after failed finalize",
            root_id, exc_info=True,
        )


def finalize_decompose_root_at_dispatch(
    conn: sqlite3.Connection,
    root_id: str,
    *,
    dry_run: bool = False,
    gate_runner=None,
) -> str:
    """Dispatch-time finalizer for a decomposed chain root (Befund 6, 2026-07-02).

    Called by the dispatcher when a decompose root (``_is_decompose_root``)
    reaches ``ready``. A decompose root must NEVER be spawned as a worker — a
    spawn re-runs the whole chain. The completion-side integrator
    (:func:`maybe_integrate_on_complete`) auto-completes the root only when the
    LAST child completes FROM the provisioned worktree; a chain whose last
    completion came from a scratch workspace (the deferred branch at
    :func:`maybe_integrate_on_complete`) leaves the root stranded in ``ready``.
    This closes that gap without touching the happy path.

    Returns a short action string for telemetry:

    * ``"children_pending"`` — a child is still non-done → leave the chain
      visible, do NOT complete (AC-3). ``recompute_ready`` only promotes the
      root when all children are ``done``, so this is a defensive net.
    * ``"integrated"`` — a provisioned child exists → the EXISTING integrator
      (``maybe_integrate_on_complete`` → ``integrate_chain`` /
      ``_auto_complete_decompose_root``) ran and completed the root (AC-2a),
      byte-identical to the happy path.
    * ``"parked"`` — integration did not complete (missing branch evidence /
      red gate / conflict) → the root is blocked so it stops re-entering
      ``ready`` (AC-5 spirit).
    * ``"auto_completed_commitless"`` — no provisioned child / no chain branch
      → the root is directly completed with children evidence (AC-2b).
    * ``"would_integrate"`` / ``"would_complete_commitless"`` — dry-run
      preview, no side effects.
    """
    # Chain members via the SAME helper the integrator uses, so membership is
    # consistent. For a decompose root this yields {root} ∪ {build children}
    # (links are inverted: child=parent_id, root=child_id).
    members = _chain_member_ids(conn, root_id)
    members.discard(root_id)
    if members:
        placeholders = ",".join("?" for _ in members)
        child_rows = conn.execute(
            f"SELECT id, status, workspace_path FROM tasks "
            f"WHERE id IN ({placeholders})",
            tuple(members),
        ).fetchall()
    else:
        child_rows = []
    children = [(r["id"], r["status"]) for r in child_rows]

    # AC-3: any non-done child (open, blocked, gave_up, failed, …) — or no
    # children at all — means the chain is not finished. Leave it visible on
    # the board; never complete or spawn.
    if not child_rows or any(r["status"] != "done" for r in child_rows):
        return "children_pending"

    # A provisioned child committed into <repo>/.worktrees/kanban/<root_id>.
    # Its workspace_path survives completion (the done-UPDATE never clears it),
    # so a done child still points at the shared chain worktree — that lets the
    # existing integrator run byte-identically.
    prov_child = None
    for r in child_rows:
        sp = split_provisioned_path(r["workspace_path"])
        if sp is not None and sp[1] == root_id:
            prov_child = r["id"]
            break

    if dry_run:
        return "would_integrate" if prov_child else "would_complete_commitless"

    if prov_child is not None:
        # AC-2a: run the EXISTING integration path. Re-invoking the hook on the
        # (already-done) provisioned child recomputes chain membership, finds
        # the root as the single remaining open member, integrates the shared
        # branch and calls _auto_complete_decompose_root — the exact happy-path
        # code, unchanged.
        outcome = maybe_integrate_on_complete(
            conn, prov_child, gate_runner=gate_runner,
        )
        root_row = conn.execute(
            "SELECT status FROM tasks WHERE id = ?", (root_id,)
        ).fetchone()
        if root_row is not None and root_row["status"] == "done":
            return "integrated"
        # Integration parked / conflict / red gate (or the branch vanished after
        # commits → missing-branch-evidence park). Block the root so it stops
        # re-entering ready; never silently complete a chain with lost commits.
        reason = (outcome or {}).get("reason") or (
            f"integration did not complete (action={(outcome or {}).get('action')})"
        )
        _block_decompose_root(conn, root_id=root_id, reason=reason, outcome=outcome)
        return "parked"

    # AC-2b: commitless chain — no provisioned child, so no chain branch was
    # ever created. Complete the root directly with children evidence.
    _direct_complete_decompose_root(conn, root_id=root_id, children=children)
    return "auto_completed_commitless"


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

    # Don't trust repo_root_for (git rev-parse --show-toplevel) here: if
    # *resolved* is itself inside an existing LINKED worktree (e.g. a chain
    # branch checkout, or an ad-hoc ``.worktrees/<task.id>`` path that
    # split_provisioned_path didn't recognize above), --show-toplevel returns
    # that worktree's own path, not the main repo — ensure_worktree would then
    # nest a new provisioned worktree INSIDE the linked worktree and fail with
    # "branch already used" (live incident t_87143651). Derive the real main
    # repo root via --git-common-dir, which resolves to the main repo's .git
    # in both a plain checkout and a linked worktree.
    common_dir = kb._git_common_dir(resolved)
    if common_dir is None:
        return resolved  # non-repo dir task: today's behavior, untouched
    repo_root = common_dir.parent

    root_id = chain_root_id(conn, task.id)
    info = ensure_worktree(repo_root, root_id)
    # A workspace pointing at a SUBDIRECTORY of the repo keeps its relative
    # part inside the worktree (e.g. <repo>/web → <worktree>/web). Compute
    # that relative part against *resolved*'s OWN containing checkout root
    # (repo_root_for / --show-toplevel — main checkout OR a linked worktree,
    # whichever directly contains *resolved*), NOT against the new provisioned
    # repo_root above: a linked worktree lives at a different absolute path
    # than the main repo, so relative_to(repo_root) raises for a subdir
    # inside it and silently collapses to the new worktree's ROOT instead of
    # <new-worktree>/<subdir> (cross-family review finding 3, 2026-07-17).
    original_root = repo_root_for(resolved) or repo_root
    try:
        rel = resolved.resolve().relative_to(Path(original_root).resolve())
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

        leftovers_sorted = sorted(leftovers)
        listing = ", ".join(leftovers_sorted[:15])
        if len(leftovers_sorted) > 15:
            listing += f", … ({len(leftovers_sorted)} total)"
        dirty_class = _classify_dirty_paths(leftovers_sorted)
        kb.add_comment(
            conn, task_id, "integrator",
            f"⚠️ {dirty_class}: working tree is not clean after the worker run. "
            f"Uncommitted paths: {listing}. "
            f"{_dirty_recovery_instruction(dirty_class)}",
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


def _integrator_lock_path(repo_root: Path) -> Path:
    """Cross-process serialization lock shared by merge and activation."""
    repo_root = Path(repo_root)
    try:
        return (
            Path(_git(repo_root, "rev-parse", "--absolute-git-dir"))
            / "hermes-kanban-integrator.lock"
        )
    except (WorktreeError, subprocess.SubprocessError, OSError):
        return (
            repo_root
            / WORKTREES_DIRNAME
            / WORKTREES_NAMESPACE
            / ".integrator.lock"
        )


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
    themselves; ``<pkg>/<name>.py`` runs ``tests/<pkg>/test_<name>.py``.

    Fallback: when the 1:1 test file is absent (monolith source files whose
    tests are feature-named, e.g. ``gateway/run.py``), select the entire
    ``tests/<pkg>/`` directory so regressions are caught at the merge gate."""
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
        source = Path(f)
        rel_dir = str(source.parent)
        candidate = Path("tests") / rel_dir / f"test_{name}"
        if (repo_root / candidate).is_file():
            modules.add(str(candidate))
        modules.update(_feature_named_sibling_tests(repo_root, rel_dir, source))
        if not (repo_root / candidate).is_file():
            pkg_test_dir = Path("tests") / rel_dir
            if pkg_test_dir != Path("tests") and (repo_root / pkg_test_dir).is_dir():
                # Cap: if the directory has too many test files, downgrade to
                # no selection — the nightly full suite remains the backstop
                # (AC-2 counter-metric: no gate-tempo-for-coverage trade).
                test_file_count = sum(
                    1 for _p in (repo_root / pkg_test_dir).glob("test_*.py")
                )
                if test_file_count <= _FALLBACK_MAX_TEST_FILES:
                    modules.add(str(pkg_test_dir) + "/")
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


def _link_shared_dependencies(repo_root: Path, worktree: Path) -> None:
    """Expose the checkout's dependency trees in a short-lived worktree."""
    for rel in _NODE_MODULES_LINKS:
        src = Path(repo_root) / rel
        dst = Path(worktree) / rel
        if src.is_dir() and not dst.exists() and dst.parent.is_dir():
            try:
                dst.symlink_to(src, target_is_directory=True)
            except OSError:
                _log.warning("could not symlink %s into worktree %s", rel, worktree)


def _cleanup_validation_worktree(repo_root: Path, worktree: Path) -> None:
    """Remove a detached validation worktree and all of its registration."""
    for rel in _NODE_MODULES_LINKS:
        link = worktree / rel
        try:
            if link.is_symlink():
                link.unlink()
        except OSError:
            _log.warning("could not unlink %s from validation worktree", link)
    _reap_partial(repo_root, worktree)
    try:
        worktree.rmdir()
    except OSError:
        pass
    for parent in (worktree.parent, worktree.parent.parent):
        try:
            parent.rmdir()
        except OSError:
            break


def _run_gate_in_validation_worktree(
    repo_root: Path,
    commit: str,
    diff_files: list[str],
    gate: Callable[[Path, list[str]], tuple[bool, str]],
) -> tuple[bool, str]:
    """Run a gate at exactly *commit* in a clean, detached worktree.

    The worktree is registered under ``.worktrees/kanban-validation`` so it is
    invisible to live-checkout dirty detection, and is removed in ``finally``
    on green, red, timeout, or a crashing injected runner.
    """
    repo_root = Path(repo_root)
    expected = _git(repo_root, "rev-parse", f"{commit}^{{commit}}")
    base = repo_root / WORKTREES_DIRNAME / "kanban-validation"
    base.mkdir(parents=True, exist_ok=True)
    token = f"{os.getpid()}-{threading.get_ident()}-{time.time_ns()}"
    worktree = base / token
    try:
        _git(repo_root, "worktree", "add", "--detach", str(worktree), expected)
        actual = _git(worktree, "rev-parse", "HEAD")
        if actual != expected:
            return False, (
                "validation worktree commit mismatch: "
                f"expected {expected}, got {actual}"
            )
        _link_shared_dependencies(repo_root, worktree)
        return gate(worktree, diff_files)
    except Exception as exc:  # a broken gate must not pass silently
        return False, f"validation worktree gate failed: {exc}"
    finally:
        _cleanup_validation_worktree(repo_root, worktree)


# ---------------------------------------------------------------------------
# Post-merge quick gates (default_quick_gate / fo_integration_gate)
# ---------------------------------------------------------------------------

def _quick_gate_run_cmd(
    label: str, cmd: list[str], cwd: Path, timeout: int, notes: list[str],
) -> Optional[str]:
    """Run one quick-gate subprocess; append success note or return error."""
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


def _default_quick_gate_ruff(
    repo_root: Path, changed_files: list[str], notes: list[str],
) -> Optional[str]:
    """Ruff over changed .py files only; return error or None."""
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
        return _quick_gate_run_cmd("ruff", ruff_cmd, repo_root, 300, notes)
    # No .py files in diff — skip ruff entirely (non-Python-only change).
    notes.append("ruff skipped (no .py files in diff)")
    return None


def _default_quick_gate_pytest(
    repo_root: Path, changed_files: list[str], notes: list[str],
) -> Optional[str]:
    """Affected-pytest modules via isolated parallel runner; error or None."""
    modules = _affected_pytest_modules(repo_root, changed_files)
    if modules:
        # Run the affected modules through the canonical per-file isolation
        # runner (one freshly-spawned ``python -m pytest <file>`` subprocess
        # per file) rather than a single ``pytest <all modules>`` process.
        #
        # Why: ``tests/conftest.py`` documents per-file subprocess isolation as
        # THE cross-file isolation boundary and deliberately does NOT reset
        # module-level state between files — so running multiple files in one
        # pytest process can surface latent cross-file leaks as false-positive
        # failures. The worker gate (``scripts/run-affected.sh`` →
        # ``run_tests.sh`` → ``run_tests_parallel.py``) already isolates; this
        # post-merge gate must match it. The package-directory fallback in
        # ``_affected_pytest_modules`` (which can select a whole ``tests/<pkg>/``
        # directory) made the single-process run span hundreds of files and
        # parked chain t_c4ff7329 on exactly such a leak (web_* tests that pass
        # standalone failed when run after kanban tests in the same process).
        # The fallback's own walltime calibration (~26s for 437 files) assumes
        # the parallel isolated runner, not a single process.
        #
        # No extra pytest flags: invoke exactly like the canonical runner
        # (``python -m pytest <file>``). The previous ``-p no:cacheprovider``
        # was inherited into ``sys.argv`` by ``relaunch.build_relaunch_argv``
        # (it copies ambient process flags), failing test_relaunch.py — the
        # other half of the t_c4ff7329 park. ``.pytest_cache`` is gitignored,
        # so dropping the flag does not dirty the post-merge tree.
        runner = str(repo_root / "scripts" / "run_tests_parallel.py")
        return _quick_gate_run_cmd(
            f"pytest[{len(modules)}]",
            [sys.executable, runner, *modules],
            repo_root, 1200, notes,
        )
    notes.append("pytest skipped (no affected test modules)")
    return None


def _default_quick_gate_web(
    repo_root: Path, changed_files: list[str], notes: list[str],
) -> Optional[str]:
    """lint:control + tsc + vitest when web/ is in the diff; error or None."""
    if not any(f.startswith("web/") for f in changed_files):
        return None
    web_root = repo_root / "web"
    # Resolve tolerant of npm-workspace hoisting (bins may live in web/ OR
    # the hoisted ROOT node_modules/.bin). See _resolve_node_bin.
    tsc = _resolve_node_bin(repo_root, "tsc")
    vitest = _resolve_node_bin(repo_root, "vitest")
    npm_bin = shutil.which("npm") or "npm"
    npx_bin = shutil.which("npx") or "npx"

    err = _quick_gate_run_cmd(
        "lint:control", [npm_bin, "run", "lint:control"], web_root, 600, notes,
    )
    if err:
        return err
    if tsc is None:
        # Fail closed: a web diff we cannot type-check is not "green".
        return "tsc: web/ in diff but tsc not found in web/ or root node_modules/.bin"
    err = _quick_gate_run_cmd(
        "tsc -b", [str(tsc), "-b", "--noEmit"], web_root, 600, notes,
    )
    if err:
        return err
    if vitest is None:
        return (
            "vitest[control]: web/ in diff but "
            "vitest not found in web/ or root node_modules/.bin"
        )
    return _quick_gate_run_cmd(
        "vitest[control]", [npx_bin, "vitest", "run", "src/control"],
        web_root, 900, notes,
    )


def _default_quick_gate_visual(
    repo_root: Path, changed_files: list[str], notes: list[str],
) -> Optional[str]:
    """Optional visual gate when control UI paths changed; error or None."""
    if visual_gate_enabled() and any(
        f.startswith("web/src/control/") for f in changed_files
    ):
        err = _run_visual_gate(repo_root, _VISUAL_GATE_SCREENSHOTS_ROOT)
        if err:
            return _visual_gate_with_ime_note(err)
        notes.append(_VISUAL_GATE_IME_NOTE)
    return None


def default_quick_gate(repo_root: Path, changed_files: list[str]) -> tuple[bool, str]:
    """Post-merge quick gate (Entscheidung 5): ruff + affected pytest
    modules; when the diff touches ``web/``, run lint:control,
    ``tsc -b --noEmit``, and the control Vitest suite. ``npm run build`` intentionally
    stays out of this automatic merge path because it mutates generated
    dashboard assets and belongs to the parked post-merge release gate."""
    notes: list[str] = []

    err = _default_quick_gate_ruff(repo_root, changed_files, notes)
    if err:
        return False, err

    err = _default_quick_gate_pytest(repo_root, changed_files, notes)
    if err:
        return False, err

    err = _default_quick_gate_web(repo_root, changed_files, notes)
    if err:
        return False, err

    err = _default_quick_gate_visual(repo_root, changed_files, notes)
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
    if visual_gate_enabled():
        err = _run_visual_gate(repo_root, _VISUAL_GATE_SCREENSHOTS_ROOT)
        if err:
            return False, _visual_gate_with_ime_note(err)
        notes.append(_VISUAL_GATE_IME_NOTE)
    return True, "; ".join(notes)


# ---------------------------------------------------------------------------
# integrate_chain (serialized merge into the frozen target)
# ---------------------------------------------------------------------------

def _integrate_parked(branch: str, reason: str, **extra) -> dict:
    out = {"action": "parked", "reason": reason, "branch": branch}
    out.update(extra)
    return out


def _integrate_precheck_live(
    repo_root: Path, merge_target: Optional[str], branch: str,
) -> tuple[Optional[dict], Optional[str]]:
    """(0) live checkout clean operation state + frozen target.

    Returns ``(parked_result, None)`` on failure or ``(None, cur)`` on success.
    """
    try:
        git_dir = Path(_git(repo_root, "rev-parse", "--absolute-git-dir"))
    except WorktreeError as exc:
        return _integrate_parked(branch, f"cannot inspect live checkout: {exc}"), None
    for marker in ("MERGE_HEAD", "CHERRY_PICK_HEAD", "REVERT_HEAD",
                   "rebase-merge", "rebase-apply"):
        if (git_dir / marker).exists():
            return (
                _integrate_parked(
                    branch,
                    f"live checkout has an operation in progress ({marker})",
                ),
                None,
            )
    try:
        cur = current_branch(repo_root)
    except WorktreeError as exc:
        return _integrate_parked(branch, str(exc)), None
    if merge_target and cur != merge_target:
        return (
            _integrate_parked(
                branch,
                f"checked-out branch {cur!r} != frozen merge target "
                f"{merge_target!r}",
            ),
            None,
        )
    return None, cur


def _preserve_or_park_chain_artifacts(
    wt_path: Path, branch: str,
) -> tuple[Optional[dict], Optional[dict]]:
    """Preserve preservable dirty artifacts or park. Returns (failure, receipt)."""
    if not (wt_path.exists() and (wt_path / ".git").exists()):
        return None, None
    leftovers = dirty_files(wt_path)
    if not leftovers:
        return None, None
    leftovers_sorted = sorted(leftovers)
    dirty_class = _classify_dirty_paths(leftovers_sorted)
    if dirty_class != PRESERVABLE_ARTIFACTS_CLASS:
        return (
            _integrate_parked(
                branch,
                f"{dirty_class}: "
                + ", ".join(leftovers_sorted[:10])
                + ". "
                + _dirty_recovery_instruction(dirty_class),
                dirty_files=leftovers_sorted,
                park_class=dirty_class,
            ),
            None,
        )
    try:
        artifact_receipt = _preserve_artifact_files(
            wt_path, wt_path.name, leftovers_sorted
        )
    except Exception as exc:
        return (
            _integrate_parked(
                branch,
                f"ARTIFACT_PRESERVE_FAILED: {exc}",
                dirty_files=sorted(leftovers),
                park_class="ARTIFACT_PRESERVE_FAILED",
            ),
            None,
        )
    remaining = dirty_files(wt_path)
    if remaining:
        return (
            _integrate_parked(
                branch,
                "chain worktree has uncommitted changes after artifact cleanup: "
                + ", ".join(sorted(remaining)[:10]),
                dirty_files=sorted(remaining),
                artifact_receipt=artifact_receipt,
            ),
            None,
        )
    return None, artifact_receipt


def _integrate_empty_or_already_merged(
    repo_root: Path,
    wt_path: Path,
    branch: str,
    cur: str,
    gate_runner: Optional[Callable[[Path, list[str]], tuple[bool, str]]],
    artifact_receipt: Optional[dict],
) -> dict:
    """Handle ``ahead == 0``: reintegrate-after-revert, already-integrated, or empty."""
    already_integrated = _branch_is_ancestor(repo_root, branch, cur)
    if already_integrated:
        merged_commits = _first_parent_merges_reaching_branch(
            repo_root, branch, cur
        )
        if merged_commits:
            merge_commit = merged_commits[0]
            parents = _git(
                repo_root, "rev-list", "--parents", "-n", "1",
                merge_commit
            ).split()
            diff_files = _changed_files_between(
                repo_root, parents[1], branch
            ) if len(parents) >= 2 else []
            revert_commits = _revert_commits_for_merge(
                repo_root, merge_commit, cur
            )
            content_differs = False
            if diff_files:
                try:
                    _git(
                        repo_root,
                        "diff",
                        "--quiet",
                        cur,
                        branch,
                        "--",
                        *diff_files,
                    )
                except WorktreeError:
                    content_differs = True
            if revert_commits and content_differs:
                restore_commit = revert_commits[0]
                try:
                    _git(
                        repo_root, "revert", "--no-edit",
                        restore_commit,
                        timeout=MERGE_TIMEOUT_SECONDS,
                    )
                except (WorktreeError, subprocess.TimeoutExpired) as exc:
                    _git(repo_root, "revert", "--abort", check=False)
                    return _integrate_parked(
                        branch,
                        "reverted merge reachable by ancestry, but "
                        f"revert-of-revert failed: {exc}",
                        merge_commit=merge_commit,
                        revert_commit=restore_commit,
                        reintegrated_after_revert=False,
                    )
                restored_commit = _git(repo_root, "rev-parse", "HEAD")
                gate = gate_runner or _integration_gate_for_repo(repo_root)
                ok, detail = _run_gate_in_validation_worktree(
                    repo_root, restored_commit, diff_files, gate,
                )
                if not ok:
                    try:
                        _git(
                            repo_root,
                            "revert",
                            "--no-edit",
                            restored_commit,
                            timeout=MERGE_TIMEOUT_SECONDS,
                        )
                        reverted = True
                    except (WorktreeError, subprocess.TimeoutExpired) as exc:
                        _git(repo_root, "revert", "--abort", check=False)
                        reverted = False
                        detail += f" — AND REVERT FAILED: {exc}"
                    return _integrate_parked(
                        branch,
                        f"post-reintegration gate failed: {detail}",
                        merge_commit=merge_commit,
                        revert_commit=restore_commit,
                        restored_commit=restored_commit,
                        reverted=reverted,
                        reintegrated_after_revert=True,
                        gate_output=detail,
                    )
                remove_worktree(repo_root, wt_path, branch)
                result = {
                    "action": "merged",
                    "state": MERGED_GREEN,
                    "merge_commit": restored_commit,
                    "original_merge_commit": merge_commit,
                    "revert_commit": restore_commit,
                    "branch": branch,
                    "target": cur,
                    "gate": detail,
                    "files": len(diff_files),
                    "changed_files": diff_files,
                    "reintegrated_after_revert": True,
                }
                if artifact_receipt:
                    result["artifact_receipt"] = artifact_receipt
                return result
        remove_worktree(repo_root, wt_path, branch)
        result = {
            "action": "clean",
            "branch": branch,
            "target": cur,
            "already_integrated": True,
            "reason": f"chain branch already reachable from {cur}",
        }
        if artifact_receipt:
            result["artifact_receipt"] = artifact_receipt
        return result
    remove_worktree(repo_root, wt_path, branch)
    result = {"action": "clean", "branch": branch,
              "reason": "no commits on chain branch"}
    if artifact_receipt:
        result["artifact_receipt"] = artifact_receipt
    return result


def _integrate_rebase_branch(
    repo_root: Path, wt_path: Path, branch: str, cur: str,
) -> tuple[Optional[dict], Optional[list[str]]]:
    """(a2) Rebase chain onto live target. Returns (early_result, diff_files)."""
    # (a2) Rebase the chain branch onto the live target HEAD inside its
    # OWN worktree (B1), so the following merge is FF/conflict-free.
    # Reuse `cur` (the frozen, already-validated merge target) — do NOT
    # git fetch: repo_root is a LOCAL checkout, `cur`/HEAD is the live
    # local tip, and this integrator never pushes. (If a chain branch
    # could legitimately diverge from a REMOTE, escalate — do not add a
    # network fetch here.)
    if not (wt_path.exists() and (wt_path / ".git").exists()):
        return _integrate_parked(branch, "chain worktree missing before rebase"), None
    target_head = _git(repo_root, "rev-parse", cur)
    reverted_ancestor = _reverted_merged_ancestor(
        repo_root, branch, target_head,
    )
    rebase_args = ["rebase", target_head]
    if reverted_ancestor:
        replay_base = _git(
            repo_root, "rev-parse", f"{reverted_ancestor}^1",
        )
        rebase_args = [
            "rebase", "--onto", target_head, replay_base, branch,
        ]
    try:
        _git(wt_path, *rebase_args, timeout=MERGE_TIMEOUT_SECONDS)
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
        }, None
    # A reverted-ancestor replay can restore acceptance paths omitted by
    # the pre-rebase triple-dot diff as shared ancestry.  Gate and
    # receipt the actual tree delta that is about to land.
    diff_files = _changed_files_between(repo_root, target_head, branch)
    # Successful rebase: fall through to the existing merge block. The
    # --no-ff merge stays (preserves the merge-commit audit trail).
    return None, diff_files


def _integrate_merge_and_gate(
    repo_root: Path,
    wt_path: Path,
    branch: str,
    cur: str,
    diff_files: list[str],
    gate_runner: Optional[Callable[[Path, list[str]], tuple[bool, str]]],
    artifact_receipt: Optional[dict],
) -> dict:
    """(b) --no-ff merge + post-merge gate; park on red/conflict."""
    # (b) the merge itself; conflicts → abort + park.
    msg = f"kanban: merge {branch} (worker-isolation integrator)"
    try:
        _git(repo_root, "merge", "--no-ff", "--no-edit", "-m", msg,
             branch, timeout=MERGE_TIMEOUT_SECONDS)
    except (WorktreeError, subprocess.TimeoutExpired) as exc:
        _git(repo_root, "merge", "--abort", check=False)
        return _integrate_parked(
            branch, f"merge conflict/failure (aborted): {exc}",
        )
    merge_commit = _git(repo_root, "rev-parse", "HEAD")

    # Post-merge quick gate (Entscheidung 5); red → revert -m 1 + park.
    # The gate runs at the exact merge commit in a clean detached
    # validation worktree, never in the potentially dirty live checkout.
    gate = gate_runner or _integration_gate_for_repo(repo_root)
    ok, detail = _run_gate_in_validation_worktree(
        repo_root, merge_commit, diff_files, gate,
    )
    if not ok:
        try:
            _git(repo_root, "revert", "-m", "1", "--no-edit",
                 merge_commit, timeout=MERGE_TIMEOUT_SECONDS)
            reverted = True
        except (WorktreeError, subprocess.TimeoutExpired) as exc:
            _git(repo_root, "revert", "--abort", check=False)
            reverted = False
            detail += f" — AND REVERT FAILED: {exc}"
        return _integrate_parked(
            branch,
            f"post-merge gate failed: {detail}",
            merge_commit=merge_commit, reverted=reverted,
            gate_output=detail,
        )

    remove_worktree(repo_root, wt_path, branch)
    result = {
        "action": "merged",
        "state": MERGED_GREEN,
        "merge_commit": merge_commit,
        "branch": branch,
        "target": cur,
        "gate": detail,
        "files": len(diff_files),
        "changed_files": diff_files,
    }
    if artifact_receipt:
        result["artifact_receipt"] = artifact_receipt
    return result


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

    # Lock lives in the repo's .git dir: never visible in `git status`,
    # never blocks worktree cleanup, and is shared with release activation.
    lock_path = _integrator_lock_path(repo_root)
    with _PROCESS_LOCK:
        try:
            lock = _acquire_file_lock(lock_path)
        except WorktreeError as exc:
            return _integrate_parked(branch, str(exc))
        try:
            if not _branch_exists(repo_root, branch):
                return {"action": "clean", "branch": branch,
                        "reason": "chain branch does not exist (nothing to merge)"}

            # (0) live checkout in a clean operation state + frozen target.
            parked, cur = _integrate_precheck_live(
                repo_root, merge_target, branch,
            )
            if parked is not None:
                return parked

            preserve_failure, artifact_receipt = _preserve_or_park_chain_artifacts(
                wt_path, branch,
            )
            if preserve_failure:
                return preserve_failure

            ahead = _git(repo_root, "rev-list", "--count", f"{cur}..{branch}")
            if ahead == "0":
                return _integrate_empty_or_already_merged(
                    repo_root, wt_path, branch, cur, gate_runner,
                    artifact_receipt,
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
                return _integrate_parked(
                    branch,
                    "dirty files in live checkout overlap the branch diff: "
                    + ", ".join(overlap[:10]),
                )

            early, diff_files = _integrate_rebase_branch(
                repo_root, wt_path, branch, cur,
            )
            if early is not None:
                return early

            return _integrate_merge_and_gate(
                repo_root, wt_path, branch, cur, diff_files,
                gate_runner, artifact_receipt,
            )
        finally:
            _release_file_lock(lock)


# ---------------------------------------------------------------------------
# Completion hook (maybe_integrate_on_complete)
# ---------------------------------------------------------------------------

def _find_open_chain_sibling(
    conn: sqlite3.Connection,
    task_id: str,
    members: set[str],
    wt: Path,
):
    """Chain-complete check via BOTH signals, conservatively OR-ed.

    (a) task_links membership from the chain root — covers unclaimed
    children whose workspace_path still points at the repo root;
    (b) same provisioned worktree path — covers tasks attached to the
    worktree outside the link graph (e.g. cloned fix tasks).
    """
    open_sibling = None
    if members:
        placeholders = ",".join("?" for _ in members)
        open_sibling = conn.execute(
            f"SELECT 1 FROM tasks WHERE id IN ({placeholders}) "
            "AND status NOT IN ('done', 'archived', 'failed', 'cancelled') "
            "AND NOT EXISTS (SELECT 1 FROM task_events e "
            "WHERE e.task_id = tasks.id AND e.kind = 'release_gate_parked') "
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
    return open_sibling


def _resolve_open_sibling_finalizer(
    conn: sqlite3.Connection,
    task_id: str,
    root_id: str,
    wt: Path,
    members: set[str],
    open_sibling,
) -> tuple[Optional[dict], Optional[str]]:
    """If open siblings remain, decide deferred vs auto-complete root.

    Returns ``(deferred_result, None)`` or ``(None, auto_complete_root_id)``.
    """
    auto_complete_root_id = None
    if not open_sibling:
        return None, auto_complete_root_id
    pending_root_id = _pending_root_finalizer_id(
        conn, task_id=task_id, root_id=root_id, wt=wt, members=members,
    )
    if pending_root_id is not None and _is_decompose_root(conn, pending_root_id):
        auto_complete_root_id = pending_root_id
    elif pending_root_id is not None:
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
    else:
        return {"action": "deferred", "reason": "chain has open siblings"}, None
    if auto_complete_root_id is None:
        return {"action": "deferred", "reason": "chain has open siblings"}, None
    return None, auto_complete_root_id


def _recover_missing_branch_integration(
    conn: sqlite3.Connection,
    task_id: str,
    root_id: str,
    repo_root: Path,
    branch: str,
    target: Optional[str],
    kb,
) -> dict:
    """Recover or park when the chain branch is already gone after a prior merge."""
    # A previous completion attempt may have merged, gated, and removed the
    # branch/worktree before its later DB done/outbox transaction rolled
    # back.  Recover only from two durable integration witnesses whose
    # commit still reaches the frozen target; a bare missing branch remains
    # a hard park.
    merged_row = conn.execute(
        "SELECT payload FROM task_events WHERE task_id = ? "
        "AND kind = 'integration_merged' ORDER BY id DESC LIMIT 1",
        (task_id,),
    ).fetchone()
    verified_row = conn.execute(
        "SELECT payload FROM task_events WHERE task_id = ? "
        "AND kind = 'INTEGRATOR_VERIFIED' ORDER BY id DESC LIMIT 1",
        (task_id,),
    ).fetchone()
    try:
        merged_payload = json.loads(merged_row["payload"]) if merged_row else {}
        verified_payload = (
            json.loads(verified_row["payload"]) if verified_row else {}
        )
    except (TypeError, ValueError, json.JSONDecodeError):
        merged_payload = {}
        verified_payload = {}
    merge_commit = str(merged_payload.get("merge_commit") or "").strip()
    verified_commit = str(verified_payload.get("merge_commit") or "").strip()
    recorded_target = str(merged_payload.get("target") or target).strip()
    if (
        merge_commit
        and merge_commit == verified_commit
        and recorded_target == target
        and _branch_is_ancestor(repo_root, merge_commit, target)
    ):
        changed_files = [
            str(path)
            for path in merged_payload.get("changed_files", [])
            if str(path).strip()
        ]
        content_differs = False
        if changed_files:
            try:
                _git(
                    repo_root,
                    "diff",
                    "--quiet",
                    merge_commit,
                    target,
                    "--",
                    *changed_files,
                )
            except WorktreeError:
                content_differs = True
        if content_differs:
            revert_commits = _revert_commits_for_merge(
                repo_root, merge_commit, target,
            )
            return {
                **merged_payload,
                "action": "parked",
                "reason": (
                    "recorded green merge content is no longer active on "
                    f"{target}; refusing already-integrated recovery"
                ),
                "branch": branch,
                "target": target,
                "merge_commit": merge_commit,
                "revert_commits": revert_commits,
                "content_drift_after_merge": True,
            }
        outcome = {
            **merged_payload,
            "action": "clean",
            "already_integrated": True,
            "merge_commit": merge_commit,
            "branch": branch,
            "target": target,
            "reconciled_from": "integration_merged+INTEGRATOR_VERIFIED",
        }
        release_row = conn.execute(
            "SELECT payload FROM task_events WHERE task_id = ? "
            "AND kind = 'release_gate_created' ORDER BY id DESC LIMIT 1",
            (task_id,),
        ).fetchone()
        if release_row is not None:
            try:
                release_payload = json.loads(release_row["payload"] or "{}")
            except (TypeError, ValueError, json.JSONDecodeError):
                release_payload = {}
            child_id = str(release_payload.get("child_id") or "").strip()
            if child_id:
                outcome["release_gate_child_id"] = child_id
        if (
            "release_gate_child_id" not in outcome
            and outcome.get("release_gate_required")
        ):
            try:
                _create_parked_release_gate_child(
                    conn, task_id, root_id, outcome,
                )
            except Exception as exc:
                return {
                    **outcome,
                    "action": "parked",
                    "reason": f"required release-gate creation failed: {exc}",
                    "release_gate_creation_failed": True,
                }
        return outcome
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


def _maybe_auto_complete_after_integration(
    conn: sqlite3.Connection,
    auto_complete_root_id: Optional[str],
    task_id: str,
    outcome: dict,
) -> None:
    if auto_complete_root_id is not None:
        try:
            _auto_complete_decompose_root(
                conn,
                root_id=auto_complete_root_id,
                completed_task_id=task_id,
                outcome=outcome,
            )
        except Exception:
            _log.warning(
                "could not auto-complete decompose root %s",
                auto_complete_root_id,
                exc_info=True,
            )


def _record_integration_events_and_receipts(
    conn: sqlite3.Connection,
    task_id: str,
    root_id: str,
    target: Optional[str],
    outcome: dict,
    auto_complete_root_id: Optional[str],
    kb,
) -> dict:
    """Write integration events/comments/release-gate; return final outcome."""
    try:
        with kb.write_txn(conn):
            kind = {
                "merged": "integration_merged",
                "clean": "integration_clean",
                "rebase_conflict": "integration_rebase_conflict",
            }.get(outcome["action"], "integration_parked")
            kb._append_event(conn, task_id, kind, outcome)
            if outcome.get("artifact_receipt"):
                kb._append_event(
                    conn,
                    task_id,
                    "artifact_preserved",
                    outcome["artifact_receipt"],
                )
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
    if outcome.get("artifact_receipt"):
        receipt = outcome["artifact_receipt"]
        paths = receipt.get("paths", [])
        try:
            kb.add_comment(
                conn, task_id, "integrator",
                "📎 Preserved Visual-QA artifacts: "
                f"{receipt.get('file_count', 0)} file(s) copied to "
                f"`{receipt.get('destination')}`; removed dirty path(s): "
                f"{', '.join(paths[:20])}.",
            )
        except Exception:
            _log.debug("artifact preserve receipt comment failed", exc_info=True)
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
        if outcome.get("release_gate_required"):
            try:
                _create_parked_release_gate_child(conn, task_id, root_id, outcome)
            except Exception as exc:
                _log.warning(
                    "could not create parked release-gate child for %s",
                    task_id, exc_info=True,
                )
                return {
                    **outcome,
                    "action": "parked",
                    "reason": f"required release-gate creation failed: {exc}",
                    "release_gate_creation_failed": True,
                }
        _maybe_auto_complete_after_integration(
            conn, auto_complete_root_id, task_id, outcome,
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
        _maybe_auto_complete_after_integration(
            conn, auto_complete_root_id, task_id, outcome,
        )
    return outcome


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
    open_sibling = _find_open_chain_sibling(conn, task_id, members, wt)
    deferred, auto_complete_root_id = _resolve_open_sibling_finalizer(
        conn, task_id, root_id, wt, members, open_sibling,
    )
    if deferred is not None:
        return deferred

    target = frozen_merge_target(conn, root_id)
    branch = chain_branch(root_id)
    if not _branch_exists(repo_root, branch):
        return _recover_missing_branch_integration(
            conn, task_id, root_id, repo_root, branch, target, kb,
        )
    outcome = integrate_chain(
        repo_root, wt, branch, target, gate_runner=gate_runner,
    )
    if outcome.get("action") == "merged" and any(
        str(path).startswith("web/")
        for path in outcome.get("changed_files", [])
    ):
        outcome["release_gate_required"] = True

    return _record_integration_events_and_receipts(
        conn, task_id, root_id, target, outcome, auto_complete_root_id, kb,
    )
