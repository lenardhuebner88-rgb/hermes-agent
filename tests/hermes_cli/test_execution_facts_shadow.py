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
    assert run_identity["observed"] == 100
    assert run_identity["eligible"] == 120
    assert run_identity["value"] == 100
    assert run_identity["percent"] == "83.33"
    assert run_identity["unknown_reason"] is None
    assert payload["p0"]["terminal_identity"]["observed"] == _SAMPLE_SIZE
    assert payload["p0"]["terminal_identity"]["eligible"] == _SAMPLE_SIZE
    assert payload["p0"]["cron_systemd"]["observed"] == 40
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
            '{"_BOOT_ID":"boot","_PID":"1","__REALTIME_TIMESTAMP":"1000"}\n'
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


class _ScriptedJournalRunner(_FakeRunner):
    """Serve an exact set of CRON journal records for one collection pass."""

    def __init__(self, records: Sequence[tuple[str, str, int]]) -> None:
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
                    "_BOOT_ID": boot_id,
                    "_PID": pid,
                    "__REALTIME_TIMESTAMP": str(timestamp_ms * 1000),
                }
            )
            + "\n"
            for boot_id, pid, timestamp_ms in self._records
        )
        return subprocess.CompletedProcess(command, 0, payload, "")


def _crontab_identities(
    records: Sequence[tuple[str, str, int]],
) -> dict[str, int]:
    cohort = collect_crontab_cohort(
        observed_at_ms=_NOW_MS,
        window_days=30,
        runner=_ScriptedJournalRunner(records),
    )
    return {
        event.source_execution_id: event.observed_at_ms
        for event in cohort.events
    }


def test_crontab_reused_pid_is_not_merged_into_one_invocation() -> None:
    """A PID recycled days later is a second invocation, not the same one."""
    first_run_ms = _NOW_MS - 9 * 86_400_000
    recycled_run_ms = _NOW_MS - 86_400_000

    identities = _crontab_identities(
        [
            ("boot-a", "4242", first_run_ms),
            ("boot-a", "4242", first_run_ms + 120),
            ("boot-a", "4242", recycled_run_ms),
        ]
    )

    assert len(identities) == 2, identities
    assert sorted(identities.values()) == [first_run_ms, recycled_run_ms]


def test_crontab_identity_survives_journal_rotation() -> None:
    """Losing the oldest journal record must not restate a retained fact.

    The journal is rotated out from under the collector. Any invocation that
    is still readable has to keep the exact identity *and* observed time it
    had before, otherwise the same idempotency key describes a different fact
    and the whole shadow batch is rejected.
    """
    older_run_ms = _NOW_MS - 20 * 86_400_000
    retained_run_ms = _NOW_MS - 5 * 86_400_000

    before_rotation = _crontab_identities(
        [
            ("boot-a", "178442", older_run_ms),
            ("boot-a", "178442", retained_run_ms),
            ("boot-a", "178442", retained_run_ms + 2),
        ]
    )
    after_rotation = _crontab_identities(
        [
            ("boot-a", "178442", retained_run_ms),
            ("boot-a", "178442", retained_run_ms + 2),
        ]
    )

    survivors = set(before_rotation) & set(after_rotation)
    assert survivors, "rotation dropped every identity"
    for identity in survivors:
        assert before_rotation[identity] == after_rotation[identity], (
            f"{identity} changed its observed time across rotation: "
            f"{before_rotation[identity]} -> {after_rotation[identity]}"
        )


def test_crontab_multiline_records_stay_one_invocation() -> None:
    """CRON logs several lines per run; they are one execution, not many."""
    run_ms = _NOW_MS - 3 * 86_400_000

    identities = _crontab_identities(
        [
            ("boot-a", "77", run_ms),
            ("boot-a", "77", run_ms + 2),
            ("boot-a", "77", run_ms + 150),
        ]
    )

    assert len(identities) == 1
    assert next(iter(identities.values())) == run_ms


def test_poisoned_identity_neither_aborts_the_sweep_nor_hides(
    tmp_path: Path,
) -> None:
    """A restated identity keeps the sweep alive but stays visible.

    Regression for the live failure of 2026-07-31: one crontab identity
    restated its observed time, the batch rolled back, and 7 of 11 sources
    silently stopped collecting for hours while the unit just looked failed.
    """
    hermes_home = tmp_path / "hermes"
    hermes_home.mkdir()
    _seed_kanban(hermes_home / "kanban.db")
    _seed_cron(hermes_home / "cron" / "executions.db")
    _seed_loops(hermes_home / "loops" / "shadow" / "ledger.jsonl")
    database = tmp_path / "execution_facts.db"
    config = ShadowCollectionConfig(
        database=database,
        hermes_home=hermes_home,
        evidence_dir=tmp_path / "evidence",
        include_systemd=False,
        include_crontab=False,
    )

    first = collect_shadow(
        config, runner=_SmallTmuxRunner(), clock_ms=lambda: _NOW_MS
    )
    assert first.collector["identity_conflicts"] == 0

    # Reproduce the live shape: the dedupe registry holds a fingerprint that
    # no longer matches what the source now reports for the same identity.
    ledger = ExecutionFactsLedger(database)
    retained = next(
        event
        for event in ledger.iter_events()
        if event.source == "kanban_timeline"
    )
    with sqlite3.connect(database) as connection:
        connection.execute(
            """
            UPDATE execution_event_dedupe
               SET immutable_payload_sha256 = 'stale-fingerprint'
             WHERE source = ? AND idempotency_key = ?
            """,
            (retained.source, retained.idempotency_key),
        )
        connection.commit()

    second = collect_shadow(
        config, runner=_SmallTmuxRunner(), clock_ms=lambda: _NOW_MS
    )

    # The conflict is real, counted, and attributed to its source...
    assert second.collector["identity_conflicts"] >= 1
    assert (
        second.collector["identity_conflicts_by_source"]["kanban_timeline"]
        >= 1
    )
    # ...the affected source is no longer claimed as reconciled...
    kanban_row = next(
        row for row in second.cohorts if row["source"] == "kanban_timeline"
    )
    assert kanban_row["dedupe_reconciled"] is False
    # ...every other source still collected instead of being rolled back...
    assert {row["source"] for row in second.cohorts} == {
        row["source"] for row in first.cohorts
    }
    assert second.collector["reconciled_inserted"] >= 0
    # ...and the retained fact was never overwritten.
    restated = [
        event
        for event in ExecutionFactsLedger(database).iter_events()
        if event.idempotency_key == retained.idempotency_key
    ]
    assert len(restated) == 1
    assert restated[0].observed_at_ms == retained.observed_at_ms
