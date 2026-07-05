"""Subsystem B — verification economy (Plan→Board→Release pipeline, 2026-07-05).

B1: with ``kanban.review_gate.standard_uses_llm_verifier: false`` a completed
``standard``-tier code task whose deterministic worker gate ran GREEN goes
straight to ``done`` — no LLM verifier spawn. Safety floor: without green gate
evidence (gate unconfigured / no workspace) the verifier still fires — never
ship un-gated.
"""

from __future__ import annotations

import json
import subprocess
from pathlib import Path

import pytest

import hermes_cli.profiles as profiles_mod
from hermes_cli import kanban_db as kb


def _write_profile(home: Path, name: str) -> None:
    d = home / "profiles" / name
    d.mkdir(parents=True, exist_ok=True)
    (d / "config.yaml").write_text("model: {}\n")


@pytest.fixture
def kanban_home(tmp_path, monkeypatch):
    home = tmp_path / ".hermes"
    home.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(home))
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    for name in ["coder", "verifier"]:
        _write_profile(home, name)
    kb.init_db()
    return home


def _gate_cfg(**overrides):
    cfg = {
        "enabled": True,
        "code_roles": frozenset({"coder", "premium"}),
        "verifier_profile": "verifier",
        "review_profile": "reviewer",
        "critic_profile": "critic",
        "auto_tier": False,
        "standard_uses_llm_verifier": True,
    }
    cfg.update(overrides)
    return cfg


@pytest.fixture
def economy_on(monkeypatch):
    """Review gate ON, standard tier trusts deterministic gates (no verifier)."""
    monkeypatch.setattr(
        kb, "_review_gate_config", lambda: _gate_cfg(standard_uses_llm_verifier=False)
    )
    monkeypatch.setattr(profiles_mod, "profile_exists", lambda name: True)
    return True


def _git(repo: Path, *args: str) -> None:
    subprocess.run(["git", *args], cwd=repo, check=True, capture_output=True)


def _green_gate_repo(tmp_path: Path) -> Path:
    repo = tmp_path / "repo"
    repo.mkdir()
    _git(repo, "init", "-b", "main")
    _git(repo, "config", "user.email", "t@example.com")
    _git(repo, "config", "user.name", "tester")
    (repo / "a.py").write_text("x = 1\n")
    _git(repo, "add", "-A")
    _git(repo, "commit", "-m", "init")
    return repo


def _worker_gate_for(repo: Path, commands=("true",)) -> dict:
    return {
        "enabled": True,
        "repos": {str(repo.resolve()): list(commands)},
        "default": [],
        "timeout": 60,
        "code_roles": frozenset({"coder"}),
    }


def test_standard_tier_no_llm_verifier(kanban_home, economy_on, tmp_path, monkeypatch):
    """standard + flag off + green worker gate → terminal done, no review park."""
    repo = _green_gate_repo(tmp_path)
    monkeypatch.setattr(kb, "_worker_gate_config", lambda: _worker_gate_for(repo))
    with kb.connect() as conn:
        tid = kb.create_task(
            conn, title="impl X", assignee="coder", workspace_path=str(repo)
        )
        kb.claim_task(conn, tid)
        assert kb.complete_task(conn, tid, summary="impl done", review_gate=True)
        assert kb.get_task(conn, tid).status == "done"
        submitted = conn.execute(
            "SELECT 1 FROM task_events "
            "WHERE task_id = ? AND kind = 'submitted_for_review'",
            (tid,),
        ).fetchone()
        assert submitted is None, "standard task must not enter the review chain"
        skipped = conn.execute(
            "SELECT payload FROM task_events "
            "WHERE task_id = ? AND kind = 'review_skipped_deterministic'",
            (tid,),
        ).fetchone()
        assert skipped is not None, "skip must leave an audit event"
        payload = json.loads(skipped["payload"])
        assert payload["worker_gate"]["passed"] is True


def test_standard_without_green_gate_still_verified(
    kanban_home, economy_on, monkeypatch
):
    """Safety floor: no worker gate configured → verifier fires anyway."""
    monkeypatch.setattr(
        kb,
        "_worker_gate_config",
        lambda: {
            "enabled": False,
            "repos": {},
            "default": [],
            "timeout": 60,
            "code_roles": frozenset({"coder"}),
        },
    )
    with kb.connect() as conn:
        tid = kb.create_task(conn, title="impl Y", assignee="coder")
        kb.claim_task(conn, tid)
        assert kb.complete_task(conn, tid, summary="done", review_gate=True)
        assert kb.get_task(conn, tid).status == "review"


