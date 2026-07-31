from __future__ import annotations

import subprocess
from datetime import datetime, timezone
from pathlib import Path

import pytest

from hermes_cli import landing_loop as landing_module
from hermes_cli.landing_loop import (
    BaselineProbe,
    FailureClass,
    LL2Candidate,
    LandingLoop,
    classify_failure,
    main,
    plan_queue,
    render_discord,
)


def git(cwd: Path, *args: str, check: bool = True) -> subprocess.CompletedProcess[str]:
    result = subprocess.run(
        ["git", "-C", str(cwd), *args],
        capture_output=True,
        encoding="utf-8",
        errors="replace",
        check=False,
    )
    if check:
        assert result.returncode == 0, result.stderr or result.stdout
    return result


@pytest.fixture
def git_world(tmp_path):
    repo = tmp_path / "repo"
    repo.mkdir()
    git(repo, "init", "-b", "main")
    git(repo, "config", "user.email", "landing@test")
    git(repo, "config", "user.name", "landing-test")
    (repo / "README.md").write_text("initial\n", encoding="utf-8")
    git(repo, "add", "README.md")
    git(repo, "commit", "-m", "initial")
    loops_root = tmp_path / "loops"
    ledger_dir = tmp_path / "vault-ledger"

    def add_loop(name: str, start: str = "main") -> Path:
        worktree = loops_root / name / "wt"
        worktree.parent.mkdir(parents=True)
        git(repo, "worktree", "add", "-b", f"loop/{name}", str(worktree), start)
        git(worktree, "config", "user.email", "landing@test")
        git(worktree, "config", "user.name", "landing-test")
        return worktree

    def commit_main(name: str, text: str | None = None) -> str:
        path = repo / f"{name}.txt"
        path.write_text(text or f"{name}\n", encoding="utf-8")
        git(repo, "add", path.name)
        git(repo, "commit", "-m", name)
        return git(repo, "rev-parse", "HEAD").stdout.strip()

    def commit_loop(worktree: Path, name: str) -> str:
        path = worktree / f"{name}.txt"
        path.write_text(f"{name}\n", encoding="utf-8")
        git(worktree, "add", path.name)
        git(worktree, "commit", "-m", name)
        return git(worktree, "rev-parse", "HEAD").stdout.strip()

    return repo, loops_root, ledger_dir, add_loop, commit_main, commit_loop


def make_loop(repo: Path, loops_root: Path, ledger_dir: Path, **kwargs) -> LandingLoop:
    fixed = datetime(2026, 7, 29, 4, 0, tzinfo=timezone.utc)
    kwargs.setdefault(
        "baseline_records",
        lambda: [
            {
                "result": "pass",
                "head_sha": git(repo, "rev-parse", "main").stdout.strip(),
            }
        ],
    )
    kwargs.setdefault("landing_baseline_records", lambda: [])
    kwargs.setdefault("landing_evidence_writer", lambda *_args: None)
    return LandingLoop(
        repo,
        loops_root,
        ledger_dir,
        now=lambda: fixed,
        **kwargs,
    )


def outcome(run, branch: str):
    return next(item for item in run.outcomes if item.branch == branch)


def refs(repo: Path) -> str:
    return git(repo, "show-ref").stdout


def test_inventory_records_ahead_and_behind_for_every_loop_branch(git_world):
    repo, loops_root, ledger_dir, add_loop, commit_main, commit_loop = git_world
    stale = add_loop("stale")
    commit_main("main-new")
    active = add_loop("active")
    commit_loop(active, "branch-new")

    run = make_loop(
        repo,
        loops_root,
        ledger_dir,
        dry_run=True,
    ).run()

    inventory = {item.branch: (item.ahead, item.behind) for item in run.inventory}
    assert inventory == {
        "loop/active": (1, 0),
        "loop/stale": (0, 1),
    }
    assert stale.is_dir()


def test_ahead_branch_is_never_reset(git_world):
    repo, loops_root, ledger_dir, add_loop, _commit_main, commit_loop = git_world
    worktree = add_loop("active")
    branch_head = commit_loop(worktree, "branch-work")
    loop = make_loop(
        repo,
        loops_root,
        ledger_dir,
        gate_runner=lambda _repo, _base: (False, "absichtlich rot"),
    )

    run = loop.run()

    assert outcome(run, "loop/active").action == "parked"
    assert git(repo, "rev-parse", "loop/active").stdout.strip() == branch_head
    assert git(repo, "rev-parse", "main").stdout.strip() != branch_head


def test_ahead_zero_with_uncommitted_file_is_parked_without_reset(git_world):
    repo, loops_root, ledger_dir, add_loop, commit_main, _commit_loop = git_world
    worktree = add_loop("modified")
    original_branch = git(repo, "rev-parse", "loop/modified").stdout.strip()
    commit_main("new-main")
    (worktree / "README.md").write_text("uncommitted\n", encoding="utf-8")

    run = make_loop(repo, loops_root, ledger_dir).run()

    item = outcome(run, "loop/modified")
    assert item.action == "parked"
    assert "uncommittete" in item.reason
    assert git(repo, "rev-parse", "loop/modified").stdout.strip() == original_branch
    assert (worktree / "README.md").read_text(encoding="utf-8") == "uncommitted\n"


def test_ahead_zero_with_untracked_file_is_parked_without_reset(git_world):
    repo, loops_root, ledger_dir, add_loop, commit_main, _commit_loop = git_world
    worktree = add_loop("untracked")
    original_branch = git(repo, "rev-parse", "loop/untracked").stdout.strip()
    commit_main("new-main")
    untracked = worktree / "keep-me.txt"
    untracked.write_text("important\n", encoding="utf-8")

    run = make_loop(repo, loops_root, ledger_dir).run()

    item = outcome(run, "loop/untracked")
    assert item.action == "parked"
    assert "untrackte" in item.reason
    assert git(repo, "rev-parse", "loop/untracked").stdout.strip() == original_branch
    assert untracked.read_text(encoding="utf-8") == "important\n"


def test_commit_added_between_inventory_and_reset_prevents_reset(git_world):
    repo, loops_root, ledger_dir, add_loop, commit_main, commit_loop = git_world
    worktree = add_loop("racing")
    commit_main("new-main")
    committed: list[str] = []

    def race(_inventory):
        committed.append(commit_loop(worktree, "arrived-during-run"))

    run = make_loop(repo, loops_root, ledger_dir).run(after_inventory=race)

    item = outcome(run, "loop/racing")
    assert item.action == "parked"
    assert "ahead seit Inventur 0→1" in item.reason
    assert git(repo, "rev-parse", "loop/racing").stdout.strip() == committed[0]


