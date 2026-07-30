import { expect, test, type Page } from "@playwright/test";

/**
 * Fleet → Plan/Board/Ketten: Trefferbarkeit und Entscheidungsinhalte.
 *
 * Diese Suite prüft, was jsdom nicht prüfen kann: ob ein ECHTER Pointer-Klick
 * das Element trifft, das darunter liegt. Alle Regressionen hier waren
 * unsichtbar für die Komponententests, weil jsdom keinen Hit-Test kennt und
 * `element.click()` den Hit-Test ohnehin umgeht.
 *
 * Gemessen 2026-07-30 auf main dfeb9fe13d:
 *  - Unter lg (900x900) verdeckte der modale PlanSpec-Drawer (`fixed inset-0
 *    z-50`) die komplette Subtab-Leiste. Jeder Chip traf per
 *    document.elementFromPoint den Drawer; ein Pointer-Klick auf „Ketten" ließ
 *    aria-pressed auf Plan=true stehen.
 *  - Der Knopf „Als gehaltene Kette übergeben" stand nach der Vorprüfung bei
 *    y=1021 (Viewport 1000) — außerhalb des Viewports, elementFromPoint = null.
 *    Ein Klick konnte ihn nicht erreichen, also blieben UI und DB stumm.
 *
 * Diese Suite ist read-only: „Übergabe prüfen" ist eine Vorschau ohne
 * Schreibpfad, und der Übergabe-Knopf wird bewusst NUR vermessen, nie geklickt.
 */

/** Zentrum jedes Subtab-Chips samt Hit-Test gegen das oberste Element dort. */
async function chipProbe(page: Page) {
  return await page.evaluate(() => {
    const chips: { label: string; pressed: string | null; x: number; y: number; self: boolean; hit: string | null }[] = [];
    document.querySelectorAll<HTMLElement>(".fleet-subtabs button").forEach((button) => {
      const rect = button.getBoundingClientRect();
      const x = rect.left + rect.width / 2;
      const y = rect.top + rect.height / 2;
      const top = document.elementFromPoint(x, y);
      chips.push({
        label: (button.textContent || "").trim(),
        pressed: button.getAttribute("aria-pressed"),
        x: Math.round(x),
        y: Math.round(y),
        self: top === button || button.contains(top),
        hit: top ? `${top.tagName}.${String(top.className).slice(0, 48)}` : null,
      });
    });
    return chips;
  });
}

function chip(chips: Awaited<ReturnType<typeof chipProbe>>, label: string) {
  const found = chips.find((entry) => entry.label.startsWith(label));
  if (!found) throw new Error(`Subtab-Chip "${label}" nicht gefunden: ${chips.map((c) => c.label).join(", ")}`);
  return found;
}

/**
 * Nur produktrelevante Fehler. Der Vite-Dev-Server (Quellzweig-Verifikation,
 * s. README) liefert Schriftdateien unter `/@fs/…` mit 403 aus, weil sie
 * außerhalb der fs-allow-Liste liegen — ein Dev-Server-Artefakt, das im
 * gebauten Dashboard nicht existiert und nichts über den Slice aussagt.
 */
function watchPage(page: Page) {
  const errors: string[] = [];
  page.on("pageerror", (error) => errors.push(error.message));
  page.on("console", (message) => {
    if (message.type() !== "error") return;
    if (message.text().includes("403")) return;
    errors.push(message.text());
  });
  page.on("response", (response) => {
    if (response.status() < 400) return;
    if (response.url().includes("/@fs/")) return;
    errors.push(`${response.status()} ${response.request().method()} ${response.url()}`);
  });
  return errors;
}

async function openFleetPlan(page: Page) {
  await page.goto("/control/fleet");
  await expect(page.locator(".fleet-subtabs")).toBeVisible({ timeout: 20_000 });
  const plan = chip(await chipProbe(page), "Plan");
  await page.mouse.click(plan.x, plan.y);
  await expect(page.locator(".fleet-plan-row").first()).toBeVisible({ timeout: 25_000 });
}

