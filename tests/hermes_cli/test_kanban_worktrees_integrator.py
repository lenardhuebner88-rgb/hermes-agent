"""Kanban worktrees tests: integrator.

Split from test_kanban_worktrees.py (pure move; no test logic changes).
"""

from __future__ import annotations

import inspect
import json
import os
import subprocess
from pathlib import Path
from types import SimpleNamespace
import pytest
from hermes_cli import kanban_db as kb
from hermes_cli import kanban_worktrees as kwt

from tests.hermes_cli._kanban_test_helpers import (
    _git,
    _commit_in,
    _insert_ended_run,
    _ok_gate,
    _provisioned_chain,
)

@pytest.fixture
def kanban_home(tmp_path, monkeypatch):
    home = tmp_path / ".hermes"
    home.mkdir()
    # Kanban workers inherit dispatcher pins for the live board. Tests must
    # explicitly clear them before resolving kanban_db_path(), otherwise a
    # worker-run pytest can write fixture tasks into /home/piet/.hermes/kanban.db.
    for key in list(os.environ):
        if key.startswith("HERMES_KANBAN_"):
            monkeypatch.delenv(key, raising=False)
    monkeypatch.setenv("HERMES_HOME", str(home))
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    db_path = kb.kanban_db_path(board="default")
    live_db = Path("/home/piet/.hermes/kanban.db").resolve()
    assert db_path.resolve() != live_db
    assert home.resolve() in db_path.resolve().parents
    kb._INITIALIZED_PATHS.discard(str(db_path.resolve()))
    kb.init_db()
    return home


@pytest.fixture
def repo(tmp_path):
    """Real git repo on branch ``main`` with one base commit."""
    r = tmp_path / "repo"
    r.mkdir()
    _git(r, "init", "-b", "main")
    _git(r, "config", "user.email", "t@example.com")
    _git(r, "config", "user.name", "tester")
    (r / "a.txt").write_text("base\n")
    (r / "web").mkdir()
    (r / "web" / "index.txt").write_text("web\n")
    _git(r, "add", "-A")
    _git(r, "commit", "-m", "base")
    return r


def _red_gate_web(_repo, _files):
    """Stub mimicking a real ``tsc -b`` failure label — the incident shape
    (t_2fa852c6): AutoReleaseTile.test.tsx is an untracked foreign file."""
    return False, "tsc -b: exit 2\nerror TS2345 in AutoReleaseTile.test.tsx"


def _red_gate_python(_repo, _files):
    """Stub mimicking a real ``pytest[N]`` failure label."""
    return False, "pytest[1]: exit 1\nFAILED tests/hermes_cli/test_wip_broken.py"


@pytest.mark.parametrize(
    "reason",
    [
        "live checkout has an operation in progress (MERGE_HEAD)",
        "checked-out branch 'other-branch' != frozen merge target 'main'",
        "worktree has uncommitted changes but no commits to merge",
        "chain worktree has uncommitted changes: uncommitted.py",
        "dirty files in live checkout overlap the branch diff: a.txt",
        "chain worktree missing before rebase",
    ],
)
def test_integration_park_class_marks_transient_reasons(reason):
    assert kwt._integration_park_class(reason) == "transient"


@pytest.mark.parametrize(
    "reason",
    [
        "merge conflict/failure (aborted): conflict details",
        "post-merge gate failed: ruff failed",
    ],
)
def test_integration_park_class_marks_orchestrator_reasons(reason):
    assert kwt._integration_park_class(reason) == "needs_orchestrator"


@pytest.mark.parametrize(
    "reason",
    [
        "cannot inspect live checkout: rev-parse failed",
        "some unexpected integrator failure",
        "",
    ],
)
def test_integration_park_class_marks_operator_reasons(reason):
    assert kwt._integration_park_class(reason) == "needs_operator"


@pytest.mark.parametrize(
    ("reason", "expected"),
    [
        # As stored by _park_integration (kanban_db.py): the raw integrator
        # reason gets an "integration parked: " prefix. The retry lane reads
        # this stored form, so the classifier must strip it before matching.
        (
            "integration parked: dirty files in live checkout overlap the "
            "branch diff: a.txt",
            "transient",
        ),
        (
            "integration parked: merge conflict/failure (aborted): boom",
            "needs_orchestrator",
        ),
        (
            "integration parked: cannot inspect live checkout: rev-parse failed",
            "needs_operator",
        ),
    ],
)
def test_integration_park_class_strips_stored_prefix(reason, expected):
    assert kwt._integration_park_class(reason) == expected


def test_integrate_merges_no_ff_and_cleans_up(repo):
    info = _provisioned_chain(repo, "t_m1")
    validated_heads = []

    def gate(validation_root, _files):
        validated_heads.append(_git(validation_root, "rev-parse", "HEAD"))
        return True, "stub gate"

    out = kwt.integrate_chain(
        repo, info["path"], info["branch"], "main", gate_runner=gate,
    )
    assert out["action"] == "merged"
    assert validated_heads == [out["merge_commit"]]
    # --no-ff: HEAD is a real merge commit with two parents.
    parents = _git(repo, "rev-list", "--parents", "-n", "1", "HEAD").split()
    assert len(parents) == 3
    assert (repo / "feature.py").read_text() == "VALUE = 1\n"
    # Worktree and branch are gone.
    assert not info["path"].exists()
    assert "kanban/t_m1" not in _git(repo, "branch", "--list", "kanban/*")


def test_two_chains_two_separate_merge_commits(repo):
    a = _provisioned_chain(repo, "t_a", relpath="a_mod.py")
    b = _provisioned_chain(repo, "t_b", relpath="b_mod.py")
    out_a = kwt.integrate_chain(repo, a["path"], a["branch"], "main",
                                gate_runner=_ok_gate)
    out_b = kwt.integrate_chain(repo, b["path"], b["branch"], "main",
                                gate_runner=_ok_gate)
    assert out_a["action"] == "merged"
    assert out_b["action"] == "merged"
    assert out_a["merge_commit"] != out_b["merge_commit"]
    merges = _git(repo, "log", "--merges", "--oneline").splitlines()
    assert len(merges) == 2


