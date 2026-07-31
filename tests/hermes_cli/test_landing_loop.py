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

    def green_gate(gate_repo: Path, base: str):
        calls.append((gate_repo, base))
        return True, "shared gates grün"

    monkeypatch.setattr(landing_module, "_land_gates", green_gate)
    old_main = git(repo, "rev-parse", "main").stdout.strip()

    run = make_loop(repo, loops_root, ledger_dir).run()

    assert calls == [(repo.resolve(), old_main)]
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