def test_clean_ahead_zero_branch_is_reset_from_fresh_measurements(git_world):
    repo, loops_root, ledger_dir, add_loop, commit_main, _commit_loop = git_world
    worktree = add_loop("empty")
    main_head = commit_main("new-main")

    run = make_loop(repo, loops_root, ledger_dir).run()

    assert outcome(run, "loop/empty").action == "cleaned"
    assert git(repo, "rev-parse", "loop/empty").stdout.strip() == main_head
    assert git(worktree, "status", "--porcelain").stdout == ""


def test_green_landing_uses_shared_land_gates_and_freshens_branch(
    git_world, monkeypatch
):
    repo, loops_root, ledger_dir, add_loop, _commit_main, commit_loop = git_world
    worktree = add_loop("green")
    commit_loop(worktree, "green-work")
    calls = []

    def green_gate(gate_repo: Path, base: str, *, include_collection: bool):
        calls.append((gate_repo, base, include_collection))
        return True, "shared gates grün"

    monkeypatch.setattr(landing_module, "_land_gates", green_gate)
    old_main = git(repo, "rev-parse", "main").stdout.strip()

    run = make_loop(repo, loops_root, ledger_dir).run()

    assert calls == [(repo.resolve(), old_main, True)]
    assert outcome(run, "loop/green").action == "landed"
    new_main = git(repo, "rev-parse", "main").stdout.strip()
    assert new_main != old_main
    assert git(repo, "rev-parse", "loop/green").stdout.strip() == new_main


def test_landing_multiple_branches_accepts_behind_change_caused_by_this_run(
    git_world,
):
    repo, loops_root, ledger_dir, add_loop, _commit_main, commit_loop = git_world
    first = add_loop("a-first")
    second = add_loop("b-second")
    commit_loop(first, "first-work")
    commit_loop(second, "second-work")

    run = make_loop(
        repo,
        loops_root,
        ledger_dir,
        gate_runner=lambda _repo, _base: (True, "gates grün"),
    ).run()

    assert [item.action for item in run.outcomes] == ["landed", "landed"]
    assert (repo / "first-work.txt").is_file()
    assert (repo / "second-work.txt").is_file()
    main_head = git(repo, "rev-parse", "main").stdout.strip()
    assert git(repo, "rev-parse", "loop/a-first").stdout.strip() == main_head
    assert git(repo, "rev-parse", "loop/b-second").stdout.strip() == main_head


def test_landing_batches_collection_after_first_candidate(git_world, monkeypatch):
    repo, loops_root, ledger_dir, add_loop, _commit_main, commit_loop = git_world
    first = add_loop("a-first")
    second = add_loop("b-second")
    commit_loop(first, "first-work")
    commit_loop(second, "second-work")
    collection_calls: list[bool] = []

    def green_gate(_repo: Path, _base: str, *, include_collection: bool):
        collection_calls.append(include_collection)
        return True, "gates grün"

    monkeypatch.setattr(landing_module, "_land_gates", green_gate)

    run = make_loop(repo, loops_root, ledger_dir).run()

    assert [item.action for item in run.outcomes] == ["landed", "landed"]
    assert collection_calls == [True, False]


def test_import_relevant_followup_merge_repeats_collection(git_world, monkeypatch):
    repo, loops_root, ledger_dir, add_loop, _commit_main, commit_loop = git_world
    first = add_loop("a-first")
    second = add_loop("b-second")
    commit_loop(first, "first-work")
    package = second / "sample_package"
    package.mkdir()
    (package / "__init__.py").write_text("VALUE = 1\n", encoding="utf-8")
    git(second, "add", "sample_package/__init__.py")
    git(second, "commit", "-m", "import relevant work")
    collection_calls: list[bool] = []

    def green_gate(_repo: Path, _base: str, *, include_collection: bool):
        collection_calls.append(include_collection)
        return True, "gates grün"

    monkeypatch.setattr(landing_module, "_land_gates", green_gate)

    run = make_loop(repo, loops_root, ledger_dir).run()

    assert [item.action for item in run.outcomes] == ["landed", "landed"]
    assert collection_calls == [True, True]


def test_red_gate_rolls_merge_back_and_parks_branch(git_world):
    repo, loops_root, ledger_dir, add_loop, _commit_main, commit_loop = git_world
    worktree = add_loop("red")
    branch_head = commit_loop(worktree, "red-work")
    pre_merge_main = git(repo, "rev-parse", "main").stdout.strip()

    run = make_loop(
        repo,
        loops_root,
        ledger_dir,
        gate_runner=lambda _repo, _base: (False, "collection rot (rc=1)"),
    ).run()

    item = outcome(run, "loop/red")
    assert item.action == "parked"
    assert "Gate rot" in item.reason
    assert "zurückgerollt" in item.reason
    assert git(repo, "rev-parse", "main").stdout.strip() == pre_merge_main
    assert git(repo, "rev-parse", "loop/red").stdout.strip() == branch_head
    assert git(repo, "status", "--porcelain").stdout == ""


def test_unwritable_landing_evidence_rolls_merge_back_and_parks_branch(git_world):
    repo, loops_root, ledger_dir, add_loop, _commit_main, commit_loop = git_world
    worktree = add_loop("evidence-red")
    branch_head = commit_loop(worktree, "evidence-work")
    pre_merge_main = git(repo, "rev-parse", "main").stdout.strip()

    def fail_evidence(*_args):
        raise OSError("disk full")

    run = make_loop(
        repo,
        loops_root,
        ledger_dir,
        gate_runner=lambda _repo, _base: (True, "collection + affected grün"),
        landing_evidence_writer=fail_evidence,
    ).run()

    item = outcome(run, "loop/evidence-red")
    assert item.action == "parked"
    assert "Landing-Evidenz nicht schreibbar" in item.reason
    assert "disk full" in item.reason
    assert "zurückgerollt" in item.reason
    assert git(repo, "rev-parse", "main").stdout.strip() == pre_merge_main
    assert git(repo, "rev-parse", "loop/evidence-red").stdout.strip() == branch_head
    assert git(repo, "status", "--porcelain").stdout == ""


