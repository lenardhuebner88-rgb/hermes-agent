"""End-to-end truth checks for the read-only Execution-Facts Shadow collector."""

from __future__ import annotations

import hashlib
import json
from pathlib import Path
import sqlite3
import subprocess
from typing import Sequence

import pytest

import hermes_cli.execution_facts_shadow as execution_facts_shadow
from hermes_cli.execution_facts_contract import Validity
from hermes_cli.execution_facts_ledger import ExecutionFactsLedger
from hermes_cli.execution_facts_readmodel import build_execution_facts_payload
from hermes_cli.execution_facts_shadow import (
    SOURCE_CENSUS_VERSION,
    ShadowCollectionConfig,
    SourceCohort,
    collect_crontab_cohort,
    collect_kanban_cohort,
    collect_loop_cohort,
    collect_systemd_cohort,
    collect_usage_cohort,
    collect_shadow,
)

_SAMPLE_SIZE = 20
_NOW_MS = 1_800_000_000_000


def _sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _seed_kanban(path: Path) -> None:
    with sqlite3.connect(path) as connection:
        connection.execute(
            """
            CREATE TABLE task_runs (
                id INTEGER PRIMARY KEY,
                task_id TEXT NOT NULL,
                profile TEXT,
                status TEXT NOT NULL,
                started_at INTEGER NOT NULL,
                ended_at INTEGER,
                outcome TEXT,
                worker_exit_kind TEXT,
                worker_exit_code INTEGER
            )
            """
        )
        connection.executemany(
            """
            INSERT INTO task_runs (
                id, task_id, profile, status, started_at, ended_at, outcome,
                worker_exit_kind, worker_exit_code
            ) VALUES (?, ?, 'coder', 'done', ?, ?, 'succeeded', 'clean', 0)
            """,
            (
                (
                    ordinal,
                    f"task-{ordinal}",
                    1_700_000_000 + ordinal * 10,
                    1_700_000_005 + ordinal * 10,
                )
                for ordinal in range(1, _SAMPLE_SIZE + 1)
            ),
        )


def _seed_cron(path: Path) -> None:
    path.parent.mkdir(parents=True)
    with sqlite3.connect(path) as connection:
        connection.execute(
            """
            CREATE TABLE executions (
                id TEXT PRIMARY KEY,
                job_id TEXT NOT NULL,
                status TEXT NOT NULL,
                claimed_at TEXT,
                started_at TEXT,
                finished_at TEXT,
                error TEXT
            )
            """
        )
        connection.executemany(
            """
            INSERT INTO executions VALUES (
                ?, 'shadow-test', 'complete', ?, ?, ?, NULL
            )
            """,
            (
                (
                    f"cron-{ordinal}",
                    f"2026-07-30T10:{ordinal:02d}:00+00:00",
                    f"2026-07-30T10:{ordinal:02d}:01+00:00",
                    f"2026-07-30T10:{ordinal:02d}:02+00:00",
                )
                for ordinal in range(_SAMPLE_SIZE)
            ),
        )


def _seed_loops(path: Path) -> None:
    path.parent.mkdir(parents=True)
    records = [
        {
            "ts": f"2026-07-30T11:{ordinal:02d}:00+00:00",
            "pack": "shadow",
            "event": "phase",
            "phase": "build",
            "round": ordinal,
            "loop_run_id": f"shadow-{ordinal}",
        }
        for ordinal in range(_SAMPLE_SIZE)
    ]
    path.write_text(
        "".join(
            json.dumps(record, sort_keys=True, separators=(",", ":")) + "\n"
            for record in records
        ),
        encoding="utf-8",
    )


