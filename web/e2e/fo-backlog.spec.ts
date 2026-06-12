import { expect, test, type Page, type Route } from "@playwright/test";

const STORAGE_KEY = "fo-backlog-view-v1";
const QUICK_VIEWS: Array<string | RegExp> = ["Commission-ready", /Grooming n.tig/, "Stale", "Ohne Owner", "Alle"];
const DRAWER_SECTIONS = ["Decision / Why now", "Acceptance Criteria", "Current Evidence / Last Proof", "Blockers"];

// FO-Backlog-Fixture (t_278af546): Der CandidateCompareStrip rendert per Design
// erst ab 2 aktiven ranked-Kandidaten (BacklogSections.tsx: `topCandidates.length
// <= 1 → null`). Gegen das LIVE-Backlog hing die Suite damit von der aktuellen
// Kandidatenzahl ab (<2 ranked → Strip fehlt → rote Tests). Wie in
// control-triage.spec.ts mocken wir deshalb NUR die FO-Backlog-Datenschicht per
// page.route (Liste + Detail) und fahren weiter die echte SPA auf :9119 —
// deterministisch, egal ob das Live-Backlog 0, 1 oder >=2 Kandidaten hat.
const BACKLOG_API = "**/api/family-organizer/backlog";
const BACKLOG_DETAIL_API = "**/api/family-organizer/backlog/*";
const FIXTURE_NOW = 1_781_222_400; // 2026-06-12T00:00:00Z — fix, damit Age/Stale-Ableitungen stabil sind

// 4 aktive Items (now/next/in_progress/blocked) + 1 done: deckt alle Quick-Views
// (ready/groom/stale/unowned) und den Stale+High-Risk-Filterpfad des Tests ab.
// Server-Facts (freshness/age_days/quality_issues/readiness) sind gesetzt, damit
// keine Client-Heuristik gegen die echte Uhr rechnet.
const FIXTURE_ITEMS = [
  {
    id: "0901",
    title: "Kalender-Sync: DB-Migrationspfad absichern",
    status: "now",
    owner: "piet",
    risk: "high",
    area: "db",
    updated: "2026-06-10",
    lane: null,
    result: null,
    stale: false,
    excerpt: "Migrationspfad für den Kalender-Sync gegen Teilausfälle härten.",
    source_path: "backlog/items/0901-kalender-sync-migration.md",
    missing_acceptance: false,
    missing_next_action: false,
    age_days: 2,
    freshness: "fresh",
    quality_issues: [],
    readiness: "ready",
  },
  {
    id: "0902",
    title: "Einkaufslisten-Offline-Queue stabilisieren",
    status: "next",
    owner: "claude",
    risk: "high",
    area: "shopping",
    updated: "2026-05-18",
    lane: null,
    result: null,
    stale: true,
    excerpt: "Offline-Mutationen gehen bei Reconnect verloren.",
    source_path: "backlog/items/0902-offline-queue.md",
    missing_acceptance: true,
    missing_next_action: false,
    age_days: 25,
    freshness: "stale",
    quality_issues: [
      { code: "missing_acceptance", severity: "risk" },
      { code: "stale_update", severity: "risk" },
    ],
    readiness: "needs_grooming",
  },
  {
    id: "0903",
    title: "Küchen-Display: Wochenplan-Karte",
    status: "in_progress",
    owner: "unassigned",
    risk: "medium",
    area: "kitchen",
    updated: "2026-06-04",
    lane: null,
    result: null,
    stale: false,
    excerpt: "Wochenplan als Karte auf dem Küchen-Tablet.",
    source_path: "backlog/items/0903-wochenplan-karte.md",
    missing_acceptance: false,
    missing_next_action: true,
    age_days: 8,
    freshness: "aging",
    quality_issues: [{ code: "missing_next_action", severity: "risk" }],
    readiness: "needs_grooming",
  },
  {
    id: "0904",
    title: "Admin: Geburtstags-Reminder konfigurierbar",
    status: "blocked",
    owner: "codex",
    risk: "low",
    area: "admin",
    updated: "2026-06-11",
    lane: null,
    result: null,
    stale: false,
    excerpt: "Reminder-Vorlauf pro Familienmitglied einstellbar machen.",
    source_path: "backlog/items/0904-geburtstags-reminder.md",
    missing_acceptance: false,
    missing_next_action: false,
    age_days: 1,
    freshness: "fresh",
    quality_issues: [],
    readiness: "blocked",
  },
  {
    id: "0999",
    title: "Anwesenheits-Badge im Header",
    status: "done",
    owner: "piet",
    risk: "low",
    area: "process",
    updated: "2026-06-09",
    lane: null,
    result: "Shipped — Badge live auf dem Küchen-Tablet.",
    stale: false,
    excerpt: "Erledigt.",
    source_path: "backlog/items/0999-anwesenheits-badge.md",
    missing_acceptance: false,
    missing_next_action: false,
    age_days: 3,
    freshness: "fresh",
    quality_issues: [],
    readiness: "ready",
  },
];