def test_dry_run_parks_a_predictable_merge_conflict_without_touching_refs(git_world):
    repo, loops_root, ledger_dir, add_loop, _commit_main, _commit_loop = git_world
    worktree = add_loop("conflict")
    (worktree / "README.md").write_text("branch version\n", encoding="utf-8")
    git(worktree, "add", "README.md")
    git(worktree, "commit", "-m", "branch conflict")
    (repo / "README.md").write_text("main version\n", encoding="utf-8")
    git(repo, "add", "README.md")
    git(repo, "commit", "-m", "main conflict")
    before = refs(repo)

    run = make_loop(repo, loops_root, ledger_dir, dry_run=True).run()

    item = outcome(run, "loop/conflict")
    assert item.action == "parked"
    assert "Merge würde scheitern" in item.reason
    assert refs(repo) == before


def test_cli_dry_run_changes_no_ref_branch_or_worktree(git_world, capsys):
    repo, loops_root, ledger_dir, add_loop, commit_main, commit_loop = git_world
    empty = add_loop("empty")
    commit_main("main-new")
    active = add_loop("active")
    commit_loop(active, "active-new")
    before_refs = refs(repo)
    before_main_status = git(repo, "status", "--porcelain").stdout
    before_statuses = {
        empty: git(empty, "status", "--porcelain").stdout,
        active: git(active, "status", "--porcelain").stdout,
    }
    before_heads = {
        "main": git(repo, "rev-parse", "main").stdout.strip(),
        "loop/empty": git(repo, "rev-parse", "loop/empty").stdout.strip(),
        "loop/active": git(repo, "rev-parse", "loop/active").stdout.strip(),
    }

    rc = main(
        [
            "--repo",
            str(repo),
            "--loops-root",
            str(loops_root),
            "--ledger-dir",
            str(ledger_dir),
            "--dry-run",
        ]
    )

    output = capsys.readouterr().out
    assert rc == 0
    assert "mode: dry-run" in output
    assert "loop/empty" in output and "loop/active" in output
    assert refs(repo) == before_refs
    assert git(repo, "status", "--porcelain").stdout == before_main_status
    assert {
        branch: git(repo, "rev-parse", branch).stdout.strip()
        for branch in before_heads
    } == before_heads
    for worktree, status in before_statuses.items():
        assert git(worktree, "status", "--porcelain").stdout == status
    assert not ledger_dir.exists()


def test_cli_excludes_its_own_loop_branch_from_inventory(
    git_world, capsys
):
    repo, loops_root, ledger_dir, add_loop, _commit_main, commit_loop = git_world
    own_worktree = add_loop("landing")
    other_worktree = add_loop("other")
    commit_loop(own_worktree, "own-work")
    commit_loop(other_worktree, "other-work")
    own_head = git(repo, "rev-parse", "loop/landing").stdout.strip()
    rc = main(
        [
            "--repo",
            str(repo),
            "--loops-root",
            str(loops_root),
            "--ledger-dir",
            str(ledger_dir),
            "--dry-run",
        ]
    )

    output = capsys.readouterr().out
    assert rc == 0
    assert "loop/other" in output
    assert "loop/landing" not in output
    assert git(repo, "rev-parse", "loop/landing").stdout.strip() == own_head


def test_receipt_contains_inventory_and_each_branch_outcome(git_world):
    repo, loops_root, ledger_dir, add_loop, commit_main, _commit_loop = git_world
    add_loop("empty")
    commit_main("main-new")

    run = make_loop(repo, loops_root, ledger_dir).run()

    assert run.ledger_path is not None
    text = run.ledger_path.read_text(encoding="utf-8")
    assert "| loop/empty | 0 | 1 |" in text
    assert "loop/empty: **cleaned**" in text


def test_no_work_discord_message_is_exactly_one_line(git_world):
    repo, loops_root, ledger_dir, _add_loop, _commit_main, _commit_loop = git_world

    run = make_loop(repo, loops_root, ledger_dir, dry_run=True).run()

    message = render_discord(run)
    assert message.splitlines() == [
        "Landing-Loop dry-run: 0 gelandet · 0 bereinigt · 0 geparkt"
    ]


def test_discord_lists_only_parked_branches_and_never_exceeds_ten_lines(git_world):
    repo, loops_root, ledger_dir, add_loop, commit_main, _commit_loop = git_world
    worktree = add_loop("park-me")
    commit_main("main-new")
    (worktree / "untracked.txt").write_text("keep\n", encoding="utf-8")

    run = make_loop(repo, loops_root, ledger_dir, dry_run=True).run()

    lines = render_discord(run).splitlines()
    assert len(lines) <= 10
    assert lines[0].endswith("0 gelandet · 0 bereinigt · 1 geparkt")
    assert lines[1].startswith("- loop/park-me:")


def test_worktree_on_a_foreign_branch_is_parked_without_reset(git_world):
    """Der Reset bewegt den AUSGECHECKTEN Branch, nicht den gemeinten.

    Steht im Loop-Worktree ein fremder Branch mit eigenen Commits, wuerde
    ``reset --hard`` dessen Arbeit vernichten -- und die Nachpruefung meldete
    trotzdem Erfolg, weil sie nur ``item.branch`` gegen ``base`` vergleicht.
    """
    repo, loops_root, ledger_dir, add_loop, commit_main, commit_loop = git_world
    worktree = add_loop("foo")
    commit_main("advance")

    # Fremder Branch mit eigener Arbeit, im Loop-Worktree ausgecheckt.
    git(worktree, "checkout", "-b", "bar")
    fremd = commit_loop(worktree, "fremde-arbeit")

    loop = make_loop(repo, loops_root, ledger_dir)
    result = loop.run()

    outcome = next(o for o in result.outcomes if o.branch == "loop/foo")
    assert outcome.action == "parked", outcome
    assert "bar" in outcome.reason and "erwartet loop/foo" in outcome.reason
    # Entscheidend: die fremde Arbeit lebt noch.
    assert git(worktree, "rev-parse", "HEAD").stdout.strip() == fremd


def test_candidate_fingerprint_is_stable_and_uses_closed_failure_class():
    candidate = LL2Candidate(
        task_or_branch_id="loop/example",
        candidate_commit="abc123",
        failing_gate="affected",
        failure_class=FailureClass.CANDIDATE_REGRESSION,
    )

    assert candidate.fingerprint == candidate.fingerprint
    assert len(candidate.fingerprint) == 64
    assert set(FailureClass) == {
        FailureClass.CANDIDATE_REGRESSION,
        FailureClass.MERGE_CONFLICT,
        FailureClass.MAIN_RED,
        FailureClass.INFRA,
        FailureClass.UNCLEAR,
        FailureClass.CLEAN_LAND,
        FailureClass.HELD_ESCALATED,
    }