class _FakeRunner:
    def __init__(self) -> None:
        self.calls: list[tuple[str, ...]] = []

    def run(
        self,
        argv: Sequence[str],
    ) -> subprocess.CompletedProcess[str]:
        command = tuple(argv)
        self.calls.append(command)
        if command[0] == "tmux":
            output = "\n".join(
                (
                    f"${ordinal}\x1f@{ordinal}\x1f%{ordinal}\x1f"
                    f"{10_000 + ordinal}\x1f0\x1f{ordinal:032x}"
                )
                for ordinal in range(_SAMPLE_SIZE)
            )
            return subprocess.CompletedProcess(command, 0, output, "")
        if command[:3] == (
            "systemctl",
            "--user",
            "list-units",
        ):
            output = "\n".join(
                f"shadow-{ordinal}.service loaded inactive dead"
                for ordinal in range(_SAMPLE_SIZE)
            )
            return subprocess.CompletedProcess(command, 0, output, "")
        if command[:3] == ("systemctl", "--user", "show"):
            unit = command[3]
            ordinal = int(unit.removeprefix("shadow-").removesuffix(".service"))
            output = "\n".join(
                (
                    f"InvocationID=invocation{ordinal:032d}",
                    (
                        "ExecMainStartTimestamp="
                        f"Thu 2026-07-30 10:{ordinal:02d}:00.000000 UTC"
                    ),
                    (
                        "ExecMainExitTimestamp="
                        f"Thu 2026-07-30 10:{ordinal:02d}:01.000000 UTC"
                    ),
                    "Result=success",
                    "ExecMainStatus=0",
                )
            )
            return subprocess.CompletedProcess(command, 0, output, "")
        if command[0] == "journalctl":
            output = "\n".join(
                json.dumps(
                    {
                        "_BOOT_ID": "a" * 32,
                        "_PID": str(20_000 + ordinal),
                        "__REALTIME_TIMESTAMP": str(
                            (_NOW_MS - ordinal * 1_000) * 1_000
                        ),
                        "MESSAGE": f"(piet) CMD (/home/piet/job-{ordinal}.sh)",
                    },
                    sort_keys=True,
                )
                for ordinal in range(_SAMPLE_SIZE)
            )
            return subprocess.CompletedProcess(command, 0, output, "")
        raise AssertionError(f"unexpected command: {command!r}")


class _SmallTmuxRunner(_FakeRunner):
    def run(
        self,
        argv: Sequence[str],
    ) -> subprocess.CompletedProcess[str]:
        command = tuple(argv)
        if command[0] == "tmux":
            self.calls.append(command)
            output = "\n".join(
                (
                    f"${ordinal}\x1f@{ordinal}\x1f%{ordinal}\x1f"
                    f"{10_000 + ordinal}\x1f0\x1f{ordinal:032x}"
                )
                for ordinal in range(6)
            )
            return subprocess.CompletedProcess(command, 0, output, "")
        return super().run(argv)