def test_finalreview_integrates_approved_salvage_commit_not_card_branch(
    repo, kanban_home,
):
    """Regression: a salvage worker ran in chain worktree A via ``dir:A``,
    while its final-review card was later materialized on card branch B.

    The approved completion metadata points at A.  B must not be mistaken for
    the approved code merely because it is the completing task's current
    workspace/branch.
    """
    approved = _provisioned_chain(
        repo, "t_chain_a", relpath="approved.py", content="APPROVED = True\n",
    )
    card = _provisioned_chain(
        repo, "t_salvage_b", relpath="card_only.py", content="WRONG = True\n",
    )
    approved_commit = _git(approved["path"], "rev-parse", "HEAD")

    with kb.connect() as conn:
        salvage_id = kb.create_task(
            conn,
            title="salvage fix",
            assignee="coder",
            workspace_kind="dir",
            workspace_path=str(approved["path"]),
        )
        _insert_ended_run(
            conn,
            salvage_id,
            profile="coder",
            metadata={
                "commit": approved_commit,
                "workspace_path": str(approved["path"]),
            },
        )
        conn.execute(
            "UPDATE tasks SET status = 'done' WHERE id = ?", (salvage_id,),
        )
        finalreview_id = kb.create_task(
            conn,
            title="salvage finalreview",
            assignee="reviewer",
            parents=[salvage_id],
            workspace_kind="dir",
            workspace_path=str(card["path"]),
        )
        # Reproduce the bad final-review rematerialization: the review card
        # points at branch B, while its approved parent actually ran in dir:A.
        conn.execute(
            "UPDATE tasks SET status = 'done' WHERE id = ?",
            (finalreview_id,),
        )
        conn.commit()

        out = kwt.maybe_integrate_on_complete(
            conn,
            finalreview_id,
            completion_metadata={"review_verdict": "APPROVED"},
            gate_runner=_ok_gate,
        )

    assert out is not None and out["action"] == "merged"
    assert out["approved_commit"] == approved_commit
    assert (repo / "approved.py").read_text() == "APPROVED = True\n"
    assert not (repo / "card_only.py").exists()
    assert not approved["path"].exists()
    # The unrelated card worktree/branch was not selected or cleaned up.
    assert card["path"].exists()
    assert _git(repo, "rev-parse", card["branch"])


def test_finalreview_parks_on_ambiguous_approved_chain_commits(repo, kanban_home):
    """Restfix 3/5: two DISTINCT, divergent approved commits live in the same
    parent/salvage chain (branch A and branch C), while the finalreview card
    closes on an unrelated branch B.  The finalizer must NOT pick one branch
    arbitrarily — it fails closed and parks, merging nothing."""
    branch_a = _provisioned_chain(
        repo, "t_amb_a", relpath="a_code.py", content="A = 1\n",
    )
    branch_c = _provisioned_chain(
        repo, "t_amb_c", relpath="c_code.py", content="C = 1\n",
    )
    card = _provisioned_chain(
        repo, "t_amb_b", relpath="card.py", content="B = 1\n",
    )
    commit_a = _git(branch_a["path"], "rev-parse", "HEAD")
    commit_c = _git(branch_c["path"], "rev-parse", "HEAD")
    assert commit_a != commit_c

    with kb.connect() as conn:
        salvage_id = kb.create_task(
            conn,
            title="salvage fix",
            assignee="coder",
            workspace_kind="dir",
            workspace_path=str(branch_a["path"]),
        )
        # Two completed runs in the SAME chain recording divergent approved
        # commits on different code branches — the ambiguity to fail closed on.
        _insert_ended_run(
            conn,
            salvage_id,
            profile="coder",
            metadata={"commit": commit_a, "workspace_path": str(branch_a["path"])},
        )
        _insert_ended_run(
            conn,
            salvage_id,
            profile="coder",
            metadata={"commit": commit_c, "workspace_path": str(branch_c["path"])},
        )
        conn.execute(
            "UPDATE tasks SET status = 'done' WHERE id = ?", (salvage_id,),
        )
        finalreview_id = kb.create_task(
            conn,
            title="salvage finalreview",
            assignee="reviewer",
            parents=[salvage_id],
            workspace_kind="dir",
            workspace_path=str(card["path"]),
        )
        conn.execute(
            "UPDATE tasks SET status = 'done' WHERE id = ?", (finalreview_id,),
        )
        conn.commit()

        out = kwt.maybe_integrate_on_complete(
            conn,
            finalreview_id,
            completion_metadata={"review_verdict": "APPROVED"},
            gate_runner=_ok_gate,
        )

    assert out is not None and out["action"] == "parked"
    assert "ambiguous" in out["reason"].lower()
    # Nothing merged; both code branches and the card worktree survive intact.
    assert _git(repo, "log", "--merges", "--oneline") == ""
    assert not (repo / "a_code.py").exists()
    assert not (repo / "c_code.py").exists()
    assert branch_a["path"].exists()
    assert branch_c["path"].exists()
    assert card["path"].exists()


def test_rebased_review_stamps_resolve_to_chain_tip(repo):
    """Nacht M5.1: pre-rebase stamps replaced patch-for-patch are not divergent."""
    info = _provisioned_chain(
        repo, "t_rebased_stamps", relpath="first.py", content="FIRST = 1\n",
    )
    first_pre_rebase = _git(info["path"], "rev-parse", "HEAD")
    _commit_in(info["path"], "second.py", "SECOND = 2\n", msg="second candidate")
    second_pre_rebase = _git(info["path"], "rev-parse", "HEAD")

    # Advance main so rebasing replaces both stamped SHAs without changing
    # either patch. This is the t_6fd680c4 / runs 7612+7616 incident shape.
    _commit_in(repo, "unrelated.txt", "main advanced\n", msg="advance main")
    _git(info["path"], "rebase", "main")

    assert not kwt._branch_is_ancestor(repo, first_pre_rebase, info["branch"])
    assert not kwt._branch_is_ancestor(repo, second_pre_rebase, info["branch"])
    assert kwt._select_override_source(
        repo,
        info["branch"],
        [{"commit": second_pre_rebase}, {"commit": first_pre_rebase}],
    ) is None


