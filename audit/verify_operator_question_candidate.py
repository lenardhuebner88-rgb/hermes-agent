#!/usr/bin/env python3
"""Prove verdict-aware operator-question truth on audit-scratch."""
from __future__ import annotations

import json
import os
import re
import time
from pathlib import Path
from urllib.parse import parse_qs, urlparse

from playwright.sync_api import Route, sync_playwright


BASE = os.environ.get("KANBAN_CANDIDATE_BASE", "http://127.0.0.1:9122")
OUT = Path("audit/iteration-4-state")


def create_fixtures() -> tuple[str, str, object]:
    os.environ["HERMES_KANBAN_BOARD"] = "audit-scratch"
    from hermes_cli import kanban_db as kb

    conn = kb.connect()
    suffix = time.time_ns()

    verifier_id = kb.create_task(
        conn,
        title="AUDIT VERIFIER PROSE IS NOT OPERATOR INPUT",
        body="AC v1",
        assignee="coder",
        created_by="codex-audit",
        idempotency_key=f"codex-audit-verifier-question-{suffix}",
    )
    if not kb.claim_task(conn, verifier_id, claimer="codex-audit-coder"):
        raise RuntimeError("could not claim verifier fixture as coder")
    coder_run = kb.latest_run(conn, verifier_id)
    if not coder_run or not kb.complete_task(
        conn,
        verifier_id,
        summary="Candidate submitted for review",
        expected_run_id=coder_run.id,
        review_gate=True,
    ):
        raise RuntimeError("could not submit verifier fixture for review")
    if not kb.claim_review_task(
        conn,
        verifier_id,
        claimer="codex-audit-verifier",
        reviewer_profile="verifier",
    ):
        raise RuntimeError("could not claim verifier fixture for review")
    if not kb.block_task(
        conn,
        verifier_id,
        reason="Verifier asks: why is this assertion missing?",
    ):
        raise RuntimeError("could not REQUEST_CHANGES-block verifier fixture")

    operator_id = kb.create_task(
        conn,
        title="AUDIT REAL OPERATOR QUESTION",
        body="Wait for the operator's credential choice.",
        assignee="coder",
        created_by="codex-audit",
        idempotency_key=f"codex-audit-real-operator-question-{suffix}",
    )
    if not kb.claim_task(conn, operator_id, claimer="codex-audit-coder"):
        raise RuntimeError("could not claim operator fixture")
    if not kb.hold_task(conn, operator_id, reason="operator hold: which credential should be used?"):
        raise RuntimeError("could not hold operator fixture")
    return verifier_id, operator_id, conn