def test_standard_red_gate_still_blocks(kanban_home, economy_on, tmp_path, monkeypatch):
    """A RED worker gate keeps today's fail-safe: WorkerGateError, task in-flight."""
    repo = _green_gate_repo(tmp_path)
    monkeypatch.setattr(
        kb, "_worker_gate_config", lambda: _worker_gate_for(repo, commands=("false",))
    )
    with kb.connect() as conn:
        tid = kb.create_task(
            conn, title="impl Z", assignee="coder", workspace_path=str(repo)
        )
        kb.claim_task(conn, tid)
        with pytest.raises(kb.WorkerGateError):
            kb.complete_task(conn, tid, summary="done", review_gate=True)
        assert kb.get_task(conn, tid).status == "running"


def test_flag_default_keeps_verifier(kanban_home, tmp_path, monkeypatch):
    """standard_uses_llm_verifier defaults True → parity with today (review park)."""
    monkeypatch.setattr(kb, "_review_gate_config", lambda: _gate_cfg())
    monkeypatch.setattr(profiles_mod, "profile_exists", lambda name: True)
    repo = _green_gate_repo(tmp_path)
    monkeypatch.setattr(kb, "_worker_gate_config", lambda: _worker_gate_for(repo))
    with kb.connect() as conn:
        tid = kb.create_task(
            conn, title="impl W", assignee="coder", workspace_path=str(repo)
        )
        kb.claim_task(conn, tid)
        assert kb.complete_task(conn, tid, summary="done", review_gate=True)
        assert kb.get_task(conn, tid).status == "review"


def test_review_tier_never_skips(kanban_home, economy_on, tmp_path, monkeypatch):
    """The economy applies to standard ONLY — review/critical always park."""
    repo = _green_gate_repo(tmp_path)
    monkeypatch.setattr(kb, "_worker_gate_config", lambda: _worker_gate_for(repo))
    with kb.connect() as conn:
        tid = kb.create_task(
            conn,
            title="impl R",
            assignee="coder",
            workspace_path=str(repo),
            review_tier="review",
        )
        kb.claim_task(conn, tid)
        assert kb.complete_task(conn, tid, summary="done", review_gate=True)
        assert kb.get_task(conn, tid).status == "review"


def test_review_gate_config_parses_standard_flag(kanban_home, monkeypatch):
    """_review_gate_config surfaces the new key; absent → True (conservative)."""
    import yaml as _yaml

    cfg_path = kanban_home / "config.yaml"
    cfg_path.write_text(
        _yaml.safe_dump(
            {"kanban": {"review_gate": {"enabled": True}}}, sort_keys=False
        )
    )
    import hermes_constants

    monkeypatch.setattr(
        hermes_constants, "get_default_hermes_root", lambda: kanban_home
    )
    assert kb._review_gate_config()["standard_uses_llm_verifier"] is True
    cfg_path.write_text(
        _yaml.safe_dump(
            {
                "kanban": {
                    "review_gate": {
                        "enabled": True,
                        "standard_uses_llm_verifier": False,
                    }
                }
            },
            sort_keys=False,
        )
    )
    assert kb._review_gate_config()["standard_uses_llm_verifier"] is False


# ---------------------------------------------------------------------------
# B2: judge once at the chain tip (review tier), not per slice
# ---------------------------------------------------------------------------


@pytest.fixture
def tip_judgment_on(monkeypatch):
    """Review gate ON + judge_at_chain_tip: review-tier slices defer to tip."""
    monkeypatch.setattr(
        kb, "_review_gate_config", lambda: _gate_cfg(judge_at_chain_tip=True)
    )
    monkeypatch.setattr(profiles_mod, "profile_exists", lambda name: True)
    return True


def _planspec_chain(conn, repo, n=3, tier="review", source="/tmp/spec.md"):
    tids = []
    for i in range(n):
        tid = kb.create_task(
            conn,
            title=f"slice {i + 1}",
            assignee="coder",
            workspace_path=str(repo),
            review_tier=tier,
        )
        conn.execute(
            "UPDATE tasks SET planspec_source = ? WHERE id = ?", (source, tid)
        )
        tids.append(tid)
    conn.commit()
    return tids


