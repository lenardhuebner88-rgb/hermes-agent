"""TDD slice cse_013: three additive hardening changes.

  #4  – _WORKER_SCOPE_LANES covers coder-claude, premium, verifier
  #3-C – default_quick_gate runs ruff diff-relative (not ruff check .)
  #3-A – worker_gate stamp injected into submitted_for_review payload
         and rendered in _render_review_verifier_section
"""
from __future__ import annotations

import json
import subprocess
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import pytest

from hermes_cli import kanban_db as kb
from hermes_cli import kanban_worktrees as kwt
from hermes_cli import kanban_decompose as decomp


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

@pytest.fixture
def kanban_home(tmp_path, monkeypatch):
    home = tmp_path / ".hermes"
    home.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(home))
    monkeypatch.delenv("HERMES_KANBAN_BOARD", raising=False)
    monkeypatch.delenv("HERMES_KANBAN_DB", raising=False)
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    kb.init_db()
    return home


def _git(repo: Path, *args: str) -> str:
    return subprocess.run(
        ["git", *args], cwd=repo,
        check=True, text=True,
        stdout=subprocess.PIPE, stderr=subprocess.PIPE,
    ).stdout.strip()


@pytest.fixture
def repo(tmp_path):
    r = tmp_path / "repo"
    r.mkdir()
    _git(r, "init", "-b", "main")
    _git(r, "config", "user.email", "t@example.com")
    _git(r, "config", "user.name", "tester")
    (r / "a.py").write_text("x = 1\n")
    _git(r, "add", "-A")
    _git(r, "commit", "-m", "base")
    return r


# ---------------------------------------------------------------------------
# #4 — _WORKER_SCOPE_LANES covers coder-claude, premium, verifier
# ---------------------------------------------------------------------------

def test_worker_scope_lanes_includes_coder_claude():
    assert "coder-claude" in decomp._WORKER_SCOPE_LANES


def test_worker_scope_lanes_includes_premium():
    assert "premium" in decomp._WORKER_SCOPE_LANES


def test_worker_scope_lanes_includes_verifier():
    assert "verifier" in decomp._WORKER_SCOPE_LANES


def test_is_worker_lane_premium():
    """premium is dispatched and must receive a scope_contract."""
    assert decomp._is_worker_lane("premium") is True


def test_is_worker_lane_coder_claude():
    """coder-claude is dispatched and must receive a scope_contract."""
    assert decomp._is_worker_lane("coder-claude") is True


def test_is_worker_lane_verifier():
    """verifier is review-gated and must receive a scope_contract."""
    assert decomp._is_worker_lane("verifier") is True


# ---------------------------------------------------------------------------
# #3-C — default_quick_gate ruff diff-relative
# ---------------------------------------------------------------------------

def test_default_quick_gate_ruff_uses_changed_py_files_not_dot(repo, monkeypatch):
    """When changed_files contains .py files, ruff must be called with those
    specific files — NOT with '.' as the argument."""
    calls = []

    def fake_run(argv, **kwargs):
        calls.append(list(argv))
        return SimpleNamespace(returncode=0, stdout="", stderr="")

    monkeypatch.setattr(kwt.shutil, "which", lambda name: "/usr/bin/ruff" if name == "ruff" else None)
    monkeypatch.setattr(kwt.subprocess, "run", fake_run)

    ok, detail = kwt.default_quick_gate(repo, ["hermes_cli/kanban_db.py"])

    assert ok is True
    ruff_calls = [c for c in calls if "ruff" in c[0] or (len(c) > 1 and c[1] == "check")]
    assert ruff_calls, "ruff should have been called"
    ruff_args = ruff_calls[0]
    # Must NOT be ruff check . (dot means whole repo)
    assert "." not in ruff_args, f"ruff was called with '.' (whole repo): {ruff_args}"
    # Must contain the specific changed file
    assert any("kanban_db.py" in arg for arg in ruff_args), (
        f"ruff call did not include the changed file: {ruff_args}"
    )


def test_default_quick_gate_ruff_skipped_when_no_py_files(repo, monkeypatch):
    """When changed_files contains no .py files, ruff should be skipped entirely."""
    calls = []

    def fake_run(argv, **kwargs):
        calls.append(list(argv))
        return SimpleNamespace(returncode=0, stdout="", stderr="")

    monkeypatch.setattr(kwt.shutil, "which", lambda name: "/usr/bin/ruff" if name == "ruff" else None)
    monkeypatch.setattr(kwt.subprocess, "run", fake_run)

    ok, detail = kwt.default_quick_gate(repo, ["README.md", "docs/architecture.md"])

    # ruff must not have been called at all for non-py diff
    ruff_calls = [c for c in calls if "ruff" in " ".join(c)]
    assert not ruff_calls, f"ruff should be skipped for non-.py diff, got: {ruff_calls}"
    assert ok is True
    assert "ruff skipped" in detail


