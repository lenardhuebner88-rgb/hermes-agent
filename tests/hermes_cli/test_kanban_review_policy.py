"""Diff-derived Kanban review floors and chain-tip evidence."""

from __future__ import annotations

import json
from pathlib import Path
import subprocess

import pytest

import hermes_cli.profiles as profiles_mod
from hermes_cli import kanban_db as kb
from hermes_cli import kanban_review_policy as policy


def _git(repo: Path, *args: str) -> str:
    proc = subprocess.run(
        ["git", *args],
        cwd=repo,
        check=True,
        capture_output=True,
        text=True,
    )
    return proc.stdout.strip()


def _repo(tmp_path: Path, *, feature_branch: bool = True) -> Path:
    repo = tmp_path / "repo"
    repo.mkdir()
    _git(repo, "init", "-b", "main")
    _git(repo, "config", "user.email", "review-policy@example.invalid")
    _git(repo, "config", "user.name", "review-policy-test")
    (repo / "README.md").write_text("base\n", encoding="utf-8")
    _git(repo, "add", "-A")
    _git(repo, "commit", "-m", "base")
    if feature_branch:
        _git(repo, "switch", "-c", "feature")
    return repo


@pytest.fixture
def kanban_home(tmp_path, monkeypatch):
    home = tmp_path / ".hermes"
    home.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(home))
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    kb.init_db()
    return home


def _review_cfg(**overrides) -> dict:
    cfg = {
        "enabled": True,
        "code_roles": frozenset({"coder", "premium"}),
        "acceptance_roles": frozenset(),
        "verifier_profile": "verifier",
        "review_profile": "reviewer",
        "critic_profile": "critic",
        "auto_tier": True,
        "auto_scout_on_critical": False,
        "scout_max_runtime_seconds": None,
        "standard_uses_llm_verifier": False,
        "judge_at_chain_tip": False,
        "critical_reviews_each_slice": True,
        "max_review_rounds": 3,
    }
    cfg.update(overrides)
    return cfg


def _worker_gate(repo: Path) -> dict:
    return {
        "enabled": True,
        "repos": {
            str(repo.resolve()): [
                "printf '=== Summary: 1 files, 1 tests passed, 0 failed ===\\n'"
            ]
        },
        "default": [],
        "timeout": 60,
        "code_roles": frozenset({"coder"}),
    }


def _linked_children(
    conn,
    repo: Path,
    *,
    merge_target: str,
) -> tuple[str, str, str]:
    root = kb.create_task(
        conn,
        title="review chain",
        assignee="verifier",
        workspace_path=str(repo),
        triage=True,
    )
    children = kb.decompose_triage_task(
        conn,
        root,
        root_assignee="verifier",
        children=[
            {
                "title": "slice one",
                "assignee": "coder",
                "kind": "code",
                "review_tier": "standard",
                "parents": [],
            },
            {
                "title": "slice two",
                "assignee": "coder",
                "kind": "code",
                "review_tier": "standard",
                "parents": [0],
            },
        ],
        author="test",
    )
    assert children is not None
    with kb.write_txn(conn):
        kb._append_event(
            conn,
            root,
            "worktree_provisioned",
            {"merge_target": merge_target},
        )
    return root, children[0], children[1]


def test_classifier_covers_sensitive_production_paths_and_excludes_tests():
    decision = policy.classify_changed_paths(
        [
            "agent/tool_executor.py",
            "gateway/oauth_callback.py",
            "supabase/migrations/20260725_add_index.sql",
            "tests/gateway/test_oauth_callback.py",
            "docs/auth-notes.md",
        ]
    )

    assert decision.tier == "critical"
    assert set(decision.rule_ids) == {
        "auth_surface",
        "autonomy_core",
        "schema_migration",
    }
    assert {match.path for match in decision.matches}.isdisjoint(
        {"tests/gateway/test_oauth_callback.py", "docs/auth-notes.md"}
    )