@pytest.mark.parametrize(
    ("gate_name", "output", "expected"),
    [
        ("affected", "FAILED tests/test_feature.py::test_case", FailureClass.CANDIDATE_REGRESSION),
        ("affected", "command timed out after 300s", FailureClass.INFRA),
        ("baseline", "green receipt missing for baseline abc", FailureClass.MAIN_RED),
        ("policy", "candidate held and escalated", FailureClass.HELD_ESCALATED),
        ("affected", "unexpected gate response", FailureClass.UNCLEAR),
    ],
)
def test_classify_failure_uses_explicit_heuristics_and_unclear_default(
    gate_name, output, expected
):
    assert classify_failure(gate_name, output, "abc", "def") is expected


def test_plan_queue_stops_without_exact_green_baseline_and_sorts_candidates():
    candidates = (
        LL2Candidate("loop/z", "2"),
        LL2Candidate("loop/a", "1"),
    )

    stopped = plan_queue(
        candidates,
        BaselineProbe("main-sha", False, FailureClass.MAIN_RED, "missing"),
    )
    green = plan_queue(
        candidates,
        BaselineProbe("main-sha", True, FailureClass.CLEAN_LAND, "exact pass"),
    )

    assert stopped.candidates == ()
    assert stopped.stop_rest is True
    assert [item.task_or_branch_id for item in green.candidates] == ["loop/a", "loop/z"]
    assert green.stop_rest is False


def test_plan_queue_marks_candidate_isolation_and_global_stop():
    candidates = (
        LL2Candidate(
            "loop/a-regression",
            "1",
            "affected",
            FailureClass.CANDIDATE_REGRESSION,
        ),
        LL2Candidate("loop/b-infra", "2", "affected", FailureClass.INFRA),
        LL2Candidate("loop/c-not-run", "3"),
    )
    baseline = BaselineProbe("main", True, FailureClass.CLEAN_LAND, "exact pass")

    plan = plan_queue(candidates, baseline)

    assert plan.isolate == ("loop/a-regression",)
    assert plan.stop_after == "loop/b-infra"
    assert plan.stop_rest is True
    assert [item.task_or_branch_id for item in plan.candidates] == [
        "loop/a-regression",
        "loop/b-infra",
    ]


def test_missing_baseline_proof_stops_queue_without_touching_refs(git_world):
    repo, loops_root, ledger_dir, add_loop, _commit_main, commit_loop = git_world
    worktree = add_loop("blocked")
    commit_loop(worktree, "candidate")
    before = refs(repo)

    run = make_loop(repo, loops_root, ledger_dir, baseline_records=lambda: []).run()

    assert run.plan.stop_rest is True
    assert run.diagnostics[0].failure_class is FailureClass.MAIN_RED
    assert outcome(run, "loop/blocked").action == "parked"
    assert refs(repo) == before


def test_infra_failure_stops_rest_queue_but_candidate_regression_isolates(git_world):
    repo, loops_root, ledger_dir, add_loop, _commit_main, commit_loop = git_world
    first = add_loop("a-first")
    second = add_loop("b-second")
    commit_loop(first, "first")
    second_head = commit_loop(second, "second")
    calls: list[str] = []

    def infra_gate(_repo: Path, _base: str):
        calls.append("gate")
        return False, "command timed out after 300s"

    run = make_loop(repo, loops_root, ledger_dir, gate_runner=infra_gate).run()

    assert calls == ["gate"]
    assert run.diagnostics[0].failure_class is FailureClass.INFRA
    assert run.diagnostics[0].stop_rest is True
    assert outcome(run, "loop/b-second").action == "parked"
    assert "Rest-Queue gestoppt" in outcome(run, "loop/b-second").reason
    assert git(repo, "rev-parse", "loop/b-second").stdout.strip() == second_head


def test_candidate_regression_isolated_and_next_candidate_rebased_on_main(git_world):
    repo, loops_root, ledger_dir, add_loop, _commit_main, commit_loop = git_world
    first = add_loop("a-regression")
    second = add_loop("b-clean")
    commit_loop(first, "regression")
    second_head = commit_loop(second, "clean")
    reports = iter(((False, "FAILED tests/test_feature.py"), (True, "all green")))

    run = make_loop(
        repo,
        loops_root,
        ledger_dir,
        gate_runner=lambda _repo, _base: next(reports),
    ).run()

    assert run.diagnostics[0].failure_class is FailureClass.CANDIDATE_REGRESSION
    assert run.diagnostics[0].stop_rest is False
    assert outcome(run, "loop/a-regression").action == "parked"
    assert outcome(run, "loop/b-clean").action == "landed"
    assert git(repo, "rev-parse", "main").stdout.strip() == second_head


def test_automation_off_holds_queue_without_recovery_or_gate(git_world):
    repo, loops_root, ledger_dir, add_loop, _commit_main, commit_loop = git_world
    worktree = add_loop("t_disabled")
    head = commit_loop(worktree, "candidate")
    gate_calls: list[str] = []
    recovery_calls = []

    run = make_loop(
        repo,
        loops_root,
        ledger_dir,
        automation_enabled=lambda: False,
        gate_runner=lambda _repo, _base: gate_calls.append("gate") or (True, "green"),
        recovery_request=lambda candidate: recovery_calls.append(candidate) or "requested",
    ).run()

    assert gate_calls == []
    assert recovery_calls == []
    assert outcome(run, "loop/t_disabled").action == "parked"
    assert "Landing-Automatik deaktiviert" in outcome(run, "loop/t_disabled").reason
    assert git(repo, "rev-parse", "loop/t_disabled").stdout.strip() == head


def test_candidate_regression_requests_exactly_one_recovery(git_world):
    repo, loops_root, ledger_dir, add_loop, _commit_main, commit_loop = git_world
    worktree = add_loop("t_recovery")
    commit_loop(worktree, "regression")
    recovery_calls = []

    run = make_loop(
        repo,
        loops_root,
        ledger_dir,
        gate_runner=lambda _repo, _base: (False, "FAILED tests/test_feature.py"),
        recovery_request=lambda candidate: recovery_calls.append(candidate) or "requested",
    ).run()

    assert run.diagnostics[0].failure_class is FailureClass.CANDIDATE_REGRESSION
    assert len(recovery_calls) == 1
    assert recovery_calls[0] == run.diagnostics[0].candidate


