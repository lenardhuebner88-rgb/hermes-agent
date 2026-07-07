"""Tailwind Play CDN inlining for Design Board HTML mockups."""
from __future__ import annotations

import asyncio
import re


TAILWIND_CDN_SCRIPT_RE = re.compile(
    r"<script[^>]*src=[\"']https://cdn\.tailwindcss\.com[^\"']*[\"'][^>]*></script>",
    re.IGNORECASE,
)


def has_tailwind_cdn(html: str) -> bool:
    """Return True when the HTML depends on Tailwind Play CDN."""
    return bool(TAILWIND_CDN_SCRIPT_RE.search(html))


def inline_tailwind_cdn_mockup_html(html: str, *, width: int = 1280, height: int = 900) -> str:
    """Return self-contained HTML for Design Board mockups using Tailwind CDN.

    Non-Tailwind HTML is returned unchanged. When Tailwind Play CDN is present,
    a short-lived Playwright Chromium page lets Tailwind generate the CSS; the
    CDN script is then replaced by a static ``<style>`` block so the stored
    mockup can render offline/sandboxed in the Design tab.
    """
    if not has_tailwind_cdn(html):
        return html
    return asyncio.run(_inline_tailwind_cdn_mockup_html(html, width=width, height=height))


async def _inline_tailwind_cdn_mockup_html(html: str, *, width: int, height: int) -> str:
    try:
        from playwright.async_api import async_playwright
    except ImportError as exc:  # pragma: no cover - environment dependent
        raise RuntimeError(
            "Tailwind mockup preprocessing requires Playwright; install it or upload self-contained HTML"
        ) from exc

    async with async_playwright() as p:
        browser = await p.chromium.launch()
        page = await browser.new_page(viewport={"width": width, "height": height})
        await page.set_content(html, wait_until="networkidle")
        await page.wait_for_timeout(1500)
        generated_css = await page.evaluate(
            """
            () => {
                const styles = [];
                document.querySelectorAll('style').forEach((style) => {
                    const text = style.textContent || '';
                    if (text.includes('tailwind') || text.includes('--tw-') || text.includes('.')) {
                        styles.push(text);
                    }
                });
                return styles.join(String.fromCharCode(10));
            }
            """
        )
        await browser.close()

    if not generated_css.strip():
        raise RuntimeError("Tailwind CDN generated no CSS for mockup")

    style_block = f"<style data-design-board-tailwind-inline>\n{generated_css}\n</style>"
    return TAILWIND_CDN_SCRIPT_RE.sub(style_block, html, count=1)