def test_uncapped_path_floor_sees_sensitive_file_after_display_cap(
    kanban_home, tmp_path, monkeypatch
):
    repo = _repo(tmp_path)
    monkeypatch.setattr(kb, "_review_gate_config", lambda: _review_cfg())
    with kb.connect() as conn:
        task_id = kb.create_task(
            conn,
            title="large landing",
            assignee="coder",
            workspace_path=str(repo),
            review_tier="standard",
        )
        assert kb.claim_task(conn, task_id) is not None
        benign = repo / "agent"
        benign.mkdir()
        for index in range(205):
            (benign / f"benign_{index:03d}.py").write_text(
                "VALUE = 1\n",
                encoding="utf-8",
            )
        sensitive = repo / "tools" / "zz_auth.py"
        sensitive.parent.mkdir()
        sensitive.write_text("TOKEN_POLICY = 'strict'\n", encoding="utf-8")

        context = policy.build_review_context(conn, task_id)
        assert len(context.changed_files) == 206
        assert context.path_risk.tier == "critical"
        assert kb._effective_review_tier(
            conn,
            task_id,
            cfg=_review_cfg(),
            review_context=context,
        ) == "critical"

        snapshot = policy.capture_review_snapshot(
            context,
            conn,
            task_id,
            context.run_id,
            capture=kb._capture_review_diff_snapshot,
        )
        assert len(snapshot["changed_files"]) == 200
        assert snapshot["changed_files_total"] == 206
        assert snapshot["changed_files_truncated"] is True
        assert "tools/zz_auth.py" not in snapshot["changed_files"]


def test_path_floor_applies_to_non_code_assignee(
    kanban_home, tmp_path, monkeypatch
):
    repo = _repo(tmp_path)
    monkeypatch.setattr(kb, "_review_gate_config", lambda: _review_cfg())
    monkeypatch.setattr(profiles_mod, "profile_exists", lambda _name: True)
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
        task_id = kb.create_task(
            conn,
            title="touch auth surface",
            assignee="scribe",
            kind="research",
            workspace_path=str(repo),
        )
        assert kb.claim_task(conn, task_id) is not None
        auth_file = repo / "gateway" / "oauth_callback.py"
        auth_file.parent.mkdir()
        auth_file.write_text("ENABLED = True\n", encoding="utf-8")

        assert kb.complete_task(
            conn,
            task_id,
            summary="auth surface changed",
            review_gate=True,
        )
        assert kb.get_task(conn, task_id).status == "review"
        submitted = conn.execute(
            "SELECT payload FROM task_events "
            "WHERE task_id = ? AND kind = 'submitted_for_review' "
            "ORDER BY id DESC LIMIT 1",
            (task_id,),
        ).fetchone()
        payload = json.loads(submitted["payload"])
        assert payload["review_tier"] == "critical"
        assert payload["changed_files"] == ["gateway/oauth_callback.py"]


def test_path_downgrade_ack_is_bound_to_observed_diff(
    kanban_home, tmp_path, monkeypatch
):
    repo = _repo(tmp_path)
    monkeypatch.setattr(kb, "_review_gate_config", lambda: _review_cfg())
    with kb.connect() as conn:
        task_id = kb.create_task(
            conn,
            title="explicit downgrade",
            assignee="coder",
            workspace_path=str(repo),
            review_tier="standard",
        )
        assert kb.claim_task(conn, task_id) is not None
        first = repo / "gateway" / "oauth.py"
        first.parent.mkdir()
        first.write_text("FLOW = 1\n", encoding="utf-8")
        context = policy.build_review_context(conn, task_id)
        assert kb._effective_review_tier(
            conn, task_id, cfg=_review_cfg(), review_context=context
        ) == "critical"

        assert kb.set_task_review_tier(
            conn,
            task_id,
            "standard",
            acknowledge_downgrade=True,
        )
        acknowledged = policy.build_review_context(conn, task_id)
        assert kb._effective_review_tier(
            conn,
            task_id,
            cfg=_review_cfg(),
            review_context=acknowledged,
        ) == "standard"

        first.write_text("FLOW = 2\n", encoding="utf-8")
        same_path_changed = policy.build_review_context(conn, task_id)
        assert kb._effective_review_tier(
            conn,
            task_id,
            cfg=_review_cfg(),
            review_context=same_path_changed,
        ) == "critical"

        assert kb.set_task_review_tier(
            conn,
            task_id,
            "standard",
            acknowledge_downgrade=True,
        )
        second = repo / "tools" / "authz.py"
        second.parent.mkdir()
        second.write_text("FLOW = 3\n", encoding="utf-8")
        changed = policy.build_review_context(conn, task_id)
        assert kb._effective_review_tier(
            conn, task_id, cfg=_review_cfg(), review_context=changed
        ) == "critical"


