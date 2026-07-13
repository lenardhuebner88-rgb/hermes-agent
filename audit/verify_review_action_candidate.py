#!/usr/bin/env python3
"""Prove review action honesty with a short-lived audit-scratch fixture."""
from __future__ import annotations

import json
import os
import time
from pathlib import Path
from urllib.parse import parse_qs, urlparse

from playwright.sync_api import Route, sync_playwright


BASE = os.environ.get("KANBAN_CANDIDATE_BASE", "http://127.0.0.1:9121")
OUT = Path("audit/iteration-4-state")


def create_review_fixture() -> tuple[str, object]:
    os.environ["HERMES_KANBAN_BOARD"] = "audit-scratch"
    from hermes_cli import kanban_db as kb

    conn = kb.connect()
    task_id = kb.create_task(
        conn,
        title="AUDIT REVIEW ACTION TRUTH",
        body="Prove the review state-machine action contract before the dispatcher can claim it.",
        acceptance_criteria="- AC-1: Review transitions are backend-owned",
        assignee="coder",
        created_by="codex-audit",
        idempotency_key=f"codex-audit-review-action-{time.time_ns()}",
    )
    claimed = kb.claim_task(conn, task_id, claimer="codex-audit")
    run = kb.latest_run(conn, task_id)
    if not claimed or not run:
        raise RuntimeError("audit review fixture could not be claimed")
    completed = kb.complete_task(
        conn,
        task_id,
        summary="Candidate implementation submitted for review",
        metadata={"tests_run": ["state-machine fixture"]},
        expected_run_id=run.id,
        review_gate=True,
    )
    task = kb.get_task(conn, task_id)
    if not completed or not task or task.status != "review":
        raise RuntimeError(f"audit fixture did not reach review: {task.status if task else None}")
    return task_id, conn


def main() -> int:
    OUT.mkdir(parents=True, exist_ok=True)
    console_errors: list[str] = []
    fixture_id: str | None = None
    fixture_conn = None
    captured_board: dict | None = None
    captured_detail: dict | None = None
    api_truth: dict = {}
    try:
        with sync_playwright() as playwright:
            browser = playwright.chromium.launch(headless=True)
            context = browser.new_context(viewport={"width": 1440, "height": 900})
            page = context.new_page()
            page.on("console", lambda msg: console_errors.append(msg.text) if msg.type == "error" else None)

            def captured_payloads(route: Route) -> None:
                parsed = urlparse(route.request.url)
                board = parse_qs(parsed.query).get("board", [None])[0]
                if route.request.method == "GET" and board is None:
                    if parsed.path.endswith("/api/plugins/kanban/board") and captured_board is not None:
                        route.fulfill(status=200, content_type="application/json", body=json.dumps(captured_board))
                        return
                    if fixture_id and parsed.path.endswith(f"/api/plugins/kanban/tasks/{fixture_id}") and captured_detail is not None:
                        route.fulfill(status=200, content_type="application/json", body=json.dumps(captured_detail))
                        return
                route.continue_()

            page.route("**/api/plugins/kanban/**", captured_payloads)
            page.goto(f"{BASE}/control/fleet", wait_until="domcontentloaded", timeout=30_000)
            page.get_by_role("button", name="Subtab Board", exact=True).wait_for(timeout=30_000)

            fixture_id, fixture_conn = create_review_fixture()
            api_truth = page.evaluate(
                """async ({taskId}) => {
                  const headers = {'X-Hermes-Session-Token': window.__HERMES_SESSION_TOKEN__};
                  const getJson = async (url) => {
                    const response = await fetch(url, {headers});
                    return {status: response.status, body: await response.json()};
                  };
                  const attempt = async (status) => {
                    const response = await fetch(`/api/plugins/kanban/tasks/${taskId}?board=audit-scratch`, {
                      method: 'PATCH', headers: {...headers, 'Content-Type': 'application/json'}, body: JSON.stringify({status})
                    });
                    return {status: response.status, body: await response.json()};
                  };
                  return {
                    detail: await getJson(`/api/plugins/kanban/tasks/${taskId}?board=audit-scratch`),
                    board: await getJson('/api/plugins/kanban/board?board=audit-scratch'),
                    done: await attempt('done'),
                    blocked: await attempt('blocked'),
                  };
                }""",
                {"taskId": fixture_id},
            )
            captured_detail = api_truth["detail"]["body"]
            captured_board = api_truth["board"]["body"]

            # Keep the live scratch board clean and prevent the dispatcher from
            # claiming the verifier fixture. The DOM below uses the captured
            # authenticated API payload from the review state.
            from hermes_cli import kanban_db as kb
            kb.archive_task(fixture_conn, fixture_id)

            page.reload(wait_until="domcontentloaded", timeout=30_000)
            page.get_by_role("button", name="Subtab Board", exact=True).click()
            title = str(captured_detail["task"]["title"])
            page.get_by_label("Tasks durchsuchen").fill(title)
            page.get_by_text(title, exact=True).click()
            page.get_by_text("review", exact=True).wait_for(timeout=20_000)

            ship_count = page.get_by_role("button", name="Ausliefern", exact=True).count()
            rework_count = page.get_by_role("button", name="Nacharbeit", exact=True).count()
            cancel_count = page.get_by_role("button", name="Abbrechen", exact=True).count()
            screenshot = OUT / "review-action-truth-1440x900.png"
            page.screenshot(path=str(screenshot), full_page=True)
            page.unroute_all(behavior="wait")
            context.close()
            browser.close()
    finally:
        if fixture_id and fixture_conn is not None:
            from hermes_cli import kanban_db as kb
            task = kb.get_task(fixture_conn, fixture_id)
            if task and task.status != "archived":
                kb.archive_task(fixture_conn, fixture_id)
            fixture_conn.close()

    expected_console_errors = [item for item in console_errors if "409 (Conflict)" in item]
    unexpected_console_errors = [item for item in console_errors if item not in expected_console_errors]
    result = {
        "base": BASE,
        "board": "audit-scratch",
        "task_id": fixture_id,
        "captured_task_status": captured_detail["task"]["status"] if captured_detail else None,
        "patch_done": api_truth.get("done"),
        "patch_blocked": api_truth.get("blocked"),
        "dom_ship_buttons": ship_count,
        "dom_rework_buttons": rework_count,
        "dom_valid_cancel_buttons": cancel_count,
        "fixture_archived_after_capture": True,
        "expected_409_console_errors": len(expected_console_errors),
        "unexpected_console_errors": unexpected_console_errors,
        "screenshot": str(screenshot),
    }
    (OUT / "review-action-summary.json").write_text(
        json.dumps(result, indent=2, sort_keys=True) + "\n", encoding="utf-8"
    )
    print(json.dumps(result, indent=2, sort_keys=True))
    return 0 if (
        result["captured_task_status"] == "review"
        and result["patch_done"]["status"] == 409
        and result["patch_blocked"]["status"] == 409
        and ship_count == 0
        and rework_count == 0
        and cancel_count == 1
        and len(expected_console_errors) == 2
        and not unexpected_console_errors
    ) else 1


if __name__ == "__main__":
    raise SystemExit(main())