def test_shadow_collects_universal_denominator_without_mutating_sources(
    tmp_path: Path,
) -> None:
    hermes_home = tmp_path / "hermes"
    hermes_home.mkdir()
    kanban_path = hermes_home / "kanban.db"
    cron_path = hermes_home / "cron" / "executions.db"
    loop_path = hermes_home / "loops" / "shadow" / "ledger.jsonl"
    _seed_kanban(kanban_path)
    _seed_cron(cron_path)
    _seed_loops(loop_path)
    source_hashes = {
        path: _sha256(path) for path in (kanban_path, cron_path, loop_path)
    }
    runner = _FakeRunner()
    database = tmp_path / "execution_facts.db"

    result = collect_shadow(
        ShadowCollectionConfig(
            database=database,
            hermes_home=hermes_home,
            evidence_dir=tmp_path / "evidence",
        ),
        runner=runner,
        clock_ms=lambda: _NOW_MS,
    )

    assert result.to_dict()["schema_version"] == SOURCE_CENSUS_VERSION
    assert [row["source"] for row in result.cohorts] == [
        "kanban_timeline",
        "hermes_cron",
        "loop_ledger",
        "tmux_reconciliation",
        "systemd_invocation",
        "crontab_invocation",
    ]
    journal_call = next(call for call in runner.calls if call[0] == "journalctl")
    assert "SYSLOG_IDENTIFIER=CRON" in journal_call
    for row in result.cohorts:
        assert row["eligible"] == _SAMPLE_SIZE
        assert row["sample_events"] >= _SAMPLE_SIZE
        assert row["validity"] == "exact"
        assert row["writer_p95_us"] < 1_000
        assert row["dedupe_reconciled"] is True
        assert row["behavior_equivalent"] is True
    assert result.collector["dropped"] == 0
    assert result.collector["projection_reproducible"] is True
    assert source_hashes == {
        path: _sha256(path) for path in (kanban_path, cron_path, loop_path)
    }

    evidence = json.loads(Path(result.evidence_path).read_text(encoding="utf-8"))
    assert evidence["activation_effect"] == "none"
    assert "DO_NOT_PERSIST" not in json.dumps(evidence)

    payload = build_execution_facts_payload(database)
    run_identity = payload["p0"]["run_identity"]
    assert run_identity["metric_id"] == "run_identity_adoption"
    assert run_identity["computed_status"] == "ready"
    assert run_identity["validity"] == "exact"
    # All six sources carry exact identity: the journal names the cron
    # command, so crontab runs are attributed rather than counted anonymously.
    assert run_identity["observed"] == 120
    assert run_identity["eligible"] == 120
    assert run_identity["value"] == 120
    assert run_identity["percent"] == "100.00"
    assert run_identity["unknown_reason"] is None
    assert payload["p0"]["terminal_identity"]["observed"] == _SAMPLE_SIZE
    assert payload["p0"]["terminal_identity"]["eligible"] == _SAMPLE_SIZE
    # hermes_cron + systemd + crontab, all three now exactly identified.
    assert payload["p0"]["cron_systemd"]["observed"] == 60
    assert payload["p0"]["cron_systemd"]["eligible"] == 60
    assert payload["p0"]["loops"]["observed"] == _SAMPLE_SIZE
    assert payload["p0"]["loops"]["eligible"] == _SAMPLE_SIZE
    for metric in payload["p0"].values():
        if metric["observed"] is not None and metric["eligible"] is not None:
            assert metric["observed"] <= metric["eligible"]
    assert payload["outcomes"]["observed"] <= payload["outcomes"]["eligible"]
    assert (
        payload["source_census"]["schema_version"]
        == SOURCE_CENSUS_VERSION
    )
    assert len(payload["source_census"]["sources"]) == 6


def test_writer_probe_reaches_twenty_without_inflating_tmux_source_events(
    tmp_path: Path,
) -> None:
    hermes_home = tmp_path / "hermes"
    hermes_home.mkdir()
    _seed_kanban(hermes_home / "kanban.db")
    _seed_cron(hermes_home / "cron" / "executions.db")
    _seed_loops(hermes_home / "loops" / "shadow" / "ledger.jsonl")

    result = collect_shadow(
        ShadowCollectionConfig(
            database=tmp_path / "execution_facts.db",
            hermes_home=hermes_home,
            evidence_dir=tmp_path / "evidence",
            include_systemd=False,
            include_crontab=False,
        ),
        runner=_SmallTmuxRunner(),
        clock_ms=lambda: _NOW_MS,
    )
    tmux = next(
        row
        for row in result.cohorts
        if row["source"] == "tmux_reconciliation"
    )

    assert tmux["sample_events"] == 6
    assert result.collector["accepted_by_source"]["tmux_reconciliation"] == 20


def test_missing_sources_stay_unknown_instead_of_becoming_zero(
    tmp_path: Path,
) -> None:
    hermes_home = tmp_path / "empty-hermes"
    hermes_home.mkdir()
    runner = _FakeRunner()
    database = tmp_path / "execution_facts.db"

    result = collect_shadow(
        ShadowCollectionConfig(
            database=database,
            hermes_home=hermes_home,
            evidence_dir=tmp_path / "evidence",
            include_systemd=False,
            include_crontab=False,
        ),
        runner=runner,
        clock_ms=lambda: _NOW_MS,
    )
    payload = build_execution_facts_payload(database)
    rows = {row["source"]: row for row in result.cohorts}

    assert rows["kanban_timeline"]["eligible"] is None
    assert rows["hermes_cron"]["eligible"] is None
    assert rows["loop_ledger"]["eligible"] is None
    assert rows["kanban_timeline"]["writer_p95_us"] is None
    assert rows["hermes_cron"]["writer_p95_us"] is None
    assert rows["loop_ledger"]["writer_p95_us"] is None
    assert payload["p0"]["run_identity"]["computed_status"] == "unknown"
    assert payload["p0"]["run_identity"]["eligible"] is None
    assert payload["p0"]["run_identity"]["percent"] is None