def test_missing_baseline_disables_deterministic_skip(
    kanban_home, tmp_path, monkeypatch
):
    repo = _repo(tmp_path)
    monkeypatch.setattr(kb, "_review_gate_config", lambda: _review_cfg())
    monkeypatch.setattr(profiles_mod, "profile_exists", lambda _name: True)
    monkeypatch.setattr(kb, "_worker_gate_config", lambda: _worker_gate(repo))
    with kb.connect() as conn:
        task_id = kb.create_task(
            conn,
            title="manual ready completion",
            assignee="coder",
            workspace_path=str(repo),
        )
        assert kb.complete_task(
            conn,
            task_id,
            summary="no claimed run baseline",
            review_gate=True,
        )
        assert kb.get_task(conn, task_id).status == "review"
        assert conn.execute(
            "SELECT 1 FROM task_events "
            "WHERE task_id = ? AND kind = 'review_skipped_deterministic'",
            (task_id,),
        ).fetchone() is None


def test_chain_tip_reuses_one_uncapped_scan_and_accumulated_diff(
    kanban_home, tmp_path, monkeypatch
):
    repo = _repo(tmp_path)
    cfg = _review_cfg(
        judge_at_chain_tip=True,
        critical_reviews_each_slice=False,
    )
    monkeypatch.setattr(kb, "_review_gate_config", lambda: cfg)
    monkeypatch.setattr(kb, "_worker_gate_config", lambda: _worker_gate(repo))
    monkeypatch.setattr(profiles_mod, "profile_exists", lambda _name: True)
    real_run = subprocess.run
    commands: list[tuple[str, ...]] = []

    def recording_run(command, *args, **kwargs):
        commands.append(tuple(str(part) for part in command))
        return real_run(command, *args, **kwargs)

    monkeypatch.setattr(policy.subprocess, "run", recording_run)
    with kb.connect() as conn:
        _root, first, tip = _linked_children(conn, repo, merge_target="main")
        assert kb.claim_task(conn, first) is not None
        mid = repo / "agent" / "tool_executor.py"
        mid.parent.mkdir()
        mid.write_text("MID = 1\n", encoding="utf-8")
        _git(repo, "add", "-A")
        _git(repo, "commit", "-m", "mid")
        assert kb.complete_task(conn, first, summary="mid", review_gate=True)
        assert kb.get_task(conn, first).status == "done"

        assert kb.claim_task(conn, tip) is not None
        tip_file = repo / "gateway" / "oauth_tip.py"
        tip_file.parent.mkdir()
        tip_file.write_text("TIP = 1\n", encoding="utf-8")
        _git(repo, "add", "-A")
        _git(repo, "commit", "-m", "tip")
        assert kb.complete_task(conn, tip, summary="tip", review_gate=True)
        assert kb.get_task(conn, tip).status == "review"

        row = conn.execute(
            "SELECT payload FROM task_events "
            "WHERE task_id = ? AND kind = 'submitted_for_review' "
            "ORDER BY id DESC LIMIT 1",
            (tip,),
        ).fetchone()
        payload = json.loads(row["payload"])
        assert payload["diff_baseline"] == "chain_merge_target"
        assert payload["merge_target"] == "main"
        assert payload["review_chain_accumulated"] is True
        assert payload["changed_files"] == [
            "agent/tool_executor.py",
            "gateway/oauth_tip.py",
        ]

    full_scans = [
        command
        for command in commands
        if "diff" in command and "--name-only" in command and "-z" in command
    ]
    assert len(full_scans) == 2


