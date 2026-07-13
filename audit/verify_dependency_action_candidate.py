#!/usr/bin/env python3
"""Prove unsatisfied-parent action honesty on audit-scratch."""
from __future__ import annotations

import json
import os
import time
from pathlib import Path
from urllib.parse import parse_qs, urlparse

from playwright.sync_api import Route, sync_playwright


BASE = os.environ.get("KANBAN_CANDIDATE_BASE", "http://127.0.0.1:9121")
OUT = Path("audit/iteration-4-state")


def create_fixtures() -> tuple[str, str, object]:
    os.environ["HERMES_KANBAN_BOARD"] = "audit-scratch"
    from hermes_cli import kanban_db as kb

    conn = kb.connect()
    suffix = time.time_ns()
    parent_id = kb.create_task(
        conn,
        title="AUDIT BLOCKING PARENT",
        body="Stable blocked parent for dependency truth.",
        created_by="codex-audit",
        initial_status="blocked",
        idempotency_key=f"codex-audit-blocking-parent-{suffix}",
    )
    child_id = kb.create_task(
        conn,
        title="AUDIT DEPENDENCY ACTION CHILD",
        body="Child must not offer Starten until its parent is done.",
        created_by="codex-audit",
        parents=[parent_id],
        idempotency_key=f"codex-audit-dependent-child-{suffix}",
    )
    child = kb.get_task(conn, child_id)
    if not child or child.status != "todo":
        raise RuntimeError(f"dependent child did not remain todo: {child.status if child else None}")
    return parent_id, child_id, conn


def main() -> int:
    OUT.mkdir(parents=True, exist_ok=True)
    parent_id: str | None = None
    child_id: str | None = None
    fixture_conn = None
    captured_board: dict | None = None
    captured_detail: dict | None = None
    console_errors: list[str] = []
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
                    if child_id and parsed.path.endswith(f"/api/plugins/kanban/tasks/{child_id}") and captured_detail is not None:
                        route.fulfill(status=200, content_type="application/json", body=json.dumps(captured_detail))
                        return
                route.continue_()

            page.route("**/api/plugins/kanban/**", captured_payloads)
            page.goto(f"{BASE}/control/fleet", wait_until="domcontentloaded", timeout=30_000)
            page.get_by_role("button", name="Subtab Board", exact=True).wait_for(timeout=30_000)

            parent_id, child_id, fixture_conn = create_fixtures()
            api_truth = page.evaluate(
                """async ({childId}) => {
                  const headers = {'X-Hermes-Session-Token': window.__HERMES_SESSION_TOKEN__};
                  const getJson = async (url) => {
                    const response = await fetch(url, {headers});
                    return {status: response.status, body: await response.json()};
                  };
                  const promote = await fetch(`/api/plugins/kanban/tasks/${childId}?board=audit-scratch`, {
                    method: 'PATCH', headers: {...headers, 'Content-Type': 'application/json'}, body: JSON.stringify({status: 'ready'})
                  });
                  return {
                    detail: await getJson(`/api/plugins/kanban/tasks/${childId}?board=audit-scratch`),
                    board: await getJson('/api/plugins/kanban/board?board=audit-scratch'),
                    promote: {status: promote.status, body: await promote.json()},
                  };
                }""",
                {"childId": child_id},
            )
            captured_detail = api_truth["detail"]["body"]
            captured_board = api_truth["board"]["body"]

            from hermes_cli import kanban_db as kb
            kb.archive_task(fixture_conn, child_id)
            kb.archive_task(fixture_conn, parent_id)

            page.reload(wait_until="domcontentloaded", timeout=30_000)
            page.get_by_role("button", name="Subtab Board", exact=True).click()
            title = str(captured_detail["task"]["title"])
            page.get_by_label("Tasks durchsuchen").fill(title)
            page.locator(".fleet-boardtab-title", has_text=title).click()
            page.get_by_text("Starten nicht verfügbar", exact=False).wait_for(timeout=20_000)

            start_count = page.get_by_role("button", name="Starten", exact=True).count()
            cancel_count = page.get_by_role("button", name="Abbrechen", exact=True).count()
            explanation = page.get_by_text("Starten nicht verfügbar", exact=False).inner_text()
            screenshot = OUT / "dependency-action-truth-1440x900.png"
            page.screenshot(path=str(screenshot), full_page=True)
            page.unroute_all(behavior="wait")
            context.close()
            browser.close()
    finally:
        if fixture_conn is not None:
            from hermes_cli import kanban_db as kb
            for task_id in (child_id, parent_id):
                if task_id:
                    task = kb.get_task(fixture_conn, task_id)
                    if task and task.status != "archived":
                        kb.archive_task(fixture_conn, task_id)
            fixture_conn.close()

    expected_console_errors = [item for item in console_errors if "409 (Conflict)" in item]
    unexpected_console_errors = [item for item in console_errors if item not in expected_console_errors]
    parent_states = captured_detail["links"]["parent_states"] if captured_detail else []
    result = {
        "base": BASE,
        "board": "audit-scratch",
        "parent_id": parent_id,
        "child_id": child_id,
        "captured_child_status": captured_detail["task"]["status"] if captured_detail else None,
        "captured_parent_states": parent_states,
        "patch_ready": api_truth["promote"],
        "dom_start_buttons": start_count,
        "dom_valid_cancel_buttons": cancel_count,
        "dom_explanation": explanation,
        "fixtures_archived_after_capture": True,
        "expected_409_console_errors": len(expected_console_errors),
        "unexpected_console_errors": unexpected_console_errors,
        "screenshot": str(screenshot),
    }
    (OUT / "dependency-action-summary.json").write_text(
        json.dumps(result, indent=2, sort_keys=True) + "\n", encoding="utf-8"
    )
    print(json.dumps(result, indent=2, sort_keys=True))
    return 0 if (
        result["captured_child_status"] == "todo"
        and len(parent_states) == 1
        and parent_states[0]["status"] == "blocked"
        and result["patch_ready"]["status"] == 409
        and start_count == 0
        and cancel_count == 1
        and "AUDIT BLOCKING PARENT (blocked)" in explanation
        and len(expected_console_errors) == 1
        and not unexpected_console_errors
    ) else 1


if __name__ == "__main__":
    raise SystemExit(main())
