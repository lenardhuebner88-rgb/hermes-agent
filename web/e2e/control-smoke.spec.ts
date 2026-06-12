import { expect, test, type Page } from "@playwright/test";

// Live-Smoke gegen das echte Dashboard (:9119, keine Mocks): die vier
// Primär-Tabs laden ohne console.error/4xx, die Bottom-Nav (Mobile) bzw.
// Tab-Leiste (Desktop) navigiert, und der Flow-Tab hat seine Kern-Bedienung
// (Aktualisieren-Button). Bewusst datentolerant — das Board ist live.

type PageWatch = {
  consoleErrors: string[];
  failedRequests: string[];
  assertClean: () => void;
};

function watchPage(page: Page): PageWatch {
  const consoleErrors: string[] = [];
  const failedRequests: string[] = [];

  page.on("console", (message) => {
    if (message.type() === "error") consoleErrors.push(message.text());
  });
  page.on("requestfailed", (request) => {
    failedRequests.push(`${request.method()} ${request.url()} ${request.failure()?.errorText ?? "request failed"}`);
  });
  page.on("response", (response) => {
    const status = response.status();
    if (status >= 400) failedRequests.push(`${status} ${response.url()}`);
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

const TABS: Array<{ path: string; probe: RegExp | string }> = [
  { path: "/control", probe: /Hermes Control/ },
  { path: "/control/flow", probe: /Flow Command Board|Lauf|Kanban/ },
  { path: "/control/statistik", probe: /Statistik|Zuverlässigkeit|Kosten/ },
  { path: "/control/bibliothek", probe: /Bibliothek|Regal|News/ },
];

test.describe("Control Smoke (live)", () => {
  for (const tab of TABS) {
    test(`lädt ${tab.path} ohne Konsolen-/Netzwerkfehler`, async ({ page }) => {
      const watch = watchPage(page);
      await page.goto(tab.path);
      await expect(page.getByText(tab.probe).first()).toBeVisible({ timeout: 15_000 });
      // Polls kurz arbeiten lassen, damit kaputte Endpoints auffallen würden.
      await page.waitForTimeout(2_000);
      watch.assertClean();
    });
  }

  test("Navigation: alle Primär-Tabs sind erreichbar und führen zum Ziel", async ({ page, isMobile }) => {
    const watch = watchPage(page);
    await page.goto("/control");
    await expect(page.getByText(/Hermes Control/).first()).toBeVisible({ timeout: 15_000 });

    const nav = isMobile
      ? [
          { name: "Flow", url: /\/control\/flow$/ },
          { name: "Stats", url: /\/control\/statistik$/ },
          { name: "Bibliothek", url: /\/control\/bibliothek$/ },
          { name: "Start", url: /\/control$/ },
        ]
      : [
          { name: /Flow/, url: /\/control\/flow$/ },
          { name: /Statistik|Stats/, url: /\/control\/statistik$/ },
          { name: "Bibliothek", url: /\/control\/bibliothek$/ },
          { name: "Start", url: /\/control$/ },
        ];

    for (const target of nav) {
      await page.getByRole("link", { name: target.name, exact: typeof target.name === "string" }).first().click();
      await expect(page).toHaveURL(target.url);
    }
    watch.assertClean();
  });

  test("Flow-Tab: Aktualisieren-Button ist vorhanden und klickbar", async ({ page }) => {
    const watch = watchPage(page);
    await page.goto("/control/flow");
    const refresh = page.getByRole("button", { name: "Aktualisieren" });
    await expect(refresh).toBeVisible({ timeout: 15_000 });
    await refresh.click();
    await page.waitForTimeout(1_000);
    watch.assertClean();
  });
});