def test_stale_baseline_clamp_excludes_target_advance_and_keeps_slice(
    kanban_home, tmp_path
):
    repo = _repo(tmp_path)
    with kb.connect() as conn:
        task_id = kb.create_task(
            conn,
            title="target advance plus slice",
            assignee="coder",
            workspace_path=str(repo),
            review_tier="standard",
        )
        with kb.write_txn(conn):
            kb._append_event(
                conn,
                task_id,
                "worktree_provisioned",
                {"merge_target": "main"},
            )
        assert kb.claim_task(conn, task_id) is not None
        pinned_baseline = _git(repo, "rev-parse", "HEAD")

        _git(repo, "checkout", "main")
        foreign = repo / "fremde_datei.py"
        foreign.write_text("FOREIGN = 1\n", encoding="utf-8")
        _git(repo, "add", "-A")
        _git(repo, "commit", "-m", "foreign advancement")

        _git(repo, "checkout", "feature")
        slice_file = repo / "slice_datei.py"
        slice_file.write_text("SLICE = 1\n", encoding="utf-8")
        _git(repo, "add", "-A")
        _git(repo, "commit", "-m", "own slice after pin")
        _git(repo, "merge", "main", "-m", "sync target advancement")
        fork_point = _git(repo, "merge-base", "HEAD", "main")

        context = policy.build_review_context(conn, task_id)
        assert context.baseline_commit == fork_point, (
            f"Baseline should be clamped to fork point {fork_point[:12]}, "
            f"got {context.baseline_commit[:12] if context.baseline_commit else 'None'}"
        )
        assert context.baseline_kind == "pre_run_commit_sha"
        assert context.baseline_clamped_to_fork_point is True
        assert context.pre_clamp_baseline_commit == pinned_baseline
        assert context.clamp_target_ref == "main"
        assert context.changed_files
        foreign_excluded = "fremde_datei.py" not in context.changed_files
        slice_included = "slice_datei.py" in context.changed_files
        print(
            "clamp assertions: "
            f"foreign excluded={foreign_excluded}; "
            f"slice included={slice_included}; "
            f"changed_files={context.changed_files}"
        )
        assert foreign_excluded
        assert slice_included

        snapshot = policy.capture_review_snapshot(
            context,
            conn,
            task_id,
            context.run_id,
            capture=kb._capture_review_diff_snapshot,
        )
        assert snapshot["diff_baseline"] == "pre_run_commit_sha"
        assert snapshot["diff_base_clamped_to_fork_point"] is True
        assert snapshot["diff_base_pre_clamp_commit"] == pinned_baseline
        assert snapshot["diff_clamp_target"] == "main"

        deferred = policy.defer_event_payload(context, {"passed": True})
        assert deferred["diff_baseline"] == "pre_run_commit_sha"
        assert deferred["diff_base_clamped_to_fork_point"] is True
        assert deferred["diff_base_pre_clamp_commit"] == pinned_baseline
        assert deferred["diff_clamp_target"] == "main"


def test_landed_candidate_preserves_slice_diff_and_critical_path_risk(
    kanban_home, tmp_path
):
    repo = _repo(tmp_path)
    with kb.connect() as conn:
        task_id = kb.create_task(
            conn,
            title="landed critical slice",
            assignee="coder",
            workspace_path=str(repo),
            review_tier="standard",
        )
        with kb.write_txn(conn):
            kb._append_event(
                conn,
                task_id,
                "worktree_provisioned",
                {"merge_target": "main"},
            )
        assert kb.claim_task(conn, task_id) is not None
        pinned_baseline = _git(repo, "rev-parse", "HEAD")

        critical = repo / "gateway" / "run.py"
        critical.parent.mkdir()
        critical.write_text("AUTONOMY = True\n", encoding="utf-8")
        _git(repo, "add", "-A")
        _git(repo, "commit", "-m", "critical slice")
        _git(repo, "checkout", "main")
        _git(repo, "merge", "--no-ff", "feature", "-m", "land critical slice")
        _git(repo, "checkout", "feature")

        context = policy.build_review_context(conn, task_id)
        assert context.baseline_commit == pinned_baseline
        assert context.baseline_clamped_to_fork_point is False
        assert context.changed_files == ("gateway/run.py",)
        assert context.path_risk.tier == "critical"
        assert kb._effective_review_tier(
            conn,
            task_id,
            cfg=_review_cfg(),
            review_context=context,
        ) == "critical"