def test_approved_commit_resolution_fails_closed_for_ambiguous_branches(repo):
    approved_commit = _git(repo, "rev-parse", "HEAD")
    for root_id in ("t_ambiguous_a", "t_ambiguous_b"):
        branch = f"kanban/{root_id}"
        path = repo / ".worktrees" / "kanban" / root_id
        path.parent.mkdir(parents=True, exist_ok=True)
        _git(repo, "branch", branch, approved_commit)
        _git(repo, "worktree", "add", str(path), branch)

    with pytest.raises(
        kwt.WorktreeError,
        match="exactly one kanban worktree/branch.*candidates=2",
    ):
        getattr(kwt, "_approved_source_worktree")(
            repo, {"commit": approved_commit},
        )


def test_dirty_files_reports_full_path_of_unstaged_first_entry(repo):
    """Regression: a single unstaged modification must report its FULL path.

    ``git status --porcelain -z`` renders an unstaged change as ``" M a.txt\0"``
    — a leading space in the status column. ``dirty_files`` must not let that
    leading space be stripped away (it would shift the parse and drop the first
    character of the path, e.g. ``a.txt`` -> ``.txt``), or the overlap pre-check
    silently misses real dirty-overlaps and a transient park misclassifies as a
    merge conflict.
    """
    (repo / "a.txt").write_text("foreign edit\n")
    assert kwt.dirty_files(repo) == ["a.txt"]
    # A second dirty file must still parse correctly regardless of ordering.
    (repo / "z_new.txt").write_text("new\n")
    assert set(kwt.dirty_files(repo)) == {"a.txt", "z_new.txt"}


def test_overlap_with_dirty_live_checkout_parks(repo):
    info = _provisioned_chain(repo, "t_ovl", relpath="a.txt",
                              content="branch change\n")
    # Foreign uncommitted edit of the SAME file in the live checkout.
    (repo / "a.txt").write_text("manual session edit\n")
    out = kwt.integrate_chain(repo, info["path"], info["branch"], "main",
                              gate_runner=_ok_gate)
    assert out["action"] == "parked"
    # Be specific: the park must be the OVERLAP pre-check, not an incidental
    # "overlap" substring leaking in from the tmp repo path inside a merge-error
    # reason. That coincidence masked a real dirty_files parse bug before.
    assert out["reason"].startswith(
        "dirty files in live checkout overlap the branch diff:"
    )
    assert "a.txt" in out["reason"]
    # Nothing merged; the manual edit is untouched.
    assert (repo / "a.txt").read_text() == "manual session edit\n"
    assert _git(repo, "log", "--merges", "--oneline") == ""


def test_nonoverlapping_dirty_file_does_not_park(repo):
    """Entscheidung 2: overlap check only — foreign dirty files OUTSIDE the
    branch diff don't block the merge."""
    info = _provisioned_chain(repo, "t_novl", relpath="feature.py")
    (repo / "unrelated.txt").write_text("manual session\n")
    out = kwt.integrate_chain(repo, info["path"], info["branch"], "main",
                              gate_runner=_ok_gate)
    assert out["action"] == "merged"
    assert (repo / "unrelated.txt").read_text() == "manual session\n"


def test_rebase_conflict_aborts_and_returns_to_coder(repo):
    # B1: the pre-merge rebase catches the conflict FIRST (before the merge), so
    # a branch that conflicts with the advanced main is routed back to the coder
    # via a ``rebase_conflict`` outcome instead of a silent ``parked``.
    info = _provisioned_chain(repo, "t_cfl", relpath="a.txt",
                              content="branch version\n")
    _commit_in(repo, "a.txt", "main version\n", msg="conflicting main commit")
    head_before = _git(repo, "rev-parse", "HEAD")
    out = kwt.integrate_chain(repo, info["path"], info["branch"], "main",
                              gate_runner=_ok_gate)
    assert out["action"] == "rebase_conflict"
    assert "conflict" in out["reason"]
    assert out["target"] == "main"
    # main HEAD unchanged (the rebase ran in the chain worktree; the merge
    # never ran), no MERGE_HEAD left behind.
    assert _git(repo, "rev-parse", "HEAD") == head_before
    git_dir = Path(_git(repo, "rev-parse", "--absolute-git-dir"))
    assert not (git_dir / "MERGE_HEAD").exists()
    # Rebase aborted cleanly: no rebase state in the chain worktree, tree clean.
    wt_git_dir = Path(_git(info["path"], "rev-parse", "--absolute-git-dir"))
    assert not (wt_git_dir / "rebase-merge").exists()
    assert not (wt_git_dir / "rebase-apply").exists()
    assert _git(info["path"], "status", "--porcelain") == ""


def test_rebase_onto_advanced_main_then_merges(repo):
    # B1: main advances with an unrelated, non-overlapping commit AFTER the chain
    # branched. The pre-merge rebase replays the chain onto the new main, so the
    # merge lands cleanly and history contains BOTH commits (no conflict, no park).
    info = _provisioned_chain(repo, "t_ff", relpath="feature.py",
                              content="VALUE = 1\n")
    _commit_in(repo, "unrelated.txt", "advanced\n", msg="unrelated main commit")
    out = kwt.integrate_chain(repo, info["path"], info["branch"], "main",
                              gate_runner=_ok_gate)
    assert out["action"] == "merged"
    assert out.get("merge_commit")
    log = _git(repo, "log", "--oneline")
    assert "unrelated main commit" in log
    assert (repo / "feature.py").exists()
    assert (repo / "unrelated.txt").exists()


