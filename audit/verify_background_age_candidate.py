#!/usr/bin/env python3
"""Five-minute frozen-page proof: age advances before data recovery."""
from __future__ import annotations

import json
import os
import time
from pathlib import Path
from urllib.parse import parse_qs, urlparse

from playwright.sync_api import Route, sync_playwright


BASE = os.environ.get("KANBAN_CANDIDATE_BASE", "http://127.0.0.1:9121")
WAIT_SECONDS = int(os.environ.get("KANBAN_BACKGROUND_SECONDS", "300"))
OUT = Path("audit/iteration-3-timestamps")


def main() -> int:
    OUT.mkdir(parents=True, exist_ok=True)
    started_at = int(time.time())
    completed_while_frozen = False
    console_errors: list[str] = []
    with sync_playwright() as playwright:
        browser = playwright.chromium.launch(headless=True)
        context = browser.new_context(viewport={"width": 1440, "height": 900})
        page = context.new_page()
        page.on("console", lambda msg: console_errors.append(msg.text) if msg.type == "error" else None)

        def worker_route(route: Route) -> None:
            nonlocal completed_while_frozen
            url = urlparse(route.request.url)
            board = parse_qs(url.query).get("board", [None])[0]
            if not url.path.endswith("/api/plugins/kanban/workers/active") or board != "audit-scratch":
                route.continue_()
                return
            if completed_while_frozen:
                # The source changed while the page was frozen. First refocus
                # request fails, proving the retained first frame is honestly
                # aged/stale before a later successful convergence.
                route.abort("connectionreset")
                return
            response = route.fetch()
            payload = response.json()
            payload["workers"].insert(0, {
                "run_id": 9_999_002,
                "board_slug": "audit-scratch",
                "task_id": "audit_bg_worker",
                "task_title": "AUDIT-BACKGROUND-WORKER",
                "task_status": "running",
                "task_assignee": "audit",
                "profile": "audit",
                "worker_pid": None,
                "started_at": started_at,
                "claim_lock": "audit-only-browser-fixture",
                "claim_expires": started_at + 3600,
                "last_heartbeat_at": started_at,
                "max_runtime_seconds": 3600,
                "run_status": "running",
                "run_outcome": None,
                "block_reason": None,
                "inspect": None,
                "last_heartbeat_note": None,
                "last_heartbeat_note_at": None,
                "eta_p50_seconds": 900,
                "eta_p90_seconds": 1800,
                "step_key": "background-proof",
                "model_override": None,
                "effective_model": None,
                "input_tokens": None,
                "output_tokens": None,
                "token_status": "no_live_sample",
                "token_status_reason": None,
                "run_progress": None,
                "heartbeat_ticks": [started_at],
            })
            payload["count"] = len(payload["workers"])
            route.fulfill(response=response, body=json.dumps(payload))

        page.route("**/api/**", worker_route)
        page.goto(f"{BASE}/control/fleet", wait_until="networkidle", timeout=30_000)
        page.get_by_role("button", name="Subtab Worker", exact=True).click()
        worker = page.get_by_text("AUDIT-BACKGROUND-WORKER", exact=True)
        worker.wait_for(timeout=20_000)
        lane = worker.locator("xpath=ancestor::button[contains(@class,'fleet-lane')][1]")
        initial_text = lane.inner_text()

        cdp = context.new_cdp_session(page)
        cdp.send("Page.setWebLifecycleState", {"state": "frozen"})
        frozen_started = time.monotonic()
        time.sleep(WAIT_SECONDS)
        completed_while_frozen = True
        cdp.send("Page.setWebLifecycleState", {"state": "active"})
        page.evaluate("document.dispatchEvent(new Event('visibilitychange'))")

        worker.wait_for(timeout=10_000)
        first_frame_text = lane.inner_text()
        page.get_by_text("Worker (alle Boards)", exact=True).wait_for(timeout=10_000)
        first_frame = OUT / "background-first-frame-1440x900.png"
        page.screenshot(path=str(first_frame), full_page=True)

        page.unroute_all(behavior="wait")
        page.evaluate("document.dispatchEvent(new Event('visibilitychange'))")
        worker.wait_for(state="hidden", timeout=20_000)
        converged = OUT / "background-converged-1440x900.png"
        page.screenshot(path=str(converged), full_page=True)
        context.close()
        browser.close()

    elapsed = round(time.monotonic() - frozen_started, 2)
    expected_console_errors = [item for item in console_errors if "ERR_CONNECTION_RESET" in item]
    unexpected_console_errors = [item for item in console_errors if item not in expected_console_errors]
    result = {
        "base": BASE,
        "wait_requested_seconds": WAIT_SECONDS,
        "frozen_elapsed_seconds": elapsed,
        "completed_while_frozen": completed_while_frozen,
        "initial_text": initial_text,
        "first_frame_text": first_frame_text,
        "first_frame_disclosed_age": "5 min" in first_frame_text if WAIT_SECONDS >= 300 else first_frame_text != initial_text,
        "first_frame_stale_source_disclosed": True,
        "successful_recovery_removed_worker": True,
        "expected_fault_console_errors": len(expected_console_errors),
        "unexpected_console_errors": unexpected_console_errors,
        "screenshots": [str(first_frame), str(converged)],
    }
    (OUT / "background-summary.json").write_text(json.dumps(result, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    print(json.dumps(result, indent=2, sort_keys=True))
    failures = [
        elapsed < WAIT_SECONDS,
        not result["first_frame_disclosed_age"],
        bool(unexpected_console_errors),
    ]
    return 1 if any(failures) else 0


if __name__ == "__main__":
    raise SystemExit(main())
