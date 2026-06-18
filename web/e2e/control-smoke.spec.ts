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

async function mockControlApis(page: Page) {
  await page.route("**/api/**", async (route) => {
    const path = new URL(route.request().url()).pathname;
    const body = path === "/api/dashboard/plugins" ? [] : path === "/api/account-usage" ? {
      cache_ttl_seconds: 60,
      providers: [
        {
          provider: "anthropic",
          available: true,
          source: "oauth",
          fetched_at: "2026-01-01T00:00:00+00:00",
          title: "Account limits",
          plan: "Max",
          cached: false,
          unavailable_reason: null,
          windows: [
            { label: "5h", used_percent: 82, reset_at: null, detail: "Reset rollierend" },
            { label: "Weekly", used_percent: null, reset_at: null, detail: "Limit unbekannt" },
          ],
          details: ["Details sichtbar"],
        },
      ],
    } : path === "/api/health-status" ? {
      overall: "healthy",
      subsystems: {
        gateway: { status: "healthy" },
        autoresearch: { status: "healthy" },
        kanban_db: { status: "healthy" },
      },
    } : path.includes("/workers/active") ? {
      workers: [],
      count: 0,
      checked_at: Math.floor(Date.now() / 1000),
    } : path.includes("/kanban/board") ? {
      columns: [],
      now: Math.floor(Date.now() / 1000),
    } : path.includes("/runs/today-digest") ? {
      count: 0,
      items: [],
    } : path.includes("/runs/daily") ? {
      series: [],
    } : path.includes("/autoresearch/proposals") ? {
      proposals: [],
      count: 0,
    } : path.includes("/family-organizer/backlog") ? {
      items: [],
    } : path.includes("/orchestration/backlog") ? {
      items: [],
    } : path.includes("/metrics-lite") ? {
      routes: [],
    } : {};
    await route.fulfill({ contentType: "application/json", body: JSON.stringify(body) });
  });
}

// probe = Hero-Eyebrow des Tabs; filter({ visible: true }) ist nötig, weil
// dieselben Wörter auch in versteckten Nav-/Overflow-Links vorkommen.
const TABS: Array<{ path: string; probe: string }> = [
  { path: "/control", probe: "Hermes Control" },
  { path: "/control/flow", probe: "Flow Command Board" },
  { path: "/control/statistik", probe: "Statistik" },
  { path: "/control/bibliothek", probe: "Bibliothek" },
];

test.describe("Control Smoke (live)", () => {
  for (const tab of TABS) {
    test(`lädt ${tab.path} ohne Konsolen-/Netzwerkfehler`, async ({ page }) => {
      const watch = watchPage(page);
      await page.goto(tab.path);
      await expect(page.getByText(tab.probe).filter({ visible: true }).first()).toBeVisible({ timeout: 15_000 });
      // Polls kurz arbeiten lassen, damit kaputte Endpoints auffallen würden.
      await page.waitForTimeout(2_000);
      watch.assertClean();
    });
  }

  test("Navigation: alle Primär-Tabs sind erreichbar und führen zum Ziel", async ({ page }) => {
    const watch = watchPage(page);
    await page.goto("/control");
    await expect(page.getByText("Hermes Control").filter({ visible: true }).first()).toBeVisible({ timeout: 15_000 });

    // Die Primär-Tabs sind BUTTONS (onNavigate), keine Links — Mobile
    // (Bottom-Nav, mobileLabel) und Desktop (Tab-Leiste, label) tragen
    // teils unterschiedliche Beschriftungen; der visible-Filter hält
    // versteckte Varianten der jeweils anderen Breakpoints fern.
    const nav = [
      { name: /^Flow/, url: /\/control\/flow$/ },
      { name: /^(Statistik|Stats)/, url: /\/control\/statistik$/ },
      { name: /^Bibliothek/, url: /\/control\/bibliothek$/ },
      { name: /^Start/, url: /\/control$/ },
    ];

    for (const target of nav) {
      await page.getByRole("button", { name: target.name }).filter({ visible: true }).first().click();
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

  for (const viewport of [
    { name: "Desktop", size: { width: 1440, height: 1000 } },
    { name: "Tablet", size: { width: 820, height: 1180 } },
  ]) {
    test(`Abo-Limits Tile rendert Gauges und unbekannte Limits (${viewport.name})`, async ({ page }) => {
      await page.setViewportSize(viewport.size);
      await mockControlApis(page);

      await page.goto("/control");

      await expect(page.getByText("Abo-Limits").filter({ visible: true })).toBeVisible({ timeout: 15_000 });
      // Bekanntes Fenster: Gauge ist ein role="meter" mit Prozent im Accessible Name.
      await expect(page.getByRole("meter", { name: /5-Std-Fenster: 82\s*% genutzt/ })).toBeVisible();
      // Unbekanntes Limit: das Wochen-Fenster ohne Prozentwert meldet "unbekannt".
      await expect(page.getByRole("meter", { name: /Diese Woche: unbekannt/ })).toBeVisible();
      // Nebendetails liegen im aufklappbaren Collapse — öffnen, dann ist der Inhalt sichtbar.
      await page.getByText("Details", { exact: true }).first().click();
      await expect(page.getByText("Details sichtbar").filter({ visible: true })).toBeVisible();
    });
  }
});