def test_target_mismatch_parks(repo):
    info = _provisioned_chain(repo, "t_tgt")
    _git(repo, "checkout", "-b", "other-branch")
    out = kwt.integrate_chain(repo, info["path"], info["branch"], "main",
                              gate_runner=_ok_gate)
    assert out["action"] == "parked"
    assert "frozen merge target" in out["reason"]
    assert _git(repo, "log", "--merges", "--oneline") == ""


def test_operation_in_progress_parks(repo):
    info = _provisioned_chain(repo, "t_oip")
    git_dir = Path(_git(repo, "rev-parse", "--absolute-git-dir"))
    (git_dir / "MERGE_HEAD").write_text(_git(repo, "rev-parse", "HEAD") + "\n")
    try:
        out = kwt.integrate_chain(repo, info["path"], info["branch"], "main",
                                  gate_runner=_ok_gate)
    finally:
        (git_dir / "MERGE_HEAD").unlink()
    assert out["action"] == "parked"
    assert "operation in progress" in out["reason"]


def test_red_gate_reverts_merge_and_parks(repo):
    info = _provisioned_chain(repo, "t_red", relpath="breaks.py")
    validation_roots = []

    def red_gate(validation_root, _files):
        validation_roots.append(Path(validation_root))
        return False, "stub gate red"

    out = kwt.integrate_chain(repo, info["path"], info["branch"], "main",
                              gate_runner=red_gate)
    assert out["action"] == "parked"
    assert "post-merge gate failed" in out["reason"]
    assert out["reverted"] is True
    # The merge commit exists in history but its content is reverted.
    merges = _git(repo, "log", "--merges", "--oneline").splitlines()
    assert len(merges) == 1
    assert not (repo / "breaks.py").exists()
    # Live branch stays provably green: HEAD is the revert commit.
    head_subject = _git(repo, "log", "-1", "--format=%s")
    assert head_subject.startswith("Revert")
    assert validation_roots and all(not root.exists() for root in validation_roots)


def test_foreign_dirty_web_file_is_absent_from_clean_validation_worktree(repo):
    """A foreign live-checkout web file cannot create a false-red gate."""
    info = _provisioned_chain(repo, "t_fdc_web", relpath="feature.py")
    foreign = repo / "web" / "src" / "control" / "AutoReleaseTile.test.tsx"
    foreign.parent.mkdir(parents=True, exist_ok=True)
    foreign.write_text("// half-finished foreign test\n")
    validation_roots = []

    def gate(validation_root, _files):
        validation_roots.append(Path(validation_root))
        contaminated = (
            Path(validation_root)
            / "web/src/control/AutoReleaseTile.test.tsx"
        ).exists()
        return (not contaminated, "clean" if not contaminated else "contaminated")

    out = kwt.integrate_chain(repo, info["path"], info["branch"], "main",
                              gate_runner=gate)
    assert out["action"] == "merged"
    assert validation_roots and all(root != repo for root in validation_roots)
    assert all(not root.exists() for root in validation_roots)
    assert foreign.read_text() == "// half-finished foreign test\n"


def test_foreign_dirty_python_file_is_absent_from_clean_validation_worktree(repo):
    """A foreign live-checkout Python test cannot create a false-red gate."""
    info = _provisioned_chain(repo, "t_fdc_py", relpath="feature.py")
    foreign = repo / "tests" / "hermes_cli" / "test_wip_broken.py"
    foreign.parent.mkdir(parents=True, exist_ok=True)
    foreign.write_text("def test_x():\n    assert False\n")
    validation_roots = []

    def gate(validation_root, _files):
        validation_roots.append(Path(validation_root))
        contaminated = (
            Path(validation_root) / "tests/hermes_cli/test_wip_broken.py"
        ).exists()
        return (not contaminated, "clean" if not contaminated else "contaminated")

    out = kwt.integrate_chain(repo, info["path"], info["branch"], "main",
                              gate_runner=gate)
    assert out["action"] == "merged"
    assert validation_roots and all(not root.exists() for root in validation_roots)
    assert foreign.exists()


def test_red_gate_without_foreign_dirty_keeps_generic_classification(repo):
    """DONE-WHEN (b) regression: a red gate with NO foreign dirty files in
    the failing stage's scope keeps today's generic 'post-merge gate failed'
    park + revert, byte-identical to test_red_gate_reverts_merge_and_parks."""
    info = _provisioned_chain(repo, "t_red_clean", relpath="breaks.py")
    out = kwt.integrate_chain(repo, info["path"], info["branch"], "main",
                              gate_runner=_red_gate_web)
    assert out["action"] == "parked"
    assert out["reason"].startswith("post-merge gate failed:")
    assert "park_class" not in out
    assert out["reverted"] is True


def test_foreign_dirty_web_file_cannot_contaminate_green_gate(repo):
    """A green result now proves a clean commit checkout, not annotated dirt."""
    info = _provisioned_chain(
        repo, "t_fdc_green", relpath="web/src/control/Foo.tsx",
        content="export const x = 1;\n",
    )
    foreign = repo / "web" / "src" / "control" / "AutoReleaseTile.test.tsx"
    foreign.parent.mkdir(parents=True, exist_ok=True)
    foreign.write_text("// half-finished foreign test\n")
    out = kwt.integrate_chain(repo, info["path"], info["branch"], "main",
                              gate_runner=_ok_gate)
    assert out["action"] == "merged"
    assert "gate_environment" not in out
    assert "foreign_dirty_files" not in out
    assert foreign.exists()


def test_clean_checkout_green_has_no_gate_environment_flag(repo):
    """DONE-WHEN (4) regression: a genuinely clean checkout must not gain the
    additive gate_environment metadata — identical behavior to today."""
    info = _provisioned_chain(repo, "t_clean_green", relpath="feature.py")
    out = kwt.integrate_chain(repo, info["path"], info["branch"], "main",
                              gate_runner=_ok_gate)
    assert out["action"] == "merged"
    assert "gate_environment" not in out
    assert "foreign_dirty_files" not in out