def test_source_cohort_rejects_observed_counts_above_eligible() -> None:
    try:
        SourceCohort(
            source="fixture",
            events=(),
            eligible=1,
            identity_observed=2,
            metric_observed=0,
            eligibility_rule="fixture",
            store_count=1,
            read_errors=0,
            window_start_ms=1,
            window_end_ms=2,
            behavior_equivalent=True,
        )
    except ValueError as exc:
        assert "eligible" in str(exc)
    else:
        raise AssertionError("invalid cohort was accepted")


def test_usage_sample_cannot_attest_full_metric_coverage(
    tmp_path: Path,
) -> None:
    database = tmp_path / "usage.db"
    with sqlite3.connect(database) as connection:
        connection.execute(
            """
            CREATE TABLE run_usage_facts (
                run_id TEXT, origin TEXT, task_run_id TEXT, task_id TEXT,
                chain_id TEXT, board TEXT, provider TEXT, model TEXT,
                profile TEXT, billing_mode TEXT, serving_tier TEXT,
                reasoning_effort TEXT, input_tokens INTEGER,
                output_tokens INTEGER, cache_read_tokens INTEGER,
                cache_write_tokens INTEGER, reasoning_tokens INTEGER,
                finish_reason TEXT, error_type TEXT, duration_ms REAL,
                captured_at TEXT, source TEXT
            )
            """
        )
        connection.executemany(
            """
            INSERT INTO run_usage_facts VALUES (
                ?, 'hermes_agent', ?, NULL, NULL, 'default',
                'kimi-coding', 'k3', 'coder', 'subscription_included',
                NULL, NULL, 10, 2, 1, 1, NULL, 'stop', NULL, 10,
                ?, 'measured'
            )
            """,
            (
                (
                    f"usage-{ordinal}",
                    "shared-task-run",
                    f"2026-07-30T10:{ordinal:02d}:00+00:00",
                )
                for ordinal in range(21)
            ),
        )

    cohort = collect_usage_cohort(
        database,
        observed_at_ms=_NOW_MS,
        sample_limit=20,
    )

    assert cohort.eligible == 21
    assert cohort.identity_observed == 21
    assert cohort.metric_observed == 20
    assert cohort.observed == 1
    assert cohort.metric_coverage_passed is False


def test_kanban_denominator_includes_queued_run_without_started_at(
    tmp_path: Path,
) -> None:
    database = tmp_path / "kanban.db"
    with sqlite3.connect(database) as connection:
        connection.execute(
            "CREATE TABLE task_runs ("
            "id INTEGER PRIMARY KEY, task_id TEXT, profile TEXT, status TEXT, "
            "started_at INTEGER, ended_at INTEGER, outcome TEXT, "
            "worker_exit_kind TEXT, worker_exit_code INTEGER)"
        )
        connection.executemany(
            "INSERT INTO task_runs VALUES (?, ?, 'coder', ?, ?, ?, ?, ?, ?)",
            (
                (1, "started", "done", 1, 2, "done", "clean", 0),
                (2, "queued", "queued", None, None, None, None, None),
            ),
        )
        connection.execute(
            "CREATE TABLE worker_run_timeline_events ("
            "task_run_id INTEGER, event_kind TEXT, observed_at_ms INTEGER, "
            "source TEXT, task_id TEXT, board TEXT, chain_root_id TEXT, "
            "profile TEXT)"
        )
        connection.execute(
            "INSERT INTO worker_run_timeline_events VALUES "
            "(2, 'queued', 1000, 'fixture', 'queued', 'default', NULL, 'coder')"
        )

    cohort = collect_kanban_cohort(database, observed_at_ms=2_000)

    assert cohort.eligible == 2
    assert cohort.observed == 2
    assert cohort.metric_observed == 2
    assert cohort.behavior_equivalent is True


class _PartialSystemRunner(_FakeRunner):
    def run(
        self,
        argv: Sequence[str],
    ) -> subprocess.CompletedProcess[str]:
        command = tuple(argv)
        if command[:3] == ("systemctl", "--user", "show") and command[3] == (
            "shadow-19.service"
        ):
            self.calls.append(command)
            return subprocess.CompletedProcess(command, 1, "", "failed")
        return super().run(argv)


