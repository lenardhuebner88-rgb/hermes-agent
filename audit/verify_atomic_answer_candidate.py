#!/usr/bin/env python3
"""Prove atomic answer/unblock semantics and a real two-tab loss race."""
from __future__ import annotations

import json
import os
import re
import time
from pathlib import Path

from playwright.sync_api import sync_playwright


BASE = os.environ.get("KANBAN_CANDIDATE_BASE", "http://127.0.0.1:9123")
OUT = Path("audit/iteration-4-state")


def create_fixtures() -> tuple[str, str, object]:
    os.environ["HERMES_KANBAN_BOARD"] = "audit-scratch"
    from hermes_cli import kanban_db as kb

    conn = kb.connect()
    suffix = time.time_ns()
    ids: list[str] = []
    for label in ("SUCCESS", "SECOND TAB RACE"):
        task_id = kb.create_task(
            conn,
            title=f"AUDIT ATOMIC ANSWER {label}",
            body="Wait for an explicit operator answer.",
            assignee=None,
            created_by="codex-audit",
            idempotency_key=f"codex-audit-atomic-answer-{label}-{suffix}",
        )
        if not kb.claim_task(conn, task_id, claimer="codex-audit"):
            raise RuntimeError(f"could not claim {label} fixture")
        if not kb.hold_task(conn, task_id, reason="operator hold: which credential?"):
            raise RuntimeError(f"could not hold {label} fixture")
        ids.append(task_id)
    return ids[0], ids[1], conn


def main() -> int:
    OUT.mkdir(parents=True, exist_ok=True)
    success_id: str | None = None
    race_id: str | None = None
    fixture_conn = None
    console_errors: list[str] = []
    try:
        success_id, race_id, fixture_conn = create_fixtures()
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
            first.get_by_role("button", name=re.compile(r"^Subtab Risiko")).click()

            success_card = first.locator(
                f'[aria-label="Operator-Halt: AUDIT ATOMIC ANSWER SUCCESS"]'
            )
            success_card.wait_for(timeout=20_000)
            success_card.get_by_label("Antwort eingeben …").fill("Use scoped credential A.")
            with first.expect_response(
                lambda response: response.url.endswith(f"/tasks/{success_id}/answer"),
                timeout=20_000,
            ) as success_response_info:
                success_card.get_by_role("button", name="Antworten", exact=True).click()
            success_response = success_response_info.value
            success_response_body = success_response.json()

            success_truth = second.evaluate(
                """async ({taskId}) => {
                  const headers = {'X-Hermes-Session-Token': window.__HERMES_SESSION_TOKEN__};
                  const response = await fetch(`/api/plugins/kanban/tasks/${taskId}`, {headers});
                  return {status: response.status, body: await response.json()};
                }""",
                {"taskId": success_id},
            ) if second.url != "about:blank" else None
            if success_truth is None:
                second.goto(f"{BASE}/control/fleet", wait_until="domcontentloaded", timeout=30_000)
                success_truth = second.evaluate(
                    """async ({taskId}) => {
                      const headers = {'X-Hermes-Session-Token': window.__HERMES_SESSION_TOKEN__};
                      const response = await fetch(`/api/plugins/kanban/tasks/${taskId}`, {headers});
                      return {status: response.status, body: await response.json()};
                    }""",
                    {"taskId": success_id},
                )

            race_card = first.locator(
                f'[aria-label="Operator-Halt: AUDIT ATOMIC ANSWER SECOND TAB RACE"]'
            )
            race_card.get_by_label("Antwort eingeben …").fill("This must not persist.")
            archived = second.evaluate(
                """async ({taskId}) => {
                  const headers = {
                    'X-Hermes-Session-Token': window.__HERMES_SESSION_TOKEN__,
                    'Content-Type': 'application/json'
                  };
                  const response = await fetch(`/api/plugins/kanban/tasks/${taskId}`, {
                    method: 'PATCH', headers, body: JSON.stringify({status: 'archived'})
                  });
                  return {status: response.status, body: await response.json()};
                }""",
                {"taskId": race_id},
            )
            with first.expect_response(
                lambda response: response.url.endswith(f"/tasks/{race_id}/answer"),
                timeout=20_000,
            ) as race_response_info:
                race_card.get_by_role("button", name="Antworten", exact=True).click()
            race_response = race_response_info.value
            race_response_body = race_response.json()
            race_card.get_by_role("alert").wait_for(timeout=10_000)
            race_error = race_card.get_by_role("alert").inner_text()

            screenshot = OUT / "atomic-answer-two-tab-1440x900.png"
            first.screenshot(path=str(screenshot), full_page=True)
            context.close()
            browser.close()

        from hermes_cli import kanban_db as kb

        success_task = kb.get_task(fixture_conn, success_id)
        race_task = kb.get_task(fixture_conn, race_id)
        success_comments = kb.list_comments(fixture_conn, success_id)
        race_comments = kb.list_comments(fixture_conn, race_id)
        result = {
            "base": BASE,
            "board": "audit-scratch",
            "success_task_id": success_id,
            "success_answer_http": success_response.status,
            "success_answer_body": success_response_body,
            "success_detail_http": success_truth["status"],
            "success_status": success_task.status if success_task else None,
            "success_comments": [
                {"author": comment.author, "body": comment.body}
                for comment in success_comments
            ],
            "race_task_id": race_id,
            "second_tab_archive_http": archived["status"],
            "race_answer_http": race_response.status,
            "race_answer_body": race_response_body,
            "race_status": race_task.status if race_task else None,
            "race_comments": [
                {"author": comment.author, "body": comment.body}
                for comment in race_comments
            ],
            "race_dom_error": race_error,
            "unexpected_console_errors": [
                item for item in console_errors if "409 (Conflict)" not in item
            ],
            "expected_409_console_errors": len(
                [item for item in console_errors if "409 (Conflict)" in item]
            ),
            "screenshot": str(screenshot),
        }
        (OUT / "atomic-answer-summary.json").write_text(
            json.dumps(result, indent=2, sort_keys=True) + "\n", encoding="utf-8"
        )
        print(json.dumps(result, indent=2, sort_keys=True))
        return 0 if (
            success_response.status == 200
            and success_response_body.get("status") == "ready"
            and success_task is not None
            and success_task.status == "ready"
            and [(c.author, c.body) for c in success_comments]
            == [("operator", "Use scoped credential A.")]
            and archived["status"] == 200
            and race_response.status == 409
            and race_task is not None
            and race_task.status == "archived"
            and race_comments == []
            and "keine aktuelle Operator-Frage" in race_error
            and not result["unexpected_console_errors"]
        ) else 1
    finally:
        if fixture_conn is not None:
            from hermes_cli import kanban_db as kb

            for task_id in (success_id, race_id):
                if task_id:
                    task = kb.get_task(fixture_conn, task_id)
                    if task and task.status != "archived":
                        kb.archive_task(fixture_conn, task_id)
            fixture_conn.close()


if __name__ == "__main__":
    raise SystemExit(main())
