import { describe, expect, it } from "vitest";
import { readFileSync } from "node:fs";
import { fileURLToPath } from "node:url";

// Source tripwire, not a build assertion: incident 2026-07-03, the
// vite-plugin-pwa/Workbox service worker precached `index.html` and (via its
// default navigate fallback) answered every navigation from that static
// precache instead of letting the Python dashboard server render the page.
// The server injects auth bootstrap flags per-request
// (`__HERMES_SESSION_TOKEN__` / `__HERMES_AUTH_REQUIRED__`); the SW-served
// static HTML had neither, which silently broke WebSocket auth (Terminal-
// Attach, Kanban-Live-Events) while REST kept working via the cookie. This
// test reads `vite.config.ts` as text and asserts the two invariants that
// keep the SW out of the navigation path, so a future edit can't
// reintroduce the regression without a compile step or a running browser.
const configSrc = readFileSync(
  fileURLToPath(new URL("../../vite.config.ts", import.meta.url)),
  "utf8",
);

describe("vite.config.ts service worker precache", () => {
  it("disables the Workbox navigate fallback", () => {
    expect(configSrc).toMatch(/navigateFallback:\s*null/);
  });

  it("does not precache html (index.html must always be server-rendered)", () => {
    const globPatternsLine = configSrc
      .split("\n")
      .find((line) => line.includes("globPatterns"));
    expect(globPatternsLine).toBeDefined();
    expect(globPatternsLine).not.toMatch(/html/);
  });
});
