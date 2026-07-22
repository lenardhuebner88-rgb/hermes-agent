"""Operator-only terminal candidate intake endpoint."""

from typing import Optional

from pydantic import BaseModel, Field

from hermes_cli.config import load_config_readonly
from hermes_cli.terminal_candidates import (
    CandidateBusyError,
    CandidateImportError,
    CandidatePreflightError,
    submit_terminal_candidate,
)
from hermes_constants import get_hermes_home, terminal_runs_root


class TerminalCandidateSubmitRequest(BaseModel):
    terminal_run_id: str = Field(min_length=1, max_length=200)
    correlation_id: Optional[str] = Field(default=None, min_length=1, max_length=300)
    candidate_sha: Optional[str] = Field(default=None, min_length=40, max_length=40)
    board: Optional[str] = None


@planspec_routes.post("/terminal-candidates/submit")
def submit_terminal_candidate_route(body: TerminalCandidateSubmitRequest):
    config = load_config_readonly()
    raw = ((config.get("kanban") or {}).get("candidate_submit") or {})
    if not isinstance(raw, dict) or not raw.get("enabled", False):
        raise HTTPException(status_code=403, detail="terminal candidate submit is disabled")
    allowlist = raw.get("repo_allowlist") or []
    if not isinstance(allowlist, list):
        raise HTTPException(status_code=500, detail="candidate repo allowlist is invalid")
    try:
        with _conn(_resolve_board(body.board)) as conn:
            result = submit_terminal_candidate(
                conn, terminal_run_id=body.terminal_run_id,
                correlation_id=body.correlation_id,
                candidate_sha=body.candidate_sha,
                terminal_runs_dir=terminal_runs_root(get_hermes_home()),
                repo_allowlist=allowlist, enabled=True,
                intake_assignee=str(raw.get("intake_assignee") or "coder"),
            )
    except CandidateBusyError as exc:
        raise HTTPException(
            status_code=409,
            detail={"message": str(exc), "retry": True},
            headers={"Retry-After": "1"},
        ) from exc
    except CandidatePreflightError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    except CandidateImportError as exc:
        raise HTTPException(
            status_code=409 if not exc.recovery_required else 500,
            detail={"message": str(exc), "recovery_required": exc.recovery_required},
        ) from exc
    return {
        "ok": True, "root_task_id": result.root_task_id,
        "intake_task_id": result.intake_task_id,
        "source_commit": result.source_commit,
        "imported_commit": result.imported_commit,
        "workspace_path": result.workspace_path,
        "idempotent": result.idempotent,
    }


__all__ = ["TerminalCandidateSubmitRequest", "submit_terminal_candidate_route"]
