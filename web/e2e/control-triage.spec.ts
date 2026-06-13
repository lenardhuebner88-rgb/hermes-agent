import { expect, test, type Page, type Route } from "@playwright/test";

// Fehler-Triage Button-Contract (Operator-Befund 2026-06-12, t_748896f7):
// „Nochmal"/„Nochmal stärker" wirkten wie tote Buttons, weil der PATCH zwar
// griff, die Karte aber unverändert „blocked" stehen blieb. Diese Suite
// fixiert den vollen UI-Contract gegen die ECHTE SPA (live :9119), mit
// gemockter Failures-/Lanes-/PATCH-Schicht (page.route), damit der Test
// deterministisch ist und das Produktions-Board nicht anfasst:
//   1. blocked-Karte → beide Buttons sichtbar, Zwei-Schritt-Confirm.
//   2. „Nochmal" → PATCH {status:"ready"} + Erfolgsmeldung + Requeue-Plakette,
//      der sinnlose Retry-Button verschwindet, Eskalieren bleibt.
//   3. „Nochmal stärker" → PATCH {model_override:"claude-opus-4-8"}.

const TASK_ID = "t_e2e_triage";
const FAILURES_API = "**/api/plugins/kanban/runs/failures*";
const LANES_API = "**/api/plugins/kanban/lanes";
const TASK_API = `**/api/plugins/kanban/tasks/${TASK_ID}`;

type FailureFixture = {
  task_status: string;
  model_override: string | null;
};

function failuresPayload(state: FailureFixture) {
  return {
    hours: 48,
    now: 1781300000,
    count: 1,
    truncated: false,
    failures: [
      {
        run_id: 9001,
        task_id: TASK_ID,
        title: "E2E: Verifier-Gate Fixture-Karte",
        profile: "coder",
        assignee: "coder",
        outcome: "blocked",
        reason: "REQUEST_CHANGES — E2E-Fixture (kein echter Task)",
        ended_at: 1781299000,
        task_status: state.task_status,
        model_override: state.model_override,
      },
    ],
  };
}

const lanesPayload = {
  active_id: "lane_e2e",
  lanes: [
    {
      id: "lane_e2e",
      active: true,
      profiles: {
        coder: { worker_runtime: "claude-cli", kanban_spawn_health: { status: "healthy" } },
        premium: { worker_runtime: "claude-cli", kanban_spawn_health: { status: "healthy" } },
      },
    },
  ],
  profiles: [
    { name: "coder", worker_runtime: "claude-cli", kanban_spawn_health: { status: "healthy" } },
    { name: "premium", worker_runtime: "claude-cli", kanban_spawn_health: { status: "healthy" } },
  ],
};

async function installTriageMocks(page: Page) {
  const state: FailureFixture = { task_status: "blocked", model_override: null };
  const patches: Array<Record<string, unknown>> = [];

  await page.route(FAILURES_API, async (route: Route) => {
    await route.fulfill({ json: failuresPayload(state) });
  });
  await page.route(LANES_API, async (route: Route) => {
    await route.fulfill({ json: lanesPayload });
  });
  await page.route(TASK_API, async (route: Route) => {
    const request = route.request();
    if (request.method() !== "PATCH") {
      await route.fallback();
      return;
    }
    const body = request.postDataJSON() as Record<string, unknown>;
    patches.push(body);
    if (body.status === "ready") state.task_status = "ready";
    if (typeof body.model_override === "string") state.model_override = body.model_override;
    await route.fulfill({ json: { ok: true, task_id: TASK_ID } });
  });

  return { state, patches };
}

async function gotoFlowTriage(page: Page) {
  await page.goto("/control/flow");
  await expect(page.getByText("E2E: Verifier-Gate Fixture-Karte")).toBeVisible({ timeout: 15_000 });
}

test.describe("Fehler-Triage Buttons (Flow-Tab)", () => {
  test("blocked-Karte zeigt beide Aktionen mit Zwei-Schritt-Confirm", async ({ page }) => {
    await installTriageMocks(page);
    await gotoFlowTriage(page);

    const retry = page.getByRole("button", { name: "Nochmal", exact: true });
    const escalate = page.getByRole("button", { name: "Nochmal stärker", exact: true });
    await expect(retry).toBeVisible();
    await expect(escalate).toBeVisible();

    // Erster Klick armiert nur (Zwei-Schritt-Confirm), es geht KEIN PATCH raus.
    await retry.click();
    await expect(page.getByRole("button", { name: /Nochmal · Bestätigen/ })).toBeVisible();
    await expect(page.getByRole("button", { name: "Abbrechen" })).toBeVisible();
    await expect(page.getByText("stellt den Task wieder ready (gleiche Lane)")).toBeVisible();
    await page.getByRole("button", { name: "Abbrechen" }).click();
    await expect(retry).toBeVisible();
  });

  test("Nochmal → PATCH status:ready, Requeue-Plakette erscheint, Retry-Button entfällt", async ({ page }) => {
    const mocks = await installTriageMocks(page);
    await gotoFlowTriage(page);

    await page.getByRole("button", { name: "Nochmal", exact: true }).click();
    await page.getByRole("button", { name: /Nochmal · Bestätigen/ }).click();

    // Der Klick muss als echter PATCH-Body ankommen — das war der Kern des
    // „toter Button"-Befunds: wirkt, aber unsichtbar.
    await expect.poll(() => mocks.patches).toEqual([{ status: "ready" }]);
    await expect(page.getByText(`${TASK_ID} wieder eingereiht.`)).toBeVisible();

    // Nach dem Refetch spiegelt die Karte den LIVE-Task-Status:
    await expect(page.getByText("wieder eingereiht — wartet auf Dispatcher-Tick + freie Lane")).toBeVisible();
    await expect(page.getByRole("button", { name: "Nochmal", exact: true })).toHaveCount(0);
    await expect(page.getByRole("button", { name: "Nochmal stärker", exact: true })).toBeVisible();
    await expect(page.getByText("eskaliert die schon eingereihte Karte, bevor der Dispatcher sie zieht")).toBeVisible();
  });

  test("Nochmal stärker → PATCH model_override auf die Premium-Eskalation", async ({ page }) => {
    const mocks = await installTriageMocks(page);
    await gotoFlowTriage(page);

    await page.getByRole("button", { name: "Nochmal stärker", exact: true }).click();
    await page.getByRole("button", { name: /Nochmal stärker · Bestätigen/ }).click();

    // Eskalation = model_override-PATCH + Requeue-PATCH (escalationPatchSequence).
    await expect.poll(() => mocks.patches).toEqual([
      { model_override: "claude-opus-4-8" },
      { status: "ready" },
    ]);
    await expect(page.getByText(`${TASK_ID} eskaliert auf claude-opus-4-8 und wieder eingereiht.`)).toBeVisible();
  });
});
