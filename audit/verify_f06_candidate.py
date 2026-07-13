#!/usr/bin/env python3
"""Prove archive DB/API/DOM truth against an isolated candidate server."""
from __future__ import annotations

import json
import os
import sqlite3
import time
from pathlib import Path
from typing import Any

from playwright.sync_api import Page, sync_playwright


BASE = os.environ.get("KANBAN_CANDIDATE_BASE", "http://127.0.0.1:9121")
DB_PATH = Path(os.environ.get("KANBAN_DB_PATH", "/home/piet/.hermes/kanban.db"))
OUT = Path("audit/iteration-1-f06")
WIDTH = int(os.environ.get("KANBAN_VIEWPORT_WIDTH", "1440"))
HEIGHT = int(os.environ.get("KANBAN_VIEWPORT_HEIGHT", "900"))
LABEL = f"{WIDTH}x{HEIGHT}"
ENUMERATE_ALL = os.environ.get("KANBAN_ENUMERATE_ALL", "1") == "1"


def db_archive_count() -> int:
    uri = f"file:{DB_PATH}?mode=ro&immutable=1"
    conn = sqlite3.connect(uri, uri=True)
    try:
        return int(conn.execute("SELECT COUNT(*) FROM tasks WHERE status = 'archived'").fetchone()[0])
    finally:
        conn.close()


def page_fetch(page: Page, path: str) -> dict[str, Any]:
    result = page.evaluate(
        """async ({path}) => {
          const token = window.__HERMES_SESSION_TOKEN__;
          const started = performance.now();
          const response = await fetch(path, {
            headers: {
              Accept: 'application/json',
              ...(token ? {'X-Hermes-Session-Token': token} : {}),
            },
          });
          const text = await response.text();
          return {
            status: response.status,
            bytes: new TextEncoder().encode(text).length,
            elapsed_ms: performance.now() - started,
            body: JSON.parse(text),
          };
        }""",
        {"path": path},
    )
    if result["status"] != 200:
        raise RuntimeError(f"GET {path} returned HTTP {result['status']}")
    return result


def enumerate_archive(page: Page) -> dict[str, Any]:
    cursor: str | None = None
    seen: list[str] = []
    pages = 0
    bytes_total = 0
    elapsed_total = 0.0
    first_page: dict[str, Any] | None = None
    while True:
        path = "/api/plugins/kanban/board/archive?limit=200"
        if cursor:
            from urllib.parse import quote

            path += f"&cursor={quote(cursor, safe='')}"
        response = page_fetch(page, path)
        body = response["body"]
        if first_page is None:
            first_page = response
        pages += 1
        bytes_total += int(response["bytes"])
        elapsed_total += float(response["elapsed_ms"])
        page_ids = [str(task["id"]) for task in body["tasks"]]
        if len(page_ids) != len(set(page_ids)):
            raise RuntimeError(f"duplicate ids within archive page {pages}")
        seen.extend(page_ids)
        if not body["has_more"]:
            if body["next_cursor"] is not None:
                raise RuntimeError("terminal archive page exposed a next cursor")
            break
        cursor = body["next_cursor"]
        if not cursor:
            raise RuntimeError("non-terminal archive page omitted next cursor")
    assert first_page is not None
    return {
        "pages": pages,
        "ids": seen,
        "bytes_total": bytes_total,
        "elapsed_ms_total": round(elapsed_total, 2),
        "first_page": {
            "bytes": first_page["bytes"],
            "elapsed_ms": round(first_page["elapsed_ms"], 2),
            "loaded_count": first_page["body"]["loaded_count"],
            "total_count": first_page["body"]["total_count"],
        },
    }


