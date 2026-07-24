import { mkdirSync } from "node:fs";
import path from "node:path";
import { fileURLToPath } from "node:url";
import { expect, test, type Page } from "@playwright/test";

// ESM has no __dirname; derive the spec dir from import.meta.url (a real file://
// URL under Playwright's Node runner — unlike vitest's jsdom, where it is http://).
const specDir = path.dirname(fileURLToPath(import.meta.url));

// E2E for the /lanes Modell-Plattform (greenfield). Drives the REAL built SPA
// against the REAL worktree backend (scripts/lanes-e2e.sh seeds a disposable
// HERMES_HOME with profile configs so the matrix + reasoning enabled/disabled
// states render for real). Only the probe POSTs are route-mocked — deterministic
// and cost-free; the catalog/lanes data is genuine backend output. The house
// watchPage/assertClean layer (copied from control-smoke.spec.ts) fails the test
// on any console.error / pageerror / failed request / 4xx-5xx.

type PageWatch = { consoleErrors: string[]; failedRequests: string[]; assertClean: () => void };
function watchPage(page: Page): PageWatch {
  const consoleErrors: string[] = [];
  const failedRequests: string[] = [];
  page.on("console", (m) => {
    if (m.type() === "error") {
      const loc = m.location();
      const where = loc.url ? ` @ ${loc.url}:${loc.lineNumber}` : "";
      consoleErrors.push(`${m.text()}${where}`);
    }
  });
  page.on("requestfailed", (r) => {
    const err = r.failure()?.errorText ?? "";
    if (err === "net::ERR_ABORTED") return;
    failedRequests.push(`${r.method()} ${r.url()} ${err}`);
  });
  page.on("pageerror", (e) => consoleErrors.push(e.stack ?? e.message));
  page.on("response", (res) => {
    if (res.status() >= 400) {
      failedRequests.push(`${res.status()} ${res.request().method()} ${res.url()} (${res.request().resourceType()})`);
    }
  });
  return {
    consoleErrors,
    failedRequests,
    assertClean() {
      expect(consoleErrors, "console.error messages").toEqual([]);
      expect(failedRequests, "failed or 4xx/5xx requests").toEqual([]);
    },
  };
}

// Deterministic probe results (route-mocked). Models exist in the seeded
// backend catalog so the feed rows resolve a provider dot + label.
const CATALOG_PROBE_BODY = {
  results: [
    { provider: "openai-codex", model: "gpt-5.6-sol", status: "ok", duration_ms: 380, observed_provider: "openai-codex", observed_model: "gpt-5.6-sol", at: 1 },
    { provider: "alibaba-token-plan", model: "qwen3.8-max-preview", status: "auth_error", duration_ms: 0, error_class: "auth_error", reason: "seat key", at: 1 },
  ],
  truncated: false,
};

async function mockProbes(page: Page) {
  await page.route(/\/api\/plugins\/kanban\/lanes\/catalog-probe$/, async (route) => {
    if (route.request().method() !== "POST") return route.continue();
    await route.fulfill({ status: 200, contentType: "application/json", body: JSON.stringify(CATALOG_PROBE_BODY) });
  });
  await page.route(/\/api\/plugins\/kanban\/lanes\/model-probe$/, async (route) => {
    if (route.request().method() !== "POST") return route.continue();
    await route.fulfill({
      status: 200,
      contentType: "application/json",
      body: JSON.stringify({ provider: "openai-codex", model: "gpt-5.6-sol", status: "ok", duration_ms: 380, at: 1 }),
    });
  });
}

function shotPath(page: Page, name: string): string {
  const dir = path.resolve(specDir, "../../docs/design/lanes-mockup-renders");
  mkdirSync(dir, { recursive: true });
  return path.join(dir, `${name}-${test.info().project.name}.png`);
}

test.describe("/lanes Modell-Plattform", () => {
  test("Expanded: lane bar + matrix + reasoning states + smoke probe feed + compass", async ({ page }) => {
    await page.setViewportSize({ width: 1440, height: 1000 });
    const watch = watchPage(page);
    await mockProbes(page);
    await page.goto("/control/lanes");

    // Lane bar (backend-seeded builtins) + new-lane affordance.
    await expect(page.getByText("api-standard").first()).toBeVisible({ timeout: 15_000 });
    await expect(page.getByText("max-abo").first()).toBeVisible();
    await expect(page.getByText("Neue Lane")).toBeVisible();

    // Matrix renders one row per seeded profile.
    await expect(page.getByText("Profil-Matrix")).toBeVisible();
    for (const profile of ["coder", "reviewer", "research"]) {
      await expect(page.getByText(profile, { exact: true }).first()).toBeVisible();
    }
    // Honest reasoning state: qwen/alibaba row has NO Reasoning-Knopf.
    await expect(page.getByText("Modell hat keinen Reasoning-Knopf").first()).toBeVisible();

    // Right pane subtabs + sinnvoll CTA + empty-state doctrine.
    await expect(page.getByText("Rauch").first()).toBeVisible();
    await expect(page.getByText("Kompass").first()).toBeVisible();
    const cta = page.getByRole("button", { name: /Katalog messen · \d+ sinnvolle Modelle/ });
    await expect(cta).toBeVisible();
    await expect(page.getByText("Noch keine Messungen")).toBeVisible();

    // Trigger the batch probe (route-mocked) → result feed renders cleanly.
    await cta.click();
    await expect(page.getByText("Noch keine Messungen")).toBeHidden({ timeout: 10_000 });
    await expect(page.getByText("Auth-Fehler").first()).toBeVisible();
    await expect(page.getByText(/alibaba-token-plan\/qwen3\.8-max-preview/).first()).toBeVisible();

    // Compass: role fit ranking against the real catalog.
    await page.getByText("Kompass").first().click();
    await expect(page.getByText("Top-Modelle für diese Rolle")).toBeVisible();

    await page.screenshot({ path: shotPath(page, "lanes-e2e-expanded"), fullPage: true });
    watch.assertClean();
  });

  test("Compact (390): lane pills + cards + Rauch/Kompass drawer", async ({ page }) => {
    await page.setViewportSize({ width: 390, height: 844 });
    const watch = watchPage(page);
    await mockProbes(page);
    await page.goto("/control/lanes");

    await expect(page.getByText("api-standard").first()).toBeVisible({ timeout: 15_000 });
    await expect(page.getByText("coder", { exact: true }).first()).toBeVisible();

    // Compact tier moves the panes into a drawer via two 44px triggers.
    await page.getByRole("button", { name: "Rauch", exact: true }).click();
    await expect(page.getByRole("button", { name: /Katalog messen/ })).toBeVisible({ timeout: 10_000 });

    await page.screenshot({ path: shotPath(page, "lanes-e2e-compact"), fullPage: true });
    watch.assertClean();
  });
});
