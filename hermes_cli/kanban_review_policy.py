"""Fork-owned review policy derived from the actual Git diff.

The Kanban monolith owns orchestration.  This edge owns the path-risk floor
and the one immutable decision context shared by skip, defer, and submission.
"""

from __future__ import annotations

from dataclasses import dataclass
import hashlib
import json
import os
from pathlib import Path, PurePosixPath
import re
import sqlite3
import subprocess
from typing import Callable, Iterable, Optional


TIER_ORDER = {"standard": 0, "review": 1, "critical": 2}
_TERMINAL = frozenset(
    {"archived", "canceled", "cancelled", "done", "failed"}
)
_EXCLUDED_ROOTS = frozenset(
    {"docs", "optional-skills", "skills", "tests", "website"}
)
_PRODUCTION_ROOTS = frozenset(
    {
        "acp_adapter",
        "agent",
        "apps",
        "gateway",
        "hermes_cli",
        "model_tools.py",
        "plugins",
        "run_agent.py",
        "tools",
        "toolsets.py",
        "web",
    }
)
_AUTH_TOKEN_RE = re.compile(
    r"(?:^|[^a-z0-9])"
    r"(?:auth|authn|authz|authentication|oauth|credential|credentials|secret|secrets)"
    r"(?:$|[^a-z0-9])"
)
_AUTONOMY_CORE_PATHS = frozenset(
    {
        "agent/tool_executor.py",
        "gateway/run.py",
        "hermes_cli/kanban_db.py",
        "hermes_cli/kanban_worktrees.py",
        "model_tools.py",
        "run_agent.py",
        "tools/registry.py",
        "toolsets.py",
    }
)
_MIGRATION_SUFFIXES = frozenset({".js", ".py", ".sql", ".ts"})
_FORK_POINT_CLAMP_BASELINE_KINDS = frozenset(
    {
        "candidate_parent_same_tree_fallback",
        "pre_run_commit_sha",
    }
)


@dataclass(frozen=True)
class PathRiskMatch:
    rule_id: str
    path: str
    tier: str = "critical"


@dataclass(frozen=True)
class PathRiskDecision:
    tier: str
    matches: tuple[PathRiskMatch, ...]

    @property
    def rule_ids(self) -> tuple[str, ...]:
        return tuple(dict.fromkeys(match.rule_id for match in self.matches))


@dataclass(frozen=True)
class ReviewDecisionContext:
    workspace_path: str = ""
    repo_identity: str = ""
    run_id: Optional[int] = None
    baseline_commit: Optional[str] = None
    baseline_kind: str = "unavailable"
    candidate_commit: Optional[str] = None
    target_ref: Optional[str] = None
    target_head: Optional[str] = None
    chain_root_id: Optional[str] = None
    chain_kind: str = "solo"
    member_ids: tuple[str, ...] = ()
    planspec_source: str = ""
    changed_files: tuple[str, ...] = ()
    evidence_complete: bool = False
    chain_accumulated: bool = False
    evidence_error: Optional[str] = None
    accumulated_required: bool = False
    path_state_digest: Optional[str] = None
    baseline_clamped_to_fork_point: bool = False
    pre_clamp_baseline_commit: Optional[str] = None
    clamp_target_ref: Optional[str] = None

    @property
    def path_risk(self) -> PathRiskDecision:
        return classify_changed_paths(self.changed_files)

    @property
    def can_defer(self) -> bool:
        if not self.evidence_complete or len(self.member_ids) <= 1:
            return False
        if self.chain_kind == "linked":
            return bool(self.target_ref and self.target_head)
        return self.chain_kind in {"legacy_active", "legacy_stamped"}


class ReviewEvidenceError(RuntimeError):
    """Accumulated review evidence was required but could not be captured."""


@dataclass(frozen=True)
class _LegacyDeferResolution:
    required: bool = False
    baseline: Optional[str] = None
    members: tuple[str, ...] = ()
    error: Optional[str] = None


def normalize_repo_path(path: object) -> str:
    """Return a stable repository-relative POSIX path."""
    raw = str(path or "").replace("\\", "/").strip()
    while raw.startswith("./"):
        raw = raw[2:]
    raw = re.sub(r"/+", "/", raw).lstrip("/")
    parts = [part for part in PurePosixPath(raw).parts if part not in {"", "."}]
    return "/".join(parts)