def test_stop_file_holds_at_safe_checkpoint(git_world, tmp_path):
    repo, loops_root, ledger_dir, add_loop, _commit_main, commit_loop = git_world
    worktree = add_loop("stopped")
    commit_loop(worktree, "candidate")
    stop_path = tmp_path / "STOP"
    stop_path.touch()

    run = make_loop(
        repo,
        loops_root,
        ledger_dir,
        stop_path=stop_path,
        gate_runner=lambda _repo, _base: pytest.fail("gate must not run after STOP"),
    ).run()

    assert outcome(run, "loop/stopped").action == "parked"
    assert "STOP angefordert" in outcome(run, "loop/stopped").reason


# ---------------------------------------------------------------------------
# Fix 1 — BaselineProbe: prefix-tolerant SHA match (ledger stores short SHAs)
# ---------------------------------------------------------------------------


def test_baseline_probe_matches_short_sha_pass_record():
    baseline = "3cca3aac0f60227bca5c6a0e1a2ee9a51ca433e3"
    probe = BaselineProbe.from_records(
        baseline,
        [{"result": "pass", "head_sha": baseline[:9]}],
    )

    assert probe.green is True
    assert probe.source == "nightly"


def test_baseline_probe_accepts_landing_pass_but_prefers_nightly():
    baseline = "3cca3aac0f60227bca5c6a0e1a2ee9a51ca433e3"
    landing = {
        "result": "pass",
        "head_sha": baseline,
        "source": "landing_loop",
    }

    landing_probe = BaselineProbe.from_records(baseline, [], [landing])
    nightly_probe = BaselineProbe.from_records(
        baseline,
        [{"result": "pass", "head_sha": baseline}],
        [landing],
    )

    assert landing_probe.green is True
    assert landing_probe.source == "landing"
    assert "Landing-Gate" in landing_probe.reason
    assert nightly_probe.source == "nightly"


def test_two_runs_land_consecutively_from_dedicated_landing_evidence(git_world):
    repo, loops_root, ledger_dir, add_loop, _commit_main, commit_loop = git_world
    first_worktree = add_loop("first")
    commit_loop(first_worktree, "first")
    nightly_head = git(repo, "rev-parse", "main").stdout.strip()
    landing_ledger = ledger_dir / "landing-gate-ledger.jsonl"

    def write_evidence(head, gates, candidate, ts):
        return landing_module.record_landing_gate_pass(
            head, gates, candidate, ts=ts, path=landing_ledger
        )

    common = {
        "baseline_records": lambda: [
            {"result": "pass", "head_sha": nightly_head}
        ],
        "landing_baseline_records": lambda: landing_module.read_landing_gate_records(
            landing_ledger
        ),
        "landing_evidence_writer": write_evidence,
        "gate_runner": lambda _repo, _base: (True, "collection + affected grün"),
    }
    first_run = make_loop(repo, loops_root, ledger_dir, **common).run()
    second_worktree = add_loop("second")
    commit_loop(second_worktree, "second")
    second_run = make_loop(repo, loops_root, ledger_dir, **common).run()

    assert outcome(first_run, "loop/first").action == "landed"
    assert second_run.plan.baseline.source == "landing"
    assert outcome(second_run, "loop/second").action == "landed"
    assert (repo / "first.txt").is_file()
    assert (repo / "second.txt").is_file()
    assert len(landing_module.read_landing_gate_records(landing_ledger)) == 2


def test_baseline_probe_rejects_foreign_and_degenerate_shas():
    baseline = "3cca3aac0f60227bca5c6a0e1a2ee9a51ca433e3"
    foreign = BaselineProbe.from_records(
        baseline,
        [{"result": "pass", "head_sha": "deadbeef1"}],
    )
    empty = BaselineProbe.from_records(baseline, [{"result": "pass"}])
    fragment = BaselineProbe.from_records(
        baseline, [{"result": "pass", "head_sha": "3cc"}]
    )
    red_same_sha = BaselineProbe.from_records(
        baseline,
        [{"result": "fail", "head_sha": baseline}],
    )

    assert foreign.green is False
    assert empty.green is False
    assert fragment.green is False
    assert red_same_sha.green is False


# ---------------------------------------------------------------------------
# Fix 2 — merge conflicts are recoverable candidates, not policy holds
# ---------------------------------------------------------------------------


def _conflicting_loop(add_loop, repo):
    worktree = add_loop("conflict")
    (worktree / "README.md").write_text("branch version\n", encoding="utf-8")
    git(worktree, "add", "README.md")
    git(worktree, "commit", "-m", "branch conflict")
    (repo / "README.md").write_text("main version\n", encoding="utf-8")
    git(repo, "add", "README.md")
    git(repo, "commit", "-m", "main conflict")
    return worktree


def test_merge_conflict_is_candidate_local_and_requests_recovery(git_world):
    repo, loops_root, ledger_dir, add_loop, _commit_main, _commit_loop = git_world
    _conflicting_loop(add_loop, repo)
    clean = add_loop("z-clean")
    (clean / "clean.txt").write_text("clean\n", encoding="utf-8")
    git(clean, "add", "clean.txt")
    git(clean, "commit", "-m", "clean work")
    recovery_calls = []

    run = make_loop(
        repo,
        loops_root,
        ledger_dir,
        gate_runner=lambda _repo, _base: (True, "gates grün"),
        recovery_request=lambda candidate: (
            recovery_calls.append(candidate) or "requested"
        ),
    ).run()

    conflict_diag = next(
        item
        for item in run.diagnostics
        if item.candidate.task_or_branch_id == "loop/conflict"
    )
    assert conflict_diag.failure_class is FailureClass.MERGE_CONFLICT
    assert conflict_diag.candidate.failing_gate == "merge"
    assert conflict_diag.stop_rest is False
    assert outcome(run, "loop/conflict").action == "parked"
    # Die Queue läuft weiter: der saubere Kandidat landet danach noch.
    assert outcome(run, "loop/z-clean").action == "landed"
    assert len(recovery_calls) == 1
    assert recovery_calls[0].failure_class is FailureClass.MERGE_CONFLICT