def test_overlapping_dirty_file_parks_by_overlap_not_foreign_dirty_checkout(repo):
    """DONE-WHEN (d) regression: a dirty file that OVERLAPS the branch diff
    still parks via the pre-existing overlap pre-check (a), unaffected by the
    new foreign-dirty-checkout classification introduced above."""
    info = _provisioned_chain(repo, "t_ovl_regression", relpath="a.txt",
                              content="branch change\n")
    (repo / "a.txt").write_text("manual session edit\n")
    out = kwt.integrate_chain(repo, info["path"], info["branch"], "main",
                              gate_runner=_red_gate_web)
    assert out["action"] == "parked"
    assert out["reason"].startswith(
        "dirty files in live checkout overlap the branch diff:"
    )
    assert "park_class" not in out


def test_reverted_merge_is_reintegrated_not_clean(repo):
    info = _provisioned_chain(repo, "t_reverted", relpath="restored.py")
    gate_results = iter([(False, "first gate failed"), (True, "gate ok")])

    out1 = kwt.integrate_chain(
        repo,
        info["path"],
        info["branch"],
        "main",
        gate_runner=lambda _repo, _files: next(gate_results),
    )

    assert out1["action"] == "parked"
    assert out1["gate_output"] == "first gate failed"
    assert kwt._branch_is_ancestor(repo, info["branch"], "main") is True
    assert not (repo / "restored.py").exists()

    out2 = kwt.integrate_chain(
        repo,
        info["path"],
        info["branch"],
        "main",
        gate_runner=lambda _repo, _files: next(gate_results),
    )

    assert out2["action"] == "merged"
    assert out2["reintegrated_after_revert"] is True
    assert out2["original_merge_commit"] == out1["merge_commit"]
    assert "revert_commit" in out2
    assert (repo / "restored.py").read_text() == "VALUE = 1\n"
    assert not info["path"].exists()


def test_reverted_ancestor_is_replayed_with_later_branch_commit(repo):
    """A later B commit must not make a reverted, reviewed A look integrated."""
    info = _provisioned_chain(
        repo, "t_reverted_ancestor", relpath="acceptance.py",
        content="ACCEPTED = True\n",
    )
    accepted_commit = _git(info["path"], "rev-parse", "HEAD")
    first = kwt.integrate_chain(
        repo, info["path"], info["branch"], "main", gate_runner=_ok_gate,
    )
    assert first["action"] == "merged"

    _git(repo, "revert", "-m", "1", "--no-edit", first["merge_commit"])
    _git(repo, "branch", info["branch"], accepted_commit)
    _git(repo, "worktree", "add", str(info["path"]), info["branch"])
    _commit_in(info["path"], "hardening.py", "HARDENED = True\n", "B")

    gated_files = []

    def recording_gate(_repo, files):
        gated_files.extend(files)
        return True, "recorded"

    out = kwt.integrate_chain(
        repo, info["path"], info["branch"], "main", gate_runner=recording_gate,
    )

    assert out["action"] == "merged"
    assert (repo / "acceptance.py").read_text() == "ACCEPTED = True\n"
    assert (repo / "hardening.py").read_text() == "HARDENED = True\n"
    assert set(out["changed_files"]) == {"acceptance.py", "hardening.py"}
    assert set(gated_files) == {"acceptance.py", "hardening.py"}


def test_branch_created_after_revert_does_not_restore_unrelated_merge(repo):
    info = _provisioned_chain(repo, "t_old", relpath="old.py", content="OLD = True\n")
    first = kwt.integrate_chain(
        repo, info["path"], info["branch"], "main", gate_runner=_ok_gate,
    )
    _git(repo, "revert", "-m", "1", "--no-edit", first["merge_commit"])

    later = _provisioned_chain(
        repo, "t_later", relpath="later.py", content="LATER = True\n",
    )
    out = kwt.integrate_chain(
        repo, later["path"], later["branch"], "main", gate_runner=_ok_gate,
    )

    assert out["action"] == "merged"
    assert not (repo / "old.py").exists()
    assert out["changed_files"] == ["later.py"]


def test_reverted_ancestor_scan_ignores_history_already_in_branch(repo, monkeypatch):
    """A fresh worker branch must not rescan every historical merge/revert."""
    _git(repo, "checkout", "-b", "historical-worker")
    _commit_in(repo, "historical.py", "HISTORICAL = True\n", "historical")
    _git(repo, "checkout", "main")
    _git(repo, "merge", "--no-ff", "--no-edit", "historical-worker")
    historical_merge = _git(repo, "rev-parse", "HEAD")
    _git(repo, "revert", "-m", "1", "--no-edit", historical_merge)
    info = _provisioned_chain(
        repo, "t_after_history", relpath="current.py", content="CURRENT = True\n",
    )

    real_git = kwt._git
    calls = []

    def recording_git(root, *args, **kwargs):
        calls.append(args)
        return real_git(root, *args, **kwargs)

    monkeypatch.setattr(kwt, "_git", recording_git)

    assert kwt._reverted_merged_ancestor(repo, info["branch"], "main") is None
    assert not any(any(str(arg).startswith("--grep=") for arg in call) for call in calls)
    assert any(
        call[:3] == ("rev-list", "--first-parent", "--merges")
        and "..main" in str(call[3])
        for call in calls
    )