def classify_changed_paths(paths: Iterable[object]) -> PathRiskDecision:
    """Classify production paths independently of task prose."""
    matches: list[PathRiskMatch] = []
    seen: set[tuple[str, str]] = set()
    for raw_path in paths:
        path = normalize_repo_path(raw_path)
        if not path:
            continue
        parts = path.split("/")
        root = parts[0].lower()
        lowered = path.lower()
        if root in _EXCLUDED_ROOTS or lowered.startswith("hermes_cli/web_dist/"):
            continue

        rule_id: Optional[str] = None
        if path in _AUTONOMY_CORE_PATHS:
            rule_id = "autonomy_core"
        elif (
            lowered.startswith("supabase/migrations/")
            or lowered.startswith("alembic/versions/")
            or (
                "migrations" in {part.lower() for part in parts[:-1]}
                and PurePosixPath(path).suffix.lower() in _MIGRATION_SUFFIXES
            )
        ):
            rule_id = "schema_migration"
        elif root in _PRODUCTION_ROOTS and _AUTH_TOKEN_RE.search(lowered):
            rule_id = "auth_surface"

        if rule_id is not None and (rule_id, path) not in seen:
            seen.add((rule_id, path))
            matches.append(PathRiskMatch(rule_id=rule_id, path=path))

    return PathRiskDecision(
        tier="critical" if matches else "standard",
        matches=tuple(matches),
    )


def _git(workspace: str, *args: str) -> Optional[str]:
    try:
        proc = subprocess.run(
            ["git", "-C", workspace, *args],
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=5,
            check=False,
        )
    except (OSError, subprocess.SubprocessError, ValueError):
        return None
    return (proc.stdout or "") if proc.returncode == 0 else None


def _git_succeeds(workspace: str, *args: str) -> bool:
    try:
        proc = subprocess.run(
            ["git", "-C", workspace, *args],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            timeout=5,
            check=False,
        )
    except (OSError, subprocess.SubprocessError, ValueError):
        return False
    return proc.returncode == 0


def _repo_identity(workspace: str) -> str:
    common_dir = _git(workspace, "rev-parse", "--git-common-dir")
    if not common_dir or not common_dir.strip():
        return ""
    raw = common_dir.strip()
    if not os.path.isabs(raw):
        raw = os.path.join(workspace, raw)
    return str(Path(raw).resolve())


def _workspace_identity(workspace: str) -> str:
    try:
        return str(Path(workspace).expanduser().resolve())
    except (OSError, RuntimeError, ValueError):
        return ""


def _branch_identity(workspace: str) -> str:
    branch = _git(workspace, "symbolic-ref", "--quiet", "--short", "HEAD")
    return branch.strip() if branch else ""


def _task_run_baseline(
    conn: sqlite3.Connection,
    task_row: sqlite3.Row,
    run_id: Optional[int],
) -> Optional[str]:
    selected_run = run_id
    if selected_run is None and task_row["current_run_id"] is not None:
        try:
            selected_run = int(task_row["current_run_id"])
        except (TypeError, ValueError):
            selected_run = None
    if selected_run is None:
        return None
    try:
        row = conn.execute(
            "SELECT pre_run_commit_sha FROM task_runs "
            "WHERE id = ? AND task_id = ?",
            (selected_run, task_row["id"]),
        ).fetchone()
    except sqlite3.Error:
        return None
    return str(row["pre_run_commit_sha"] or "").strip() or None if row else None


def _same_tree_parent_fallback(
    workspace: str,
    baseline: str,
    candidate: str,
) -> tuple[str, str]:
    baseline_tree = _git(workspace, "rev-parse", "--verify", f"{baseline}^{{tree}}")
    candidate_tree = _git(workspace, "rev-parse", "--verify", f"{candidate}^{{tree}}")
    parents = _git(workspace, "show", "-s", "--format=%P", candidate)
    if (
        baseline_tree
        and candidate_tree
        and baseline_tree.strip() == candidate_tree.strip()
        and parents
        and len(parents.split()) == 1
    ):
        parent = parents.strip()
        if _git(workspace, "rev-parse", "--verify", f"{parent}^{{commit}}"):
            return parent, "candidate_parent_same_tree_fallback"
    return baseline, "pre_run_commit_sha"