def test_systemd_partial_read_cannot_shrink_denominator_or_pass() -> None:
    cohort = collect_systemd_cohort(
        observed_at_ms=_NOW_MS,
        runner=_PartialSystemRunner(),
    )

    assert cohort.eligible is None
    assert cohort.read_errors == 1
    assert cohort.behavior_equivalent is False
    assert cohort.validity.value == "unknown"


class _MissingSystemdOutcomeRunner(_FakeRunner):
    def run(
        self,
        argv: Sequence[str],
    ) -> subprocess.CompletedProcess[str]:
        result = super().run(argv)
        command = tuple(argv)
        if command[:3] == ("systemctl", "--user", "show"):
            output = "\n".join(
                line
                for line in result.stdout.splitlines()
                if not line.startswith("ExecMainStatus=")
            )
            return subprocess.CompletedProcess(command, 0, output, "")
        return result


def test_systemd_missing_exit_status_stays_unknown_not_zero() -> None:
    cohort = collect_systemd_cohort(
        observed_at_ms=_NOW_MS,
        runner=_MissingSystemdOutcomeRunner(),
    )

    assert cohort.eligible is None
    assert cohort.read_errors == _SAMPLE_SIZE
    assert cohort.behavior_equivalent is False
    assert all(event.exit_code is None for event in cohort.events)


class _MissingSystemdPropertyRunner(_FakeRunner):
    def __init__(self, property_name: str) -> None:
        super().__init__()
        self.property_name = property_name

    def run(
        self,
        argv: Sequence[str],
    ) -> subprocess.CompletedProcess[str]:
        result = super().run(argv)
        command = tuple(argv)
        if command[:3] == ("systemctl", "--user", "show"):
            prefix = f"{self.property_name}="
            output = "\n".join(
                line
                for line in result.stdout.splitlines()
                if not line.startswith(prefix)
            )
            return subprocess.CompletedProcess(command, 0, output, "")
        return result


@pytest.mark.parametrize(
    "property_name",
    (
        "InvocationID",
        "ExecMainStartTimestamp",
        "ExecMainExitTimestamp",
        "Result",
        "ExecMainStatus",
    ),
)
def test_systemd_missing_requested_property_fails_closed(
    property_name: str,
) -> None:
    cohort = collect_systemd_cohort(
        observed_at_ms=_NOW_MS,
        runner=_MissingSystemdPropertyRunner(property_name),
    )

    assert cohort.eligible is None
    assert cohort.read_errors == _SAMPLE_SIZE
    assert cohort.behavior_equivalent is False


def test_systemd_realtime_parser_preserves_exact_milliseconds() -> None:
    parse = execution_facts_shadow._systemd_realtime_epoch_ms

    assert parse("Thu 2026-07-30 10:20:00.123456 UTC") == 1_785_406_800_123
    assert parse("") is None
    with pytest.raises(ValueError, match="systemd realtime"):
        parse("not-a-timestamp")


@pytest.mark.parametrize(
    "invalid_line",
    (
        "[]",
        '{"pack":"shadow","event":"phase"}',
    ),
)
def test_loop_parse_or_identity_errors_make_denominator_unknown(
    tmp_path: Path,
    invalid_line: str,
) -> None:
    ledger = tmp_path / "loops" / "shadow" / "ledger.jsonl"
    ledger.parent.mkdir(parents=True)
    ledger.write_text(invalid_line + "\n", encoding="utf-8")

    cohort = collect_loop_cohort(tmp_path, observed_at_ms=_NOW_MS)

    assert cohort.eligible is None
    assert cohort.read_errors == 1
    assert cohort.behavior_equivalent is False


def test_loop_pack_can_be_derived_from_ledger_directory(
    tmp_path: Path,
) -> None:
    ledger = tmp_path / "loops" / "derived-pack" / "ledger.jsonl"
    ledger.parent.mkdir(parents=True)
    ledger.write_text(
        (
            '{"ts":"2026-07-30T10:00:00+00:00",'
            '"event":"phase","phase":"build"}\n'
        ),
        encoding="utf-8",
    )

    cohort = collect_loop_cohort(tmp_path, observed_at_ms=_NOW_MS)

    assert cohort.eligible == 1
    assert cohort.read_errors == 0
    assert cohort.observed == 1
    assert cohort.validity.value == "exact"