def test_default_quick_gate_ruff_multiple_py_files_all_passed(repo, monkeypatch):
    """When multiple .py files changed, all are passed to ruff (not '.')."""
    calls = []

    def fake_run(argv, **kwargs):
        calls.append(list(argv))
        return SimpleNamespace(returncode=0, stdout="", stderr="")

    monkeypatch.setattr(kwt.shutil, "which", lambda name: "/usr/bin/ruff" if name == "ruff" else None)
    monkeypatch.setattr(kwt.subprocess, "run", fake_run)

    changed = ["hermes_cli/kanban_db.py", "hermes_cli/kanban_decompose.py"]
    ok, detail = kwt.default_quick_gate(repo, changed)

    assert ok is True
    ruff_calls = [c for c in calls if "ruff" in c[0] or (len(c) > 1 and c[1] == "check")]
    assert ruff_calls, "ruff should have been called"
    ruff_args = ruff_calls[0]
    assert "." not in ruff_args, f"ruff should not use '.' with explicit files: {ruff_args}"
    assert "hermes_cli/kanban_db.py" in ruff_args
    assert "hermes_cli/kanban_decompose.py" in ruff_args


# ---------------------------------------------------------------------------
# #3-A — worker_gate stamp in submitted_for_review + verifier render
# ---------------------------------------------------------------------------

def _make_review_task(conn, *, assignee="coder"):
    tid = kb.create_task(conn, title="gate stamp test", assignee=assignee)
    kb.claim_task(conn, tid)
    return tid


def test_submitted_for_review_includes_worker_gate_passed(kanban_home, tmp_path, monkeypatch):
    """When worker_gate is configured and passes, payload must have worker_gate
    with passed=True, exit_codes, commands, ts, and commit."""
    repo = tmp_path / "repo"
    repo.mkdir()
    _git(repo, "init", "-b", "main")
    _git(repo, "config", "user.email", "t@example.com")
    _git(repo, "config", "user.name", "tester")
    (repo / "a.py").write_text("x = 1\n")
    _git(repo, "add", "-A")
    _git(repo, "commit", "-m", "init")

    sha = subprocess.run(
        ["git", "rev-parse", "HEAD"], cwd=repo,
        capture_output=True, text=True, check=True,
    ).stdout.strip()

    wg_config = {
        "enabled": True,
        "repos": {str(repo.resolve()): ["true"]},
        "default": [],
        "timeout": 60,
        "code_roles": frozenset({"coder"}),
    }
    monkeypatch.setattr(kb, "_worker_gate_config", lambda: wg_config)

    with kb.connect() as conn:
        tid = kb.create_task(conn, title="g", assignee="coder",
                             workspace_path=str(repo))
        kb.claim_task(conn, tid)
        kb._submit_for_review(conn, tid, result="done", summary="done",
                              metadata=None, verified_cards=[], expected_run_id=None)

        row = conn.execute(
            "SELECT payload FROM task_events "
            "WHERE task_id = ? AND kind = 'submitted_for_review' "
            "ORDER BY id DESC LIMIT 1",
            (tid,),
        ).fetchone()

    payload = json.loads(row["payload"])
    wg = payload.get("worker_gate")
    assert wg is not None, "worker_gate field missing from payload"
    assert wg.get("passed") is True
    assert "exit_codes" in wg
    assert "commands" in wg
    assert "ts" in wg
    assert "commit" in wg
    assert wg["commit"].startswith(sha[:7]) or len(wg["commit"]) >= 7


def test_submitted_for_review_worker_gate_not_configured(kanban_home, tmp_path, monkeypatch):
    """When no commands are configured for the repo, payload must have
    worker_gate: {configured: false}."""
    wg_config = {
        "enabled": True,
        "repos": {},
        "default": [],
        "timeout": 60,
        "code_roles": frozenset({"coder"}),
    }
    monkeypatch.setattr(kb, "_worker_gate_config", lambda: wg_config)

    with kb.connect() as conn:
        tid = kb.create_task(conn, title="g", assignee="coder")
        kb.claim_task(conn, tid)
        kb._submit_for_review(conn, tid, result="done", summary="done",
                              metadata=None, verified_cards=[], expected_run_id=None)

        row = conn.execute(
            "SELECT payload FROM task_events "
            "WHERE task_id = ? AND kind = 'submitted_for_review' "
            "ORDER BY id DESC LIMIT 1",
            (tid,),
        ).fetchone()

    payload = json.loads(row["payload"])
    wg = payload.get("worker_gate")
    assert wg is not None, "worker_gate field missing"
    assert wg.get("configured") is False