def _linked_chain(
    conn: sqlite3.Connection,
    task_id: str,
) -> tuple[Optional[str], tuple[str, ...], Optional[str]]:
    try:
        from hermes_cli import kanban_worktrees as worktrees

        root_id = worktrees.chain_root_id(conn, task_id)
        members = tuple(sorted(worktrees._chain_member_ids(conn, root_id)))
        if len(members) <= 1:
            return None, (task_id,), None
        return root_id, members, worktrees.frozen_merge_target(conn, root_id)
    except Exception:
        return None, (task_id,), None


def _recorded_task_commits(
    conn: sqlite3.Connection,
    task_id: str,
    *,
    workspace: str,
) -> tuple[str, ...]:
    """Resolve task-local worker commits from persisted run receipts.

    This deliberately mirrors the receipt source and resolution behavior of
    ``kanban_worktrees._lane_scope_recorded_task_commit_paths``: malformed
    metadata is ignored, duplicate commit receipts are collapsed, and
    unresolvable commits do not count as usable attribution.

    Note the deliberate difference in granularity: that helper answers with a
    PATH set and therefore also rejects a commit which resolves but yields no
    path (a merge commit, an empty commit) as unattributable. This function
    answers with COMMITS, where resolvability is the whole question, so a
    merge commit stays a usable commit here.
    """
    try:
        rows = conn.execute(
            "SELECT metadata FROM task_runs "
            "WHERE task_id = ? ORDER BY id DESC",
            (task_id,),
        ).fetchall()
    except sqlite3.Error:
        return ()

    commits: list[str] = []
    seen: set[str] = set()
    for row in rows:
        raw = row["metadata"] if isinstance(row, sqlite3.Row) else row[0]
        if not raw:
            continue
        try:
            metadata = json.loads(raw)
        except (TypeError, json.JSONDecodeError):
            continue
        if not isinstance(metadata, dict):
            continue
        commit = str(metadata.get("commit") or "").strip()
        if not commit or commit in seen:
            continue
        seen.add(commit)
        resolved = _git(
            workspace,
            "rev-parse",
            "--verify",
            f"{commit}^{{commit}}",
        )
        if resolved:
            commits.append(resolved.strip())
    return tuple(commits)


def _clamp_to_fork_point(
    conn: sqlite3.Connection,
    task_id: str,
    *,
    workspace: str,
    baseline: str,
    baseline_kind: str,
    candidate: str,
) -> tuple[str, bool, Optional[str]]:
    """Advance a stale frozen base forward to its target branch fork point.

    Clamp only when the candidate is not already landed, the base moves
    forward, and no recorded worker commit for this task lies in the proposed
    ``(baseline, fork_point]`` span.

    Remaining attribution gap: when this task has no recorded commit receipts
    (or none resolves), the landed-candidate guard is the only upper bound.
    Task work inside ``(baseline, fork_point]`` then cannot be distinguished
    from target-branch movement; this is a known gap, not intended semantics.
    """
    if baseline_kind not in _FORK_POINT_CLAMP_BASELINE_KINDS:
        return baseline, False, None

    target_ref = "main"
    try:
        from hermes_cli import kanban_worktrees as worktrees

        root_id = worktrees.chain_root_id(conn, task_id)
        target_ref = worktrees.frozen_merge_target(conn, root_id) or "main"
    except Exception:
        target_ref = "main"

    target_out = _git(
        workspace, "rev-parse", "--verify", f"{target_ref}^{{commit}}"
    )
    target_head = target_out.strip() if target_out else None
    if not target_head and target_ref != "main":
        target_ref = "main"
        target_out = _git(
            workspace, "rev-parse", "--verify", "main^{commit}"
        )
        target_head = target_out.strip() if target_out else None
    if not target_head:
        return baseline, False, None

    if _git_succeeds(
        workspace,
        "merge-base",
        "--is-ancestor",
        candidate,
        target_head,
    ):
        return baseline, False, None

    fork_out = _git(workspace, "merge-base", candidate, target_head)
    fork_point = fork_out.strip() if fork_out else None
    if not fork_point or fork_point == baseline:
        return baseline, False, None
    if not _git_succeeds(
        workspace, "merge-base", "--is-ancestor", baseline, fork_point
    ):
        return baseline, False, None
    for recorded_commit in _recorded_task_commits(
        conn,
        task_id,
        workspace=workspace,
    ):
        if (
            recorded_commit != baseline
            and _git_succeeds(
                workspace,
                "merge-base",
                "--is-ancestor",
                baseline,
                recorded_commit,
            )
            and _git_succeeds(
                workspace,
                "merge-base",
                "--is-ancestor",
                recorded_commit,
                fork_point,
            )
        ):
            return baseline, False, None
    return fork_point, True, target_ref


