from __future__ import annotations

import argparse
import json
import re
from pathlib import Path

import pytest

from hermes_cli import kanban as kc
from hermes_cli import kanban_db as kb
from hermes_cli import kanban_discord_report as kdr


@pytest.fixture
def kanban_home(tmp_path, monkeypatch):
    home = tmp_path / ".hermes"
    home.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(home))
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    kb._INITIALIZED_PATHS.clear()
    kb.init_db()
    return home


def _run_cli(argv: list[str]) -> int:
    parser = argparse.ArgumentParser(prog="hermes")
    sub = parser.add_subparsers(dest="command")
    kc.build_parser(sub)
    args = parser.parse_args(["kanban", *argv])
    return kc.kanban_command(args)


def _extract_structured_block(markdown: str) -> dict:
    match = re.search(r"```json kanban-report-v1\n(.*?)\n```", markdown, flags=re.S)
    assert match, markdown
    return json.loads(match.group(1))


def test_empty_board_renders_valid_contract_in_canonical_order(kanban_home):
    with kb.connect() as conn:
        report = kdr.build_discord_report(
            conn,
            board="default",
            generated_at="2026-05-27T22:20:00Z",
            report_title="Empty report",
            scope_description="Empty test board",
        )

    markdown = kdr.render_discord_report_markdown(report)

    expected_order = [
        "**Empty report**",
        "**Summary**",
        "**Blocked**",
        "**Active / review**",
        "**Completed**",
        "**Next actions**",
        "**Structured Report**",
        "```json kanban-report-v1",
    ]
    positions = [markdown.index(token) for token in expected_order]
    assert positions == sorted(positions)
    assert "**Warnings / errors**" not in markdown

    payload = _extract_structured_block(markdown)
    assert payload["report_type"] == "kanban_result_report"
    assert payload["contract_version"] == 1
    assert payload["generated_at"] == "2026-05-27T22:20:00Z"
    assert payload["status"] == "empty"
    assert payload["summary"] == "No tasks matched the report scope."
    assert payload["tasks"] == []
    assert payload["relationships"] == []
    assert payload["errors"] == []
    assert payload["next_actions"] == []
    assert payload["counts"]["tasks_total"] == 0
    assert "- None." in markdown


def test_report_maps_tasks_relationships_and_human_sections_to_json(kanban_home):
    with kb.connect() as conn:
        done = kb.create_task(conn, title="ship renderer", assignee="coder")
        kb.complete_task(
            conn,
            done,
            summary="Renderer done with tests.",
            metadata={"artifacts": ["/tmp/receipt.md"]},
        )
        active = kb.create_task(conn, title="review output contract", assignee="reviewer", parents=[done])
        blocked = kb.create_task(conn, title="wait for operator approval", assignee=None)
        kb.block_task(conn, blocked, reason="Needs operator Go before Discord delivery.")

        report = kdr.build_discord_report(
            conn,
            board="default",
            generated_at="2026-05-27T22:21:00Z",
            report_title="Sprint report",
            scope_description="Sprint implementation",
        )

    payload = report["structured"]
    by_id = {task["id"]: task for task in payload["tasks"]}
    assert by_id[done]["summary"] == "Renderer done with tests."
    assert by_id[done]["child_ids"] == [active]
    assert by_id[active]["parent_ids"] == [done]
    assert by_id[blocked]["assignee"] == {"slug": "unknown", "kind": "unknown"}
    assert by_id[blocked]["blocked_reason"] == "Needs operator Go before Discord delivery."
    assert payload["relationships"] == [
        {"parent_id": done, "child_id": active, "type": "blocks_until_done"}
    ]
    assert payload["counts"]["by_status"]["done"] == 1
    assert payload["counts"]["by_status"]["ready"] == 1
    assert payload["counts"]["by_status"]["blocked"] == 1
    assert payload["counts"]["active_total"] == 1

    markdown = kdr.render_discord_report_markdown(report)
    assert f"`{blocked}` **wait for operator approval** — @unknown · `blocked`" in markdown
    assert "Reason: Needs operator Go before Discord delivery. · Needs: review block reason" in markdown
    assert f"`{active}` **review output contract** — @reviewer · `ready`" in markdown
    assert f"Depends on: `{done}`" in markdown
    assert f"`{done}` **ship renderer** — @coder" in markdown
    assert "Evidence: /tmp/receipt.md" in markdown
    assert _extract_structured_block(markdown)["tasks"] == payload["tasks"]