def test_recorded_task_commit_in_clamp_span_keeps_original_baseline(
    kanban_home, tmp_path
):
    repo = _repo(tmp_path)
    with kb.connect() as conn:
        task_id = kb.create_task(
            conn,
            title="recorded task commit in clamp span",
            assignee="coder",
            workspace_path=str(repo),
            review_tier="standard",
        )
        with kb.write_txn(conn):
            kb._append_event(
                conn,
                task_id,
                "worktree_provisioned",
                {"merge_target": "main"},
            )
        claim = kb.claim_task(conn, task_id)
        assert claim is not None
        assert claim.current_run_id is not None
        pinned_baseline = _git(repo, "rev-parse", "HEAD")

        own = repo / "own_recorded_slice.py"
        own.write_text("OWN = 1\n", encoding="utf-8")
        _git(repo, "add", "-A")
        _git(repo, "commit", "-m", "recorded own slice")
        own_commit = _git(repo, "rev-parse", "HEAD")
        with kb.write_txn(conn):
            conn.execute(
                "UPDATE task_runs SET metadata = ? WHERE id = ?",
                (json.dumps({"commit": own_commit}), claim.current_run_id),
            )

        _git(repo, "checkout", "main")
        _git(repo, "merge", "--no-ff", "feature", "-m", "land recorded slice")
        foreign = repo / "foreign_after_landing.py"
        foreign.write_text("FOREIGN = 1\n", encoding="utf-8")
        _git(repo, "add", "-A")
        _git(repo, "commit", "-m", "advance target after landing")

        _git(repo, "checkout", "feature")
        followup = repo / "followup_slice.py"
        followup.write_text("FOLLOWUP = 1\n", encoding="utf-8")
        _git(repo, "add", "-A")
        _git(repo, "commit", "-m", "follow-up slice")
        _git(repo, "merge", "main", "-m", "sync landed target")
        candidate = _git(repo, "rev-parse", "HEAD")
        fork_point = _git(repo, "merge-base", candidate, "main")
        assert not policy._git_succeeds(
            str(repo),
            "merge-base",
            "--is-ancestor",
            candidate,
            "main",
        )
        assert policy._git_succeeds(
            str(repo),
            "merge-base",
            "--is-ancestor",
            pinned_baseline,
            own_commit,
        )
        assert policy._git_succeeds(
            str(repo),
            "merge-base",
            "--is-ancestor",
            own_commit,
            fork_point,
        )

        context = policy.build_review_context(conn, task_id)
        assert context.baseline_commit == pinned_baseline
        assert context.baseline_clamped_to_fork_point is False
        assert "own_recorded_slice.py" in context.changed_files


def test_fork_point_clamp_never_moves_baseline_backward(
    kanban_home, tmp_path
):
    repo = _repo(tmp_path)
    anchor = repo / "slice_anchor.py"
    anchor.write_text("ANCHOR = 1\n", encoding="utf-8")
    _git(repo, "add", "-A")
    _git(repo, "commit", "-m", "slice anchor")
    pinned_baseline = _git(repo, "rev-parse", "HEAD")

    with kb.connect() as conn:
        task_id = kb.create_task(
            conn,
            title="forward-only clamp",
            assignee="coder",
            workspace_path=str(repo),
            review_tier="standard",
        )
        with kb.write_txn(conn):
            kb._append_event(
                conn,
                task_id,
                "worktree_provisioned",
                {"merge_target": "main"},
            )
        assert kb.claim_task(conn, task_id) is not None

        _git(repo, "checkout", "main")
        main_only = repo / "main_only.py"
        main_only.write_text("MAIN = 1\n", encoding="utf-8")
        _git(repo, "add", "-A")
        _git(repo, "commit", "-m", "main advances separately")
        _git(repo, "checkout", "feature")
        slice_work = repo / "slice_after_claim.py"
        slice_work.write_text("SLICE = 1\n", encoding="utf-8")
        _git(repo, "add", "-A")
        _git(repo, "commit", "-m", "slice work after claim")
        fork_point = _git(repo, "merge-base", "HEAD", "main")
        assert not policy._git_succeeds(
            str(repo),
            "merge-base",
            "--is-ancestor",
            pinned_baseline,
            fork_point,
        )

        context = policy.build_review_context(conn, task_id)
        assert context.baseline_commit == pinned_baseline
        assert context.baseline_kind == "pre_run_commit_sha"
        assert context.baseline_clamped_to_fork_point is False
        assert context.changed_files == ("slice_after_claim.py",)
        assert kb.complete_task(
            conn,
            task_id,
            summary="forward-only clamp reviewed",
            review_gate=False,
        )


