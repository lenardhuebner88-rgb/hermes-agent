#!/usr/bin/env python3
"""Prove that a running task never offers the backend-impossible reassign action."""
from __future__ import annotations

import json
import os
import time
from pathlib import Path

from playwright.sync_api import sync_playwright


BASE = os.environ.get("KANBAN_CANDIDATE_BASE", "http://127.0.0.1:9123")
PHASE = os.environ.get("KANBAN_AUDIT_PHASE", "after").strip().lower()
OUT = Path("audit/iteration-4-state")


def create_fixture() -> tuple[str, object]:
    os.environ["HERMES_KANBAN_BOARD"] = "audit-scratch"
    from hermes_cli import kanban_db as kb

    conn = kb.connect()
    task_id = kb.create_task(
        conn,
        title=f"AUDIT RUNNING REASSIGN {time.time_ns()}",
        body="A live worker may not be silently reclaimed by profile reassignment.",
        assignee="coder",
        created_by="codex-audit",
        idempotency_key=f"codex-audit-running-reassign-{time.time_ns()}",
    )
    if not kb.claim_task(conn, task_id, claimer="codex-audit"):
        raise RuntimeError("could not claim running reassign fixture")
    kb._set_worker_pid(conn, task_id, os.getpid())
    return task_id, conn


def main() -> int:
    if PHASE not in {"before", "after"}:
        raise SystemExit("KANBAN_AUDIT_PHASE must be before or after")
    OUT.mkdir(parents=True, exist_ok=True)
    task_id: str | None = None
    conn = None
    console_errors: list[str] = []
    try:
        task_id, conn = create_fixture()
        title = conn.execute("SELECT title FROM tasks WHERE id = ?", (task_id,)).fetchone()[0]
        with sync_playwright() as playwright:
            browser = playwright.chromium.launch(headless=True)
            context = browser.new_context(viewport={"width": 1440, "height": 900})
            page = context.new_page()
            page.on(
                "console",
                lambda msg: console_errors.append(msg.text) if msg.type == "error" else None,
            )
            page.goto(f"{BASE}/control/fleet", wait_until="domcontentloaded", timeout=30_000)
            page.get_by_role("button", name="Subtab Board", exact=True).click()
            page.get_by_label("Tasks durchsuchen").fill(title)
            page.locator(".fleet-boardtab-title", has_text=title).wait_for(timeout=20_000)
            page.locator(".fleet-boardtab-title", has_text=title).click()
            page.get_by_text(task_id, exact=True).wait_for(timeout=20_000)

            lanes = page.evaluate(
                """async () => {
                  const headers = {'X-Hermes-Session-Token': window.__HERMES_SESSION_TOKEN__};
                  const response = await fetch('/api/plugins/kanban/lanes', {headers});
                  const body = await response.json();
                  return {
                    status: response.status,
                    profile_count: Array.isArray(body.profiles) ? body.profiles.length : -1
                  };
                }"""
            )
            if PHASE == "before":
                page.get_by_label("Zielprofil").wait_for(timeout=20_000)
            else:
                page.wait_for_timeout(2_000)

            profile_selects = page.get_by_label("Zielprofil").count()
            profile_buttons = page.get_by_role("button", name="Profil ändern", exact=True).count()
            backend = page.evaluate(
                """async ({taskId}) => {
                  const headers = {
                    'X-Hermes-Session-Token': window.__HERMES_SESSION_TOKEN__,
                    'Content-Type': 'application/json'
                  };
                  const response = await fetch(`/api/plugins/kanban/tasks/${taskId}/reassign`, {
                    method: 'POST', headers,
                    body: JSON.stringify({profile: 'verifier', reclaim_first: false, reason: 'audit'})
                  });
                  return {status: response.status, body: await response.json()};
                }""",
                {"taskId": task_id},
            )
            screenshot = OUT / f"running-reassign-{PHASE}-1440x900.png"
            page.screenshot(path=str(screenshot), full_page=True)
            context.close()
            browser.close()

        task = conn.execute(
            "SELECT status, assignee, current_run_id FROM tasks WHERE id = ?",
            (task_id,),
        ).fetchone()
        result = {
            "base": BASE,
            "board": "audit-scratch",
            "phase": PHASE,
            "task_id": task_id,
            "db": {
                "status": task["status"],
                "assignee": task["assignee"],
                "current_run_id": task["current_run_id"],
            },
            "backend_reassign": backend,
            "lanes": lanes,
            "dom_profile_selects": profile_selects,
            "dom_profile_buttons": profile_buttons,
            "unexpected_console_errors": [
                item for item in console_errors if "409 (Conflict)" not in item
            ],
            "expected_409_console_errors": len(
                [item for item in console_errors if "409 (Conflict)" in item]
            ),
            "screenshot": str(screenshot),
        }
        (OUT / f"running-reassign-{PHASE}-summary.json").write_text(
            json.dumps(result, indent=2, sort_keys=True) + "\n",
            encoding="utf-8",
        )
        print(json.dumps(result, indent=2, sort_keys=True))
        expected_dom_count = 1 if PHASE == "before" else 0
        return 0 if (
            task["status"] == "running"
            and task["assignee"] == "coder"
            and backend["status"] == 409
            and lanes["status"] == 200
            and lanes["profile_count"] > 0
            and profile_selects == expected_dom_count
            and profile_buttons == expected_dom_count
            and not result["unexpected_console_errors"]
        ) else 1
    finally:
        if conn is not None:
            from hermes_cli import kanban_db as kb

            if task_id:
                task = kb.get_task(conn, task_id)
                if task and task.status != "archived":
                    kb.archive_task(conn, task_id)
            conn.close()


if __name__ == "__main__":
    raise SystemExit(main())
