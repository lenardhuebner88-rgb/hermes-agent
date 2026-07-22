"""Durable Kanban↔TMAX execution-capsule domain contracts."""

from __future__ import annotations

import json
import subprocess
from pathlib import Path

import pytest

from hermes_cli import kanban_db as kb


@pytest.fixture
def capsule_env(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    home = tmp_path / ".hermes"
    home.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(home))
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    repo = tmp_path / "repo"
    repo.mkdir()
    subprocess.run(["git", "init", "-b", "main"], cwd=repo, check=True, capture_output=True)
    subprocess.run(["git", "config", "user.email", "test@example.invalid"], cwd=repo, check=True)
    subprocess.run(["git", "config", "user.name", "Test"], cwd=repo, check=True)
    (repo / "README.md").write_text("base\n", encoding="utf-8")
    subprocess.run(["git", "add", "README.md"], cwd=repo, check=True)
    subprocess.run(["git", "commit", "-m", "base"], cwd=repo, check=True, capture_output=True)
    kb.init_db()
    return home, repo


def _active_run(conn, repo: Path, *, title: str = "capsule task") -> tuple[str, int]:
    task_id = kb.create_task(
        conn,
        title=title,
        workspace_kind="worktree",
        workspace_path=str(repo),
        branch_name="main",
    )
    task = kb.claim_task(conn, task_id, claimer="test:1")
    assert task is not None and task.current_run_id is not None
    return task_id, int(task.current_run_id)


def _context(summary: str = "Continue the verified implementation") -> dict:
    return {
        "profile": "implementation",
        "summary": summary,
        "decisions": ["Keep task_runs as durable owner"],
        "next_steps": ["Run the targeted gate"],
        "risks": ["Do not mutate the live checkout"],
    }


def _begin(conn, repo: Path, task_id: str, run_id: int, **overrides):
    args = {
        "task_id": task_id,
        "run_id": run_id,
        "terminal_server_id": "a" * 64,
        "terminal_session": "work",
        "terminal_window": "codex",
        "pane_id": "%7",
        "terminal_cwd": str(repo),
        "context_handoff": _context(),
        "now": 1_800_000_000,
    }
    args.update(overrides)
    return kb.begin_execution_capsule_binding(conn, **args)


def test_capsule_pending_active_idempotent_and_run_deserialization(capsule_env):
    _home, repo = capsule_env
    with kb.connect_closing() as conn:
        task_id, run_id = _active_run(conn, repo)
        pending = _begin(conn, repo, task_id, run_id)
        assert pending["state"] == "pending"
        assert pending["task_id"] == task_id
        assert pending["run_id"] == run_id
        assert pending["terminal"]["pane_id"] == "%7"
        assert pending["workspace"]["path"] == str(repo)
        assert pending["workspace"]["branch"] == "main"
        assert pending["workspace"]["head_sha"]
        assert pending["workspace"]["pre_run_commit_sha"]

        # A transport retry before tmux activation reuses the same pending row.
        assert _begin(conn, repo, task_id, run_id) == pending
        active = kb.activate_execution_capsule(
            conn,
            task_id=task_id,
            run_id=run_id,
            correlation_id=pending["correlation_id"],
            now=1_800_000_001,
        )
        assert active["state"] == "active"
        assert kb.activate_execution_capsule(
            conn,
            task_id=task_id,
            run_id=run_id,
            correlation_id=pending["correlation_id"],
        ) == active
        run = kb.get_run(conn, run_id)
        assert run is not None
        assert run.execution_capsule == active


def test_capsule_events_never_include_context_payload(capsule_env):
    _home, repo = capsule_env
    canary = "CTX-CANARY-DO-NOT-LOG"
    with kb.connect_closing() as conn:
        task_id, run_id = _active_run(conn, repo)
        pending = _begin(
            conn,
            repo,
            task_id,
            run_id,
            context_handoff=_context(canary),
        )
        kb.activate_execution_capsule(
            conn,
            task_id=task_id,
            run_id=run_id,
            correlation_id=pending["correlation_id"],
        )
        event_payloads = "\n".join(
            row["payload"] or ""
            for row in conn.execute(
                "SELECT payload FROM task_events WHERE task_id = ?", (task_id,)
            )
        )
        assert canary not in event_payloads
        stored = conn.execute(
            "SELECT execution_capsule, metadata FROM task_runs WHERE id = ?", (run_id,)
        ).fetchone()
        assert canary in stored["execution_capsule"]
        assert canary not in (stored["metadata"] or "")


@pytest.mark.parametrize(
    "bad_context, message",
    [
        ({**_context(), "content": "raw pane"}, "unsupported field"),
        ({**_context(), "capture": "raw pane"}, "unsupported field"),
        ({**_context(), "profile": "full"}, "context profile"),
        ({**_context(), "summary": "x" * 1201}, "exceeds 1200"),
        ({**_context(), "risks": ["x"] * 9}, "at most 8"),
    ],
)
def test_capsule_context_schema_is_closed_and_bounded(bad_context, message):
    with pytest.raises(ValueError, match=message):
        kb.normalize_execution_capsule_context(bad_context)