class _InvalidJournalRunner:
    def __init__(self) -> None:
        self.calls: list[tuple[str, ...]] = []

    def run(
        self,
        argv: Sequence[str],
    ) -> subprocess.CompletedProcess[str]:
        command = tuple(argv)
        self.calls.append(command)
        return subprocess.CompletedProcess(
            command,
            0,
            '{"_BOOT_ID":"boot","_PID":"1","__REALTIME_TIMESTAMP":"1000",'
            '"MESSAGE":"(piet) CMD (/home/piet/job.sh)"}\n'
            "not-json\n",
            "",
        )


def test_crontab_reads_full_window_and_fails_closed_on_parse_error() -> None:
    runner = _InvalidJournalRunner()
    cohort = collect_crontab_cohort(
        observed_at_ms=_NOW_MS,
        window_days=30,
        runner=runner,
    )

    assert "-n" not in runner.calls[0]
    assert cohort.eligible is None
    assert cohort.read_errors == 1
    assert cohort.behavior_equivalent is False


class _RealisticCronRunner(_FakeRunner):
    """Serve CRON journal lines in the shape journalctl actually emits."""

    def __init__(self, records: Sequence[tuple[str, int, str]]) -> None:
        super().__init__()
        self._records = tuple(records)

    def run(
        self,
        argv: Sequence[str],
    ) -> subprocess.CompletedProcess[str]:
        command = tuple(argv)
        self.calls.append(command)
        payload = "".join(
            json.dumps(
                {
                    "_BOOT_ID": "boot-a",
                    "_PID": pid,
                    "__REALTIME_TIMESTAMP": str(timestamp_ms * 1000),
                    "MESSAGE": message,
                }
            )
            + "\n"
            for pid, timestamp_ms, message in self._records
        )
        return subprocess.CompletedProcess(command, 0, payload, "")


_CRON_RUN_MS = _NOW_MS - 3 * 86_400_000


def test_crontab_identity_survives_journal_rotation() -> None:
    """Losing older journal records must not restate a retained fact.

    The journal rotates independently of our window. Any run that is still
    readable has to keep the exact identity and observed time it had before,
    otherwise the same idempotency key describes a different fact and the
    whole shadow batch is rejected -- the live failure of 2026-07-31.
    """
    older_run_ms = _NOW_MS - 20 * 86_400_000
    retained_run_ms = _NOW_MS - 5 * 86_400_000
    job = "(piet) CMD (/home/piet/job-a.sh)"

    def identities(records):
        cohort = collect_crontab_cohort(
            observed_at_ms=_NOW_MS,
            window_days=30,
            runner=_RealisticCronRunner(records),
        )
        return {
            event.source_execution_id: event.observed_at_ms
            for event in cohort.events
        }

    before = identities(
        [("100", older_run_ms, job), ("101", retained_run_ms, job)]
    )
    after = identities([("101", retained_run_ms, job)])

    survivors = set(before) & set(after)
    assert survivors, "rotation dropped every identity"
    for identity in survivors:
        assert before[identity] == after[identity], (
            f"{identity} changed its observed time across rotation"
        )


def test_crontab_identity_is_independent_of_pid_reuse() -> None:
    """A recycled PID must not collapse two runs into one execution."""
    first_ms = _NOW_MS - 9 * 86_400_000
    recycled_ms = _NOW_MS - 86_400_000
    job = "(piet) CMD (/home/piet/job-a.sh)"

    cohort = collect_crontab_cohort(
        observed_at_ms=_NOW_MS,
        window_days=30,
        runner=_RealisticCronRunner(
            [("4242", first_ms, job), ("4242", recycled_ms, job)]
        ),
    )

    observed = sorted(event.observed_at_ms for event in cohort.events)
    assert observed == [first_ms, recycled_ms]


