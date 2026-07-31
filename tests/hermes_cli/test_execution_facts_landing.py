"""Landing evidence: which execution shipped, proven by a commit."""

from __future__ import annotations

from pathlib import Path
import sqlite3
import subprocess

from hermes_cli.execution_facts_contract import (
    LifecyclePhase,
    Validity,
    stable_execution_id,
)
from hermes_cli.execution_facts_landing import (
    KANBAN_SOURCE,
    LandingObservation,
    read_landing_observations,
    reconcile_landing,
    task_runs_by_task,
)

_LANDED_MS = 1_790_000_000_000


def _git(repo: Path, *args: str) -> None:
    subprocess.run(
        ["git", "-C", str(repo), *args],
        check=True,
        capture_output=True,
        text=True,
    )


def _repo(tmp_path: Path) -> Path:
    repo = tmp_path / "repo"
    repo.mkdir()
    _git(repo, "init", "-q", "-b", "main")
    _git(repo, "config", "user.email", "t@example.com")
    _git(repo, "config", "user.name", "Test")
    return repo


def _commit(repo: Path, subject: str) -> str:
    (repo / "file.txt").write_text(subject, encoding="utf-8")
    _git(repo, "add", "file.txt")
    _git(repo, "commit", "-q", "-m", subject)
    result = subprocess.run(
        ["git", "-C", str(repo), "rev-parse", "HEAD"],
        check=True,
        capture_output=True,
        text=True,
    )
    return result.stdout.strip()


def test_reads_task_ids_from_the_integration_branch(tmp_path: Path) -> None:
    repo = _repo(tmp_path)
    _commit(repo, "chore: unrelated")
    sha = _commit(repo, "kanban(t_ab12cd34): add the thing")

    observations = read_landing_observations(repo)

    assert observations is not None
    assert [o.task_id for o in observations] == ["t_ab12cd34"]
    assert observations[0].landed_sha == sha


def test_merge_subject_form_also_counts(tmp_path: Path) -> None:
    repo = _repo(tmp_path)
    sha = _commit(repo, "kanban: merge kanban/t_99887766 (slices S1-S3)")

    observations = read_landing_observations(repo)

    assert observations is not None
    assert [o.task_id for o in observations] == ["t_99887766"]
    assert observations[0].landed_sha == sha


def test_work_on_another_branch_has_not_landed(tmp_path: Path) -> None:
    """Only what the integration branch can reach counts as shipped."""
    repo = _repo(tmp_path)
    _commit(repo, "chore: base")
    _git(repo, "checkout", "-q", "-b", "feature")
    _commit(repo, "kanban(t_deadbeef): not merged yet")
    _git(repo, "checkout", "-q", "main")

    observations = read_landing_observations(repo)

    assert observations is not None
    assert [o.task_id for o in observations] == []


def test_unreadable_repository_is_unknown_not_zero(tmp_path: Path) -> None:
    assert read_landing_observations(tmp_path / "nope") is None


def test_latest_commit_wins_and_result_is_stable(tmp_path: Path) -> None:
    repo = _repo(tmp_path)
    _commit(repo, "kanban(t_ab12cd34): first attempt")
    latest = _commit(repo, "kanban(t_ab12cd34): follow-up fix")

    first_pass = read_landing_observations(repo)
    second_pass = read_landing_observations(repo)

    assert first_pass == second_pass, "repeat reads must agree"
    assert first_pass is not None
    assert [o.landed_sha for o in first_pass] == [latest]


def test_evidence_attaches_to_the_kanban_execution(tmp_path: Path) -> None:
    """The landing event must describe the run, not a parallel population."""
    observation = LandingObservation(
        task_id="t_ab12cd34",
        landed_sha="a" * 40,
        landed_at_ms=_LANDED_MS,
    )

    events = reconcile_landing([observation], {"t_ab12cd34": ("41", "42")})

    assert len(events) == 2
    for event, run_id in zip(events, ("41", "42"), strict=True):
        assert event.lifecycle_phase is LifecyclePhase.LANDED
        assert event.validity is Validity.EXACT
        assert event.execution_id == stable_execution_id(
            KANBAN_SOURCE, f"task_run:{run_id}"
        )
        assert event.attributes["landed_sha"] == "a" * 40


def test_task_without_recorded_runs_emits_nothing(tmp_path: Path) -> None:
    observation = LandingObservation(
        task_id="t_ab12cd34", landed_sha="b" * 40, landed_at_ms=_LANDED_MS
    )

    assert reconcile_landing([observation], {}) == []


def test_task_runs_lookup_is_chunked_and_read_only(tmp_path: Path) -> None:
    database = tmp_path / "kanban.db"
    with sqlite3.connect(database) as connection:
        connection.execute(
            "CREATE TABLE task_runs (id INTEGER PRIMARY KEY, task_id TEXT)"
        )
        connection.executemany(
            "INSERT INTO task_runs (id, task_id) VALUES (?, ?)",
            [(index, f"t_{index:08x}") for index in range(1, 1201)],
        )

    connection = sqlite3.connect(f"file:{database}?mode=ro", uri=True)
    try:
        runs = task_runs_by_task(
            connection, [f"t_{index:08x}" for index in range(1, 1201)]
        )
    finally:
        connection.close()

    assert len(runs) == 1200
    assert runs["t_00000001"] == ("1",)


def test_cohort_counts_executions_not_tasks(tmp_path: Path) -> None:
    """One shipped task with several runs is several landed executions.

    Counting tasks here made `observed` exceed `eligible` and the contract
    rejected the cohort outright.
    """
    from hermes_cli.execution_facts_shadow import collect_landing_cohort

    repo = _repo(tmp_path)
    _commit(repo, "kanban(t_ab12cd34): ship it")
    database = tmp_path / "kanban.db"
    with sqlite3.connect(database) as connection:
        connection.execute(
            "CREATE TABLE task_runs (id INTEGER PRIMARY KEY, task_id TEXT)"
        )
        connection.executemany(
            "INSERT INTO task_runs (id, task_id) VALUES (?, ?)",
            [(1, "t_ab12cd34"), (2, "t_ab12cd34"), (3, "t_ab12cd34")],
        )

    cohort = collect_landing_cohort(
        repo, database, observed_at_ms=_LANDED_MS + 1_000
    )

    assert cohort.eligible == 3
    assert cohort.observed == 3
    assert len(cohort.events) == 3


def test_missing_repository_reports_unknown_not_zero(tmp_path: Path) -> None:
    from hermes_cli.execution_facts_shadow import collect_landing_cohort

    database = tmp_path / "kanban.db"
    with sqlite3.connect(database) as connection:
        connection.execute(
            "CREATE TABLE task_runs (id INTEGER PRIMARY KEY, task_id TEXT)"
        )

    cohort = collect_landing_cohort(
        tmp_path / "no-repo", database, observed_at_ms=_LANDED_MS
    )

    assert cohort.eligible is None
    assert cohort.validity.value == "unknown"