def test_capsule_rejects_wrong_run_workspace_and_second_binding(capsule_env, tmp_path):
    _home, repo = capsule_env
    outside = tmp_path / "outside"
    outside.mkdir()
    with kb.connect_closing() as conn:
        task_id, run_id = _active_run(conn, repo)
        other_task = kb.create_task(conn, title="other")
        with pytest.raises(ValueError, match="does not belong"):
            _begin(conn, repo, other_task, run_id)
        with pytest.raises(kb.ExecutionCapsuleConflict, match="outside task workspace"):
            _begin(conn, repo, task_id, run_id, terminal_cwd=str(outside))

        pending = _begin(conn, repo, task_id, run_id)
        kb.activate_execution_capsule(
            conn,
            task_id=task_id,
            run_id=run_id,
            correlation_id=pending["correlation_id"],
        )
        with pytest.raises(kb.ExecutionCapsuleConflict, match="different execution capsule"):
            _begin(
                conn,
                repo,
                task_id,
                run_id,
                context_handoff=_context("different summary"),
            )
        active = kb.get_execution_capsule(conn, run_id)
        assert active is not None and active["state"] == "active"


def test_capsule_stale_pending_binding_is_repairable_by_rebind(capsule_env):
    """A crash between begin and activate plus a pane respawn must not wedge
    the run: a pending capsule is replaced (with an audit abort event), while
    an active capsule stays immutable history."""
    _home, repo = capsule_env
    with kb.connect_closing() as conn:
        task_id, run_id = _active_run(conn, repo)
        pending = _begin(conn, repo, task_id, run_id)
        # Pane respawn before the retry: new pane generation, new correlation.
        rebound = _begin(
            conn,
            repo,
            task_id,
            run_id,
            pane_id="%9",
            context_handoff=_context("retry after pane respawn"),
        )
        assert rebound["state"] == "pending"
        assert rebound["correlation_id"] != pending["correlation_id"]
        assert rebound["terminal"]["pane_id"] == "%9"
        assert kb.get_execution_capsule(conn, run_id) == rebound
        events = [
            dict(row)
            for row in conn.execute(
                "SELECT kind, payload FROM task_events WHERE task_id = ? "
                "AND kind = 'execution_capsule_aborted'",
                (task_id,),
            ).fetchall()
        ]
        assert len(events) == 1
        payload = json.loads(events[0]["payload"])
        assert payload["correlation_id"] == pending["correlation_id"]
        assert payload["reason"] == "superseded_by_rebind"
        # The replacement generation activates normally.
        active = kb.activate_execution_capsule(
            conn,
            task_id=task_id,
            run_id=run_id,
            correlation_id=rebound["correlation_id"],
        )
        assert active["state"] == "active"
        with pytest.raises(kb.ExecutionCapsuleConflict, match="different execution capsule"):
            _begin(conn, repo, task_id, run_id, pane_id="%11")


def test_capsule_abort_only_clears_matching_pending_generation(capsule_env):
    _home, repo = capsule_env
    with kb.connect_closing() as conn:
        task_id, run_id = _active_run(conn, repo)
        pending = _begin(conn, repo, task_id, run_id)
        assert not kb.abort_execution_capsule_binding(
            conn,
            task_id=task_id,
            run_id=run_id,
            correlation_id="0" * 24,
            reason="wrong generation",
        )
        assert kb.abort_execution_capsule_binding(
            conn,
            task_id=task_id,
            run_id=run_id,
            correlation_id=pending["correlation_id"],
            reason="tmux failed",
        )
        assert kb.get_execution_capsule(conn, run_id) is None


def test_capsule_cannot_bind_or_activate_after_run_loses_ownership(capsule_env):
    _home, repo = capsule_env
    with kb.connect_closing() as conn:
        task_id, run_id = _active_run(conn, repo)
        pending = _begin(conn, repo, task_id, run_id)
        assert kb.complete_task(conn, task_id, expected_run_id=run_id)
        with pytest.raises(kb.ExecutionCapsuleConflict, match="not the active"):
            kb.activate_execution_capsule(
                conn,
                task_id=task_id,
                run_id=run_id,
                correlation_id=pending["correlation_id"],
            )


def test_active_capsule_handoff_renders_in_bounded_prior_attempt_context(capsule_env):
    _home, repo = capsule_env
    with kb.connect_closing() as conn:
        task_id, run_id = _active_run(conn, repo)
        pending = _begin(conn, repo, task_id, run_id)
        kb.activate_execution_capsule(
            conn,
            task_id=task_id,
            run_id=run_id,
            correlation_id=pending["correlation_id"],
        )
        assert kb.complete_task(conn, task_id, expected_run_id=run_id)
        rendered = kb.build_worker_context(conn, task_id)
        assert "execution capsule handoff" in rendered
        assert "Continue the verified implementation" in rendered
        assert "Keep task_runs as durable owner" in rendered
        assert json.dumps(_context()) not in rendered