def test_reintegration_gate_uses_clean_validation_worktree(repo):
    """The revert-of-revert gate is isolated from later foreign live WIP."""
    info = _provisioned_chain(repo, "t_reint_fdc", relpath="restored.py")
    validation_roots = []
    calls = 0

    def gate(validation_root, _files):
        nonlocal calls
        calls += 1
        validation_roots.append(Path(validation_root))
        if calls == 1:
            return False, "first gate failed"
        contaminated = (
            Path(validation_root)
            / "web/src/control/AutoReleaseTile.test.tsx"
        ).exists()
        return (not contaminated, "clean" if not contaminated else "contaminated")

    out1 = kwt.integrate_chain(
        repo, info["path"], info["branch"], "main",
        gate_runner=gate,
    )
    assert out1["action"] == "parked"
    assert kwt._branch_is_ancestor(repo, info["branch"], "main") is True

    # A foreign session leaves an untracked WIP file behind between the
    # first (generic) park and the second (reintegration) attempt.
    foreign = repo / "web" / "src" / "control" / "AutoReleaseTile.test.tsx"
    foreign.parent.mkdir(parents=True, exist_ok=True)
    foreign.write_text("// half-finished foreign test\n")

    out2 = kwt.integrate_chain(
        repo, info["path"], info["branch"], "main",
        gate_runner=gate,
    )

    assert out2["action"] == "merged"
    assert out2["reintegrated_after_revert"] is True
    assert out2["merge_commit"] == _git(repo, "rev-parse", "HEAD")
    assert all(not root.exists() for root in validation_roots)
    assert foreign.read_text() == "// half-finished foreign test\n"


def test_integration_parked_writes_full_gate_output_comment(kanban_home):
    full_output = "line-000\n" + "x" * 5000 + "\nline-end"
    with kb.connect() as conn:
        tid = kb.create_task(conn, title="gate fail", assignee="coder")
        assert kb._park_integration(
            conn,
            tid,
            {"reason": "post-merge gate failed", "gate_output": full_output},
        )
        body = conn.execute(
            "SELECT body FROM task_comments "
            "WHERE task_id = ? AND author = 'integrator' "
            "ORDER BY created_at DESC LIMIT 1",
            (tid,),
        ).fetchone()["body"]

    assert "Post-merge gate failed; full gate output follows." in body
    assert full_output in body


def test_dirty_chain_worktree_parks(repo):
    info = _provisioned_chain(repo, "t_dwt")
    (info["path"] / "uncommitted.py").write_text("oops = 1\n")
    out = kwt.integrate_chain(repo, info["path"], info["branch"], "main",
                              gate_runner=_ok_gate)
    assert out["action"] == "parked"
    assert out["park_class"] == "DIRTY_WORKTREE"
    assert "DIRTY_WORKTREE" in out["reason"]
    assert "uncommitted" in out["reason"]


def test_artifact_policy_missing_chain_worktree_parks_with_recovery(repo):
    info = _provisioned_chain(repo, "t_artifact_policy")
    wt = info["path"]
    (wt / "coverage").mkdir()
    (wt / "coverage" / "index.html").write_text("<html></html>\n")
    out = kwt.integrate_chain(repo, wt, info["branch"], "main",
                              gate_runner=_ok_gate)
    assert out["action"] == "parked"
    assert out["park_class"] == "ARTIFACT_POLICY_MISSING"
    assert "ARTIFACT_POLICY_MISSING" in out["reason"]
    assert "extend the artifact policy" in out["reason"]


def test_deliverable_md_alone_does_not_block_clean_close(repo):
    info = kwt.ensure_worktree(repo, "t_deliverable")
    (info["path"] / ".deliverable.md").write_text("# handoff\n")

    assert kwt.dirty_files(info["path"]) == []
    out = kwt.integrate_chain(repo, info["path"], info["branch"], "main",
                              gate_runner=_ok_gate)

    assert out["action"] == "clean"
    assert not info["path"].exists()


def test_cache_byproducts_do_not_count_as_dirty(repo):
    """Gate runs write __pycache__/.pytest_cache into the worktree; in repos
    without a .gitignore those must NOT park the chain (live E2E finding
    2026-06-11: verifier's ruff run created util.cpython-311.pyc → park)."""
    info = _provisioned_chain(repo, "t_cache")
    wt = info["path"]
    (wt / "__pycache__").mkdir()
    (wt / "__pycache__" / "feature.cpython-311.pyc").write_bytes(b"\x00")
    (wt / ".pytest_cache").mkdir()
    (wt / ".pytest_cache" / "CACHEDIR.TAG").write_text("tag")
    (wt / "stray.pyc").write_bytes(b"\x00")
    assert kwt.dirty_files(wt) == []
    out = kwt.integrate_chain(repo, wt, info["branch"], "main",
                              gate_runner=_ok_gate)
    assert out["action"] == "merged"
    # A REAL uncommitted file still parks (filter is noise-only).
    info2 = _provisioned_chain(repo, "t_cache2", relpath="other.py")
    (info2["path"] / "__pycache__").mkdir()
    (info2["path"] / "real_leftover.py").write_text("x = 1\n")
    out2 = kwt.integrate_chain(repo, info2["path"], info2["branch"], "main",
                               gate_runner=_ok_gate)
    assert out2["action"] == "parked"
    assert "real_leftover.py" in out2["reason"]


def test_visual_artifacts_are_preserved_then_chain_merges(repo, tmp_path, monkeypatch):
    monkeypatch.setattr(kwt, "_ARTIFACT_RECEIPTS_ROOT", tmp_path / "receipts")
    monkeypatch.setattr(kwt, "_artifact_receipt_timestamp", lambda: "20260621T010203Z")
    info = _provisioned_chain(repo, "t_artifact")
    wt = info["path"]
    (wt / ".playwright-mcp").mkdir()
    (wt / ".playwright-mcp" / "console.log").write_text("[]")
    (wt / ".playwright-mcp" / "page.yml").write_text("a: 1")

    assert sorted(kwt.dirty_files(wt)) == [
        ".playwright-mcp/console.log",
        ".playwright-mcp/page.yml",
    ]
    out = kwt.integrate_chain(repo, wt, info["branch"], "main",
                              gate_runner=_ok_gate)

    assert out["action"] == "merged"
    receipt = out["artifact_receipt"]
    assert receipt["destination"] == str(tmp_path / "receipts" / "t_artifact-20260621T010203Z")
    assert receipt["file_count"] == 2
    assert sorted(receipt["paths"]) == [
        ".playwright-mcp/console.log",
        ".playwright-mcp/page.yml",
    ]
    assert (Path(receipt["destination"]) / ".playwright-mcp" / "console.log").read_text() == "[]"
    assert (repo / "feature.py").read_text() == "VALUE = 1\n"
    assert not wt.exists()