def test_recovery_exception_never_aborts_the_run(git_world):
    repo, loops_root, ledger_dir, add_loop, _commit_main, _commit_loop = git_world
    _conflicting_loop(add_loop, repo)

    def broken_recovery(_candidate):
        raise RuntimeError("kanban weg")

    run = make_loop(
        repo,
        loops_root,
        ledger_dir,
        gate_runner=lambda _repo, _base: (True, "gates grün"),
        recovery_request=broken_recovery,
    ).run()

    item = outcome(run, "loop/conflict")
    assert item.action == "parked"
    assert "Recovery fehlgeschlagen" in item.reason
    assert "kanban weg" in item.reason


# ---------------------------------------------------------------------------
# Fix 3 — disjoint foreign dirt is stash-protected instead of blocking
# ---------------------------------------------------------------------------


def _landable_loop(add_loop):
    worktree = add_loop("land")
    (worktree / "landed.txt").write_text("work\n", encoding="utf-8")
    git(worktree, "add", "landed.txt")
    git(worktree, "commit", "-m", "landable work")
    return worktree


def test_disjoint_foreign_dirt_survives_green_landing_via_stash(git_world):
    repo, loops_root, ledger_dir, add_loop, _commit_main, _commit_loop = git_world
    _landable_loop(add_loop)
    (repo / "foreign.txt").write_text("untracked fremd\n", encoding="utf-8")
    (repo / "README.md").write_text("tracked fremd\n", encoding="utf-8")

    run = make_loop(
        repo,
        loops_root,
        ledger_dir,
        gate_runner=lambda _repo, _base: (True, "gates grün"),
    ).run()

    item = outcome(run, "loop/land")
    assert item.action == "landed"
    assert "gestasht und zurückgeholt" in item.reason
    assert (repo / "landed.txt").is_file()
    assert (repo / "foreign.txt").read_text(encoding="utf-8") == "untracked fremd\n"
    assert (repo / "README.md").read_text(encoding="utf-8") == "tracked fremd\n"
    assert git(repo, "stash", "list").stdout == ""


def test_overlapping_dirty_path_parks_before_any_stash_or_merge(git_world):
    repo, loops_root, ledger_dir, add_loop, _commit_main, _commit_loop = git_world
    worktree = _landable_loop(add_loop)
    (worktree / "README.md").write_text("branch readme\n", encoding="utf-8")
    git(worktree, "add", "README.md")
    git(worktree, "commit", "-m", "branch readme")
    pre_main = git(repo, "rev-parse", "main").stdout.strip()
    (repo / "README.md").write_text("fremde arbeit\n", encoding="utf-8")

    run = make_loop(
        repo,
        loops_root,
        ledger_dir,
        gate_runner=lambda _repo, _base: pytest.fail("gate darf nicht laufen"),
    ).run()

    item = outcome(run, "loop/land")
    assert item.action == "parked"
    assert "kreuzen den Merge" in item.reason
    assert "README.md" in item.reason
    assert git(repo, "rev-parse", "main").stdout.strip() == pre_main
    assert git(repo, "stash", "list").stdout == ""
    assert (repo / "README.md").read_text(encoding="utf-8") == "fremde arbeit\n"


def test_red_gate_rollback_preserves_disjoint_foreign_dirt(git_world):
    repo, loops_root, ledger_dir, add_loop, _commit_main, _commit_loop = git_world
    _landable_loop(add_loop)
    pre_main = git(repo, "rev-parse", "main").stdout.strip()
    (repo / "foreign.txt").write_text("untracked fremd\n", encoding="utf-8")
    (repo / "README.md").write_text("tracked fremd\n", encoding="utf-8")

    run = make_loop(
        repo,
        loops_root,
        ledger_dir,
        gate_runner=lambda _repo, _base: (False, "FAILED tests/test_x.py"),
    ).run()

    item = outcome(run, "loop/land")
    assert item.action == "parked"
    assert "Gate rot" in item.reason
    assert git(repo, "rev-parse", "main").stdout.strip() == pre_main
    assert not (repo / "landed.txt").exists()
    assert (repo / "foreign.txt").read_text(encoding="utf-8") == "untracked fremd\n"
    assert (repo / "README.md").read_text(encoding="utf-8") == "tracked fremd\n"
    assert git(repo, "stash", "list").stdout == ""


def test_dry_run_reports_stash_protection_for_disjoint_dirt(git_world):
    repo, loops_root, ledger_dir, add_loop, _commit_main, _commit_loop = git_world
    _landable_loop(add_loop)
    (repo / "foreign.txt").write_text("untracked fremd\n", encoding="utf-8")

    run = make_loop(repo, loops_root, ledger_dir, dry_run=True).run()

    item = outcome(run, "loop/land")
    assert item.action == "landed"
    assert "Stash-Schutz" in item.reason
    assert git(repo, "stash", "list").stdout == ""


# ---------------------------------------------------------------------------
# Review-Nacharbeit (Opus): Anker-Stash, Rename-Vollständigkeit, Fremd-Eingriff
# ---------------------------------------------------------------------------


def test_parse_porcelain_paths_includes_rename_source_and_target():
    blob = "R  renamed.txt\0orig.txt\0 M dirty.txt\0?? loose.txt\0"

    paths = LandingLoop._parse_porcelain_paths(blob)

    assert paths == ["orig.txt", "renamed.txt", "dirty.txt", "loose.txt"]


def test_untracked_dir_overlapping_merge_path_parks(git_world):
    repo, loops_root, ledger_dir, add_loop, _commit_main, _commit_loop = git_world
    worktree = add_loop("dirconflict")
    (worktree / "fremd").mkdir()
    (worktree / "fremd" / "inside.txt").write_text("branch\n", encoding="utf-8")
    git(worktree, "add", "fremd/inside.txt")
    git(worktree, "commit", "-m", "branch adds fremd/inside.txt")
    pre_main = git(repo, "rev-parse", "main").stdout.strip()
    (repo / "fremd").mkdir()
    (repo / "fremd" / "operator.txt").write_text("operator\n", encoding="utf-8")

    run = make_loop(
        repo,
        loops_root,
        ledger_dir,
        gate_runner=lambda _repo, _base: pytest.fail("gate darf nicht laufen"),
    ).run()

    item = outcome(run, "loop/dirconflict")
    assert item.action == "parked"
    assert "kreuzen den Merge" in item.reason
    assert "fremd/" in item.reason
    assert git(repo, "rev-parse", "main").stdout.strip() == pre_main
    assert git(repo, "stash", "list").stdout == ""