def test_non_main_target_advance_excludes_target_only_file(
    kanban_home, tmp_path
):
    repo = _repo(tmp_path, feature_branch=False)
    _git(repo, "branch", "release")
    _git(repo, "branch", "worker")
    _git(repo, "switch", "release")
    target_only = repo / "target_only.py"
    target_only.write_text("TARGET = 1\n", encoding="utf-8")
    _git(repo, "add", "-A")
    _git(repo, "commit", "-m", "target advance")
    target_head = _git(repo, "rev-parse", "HEAD")
    _git(repo, "switch", "worker")
    mid = repo / "mid.py"
    mid.write_text("MID = 1\n", encoding="utf-8")
    _git(repo, "add", "-A")
    _git(repo, "commit", "-m", "mid")
    tip_file = repo / "tip.py"
    tip_file.write_text("TIP = 1\n", encoding="utf-8")
    _git(repo, "add", "-A")
    _git(repo, "commit", "-m", "tip")

    with kb.connect() as conn:
        _root, first, tip = _linked_children(
            conn,
            repo,
            merge_target="release",
        )
        assert kb.claim_task(conn, first) is not None
        assert kb.complete_task(
            conn,
            first,
            summary="mid slice already complete",
            review_gate=False,
        )
        with kb.write_txn(conn):
            kb._append_event(
                conn,
                first,
                "review_deferred_to_tip",
                {"review_chain_kind": "linked"},
            )
            conn.execute(
                "UPDATE tasks SET status = 'done' WHERE id = ?",
                (first,),
            )
        assert kb.claim_task(conn, tip) is not None
        context = policy.build_review_context(conn, tip)

    assert context.evidence_complete is True
    assert context.chain_accumulated is True
    assert context.target_ref == "release"
    assert context.target_head == target_head
    assert context.changed_files == ("mid.py", "tip.py")


def test_linked_chain_without_frozen_target_fails_closed(
    kanban_home, tmp_path
):
    repo = _repo(tmp_path)
    with kb.connect() as conn:
        root = kb.create_task(
            conn,
            title="missing target",
            assignee="verifier",
            workspace_path=str(repo),
            triage=True,
        )
        children = kb.decompose_triage_task(
            conn,
            root,
            root_assignee="verifier",
            children=[
                {
                    "title": "one",
                    "assignee": "coder",
                    "kind": "code",
                    "parents": [],
                },
                {
                    "title": "two",
                    "assignee": "coder",
                    "kind": "code",
                    "parents": [0],
                },
            ],
            author="test",
        )
        assert children is not None
        assert kb.claim_task(conn, children[0]) is not None
        context = policy.build_review_context(conn, children[0])

    assert context.evidence_complete is False
    assert context.evidence_error == "frozen_merge_target_missing"
    assert context.can_defer is False


def test_prior_defer_makes_accumulated_snapshot_mandatory(
    kanban_home, tmp_path, monkeypatch
):
    repo = _repo(tmp_path)
    with kb.connect() as conn:
        _root, first, tip = _linked_children(conn, repo, merge_target="main")
        with kb.write_txn(conn):
            kb._append_event(
                conn,
                first,
                "review_deferred_to_tip",
                {"review_chain_kind": "linked"},
            )
            conn.execute(
                "UPDATE tasks SET status = 'done' WHERE id = ?",
                (first,),
            )
        real_git = policy._git

        def fail_accumulated_path_scan(workspace, *args):
            if args[:2] == ("diff", "--name-only"):
                return None
            return real_git(workspace, *args)

        monkeypatch.setattr(policy, "_git", fail_accumulated_path_scan)
        context = policy.build_review_context(conn, tip)
        calls = []

        with pytest.raises(policy.ReviewEvidenceError):
            policy.capture_review_snapshot(
                context,
                conn,
                tip,
                context.run_id,
                capture=lambda *_args, **_kwargs: calls.append(True) or {},
            )

    assert context.accumulated_required is True
    assert context.evidence_error == "changed_path_scan_failed"
    assert calls == []


def test_legacy_cohort_excludes_other_workspace_branch_and_repo(
    kanban_home, tmp_path
):
    repo = _repo(tmp_path)
    _git(repo, "branch", "other-feature")
    branch_workspace = tmp_path / "repo-other-feature"
    _git(repo, "worktree", "add", str(branch_workspace), "other-feature")
    other_repo = tmp_path / "other-repo"
    other_repo.mkdir()
    _git(other_repo, "init", "-b", "main")
    _git(other_repo, "config", "user.email", "review-policy@example.invalid")
    _git(other_repo, "config", "user.name", "review-policy-test")
    (other_repo / "README.md").write_text("other\n", encoding="utf-8")
    _git(other_repo, "add", "-A")
    _git(other_repo, "commit", "-m", "base")
    source = "/tmp/branch-safe-plan.md"

    with kb.connect() as conn:
        current = kb.create_task(
            conn,
            title="current branch",
            assignee="coder",
            workspace_path=str(repo),
        )
        other = kb.create_task(
            conn,
            title="other repository",
            assignee="coder",
            workspace_path=str(other_repo),
        )
        other_branch = kb.create_task(
            conn,
            title="other branch",
            assignee="coder",
            workspace_path=str(branch_workspace),
        )
        conn.execute(
            "UPDATE tasks SET planspec_source = ? WHERE id IN (?, ?, ?)",
            (source, current, other, other_branch),
        )
        conn.commit()
        assert kb.claim_task(conn, current) is not None
        context = policy.build_review_context(conn, current)

    assert context.chain_kind == "solo"
    assert context.member_ids == (current,)
    assert context.can_defer is False


