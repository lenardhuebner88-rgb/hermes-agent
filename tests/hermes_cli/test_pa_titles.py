"""S7.6: Titel-Destillation (gemeinsamer Helper für Briefing + Inbox)."""

from __future__ import annotations

from hermes_cli.pa_titles import (
    BRIEFING_TITLE_LIMIT,
    INBOX_SUMMARY_LIMIT,
    briefing_title,
    distill_title,
)


def test_distill_strips_task_prefix_kind_suffix_id_and_path() -> None:
    raw = (
        "Gate bei Task t_abcdef12: PlanSpec GATE "
        "/home/piet/vault/03-Agents/Codex/receipts/x.md — operator_release_required"
    )
    out = distill_title(raw)
    assert "t_abcdef12" not in out
    assert "operator_release_required" not in out
    assert "/home/piet" not in out
    assert "PlanSpec GATE" in out
    assert len(out) <= INBOX_SUMMARY_LIMIT


def test_distill_respects_limit() -> None:
    raw = "A" * 200
    short = distill_title(raw, limit=40)
    assert len(short) <= 40
    assert short.endswith("…")


def test_briefing_title_uses_120_cap() -> None:
    raw = "Task t_deadbeef: " + ("Slice gelandet " * 20) + "— completed"
    out = briefing_title(raw)
    assert "t_deadbeef" not in out
    assert "completed" not in out
    assert len(out) <= BRIEFING_TITLE_LIMIT


def test_empty_falls_back_to_ereignis() -> None:
    assert distill_title("") == "Ereignis"
    assert distill_title(None) == "Ereignis"
