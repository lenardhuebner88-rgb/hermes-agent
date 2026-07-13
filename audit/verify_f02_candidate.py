#!/usr/bin/env python3
"""Verify F-02 full-value recovery across Fleet and hostile title shapes."""
from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any

from playwright.sync_api import Page, sync_playwright

from scripts.control_shot import _credentials


BASE = os.environ.get("KANBAN_CANDIDATE_BASE", "http://127.0.0.1:4173")
AUTH_BASE = "http://127.0.0.1:9119"
OUT = Path("audit/iteration-1-f02")
WIDTH = int(os.environ.get("KANBAN_VIEWPORT_WIDTH", "1440"))
HEIGHT = int(os.environ.get("KANBAN_VIEWPORT_HEIGHT", "900"))
LABEL = f"{WIDTH}x{HEIGHT}"
TITLES = {
    "long": "L" * 400,
    "rtl": "مرحبا بالعالم " * 30,
    "combining": "e\u0301" * 200,
    "emoji": "👩🏽‍💻🚀" * 80,
}
CLIP_SELECTORS = ",".join(
    (
        ".fleet-boardtab-title",
        ".fleet-boardtab-meta",
        ".fleet-wk-task",
        ".fleet-wk-note",
        ".fleet-ps-name",
        ".fleet-lane-switch__summary-lane",
        ".fleet-lane-switch__summary-facts",
        ".fleet-lane-ld",
        ".fleet-bg-bl",
        ".fleet-lav-n",
        ".fleet-lhead-t",
        ".fleet-band-step",
        ".chain-title",
        ".pstep-label",
        ".pstep-sub",
        ".detail-title",
        ".model-label",
        ".utitle",
        ".dtitle",
        ".rk-nc-title",
        ".rk-rail-desc",
        ".fleet-activity-kind",
        ".fleet-activity-note",
        ".truncate",
        ".line-clamp-2",
    )
)


def create_fixtures(page: Page) -> list[dict[str, Any]]:
    return page.evaluate(
        """async ({titles}) => {
          const token = window.__HERMES_SESSION_TOKEN__;
          const results = [];
          for (const [kind, title] of Object.entries(titles)) {
            const response = await fetch('/api/plugins/kanban/tasks?board=audit-scratch', {
              method: 'POST',
              headers: {
                Accept: 'application/json',
                'Content-Type': 'application/json',
                ...(token ? {'X-Hermes-Session-Token': token} : {}),
              },
              body: JSON.stringify({
                title,
                body: `F-02 ${kind} hostile-title fixture`,
                park: true,
                notify_home: false,
                idempotency_key: `codex-kanban-next-level-f02-${kind}-v1`,
              }),
            });
            const body = await response.json();
            results.push({
              kind,
              status: response.status,
              task_id: body?.task?.id ?? null,
              expected_title: body?.task?.title ?? title.trim(),
            });
          }
          return results;
        }""",
        {"titles": TITLES},
    )


def scan_clipping(page: Page, subtab: str) -> list[dict[str, Any]]:
    return page.evaluate(
        """({selectors, subtab}) => Array.from(document.querySelectorAll(selectors))
          .filter(el => {
            const style = getComputedStyle(el);
            const visible = style.display !== 'none' && style.visibility !== 'hidden' && el.getClientRects().length > 0;
            const clipped = el.scrollWidth > el.clientWidth + 1 || el.scrollHeight > el.clientHeight + 1;
            return visible && clipped;
          })
          .map(el => {
            const text = (el.textContent ?? '').trim();
            const title = el.getAttribute('title') ?? '';
            const owner = el.closest('[aria-label]');
            const aria = owner?.getAttribute('aria-label') ?? '';
            return {
              subtab,
              selector: el.className,
              text,
              title,
              aria,
              recovered: Boolean(title) || (Boolean(aria) && (aria.includes(text) || text.includes(aria))),
            };
          })""",
        {"selectors": CLIP_SELECTORS, "subtab": subtab},
    )


def main() -> int:
    OUT.mkdir(parents=True, exist_ok=True)
    username, password = _credentials()
    console_errors: list[str] = []
    network_errors: list[str] = []

    with sync_playwright() as playwright:
        browser = playwright.chromium.launch(headless=True)
        context = browser.new_context(viewport={"width": WIDTH, "height": HEIGHT})
        login = context.request.post(
            f"{AUTH_BASE}/auth/password-login",
            data={"provider": "basic", "username": username, "password": password, "next": "/control/fleet"},
        )
        if not login.ok or login.json().get("ok") is not True:
            raise RuntimeError(f"login failed with HTTP {login.status}")
        page = context.new_page()
        page.on("console", lambda message: console_errors.append(message.text) if message.type == "error" else None)
        page.on(
            "response",
            lambda response: network_errors.append(f"{response.status} {response.request.method} {response.url}")
            if response.status >= 400
            else None,
        )
        page.goto(f"{BASE}/control/fleet", wait_until="networkidle", timeout=30_000)
        fixtures = create_fixtures(page)
        if any(item["status"] != 200 or not item["task_id"] for item in fixtures):
            raise RuntimeError(f"fixture creation failed: {fixtures}")

        page.get_by_role("button", name="Subtab Board", exact=True).click()
        page.get_by_label("Board auswählen").select_option("audit-scratch")
        page.locator(".fleet-boardtab-title").first.wait_for(state="visible", timeout=30_000)
        page.wait_for_timeout(500)
        fixture_results: list[dict[str, Any]] = []
        for fixture in fixtures:
            kind = fixture["kind"]
            title = fixture["expected_title"]
            locator = page.locator(".fleet-boardtab-title", has_text=title).first
            fixture_results.append(
                {
                    "kind": kind,
                    "count": locator.count(),
                    "text_matches": locator.text_content() == title if locator.count() else False,
                    "title_matches": locator.get_attribute("title") == title if locator.count() else False,
                }
            )
        screenshot = OUT / f"hostile-titles-{LABEL}.png"
        page.screenshot(path=str(screenshot), full_page=True)
        body_width = page.evaluate(
            "() => ({client: document.documentElement.clientWidth, scroll: document.documentElement.scrollWidth})"
        )

        page.get_by_label("Board auswählen").select_option("")
        clipped: list[dict[str, Any]] = []
        for subtab in ("Heute", "Worker", "Ketten", "Board", "Plan", "Risiko"):
            page.get_by_role("button", name=f"Subtab {subtab}", exact=False).click()
            page.wait_for_timeout(350)
            clipped.extend(scan_clipping(page, subtab))
        missing = [item for item in clipped if not item["recovered"]]
        result = {
            "base": BASE,
            "viewport": {"width": WIDTH, "height": HEIGHT},
            "fixtures": fixtures,
            "fixture_results": fixture_results,
            "clipped_count": len(clipped),
            "missing_recovery": missing,
            "body_width": body_width,
            "console_errors": console_errors,
            "network_errors": network_errors,
            "screenshot": str(screenshot),
        }
        (OUT / f"summary-{LABEL}.json").write_text(
            json.dumps(result, indent=2, ensure_ascii=False, sort_keys=True) + "\n",
            encoding="utf-8",
        )
        context.close()
        browser.close()

    print(json.dumps(result, indent=2, ensure_ascii=False, sort_keys=True))
    fixtures_ok = all(item["count"] == 1 and item["text_matches"] and item["title_matches"] for item in fixture_results)
    overflow = body_width["scroll"] > body_width["client"]
    return 1 if not fixtures_ok or missing or overflow or console_errors or network_errors else 0


if __name__ == "__main__":
    raise SystemExit(main())