def test_legacy_defer_requires_deferred_candidate_in_tip_lineage(
    kanban_home, tmp_path
):
    repo = _repo(tmp_path)
    source = "/tmp/lineage-plan.md"
    unrelated = _git(
        repo,
        "commit-tree",
        _git(repo, "rev-parse", "HEAD^{tree}"),
        "-m",
        "unrelated",
    )
    with kb.connect() as conn:
        first = kb.create_task(
            conn,
            title="first",
            assignee="coder",
            workspace_path=str(repo),
        )
        tip = kb.create_task(
            conn,
            title="tip",
            assignee="coder",
            workspace_path=str(repo),
        )
        conn.execute(
            "UPDATE tasks SET planspec_source = ? WHERE id IN (?, ?)",
            (source, first, tip),
        )
        conn.commit()
        assert kb.claim_task(conn, first) is not None
        first_context = policy.build_review_context(conn, first)
        payload = policy.defer_event_payload(first_context, {"passed": True})
        payload["diff_candidate_commit"] = unrelated
        with kb.write_txn(conn):
            kb._append_event(
                conn,
                first,
                "review_deferred_to_tip",
                payload,
            )
            conn.execute(
                "UPDATE tasks SET status = 'done' WHERE id = ?",
                (first,),
            )
        assert kb.claim_task(conn, tip) is not None
        context = policy.build_review_context(conn, tip)

    assert context.accumulated_required is True
    assert context.evidence_complete is False
    assert context.evidence_error == "legacy_defer_candidate_not_ancestor"


def test_reingested_legacy_planspec_ignores_stale_defer_cohort(
    kanban_home, tmp_path
):
    repo = _repo(tmp_path)
    source = "/tmp/reingested-plan.md"
    with kb.connect() as conn:
        old_first = kb.create_task(
            conn,
            title="old one",
            assignee="coder",
            workspace_path=str(repo),
        )
        old_tip = kb.create_task(
            conn,
            title="old two",
            assignee="coder",
            workspace_path=str(repo),
        )
        conn.execute(
            "UPDATE tasks SET planspec_source = ? WHERE id IN (?, ?)",
            (source, old_first, old_tip),
        )
        conn.commit()
        assert kb.claim_task(conn, old_first) is not None
        old_context = policy.build_review_context(conn, old_first)
        with kb.write_txn(conn):
            kb._append_event(
                conn,
                old_first,
                "review_deferred_to_tip",
                {
                    "review_chain_kind": "legacy_active",
                    "review_cohort_member_ids": [old_first, old_tip],
                    "review_repo_identity": old_context.repo_identity,
                    "diff_base_commit": old_context.baseline_commit,
                },
            )
            conn.execute(
                "UPDATE tasks SET status = 'done' WHERE id IN (?, ?)",
                (old_first, old_tip),
            )

        new_first = kb.create_task(
            conn,
            title="new one",
            assignee="coder",
            workspace_path=str(repo),
        )
        new_tip = kb.create_task(
            conn,
            title="new two",
            assignee="coder",
            workspace_path=str(repo),
        )
        conn.execute(
            "UPDATE tasks SET planspec_source = ? WHERE id IN (?, ?)",
            (source, new_first, new_tip),
        )
        conn.commit()
        assert kb.claim_task(conn, new_first) is not None
        new_context = policy.build_review_context(conn, new_first)

    assert new_context.evidence_complete is True
    assert new_context.chain_kind == "legacy_active"
    assert new_context.chain_accumulated is False
    assert set(new_context.member_ids) == {new_first, new_tip}