def test_staged_rename_is_stashed_and_restored_with_index(git_world):
    repo, loops_root, ledger_dir, add_loop, _commit_main, _commit_loop = git_world
    _landable_loop(add_loop)
    git(repo, "mv", "README.md", "renamed.txt")

    run = make_loop(
        repo,
        loops_root,
        ledger_dir,
        gate_runner=lambda _repo, _base: (True, "gates grün"),
    ).run()

    item = outcome(run, "loop/land")
    assert item.action == "landed"
    assert (repo / "renamed.txt").is_file()
    assert not (repo / "README.md").exists()
    # --index: der Rename liegt wieder GESTAGED vor, nicht als delete+untracked.
    assert git(repo, "status", "--porcelain").stdout.startswith("R ")
    assert git(repo, "stash", "list").stdout == ""


def test_foreign_stash_during_gates_is_never_popped(git_world):
    repo, loops_root, ledger_dir, add_loop, _commit_main, _commit_loop = git_world
    _landable_loop(add_loop)
    (repo / "ours.txt").write_text("unsere fremde arbeit\n", encoding="utf-8")

    def foreign_stash_gate(gate_repo: Path, _base: str):
        # Fremdsession stasht mitten im Gate-Fenster ihre eigene Arbeit.
        (gate_repo / "foreign.txt").write_text("fremd\n", encoding="utf-8")
        git(
            gate_repo,
            "stash",
            "push",
            "--include-untracked",
            "--quiet",
            "-m",
            "fremdsession",
            "--",
            "foreign.txt",
        )
        return True, "gates grün"

    run = make_loop(repo, loops_root, ledger_dir, gate_runner=foreign_stash_gate).run()

    item = outcome(run, "loop/land")
    assert item.action == "landed"
    # Unser Dirt ist zurück, der FREMDE Stash liegt unangetastet oben.
    assert (repo / "ours.txt").read_text(encoding="utf-8") == "unsere fremde arbeit\n"
    stash_list = git(repo, "stash", "list").stdout
    assert "fremdsession" in stash_list
    assert "autostash" not in stash_list


def test_foreign_rewrite_during_gates_blocks_pop_and_keeps_stash(git_world):
    repo, loops_root, ledger_dir, add_loop, _commit_main, _commit_loop = git_world
    _landable_loop(add_loop)
    (repo / "ours.txt").write_text("original\n", encoding="utf-8")

    def rewriting_gate(gate_repo: Path, _base: str):
        # Fremdsession schreibt denselben Pfad erneut, während unser Stash liegt.
        (gate_repo / "ours.txt").write_text("neu geschrieben\n", encoding="utf-8")
        return True, "gates grün"

    run = make_loop(repo, loops_root, ledger_dir, gate_runner=rewriting_gate).run()

    item = outcome(run, "loop/land")
    assert item.action == "parked"
    assert "erneut geschrieben" in item.reason
    # Der Stash bleibt erhalten, die neue Fremd-Datei wird nicht überschrieben.
    assert "autostash" in git(repo, "stash", "list").stdout
    assert (repo / "ours.txt").read_text(encoding="utf-8") == "neu geschrieben\n"


def test_merge_failure_without_content_conflict_is_not_merge_conflict(git_world):
    repo, loops_root, ledger_dir, add_loop, _commit_main, _commit_loop = git_world
    add_loop("infra")
    loop = make_loop(repo, loops_root, ledger_dir)
    item = landing_module.BranchInventory(
        branch="loop/infra", ahead=1, behind=0,
        worktree=loops_root / "infra" / "wt", head="deadbeef",
    )
    locked = landing_module.BranchOutcome(
        "loop/infra", "parked",
        "Merge-Fehler: Unable to create .git/index.lock: File exists",
    )
    timed_out = landing_module.BranchOutcome(
        "loop/infra", "parked", "Merge-Fehler: command timed out after 300s"
    )

    unclear = loop._diagnose_outcome(item, locked, "0" * 40)
    infra = loop._diagnose_outcome(item, timed_out, "0" * 40)

    assert unclear.failure_class is FailureClass.UNCLEAR
    assert unclear.stop_rest is True
    assert infra.failure_class is FailureClass.INFRA
    assert infra.stop_rest is True


def test_dry_run_guard_blocks_recovery_even_with_conflict_class(git_world, monkeypatch):
    """Mutationssicher: _land erzeugt echt eine MERGE_CONFLICT-Diagnose —
    nur der dry-run-Guard verhindert die Recovery."""
    repo, loops_root, ledger_dir, add_loop, _commit_main, commit_loop = git_world
    worktree = add_loop("conflict")
    commit_loop(worktree, "work")
    monkeypatch.setattr(
        LandingLoop,
        "_land",
        lambda self, item: landing_module.BranchOutcome(
            item.branch, "parked", "Merge-Konflikt: simuliert"
        ),
    )

    run = make_loop(
        repo,
        loops_root,
        ledger_dir,
        dry_run=True,
        recovery_request=lambda _c: pytest.fail("recovery im dry-run verboten"),
    ).run()

    assert run.diagnostics[0].failure_class is FailureClass.MERGE_CONFLICT


def test_lost_stash_preserves_classification_recovers_and_stops_queue(git_world):
    """N1: Ein nicht zurückgeholter Stash darf weder die Klassifikation
    entwerten noch die Queue weiterlaufen lassen."""
    repo, loops_root, ledger_dir, add_loop, _commit_main, commit_loop = git_world
    first = add_loop("a-first")
    commit_loop(first, "first-work")
    second = add_loop("b-second")
    second_head = commit_loop(second, "second-work")
    (repo / "ours.txt").write_text("original\n", encoding="utf-8")
    recovery_calls = []

    def red_rewriting_gate(gate_repo: Path, _base: str):
        (gate_repo / "ours.txt").write_text("neu\n", encoding="utf-8")
        return False, "FAILED tests/test_x.py"

    run = make_loop(
        repo,
        loops_root,
        ledger_dir,
        gate_runner=red_rewriting_gate,
        recovery_request=lambda candidate: (
            recovery_calls.append(candidate) or "requested"
        ),
    ).run()

    diag = run.diagnostics[0]
    assert diag.candidate.task_or_branch_id == "loop/a-first"
    assert diag.failure_class is FailureClass.CANDIDATE_REGRESSION
    assert diag.stop_rest is True
    reason = outcome(run, "loop/a-first").reason
    assert "Gate rot" in reason
    assert "NICHT zurückgeholt" in reason
    assert len(recovery_calls) == 1
    queued = outcome(run, "loop/b-second")
    assert queued.action == "parked"
    assert "Rest-Queue gestoppt" in queued.reason
    assert git(repo, "rev-parse", "loop/b-second").stdout.strip() == second_head
    assert "autostash" in git(repo, "stash", "list").stdout