test("desktop: Plan → Ketten → Board per echtem Pointer, auch mit offenem Detail", async ({ page }) => {
  const errors = watchPage(page);
  await page.setViewportSize({ width: 1440, height: 1000 });
  await openFleetPlan(page);

  await page.locator(".fleet-plan-row").first().click();
  await expect(page.locator(".fleet-plan-detail-scope").first()).toBeVisible({ timeout: 15_000 });

  // Ab lg lebt das Detail in der rechten TwoPane-Spalte — kein Overlay, jeder
  // Chip muss unter dem Pointer der Chip selbst bleiben.
  const withDetail = await chipProbe(page);
  for (const entry of withDetail) {
    expect(entry.self, `Chip "${entry.label}" wird von ${entry.hit} verdeckt`).toBe(true);
  }

  const ketten = chip(withDetail, "Ketten");
  await page.mouse.click(ketten.x, ketten.y);
  await expect
    .poll(async () => chip(await chipProbe(page), "Ketten").pressed, { timeout: 10_000 })
    .toBe("true");
  expect(chip(await chipProbe(page), "Plan").pressed).toBe("false");
  // Das alte Detail darf nicht neben dem neuen Tab stehen bleiben.
  await expect(page.locator(".fleet-plan-detail-scope")).toHaveCount(0);

  const board = chip(await chipProbe(page), "Board");
  await page.mouse.click(board.x, board.y);
  await expect
    .poll(async () => chip(await chipProbe(page), "Board").pressed, { timeout: 10_000 })
    .toBe("true");

  expect(errors, "Konsolen-/Netzfehler").toEqual([]);
});

/**
 * Unter lg ist das PlanSpec-Detail ein MODALER Drawer (DrawerShell portalt nach
 * document.body, `fixed inset-0 z-50`, Fokus-Falle, aria-modal). Er verdeckt
 * die Chip-Leiste vollständig — das ist Modal-Verhalten, kein Zufall, und per
 * z-index aus Fleet heraus nicht korrigierbar (die Fleet-Spalte wird im
 * Wurzelkontext bei z=2 gemalt, s. fleet.css). Dieser Test hält den Ist-Zustand
 * fest, damit die Grenze dokumentiert und messbar bleibt: verdeckt, aber über
 * Backdrop/Escape verlassbar, und danach schalten echte Pointer-Klicks sauber.
 */
test("tablet: modaler Drawer verdeckt die Chips, nach dem Schließen greifen sie wieder", async ({ page }) => {
  const errors = watchPage(page);
  await page.setViewportSize({ width: 900, height: 900 });
  await openFleetPlan(page);

  await page.locator(".fleet-plan-row").first().click();
  const dialog = page.locator('[role="dialog"][aria-modal="true"]');
  await expect(dialog).toHaveCount(1, { timeout: 15_000 });

  const covered = await chipProbe(page);
  expect(covered.every((entry) => !entry.self), "Chips sind unter lg vom Modal verdeckt").toBe(true);

  // Escape ist der dokumentierte Ausweg aus dem Modal.
  await page.keyboard.press("Escape");
  await expect(dialog).toHaveCount(0, { timeout: 10_000 });

  const free = await chipProbe(page);
  for (const entry of free) {
    expect(entry.self, `Chip "${entry.label}" nach dem Schließen von ${entry.hit} verdeckt`).toBe(true);
  }

  const ketten = chip(free, "Ketten");
  await page.mouse.click(ketten.x, ketten.y);
  await expect
    .poll(async () => chip(await chipProbe(page), "Ketten").pressed, { timeout: 10_000 })
    .toBe("true");

  const board = chip(await chipProbe(page), "Board");
  await page.mouse.click(board.x, board.y);
  await expect
    .poll(async () => chip(await chipProbe(page), "Board").pressed, { timeout: 10_000 })
    .toBe("true");

  expect(errors, "Konsolen-/Netzfehler").toEqual([]);
});