def test_mixed_artifacts_and_source_change_park_without_cleanup(repo, tmp_path, monkeypatch):
    monkeypatch.setattr(kwt, "_ARTIFACT_RECEIPTS_ROOT", tmp_path / "receipts")
    info = _provisioned_chain(repo, "t_mixed")
    wt = info["path"]
    (wt / ".playwright-mcp").mkdir()
    (wt / ".playwright-mcp" / "console.log").write_text("[]")
    (wt / "uncommitted.py").write_text("oops = 1\n")

    out = kwt.integrate_chain(repo, wt, info["branch"], "main",
                              gate_runner=_ok_gate)

    assert out["action"] == "parked"
    assert "uncommitted.py" in out["reason"]
    assert "artifact_receipt" not in out
    assert (wt / ".playwright-mcp" / "console.log").exists()
    assert (wt / "uncommitted.py").exists()
    assert not (tmp_path / "receipts").exists()


def test_artifact_copy_failure_parks_without_deleting(repo, tmp_path, monkeypatch):
    monkeypatch.setattr(kwt, "_ARTIFACT_RECEIPTS_ROOT", tmp_path / "receipts")
    info = _provisioned_chain(repo, "t_copyfail")
    wt = info["path"]
    (wt / ".playwright-mcp").mkdir()
    artifact = wt / ".playwright-mcp" / "console.log"
    artifact.write_text("[]")

    def fail_copy(*_args, **_kwargs):
        raise OSError("boom")

    monkeypatch.setattr(kwt.shutil, "copy2", fail_copy)
    out = kwt.integrate_chain(repo, wt, info["branch"], "main",
                              gate_runner=_ok_gate)

    assert out["action"] == "parked"
    assert out["park_class"] == "ARTIFACT_PRESERVE_FAILED"
    assert "ARTIFACT_PRESERVE_FAILED" in out["reason"]
    assert artifact.read_text() == "[]"


def test_no_commits_is_clean_and_removes_worktree(repo):
    info = kwt.ensure_worktree(repo, "t_empty")
    out = kwt.integrate_chain(repo, info["path"], info["branch"], "main",
                              gate_runner=_ok_gate)
    assert out["action"] == "clean"
    assert not info["path"].exists()
    assert _git(repo, "log", "--merges", "--oneline") == ""


def test_affected_pytest_module_mapping(repo):
    (repo / "tests" / "hermes_cli").mkdir(parents=True)
    (repo / "tests" / "hermes_cli" / "test_kanban_db.py").write_text("")
    (repo / "tests" / "stress").mkdir(parents=True)
    (repo / "tests" / "stress" / "test_atypical_scenarios.py").write_text("")
    mods = kwt._affected_pytest_modules(
        repo,
        ["hermes_cli/kanban_db.py", "hermes_cli/no_tests.py",
         "web/src/x.ts", "tests/hermes_cli/test_kanban_db.py",
         "tests/stress/test_atypical_scenarios.py"],
    )
    # hermes_cli/kanban_db.py -> 1:1 match
    # hermes_cli/no_tests.py -> no 1:1 test -> fallback to tests/hermes_cli/
    # tests/hermes_cli/test_kanban_db.py -> runs itself
    # tests/stress/ skipped
    assert mods == ["tests/hermes_cli/", "tests/hermes_cli/test_kanban_db.py"]


def test_affected_pytest_module_matches_submodule_from_import_sibling(repo):
    (repo / "hermes_cli").mkdir(parents=True)
    (repo / "tests" / "hermes_cli").mkdir(parents=True)
    (repo / "tests" / "hermes_cli" / "test_commands.py").write_text("")
    (repo / "tests" / "hermes_cli" / "test_goals.py").write_text(
        "from hermes_cli.commands import resolve_command\n"
    )

    mods = kwt._affected_pytest_modules(repo, ["hermes_cli/commands.py"])

    assert mods == [
        "tests/hermes_cli/test_commands.py",
        "tests/hermes_cli/test_goals.py",
    ]


def test_affected_pytest_module_fallback_for_monolith(repo):
    """A monolith source file with no 1:1 test selects the package test dir."""
    (repo / "gateway").mkdir(parents=True)
    (repo / "tests" / "gateway").mkdir(parents=True)
    (repo / "tests" / "gateway" / "test_shutdown_cache_cleanup.py").write_text("")
    mods = kwt._affected_pytest_modules(repo, ["gateway/run.py"])
    assert mods == ["tests/gateway/"]


def test_affected_pytest_module_oversize_dir_downgrades(repo):
    """When the package test dir exceeds _FALLBACK_MAX_TEST_FILES, the
    fallback downgrades to no selection — nightly full suite remains the
    backstop (AC-2 counter-metric: no gate-tempo-for-coverage trade)."""
    (repo / "gateway").mkdir(parents=True)
    pkg = repo / "tests" / "gateway"
    pkg.mkdir(parents=True)
    cap = kwt._FALLBACK_MAX_TEST_FILES
    for i in range(cap + 1):
        (pkg / f"test_{i:04d}.py").write_text("")
    mods = kwt._affected_pytest_modules(repo, ["gateway/run.py"])
    assert mods == []


def test_affected_pytest_module_no_fallback_for_root_source(repo):
    """Root-level source without a package dir must not select tests/ root."""
    (repo / "tests").mkdir(parents=True)
    (repo / "tests" / "test_something.py").write_text("")
    mods = kwt._affected_pytest_modules(repo, ["run_agent.py"])
    assert mods == []


def test_affected_pytest_module_selects_root_level_sibling(repo):
    """Feature tests directly at tests/ root are import-scanned too (e.g.
    tests/test_design_board_store.py for hermes_cli/design_board_store.py)."""
    (repo / "hermes_cli").mkdir(parents=True)
    (repo / "tests").mkdir(parents=True)
    (repo / "tests" / "test_design_board_store.py").write_text(
        "from hermes_cli import design_board_store\n"
    )
    mods = kwt._affected_pytest_modules(repo, ["hermes_cli/design_board_store.py"])
    assert mods == ["tests/test_design_board_store.py"]