def test_crontab_session_noise_is_not_counted_as_execution() -> None:
    """PAM session lines and MTA notices are not job runs.

    On this host they were 65.2% and 2.2% of everything the collector called
    a crontab execution.
    """
    cohort = collect_crontab_cohort(
        observed_at_ms=_NOW_MS,
        window_days=30,
        runner=_RealisticCronRunner(
            [
                # One real job, framed by its own session lines.
                (
                    "100",
                    _CRON_RUN_MS,
                    "pam_unix(cron:session): session opened for user "
                    "piet(uid=1000) by piet(uid=0)",
                ),
                ("100", _CRON_RUN_MS + 1, "(piet) CMD (/home/piet/job.sh)"),
                ("100", _CRON_RUN_MS + 900, "(CRON) info (No MTA installed)"),
                (
                    "100",
                    _CRON_RUN_MS + 1_000,
                    "pam_unix(cron:session): session closed for user piet",
                ),
                # A session pair on its own PID that never ran a command:
                # counting these is what inflated the source by 65%.
                (
                    "200",
                    _CRON_RUN_MS + 7_200_000,
                    "pam_unix(cron:session): session opened for user "
                    "root(uid=0) by root(uid=0)",
                ),
                (
                    "200",
                    _CRON_RUN_MS + 7_201_000,
                    "pam_unix(cron:session): session closed for user root",
                ),
            ]
        ),
    )

    assert cohort.eligible == 1
    assert len(cohort.events) == 1


def test_crontab_job_identity_is_exact_and_names_the_job() -> None:
    """The command is right there in MESSAGE; identity must not discard it."""
    cohort = collect_crontab_cohort(
        observed_at_ms=_NOW_MS,
        window_days=30,
        runner=_RealisticCronRunner(
            [
                ("100", _CRON_RUN_MS, "(piet) CMD (/home/piet/job-a.sh)"),
                ("101", _CRON_RUN_MS + 60_000, "(root) CMD (/usr/bin/job-b)"),
            ]
        ),
    )

    identities = sorted(event.source_execution_id for event in cohort.events)
    assert len(identities) == 2
    # Same job on different days shares a stable job scope; the run differs.
    assert all(event.validity is Validity.EXACT for event in cohort.events)
    assert any("piet" in identity for identity in identities)
    assert any("root" in identity for identity in identities)
    assert cohort.identity_observed == 2


def test_crontab_same_job_is_one_scope_across_runs() -> None:
    """Two runs of one job must be two executions of the *same* job."""
    cohort = collect_crontab_cohort(
        observed_at_ms=_NOW_MS,
        window_days=30,
        runner=_RealisticCronRunner(
            [
                ("100", _CRON_RUN_MS, "(piet) CMD (/home/piet/job-a.sh)"),
                ("777", _CRON_RUN_MS + 3_600_000, "(piet) CMD (/home/piet/job-a.sh)"),
            ]
        ),
    )

    identities = [event.source_execution_id for event in cohort.events]
    assert len(identities) == 2
    assert len(set(identities)) == 2, "runs must stay distinct executions"
    # Strip the trailing run timestamp: the job scope has to be identical.
    scopes = {identity.rsplit(":", 1)[0] for identity in identities}
    assert len(scopes) == 1, f"same job produced different scopes: {scopes}"


def test_crontab_unparseable_message_stays_unknown_not_dropped() -> None:
    cohort = collect_crontab_cohort(
        observed_at_ms=_NOW_MS,
        window_days=30,
        runner=_RealisticCronRunner(
            [("100", _CRON_RUN_MS, "something entirely unexpected")]
        ),
    )

    assert cohort.eligible == 0
    assert cohort.events == ()


