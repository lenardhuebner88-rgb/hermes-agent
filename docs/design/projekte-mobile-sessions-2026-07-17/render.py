#!/usr/bin/env python3
"""Render the mockup HTML files to PNG at phone size (390x844, 2x scale)."""
import sys
from pathlib import Path

from playwright.sync_api import sync_playwright

HERE = Path(__file__).parent


def main() -> int:
    targets = sys.argv[1:] or ["mockup-a-projektkarten", "mockup-b-sessions-first"]
    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)
        try:
            ctx = browser.new_context(
                viewport={"width": 390, "height": 844},
                device_scale_factor=2,
            )
            page = ctx.new_page()
            for name in targets:
                html = HERE / f"{name}.html"
                out = HERE / f"{name}.png"
                page.goto(html.as_uri(), wait_until="load")
                page.wait_for_timeout(350)
                page.screenshot(path=str(out), full_page=True)
                print(f"{out} ({out.stat().st_size} bytes)")
        finally:
            browser.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
