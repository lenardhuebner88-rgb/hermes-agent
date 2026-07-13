#!/usr/bin/env python3
"""Exercise Fleet's source-local stale disclosure against an isolated build."""
from __future__ import annotations

import json
import os
import re
import time
from pathlib import Path
from typing import Any, Callable

from playwright.sync_api import Page, Route, sync_playwright


BASE = os.environ.get("KANBAN_CANDIDATE_BASE", "http://127.0.0.1:9121")
OUT = Path("audit/iteration-2-stale")
SOURCE_PATHS = (
    "/api/plugins/kanban/workers/active",
    "/api/plugins/kanban/planspecs",
    "/api/plugins/kanban/runs/costs",
    "/api/plugins/kanban/runs/daily",
    "/api/plugins/kanban/runs/reliability",
    "/api/plugins/kanban/runs/live-events",
    "/chain-graph",
    "/chain-costs",
    "/api/plugins/kanban/tasks/review-verdicts",
    "/api/plugins/kanban/decision-queue",
    "/api/plugins/kanban/release-status",
    "/api/plugins/kanban/release-mode",
    "/api/plugins/kanban/lanes",
    "/api/health-status",
    "/api/pressure-status",
    "/api/account-usage",
    "/api/plugins/kanban/planspecs/detail",
)

EXPECTED_BY_TAB = {
    "Heute": {"Worker (alle Boards)", "PlanSpecs", "Kosten", "Tagesmetriken"},
    "Worker": {"Worker (alle Boards)", "Worker (aktuelles Board)", "Verlässlichkeit", "Kosten", "Live-Ereignisse"},
    "Ketten": {"Kettengraph", "Kettenkosten", "Review-Signale", "Worker (Kette)"},
    "Plan": {"PlanSpecs", "Kosten", "Lanes", "Account-Nutzung"},
    "Risiko": {
        "Worker (aktuelles Board)",
        "Verlässlichkeit",
        "Entscheidungsqueue",
        "Release-Status",
        "Release-Modus",
        "Lanes",
        "Systemzustand",
        "Systemdruck",
    },
}


def is_source(url: str) -> bool:
    return any(path in url for path in SOURCE_PATHS)


def visible_labels(page: Page) -> set[str]:
    sections = page.get_by_label("Datenfrische der sichtbaren Fleet-Quellen")
    labels: set[str] = set()
    for index in range(sections.count()):
        text = sections.nth(index).inner_text()
        for line in text.splitlines():
            stripped = line.strip()
            if stripped and not stripped.startswith("Veraltet") and stripped != "Fehler":
                labels.add(stripped)
    return labels


def refresh_visible_pollers(page: Page) -> None:
    page.evaluate("document.dispatchEvent(new Event('visibilitychange'))")


def wait_for_labels(page: Page, expected: set[str], timeout_seconds: float = 35.0) -> set[str]:
    deadline = time.monotonic() + timeout_seconds
    labels: set[str] = set()
    while time.monotonic() < deadline:
        labels = visible_labels(page)
        if expected <= labels:
            return labels
        page.wait_for_timeout(250)
    raise AssertionError(f"missing stale labels: {sorted(expected - labels)}; visible={sorted(labels)}")


def run_500_matrix(page: Page) -> list[dict[str, Any]]:
    def fail(route: Route) -> None:
        if is_source(route.request.url):
            route.fulfill(status=500, content_type="application/json", body='{"detail":"injected 500"}')
        else:
            route.continue_()

    rows: list[dict[str, Any]] = []
    for tab, expected in EXPECTED_BY_TAB.items():
        page.get_by_role("button", name=re.compile(rf"^Subtab {re.escape(tab)}(?:$|\s|\s—)")).click()
        page.wait_for_timeout(1_000)
        page.route("**/api/**", fail)
        refresh_visible_pollers(page)
        labels = wait_for_labels(page, expected)
        retained_text = page.locator(".fleet-tablet-main-scroll").inner_text()
        shot = OUT / f"500-{tab.lower()}-1440x900.png"
        page.screenshot(path=str(shot), full_page=True)
        page.unroute("**/api/**", fail)
        refresh_visible_pollers(page)
        # The two selected-chain hooks intentionally poll at 30 s and do not
        # share pollingStore's foreground invalidation. Prove their natural
        # recovery at the real cadence instead of reloading the page.
        deadline = time.monotonic() + (40 if tab == "Ketten" else 12)
        while time.monotonic() < deadline and visible_labels(page):
            page.wait_for_timeout(250)
        rows.append({
            "tab": tab,
            "expected": sorted(expected),
            "visible": sorted(labels),
            "retained_text_chars": len(retained_text),
            "recovery_cleared": not visible_labels(page),
            "screenshot": str(shot),
        })
    return rows


def run_contract_fault(
    page: Page,
    name: str,
    responder: Callable[[Route], None],
    *,
    timeout_seconds: float = 25,
) -> dict[str, Any]:
    target = "/api/health-status"

    def fault(route: Route) -> None:
        if target in route.request.url:
            responder(route)
        else:
            route.continue_()

    page.get_by_role("button", name=re.compile(r"^Subtab Risiko")).click()
    page.wait_for_timeout(500)
    page.route("**/api/**", fault)
    refresh_visible_pollers(page)
    labels = wait_for_labels(page, {"Systemzustand"}, timeout_seconds=timeout_seconds)
    page.unroute("**/api/**", fault)
    refresh_visible_pollers(page)
    deadline = time.monotonic() + 12
    while time.monotonic() < deadline and "Systemzustand" in visible_labels(page):
        page.wait_for_timeout(250)
    return {"fault": name, "visible": sorted(labels), "recovery_cleared": "Systemzustand" not in visible_labels(page)}


