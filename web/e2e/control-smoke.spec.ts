import { expect, test, type Page } from "@playwright/test";

// Control-Shell Smoke (W2-c rewrite): die vorherige Fassung prüfte den
// gelöschten Legacy-Shell ("Hermes Control"-Header, /^Flow/-Button,
// eigenständige Pressure-/Ops-Routen) und war deshalb bereits auf HEAD rot —
// Wave 2 tauschte Shell + Nav-Ökonomie komplett aus (S1-Fusion legte
// Pressure/Ops in /control/system zusammen, Flow/Ketten leben jetzt in
// Fleet). Hier ersetzt gegen die AKTUELLE Rail/Bottombar/Puls-Leiste-
// Kontraktfläche (SHELL-SPEC.md W2-a/W2-b/W2-c) — bewusst als Smoke-Schicht:
// tiefe Pro-View-Assertions (Statistik-Ledger, Ops-Radar-Hebel, Agent-
// Terminals-Terminate …) haben ihre eigene Komponenten-Abdeckung
// (StatistikView.test.tsx, OpsRadarContent.test.tsx,
// AgentTerminalsView.render.test.tsx) und werden hier nicht dupliziert.

type PageWatch = {
  consoleErrors: string[];
  failedRequests: string[];
  assertClean: () => void;
};

function watchPage(page: Page): PageWatch {
  const consoleErrors: string[] = [];
  const failedRequests: string[] = [];
  page.on("console", (message) => {
    if (message.type() === "error") {
      const location = message.location();
      const where = location.url ? ` @ ${location.url}:${location.lineNumber}` : "";
      consoleErrors.push(`${message.text()}${where}`);
    }
  });
  page.on("requestfailed", (request) => {
    const errorText = request.failure()?.errorText ?? "";
    if (errorText === "net::ERR_ABORTED") return;
    failedRequests.push(`${request.method()} ${request.url()} ${errorText}`);
  });
  page.on("pageerror", (error) => {
    consoleErrors.push(error.stack ?? error.message);
  });
  page.on("response", (response) => {
    if (response.status() >= 400) {
      failedRequests.push(`${response.status()} ${response.request().method()} ${response.url()} (${response.request().resourceType()})`);
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

// Die 5 Primaries sind identisch auf Rail (>=tab) und Bottom-Bar (<tab) —
// muskelgedächtnis-gleiche Nav-Ökonomie, s. ControlShell.tsx `tabs`.
const PRIMARIES = ["Fleet", "Start", "Terminals", "Statistik", "Bibliothek"];

test.describe("Control Shell Smoke", () => {
  test("Rail: Hauptnavigation-Landmark trägt die 5 Primaries ab 600px", async ({ page }) => {
    await page.setViewportSize({ width: 820, height: 1180 });
    const watch = watchPage(page);
    await page.goto("/control/crons");

    const rail = page.getByRole("navigation", { name: "Hauptnavigation" });
    await expect(rail).toBeVisible({ timeout: 15_000 });
    for (const label of PRIMARIES) {
      await expect(rail.getByRole("button", { name: label })).toBeVisible();
    }
    watch.assertClean();
  });

  test("Bottom-Bar trägt die 5 Primaries bei 390px (Compact)", async ({ page }) => {
    await page.setViewportSize({ width: 390, height: 844 });
    const watch = watchPage(page);
    await page.goto("/control");

    const bottomBar = page.getByRole("navigation", { name: "Navigation" });
    await expect(bottomBar).toBeVisible({ timeout: 15_000 });
    for (const label of PRIMARIES) {
      await expect(bottomBar.getByRole("button", { name: label })).toBeVisible();
    }
    watch.assertClean();
  });

  test("Masthead zeigt das Routen-Label für eine View ohne eigenes Masthead (Crons)", async ({ page }) => {
    await page.setViewportSize({ width: 820, height: 1180 });
    const watch = watchPage(page);
    await page.goto("/control/crons");

    const masthead = page.getByTestId("control-masthead");
    await expect(masthead).toBeVisible({ timeout: 15_000 });
    await expect(masthead.getByText("Crons")).toBeVisible();
    watch.assertClean();
  });

  test("Rail-'Mehr'-Flyout öffnet und listet Loops", async ({ page }) => {
    await page.setViewportSize({ width: 820, height: 1180 });
    const watch = watchPage(page);
    await page.goto("/control/crons");

    await page.getByRole("button", { name: "Mehr" }).click();
    const flyout = page.getByTestId("rail-more-flyout");
    await expect(flyout).toBeVisible();
    await expect(flyout.getByRole("link", { name: "Loops" })).toBeVisible();
    watch.assertClean();
  });
});
