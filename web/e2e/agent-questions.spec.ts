import { expect, test, type Page, type Route } from "@playwright/test";

// Frage-Assistent Antwort-Sheet (P0c) — Klick-Regression, server-agnostisch:
// Deterministischer Seed über page.route-Mocks (kein Scrape/DB/tmux). Läuft
// identisch gegen live :9119 und gegen `vite preview` (ohne Backend), weil
// Catch-all + spezifische API-Routen die SPA vollständig hydrieren.
// Contract: Pill → Sheet → Option-POST → nächste Frage → Leerzustand;
// Doppelklick = 1 POST; 409 → „Frage hat sich geändert"; Tastatur 1/Esc.

type Option = { nr: number | string; label: string; recommended: boolean };

type QuestionEvent = {
  id: number;
  ts: string;
  updated_ts: string | null;
  source: string;
  session: string;
  window: string;
  pane_id: string;
  fingerprint: string;
  kind: string | null;
  cwd: string | null;
  question_text: string;
  options: Option[];
  class: string | null;
  status: string;
  answered_by: string | null;
  answer: string | null;
  latency_s: number | null;
  answer_verified: boolean | null;
  override: number;
  action_context?: string | null;
  hook_key?: string | null;
};

function eventFixture(id: number, text: string, options: Option[]): QuestionEvent {
  return {
    id,
    ts: "2026-07-17T10:00:00Z",
    updated_ts: null,
    source: "scrape",
    session: "hermes-e2e",
    window: String(id % 10),
    pane_id: `%${id}`,
    fingerprint: `fp-${id}`,
    kind: id === 102 ? null : "claude",
    cwd: "/home/piet/.hermes/hermes-agent",
    question_text: text,
    options,
    class: null,
    status: "open",
    answered_by: null,
    answer: null,
    latency_s: null,
    answer_verified: null,
    override: 0,
  };
}

const Q101 = eventFixture(101, "E2E Frage A: Branch mergen?", [
  { nr: 1, label: "Ja, mergen", recommended: true },
  { nr: 2, label: "Nein, warten", recommended: false },
]);

const Q102 = eventFixture(102, "E2E Frage B: Continue? (y/n)", [
  { nr: "y", label: "Yes", recommended: true },
  { nr: "n", label: "No", recommended: false },
]);

// I2 Test D: hook-sourced event — labels after hook-script convention
// (no "(Recommended)" suffix; recommended flag; nr 1..3). Real CC payload.
const Q_HOOK: QuestionEvent = {
  ...eventFixture(201, "Which deployment strategy should we use?", [
    { nr: 1, label: "Rolling update", recommended: true },
    { nr: 2, label: "Blue-green", recommended: false },
    { nr: 3, label: "Canary", recommended: false },
  ]),
  source: "hook",
  kind: "claude",
  action_context: "AskUserQuestion: Strategy",
  pane_id: "%80",
  fingerprint: "hook:e2e-fp-201",
};