function backlogPayload() {
  return {
    schema: "fo-backlog-v2",
    checked_at: FIXTURE_NOW,
    items: FIXTURE_ITEMS,
    counts: { now: 1, next: 1, in_progress: 1, blocked: 1, later: 0, done: 1 },
    contract_health: {
      source_count: FIXTURE_ITEMS.length,
      counted_sum: FIXTURE_ITEMS.length,
      unknown_statuses: [],
      invalid_risk_count: 0,
      invalid_owner_count: 0,
      unowned_count: 1,
      stale_count: 1,
      missing_acceptance_count: 1,
      missing_next_action_count: 1,
      invalid_area_count: 0,
    },
    source: { dir: "backlog/items", ref: "e2e-fixture", count: FIXTURE_ITEMS.length },
    error: null,
  };
}

function detailPayload(id: string) {
  const item = FIXTURE_ITEMS.find((candidate) => candidate.id === id) ?? FIXTURE_ITEMS[0];
  return {
    ...item,
    result: item.result,
    body: `${item.excerpt}\n\nAkzeptanzkriterien:\n- Kriterium A\n- Kriterium B`,
    decision: [`Jetzt, weil ${item.area} ein sichtbarer Operator-Pfad ist.`],
    acceptance_criteria: ["Kriterium A ist erfüllt.", "Kriterium B ist per Test belegt."],
    proofs: item.status === "done" ? ["2026-06-09 Gate grün, deployt."] : ["2026-06-10 vitest grün (Fixture)."],
    blockers: item.status === "blocked" ? ["Wartet auf Operator-Entscheid zum Reminder-Vorlauf."] : [],
    next_action: "Spec vollständig lesen und Umsetzung vorbereiten.",
    source_path: item.source_path,
    source_ref: `${item.source_path}@e2e-fixture`,
    links: [],
  };
}

async function installBacklogMocks(page: Page) {
  await page.route(BACKLOG_API, async (route: Route) => {
    await route.fulfill({ json: backlogPayload() });
  });
  await page.route(BACKLOG_DETAIL_API, async (route: Route) => {
    const id = decodeURIComponent(new URL(route.request().url()).pathname.split("/").pop() ?? "");
    await route.fulfill({ json: detailPayload(id) });
  });
}

type PageWatch = {
  consoleErrors: string[];
  failedRequests: string[];
  assertClean: () => Promise<void>;
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
    async assertClean() {
      await expect(page.getByText("Failed to fetch", { exact: false })).toHaveCount(0);
      expect(consoleErrors, "console.error messages").toEqual([]);
      expect(failedRequests, "failed or 4xx/5xx requests").toEqual([]);
    },
  };
}

async function gotoBacklog(page: Page) {
  await page.goto("/control/backlog");
  await expect(page.getByRole("heading", { name: /Backlog/ })).toBeVisible();
  await expect(page.locator("tr[data-fo-row]").first()).toBeVisible();
}

async function activeRowIds(page: Page): Promise<string[]> {
  return page.locator('tr[data-fo-row][aria-current="true"]').evaluateAll((rows) =>
    rows.map((row) => row.getAttribute("data-fo-row") ?? ""),
  );
}

async function expectClipboardText(page: Page, expected: RegExp) {
  await expect.poll(async () => page.evaluate(() => navigator.clipboard.readText())).toMatch(expected);
}

