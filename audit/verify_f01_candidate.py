#!/usr/bin/env python3
"""Verify F-01 against a candidate Vite build and the live authenticated API."""
from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any

from playwright.sync_api import sync_playwright

from scripts.control_shot import _credentials


BASE = os.environ.get("KANBAN_CANDIDATE_BASE", "http://127.0.0.1:4173")
AUTH_BASE = "http://127.0.0.1:9119"
OUT = Path("audit/iteration-1-f01")
VIEWPORT_WIDTH = int(os.environ.get("KANBAN_VIEWPORT_WIDTH", "1440"))
VIEWPORT_HEIGHT = int(os.environ.get("KANBAN_VIEWPORT_HEIGHT", "900"))
VIEWPORT_LABEL = f"{VIEWPORT_WIDTH}x{VIEWPORT_HEIGHT}"


def flatten(board: dict[str, Any]) -> list[dict[str, Any]]:
    return [task for column in board["columns"] for task in column["tasks"]]


def main() -> int:
    OUT.mkdir(parents=True, exist_ok=True)
    username, password = _credentials()
    console_errors: list[str] = []
    network_errors: list[str] = []
    api_board: dict[str, Any] | None = None

    with sync_playwright() as playwright:
        browser = playwright.chromium.launch(headless=True)
        context = browser.new_context(viewport={"width": VIEWPORT_WIDTH, "height": VIEWPORT_HEIGHT})
        login = context.request.post(
            f"{AUTH_BASE}/auth/password-login",
            data={
                "provider": "basic",
                "username": username,
                "password": password,
                "next": "/control/fleet",
            },
        )
        if not login.ok or login.json().get("ok") is not True:
            raise RuntimeError(f"login failed with HTTP {login.status}")

        page = context.new_page()

        def on_response(response: Any) -> None:
            nonlocal api_board
            if response.status >= 400:
                network_errors.append(f"{response.status} {response.request.method} {response.url}")
            if "/api/plugins/kanban/board?" in response.url and "board=" not in response.url:
                api_board = response.json()

        page.on("response", on_response)
        page.on("console", lambda message: console_errors.append(message.text) if message.type == "error" else None)
        page.goto(f"{BASE}/control/fleet", wait_until="networkidle", timeout=30_000)
        page.get_by_role("button", name="Subtab Board", exact=True).click()
        page.locator(".fleet-boardtab-row").first.wait_for(state="visible", timeout=30_000)
        page.wait_for_timeout(250)
        if api_board is None:
            raise RuntimeError("candidate page did not expose its authenticated board response")

        tasks = flatten(api_board)
        cards = page.locator(".fleet-boardtab-card").evaluate_all(
            """cards => cards.map(card => ({
              idPrefix: card.querySelector('.fleet-boardtab-id')?.textContent?.trim() ?? null,
              compactText: card.querySelector('.fleet-boardtab-row')?.textContent ?? '',
              detailText: card.querySelector('.fleet-boardtab-details')?.textContent ?? '',
              summaryTag: card.querySelector('.fleet-boardtab-disclosure summary')?.tagName ?? null,
              summaryLabel: card.querySelector('.fleet-boardtab-disclosure summary')?.getAttribute('aria-label') ?? null,
            }))"""
        )
        by_prefix = {card["idPrefix"]: card for card in cards}
        failures: list[dict[str, Any]] = []
        checked_cells = 0

        def require(task: dict[str, Any], field: str, expected: str, haystack: str) -> None:
            nonlocal checked_cells
            checked_cells += 1
            if expected not in haystack:
                failures.append({"task_id": task["id"], "field": field, "expected": expected})

        for task in tasks:
            card = by_prefix.get(task["id"][:8])
            if card is None:
                failures.append({"task_id": task["id"], "field": "card", "expected": "present"})
                continue
            compact = card["compactText"]
            detail = card["detailText"]
            if card["summaryTag"] != "SUMMARY" or not card["summaryLabel"]:
                failures.append({"task_id": task["id"], "field": "disclosure", "expected": "named SUMMARY"})
            if task.get("assignee"):
                require(task, "assignee", str(task["assignee"]), compact + detail)
            if int(task.get("priority") or 0) != 0:
                require(task, "priority", str(task["priority"]), detail)
            if int(task.get("comment_count") or 0) > 0:
                require(task, "comment_count", str(task["comment_count"]), detail)
            links = task.get("link_counts") or {}
            if int(links.get("parents") or 0) > 0:
                require(task, "link_parents", str(links["parents"]), detail)
            if int(links.get("children") or 0) > 0:
                require(task, "link_children", str(links["children"]), detail)
            progress = task.get("progress")
            if progress and int(progress.get("total") or 0) > 0:
                require(task, "progress", f"{progress['done']}/{progress['total']}", detail)
            for field, label in (
                ("created_at", "Erstellt"),
                ("started_at", "Gestartet"),
                ("completed_at", "Fertig"),
                ("due_at", "Fällig"),
                ("last_heartbeat_at", "Heartbeat"),
            ):
                if task.get(field) is not None:
                    require(task, field, label, detail)

        first_summary = page.locator(".fleet-boardtab-disclosure summary").first
        first_summary.click()
        screenshot = OUT / f"candidate-f01-disclosure-{VIEWPORT_LABEL}.png"
        page.screenshot(path=str(screenshot), full_page=True)
        body_text = page.locator("body").inner_text().strip()
        body_width = page.evaluate(
            "() => ({client: document.documentElement.clientWidth, scroll: document.documentElement.scrollWidth})"
        )
        overlay = page.locator("vite-error-overlay, .vite-error-overlay").count()
        result = {
            "base": BASE,
            "viewport": {"width": VIEWPORT_WIDTH, "height": VIEWPORT_HEIGHT},
            "api_task_count": len(tasks),
            "dom_card_count": len(cards),
            "disclosure_count": page.locator(".fleet-boardtab-disclosure").count(),
            "checked_cells": checked_cells,
            "failures": failures,
            "console_errors": console_errors,
            "network_errors": network_errors,
            "body_has_content": bool(body_text),
            "body_width": body_width,
            "error_overlay_count": overlay,
            "screenshot": str(screenshot),
        }
        (OUT / f"candidate-f01-summary-{VIEWPORT_LABEL}.json").write_text(
            json.dumps(result, indent=2, ensure_ascii=False, sort_keys=True) + "\n",
            encoding="utf-8",
        )
        context.close()
        browser.close()

    print(json.dumps(result, indent=2, ensure_ascii=False, sort_keys=True))
    overflow = body_width["scroll"] > body_width["client"]
    return 1 if failures or console_errors or network_errors or not body_text or overlay or overflow else 0


if __name__ == "__main__":
    raise SystemExit(main())
