"""Import a terminal isolated-write commit range into a held Kanban chain.

This module is intentionally an adapter: it prepares a normal provisioned
Kanban chain and never calls the integrator or writes the live target branch.
"""

from __future__ import annotations

from dataclasses import dataclass
import hashlib
import json
import os
from pathlib import Path
import re
import subprocess
from typing import Any, Iterable, Optional

from hermes_cli import kanban_db as kb
from hermes_cli import kanban_worktrees as worktrees


_RUN_ID_RE = re.compile(r"^[A-Za-z0-9._-]+$")
_SHA_RE = re.compile(r"^[0-9a-f]{40}$")
_GIT_TIMEOUT_SECONDS = 30


class CandidateError(RuntimeError):
    """Base class for candidate intake failures."""


class CandidatePreflightError(CandidateError):
    """The source is unsafe or no longer matches its terminal manifest."""


class CandidateBusyError(CandidateError):
    """The repository integrator lock is currently held."""


class CandidateImportError(CandidateError):
    def __init__(self, message: str, *, recovery_required: bool = False):
        super().__init__(message)
        self.recovery_required = recovery_required


@dataclass(frozen=True)
class CandidatePreflight:
    terminal_run_id: str
    correlation_id: str
    repo_root: Path
    source_worktree: Path
    branch: str
    base_sha: str
    candidate_sha: str
    commits: tuple[str, ...]
    manifest_sha256: str


@dataclass(frozen=True)
class CandidateSubmitResult:
    root_task_id: str
    intake_task_id: str
    source_commit: str
    imported_commit: str
    workspace_path: str
    idempotent: bool = False