test("desktop: PlanSpec-Detail zeigt Ziel, echte Kriterien, Schritte und Scope", async ({ page }) => {
  const errors = watchPage(page);
  await page.setViewportSize({ width: 1440, height: 1000 });
  await openFleetPlan(page);

  // Ein Plan, dessen Kriterien ausschließlich pro Slice stehen (Canon-Normalfall).
  const row = page.locator(".fleet-plan-row", { hasText: "Landing-Loop" }).first();
  await expect(row).toBeVisible({ timeout: 20_000 });
  await row.click();

  const detail = page.locator(".fleet-plan-detail-scope").first();
  await expect(detail).toBeVisible({ timeout: 15_000 });
  // Überblick: Ziel + Kriterien statt „Keine Kriterien im Plan".
  await expect(detail.getByText("Keine Kriterien im Plan.")).toHaveCount(0);
  await expect(detail.locator(".fleet-plan-criteria > li").first()).toBeVisible();
  expect(await detail.locator(".fleet-plan-criteria > li").count()).toBeGreaterThan(1);

  // Ablauf: Schritte, Vorgänger und Arbeitsvertrag mit Kriterien + Scope-Pfaden.
  await detail.locator("#fleet-plan-tab-steps").click();
  await expect(detail.locator(".fleet-plan-step-rail > li").first()).toBeVisible();
  const contract = detail.locator(".fleet-plan-step-detail").first();
  await contract.locator("summary").click();
  await expect(contract.locator(".fleet-plan-step-criteria > li").first()).toBeVisible();

  expect(errors, "Konsolen-/Netzfehler").toEqual([]);
});

test("desktop: die Übergabe-Aktion bleibt nach der Vorprüfung im Viewport treffbar", async ({ page }) => {
  const errors = watchPage(page);
  await page.setViewportSize({ width: 1440, height: 1000 });
  await openFleetPlan(page);

  const row = page.locator(".fleet-plan-row", { hasText: "Landing-Loop" }).first();
  await expect(row).toBeVisible({ timeout: 20_000 });
  await row.click();

  const detail = page.locator(".fleet-plan-detail-scope").first();
  await detail.locator("#fleet-plan-tab-check").click();
  await detail.getByRole("button", { name: "Übergabe prüfen" }).click();

  const ingest = detail.locator(".fleet-plan-ingest");
  await expect(ingest).toBeVisible({ timeout: 20_000 });

  // Der entscheidende Messpunkt: liegt die primäre Aktion im Viewport und
  // trifft ein Pointer-Klick sie auch? Geklickt wird bewusst NICHT — das wäre
  // eine Live-Mutation auf dem default-Board.
  const probe = await page.evaluate(() => {
    const button = document.querySelector<HTMLElement>(".fleet-plan-ingest");
    if (!button) return null;
    const rect = button.getBoundingClientRect();
    const x = rect.left + rect.width / 2;
    const y = rect.top + rect.height / 2;
    const top = document.elementFromPoint(x, y);
    return {
      disabled: (button as HTMLButtonElement).disabled,
      insideViewport: rect.top >= 0 && rect.bottom <= window.innerHeight,
      self: top === button || button.contains(top),
      hit: top ? `${top.tagName}.${String(top.className).slice(0, 48)}` : null,
    };
  });
  expect(probe).not.toBeNull();
  expect(probe!.disabled).toBe(false);
  expect(probe!.insideViewport, "Übergabe-Knopf liegt außerhalb des Viewports").toBe(true);
  expect(probe!.self, `Übergabe-Knopf wird von ${probe!.hit} verdeckt`).toBe(true);

  expect(errors, "Konsolen-/Netzfehler").toEqual([]);
});
