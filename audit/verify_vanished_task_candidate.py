#!/usr/bin/env python3
"""Prove a second-tab hard delete cannot remain plausible in an open drawer."""
from __future__ import annotations

import json
import os
import time
from pathlib import Path

from playwright.sync_api import sync_playwright


BASE = os.environ.get("KANBAN_CANDIDATE_BASE", "http://127.0.0.1:9123")
OUT = Path("audit/iteration-4-state")


def create_fixture() -> str:
    os.environ["HERMES_KANBAN_BOARD"] = "audit-scratch"
    from hermes_cli import kanban_db as kb

    with kb.connect_closing() as conn:
        task_id = kb.create_task(
            conn,
            title="AUDIT VANISHED TASK RETAINED TRUTH",
            body="This last-good body must remain visibly stale after deletion.",
            assignee=None,
            created_by="codex-audit",
            idempotency_key=f"codex-audit-vanished-task-{time.time_ns()}",
        )
        if not kb.complete_task(conn, task_id, summary="last-good result"):
            raise RuntimeError("could not complete vanished-task fixture")
        return task_id


def main() -> int:
    OUT.mkdir(parents=True, exist_ok=True)
    task_id = create_fixture()
    console_errors: list[str] = []
    with sync_playwright() as playwright:
        browser = playwright.chromium.launch(headless=True)
        context = browser.new_context(viewport={"width": 1440, "height": 900})
        first = context.new_page()
        second = context.new_page()
        first.on(
            "console",
            lambda msg: console_errors.append(msg.text) if msg.type == "error" else None,
        )
        first.goto(f"{BASE}/control/fleet", wait_until="domcontentloaded", timeout=30_000)
        first.get_by_role("button", name="Subtab Board", exact=True).click()
        first.get_by_label("Tasks durchsuchen").fill(task_id)
        first.get_by_text("AUDIT VANISHED TASK RETAINED TRUTH", exact=True).click()
        first.get_by_text("This last-good body must remain visibly stale after deletion.", exact=True).wait_for(
            timeout=20_000
        )

        second.goto(f"{BASE}/control/fleet", wait_until="domcontentloaded", timeout=30_000)
        deleted = second.evaluate(
            """async ({taskId}) => {
              const headers = {'X-Hermes-Session-Token': window.__HERMES_SESSION_TOKEN__};
              const response = await fetch(`/api/plugins/kanban/tasks/${taskId}`, {
                method: 'DELETE', headers
              });
              return {status: response.status, body: await response.json()};
            }""",
            {"taskId": task_id},
        )

        first.get_by_text("Task-Detail", exact=True).wait_for(timeout=20_000)
        stale_badge = first.get_by_text("Task-Detail", exact=True).locator("..").inner_text()
        retained_body_count = first.get_by_text(
            "This last-good body must remain visibly stale after deletion.", exact=True
        ).count()
        screenshot = OUT / "vanished-task-retained-truth-1440x900.png"
        first.screenshot(path=str(screenshot), full_page=True)
        missing = second.evaluate(
            """async ({taskId}) => {
              const headers = {'X-Hermes-Session-Token': window.__HERMES_SESSION_TOKEN__};
              const response = await fetch(`/api/plugins/kanban/tasks/${taskId}`, {headers});
              return {status: response.status, body: await response.json()};
            }""",
            {"taskId": task_id},
        )
        context.close()
        browser.close()

    expected_404s = [item for item in console_errors if "404 (Not Found)" in item]
    unexpected = [item for item in console_errors if item not in expected_404s]
    result = {
        "base": BASE,
        "board": "audit-scratch",
        "task_id": task_id,
        "second_tab_delete": deleted,
        "post_delete_detail": missing,
        "dom_source_disclosure": stale_badge,
        "dom_retained_body_count": retained_body_count,
        "expected_404_console_errors": len(expected_404s),
        "unexpected_console_errors": unexpected,
        "screenshot": str(screenshot),
    }
    (OUT / "vanished-task-summary.json").write_text(
        json.dumps(result, indent=2, sort_keys=True) + "\n", encoding="utf-8"
    )
    print(json.dumps(result, indent=2, sort_keys=True))
    return 0 if (
        deleted["status"] == 200
        and missing["status"] == 404
        and "Task-Detail" in stale_badge
        and "Daten von vor" in stale_badge
        and retained_body_count == 1
        and len(expected_404s) >= 1
        and not unexpected
    ) else 1


if __name__ == "__main__":
    raise SystemExit(main())