def _prior_linked_defer(
    conn: sqlite3.Connection,
    task_id: str,
    member_ids: tuple[str, ...],
) -> bool:
    prior_ids = [member for member in member_ids if member != task_id]
    if not prior_ids:
        return False
    placeholders = ",".join("?" for _ in prior_ids)
    try:
        return (
            conn.execute(
                "SELECT 1 FROM task_events "
                f"WHERE task_id IN ({placeholders}) "
                "AND kind = 'review_deferred_to_tip' LIMIT 1",
                tuple(prior_ids),
            ).fetchone()
            is not None
        )
    except sqlite3.Error:
        return False


def _active_legacy_cohort(
    conn: sqlite3.Connection,
    task_id: str,
    source: str,
    *,
    workspace: str,
    repo_identity: str,
) -> tuple[str, ...]:
    try:
        rows = conn.execute(
            "SELECT id, assignee, kind, status, workspace_path FROM tasks "
            "WHERE planspec_source = ?",
            (source,),
        ).fetchall()
    except sqlite3.Error:
        return (task_id,)
    current_workspace = _workspace_identity(workspace)
    current_branch = _branch_identity(workspace)
    members: set[str] = set()
    for row in rows:
        member_workspace = str(row["workspace_path"] or "").strip()
        if (
            str(row["status"] or "").strip().lower() in _TERMINAL
            or (
                str(row["kind"] or "").strip().lower() != "code"
                and str(row["assignee"] or "").strip().lower()
                not in {"coder", "coder-frontend", "premium", "programmer"}
            )
            or not member_workspace
            or _workspace_identity(member_workspace) != current_workspace
            or _repo_identity(member_workspace) != repo_identity
            or _branch_identity(member_workspace) != current_branch
        ):
            continue
        members.add(str(row["id"]))
    members.add(task_id)
    return tuple(sorted(members))


def _legacy_defer_baseline(
    conn: sqlite3.Connection,
    *,
    task_id: str,
    source: str,
    repo_identity: str,
    workspace: str,
    candidate: str,
) -> _LegacyDeferResolution:
    try:
        rows = conn.execute(
            "SELECT e.payload FROM task_events e "
            "JOIN tasks t ON t.id = e.task_id "
            "WHERE t.planspec_source = ? "
            "AND e.kind = 'review_deferred_to_tip' "
            "ORDER BY e.id DESC",
            (source,),
        ).fetchall()
    except sqlite3.Error:
        return _LegacyDeferResolution(
            required=True,
            error="legacy_defer_scan_failed",
        )
    workspace_identity = _workspace_identity(workspace)
    branch_identity = _branch_identity(workspace)
    for row in rows:
        try:
            payload = json.loads(row["payload"] or "{}")
        except (TypeError, ValueError):
            continue
        if not isinstance(payload, dict):
            continue
        members = payload.get("review_cohort_member_ids")
        stamped_repo = str(payload.get("review_repo_identity") or "")
        stamped_workspace = str(payload.get("review_workspace_identity") or "")
        stamped_branch = str(payload.get("review_branch_ref") or "")
        baseline = str(payload.get("diff_base_commit") or "").strip()
        deferred_candidate = str(
            payload.get("diff_candidate_commit") or ""
        ).strip()
        if (
            payload.get("review_chain_kind") != "legacy_active"
            or not isinstance(members, list)
            or task_id not in members
        ):
            continue
        # Only newly stamped events participate. Old events have insufficient
        # provenance and cannot safely authorize accumulated review.
        if not all(
            (
                stamped_repo,
                stamped_workspace,
                stamped_branch,
                baseline,
                deferred_candidate,
            )
        ):
            continue
        if (
            stamped_repo != repo_identity
            or stamped_workspace != workspace_identity
            or stamped_branch != branch_identity
        ):
            return _LegacyDeferResolution(
                required=True,
                error="legacy_defer_identity_mismatch",
            )
        if not _git_succeeds(
            workspace, "merge-base", "--is-ancestor", baseline, candidate
        ):
            return _LegacyDeferResolution(
                required=True,
                error="legacy_defer_baseline_not_ancestor",
            )
        if not _git_succeeds(
            workspace,
            "merge-base",
            "--is-ancestor",
            deferred_candidate,
            candidate,
        ):
            return _LegacyDeferResolution(
                required=True,
                error="legacy_defer_candidate_not_ancestor",
            )
        return _LegacyDeferResolution(
            required=True,
            baseline=baseline,
            members=tuple(sorted(str(member) for member in members)),
        )
    return _LegacyDeferResolution()