# ---------------------------------------------------------------------------
# S6.6 Integrator-Härtung: Disk-Preflight + Validation-Hygiene
# ---------------------------------------------------------------------------


def test_check_disk_space_ok_and_raises_clear_enospace(repo, monkeypatch):
    ok = kwt.check_disk_space_for_integration(repo, min_free=1)
    assert ok["free"] >= 1
    assert ok["min_free"] == 1

    class _Usage:
        free = 100
        total = 1000
        used = 900

    monkeypatch.setattr(kwt.shutil, "disk_usage", lambda _p: _Usage())
    with pytest.raises(kwt.DiskSpaceError) as excinfo:
        kwt.check_disk_space_for_integration(repo, min_free=500)
    msg = str(excinfo.value)
    assert "Zu wenig freier Speicher vor Merge-Gate" in msg
    assert "ENOSPC-Preflight" in msg
    assert "kanban-validation" in msg


def test_disk_space_error_is_transient_park_class():
    assert (
        kwt._integration_park_class(
            "Zu wenig freier Speicher vor Merge-Gate: 100 B frei, mindestens 500 B nötig"
        )
        == "transient"
    )


def test_hygiene_sweep_removes_only_system_validation_worktrees(repo, tmp_path):
    base = repo / kwt.WORKTREES_DIRNAME / kwt.VALIDATION_WORKTREES_NAMESPACE
    base.mkdir(parents=True)
    old = base / "old-token-123"
    fresh = base / "fresh-token-456"
    old.mkdir()
    (old / "marker.txt").write_text("stale\n")
    fresh.mkdir()
    (fresh / "marker.txt").write_text("fresh\n")

    # Fremde Pfade — dürfen NIE angefasst werden.
    foreign_kanban = repo / kwt.WORKTREES_DIRNAME / kwt.WORKTREES_NAMESPACE / "t_foreign"
    foreign_kanban.mkdir(parents=True)
    (foreign_kanban / "keep.txt").write_text("chain\n")
    foreign_other = repo / kwt.WORKTREES_DIRNAME / "bridges" / "manual-wt"
    foreign_other.mkdir(parents=True)
    (foreign_other / "keep.txt").write_text("bridge\n")

    now = 1_000_000.0
    # old = 2h alt, fresh = 60s alt; max_age = 30 min
    import os

    os.utime(old, (now - 7200, now - 7200))
    os.utime(fresh, (now - 60, now - 60))

    dry = kwt.hygiene_sweep_validation_worktrees(
        repo, max_age_seconds=1800, now=now, dry_run=True
    )
    assert dry["scanned"] == 2
    assert len(dry["removed"]) == 1
    assert dry["removed"][0]["path"] == str(old)
    assert old.exists() and fresh.exists()

    applied = kwt.hygiene_sweep_validation_worktrees(
        repo, max_age_seconds=1800, now=now, dry_run=False
    )
    assert not old.exists()
    assert fresh.exists()
    assert foreign_kanban.exists()
    assert foreign_other.exists()
    assert any(item["path"] == str(fresh) for item in applied["skipped"])
    assert (foreign_kanban / "keep.txt").read_text() == "chain\n"
    assert (foreign_other / "keep.txt").read_text() == "bridge\n"


def test_is_system_validation_worktree_predicate(repo):
    base = repo / kwt.WORKTREES_DIRNAME / kwt.VALIDATION_WORKTREES_NAMESPACE
    token = base / "pid-1-2-3"
    token.mkdir(parents=True)
    assert kwt._is_system_validation_worktree(repo, token) is True
    chain = repo / kwt.WORKTREES_DIRNAME / kwt.WORKTREES_NAMESPACE / "t_abc"
    chain.mkdir(parents=True)
    assert kwt._is_system_validation_worktree(repo, chain) is False
    assert kwt._is_system_validation_worktree(repo, repo / "elsewhere") is False


def test_integrate_chain_parks_on_disk_preflight(repo, monkeypatch):
    info = _provisioned_chain(repo, "t_disk")

    def _boom(_path, min_free=None):
        raise kwt.DiskSpaceError(
            "Zu wenig freier Speicher vor Merge-Gate: 0 B frei, "
            "mindestens 2.0 GiB nötig (ENOSPC-Preflight)."
        )

    monkeypatch.setattr(kwt, "check_disk_space_for_integration", _boom)
    monkeypatch.setattr(
        kwt,
        "hygiene_sweep_validation_worktrees",
        lambda *a, **k: {"scanned": 0, "removed": [], "skipped": [], "errors": []},
    )
    out = kwt.integrate_chain(
        repo, info["path"], info["branch"], "main", gate_runner=_ok_gate
    )
    assert out["action"] == "parked"
    assert "Zu wenig freier Speicher vor Merge-Gate" in out["reason"]
    assert "ENOSPC-Preflight" in out["reason"]
    # Chain-Worktree bleibt stehen (kein destruktives Cleanup bei Preflight-Park).
    assert info["path"].exists()


def test_prepare_integration_disk_runs_hygiene_then_preflight(repo, monkeypatch):
    calls: list[str] = []

    def _hygiene(root, **_kwargs):
        calls.append("hygiene")
        return {"scanned": 0, "removed": [], "skipped": [], "errors": []}

    def _disk(root, min_free=None):
        calls.append("disk")
        return {"free": 9_999_999_999, "total": 10_000_000_000, "min_free": 1}

    monkeypatch.setattr(kwt, "hygiene_sweep_validation_worktrees", _hygiene)
    monkeypatch.setattr(kwt, "check_disk_space_for_integration", _disk)
    out = kwt.prepare_integration_disk(repo)
    assert calls == ["hygiene", "disk"]
    assert out["disk"]["free"] == 9_999_999_999