def run_auth_expiry(browser: Any) -> dict[str, Any]:
    context = browser.new_context(viewport={"width": 1440, "height": 900})
    page = context.new_page()
    page.goto(f"{BASE}/control/fleet", wait_until="networkidle", timeout=30_000)

    def expire(route: Route) -> None:
        if "/api/health-status" in route.request.url:
            route.fulfill(
                status=401,
                content_type="application/json",
                body='{"error":"session_expired","login_url":"/login?next=/control/fleet"}',
            )
        else:
            route.continue_()

    page.route("**/api/**", expire)
    refresh_visible_pollers(page)
    page.wait_for_url("**/login?next=/control/fleet", timeout=15_000)
    result = {"redirected": True, "url_path": page.url.removeprefix(BASE)}
    context.close()
    return result


def run_planspec_detail_empty_object(page: Page) -> dict[str, Any]:
    page.get_by_role("button", name=re.compile(r"^Subtab Heute")).click()
    card = page.locator("button.fleet-ps").first
    if card.count() == 0:
        return {"tested": False, "reason": "no visible PlanSpec card"}
    card.click()
    detail_pane = page.locator("#fleet-detail-pane")
    detail_pane.get_by_text("Ziel", exact=True).wait_for(timeout=15_000)
    goal_before = detail_pane.locator("section").first.locator("p").nth(1).inner_text()

    def empty_detail(route: Route) -> None:
        if "/api/plugins/kanban/planspecs/detail" in route.request.url:
            route.fulfill(status=200, content_type="application/json", body="{}")
        else:
            route.continue_()

    page.route("**/api/**", empty_detail)
    refresh_visible_pollers(page)
    alert = detail_pane.get_by_role("alert")
    alert.wait_for(timeout=15_000)
    retained = detail_pane.inner_text()
    goal_after = detail_pane.locator("section").first.locator("p").nth(1).inner_text()
    page.unroute("**/api/**", empty_detail)
    refresh_visible_pollers(page)
    alert.wait_for(state="hidden", timeout=15_000)
    return {
        "tested": True,
        "error_disclosed": "entspricht nicht dem Vertrag" in retained,
        "last_good_retained": bool(goal_before) and goal_after == goal_before,
        "recovery_cleared": not alert.is_visible(),
    }


def hang_for_thirty_seconds(route: Route) -> None:
    time.sleep(30)
    route.abort("timedout")


def main() -> int:
    OUT.mkdir(parents=True, exist_ok=True)
    console_errors: list[str] = []
    expected_fault_http: list[str] = []
    unexpected_http: list[str] = []
    with sync_playwright() as playwright:
        browser = playwright.chromium.launch(headless=True)
        context = browser.new_context(viewport={"width": 1440, "height": 900})
        page = context.new_page()
        page.on("console", lambda msg: console_errors.append(msg.text) if msg.type == "error" else None)
        def record_http(response: Any) -> None:
            if response.status < 400:
                return
            row = f"{response.status} {response.url}"
            if is_source(response.url):
                expected_fault_http.append(row)
            else:
                unexpected_http.append(row)

        page.on("response", record_http)
        page.goto(f"{BASE}/control/fleet", wait_until="networkidle", timeout=30_000)
        page.get_by_role("button", name="Subtab Heute", exact=True).wait_for()

        matrix_500 = run_500_matrix(page)
        planspec_detail = run_planspec_detail_empty_object(page)
        malformed = run_contract_fault(
            page,
            "malformed-json",
            lambda route: route.fulfill(status=200, content_type="application/json", body="{not-json"),
        )
        empty = run_contract_fault(
            page,
            "schema-empty-object",
            lambda route: route.fulfill(status=200, content_type="application/json", body="{}"),
        )
        network = run_contract_fault(page, "network-drop", lambda route: route.abort("connectionreset"))
        hang = run_contract_fault(
            page,
            "30-second-hang",
            hang_for_thirty_seconds,
            timeout_seconds=70,
        )
        auth_expiry = run_auth_expiry(browser)

        expected_console_errors = [
            item for item in console_errors
            if "Failed to load resource" in item or "ERR_CONNECTION_RESET" in item
        ]
        unexpected_console_errors = [item for item in console_errors if item not in expected_console_errors]

        result = {
            "base": BASE,
            "matrix_500": matrix_500,
            "planspec_detail_empty_object": planspec_detail,
            "contract_faults": [malformed, empty, network, hang],
            "auth_expiry": auth_expiry,
            "expected_fault_console_errors": len(expected_console_errors),
            "expected_fault_http_errors": len(expected_fault_http),
            "unexpected_console_errors": unexpected_console_errors,
            "unexpected_http": unexpected_http,
        }
        (OUT / "summary.json").write_text(
            json.dumps(result, indent=2, ensure_ascii=False, sort_keys=True) + "\n",
            encoding="utf-8",
        )
        print(json.dumps(result, indent=2, ensure_ascii=False, sort_keys=True))
        context.close()
        browser.close()

    failures = [
        any(not row["recovery_cleared"] for row in matrix_500),
        not planspec_detail.get("tested", False),
        not planspec_detail.get("error_disclosed", False),
        not planspec_detail.get("last_good_retained", False),
        not planspec_detail.get("recovery_cleared", False),
        any(not row["recovery_cleared"] for row in (malformed, empty, network, hang)),
        not auth_expiry["redirected"],
        bool(unexpected_console_errors),
        bool(unexpected_http),
    ]
    return 1 if any(failures) else 0


if __name__ == "__main__":
    raise SystemExit(main())