async function installBaseMocks(page: Page) {
  // Catch-all FIRST — Playwright prefers later-registered routes.
  // Default empty object is NOT safe for every consumer (arrays use .filter/.some),
  // so bootstrap + terminals payloads below override with real shapes.
  await page.route("**/api/**", async (route: Route) => {
    await route.fulfill({ json: {} });
  });

  // App shell bootstrap (App.tsx / ProfileProvider / ThemeProvider / usePlugins).
  // Measured crash without these: `manifests.some is not a function` when plugins=`{}`.
  await page.route("**/api/dashboard/plugins", async (route) => {
    await route.fulfill({ json: [] });
  });
  await page.route("**/api/dashboard/themes", async (route) => {
    await route.fulfill({
      json: {
        active: "default",
        themes: [{ name: "default", label: "Hermes Teal", description: "e2e" }],
      },
    });
  });
  await page.route("**/api/dashboard/font", async (route) => {
    await route.fulfill({ json: { font: "default" } });
  });
  await page.route("**/api/profiles", async (route) => {
    await route.fulfill({ json: { profiles: [{ name: "default" }] } });
  });
  await page.route("**/api/profiles/active", async (route) => {
    await route.fulfill({ json: { current: "default", active: "default" } });
  });
  await page.route("**/api/status", async (route) => {
    await route.fulfill({
      json: { version: "0.0.0-e2e", gateway_running: false, gateway_state: "stopped" },
    });
  });
  await page.route("**/api/config", async (route) => {
    await route.fulfill({ json: { dashboard: {} } });
  });

  // AgentTerminalsView read-only context — getSkills/getToolsets must be arrays
  // (fulfilled `{}` from catch-all → skills.filter crash → "ANSICHT ABGESTÜRZT").
  await page.route("**/api/skills**", async (route) => {
    await route.fulfill({ json: [] });
  });
  await page.route("**/api/tools/toolsets**", async (route) => {
    await route.fulfill({ json: [] });
  });
  // Paths match api.getControlOverview* helpers (not a /control/overview/* prefix).
  await page.route("**/api/health-status**", async (route) => {
    await route.fulfill({ json: { overall: "ok", subsystems: {} } });
  });
  await page.route("**/api/vault/provenance**", async (route) => {
    await route.fulfill({ json: { open_sessions: [], recent_receipts: [] } });
  });
  await page.route("**/api/plugins/kanban/board**", async (route) => {
    await route.fulfill({ json: { columns: [] } });
  });
  await page.route("**/api/plugins/kanban/decision-queue**", async (route) => {
    await route.fulfill({ json: { count: 0, decisions: [] } });
  });

  // Minimal payloads so AgentTerminalsView can mount without crashing.
  await page.route("**/api/agent-terminals/capabilities", async (route) => {
    await route.fulfill({
      json: {
        tmux_available: false,
        hermes_tui_available: false,
        hermes_binary: null,
        reason: "e2e-mock",
        agents: {},
        workdirs: [],
      },
    });
  });
  await page.route("**/api/agent-terminals/sessions", async (route) => {
    await route.fulfill({ json: { sessions: [] } });
  });
  await page.route("**/api/agent-terminals/windows**", async (route) => {
    await route.fulfill({ json: { windows: [] } });
  });
  await page.route("**/api/agent-terminals/overview", async (route) => {
    await route.fulfill({ json: { now: Date.now() / 1000, windows: [] } });
  });
  await page.route("**/api/account-usage**", async (route) => {
    await route.fulfill({ json: { cache_ttl_seconds: 60, providers: [] } });
  });
}

type QuestionMocks = {
  questions: QuestionEvent[];
  posts: Array<{ id: number; body: Record<string, unknown> }>;
  postCounts: Record<number, number>;
  delayMs: number;
  always409Ids: Set<number>;
};

async function installQuestionMocks(
  page: Page,
  seed: QuestionEvent[] = [Q101, Q102],
): Promise<QuestionMocks> {
  const state: QuestionMocks = {
    questions: [...seed],
    posts: [],
    postCounts: {},
    delayMs: 0,
    always409Ids: new Set(),
  };

  await page.route("**/api/agent-questions**", async (route: Route) => {
    const req = route.request();
    if (req.method() === "GET") {
      await route.fulfill({ json: { questions: state.questions } });
      return;
    }
    await route.fallback();
  });

  await page.route("**/api/agent-questions/*/answer", async (route: Route) => {
    const req = route.request();
    if (req.method() !== "POST") {
      await route.fallback();
      return;
    }
    const url = req.url();
    const m = url.match(/\/api\/agent-questions\/(\d+)\/answer/);
    const id = m ? Number(m[1]) : -1;
    const body = (req.postDataJSON() as Record<string, unknown>) ?? {};
    state.posts.push({ id, body });
    state.postCounts[id] = (state.postCounts[id] ?? 0) + 1;

    if (state.delayMs > 0) {
      await new Promise((r) => setTimeout(r, state.delayMs));
    }

    if (state.always409Ids.has(id)) {
      await route.fulfill({
        status: 409,
        contentType: "application/json",
        body: JSON.stringify({ detail: { ok: false, reason: "superseded" } }),
      });
      return;
    }

    const stillOpen = state.questions.some((q) => q.id === id);
    if (!stillOpen) {
      await route.fulfill({
        status: 409,
        contentType: "application/json",
        body: JSON.stringify({ detail: { ok: false, reason: "superseded" } }),
      });
      return;
    }

    if (state.postCounts[id] === 1) {
      state.questions = state.questions.filter((q) => q.id !== id);
      await route.fulfill({
        json: { ok: true, verified: true, latency_s: 12.3 },
      });
      return;
    }

    await route.fulfill({
      status: 409,
      contentType: "application/json",
      body: JSON.stringify({ detail: { ok: false, reason: "superseded" } }),
    });
  });

  return state;
}