def main() -> int:
    OUT.mkdir(parents=True, exist_ok=True)
    verifier_id: str | None = None
    operator_id: str | None = None
    fixture_conn = None
    captured_board: dict | None = None
    captured_details: dict[str, dict] = {}
    console_errors: list[str] = []
    try:
        with sync_playwright() as playwright:
            browser = playwright.chromium.launch(headless=True)
            context = browser.new_context(viewport={"width": 1440, "height": 900})
            page = context.new_page()
            page.on(
                "console",
                lambda msg: console_errors.append(msg.text) if msg.type == "error" else None,
            )

            def captured_payloads(route: Route) -> None:
                parsed = urlparse(route.request.url)
                board = parse_qs(parsed.query).get("board", [None])[0]
                if route.request.method == "GET" and board is None:
                    if parsed.path.endswith("/api/plugins/kanban/board") and captured_board is not None:
                        route.fulfill(
                            status=200,
                            content_type="application/json",
                            body=json.dumps(captured_board),
                        )
                        return
                    for task_id, detail in captured_details.items():
                        if parsed.path.endswith(f"/api/plugins/kanban/tasks/{task_id}"):
                            route.fulfill(
                                status=200,
                                content_type="application/json",
                                body=json.dumps(detail),
                            )
                            return
                route.continue_()

            page.route("**/api/plugins/kanban/**", captured_payloads)
            page.goto(f"{BASE}/control/fleet", wait_until="domcontentloaded", timeout=30_000)
            page.get_by_role("button", name=re.compile(r"^Subtab Risiko")).wait_for(timeout=30_000)

            verifier_id, operator_id, fixture_conn = create_fixtures()
            api_truth = page.evaluate(
                """async ({verifierId, operatorId}) => {
                  const headers = {'X-Hermes-Session-Token': window.__HERMES_SESSION_TOKEN__};
                  const getJson = async (url) => {
                    const response = await fetch(url, {headers});
                    return {status: response.status, body: await response.json()};
                  };
                  return {
                    board: await getJson('/api/plugins/kanban/board?board=audit-scratch'),
                    verifier: await getJson(`/api/plugins/kanban/tasks/${verifierId}?board=audit-scratch`),
                    operator: await getJson(`/api/plugins/kanban/tasks/${operatorId}?board=audit-scratch`),
                  };
                }""",
                {"verifierId": verifier_id, "operatorId": operator_id},
            )
            captured_board = api_truth["board"]["body"]
            captured_details = {
                verifier_id: api_truth["verifier"]["body"],
                operator_id: api_truth["operator"]["body"],
            }

            from hermes_cli import kanban_db as kb

            kb.archive_task(fixture_conn, verifier_id)
            kb.archive_task(fixture_conn, operator_id)

            page.reload(wait_until="domcontentloaded", timeout=30_000)
            page.get_by_role("button", name=re.compile(r"^Subtab Risiko")).click()
            page.get_by_text("AUDIT REAL OPERATOR QUESTION", exact=True).wait_for(timeout=20_000)

            operator_dom_count = page.get_by_text("AUDIT REAL OPERATOR QUESTION", exact=True).count()
            verifier_dom_count = page.get_by_text(
                "AUDIT VERIFIER PROSE IS NOT OPERATOR INPUT", exact=True
            ).count()
            answer_dom_count = page.get_by_text("Operator-Frage beantworten", exact=True).count()
            screenshot = OUT / "operator-question-truth-1440x900.png"
            page.screenshot(path=str(screenshot), full_page=True)
            page.unroute_all(behavior="wait")
            context.close()
            browser.close()
    finally:
        if fixture_conn is not None:
            from hermes_cli import kanban_db as kb

            for task_id in (verifier_id, operator_id):
                if task_id:
                    task = kb.get_task(fixture_conn, task_id)
                    if task and task.status != "archived":
                        kb.archive_task(fixture_conn, task_id)
            fixture_conn.close()

    blocked_cards = {
        task["id"]: task
        for column in (captured_board or {}).get("columns", [])
        if column.get("name") == "blocked"
        for task in column.get("tasks", [])
    }
    result = {
        "base": BASE,
        "board": "audit-scratch",
        "verifier_task_id": verifier_id,
        "operator_task_id": operator_id,
        "board_verifier_operator_question": blocked_cards.get(verifier_id, {}).get("operator_question"),
        "board_operator_operator_question": blocked_cards.get(operator_id, {}).get("operator_question"),
        "detail_verifier_operator_question": captured_details.get(verifier_id or "", {}).get("task", {}).get("operator_question"),
        "detail_operator_operator_question": captured_details.get(operator_id or "", {}).get("task", {}).get("operator_question"),
        "dom_verifier_cards": verifier_dom_count,
        "dom_operator_cards": operator_dom_count,
        "dom_answer_forms": answer_dom_count,
        "fixtures_archived_after_capture": True,
        "unexpected_console_errors": console_errors,
        "screenshot": str(screenshot),
    }
    (OUT / "operator-question-summary.json").write_text(
        json.dumps(result, indent=2, sort_keys=True) + "\n", encoding="utf-8"
    )
    print(json.dumps(result, indent=2, sort_keys=True))
    return 0 if (
        result["board_verifier_operator_question"] is False
        and result["board_operator_operator_question"] is True
        and result["detail_verifier_operator_question"] is False
        and result["detail_operator_operator_question"] is True
        and verifier_dom_count == 0
        and operator_dom_count == 1
        and answer_dom_count == 1
        and not console_errors
    ) else 1


if __name__ == "__main__":
    raise SystemExit(main())