def _path_state_digest(
    workspace: str,
    decision: PathRiskDecision,
) -> Optional[str]:
    """Hash current sensitive-file state without retaining file contents."""
    if decision.tier == "standard" or not decision.matches:
        return None
    digest = hashlib.sha256()
    root = Path(workspace)
    try:
        for match in decision.matches:
            path = root / match.path
            digest.update(match.path.encode("utf-8", errors="surrogateescape"))
            digest.update(b"\0")
            try:
                stat_result = path.lstat()
            except OSError:
                digest.update(b"missing\0")
                continue
            digest.update(str(stat_result.st_mode).encode("ascii"))
            digest.update(b"\0")
            if path.is_symlink():
                digest.update(
                    os.readlink(path).encode("utf-8", errors="surrogateescape")
                )
            elif path.is_file():
                with path.open("rb") as stream:
                    while chunk := stream.read(1024 * 1024):
                        digest.update(chunk)
            else:
                digest.update(b"non-regular")
            digest.update(b"\0")
    except (OSError, RuntimeError, ValueError):
        return None
    return digest.hexdigest()


def build_review_context(
    conn: sqlite3.Connection,
    task_id: str,
    *,
    run_id: Optional[int] = None,
) -> ReviewDecisionContext:
    """Collect one uncapped path set for an entire completion decision."""
    try:
        row = conn.execute(
            "SELECT id, workspace_path, current_run_id, planspec_source "
            "FROM tasks WHERE id = ?",
            (task_id,),
        ).fetchone()
    except sqlite3.Error:
        row = None
    workspace = str(row["workspace_path"] or "").strip() if row else ""
    source = str(row["planspec_source"] or "").strip() if row else ""
    if row is None or not workspace:
        return ReviewDecisionContext(
            workspace_path=workspace,
            run_id=run_id,
            planspec_source=source,
            evidence_error="workspace_unavailable",
        )

    inside = _git(workspace, "rev-parse", "--is-inside-work-tree")
    candidate_out = _git(workspace, "rev-parse", "--verify", "HEAD^{commit}")
    candidate = candidate_out.strip() if candidate_out else None
    repo_identity = _repo_identity(workspace)
    if inside is None or inside.strip() != "true" or not candidate or not repo_identity:
        return ReviewDecisionContext(
            workspace_path=workspace,
            repo_identity=repo_identity,
            run_id=run_id,
            candidate_commit=candidate,
            planspec_source=source,
            evidence_error="git_identity_unavailable",
        )

    root_id, linked_members, target_ref = _linked_chain(conn, task_id)
    chain_kind = "linked" if root_id else "solo"
    members = linked_members
    target_head: Optional[str] = None
    chain_accumulated = False
    accumulated_required = False
    baseline: Optional[str] = None
    baseline_kind = "pre_run_commit_sha"

    if root_id:
        accumulated_required = _prior_linked_defer(conn, task_id, members)
        if not target_ref:
            return ReviewDecisionContext(
                workspace,
                repo_identity,
                run_id,
                candidate_commit=candidate,
                chain_root_id=root_id,
                chain_kind=chain_kind,
                member_ids=members,
                planspec_source=source,
                evidence_error="frozen_merge_target_missing",
                accumulated_required=accumulated_required,
            )
        target_out = _git(
            workspace, "rev-parse", "--verify", f"{target_ref}^{{commit}}"
        )
        target_head = target_out.strip() if target_out else None
        if not target_head:
            return ReviewDecisionContext(
                workspace,
                repo_identity,
                run_id,
                candidate_commit=candidate,
                target_ref=target_ref,
                chain_root_id=root_id,
                chain_kind=chain_kind,
                member_ids=members,
                planspec_source=source,
                evidence_error="frozen_merge_target_unresolvable",
                accumulated_required=accumulated_required,
            )
        if accumulated_required:
            merge_base = _git(
                workspace, "merge-base", target_head, candidate
            )
            baseline = merge_base.strip() if merge_base else None
            baseline_kind = "chain_merge_target"
            chain_accumulated = True
            if not baseline:
                return ReviewDecisionContext(
                    workspace,
                    repo_identity,
                    run_id,
                    candidate_commit=candidate,
                    target_ref=target_ref,
                    target_head=target_head,
                    chain_root_id=root_id,
                    chain_kind=chain_kind,
                    member_ids=members,
                    planspec_source=source,
                    evidence_error="merge_base_unavailable",
                    accumulated_required=True,
                )
    elif source:
        legacy = _legacy_defer_baseline(
            conn,
            task_id=task_id,
            source=source,
            repo_identity=repo_identity,
            workspace=workspace,
            candidate=candidate,
        )
        accumulated_required = legacy.required
        if legacy.error:
            return ReviewDecisionContext(
                workspace,
                repo_identity,
                run_id,
                candidate_commit=candidate,
                chain_kind="legacy_stamped",
                member_ids=legacy.members,
                planspec_source=source,
                evidence_error=legacy.error,
                accumulated_required=True,
            )
        if legacy.baseline is not None:
            baseline, members = legacy.baseline, legacy.members
            baseline_kind = "legacy_deferred_cohort"
            chain_kind = "legacy_stamped"
            chain_accumulated = True
        else:
            members = _active_legacy_cohort(
                conn,
                task_id,
                source,
                workspace=workspace,
                repo_identity=repo_identity,
            )
            if len(members) > 1:
                chain_kind = "legacy_active"

    selected_run = run_id
    if selected_run is None and row["current_run_id"] is not None:
        try:
            selected_run = int(row["current_run_id"])
        except (TypeError, ValueError):
            selected_run = None
    if baseline is None:
        baseline = _task_run_baseline(conn, row, selected_run)
        if baseline:
            baseline, baseline_kind = _same_tree_parent_fallback(
                workspace, baseline, candidate
            )
    pre_clamp_baseline = baseline
    baseline_clamped = False
    clamp_target_ref: Optional[str] = None
    if baseline:
        baseline, baseline_clamped, clamp_target_ref = _clamp_to_fork_point(
            conn,
            task_id,
            workspace=workspace,
            baseline=baseline,
            baseline_kind=baseline_kind,
            candidate=candidate,
        )
    if (
        not baseline
        or not _git_succeeds(
            workspace, "merge-base", "--is-ancestor", baseline, candidate
        )
    ):
        return ReviewDecisionContext(
            workspace,
            repo_identity,
            selected_run,
            baseline_commit=baseline,
            baseline_kind=baseline_kind,
            candidate_commit=candidate,
            target_ref=target_ref,
            target_head=target_head,
            chain_root_id=root_id,
            chain_kind=chain_kind,
            member_ids=members,
            planspec_source=source,
            chain_accumulated=chain_accumulated,
            evidence_error="baseline_unavailable",
            accumulated_required=accumulated_required,
            baseline_clamped_to_fork_point=baseline_clamped,
            pre_clamp_baseline_commit=(
                pre_clamp_baseline if baseline_clamped else None
            ),
            clamp_target_ref=clamp_target_ref,
        )

    changed_raw = _git(
        workspace,
        "diff",
        "--name-only",
        "--no-renames",
        "-z",
        baseline,
    )
    untracked_raw = _git(
        workspace, "ls-files", "--others", "--exclude-standard", "-z"
    )
    if changed_raw is None or untracked_raw is None:
        return ReviewDecisionContext(
            workspace,
            repo_identity,
            selected_run,
            baseline,
            baseline_kind,
            candidate,
            target_ref,
            target_head,
            root_id,
            chain_kind,
            members,
            source,
            chain_accumulated=chain_accumulated,
            evidence_error="changed_path_scan_failed",
            accumulated_required=accumulated_required,
            baseline_clamped_to_fork_point=baseline_clamped,
            pre_clamp_baseline_commit=(
                pre_clamp_baseline if baseline_clamped else None
            ),
            clamp_target_ref=clamp_target_ref,
        )
    changed = tuple(
        sorted(
            {
                normalized
                for path in (changed_raw + untracked_raw).split("\0")
                if (normalized := normalize_repo_path(path))
            }
        )
    )
    path_risk = classify_changed_paths(changed)
    return ReviewDecisionContext(
        workspace,
        repo_identity,
        selected_run,
        baseline,
        baseline_kind,
        candidate,
        target_ref,
        target_head,
        root_id,
        chain_kind,
        members,
        source,
        changed,
        True,
        chain_accumulated,
        accumulated_required=accumulated_required,
        path_state_digest=_path_state_digest(workspace, path_risk),
        baseline_clamped_to_fork_point=baseline_clamped,
        pre_clamp_baseline_commit=(
            pre_clamp_baseline if baseline_clamped else None
        ),
        clamp_target_ref=clamp_target_ref,
    )