def test_review_fires_at_tip_not_per_slice(
    kanban_home, tip_judgment_on, tmp_path, monkeypatch
):
    repo = _green_gate_repo(tmp_path)
    monkeypatch.setattr(kb, "_worker_gate_config", lambda: _worker_gate_for(repo))
    with kb.connect() as conn:
        t1, t2, t3 = _planspec_chain(conn, repo)
        for tid in (t1, t2):
            kb.claim_task(conn, tid)
            assert kb.complete_task(conn, tid, summary="s", review_gate=True)
            assert kb.get_task(conn, tid).status == "done", tid
            deferred = conn.execute(
                "SELECT 1 FROM task_events "
                "WHERE task_id = ? AND kind = 'review_deferred_to_tip'",
                (tid,),
            ).fetchone()
            assert deferred is not None, tid
        # last open code slice = the tip -> full review chain fires here
        kb.claim_task(conn, t3)
        assert kb.complete_task(conn, t3, summary="s", review_gate=True)
        assert kb.get_task(conn, t3).status == "review"
        submitted = conn.execute(
            "SELECT 1 FROM task_events "
            "WHERE task_id = ? AND kind = 'submitted_for_review'",
            (t3,),
        ).fetchone()
        assert submitted is not None


def test_critical_slice_still_reviewed_individually(
    kanban_home, tip_judgment_on, tmp_path, monkeypatch
):
    repo = _green_gate_repo(tmp_path)
    monkeypatch.setattr(kb, "_worker_gate_config", lambda: _worker_gate_for(repo))
    with kb.connect() as conn:
        t1, _t2, _t3 = _planspec_chain(conn, repo, tier="critical")
        kb.claim_task(conn, t1)
        assert kb.complete_task(conn, t1, summary="s", review_gate=True)
        assert kb.get_task(conn, t1).status == "review"


def test_tip_defer_requires_green_gate(kanban_home, tip_judgment_on, monkeypatch):
    """Safety floor: non-tip review slice without gate evidence still parks."""
    monkeypatch.setattr(
        kb,
        "_worker_gate_config",
        lambda: {
            "enabled": False,
            "repos": {},
            "default": [],
            "timeout": 60,
            "code_roles": frozenset({"coder"}),
        },
    )
    with kb.connect() as conn:
        t1, _t2, _t3 = _planspec_chain(conn, repo=Path("/nonexistent"))
        kb.claim_task(conn, t1)
        assert kb.complete_task(conn, t1, summary="s", review_gate=True)
        assert kb.get_task(conn, t1).status == "review"


def test_non_planspec_review_task_unaffected(
    kanban_home, tip_judgment_on, tmp_path, monkeypatch
):
    repo = _green_gate_repo(tmp_path)
    monkeypatch.setattr(kb, "_worker_gate_config", lambda: _worker_gate_for(repo))
    with kb.connect() as conn:
        tid = kb.create_task(
            conn,
            title="solo",
            assignee="coder",
            workspace_path=str(repo),
            review_tier="review",
        )
        kb.claim_task(conn, tid)
        assert kb.complete_task(conn, tid, summary="s", review_gate=True)
        assert kb.get_task(conn, tid).status == "review"


def test_tip_flag_default_off_keeps_per_slice(kanban_home, tmp_path, monkeypatch):
    monkeypatch.setattr(kb, "_review_gate_config", lambda: _gate_cfg())
    monkeypatch.setattr(profiles_mod, "profile_exists", lambda name: True)
    repo = _green_gate_repo(tmp_path)
    monkeypatch.setattr(kb, "_worker_gate_config", lambda: _worker_gate_for(repo))
    with kb.connect() as conn:
        t1, _t2, _t3 = _planspec_chain(conn, repo)
        kb.claim_task(conn, t1)
        assert kb.complete_task(conn, t1, summary="s", review_gate=True)
        assert kb.get_task(conn, t1).status == "review"


def test_open_noncode_sibling_does_not_block_tip(
    kanban_home, tip_judgment_on, tmp_path, monkeypatch
):
    """Tip detection counts CODE siblings only — a trailing docs/scribe task
    must not swallow the chain's single LLM judgment."""
    repo = _green_gate_repo(tmp_path)
    monkeypatch.setattr(kb, "_worker_gate_config", lambda: _worker_gate_for(repo))
    with kb.connect() as conn:
        (t1,) = _planspec_chain(conn, repo, n=1)
        doc = kb.create_task(conn, title="write receipt", assignee="scribe")
        conn.execute(
            "UPDATE tasks SET planspec_source = ? WHERE id = ?", ("/tmp/spec.md", doc)
        )
        conn.commit()
        kb.claim_task(conn, t1)
        assert kb.complete_task(conn, t1, summary="s", review_gate=True)
        # t1 is the last open CODE task -> it is the tip -> review fires
        assert kb.get_task(conn, t1).status == "review"