def test_full_usage_scan_does_not_require_configuring_fees(
    tmp_path: Path,
) -> None:
    """Reading every usage row must not hinge on fee configuration.

    The sample limit capped the cost denominator at 200 rows out of 109356,
    and the only way to lift it was to supply subscription fees -- so the
    honest population was reachable only as a side effect.
    """
    usage_database = tmp_path / "usage_facts.db"
    with sqlite3.connect(usage_database) as connection:
        connection.execute(
            """
            CREATE TABLE run_usage_facts (
                run_id TEXT PRIMARY KEY, origin TEXT, task_run_id TEXT,
                task_id TEXT, chain_id TEXT, board TEXT, provider TEXT,
                model TEXT, profile TEXT, billing_mode TEXT,
                serving_tier TEXT, reasoning_effort TEXT,
                input_tokens INTEGER, output_tokens INTEGER,
                cache_read_tokens INTEGER, cache_write_tokens INTEGER,
                reasoning_tokens INTEGER, finish_reason TEXT,
                error_type TEXT, duration_ms REAL, captured_at TEXT,
                source TEXT
            )
            """
        )
        connection.executemany(
            "INSERT INTO run_usage_facts (run_id, origin, provider, model,"
            " billing_mode, input_tokens, output_tokens, cache_read_tokens,"
            " cache_write_tokens, captured_at, source) VALUES"
            " (?, 'claude_code', 'anthropic', 'claude-sonnet-5',"
            " 'subscription_included', 10, 5, 0, 0,"
            " '2026-07-31T08:00:00+00:00', 'measured')",
            [(f"run-{index}",) for index in range(25)],
        )

    capped = collect_usage_cohort(
        usage_database, observed_at_ms=_NOW_MS, sample_limit=5
    )
    full = collect_usage_cohort(
        usage_database, observed_at_ms=_NOW_MS, sample_limit=0
    )

    assert capped.observed == 5
    assert full.observed == 25
    assert full.eligible == 25


class _ManagedPaneRunner(_FakeRunner):
    """A tmux server whose panes carry the managed task binding."""

    def run(
        self,
        argv: Sequence[str],
    ) -> subprocess.CompletedProcess[str]:
        command = tuple(argv)
        if command[0] == "tmux":
            self.calls.append(command)
            rows = [
                "\x1f".join(
                    (
                        f"${ordinal}",
                        f"@{ordinal}",
                        f"%{ordinal}",
                        str(10_000 + ordinal),
                        "0",
                        f"{ordinal:032x}",
                        "t_ab12cd34" if ordinal < 2 else "",
                        str(8800 + ordinal) if ordinal < 2 else "",
                    )
                )
                for ordinal in range(4)
            ]
            return subprocess.CompletedProcess(
                command, 0, "\n".join(rows), ""
            )
        return super().run(argv)


def test_managed_panes_bind_to_their_kanban_run(tmp_path: Path) -> None:
    """task_binding_projection was empty because nothing ever bound.

    agent_terminals sets @hermes_task_id/@hermes_run_id on managed windows
    and the adapter could record bindings, but no production caller connected
    the two -- so no execution could be joined to the task it worked on.
    """
    hermes_home = tmp_path / "hermes"
    hermes_home.mkdir()
    _seed_kanban(hermes_home / "kanban.db")
    _seed_cron(hermes_home / "cron" / "executions.db")
    _seed_loops(hermes_home / "loops" / "shadow" / "ledger.jsonl")
    database = tmp_path / "execution_facts.db"

    collect_shadow(
        ShadowCollectionConfig(
            database=database,
            hermes_home=hermes_home,
            evidence_dir=tmp_path / "evidence",
            include_systemd=False,
            include_crontab=False,
        ),
        runner=_ManagedPaneRunner(),
        clock_ms=lambda: _NOW_MS,
    )

    with sqlite3.connect(database) as connection:
        bindings = connection.execute(
            "SELECT task_run_id FROM task_binding_projection"
        ).fetchall()

    assert {row[0] for row in bindings} == {"8800", "8801"}


def test_unmanaged_panes_produce_no_binding(tmp_path: Path) -> None:
    """A pane without a task is normal and must not invent a binding."""
    hermes_home = tmp_path / "hermes"
    hermes_home.mkdir()
    _seed_kanban(hermes_home / "kanban.db")
    _seed_cron(hermes_home / "cron" / "executions.db")
    _seed_loops(hermes_home / "loops" / "shadow" / "ledger.jsonl")
    database = tmp_path / "execution_facts.db"

    collect_shadow(
        ShadowCollectionConfig(
            database=database,
            hermes_home=hermes_home,
            evidence_dir=tmp_path / "evidence",
            include_systemd=False,
            include_crontab=False,
        ),
        runner=_SmallTmuxRunner(),
        clock_ms=lambda: _NOW_MS,
    )

    with sqlite3.connect(database) as connection:
        count = connection.execute(
            "SELECT COUNT(*) FROM task_binding_projection"
        ).fetchone()[0]

    assert count == 0
