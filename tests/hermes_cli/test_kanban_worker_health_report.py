from __future__ import annotations

import argparse
import json
import textwrap
from pathlib import Path

import pytest

from hermes_cli import kanban as kanban_cli
from hermes_cli import kanban_db as kb
from hermes_cli import kanban_report as kr


NOW = 1_800_000_000


@pytest.fixture
def kanban_home(tmp_path, monkeypatch):
    home = tmp_path / ".hermes"
    home.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(home))
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    kb.init_db()
    return home


def _parse_kanban(argv: list[str]) -> argparse.Namespace:
    parser = argparse.ArgumentParser()
    sub = parser.add_subparsers(dest="command")
    kanban_cli.build_parser(sub)
    return parser.parse_args(["kanban", *argv])


def _insert_run(
    conn,
    *,
    task_id: str,
    profile: str = "coder",
    status: str = "done",
    started_delta: int = 3600,
    ended_delta: int | None = 3000,
    exit_kind: str | None = None,
    protocol_state: str | None = None,
    fingerprint: str | None = None,
    last_heartbeat_delta: int | None = None,
) -> int:
    ended_at = None if ended_delta is None else NOW - ended_delta
    last_heartbeat_at = None if last_heartbeat_delta is None else NOW - last_heartbeat_delta
    cur = conn.execute(
        """
        INSERT INTO task_runs (
            task_id, profile, status, started_at, ended_at,
            worker_exit_kind, worker_protocol_state,
            worker_failure_fingerprint, last_heartbeat_at
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            task_id,
            profile,
            status,
            NOW - started_delta,
            ended_at,
            exit_kind,
            protocol_state,
            fingerprint,
            last_heartbeat_at,
        ),
    )
    return int(cur.lastrowid)


def _render(conn) -> str:
    return kr.render_worker_health_report(kr.worker_health_report(conn, now=NOW))


def test_worker_health_crash_rate_by_profile_snapshot(kanban_home):
    with kb.connect() as conn:
        _insert_run(conn, task_id="t1", profile="coder")
        _insert_run(conn, task_id="t2", profile="coder", status="crashed", exit_kind="nonzero_exit")
        _insert_run(conn, task_id="t3", profile="reviewer", status="crashed", exit_kind="signaled")
        _insert_run(conn, task_id="t4", profile="reviewer", exit_kind="clean_exit_protocol_violation")
        conn.commit()

        assert _render(conn) == textwrap.dedent(
            """\
            # Kanban Worker Health Report

            Generated: 2027-01-15T08:00:00Z
            Window: last 30d since 2026-12-16T08:00:00Z

            ## Crash rate by profile

            | Profile | Runs | Crashes | Crash rate |
            |---|---:|---:|---:|
            | reviewer | 2 | 2 | 100.0% |
            | coder | 2 | 1 | 50.0% |

            ## Top worker_failure_fingerprint clusters

            | Fingerprint | Count | Profiles |
            |---|---:|---|
            | _No failure fingerprints in window_ | 0 |  |

            ## Protocol violations

            Total: 1

            | Profile | Violations |
            |---|---:|
            | reviewer | 1 |

            ## Stale age histogram

            | Age since heartbeat/start | Running runs |
            |---|---:|
            | <5m | 0 |
            | 5-15m | 0 |
            | 15-60m | 0 |
            | 1-6h | 0 |
            | 6-24h | 0 |
            | >24h | 0 |

            ## Heartbeat coverage

            Runs with heartbeat: 0 / 4 (0.0%)
            """
        )


def test_worker_health_top_failure_fingerprint_snapshot(kanban_home):
    with kb.connect() as conn:
        for idx in range(3):
            _insert_run(conn, task_id=f"boom{idx}", fingerprint="pid n exited with code 7")
        for idx in range(2):
            _insert_run(conn, task_id=f"scope{idx}", profile="reviewer", fingerprint="missing completion")
        _insert_run(conn, task_id="sig", profile="coordinator", fingerprint="pid n killed by signal 9")
        conn.commit()

        assert _render(conn) == textwrap.dedent(
            """\
            # Kanban Worker Health Report

            Generated: 2027-01-15T08:00:00Z
            Window: last 30d since 2026-12-16T08:00:00Z

            ## Crash rate by profile

            | Profile | Runs | Crashes | Crash rate |
            |---|---:|---:|---:|
            | coder | 3 | 0 | 0.0% |
            | coordinator | 1 | 0 | 0.0% |
            | reviewer | 2 | 0 | 0.0% |

            ## Top worker_failure_fingerprint clusters

            | Fingerprint | Count | Profiles |
            |---|---:|---|
            | pid n exited with code 7 | 3 | coder |
            | missing completion | 2 | reviewer |
            | pid n killed by signal 9 | 1 | coordinator |

            ## Protocol violations

            Total: 0

            | Profile | Violations |
            |---|---:|
            | _No protocol violations in window_ | 0 |

            ## Stale age histogram

            | Age since heartbeat/start | Running runs |
            |---|---:|
            | <5m | 0 |
            | 5-15m | 0 |
            | 15-60m | 0 |
            | 1-6h | 0 |
            | 6-24h | 0 |
            | >24h | 0 |

            ## Heartbeat coverage

            Runs with heartbeat: 0 / 6 (0.0%)
            """
        )


def test_worker_health_protocol_violation_breakdown_snapshot(kanban_home):
    with kb.connect() as conn:
        _insert_run(conn, task_id="c1", profile="coder", exit_kind="clean_exit_protocol_violation")
        _insert_run(conn, task_id="c2", profile="coder", exit_kind="clean_exit_protocol_violation")
        _insert_run(conn, task_id="r1", profile="reviewer", exit_kind="clean_exit_protocol_violation")
        _insert_run(conn, task_id="ok", profile="reviewer", exit_kind="clean_exit_complete")
        conn.commit()

        assert _render(conn) == textwrap.dedent(
            """\
            # Kanban Worker Health Report

            Generated: 2027-01-15T08:00:00Z
            Window: last 30d since 2026-12-16T08:00:00Z

            ## Crash rate by profile

            | Profile | Runs | Crashes | Crash rate |
            |---|---:|---:|---:|
            | coder | 2 | 2 | 100.0% |
            | reviewer | 2 | 1 | 50.0% |

            ## Top worker_failure_fingerprint clusters

            | Fingerprint | Count | Profiles |
            |---|---:|---|
            | _No failure fingerprints in window_ | 0 |  |

            ## Protocol violations

            Total: 3

            | Profile | Violations |
            |---|---:|
            | coder | 2 |
            | reviewer | 1 |

            ## Stale age histogram

            | Age since heartbeat/start | Running runs |
            |---|---:|
            | <5m | 0 |
            | 5-15m | 0 |
            | 15-60m | 0 |
            | 1-6h | 0 |
            | 6-24h | 0 |
            | >24h | 0 |

            ## Heartbeat coverage

            Runs with heartbeat: 0 / 4 (0.0%)
            """
        )


def test_worker_health_stale_age_histogram_snapshot(kanban_home):
    with kb.connect() as conn:
        _insert_run(conn, task_id="fresh", status="running", ended_delta=None, started_delta=120)
        _insert_run(conn, task_id="mid", status="running", ended_delta=None, started_delta=900)
        _insert_run(
            conn,
            task_id="heartbeat",
            status="running",
            ended_delta=None,
            started_delta=90_000,
            last_heartbeat_delta=3700,
        )
        _insert_run(conn, task_id="stale", status="running", ended_delta=None, started_delta=90_000)
        conn.commit()

        assert _render(conn) == textwrap.dedent(
            """\
            # Kanban Worker Health Report

            Generated: 2027-01-15T08:00:00Z
            Window: last 30d since 2026-12-16T08:00:00Z

            ## Crash rate by profile

            | Profile | Runs | Crashes | Crash rate |
            |---|---:|---:|---:|
            | coder | 4 | 0 | 0.0% |

            ## Top worker_failure_fingerprint clusters

            | Fingerprint | Count | Profiles |
            |---|---:|---|
            | _No failure fingerprints in window_ | 0 |  |

            ## Protocol violations

            Total: 0

            | Profile | Violations |
            |---|---:|
            | _No protocol violations in window_ | 0 |

            ## Stale age histogram

            | Age since heartbeat/start | Running runs |
            |---|---:|
            | <5m | 1 |
            | 5-15m | 0 |
            | 15-60m | 1 |
            | 1-6h | 1 |
            | 6-24h | 0 |
            | >24h | 1 |

            ## Heartbeat coverage

            Runs with heartbeat: 1 / 4 (25.0%)
            """
        )


def test_worker_health_heartbeat_coverage_snapshot(kanban_home):
    with kb.connect() as conn:
        run_with_column = _insert_run(
            conn,
            task_id="column",
            profile="coder",
            last_heartbeat_delta=30,
        )
        run_with_event = _insert_run(conn, task_id="event", profile="reviewer")
        _insert_run(conn, task_id="missing", profile="coordinator")
        conn.execute(
            "INSERT INTO task_events (task_id, run_id, kind, payload, created_at) VALUES (?, ?, ?, ?, ?)",
            ("event", run_with_event, "heartbeat", json.dumps({}), NOW - 20),
        )
        conn.execute(
            "INSERT INTO task_events (task_id, run_id, kind, payload, created_at) VALUES (?, ?, ?, ?, ?)",
            ("old", run_with_column, "heartbeat", json.dumps({}), NOW - 40 * 86400),
        )
        conn.commit()

        assert _render(conn) == textwrap.dedent(
            """\
            # Kanban Worker Health Report

            Generated: 2027-01-15T08:00:00Z
            Window: last 30d since 2026-12-16T08:00:00Z

            ## Crash rate by profile

            | Profile | Runs | Crashes | Crash rate |
            |---|---:|---:|---:|
            | coder | 1 | 0 | 0.0% |
            | coordinator | 1 | 0 | 0.0% |
            | reviewer | 1 | 0 | 0.0% |

            ## Top worker_failure_fingerprint clusters

            | Fingerprint | Count | Profiles |
            |---|---:|---|
            | _No failure fingerprints in window_ | 0 |  |

            ## Protocol violations

            Total: 0

            | Profile | Violations |
            |---|---:|
            | _No protocol violations in window_ | 0 |

            ## Stale age histogram

            | Age since heartbeat/start | Running runs |
            |---|---:|
            | <5m | 0 |
            | 5-15m | 0 |
            | 15-60m | 0 |
            | 1-6h | 0 |
            | 6-24h | 0 |
            | >24h | 0 |

            ## Heartbeat coverage

            Runs with heartbeat: 2 / 3 (66.7%)
            """
        )


def test_kanban_report_worker_health_cli_snapshot(kanban_home, capsys, monkeypatch):
    monkeypatch.setattr(kr.time, "time", lambda: NOW)
    with kb.connect() as conn:
        _insert_run(conn, task_id="cli", profile="coder", status="crashed", exit_kind="nonzero_exit")
        conn.commit()

    rc = kanban_cli.kanban_command(_parse_kanban(["report", "worker-health"]))
    out = capsys.readouterr().out

    assert rc == 0
    assert out == textwrap.dedent(
        """\
        # Kanban Worker Health Report

        Generated: 2027-01-15T08:00:00Z
        Window: last 30d since 2026-12-16T08:00:00Z

        ## Crash rate by profile

        | Profile | Runs | Crashes | Crash rate |
        |---|---:|---:|---:|
        | coder | 1 | 1 | 100.0% |

        ## Top worker_failure_fingerprint clusters

        | Fingerprint | Count | Profiles |
        |---|---:|---|
        | _No failure fingerprints in window_ | 0 |  |

        ## Protocol violations

        Total: 0

        | Profile | Violations |
        |---|---:|
        | _No protocol violations in window_ | 0 |

        ## Stale age histogram

        | Age since heartbeat/start | Running runs |
        |---|---:|
        | <5m | 0 |
        | 5-15m | 0 |
        | 15-60m | 0 |
        | 1-6h | 0 |
        | 6-24h | 0 |
        | >24h | 0 |

        ## Heartbeat coverage

        Runs with heartbeat: 0 / 1 (0.0%)

        """
    )
