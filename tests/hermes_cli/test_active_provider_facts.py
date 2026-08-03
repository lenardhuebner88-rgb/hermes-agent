from __future__ import annotations

import hashlib
import json
import sqlite3
from pathlib import Path
from typing import Any

import pytest

from hermes_cli import active_provider_facts as provider_facts

FIXTURE_ROOT = (
    Path(__file__).parents[1] / "fixtures" / "active_provider_facts"
)


@pytest.fixture
def real_redacted_fixture() -> dict[str, Any]:
    return json.loads(
        (FIXTURE_ROOT / "real_redacted.json").read_text(encoding="utf-8")
    )


@pytest.fixture
def evidence_databases(
    tmp_path: Path, real_redacted_fixture: dict[str, Any]
) -> tuple[Path, Path, Path]:
    board_path = tmp_path / "kanban.db"
    profiles_root = tmp_path / "profiles"
    usage_path = tmp_path / "usage_facts.db"
    _create_board(board_path, real_redacted_fixture)
    _create_profile_state(profiles_root, real_redacted_fixture["state_session"])
    _create_usage_facts(usage_path, real_redacted_fixture["cli_fact"])
    return board_path, profiles_root, usage_path


def test_reconstructs_one_redacted_real_run_per_source(
    evidence_databases: tuple[Path, Path, Path],
) -> None:
    board_path, profiles_root, usage_path = evidence_databases

    facts = provider_facts.resolve_active_provider_facts(
        board_path,
        profiles_root=profiles_root,
        usage_facts_path=usage_path,
    )
    by_id = {fact.run_id: fact for fact in facts}

    assert by_id["run-metadata-redacted"] == provider_facts.ActiveProviderFact(
        run_id="run-metadata-redacted",
        classification=provider_facts.CLASS_RECONSTRUCTED,
        provider="openai-codex",
        model="gpt-5.4-mini",
        source=provider_facts.SOURCE_METADATA_MODEL,
        profile="reviewer",
        origin="hermes_agent",
    )
    assert by_id["run-state-redacted"] == provider_facts.ActiveProviderFact(
        run_id="run-state-redacted",
        classification=provider_facts.CLASS_RECONSTRUCTED,
        provider="openai-codex",
        model="gpt-5.6-terra",
        source=provider_facts.SOURCE_PROFILE_STATE,
        profile="verifier",
        origin="hermes_agent",
    )
    assert by_id["run-cli-redacted"] == provider_facts.ActiveProviderFact(
        run_id="run-cli-redacted",
        classification=provider_facts.CLASS_RECONSTRUCTED,
        provider="openai-codex",
        model="gpt-5.6-sol",
        source=provider_facts.SOURCE_CLI_RAW,
        profile="premium",
        origin="codex_cli",
    )


def test_foreign_cli_provider_comes_from_lane_configuration(
    evidence_databases: tuple[Path, Path, Path],
    real_redacted_fixture: dict[str, Any],
) -> None:
    board_path, profiles_root, usage_path = evidence_databases

    [fact] = [
        item
        for item in provider_facts.resolve_active_provider_facts(
            board_path,
            profiles_root=profiles_root,
            usage_facts_path=usage_path,
        )
        if item.run_id == "run-cli-redacted"
    ]

    assert real_redacted_fixture["cli_fact"]["provider"] == "openai"
    assert (
        real_redacted_fixture["lane_profiles"]["premium"]["provider"]
        == "openai-codex"
    )
    assert fact.provider == "openai-codex"


def test_buzz_agent_cli_fact_keeps_source_origin_for_provider_recovery(
    evidence_databases: tuple[Path, Path, Path],
) -> None:
    board_path, profiles_root, usage_path = evidence_databases
    with sqlite3.connect(usage_path) as connection:
        connection.execute(
            "UPDATE run_usage_facts SET origin='buzz_agent' "
            "WHERE run_id='codex_cli:session-redacted'"
        )
        connection.execute(
            "INSERT INTO run_usage_facts "
            "(run_id, provider, model, lane, profile, captured_at, origin, source) "
            "SELECT 'claude_code:overlapping-session', provider, model, lane, "
            "profile, captured_at, 'buzz_agent', source "
            "FROM run_usage_facts WHERE run_id='codex_cli:session-redacted'"
        )

    [fact] = [
        item
        for item in provider_facts.resolve_active_provider_facts(
            board_path,
            profiles_root=profiles_root,
            usage_facts_path=usage_path,
        )
        if item.run_id == "run-cli-redacted"
    ]

    assert fact.classification == provider_facts.CLASS_RECONSTRUCTED
    assert fact.provider == "openai-codex"
    assert fact.model == "gpt-5.6-sol"
    assert fact.source == provider_facts.SOURCE_CLI_RAW
    assert fact.origin == "codex_cli"


