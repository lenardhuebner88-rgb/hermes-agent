#!/usr/bin/env python3
"""DOM proof for adversarial Kanban timestamps on audit-scratch only."""
from __future__ import annotations

import json
import os
import time
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, urlparse

from playwright.sync_api import Page, Route, sync_playwright


BASE = os.environ.get("KANBAN_CANDIDATE_BASE", "http://127.0.0.1:9121")
OUT = Path("audit/iteration-3-timestamps")
NOW = int(time.time())


def task(task_id: str, title: str, status: str, **timestamps: Any) -> dict[str, Any]:
    return {
        "id": task_id,
        "title": title,
        "status": status,
        "assignee": "audit",
        "priority": 0,
        "created_at": NOW - 60,
        "started_at": None,
        "completed_at": None,
        "archived_at": None,
        "due_at": None,
        "last_heartbeat_at": None,
        "branch_name": None,
        "latest_summary": None,
        "link_counts": {"parents": 0, "children": 0},
        "comment_count": 0,
        "progress": None,
        "age": None,
        "tenant": "audit",
        "root_id": task_id,
        "epic_id": None,
        **timestamps,
    }


def install_audit_scratch_payloads(page: Page) -> None:
    def handler(route: Route) -> None:
        url = urlparse(route.request.url)
        query = parse_qs(url.query)
        board = query.get("board", [None])[0]
        if url.path.endswith("/api/plugins/kanban/board") and board == "audit-scratch":
            response = route.fetch()
            payload = response.json()
            injected = [
                task(
                    "audit_ts_invalid",
                    "AUDIT-TIMESTAMP invalid units",
                    "running",
                    created_at="not-a-time",
                    started_at=0,
                    last_heartbeat_at=NOW * 1000,
                ),
                task(
                    "audit_ts_future",
                    "AUDIT-TIMESTAMP scheduled future",
                    "scheduled",
                    due_at=NOW + 86_400,
                ),
                task(
                    "audit_ts_chronology",
                    "AUDIT-TIMESTAMP impossible chronology",
                    "done",
                    created_at=NOW - 10,
                    started_at=NOW - 20,
                    completed_at=NOW - 30,
                ),
            ]
            by_name = {column["name"]: column for column in payload["columns"]}
            for row in injected:
                by_name.setdefault(row["status"], {"name": row["status"], "tasks": []})["tasks"].insert(0, row)
            payload["columns"] = list(by_name.values())
            payload["now"] = NOW
            route.fulfill(response=response, body=json.dumps(payload))
            return
        if url.path.endswith("/api/plugins/kanban/workers/active") and board == "audit-scratch":
            response = route.fetch()
            payload = response.json()
            payload["workers"].insert(0, {
                "run_id": 9_999_001,
                "board_slug": "audit-scratch",
                "task_id": "audit_ts_worker",
                "task_title": "AUDIT-TIMESTAMP-WORKER future start",
                "task_status": "running",
                "task_assignee": "audit",
                "profile": "audit",
                "worker_pid": None,
                "started_at": NOW + 86_400,
                "claim_lock": "audit-only-browser-fixture",
                "claim_expires": NOW + 120,
                "last_heartbeat_at": NOW * 1000,
                "max_runtime_seconds": 3600,
                "run_status": "running",
                "run_outcome": None,
                "block_reason": None,
                "inspect": None,
                "last_heartbeat_note": None,
                "last_heartbeat_note_at": None,
                "eta_p50_seconds": 600,
                "eta_p90_seconds": 1200,
                "step_key": "audit",
                "model_override": None,
                "effective_model": None,
                "input_tokens": None,
                "output_tokens": None,
                "token_status": "no_live_sample",
                "token_status_reason": None,
                "run_progress": None,
                "heartbeat_ticks": [NOW * 1000],
            })
            payload["count"] = len(payload["workers"])
            route.fulfill(response=response, body=json.dumps(payload))
            return
        route.continue_()

    page.route("**/api/**", handler)


def disclosure_text(page: Page, title: str) -> str:
    summary = page.get_by_label(f"Weitere Informationen zu {title}")
    summary.click()
    return summary.locator("..").inner_text()


def run_viewport(page: Page, label: str) -> dict[str, Any]:
    console_errors: list[str] = []
    page.on("console", lambda msg: console_errors.append(msg.text) if msg.type == "error" else None)
    install_audit_scratch_payloads(page)
    page.goto(f"{BASE}/control/fleet", wait_until="networkidle", timeout=30_000)

    page.get_by_role("button", name="Subtab Worker", exact=True).click()
    worker = page.get_by_text("AUDIT-TIMESTAMP-WORKER future start", exact=True)
    worker.wait_for(timeout=20_000)
    worker_html = worker.locator("xpath=ancestor::button[1]").inner_html()

    page.get_by_role("button", name="Subtab Board", exact=True).click()
    page.get_by_label("Board auswählen").select_option("audit-scratch")
    page.get_by_label("Tasks durchsuchen").fill("AUDIT-TIMESTAMP")
    page.get_by_text("AUDIT-TIMESTAMP invalid units", exact=True).wait_for(timeout=20_000)

    invalid = disclosure_text(page, "AUDIT-TIMESTAMP invalid units")
    future = disclosure_text(page, "AUDIT-TIMESTAMP scheduled future")
    chronology = disclosure_text(page, "AUDIT-TIMESTAMP impossible chronology")
    screenshot = OUT / f"timestamp-matrix-{label}.png"
    page.screenshot(path=str(screenshot), full_page=True)
    width = page.evaluate("() => ({client: document.documentElement.clientWidth, scroll: document.documentElement.scrollWidth})")
    result = {
        "worker_invalid_duration": "Dauer ungültig" in worker_html,
        "worker_has_nan_geometry": "NaN" in worker_html,
        "invalid_labels": invalid.count("Zeit ungültig"),
        "future_disclosed": "zukünftig" in future,
        "chronology_disclosed": "Start liegt vor Anlage" in chronology,
        "body_width": width,
        "console_errors": console_errors,
        "screenshot": str(screenshot),
    }
    page.unroute_all(behavior="wait")
    return result


def main() -> int:
    OUT.mkdir(parents=True, exist_ok=True)
    results: dict[str, Any] = {}
    with sync_playwright() as playwright:
        browser = playwright.chromium.launch(headless=True)
        for width, height in ((1440, 900), (390, 844)):
            label = f"{width}x{height}"
            context = browser.new_context(viewport={"width": width, "height": height})
            results[label] = run_viewport(context.new_page(), label)
            context.close()
        browser.close()

    summary = {"base": BASE, "fixture_board": "audit-scratch", "viewports": results}
    (OUT / "summary.json").write_text(json.dumps(summary, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    print(json.dumps(summary, indent=2, sort_keys=True))
    failures = []
    for result in results.values():
        failures.extend([
            not result["worker_invalid_duration"],
            result["worker_has_nan_geometry"],
            result["invalid_labels"] < 2,
            not result["future_disclosed"],
            not result["chronology_disclosed"],
            result["body_width"]["scroll"] > result["body_width"]["client"],
            bool(result["console_errors"]),
        ])
    return 1 if any(failures) else 0


if __name__ == "__main__":
    raise SystemExit(main())