def test_split_discord_report_keeps_chunks_under_soft_cap_and_json_intact(kanban_home):
    with kb.connect() as conn:
        for idx in range(8):
            task_id = kb.create_task(
                conn,
                title=("long task title " + str(idx) + " ") * 8,
                assignee="coder",
            )
            kb.complete_task(conn, task_id, summary=("completed summary " + str(idx) + " ") * 8)
        report = kdr.build_discord_report(
            conn,
            board="default",
            generated_at="2026-05-27T22:22:00Z",
            report_title="Chunked report",
            scope_description="Chunking test",
        )

    chunks = kdr.split_discord_report(report, soft_cap=6000, artifact_path="/tmp/full-report.md")

    assert len(chunks) > 1
    assert all(len(chunk) <= 6000 for chunk in chunks)
    assert "Structured block in final part" in chunks[0]
    assert all("Part " in chunk and "Chunked report" in chunk for chunk in chunks[1:])
    json_chunks = [chunk for chunk in chunks if "```json kanban-report-v1" in chunk]
    assert len(json_chunks) == 1
    assert _extract_structured_block(json_chunks[0])["tasks"] == report["structured"]["tasks"]
    assert chunks[-1].rstrip().endswith("_Full report: /tmp/full-report.md_")


def test_split_discord_report_externalizes_oversized_json_without_invalid_truncation(kanban_home):
    with kb.connect() as conn:
        for idx in range(80):
            kb.create_task(conn, title=f"task {idx} " + ("x" * 80), assignee="coder")
        report = kdr.build_discord_report(
            conn,
            board="default",
            generated_at="2026-05-27T22:23:00Z",
            report_title="Externalized report",
            scope_description="Huge JSON test",
        )

    chunks = kdr.split_discord_report(report, soft_cap=1800, artifact_path="/tmp/report.json")

    assert all(len(chunk) <= 1800 for chunk in chunks)
    json_chunks = [chunk for chunk in chunks if "```json kanban-report-v1" in chunk]
    assert len(json_chunks) == 1
    pointer_payload = _extract_structured_block(json_chunks[0])
    assert pointer_payload["status"] == "partial"
    assert pointer_payload["warnings"][0]["code"] == "structured_payload_externalized"
    assert pointer_payload["artifacts"][0]["ref"] == "/tmp/report.json"
    assert pointer_payload["tasks"] == []


def test_cli_report_discord_json_is_discoverable_and_read_only(kanban_home, capsys):
    parser = argparse.ArgumentParser(prog="hermes")
    sub = parser.add_subparsers(dest="command")
    kanban_parser = kc.build_parser(sub)
    assert "report-discord" in kanban_parser.format_help()

    with kb.connect() as conn:
        task_id = kb.create_task(conn, title="cli report", assignee="coder")
        before_events = conn.execute("SELECT COUNT(*) FROM task_events").fetchone()[0]
        before_runs = conn.execute("SELECT COUNT(*) FROM task_runs").fetchone()[0]

    code = _run_cli(["report-discord", "--json", "--generated-at", "2026-05-27T22:24:00Z"])

    captured = capsys.readouterr()
    assert code == 0
    payload = json.loads(captured.out)
    assert payload["report_type"] == "kanban_result_report"
    assert payload["contract_version"] == 1
    assert payload["tasks"][0]["id"] == task_id
    with kb.connect() as conn:
        after_events = conn.execute("SELECT COUNT(*) FROM task_events").fetchone()[0]
        after_runs = conn.execute("SELECT COUNT(*) FROM task_runs").fetchone()[0]
    assert after_events == before_events
    assert after_runs == before_runs