def main() -> int:
    OUT.mkdir(parents=True, exist_ok=True)
    db_count = db_archive_count()
    console_errors: list[str] = []
    network_errors: list[str] = []
    started = time.monotonic()
    with sync_playwright() as playwright:
        browser = playwright.chromium.launch(headless=True)
        context = browser.new_context(viewport={"width": WIDTH, "height": HEIGHT})
        page = context.new_page()
        page.on("console", lambda message: console_errors.append(message.text) if message.type == "error" else None)
        page.on(
            "response",
            lambda response: network_errors.append(f"{response.status} {response.request.method} {response.url}")
            if response.status >= 400 and not (response.status == 401 and response.url.endswith("/api/auth/me"))
            else None,
        )
        page.goto(f"{BASE}/control/fleet", wait_until="networkidle", timeout=30_000)
        active = page_fetch(page, "/api/plugins/kanban/board?card_diagnostics=summary&card_body=none")
        active_tasks = [task for column in active["body"]["columns"] for task in column["tasks"]]
        active_archived = [task["id"] for task in active_tasks if task["status"] == "archived"]

        archive = enumerate_archive(page) if ENUMERATE_ALL else None
        first_archive = page_fetch(page, "/api/plugins/kanban/board/archive?limit=200")
        search_id = first_archive["body"]["tasks"][0]["id"]
        search = page_fetch(page, f"/api/plugins/kanban/board/archive?limit=50&q={search_id}")

        page.get_by_role("button", name="Subtab Board", exact=True).click()
        page.get_by_label("Nach Status filtern").select_option("archived")
        page.get_by_text(f"50 von {db_count} Archivkarten geladen", exact=True).wait_for(timeout=30_000)
        initial_dom = page.locator(".fleet-boardtab-card").count()
        page.get_by_role("button", name="Weitere Archivkarten laden").click()
        page.get_by_text(f"100 von {db_count} Archivkarten geladen", exact=True).wait_for(timeout=30_000)
        loaded_dom = page.locator(".fleet-boardtab-card").count()

        page.get_by_label("Tasks durchsuchen").fill(search_id)
        page.get_by_text("1 von 1 Archivkarten geladen", exact=True).wait_for(timeout=30_000)
        search_dom = page.locator(".fleet-boardtab-card").count()
        search_dom_id = page.locator(".fleet-boardtab-id").first.text_content()
        screenshot = OUT / f"archive-search-{LABEL}.png"
        page.screenshot(path=str(screenshot), full_page=True)
        body_width = page.evaluate(
            "() => ({client: document.documentElement.clientWidth, scroll: document.documentElement.scrollWidth})"
        )
        context.close()
        browser.close()

    unique_archive_ids = len(set(archive["ids"])) if archive else None
    result = {
        "base": BASE,
        "viewport": {"width": WIDTH, "height": HEIGHT},
        "db_archive_count": db_count,
        "active_poll": {
            "bytes": active["bytes"],
            "elapsed_ms": round(active["elapsed_ms"], 2),
            "task_count": len(active_tasks),
            "archived_ids": active_archived,
            "has_archive_metadata": "archive" in active["body"],
        },
        "archive_enumeration": None if archive is None else {
            key: value for key, value in archive.items() if key != "ids"
        },
        "archive_unique_ids": unique_archive_ids,
        "search_api": {
            "id": search_id,
            "filtered_count": search["body"]["filtered_count"],
            "loaded_count": search["body"]["loaded_count"],
        },
        "dom": {
            "initial_count": initial_dom,
            "after_load_more_count": loaded_dom,
            "search_count": search_dom,
            "search_id_prefix": search_dom_id,
        },
        "body_width": body_width,
        "console_errors": console_errors,
        "network_errors": network_errors,
        "screenshot": str(screenshot),
        "elapsed_seconds": round(time.monotonic() - started, 2),
    }
    (OUT / f"summary-{LABEL}.json").write_text(
        json.dumps(result, indent=2, ensure_ascii=False, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    print(json.dumps(result, indent=2, ensure_ascii=False, sort_keys=True))
    failures = [
        db_count <= 0,
        bool(active_archived),
        "archive" in active["body"],
        archive is not None and len(archive["ids"]) != db_count,
        archive is not None and unique_archive_ids != db_count,
        first_archive["body"]["total_count"] != db_count,
        search["body"]["filtered_count"] != 1,
        initial_dom != 50,
        loaded_dom != 100,
        search_dom != 1,
        search_dom_id != search_id[:8],
        body_width["scroll"] > body_width["client"],
        bool(console_errors),
        bool(network_errors),
    ]
    return 1 if any(failures) else 0


if __name__ == "__main__":
    raise SystemExit(main())
