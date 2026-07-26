"""Cron-specific contracts for the idempotent Langfuse score export."""
from __future__ import annotations

import argparse
import sqlite3
from pathlib import Path


def test_cron_uses_posted_new_not_total_posted(monkeypatch, capsys) -> None:
    """AC-1: a ledger replay remains silent even if the old total is non-zero."""
    import hermes_cli.langfuse_scores_export as export_module
    from hermes_cli.kanban import _cmd_export_langfuse_scores

    monkeypatch.setattr(
        export_module,
        "export_scores",
        lambda **_kwargs: {"matched": 7, "unmatched": 0, "posted": 7, "posted_new": 0},
    )
    monkeypatch.setattr(export_module, "cron_export_alert", lambda **_kwargs: None)

    rc = _cmd_export_langfuse_scores(
        argparse.Namespace(dry_run=False, cron=True, backfill=True)
    )

    assert rc == 0
    assert capsys.readouterr().out == ""


def test_cron_reports_one_line_for_posted_new(monkeypatch, capsys) -> None:
    """AC-1: the first ledger delta produces exactly one delivery line."""
    import hermes_cli.langfuse_scores_export as export_module
    from hermes_cli.kanban import _cmd_export_langfuse_scores

    monkeypatch.setattr(
        export_module,
        "export_scores",
        lambda **_kwargs: {"matched": 7, "unmatched": 0, "posted": 7, "posted_new": 1},
    )
    monkeypatch.setattr(export_module, "cron_export_alert", lambda **_kwargs: None)

    rc = _cmd_export_langfuse_scores(
        argparse.Namespace(dry_run=False, cron=True, backfill=True)
    )
    captured = capsys.readouterr()

    assert rc == 0
    assert captured.out.splitlines() == [
        "langfuse-scores-export: posted 1 new score(s) to Langfuse"
    ]


def test_cron_command_passes_cron_mode_to_score_export(monkeypatch, capsys) -> None:
    """The CLI boundary enables the cron-only backfill safety cap."""
    import hermes_cli.langfuse_scores_export as export_module
    from hermes_cli.kanban import _cmd_export_langfuse_scores

    received: dict[str, object] = {}

    def fake_export_scores(**kwargs: object) -> dict[str, int]:
        received.update(kwargs)
        return {"matched": 0, "unmatched": 0, "posted": 0, "posted_new": 0}

    monkeypatch.setattr(export_module, "export_scores", fake_export_scores)
    monkeypatch.setattr(export_module, "cron_export_alert", lambda **_kwargs: None)

    rc = _cmd_export_langfuse_scores(
        argparse.Namespace(dry_run=False, cron=True, backfill=True, event_limit=None)
    )

    assert rc == 0
    assert received["cron"] is True
    assert capsys.readouterr().out == ""


def test_cron_alert_reports_cost_and_cache_thresholds_once(tmp_path: Path) -> None:
    """AC-2: both breaches become one compact alert string."""
    from hermes_cli.langfuse_scores_export import cron_export_alert

    db_path = tmp_path / "kanban.db"
    conn = sqlite3.connect(db_path)
    conn.execute("CREATE TABLE scores (name TEXT, value REAL, created_at INTEGER)")
    now = 1_728_086_400  # UTC midnight; keeps the daily-window fixture exact.
    day = 24 * 60 * 60
    # Seven completed daily p95 values are 1..7 (median 4); today's p95 is 10.
    for offset, value in enumerate(range(1, 8), start=1):
        conn.execute(
            "INSERT INTO scores VALUES ('run_cost_usd', ?, ?)",
            (value, now - offset * day + 60),
        )
    conn.executemany(
        "INSERT INTO scores VALUES ('run_cost_usd', ?, ?)",
        [(1.0, now + 60), (10.0, now + 120)],
    )
    conn.executemany(
        "INSERT INTO scores VALUES ('cache_hit_ratio', ?, ?)",
        [(0.10, now + 60), (0.20, now + 120)],
    )
    conn.commit()
    conn.close()

    alert = cron_export_alert(
        db_path=db_path, now=now + 3600, cost_p95_multiplier=2.0,
        cache_hit_ratio_threshold=0.20,
    )

    assert alert is not None
    assert "cost p95" in alert
    assert "cache hit ratio" in alert


def test_cron_alert_is_silent_when_metrics_are_within_thresholds(tmp_path: Path) -> None:
    """AC-2: healthy daily metrics cause no cron delivery."""
    from hermes_cli.langfuse_scores_export import cron_export_alert

    db_path = tmp_path / "kanban.db"
    conn = sqlite3.connect(db_path)
    conn.execute("CREATE TABLE scores (name TEXT, value REAL, created_at INTEGER)")
    now = 1_728_086_400
    day = 24 * 60 * 60
    for offset, value in enumerate(range(1, 8), start=1):
        conn.execute(
            "INSERT INTO scores VALUES ('run_cost_usd', ?, ?)",
            (value, now - offset * day + 60),
        )
    conn.executemany(
        "INSERT INTO scores VALUES ('run_cost_usd', ?, ?)",
        [(1.0, now + 60), (7.0, now + 120)],
    )
    conn.executemany(
        "INSERT INTO scores VALUES ('cache_hit_ratio', ?, ?)",
        [(0.35, now + 60), (0.45, now + 120)],
    )
    conn.commit()
    conn.close()

    assert cron_export_alert(
        db_path=db_path, now=now + 3600, cost_p95_multiplier=2.0,
        cache_hit_ratio_threshold=0.20,
    ) is None


def test_cron_alert_outputs_one_line_when_no_scores_are_new(monkeypatch, capsys) -> None:
    """AC-2: an alert bypasses silence with exactly one line."""
    import hermes_cli.langfuse_scores_export as export_module
    from hermes_cli.kanban import _cmd_export_langfuse_scores

    monkeypatch.setattr(
        export_module,
        "export_scores",
        lambda **_kwargs: {"matched": 3, "unmatched": 0, "posted": 3, "posted_new": 0},
    )
    monkeypatch.setattr(
        export_module,
        "cron_export_alert",
        lambda **_kwargs: "cache hit ratio 10.0% below 20.0%",
    )

    rc = _cmd_export_langfuse_scores(
        argparse.Namespace(dry_run=False, cron=True, backfill=True)
    )
    captured = capsys.readouterr()

    assert rc == 0
    assert captured.out.splitlines() == [
        "langfuse-scores-export: alert: cache hit ratio 10.0% below 20.0%"
    ]


def test_wrapper_uses_backfill_ledger() -> None:
    """The installed daily wrapper invokes the posted_new-aware export mode."""
    repo_root = Path(__file__).resolve().parents[2]
    wrapper = repo_root / "scripts" / "cron" / "export-langfuse-scores.sh"

    assert "export-langfuse-scores --cron --backfill" in wrapper.read_text(encoding="utf-8")