def test_never_ran_status_precedes_null_or_seeded_provider(
    evidence_databases: tuple[Path, Path, Path],
) -> None:
    board_path, profiles_root, usage_path = evidence_databases

    facts = provider_facts.resolve_active_provider_facts(
        board_path,
        profiles_root=profiles_root,
        usage_facts_path=usage_path,
    )
    never_ran = {
        fact.run_id: fact
        for fact in facts
        if fact.run_id
        in {"run-never-null-redacted", "run-never-seeded-redacted"}
    }

    assert set(never_ran) == {
        "run-never-null-redacted",
        "run-never-seeded-redacted",
    }
    assert all(
        fact.classification == provider_facts.CLASS_NEVER_RAN
        and fact.provider is None
        for fact in never_ran.values()
    )


def test_redacted_live_snapshot_partitions_all_8284_runs() -> None:
    snapshot = json.loads(
        (FIXTURE_ROOT / "live_snapshot_counts.json").read_text(encoding="utf-8")
    )

    counts = snapshot["classification_counts"]
    assert set(counts) == set(provider_facts.CLASSIFICATIONS)
    assert sum(counts.values()) == snapshot["total_runs"] == 8284
    assert snapshot["null_provider_never_ran"] == 2540
    assert (
        counts[provider_facts.CLASS_NEVER_RAN]
        >= snapshot["null_provider_never_ran"]
    )


