import { expect, test, type Page } from "@playwright/test";

const STORAGE_KEY = "fo-backlog-view-v1";
const QUICK_VIEWS: Array<string | RegExp> = ["Commission-ready", /Grooming n.tig/, "Stale", "Ohne Owner", "Alle"];
const DRAWER_SECTIONS = ["Decision / Why now", "Acceptance Criteria", "Current Evidence / Last Proof", "Blockers"];

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

  test("renders live v2 data and supports commandable queue interactions", async ({ page }) => {
    const watch = watchPage(page);

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

    const compareStrip = page.getByLabel("Top-Kandidaten vergleichen");
    await expect(compareStrip).toBeVisible();
    const reasonChips = compareStrip.getByText(/Status now|Status next|L.uft|Hohes Risiko|Wichtiger Bereich|Lange offen|Kein Owner|Stale|Akzeptanz fehlt|Next Action fehlt|Grooming n.tig|Vertragsdrift/);
    expect(await reasonChips.count()).toBeGreaterThan(0);
    const commissionButtons = compareStrip.getByRole("button", { name: /N.chsten beauftragen|kopiert/ });
    expect(await commissionButtons.count()).toBeGreaterThan(0);

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

    const bottomNav = page.getByRole("navigation").filter({ has: page.getByRole("button", { name: "Family Organizer" }) });
    await expect(bottomNav).toBeVisible();
    const navBox = await bottomNav.boundingBox();
    expect(navBox?.y ?? 0).toBeGreaterThan(700);

    await watch.assertClean();
  });
});