# ---------------------------------------------------------------------------
# Review Runde 3 (N5/N6/N7): Anker-Verifikation, Fail-closed, Stopp-Invariante
# ---------------------------------------------------------------------------


def test_find_own_stash_ignores_foreign_tip_entry(git_world):
    repo, loops_root, ledger_dir, _add_loop, _commit_main, _commit_loop = git_world
    loop = make_loop(repo, loops_root, ledger_dir)
    (repo / "ours.txt").write_text("uns\n", encoding="utf-8")
    git(repo, "stash", "push", "--include-untracked", "--quiet",
        "-m", landing_module._STASH_MESSAGE)
    own_sha = git(repo, "rev-parse", "-q", "--verify", "refs/stash").stdout.strip()
    (repo / "foreign.txt").write_text("fremd\n", encoding="utf-8")
    git(repo, "stash", "push", "--include-untracked", "--quiet", "-m", "fremd")

    found = loop._find_own_stash()

    assert found == own_sha  # Fremdspitze darf den Anker nie verdecken


def test_unreadable_status_before_pop_is_fail_closed(git_world, monkeypatch):
    repo, loops_root, ledger_dir, add_loop, _commit_main, _commit_loop = git_world
    _landable_loop(add_loop)
    (repo / "ours.txt").write_text("uns\n", encoding="utf-8")
    loop = make_loop(repo, loops_root, ledger_dir)
    problem, stash_sha = loop._stash_foreign_dirt(["ours.txt"])
    assert problem is None and stash_sha is not None
    monkeypatch.setattr(LandingLoop, "_main_dirty_paths", lambda self: None)

    pop_problem = loop._pop_foreign_stash(stash_sha, ["ours.txt"])

    assert pop_problem is not None
    assert "nicht lesbar" in pop_problem
    # Fail-closed: der Stash wurde nicht angefasst.
    assert "autostash" in git(repo, "stash", "list").stdout


def test_stash_left_behind_always_stops_the_queue(git_world):
    """N7-Invariante: JEDER Pfad, der unseren Stash liegen lässt, trägt den
    Marker und stoppt die Queue."""
    repo, loops_root, ledger_dir, add_loop, _commit_main, _commit_loop = git_world
    add_loop("x")
    loop = make_loop(repo, loops_root, ledger_dir)
    item = landing_module.BranchInventory(
        branch="loop/x", ahead=1, behind=0,
        worktree=loops_root / "x" / "wt", head="deadbeef",
    )
    reasons = [
        f"fremde Änderungen {landing_module._STASH_LOST_MARKER}: Stash-Anker "
        "nicht verifizierbar (Betreff-Match fehlgeschlagen) — fail-closed",
        f"Basis-Worktree nach Stash nicht sauber; fremde Änderungen "
        f"{landing_module._STASH_LOST_MARKER} (pop fehlgeschlagen) — MANUELL",
        f"Gate rot; Merge zurückgerollt: FAILED t; fremde Änderungen "
        f"{landing_module._STASH_LOST_MARKER}: erneut geschrieben",
    ]

    for reason in reasons:
        diagnosed = loop._diagnose_outcome(
            item, landing_module.BranchOutcome("loop/x", "parked", reason), "0" * 40
        )
        assert diagnosed.stop_rest is True, reason


def test_unverifiable_stash_anchor_parks_with_marker(git_world, monkeypatch):
    """Deckt die Marker-Erzeugung im Anker-Pfad (Opus-Anmerkung Runde 4)."""
    repo, loops_root, ledger_dir, add_loop, _commit_main, _commit_loop = git_world
    _landable_loop(add_loop)
    (repo / "ours.txt").write_text("uns\n", encoding="utf-8")
    loop = make_loop(repo, loops_root, ledger_dir)
    monkeypatch.setattr(LandingLoop, "_find_own_stash", lambda self: None)

    problem, stash_sha = loop._stash_foreign_dirt(["ours.txt"])

    assert stash_sha is None
    assert landing_module._STASH_LOST_MARKER in problem
    assert "autostash" in git(repo, "stash", "list").stdout
    git(repo, "stash", "pop", "--quiet")  # Welt aufräumen


def test_red_baseline_still_cleans_leerstaende_but_holds_candidates(git_world):
    """Leerstände brauchen keine grüne Baseline — nur Landungen."""
    repo, loops_root, ledger_dir, add_loop, commit_main, commit_loop = git_world
    empty = add_loop("a-empty")
    main_head = commit_main("advanced")
    candidate = add_loop("b-candidate")
    candidate_head = commit_loop(candidate, "work")

    run = make_loop(repo, loops_root, ledger_dir, baseline_records=lambda: []).run()

    cleaned = outcome(run, "loop/a-empty")
    assert cleaned.action == "cleaned"
    assert git(repo, "rev-parse", "loop/a-empty").stdout.strip() == main_head
    held = outcome(run, "loop/b-candidate")
    assert held.action == "parked"
    assert "Rest-Queue gestoppt" in held.reason
    assert git(repo, "rev-parse", "loop/b-candidate").stdout.strip() == candidate_head


def test_operator_checkpoint_holds_leerstaende_too(git_world, tmp_path):
    """Der harte Operator-Halt (STOP-Datei) gilt auch für Leerstände."""
    repo, loops_root, ledger_dir, add_loop, commit_main, _commit_loop = git_world
    empty = add_loop("empty")
    commit_main("advanced")
    empty_head = git(repo, "rev-parse", "loop/empty").stdout.strip()
    stop_path = tmp_path / "STOP"
    stop_path.touch()

    run = make_loop(
        repo,
        loops_root,
        ledger_dir,
        baseline_records=lambda: [],
        stop_path=stop_path,
    ).run()

    item = outcome(run, "loop/empty")
    assert item.action == "parked"
    assert "STOP angefordert" in item.reason
    assert git(repo, "rev-parse", "loop/empty").stdout.strip() == empty_head