async function gotoAgentTerminals(page: Page) {
  // vite preview may not SPA-fallback deep paths — try direct, else client nav.
  const response = await page.goto("/control/agent-terminals", { waitUntil: "domcontentloaded" });
  if (response && response.status() >= 400) {
    await page.goto("/", { waitUntil: "domcontentloaded" });
    await page.evaluate(() => {
      window.history.pushState({}, "", "/control/agent-terminals");
      window.dispatchEvent(new PopStateEvent("popstate"));
    });
    // Client routers often need a click on the nav link if history alone fails.
    const stillMissing = await page.getByTestId("frage-pill").count().catch(() => 0);
    if (stillMissing === 0) {
      await page.goto("/control/", { waitUntil: "domcontentloaded" });
      const link = page.locator('a[href*="agent-terminals"]').first();
      if (await link.count()) {
        await link.click();
      } else {
        await page.evaluate(() => {
          window.location.hash = "";
          window.history.replaceState({}, "", "/control/agent-terminals");
          window.location.reload();
        });
      }
    }
  }
  await expect(page.getByTestId("frage-pill").first()).toBeVisible({ timeout: 20_000 });
}

test.describe("Frage-Assistent Antwort-Sheet", () => {
  test("A: Pill → Sheet → Klick → POST → nächste Frage → Leerzustand", async ({ page }) => {
    await installBaseMocks(page);
    const mocks = await installQuestionMocks(page);
    await gotoAgentTerminals(page);

    const pill = page.getByTestId("frage-pill").first();
    await expect(pill).toContainText("2 Fragen");
    await pill.click();

    const sheet = page.getByTestId("answer-sheet");
    await expect(sheet).toBeVisible();
    await expect(sheet.getByText("E2E Frage A: Branch mergen?")).toBeVisible();
    await expect(sheet.getByText("Empfohlen")).toBeVisible();

    const postPromise = page.waitForRequest(
      (r) =>
        r.method() === "POST" &&
        r.url().includes("/api/agent-questions/101/answer"),
    );
    await sheet.getByRole("button", { name: /Ja, mergen/ }).click();
    const post = await postPromise;
    expect(post.postDataJSON()).toEqual({ answer: "1", answered_by: "operator" });

    await expect(sheet.getByText("E2E Frage B: Continue? (y/n)")).toBeVisible({ timeout: 10_000 });
    await expect(page.getByTestId("frage-pill").first()).toContainText("1 Frage");

    // Accessible name is "y Yes Empfohlen" (nr + label + badge) — match on label text.
    await sheet.getByRole("button", { name: /Yes/ }).click();
    await expect(
      sheet.getByRole("heading", { name: "Keine offenen Fragen" }),
    ).toBeVisible({ timeout: 10_000 });
    await expect(page.getByTestId("frage-pill")).toHaveCount(0);

    expect(mocks.posts.map((p) => p.id)).toEqual([101, 102]);
  });

  test("B: Doppelklick → genau 1 POST; 409 zeigt Aktualisieren", async ({ page }) => {
    await installBaseMocks(page);
    const mocks = await installQuestionMocks(page, [Q101, Q102]);
    mocks.delayMs = 300;
    await gotoAgentTerminals(page);

    await page.getByTestId("frage-pill").first().click();
    const sheet = page.getByTestId("answer-sheet");
    await expect(sheet).toBeVisible();

    const opt1 = sheet.getByRole("button", { name: /Ja, mergen/ });
    await opt1.click();
    await opt1.click({ force: true }).catch(() => {});
    // Second click may no-op if disabled; wait for first POST to settle.
    await expect.poll(() => mocks.postCounts[101] ?? 0).toBe(1);
    await expect(sheet.getByText("E2E Frage B: Continue? (y/n)")).toBeVisible({ timeout: 10_000 });

    // Force 409 on the remaining question.
    mocks.always409Ids.add(102);
    await sheet.getByRole("button", { name: /Yes/ }).click();
    await expect(sheet.getByText("Frage hat sich geändert")).toBeVisible({ timeout: 10_000 });
    await expect(sheet.getByRole("button", { name: "Aktualisieren" })).toBeVisible();
  });

  test("C: Tastatur 1 sendet POST; Esc schließt Sheet", async ({ page }) => {
    await installBaseMocks(page);
    const mocks = await installQuestionMocks(page, [Q101]);
    await gotoAgentTerminals(page);

    await page.getByTestId("frage-pill").first().click();
    const sheet = page.getByTestId("answer-sheet");
    await expect(sheet).toBeVisible();

    await page.keyboard.press("1");
    await expect.poll(() => mocks.posts.length).toBe(1);
    expect(mocks.posts[0]).toMatchObject({
      id: 101,
      body: { answer: "1", answered_by: "operator" },
    });
    // Close is intentionally disabled WHILE a POST is in flight (Codex-Lens
    // I1 #2) — wait for the answered state before pressing Esc.
    await expect(
      sheet.getByRole("heading", { name: "Keine offenen Fragen" }),
    ).toBeVisible({ timeout: 10_000 });

    // After answer, empty state is open — close via Esc and re-open would need
    // a new question. Seed a fresh open question via mock + reload path:
    mocks.questions = [Q102];
    // Open again by simulating pill (count will refresh on next poll — force
    // navigation re-mount for deterministic open).
    await page.keyboard.press("Escape");
    await expect(sheet).toHaveCount(0);

    // Re-open: poll should pick up Q102; click pill when visible.
    await expect(page.getByTestId("frage-pill").first()).toBeVisible({ timeout: 15_000 });
    await page.getByTestId("frage-pill").first().click();
    await expect(page.getByTestId("answer-sheet")).toBeVisible();
    await page.keyboard.press("Escape");
    await expect(page.getByTestId("answer-sheet")).toHaveCount(0);
    // View still intact — pill remains.
    await expect(page.getByTestId("frage-pill").first()).toBeVisible();
  });

  test("D: hook-sourced event shows exact option labels + Empfohlen badge", async ({
    page,
  }) => {
    await installBaseMocks(page);
    const mocks = await installQuestionMocks(page, [Q_HOOK]);
    await gotoAgentTerminals(page);

    const pill = page.getByTestId("frage-pill").first();
    await expect(pill).toContainText("1 Frage");
    await pill.click();

    const sheet = page.getByTestId("answer-sheet");
    await expect(sheet).toBeVisible();
    await expect(
      sheet.getByText("Which deployment strategy should we use?"),
    ).toBeVisible();

    // Three exact labels from the real PreToolUse payload after hook convention
    // (no "(Recommended)" suffix on the label text).
    await expect(sheet.getByRole("button", { name: /Rolling update/ })).toBeVisible();
    await expect(sheet.getByRole("button", { name: /Blue-green/ })).toBeVisible();
    await expect(sheet.getByRole("button", { name: /Canary/ })).toBeVisible();
    await expect(sheet.getByText("(Recommended)")).toHaveCount(0);

    // Exactly one Empfohlen badge (on option 1).
    await expect(sheet.getByText("Empfohlen")).toHaveCount(1);

    const postPromise = page.waitForRequest(
      (r) =>
        r.method() === "POST" &&
        r.url().includes("/api/agent-questions/201/answer"),
    );
    await sheet.getByRole("button", { name: /Rolling update/ }).click();
    const post = await postPromise;
    expect(post.postDataJSON()).toEqual({ answer: "1", answered_by: "operator" });
    expect(mocks.posts.map((p) => p.id)).toEqual([201]);
  });

  test("E: deep-link ?question=<id> opens AnswerSheet on that event (I3)", async ({
    page,
  }) => {
    await installBaseMocks(page);
    await installQuestionMocks(page, [Q101, Q102]);

    // Direct navigation with query param (push URL used by web-push).
    const response = await page.goto("/control/agent-terminals?question=102", {
      waitUntil: "domcontentloaded",
    });
    if (response && response.status() >= 400) {
      await page.goto("/", { waitUntil: "domcontentloaded" });
      await page.evaluate(() => {
        window.history.pushState({}, "", "/control/agent-terminals?question=102");
        window.dispatchEvent(new PopStateEvent("popstate"));
      });
    }

    const sheet = page.getByTestId("answer-sheet");
    await expect(sheet).toBeVisible({ timeout: 20_000 });
    // Focused id=102 is head even though list is newest-first (101, 102).
    await expect(sheet.getByText("E2E Frage B: Continue? (y/n)")).toBeVisible();
    // Param consumed so reload does not re-open.
    await expect
      .poll(() => new URL(page.url()).searchParams.get("question"))
      .toBeNull();
  });
});