def _risk_digest(
    context: ReviewDecisionContext,
    decision: PathRiskDecision,
) -> Optional[str]:
    if (
        decision.tier == "standard"
        or not decision.matches
        or not context.path_state_digest
    ):
        return None
    material = {
        "baseline_commit": context.baseline_commit,
        "candidate_commit": context.candidate_commit,
        "path_state_digest": context.path_state_digest,
        "matches": [
            {"path": match.path, "rule_id": match.rule_id, "tier": match.tier}
            for match in decision.matches
        ],
    }
    encoded = json.dumps(
        material,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def path_risk_requires_gate(
    conn: sqlite3.Connection,
    task_id: str,
    *,
    context: Optional[ReviewDecisionContext] = None,
    fail_closed: bool = False,
) -> bool:
    observed = context or build_review_context(conn, task_id)
    if not observed.evidence_complete:
        return bool(fail_closed and observed.workspace_path)
    return observed.path_risk.tier != "standard"


def _latest_ack_payload(conn: sqlite3.Connection, task_id: str) -> dict:
    try:
        row = conn.execute(
            "SELECT payload FROM task_events WHERE task_id = ? "
            "AND kind = 'review_tier_downgrade_ack' ORDER BY id DESC LIMIT 1",
            (task_id,),
        ).fetchone()
    except sqlite3.Error:
        return {}
    if row is None or not row["payload"]:
        return {}
    try:
        payload = json.loads(row["payload"])
    except (TypeError, ValueError):
        return {}
    return payload if isinstance(payload, dict) else {}


def resolve_effective_review_tier(
    conn: sqlite3.Connection,
    task_id: str,
    *,
    prose_floor: str,
    explicit_tier: object,
    path_floor_enabled: bool,
    legacy_ack: Callable[[sqlite3.Connection, str, str], bool],
    context: Optional[ReviewDecisionContext] = None,
) -> str:
    """Union prose/path floors while preserving explicit upgrades."""
    observed = context or build_review_context(conn, task_id)
    decision = observed.path_risk
    path_floor = (
        decision.tier
        if path_floor_enabled and observed.evidence_complete
        else "standard"
    )
    floor = max(
        (prose_floor, path_floor),
        key=lambda tier: TIER_ORDER.get(tier, 0),
    )
    explicit = str(explicit_tier or "").strip().lower()
    if explicit not in TIER_ORDER:
        return floor
    if TIER_ORDER[explicit] >= TIER_ORDER[floor]:
        return explicit

    if TIER_ORDER[explicit] < TIER_ORDER[path_floor]:
        digest = _risk_digest(observed, decision)
        ack = _latest_ack_payload(conn, task_id)
        if (
            not digest
            or str(ack.get("to_tier") or "").strip().lower() != explicit
            or ack.get("path_risk_digest") != digest
            or ack.get("diff_candidate_commit") != observed.candidate_commit
        ):
            return floor
    if (
        TIER_ORDER[explicit] < TIER_ORDER.get(prose_floor, 0)
        and not legacy_ack(conn, task_id, explicit)
    ):
        return floor
    return explicit


def downgrade_ack_payload(
    conn: sqlite3.Connection,
    task_id: str,
    target_tier: str,
) -> dict:
    """Bind a path-floor downgrade to the observed candidate and paths."""
    payload: dict = {"to_tier": target_tier}
    context = build_review_context(conn, task_id)
    decision = context.path_risk
    digest = _risk_digest(context, decision)
    if context.evidence_complete and decision.tier != "standard" and digest:
        payload.update(
            {
                "diff_candidate_commit": context.candidate_commit,
                "path_risk_digest": digest,
                "path_rule_ids": list(decision.rule_ids),
            }
        )
    return payload


def defer_event_payload(
    context: ReviewDecisionContext,
    worker_gate: dict,
) -> dict:
    """Persist enough cohort/baseline provenance for a safe future tip."""
    payload = {
        "worker_gate": worker_gate,
        "review_chain_kind": context.chain_kind,
        "review_chain_root_id": context.chain_root_id,
        "review_cohort_member_ids": list(context.member_ids),
        "review_repo_identity": context.repo_identity,
        "review_workspace_identity": _workspace_identity(context.workspace_path),
        "review_branch_ref": _branch_identity(context.workspace_path),
        "diff_base_commit": context.baseline_commit,
        "diff_candidate_commit": context.candidate_commit,
        "diff_baseline": context.baseline_kind,
        "merge_target": context.target_ref,
        "merge_target_head": context.target_head,
    }
    if context.baseline_clamped_to_fork_point:
        payload.update(
            {
                "diff_base_clamped_to_fork_point": True,
                "diff_base_pre_clamp_commit": (
                    context.pre_clamp_baseline_commit
                ),
                "diff_clamp_target": context.clamp_target_ref,
            }
        )
    return payload


def snapshot_provenance(context: ReviewDecisionContext) -> dict:
    total = len(context.changed_files)
    provenance = {
        "changed_files_total": total,
        "changed_files_truncated": total > 200,
        "review_evidence_complete": context.evidence_complete,
        "review_chain_accumulated": context.chain_accumulated,
        "review_chain_root_id": context.chain_root_id,
        "review_repo_identity": context.repo_identity or None,
        "merge_target": context.target_ref,
        "merge_target_head": context.target_head,
    }
    if context.baseline_clamped_to_fork_point:
        provenance.update(
            {
                "diff_base_clamped_to_fork_point": True,
                "diff_base_pre_clamp_commit": (
                    context.pre_clamp_baseline_commit
                ),
                "diff_clamp_target": context.clamp_target_ref,
            }
        )
    return provenance


def capture_review_snapshot(
    context: ReviewDecisionContext,
    conn: sqlite3.Connection,
    task_id: str,
    expected_run_id: Optional[int],
    *,
    capture: Callable[..., dict],
) -> dict:
    """Render bounded evidence from the already-collected decision context."""
    if not context.evidence_complete or not context.baseline_commit:
        if context.accumulated_required:
            raise ReviewEvidenceError(
                "accumulated review evidence unavailable: "
                f"{context.evidence_error or 'unknown error'}"
            )
        return capture(conn, task_id, expected_run_id)
    return capture(
        conn,
        task_id,
        expected_run_id,
        baseline_commit=context.baseline_commit,
        baseline_kind=context.baseline_kind,
        changed_files_override=context.changed_files,
        snapshot_extra=snapshot_provenance(context),
    )