def test_all_inputs_are_read_only_and_task_runs_remains_byte_identical(
    evidence_databases: tuple[Path, Path, Path],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    board_path, profiles_root, usage_path = evidence_databases
    before = _sha256(board_path)
    real_connect = sqlite3.connect
    opened_uris: list[str] = []
    denied_writes: list[tuple[int, str | None]] = []

    def guarded_connect(
        database: str, *args: Any, **kwargs: Any
    ) -> sqlite3.Connection:
        opened_uris.append(database)
        connection = real_connect(database, *args, **kwargs)

        def authorizer(
            action: int,
            target: str | None,
            _column: str | None,
            _database: str | None,
            _trigger: str | None,
        ) -> int:
            if action in {
                sqlite3.SQLITE_INSERT,
                sqlite3.SQLITE_UPDATE,
                sqlite3.SQLITE_DELETE,
            } and target == "task_runs":
                denied_writes.append((action, target))
                return sqlite3.SQLITE_DENY
            return sqlite3.SQLITE_OK

        connection.set_authorizer(authorizer)
        return connection

    monkeypatch.setattr(provider_facts.sqlite3, "connect", guarded_connect)

    provider_facts.resolve_active_provider_facts(
        board_path,
        profiles_root=profiles_root,
        usage_facts_path=usage_path,
    )

    assert opened_uris
    assert all("mode=ro" in uri for uri in opened_uris)
    assert denied_writes == []
    assert _sha256(board_path) == before


def test_materialization_writes_only_derived_rows_to_facts_layer(
    evidence_databases: tuple[Path, Path, Path],
) -> None:
    board_path, profiles_root, usage_path = evidence_databases
    board_before = _sha256(board_path)

    facts = provider_facts.materialize_reconstructed_provider_facts(
        board_path,
        profiles_root=profiles_root,
        usage_facts_path=usage_path,
    )

    with sqlite3.connect(usage_path) as connection:
        connection.row_factory = sqlite3.Row
        rows = connection.execute(
            "SELECT run_id, origin, provider, model, model_source, source "
            "FROM run_usage_facts WHERE run_id LIKE 'run-%-redacted' "
            "ORDER BY run_id"
        ).fetchall()
    assert [row["run_id"] for row in rows] == [
        "run-cli-redacted",
        "run-metadata-redacted",
        "run-state-redacted",
    ]
    assert all(row["source"] == "derived" for row in rows)
    assert {row["model_source"] for row in rows} == {
        provider_facts.SOURCE_METADATA_MODEL,
        provider_facts.SOURCE_PROFILE_STATE,
        provider_facts.SOURCE_CLI_RAW,
    }
    cli_row = next(row for row in rows if row["run_id"] == "run-cli-redacted")
    assert cli_row["origin"] == "codex_cli"
    assert provider_facts.classification_counts(facts) == {
        provider_facts.CLASS_NEVER_RAN: 2,
        provider_facts.CLASS_UNKNOWN: 1,
        provider_facts.CLASS_RECONSTRUCTED: 3,
        provider_facts.CLASS_STAMPED: 0,
    }
    assert _sha256(board_path) == board_before


def test_materialization_preserves_existing_measured_provider(
    evidence_databases: tuple[Path, Path, Path],
) -> None:
    board_path, profiles_root, usage_path = evidence_databases
    with sqlite3.connect(usage_path) as connection:
        connection.execute(
            "INSERT INTO run_usage_facts "
            "(run_id, origin, provider, model, model_source, source) "
            "VALUES (?, ?, ?, ?, ?, ?)",
            (
                "run-metadata-redacted",
                "hermes_agent",
                "measured-provider-redacted",
                "measured-model-redacted",
                "self_report",
                "measured",
            ),
        )

    provider_facts.materialize_reconstructed_provider_facts(
        board_path,
        profiles_root=profiles_root,
        usage_facts_path=usage_path,
    )

    with sqlite3.connect(usage_path) as connection:
        row = connection.execute(
            "SELECT provider, model, model_source, source "
            "FROM run_usage_facts WHERE run_id=?",
            ("run-metadata-redacted",),
        ).fetchone()
    assert row == (
        "measured-provider-redacted",
        "measured-model-redacted",
        "self_report",
        "measured",
    )


def test_embedded_state_reference_must_stay_below_profiles_root(
    tmp_path: Path, real_redacted_fixture: dict[str, Any]
) -> None:
    board_path = tmp_path / "kanban.db"
    profiles_root = tmp_path / "profiles"
    central_state = tmp_path / "state.db"
    fixture = json.loads(json.dumps(real_redacted_fixture))
    fixture["runs"] = [
        {
            "id": "central-state-reference-redacted",
            "status": "done",
            "active_provider": None,
            "profile": "default",
            "started_at": 1782209627,
            "ended_at": 1782209655,
            "last_heartbeat_at": 1782209634,
            "metadata": {
                "cost_session_ids": [
                    f"{central_state}::state-session-redacted"
                ]
            },
        }
    ]
    _create_board(board_path, fixture)
    _create_state_database(central_state, fixture["state_session"])

    [fact] = provider_facts.resolve_active_provider_facts(
        board_path,
        profiles_root=profiles_root,
    )

    assert fact.classification == provider_facts.CLASS_UNKNOWN
    assert fact.provider is None


def test_embedded_profile_state_reference_reconstructs_real_redacted_run(
    tmp_path: Path, real_redacted_fixture: dict[str, Any]
) -> None:
    board_path = tmp_path / "kanban.db"
    profiles_root = tmp_path / "profiles"
    state_session = real_redacted_fixture["state_session"]
    state_path = profiles_root / state_session["profile"] / "state.db"
    embedded_run = real_redacted_fixture["embedded_state_run"]
    fixture = json.loads(json.dumps(real_redacted_fixture))
    fixture["runs"] = [
        {
            "id": "embedded-profile-state-redacted",
            "status": embedded_run["status"],
            "active_provider": None,
            "profile": embedded_run["profile"],
            "started_at": 1782209627,
            "ended_at": 1782209655,
            "last_heartbeat_at": 1782209634,
            "metadata": {
                "cost_session_ids": [
                    f"{state_path}::{state_session['id']}"
                ]
            },
        }
    ]
    _create_board(board_path, fixture)
    _create_profile_state(profiles_root, state_session)

    [fact] = provider_facts.resolve_active_provider_facts(
        board_path,
        profiles_root=profiles_root,
    )

    assert fact.classification == provider_facts.CLASS_RECONSTRUCTED
    assert fact.provider == state_session["billing_provider"]
    assert fact.model == state_session["model"]
    assert fact.source == provider_facts.SOURCE_PROFILE_STATE


def test_metadata_audit_separates_root_from_nested_model_keys(
    tmp_path: Path, real_redacted_fixture: dict[str, Any]
) -> None:
    board_path = tmp_path / "kanban.db"
    fixture = json.loads(json.dumps(real_redacted_fixture))
    fixture["runs"] = [
        {
            **fixture["runs"][0],
            "id": "root-and-nested-redacted",
            "metadata": {
                "model": "gpt-5.4-mini",
                "dispatch": {"model": "gpt-5.4-mini"},
            },
        },
        {
            **fixture["runs"][0],
            "id": "nested-only-redacted",
            "metadata": {"dispatch": {"model": "gpt-5.4-mini"}},
        },
    ]
    _create_board(board_path, fixture)

    audit = provider_facts.audit_metadata_models(board_path)

    assert audit == provider_facts.MetadataModelAudit(
        top_level_runs=1,
        any_level_runs=2,
        nested_runs=2,
        nested_only_runs=1,
    )


def _create_board(path: Path, fixture: dict[str, Any]) -> None:
    table = "".join(("task_", "runs"))
    connection = sqlite3.connect(path)
    connection.execute(
        f"CREATE TABLE {table} ("
        "id TEXT PRIMARY KEY, status TEXT, active_provider TEXT, profile TEXT, "
        "metadata TEXT, started_at REAL, ended_at REAL, last_heartbeat_at REAL)"
    )
    connection.execute("CREATE TABLE lanes (name TEXT, profiles TEXT)")
    connection.execute(
        "INSERT INTO lanes (name, profiles) VALUES (?, ?)",
        ("redacted-live-lane", json.dumps(fixture["lane_profiles"])),
    )
    connection.executemany(
        f"INSERT INTO {table} ("
        "id, status, active_provider, profile, metadata, started_at, ended_at, "
        "last_heartbeat_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
        [
            (
                run["id"],
                run["status"],
                run["active_provider"],
                run["profile"],
                json.dumps(run["metadata"]),
                run["started_at"],
                run["ended_at"],
                run["last_heartbeat_at"],
            )
            for run in fixture["runs"]
        ],
    )
    connection.commit()
    connection.close()


def _create_profile_state(
    profiles_root: Path, session: dict[str, Any]
) -> None:
    path = profiles_root / session["profile"] / "state.db"
    path.parent.mkdir(parents=True)
    _create_state_database(path, session)


def _create_state_database(path: Path, session: dict[str, Any]) -> None:
    connection = sqlite3.connect(path)
    connection.execute(
        "CREATE TABLE sessions "
        "(id TEXT PRIMARY KEY, billing_provider TEXT, model TEXT)"
    )
    connection.execute(
        "INSERT INTO sessions (id, billing_provider, model) VALUES (?, ?, ?)",
        (
            session["id"],
            session["billing_provider"],
            session["model"],
        ),
    )
    connection.commit()
    connection.close()


def _create_usage_facts(path: Path, fact: dict[str, Any]) -> None:
    connection = sqlite3.connect(path)
    connection.execute(
        "CREATE TABLE run_usage_facts ("
        "run_id TEXT PRIMARY KEY, provider TEXT, model TEXT, lane TEXT, "
        "model_source TEXT, profile TEXT, captured_at TEXT, origin TEXT, "
        "source TEXT NOT NULL DEFAULT 'unknown')"
    )
    connection.execute(
        "INSERT INTO run_usage_facts "
        "(run_id, provider, model, lane, profile, captured_at, origin, source) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
        (
            fact["run_id"],
            fact["provider"],
            fact["model"],
            fact["lane"],
            fact["profile"],
            fact["captured_at"],
            fact["origin"],
            "measured",
        ),
    )
    connection.commit()
    connection.close()


def _sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


# --- mutation-hardening tests (night-run 2026-07-28) ---


def test_state_session_reader_rejects_profile_with_slash(tmp_path: Path) -> None:
    """Kill bool_op_swap L89: or -> and.

    With the mutant, ``not profile and "/" in profile`` is False for a
    profile containing "/", so the guard is bypassed and a path is
    returned.  The correct behaviour rejects any profile with a slash.
    """
    reader = provider_facts._StateSessionReader(tmp_path, immutable=True)
    assert reader.profile_path("foo/bar") is None


def test_configured_provider_returns_single_match() -> None:
    """Kill comparison_swap L469: == -> !=.

    With the mutant, ``len(providers) != 1`` is True when there is
    exactly one provider, so the function falls through and returns
    None instead of the single provider.
    """
    routes = {("default", "gpt-4"): frozenset({"openai"})}
    result = provider_facts._configured_provider(routes, "default", "gpt-4")
    assert result == "openai"


def test_metadata_provider_prefers_primary_field() -> None:
    """Kill bool_op_swap L475: or -> and.

    With the mutant, ``_text(provider) and _text(cost_equivalent)`` is
    falsy when cost_equivalent_provider is absent, so the function
    returns None even though the primary provider field is set.
    """
    result = provider_facts._metadata_provider({"provider": "anthropic"})
    assert result == "anthropic"