def _git(path: Path, *args: str, check: bool = True) -> subprocess.CompletedProcess[str]:
    try:
        proc = subprocess.run(
            ["git", *args], cwd=path, text=True, capture_output=True,
            timeout=_GIT_TIMEOUT_SECONDS, check=False,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise CandidatePreflightError(f"git {' '.join(args)} failed: {exc}") from exc
    if check and proc.returncode != 0:
        detail = (proc.stderr or proc.stdout).strip()[-500:]
        raise CandidatePreflightError(f"git {' '.join(args)} failed: {detail}")
    return proc


def _read_manifest(terminal_runs_dir: Path, terminal_run_id: str) -> tuple[dict, str]:
    if not _RUN_ID_RE.fullmatch(terminal_run_id or ""):
        raise CandidatePreflightError("invalid terminal run id")
    runs_root = Path(terminal_runs_dir).expanduser().resolve()
    manifest_path = (runs_root / terminal_run_id / "manifest.json").resolve()
    if manifest_path.parent.parent != runs_root:
        raise CandidatePreflightError("terminal manifest escapes the server run root")
    try:
        raw = manifest_path.read_bytes()
        manifest = json.loads(raw)
    except (OSError, json.JSONDecodeError) as exc:
        raise CandidatePreflightError("unknown terminal run or invalid manifest") from exc
    if not isinstance(manifest, dict) or manifest.get("run_id") != terminal_run_id:
        raise CandidatePreflightError("terminal run manifest identity mismatch")
    return manifest, hashlib.sha256(raw).hexdigest()


def _canonical_allowlist(values: Iterable[str | Path]) -> set[Path]:
    result: set[Path] = set()
    for value in values:
        try:
            path = Path(value).expanduser().resolve(strict=True)
        except (OSError, TypeError, ValueError):
            continue
        if path.is_dir():
            result.add(path)
    return result


def preflight_terminal_candidate(
    *,
    terminal_run_id: str,
    correlation_id: Optional[str],
    candidate_sha: Optional[str],
    terminal_runs_dir: Path,
    repo_allowlist: Iterable[str | Path],
) -> CandidatePreflight:
    """Pure read-only validation. No board, worktree, ref, or file mutation."""
    manifest, manifest_digest = _read_manifest(terminal_runs_dir, terminal_run_id)
    manifest_correlation = str(manifest.get("correlation_id") or "").strip()
    if not manifest_correlation or (correlation_id and manifest_correlation != correlation_id):
        raise CandidatePreflightError("terminal correlation id mismatch")
    if manifest.get("start_mode") != "isolated_write":
        raise CandidatePreflightError("only isolated-write terminal runs may submit")

    try:
        repo_root = Path(str(manifest["repo_root"])).expanduser().resolve(strict=True)
        source = Path(str(manifest["worktree_path"])).expanduser().resolve(strict=True)
    except (KeyError, OSError, ValueError) as exc:
        raise CandidatePreflightError("manifest repo/worktree path is invalid") from exc
    if repo_root not in _canonical_allowlist(repo_allowlist):
        raise CandidatePreflightError("candidate repo is not in the server allowlist")
    expected_parent = (repo_root / ".worktrees" / "terminal").resolve()
    if source.parent != expected_parent or not source.is_dir():
        raise CandidatePreflightError("source is not a canonical terminal worktree")
    common = _git(source, "rev-parse", "--path-format=absolute", "--git-common-dir").stdout.strip()
    if Path(common).resolve() != (repo_root / ".git").resolve():
        raise CandidatePreflightError("source worktree belongs to a foreign repository")

    status = _git(source, "status", "--porcelain=v1", "--untracked-files=all").stdout
    if status:
        raise CandidatePreflightError("candidate worktree must be clean (including untracked files)")
    branch_proc = _git(source, "symbolic-ref", "--quiet", "--short", "HEAD", check=False)
    branch = branch_proc.stdout.strip()
    expected_branch = str(manifest.get("branch") or "").strip()
    if branch_proc.returncode != 0 or not branch or branch != expected_branch:
        raise CandidatePreflightError("candidate branch is detached or differs from the manifest")
    base_sha = str(manifest.get("base_sha") or "").strip().lower()
    if not _SHA_RE.fullmatch(base_sha):
        raise CandidatePreflightError("manifest base SHA is missing or invalid")
    resolved_base = _git(source, "rev-parse", "--verify", f"{base_sha}^{{commit}}").stdout.strip()
    if resolved_base != base_sha:
        raise CandidatePreflightError("manifest base SHA no longer resolves exactly")
    head = _git(source, "rev-parse", "HEAD").stdout.strip()
    requested = str(candidate_sha or head).strip().lower()
    if not _SHA_RE.fullmatch(requested) or requested != head:
        raise CandidatePreflightError("candidate commit must be the current source HEAD")
    if _git(source, "merge-base", "--is-ancestor", base_sha, head, check=False).returncode != 0:
        raise CandidatePreflightError("candidate commit is not a descendant of the stored base")
    lines = [
        line.split()
        for line in _git(source, "rev-list", "--reverse", "--parents", f"{base_sha}..{head}").stdout.splitlines()
        if line.strip()
    ]
    if not lines:
        raise CandidatePreflightError("terminal run has no candidate commit")
    if any(len(parts) != 2 for parts in lines):
        raise CandidatePreflightError("candidate range contains a merge commit")
    previous = base_sha
    for commit, parent in lines:
        if parent != previous:
            raise CandidatePreflightError("candidate range is not linear")
        previous = commit
    if previous != head:
        raise CandidatePreflightError("candidate range does not terminate at source HEAD")
    return CandidatePreflight(
        terminal_run_id=terminal_run_id,
        correlation_id=manifest_correlation,
        repo_root=repo_root,
        source_worktree=source,
        branch=branch,
        base_sha=base_sha,
        candidate_sha=head,
        commits=tuple(parts[0] for parts in lines),
        manifest_sha256=manifest_digest,
    )


def _event_payload(conn, task_id: str, kind: str) -> Optional[dict[str, Any]]:
    row = conn.execute(
        "SELECT payload FROM task_events WHERE task_id=? AND kind=? ORDER BY id DESC LIMIT 1",
        (task_id, kind),
    ).fetchone()
    if row is None:
        return None
    try:
        value = json.loads(row["payload"] or "{}")
    except (TypeError, json.JSONDecodeError):
        return None
    return value if isinstance(value, dict) else None


def _existing_intake(conn, root_id: str) -> Optional[str]:
    row = conn.execute(
        "SELECT l.parent_id FROM task_links l JOIN tasks t ON t.id=l.parent_id "
        "WHERE l.child_id=? AND t.kind='code' ORDER BY t.created_at, t.id LIMIT 1",
        (root_id,),
    ).fetchone()
    return str(row["parent_id"]) if row else None



def _create_or_resolve_chain(conn, preflight: CandidatePreflight, *, intake_assignee: str) -> tuple[str, str]:
    idempotency_key = f"terminal-candidate:{preflight.correlation_id}:{preflight.candidate_sha}"
    prior = conn.execute(
        "SELECT id FROM tasks WHERE idempotency_key=? LIMIT 1",
        (idempotency_key,),
    ).fetchone()
    root_id = str(prior["id"]) if prior else kb.create_held_decompose_root(
        conn,
        title=f"Terminal candidate {preflight.candidate_sha[:12]}",
        body="Held terminal candidate chain. Release only after operator inspection.",
        assignee=None,
        created_by="terminal-candidate-submit",
        workspace_kind="worktree",
        workspace_path=str(preflight.repo_root),
        branch_name=worktrees.chain_branch("pending"),
        idempotency_key=idempotency_key,
        kind="analysis",
        freigabe="operator",
        live_test_depth="contract",
        hold_reason="Terminal candidate: held before release",
        root_kind="terminal_candidate",
        correlation_id=preflight.correlation_id,
        artifact_digest=preflight.manifest_sha256,
        initial_event_kind="terminal_candidate_pending",
        initial_event_payload={
            "schema_version": 1,
            "correlation_id": preflight.correlation_id,
            "terminal_run_id": preflight.terminal_run_id,
            "source_commit": preflight.candidate_sha,
            "repo_root": str(preflight.repo_root),
        },
    )
    pending = _event_payload(conn, root_id, "terminal_candidate_pending")
    expected_identity = {
        "correlation_id": preflight.correlation_id,
        "terminal_run_id": preflight.terminal_run_id,
        "source_commit": preflight.candidate_sha,
        "repo_root": str(preflight.repo_root),
    }
    if pending is None or any(pending.get(key) != value for key, value in expected_identity.items()):
        raise CandidateImportError("idempotent candidate root identity does not match the source")

    intake_id = _existing_intake(conn, root_id)
    if intake_id is None:
        children = [{
            "title": f"Import and validate terminal candidate {preflight.candidate_sha[:12]}",
            "body": (
                "Candidate Intake: inspect the already imported chain HEAD, fix issues if needed, "
                "then complete with structured metadata containing commit and workspace_path. "
                "Do not call the integrator directly."
            ),
            "assignee": intake_assignee,
            "kind": "code",
            "workspace_kind": "worktree",
            "workspace_path": str(preflight.repo_root),
            "branch_name": worktrees.chain_branch(root_id),
        }]
        ids = kb.decompose_triage_task(
            conn,
            root_id,
            root_assignee=None,
            children=children,
            validate_assignees=True,
            expected_root_status="scheduled",
            initial_child_status="scheduled",
            auto_promote=False,
        )
        intake_id = ids[0]
        kb.add_event(conn, intake_id, "terminal_candidate_intake", {
            "schema_version": 1,
            "root_task_id": root_id,
            "source_commit": preflight.candidate_sha,
            "repo_root": str(preflight.repo_root),
        })
    task = kb.get_task(conn, intake_id)
    if task is None:
        raise CandidateImportError("candidate intake task disappeared")
    workspace = Path(worktrees.provision_candidate_intake(conn, task, preflight.repo_root))
    worktrees.pin_candidate_chain_workspace(
        conn, root_id, preflight.repo_root, workspace, task_ids=(intake_id,),
    )
    return root_id, intake_id


def _abort_and_verify(workspace: Path, original_head: str) -> bool:
    _git(workspace, "cherry-pick", "--abort", check=False)
    head = _git(workspace, "rev-parse", "HEAD", check=False).stdout.strip()
    clean = not _git(workspace, "status", "--porcelain=v1", "--untracked-files=all", check=False).stdout
    marker = _git(workspace, "rev-parse", "--verify", "CHERRY_PICK_HEAD", check=False)
    return head == original_head and clean and marker.returncode != 0


def _attach_capsule(conn, root_id: str, runs_dir: Path, preflight: CandidatePreflight, payload: dict) -> None:
    raw = (json.dumps(payload, sort_keys=True, indent=2) + "\n").encode("utf-8")
    digest = hashlib.sha256(raw).hexdigest()
    artifact_dir = Path(runs_dir) / preflight.terminal_run_id / "artifacts"
    artifact_dir.mkdir(parents=True, exist_ok=True)
    path = artifact_dir / f"candidate-submit-{preflight.candidate_sha}.json"
    if path.exists():
        if path.read_bytes() != raw:
            raise CandidateImportError("candidate capsule already exists with different content")
    else:
        fd = os.open(path, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
        try:
            os.write(fd, raw)
        finally:
            os.close(fd)
    os.chmod(path, 0o600)
    kb.add_immutable_handoff_attachment(
        conn,
        root_id,
        source_path=path,
        sha256=digest,
        size=len(raw),
        artifact_kind="terminal_candidate_capsule",
        filename=path.name,
        content_type="application/json",
        uploaded_by="terminal_candidate_submit",
    )


def submit_terminal_candidate(
    conn,
    *,
    terminal_run_id: str,
    correlation_id: Optional[str],
    candidate_sha: Optional[str],
    terminal_runs_dir: Path,
    repo_allowlist: Iterable[str | Path],
    enabled: bool,
    intake_assignee: str = "coder",
) -> CandidateSubmitResult:
    if not enabled:
        raise CandidatePreflightError("terminal candidate submit is disabled")
    first = preflight_terminal_candidate(
        terminal_run_id=terminal_run_id, correlation_id=correlation_id,
        candidate_sha=candidate_sha, terminal_runs_dir=terminal_runs_dir,
        repo_allowlist=repo_allowlist,
    )
    lock = worktrees._try_acquire_file_lock(worktrees._integrator_lock_path(first.repo_root))
    if lock is None:
        raise CandidateBusyError("repository is busy; retry candidate submit later")
    try:
        current = preflight_terminal_candidate(
            terminal_run_id=terminal_run_id, correlation_id=correlation_id,
            candidate_sha=first.candidate_sha, terminal_runs_dir=terminal_runs_dir,
            repo_allowlist=repo_allowlist,
        )
        if current != first:
            raise CandidatePreflightError("candidate source or manifest changed during submit")
        root_id, intake_id = _create_or_resolve_chain(
            conn, current, intake_assignee=intake_assignee,
        )
        task = kb.get_task(conn, intake_id)
        workspace = Path(task.workspace_path).resolve()
        imported = _event_payload(conn, root_id, "terminal_candidate_imported")
        if imported and imported.get("source_commit") == current.candidate_sha:
            _attach_capsule(conn, root_id, terminal_runs_dir, current, imported)
            return CandidateSubmitResult(
                root_id, intake_id, current.candidate_sha,
                str(imported["imported_commit"]), str(workspace), True,
            )
        chain_head = _git(workspace, "rev-parse", "HEAD").stdout.strip()
        pick = _git(workspace, "cherry-pick", *current.commits, check=False)
        if pick.returncode != 0:
            recovered = _abort_and_verify(workspace, chain_head)
            kind = "terminal_candidate_import_failed" if recovered else "candidate_import_recovery_required"
            kb.add_event(conn, root_id, kind, {
                "schema_version": 1, "source_commit": current.candidate_sha,
                "chain_head_before_import": chain_head,
                "recovered": recovered,
            })
            detail = (pick.stderr or pick.stdout).strip()[-500:]
            raise CandidateImportError(
                f"candidate cherry-pick failed: {detail}", recovery_required=not recovered,
            )
        imported_sha = _git(workspace, "rev-parse", "HEAD").stdout.strip()
        payload = {
            "schema_version": 1,
            "correlation_id": current.correlation_id,
            "terminal_run_id": current.terminal_run_id,
            "source_base": current.base_sha,
            "source_commit": current.candidate_sha,
            "imported_commit": imported_sha,
            "chain_head_before_import": chain_head,
            "workspace_path": str(workspace),
            "repo_root": str(current.repo_root),
        }
        _attach_capsule(conn, root_id, terminal_runs_dir, current, payload)
        kb.add_event(conn, root_id, "terminal_candidate_imported", payload)
        kb.add_event(conn, intake_id, "terminal_candidate_imported", payload)
        return CandidateSubmitResult(
            root_id, intake_id, current.candidate_sha, imported_sha, str(workspace), False,
        )
    finally:
        worktrees._release_file_lock(lock)


__all__ = [
    "CandidateBusyError", "CandidateError", "CandidateImportError",
    "CandidatePreflight", "CandidatePreflightError", "CandidateSubmitResult",
    "preflight_terminal_candidate", "submit_terminal_candidate",
]