def test_submitted_for_review_worker_gate_disabled(kanban_home, monkeypatch):
    """When worker_gate is disabled, payload must still have
    worker_gate: {configured: false}."""
    wg_config = {
        "enabled": False,
        "repos": {},
        "default": [],
        "timeout": 60,
        "code_roles": frozenset({"coder"}),
    }
    monkeypatch.setattr(kb, "_worker_gate_config", lambda: wg_config)

    with kb.connect() as conn:
        tid = kb.create_task(conn, title="g", assignee="coder")
        kb.claim_task(conn, tid)
        kb._submit_for_review(conn, tid, result="done", summary="done",
                              metadata=None, verified_cards=[], expected_run_id=None)

        row = conn.execute(
            "SELECT payload FROM task_events "
            "WHERE task_id = ? AND kind = 'submitted_for_review' "
            "ORDER BY id DESC LIMIT 1",
            (tid,),
        ).fetchone()

    payload = json.loads(row["payload"])
    wg = payload.get("worker_gate")
    assert wg is not None, "worker_gate field missing"
    assert wg.get("configured") is False


def test_render_review_verifier_section_gate_passed(kanban_home, tmp_path, monkeypatch):
    """Verifier context must contain 'Coder worker_gate: PASSED' line."""
    repo = tmp_path / "repo"
    repo.mkdir()
    _git(repo, "init", "-b", "main")
    _git(repo, "config", "user.email", "t@example.com")
    _git(repo, "config", "user.name", "tester")
    (repo / "a.py").write_text("x = 1\n")
    _git(repo, "add", "-A")
    _git(repo, "commit", "-m", "init")

    wg_config = {
        "enabled": True,
        "repos": {str(repo.resolve()): ["true"]},
        "default": [],
        "timeout": 60,
        "code_roles": frozenset({"coder"}),
    }
    monkeypatch.setattr(kb, "_worker_gate_config", lambda: wg_config)

    with kb.connect() as conn:
        tid = kb.create_task(conn, title="g", assignee="coder",
                             workspace_path=str(repo))
        kb.claim_task(conn, tid)
        kb._submit_for_review(conn, tid, result="done", summary="done",
                              metadata=None, verified_cards=[], expected_run_id=None)
        # Claim as verifier (uses claim_review_task to set source_status=review)
        kb.claim_review_task(conn, tid)

        lines = kb._render_review_verifier_section(conn, tid)

    text = "\n".join(str(l) for l in lines)
    assert "Coder worker_gate: PASSED" in text


def test_render_review_verifier_section_gate_not_configured(kanban_home, monkeypatch):
    """Verifier context must say 'not configured' when no gate ran."""
    wg_config = {
        "enabled": False,
        "repos": {},
        "default": [],
        "timeout": 60,
        "code_roles": frozenset({"coder"}),
    }
    monkeypatch.setattr(kb, "_worker_gate_config", lambda: wg_config)

    with kb.connect() as conn:
        tid = kb.create_task(conn, title="g", assignee="coder")
        kb.claim_task(conn, tid)
        kb._submit_for_review(conn, tid, result="done", summary="done",
                              metadata=None, verified_cards=[], expected_run_id=None)
        kb.claim_review_task(conn, tid)

        lines = kb._render_review_verifier_section(conn, tid)

    text = "\n".join(str(l) for l in lines)
    assert "not configured" in text


def test_render_review_verifier_section_gate_failed_format(kanban_home, tmp_path, monkeypatch):
    """Verifier context format for a failed gate: 'Coder worker_gate: FAILED (<cmd> exit <n>)'."""
    # Inject a pre-baked worker_gate payload with passed=False directly
    # rather than running a real failing command (avoid side effects).
    wg_config = {
        "enabled": False,  # disabled so _submit_for_review won't actually run
        "repos": {},
        "default": [],
        "timeout": 60,
        "code_roles": frozenset({"coder"}),
    }
    monkeypatch.setattr(kb, "_worker_gate_config", lambda: wg_config)

    with kb.connect() as conn:
        tid = kb.create_task(conn, title="g", assignee="coder")
        kb.claim_task(conn, tid)
        kb._submit_for_review(conn, tid, result="done", summary="done",
                              metadata=None, verified_cards=[], expected_run_id=None)

        # Patch the submitted_for_review payload to simulate a failed gate
        row = conn.execute(
            "SELECT id, payload FROM task_events "
            "WHERE task_id = ? AND kind = 'submitted_for_review' ORDER BY id DESC LIMIT 1",
            (tid,),
        ).fetchone()
        payload = json.loads(row["payload"])
        payload["worker_gate"] = {
            "passed": False,
            "commands": ["make test"],
            "exit_codes": [2],
            "ts": "2026-06-18T00:00:00Z",
            "commit": "abc1234",
        }
        conn.execute(
            "UPDATE task_events SET payload = ? WHERE id = ?",
            (json.dumps(payload), row["id"]),
        )
        conn.commit()

        kb.claim_review_task(conn, tid)
        lines = kb._render_review_verifier_section(conn, tid)

    text = "\n".join(str(l) for l in lines)
    assert "Coder worker_gate: FAILED" in text
    assert "make test" in text
    assert "exit 2" in text