test.describe("FO backlog command UX", () => {
  test.beforeEach(async ({ context, page, baseURL }) => {
    await context.grantPermissions(["clipboard-read", "clipboard-write"], { origin: baseURL });
    await page.addInitScript((key) => {
      const flag = key + ":cleared-for-test";
      if (!window.sessionStorage.getItem(flag)) {
        window.localStorage.removeItem(key);
        window.sessionStorage.setItem(flag, "1");
      }
    }, STORAGE_KEY);
  });

  test("renders fixture-backed v2 data and supports commandable queue interactions", async ({ page }) => {
    const watch = watchPage(page);
    await installBacklogMocks(page);

    await gotoBacklog(page);

    await expect(page.getByLabel("FO Contract Health")).toBeVisible();
    for (const label of ["Now", "Next Ready", "Blocked", "Unowned", "Stale", "High Risk", "Contract Drift", "Missing Acceptance"]) {
      await expect(page.getByLabel("FO Contract Health").getByText(label, { exact: true })).toBeVisible();
    }
    await expect(page.getByText("Backlog konnte nicht geladen werden.")).toHaveCount(0);
    await expect(page.getByText("Backlog-Verzeichnis nicht gefunden", { exact: false })).toHaveCount(0);

    const rows = page.locator("tr[data-fo-row]");
    const rowCount = await rows.count();
    expect(rowCount, "FO backlog rows").toBeGreaterThan(0);

    await page.keyboard.press("j");
    await expect.poll(() => activeRowIds(page)).toHaveLength(1);
    const firstSelected = (await activeRowIds(page))[0];
    await expect(page.locator('tr[data-fo-row][aria-current="true"]')).toHaveCount(1);

    if (rowCount > 1) {
      await page.keyboard.press("j");
      await expect.poll(() => activeRowIds(page)).not.toEqual([firstSelected]);
      await page.keyboard.press("k");
      await expect.poll(() => activeRowIds(page)).toEqual([firstSelected]);
      await page.keyboard.press("ArrowDown");
      await expect.poll(() => activeRowIds(page)).toHaveLength(1);
      await page.keyboard.press("ArrowUp");
      await expect.poll(() => activeRowIds(page)).toHaveLength(1);
    }

    await page.keyboard.press("Enter");
    const dialog = page.getByRole("dialog");
    await expect(dialog).toBeVisible();
    await expect(dialog).toHaveAttribute("aria-modal", "true");
    for (const section of DRAWER_SECTIONS) {
      await expect(dialog.getByRole("heading", { name: section })).toBeVisible();
    }

    await dialog.getByRole("button", { name: "Copy operator brief" }).click();
    await expectClipboardText(page, /^FO Backlog /);
    await dialog.getByRole("button", { name: "Copy implementation prompt" }).click();
    await expectClipboardText(page, /Arbeite GENAU EINEN FO-Backlog-Task ab/);
    await page.keyboard.press("Escape");
    await expect(dialog).toHaveCount(0);

    await page.keyboard.press("?");
    await expect(page.getByRole("region", { name: /Tastenk.rzel/ })).toBeVisible();
    await page.keyboard.press("?");
    await expect(page.getByRole("region", { name: /Tastenk.rzel/ })).toHaveCount(0);

    const quickViews = page.getByRole("group", { name: "Gespeicherte Ansichten" });
    for (const quickView of QUICK_VIEWS) {
      const button = quickViews.getByRole("button", { name: quickView });
      await button.click();
      await expect(button).toHaveAttribute("aria-pressed", "true");
    }

    await page.getByRole("button", { name: /Grooming n.tig/ }).click();
    await page.getByRole("button", { name: "Stale" }).click();
    await page.getByRole("button", { name: "Risiko" }).click();
    await page.getByRole("button", { name: "high" }).click();
    const persisted = await page.evaluate((key) => JSON.parse(window.localStorage.getItem(key) ?? "{}"), STORAGE_KEY);
    expect(persisted).toMatchObject({ quickView: "stale", sortKey: "risk", filterRisk: "high" });

    await page.reload();
    await expect(page.getByRole("heading", { name: /Backlog/ })).toBeVisible();
    await expect(page.getByRole("button", { name: "Stale" })).toHaveAttribute("aria-pressed", "true");
    await expect(page.getByRole("button", { name: "Risiko" })).toHaveClass(/border-cyan/);
    await expect(page.getByRole("button", { name: "high" })).toHaveClass(/border-cyan/);

    // Fixture-deterministisch: 4 aktive Kandidaten → Strip rendert die Top 3,
    // Rang 1 ist das now/high/db-Item 0901 (rankFoItems-Gewichte). Locator über
    // das Heading statt getByLabel: das Section-Primitive setzt kein aria-label.
    const compareStrip = page
      .locator("section")
      .filter({ has: page.getByRole("heading", { name: "Top-Kandidaten vergleichen" }) });
    await expect(compareStrip).toBeVisible();
    await expect(compareStrip.getByText("#1 · 0901")).toBeVisible();
    const reasonChips = compareStrip.getByText(/Status now|Status next|L.uft|Hohes Risiko|Wichtiger Bereich|Lange offen|Kein Owner|Stale|Akzeptanz fehlt|Next Action fehlt|Grooming n.tig|Vertragsdrift/);
    expect(await reasonChips.count()).toBeGreaterThan(0);
    const commissionButtons = compareStrip.getByRole("button", { name: /N.chsten beauftragen|kopiert/ });
    expect(await commissionButtons.count()).toBe(3);

    await watch.assertClean();
  });

  test("loads control regressions without browser or network errors", async ({ context }) => {
    for (const route of [
      { path: "/control/orchestrator" },
      { path: "/control/autoresearch" },
    ]) {
      const routePage = await context.newPage();
      const watch = watchPage(routePage);
      await routePage.goto(route.path);
      await expect(routePage.locator("[data-control]")).toBeVisible();
      await expect(routePage).toHaveURL(new RegExp(route.path.replaceAll("/", "\\/")));
      await expect(routePage.getByText("Failed to fetch", { exact: false })).toHaveCount(0);
      await watch.assertClean();
      await routePage.close();
    }
  });

  test("keeps the mobile backlog usable at 390px", async ({ page }) => {
    const watch = watchPage(page);
    await installBacklogMocks(page);
    await page.setViewportSize({ width: 390, height: 844 });

    await gotoBacklog(page);

    const viewport = page.viewportSize();
    expect(viewport).toMatchObject({ width: 390, height: 844 });

    await expect(page.getByLabel("FO Contract Health")).toBeVisible();
    const healthGrid = await page.getByLabel("FO Contract Health").evaluate((el) => {
      const first = el.children.item(0)?.getBoundingClientRect();
      const second = el.children.item(1)?.getBoundingClientRect();
      return {
        width: el.getBoundingClientRect().width,
        firstTop: first?.top ?? null,
        secondTop: second?.top ?? null,
      };
    });
    expect(healthGrid.width).toBeLessThanOrEqual(390);
    expect(healthGrid.secondTop).toBeGreaterThan(healthGrid.firstTop ?? 0);

    const overflow = await page.evaluate(() => {
      const doc = document.documentElement;
      return {
        scrollWidth: doc.scrollWidth,
        clientWidth: doc.clientWidth,
        offenders: Array.from(document.querySelectorAll("body *"))
          .filter((node) => {
            const rect = (node as HTMLElement).getBoundingClientRect();
            return rect.right > doc.clientWidth + 1 || rect.left < -1;
          })
          .slice(0, 5)
          .map((node) => `${node.tagName.toLowerCase()}${node.id ? `#${node.id}` : ""}${node.className ? `.${String(node.className).split(/\s+/).slice(0, 3).join(".")}` : ""}`),
      };
    });
    expect(overflow.offenders, `horizontal overflow offenders: ${overflow.offenders.join(", ")}`).toEqual([]);
    expect(overflow.scrollWidth).toBeLessThanOrEqual(overflow.clientWidth + 1);

    // Mobile Bottom-Nav = Spine-Tabs + "Mehr" (Nav-Redesign 2026-06-11; der
    // Family-Organizer-Eintrag lebt seitdem im Mehr-Sheet, nicht als eigener Tab).
    const bottomNav = page.getByRole("navigation").filter({ has: page.getByRole("button", { name: "Mehr" }) });
    await expect(bottomNav).toBeVisible();
    await expect(bottomNav.getByRole("button", { name: "Start" })).toBeVisible();
    const navBox = await bottomNav.boundingBox();
    expect(navBox?.y ?? 0).toBeGreaterThan(700);

    await watch.assertClean();
  });
});
