#!/usr/bin/env python3
"""Compare live DB/API/DOM run-state truth without mutating the default board."""
from __future__ import annotations

import json
import os
import sqlite3
from pathlib import Path

from playwright.sync_api import sync_playwright


BASE = os.environ.get("KANBAN_CANDIDATE_BASE", "http://127.0.0.1:9122")
OUT = Path("audit/iteration-4-state")
LEGACY_UI_RUN_STATES = {
    "running", "done", "blocked", "crashed", "timed_out", "failed", "released"
}


def db_truth() -> tuple[dict, dict[str, int], dict[str, int]]:
    from hermes_cli import kanban_db as kb

    db_path = kb.kanban_db_path(board="default")
    conn = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
    conn.row_factory = sqlite3.Row
    try:
        status_counts = {
            str(row["status"]): int(row["count"])
            for row in conn.execute(
                "SELECT status, COUNT(*) AS count FROM task_runs GROUP BY status ORDER BY status"
            ).fetchall()
        }
        outcome_counts = {
            str(row["outcome"]): int(row["count"])
            for row in conn.execute(
                "SELECT outcome, COUNT(*) AS count FROM task_runs "
                "WHERE outcome IS NOT NULL GROUP BY outcome ORDER BY outcome"
            ).fetchall()
        }
        placeholders = ",".join("?" for _ in LEGACY_UI_RUN_STATES)
        row = conn.execute(
            f"""
            SELECT t.id, t.title, t.status AS task_status,
                   r.id AS run_id, r.status AS run_status, r.outcome AS run_outcome
              FROM tasks t
              JOIN task_runs r ON r.id = (
                  SELECT id FROM task_runs
                   WHERE task_id = t.id
                   ORDER BY started_at DESC, id DESC LIMIT 1
              )
             WHERE t.status = 'done'
               AND r.status NOT IN ({placeholders})
             ORDER BY r.id DESC
             LIMIT 1
            """,
            tuple(sorted(LEGACY_UI_RUN_STATES)),
        ).fetchone()
        if row is None:
            raise RuntimeError("default board has no done task with a non-legacy run state")
        return dict(row), status_counts, outcome_counts
    finally:
        conn.close()


def main() -> int:
    OUT.mkdir(parents=True, exist_ok=True)
    selected, status_counts, outcome_counts = db_truth()
    console_errors: list[str] = []
    with sync_playwright() as playwright:
        browser = playwright.chromium.launch(headless=True)
        context = browser.new_context(viewport={"width": 1440, "height": 900})
        page = context.new_page()
        page.on(
            "console",
            lambda msg: console_errors.append(msg.text) if msg.type == "error" else None,
        )
        page.goto(f"{BASE}/control/fleet", wait_until="domcontentloaded", timeout=30_000)
        page.get_by_role("button", name="Subtab Board", exact=True).wait_for(timeout=30_000)

        api_truth = page.evaluate(
            """async ({taskId}) => {
              const headers = {'X-Hermes-Session-Token': window.__HERMES_SESSION_TOKEN__};
              const getJson = async (url) => {
                const response = await fetch(url, {headers});
                return {status: response.status, body: await response.json()};
              };
              return {
                detail: await getJson(`/api/plugins/kanban/tasks/${taskId}?board=default`),
                graph: await getJson(`/api/plugins/kanban/tasks/${taskId}/chain-graph?board=default`),
              };
            }""",
            {"taskId": selected["id"]},
        )

        page.get_by_role("button", name="Subtab Board", exact=True).click()
        page.get_by_label("Tasks durchsuchen").fill(str(selected["id"]))
        page.locator(".fleet-boardtab-title", has_text=str(selected["title"])).click()
        page.get_by_text("Laufstatus", exact=True).wait_for(timeout=20_000)
        dom_run_state = page.get_by_text("Laufstatus", exact=True).locator("..").inner_text()
        screenshot = OUT / "run-state-truth-1440x900.png"
        page.screenshot(path=str(screenshot), full_page=True)
        context.close()
        browser.close()

    graph_nodes = api_truth["graph"]["body"].get("nodes", [])
    selected_graph_node = next(
        (node for node in graph_nodes if node.get("id") == selected["id"]), None
    )
    api_graph_status = (
        selected_graph_node.get("latest_run", {}).get("status")
        if selected_graph_node and selected_graph_node.get("latest_run")
        else None
    )
    detail_runs = api_truth["detail"]["body"].get("runs", [])
    # Detail history is intentionally chronological (oldest first); the drawer
    # must select the last row as the latest attempt.
    api_detail_statuses = [run.get("status") for run in detail_runs]
    api_detail_status = detail_runs[-1].get("status") if detail_runs else None
    expected_dom = f"({selected['run_status']})"
    result = {
        "base": BASE,
        "board": "default",
        "db_run_status_counts": status_counts,
        "db_run_outcome_counts": outcome_counts,
        "legacy_ui_run_statuses": sorted(LEGACY_UI_RUN_STATES),
        "selected": selected,
        "api_detail_http": api_truth["detail"]["status"],
        "api_graph_http": api_truth["graph"]["status"],
        "api_detail_run_status": api_detail_status,
        "api_detail_run_statuses_oldest_first": api_detail_statuses,
        "api_graph_run_status": api_graph_status,
        "dom_run_state": dom_run_state,
        "unexpected_console_errors": console_errors,
        "screenshot": str(screenshot),
    }
    (OUT / "run-state-truth-summary.json").write_text(
        json.dumps(result, indent=2, sort_keys=True) + "\n", encoding="utf-8"
    )
    print(json.dumps(result, indent=2, sort_keys=True))
    return 0 if (
        len(status_counts) > len(LEGACY_UI_RUN_STATES)
        and selected["run_status"] not in LEGACY_UI_RUN_STATES
        and api_truth["detail"]["status"] == 200
        and api_truth["graph"]["status"] == 200
        and api_detail_status == selected["run_status"]
        and api_graph_status == selected["run_status"]
        and expected_dom in dom_run_state
        and not console_errors
    ) else 1


if __name__ == "__main__":
    raise SystemExit(main())
