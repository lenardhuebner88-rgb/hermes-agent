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

async function mockControlApis(page: Page) {
  await page.addInitScript(() => {
    const NativeWebSocket = window.WebSocket;
    class MockKanbanEventsSocket extends EventTarget {
      static readonly CONNECTING = 0;
      static readonly OPEN = 1;
      static readonly CLOSING = 2;
      static readonly CLOSED = 3;
      readonly CONNECTING = 0;
      readonly OPEN = 1;
      readonly CLOSING = 2;
      readonly CLOSED = 3;
      readonly binaryType = "blob";
      readonly bufferedAmount = 0;
      readonly extensions = "";
      readonly protocol = "";
      readonly url: string;
      readyState = MockKanbanEventsSocket.CONNECTING;
      onopen: ((event: Event) => void) | null = null;
      onmessage: ((event: MessageEvent) => void) | null = null;
      onerror: ((event: Event) => void) | null = null;
      onclose: ((event: CloseEvent) => void) | null = null;

      constructor(url: string | URL) {
        super();
        this.url = String(url);
        window.setTimeout(() => {
          this.readyState = MockKanbanEventsSocket.OPEN;
          const event = new Event("open");
          this.onopen?.(event);
          this.dispatchEvent(event);
          const message = new MessageEvent("message", { data: JSON.stringify({ cursor: 0, events: [] }) });
          this.onmessage?.(message);
          this.dispatchEvent(message);
        }, 0);
      }
      close() {
        if (this.readyState === MockKanbanEventsSocket.CLOSED) return;
        this.readyState = MockKanbanEventsSocket.CLOSED;
        const event = new CloseEvent("close");
        this.onclose?.(event);
        this.dispatchEvent(event);
      }
      send() {}
    }
    window.WebSocket = new Proxy(NativeWebSocket, {
      construct(target, args) {
        const [url] = args;
        if (String(url).includes("/api/plugins/kanban/events")) {
          return new MockKanbanEventsSocket(url as string | URL);
        }
        return Reflect.construct(target, args);
      },
    });
  });
  await page.route("**/*.{woff,woff2}", async (route) => {
    await route.fulfill({ status: 200, contentType: "font/woff2", body: "" });
  });
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
    } : path.includes("/runs/windowed-rollup") ? {
      schema: "kanban-windowed-rollup-v1",
      since_hours: 168,
      now: 1782230000,
      completed_roots: 1,
      roots: [{
        id: "t_mother",
        title: "Stats Mother Ledger",
        status: "done",
        assignee: "coder",
        created_at: 1782220000,
        started_at: 1782220100,
        completed_at: 1782220900,
        ended_at: 1782220900,
        providers: ["openrouter", "anthropic"],
        cost_usd: 0.03760227,
        cost_usd_equivalent: 0.953664,
        cost_effective_usd: 0.99126627,
        billing_mode: "metered+subscription_included",
        neuralwatt: null,
        runtime_seconds: 800,
        workers: [{
          profile: "coder",
          input_tokens: 81750,
          output_tokens: 2226,
          cost_usd: 0.03760227,
          actual_cost_usd: 0.03760227,
          cost_usd_equivalent: 0,
          api_equivalent_usd: 0.03760227,
          cost_effective_usd: 0.03760227,
          billing_neuralwatt_kwh: 0,
          billing_neuralwatt_cost_usd: 0,
          run_count: 1,
          provider: "openrouter",
          model: "deepseek-v4-pro",
        }, {
          profile: "verifier",
          input_tokens: 131747,
          output_tokens: 4793,
          cost_usd: 0,
          actual_cost_usd: 0,
          cost_usd_equivalent: 0.953664,
          api_equivalent_usd: 0.953664,
          cost_effective_usd: 0.953664,
          billing_neuralwatt_kwh: 0,
          billing_neuralwatt_cost_usd: 0,
          run_count: 1,
          provider: "anthropic",
          model: "claude-opus-4-8",
        }],
        runners: [{
          id: 837,
          task_id: "t_mother",
          profile: "coder",
          provider: "openrouter",
          model: "deepseek-v4-pro",
          input_tokens: 81750,
          output_tokens: 2226,
          cost_usd: 0.03760227,
          cost_usd_equivalent: 0,
          cost_effective_usd: 0.03760227,
          billing_mode: "metered",
          neuralwatt: null,
          started_at: 1782220100,
          ended_at: 1782220400,
          runtime_seconds: 300,
        }, {
          id: 4828,
          task_id: "t_mother",
          profile: "verifier",
          provider: "anthropic",
          model: "claude-opus-4-8",
          input_tokens: 131747,
          output_tokens: 4793,
          cost_usd: 0,
          cost_usd_equivalent: 0.953664,
          cost_effective_usd: 0.953664,
          billing_mode: "subscription_included",
          neuralwatt: null,
          started_at: 1782220500,
          ended_at: 1782220900,
          runtime_seconds: 400,
        }],
      }],
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
      schema: "hermes-metrics-lite-v1",
      checked_at: Math.floor(Date.now() / 1000),
      uptime_seconds: 60,
      groups: {},
    } : path === "/api/pressure-status" ? {
      schema: "hermes-pressure-v1",
      checked_at: Math.floor(Date.now() / 1000),
      overall: "busy",
      cause: "Ungedrosselte Testprozesse laufen im gleichen Sitzungsbereich",
      recommendation: { label: "Tests laufen", detail: "2 Testprozesse aktiv.", tone: "amber" },
      host: { cpu_percent: 36, load_avg: [5.4, 4.8, 3.1], cpu_count: 12, memory_percent: 62 },
      dashboard: { pid: 4242, rss_mb: 188, cpu_percent: 5, cpu_weight: 100, cpu_quota: "max", tasks_current: 24 },
      pressure_sources: [{ kind: "test", label: "pytest", count: 2, cpu_percent: 190, rss_mb: 810, scope: "user-session", throttled: false }],
      access: { tailnet: "direct", api_latency_ms: 128, detail: "tailnet direct" },
      token_pressure: { class: "unknown", pct: null, updated_at: null },
      errors: [],
    } : path === "/api/operator-inventory" ? {
      schema: "hermes-operator-inventory-v1",
      checked_at: Math.floor(Date.now() / 1000),
      summary: { worktrees_total: 86, worktrees_locked: 60, worktrees_dirty: 2, worktrees_prunable: 0, worktrees_orphaned: 1, worktrees_status_unknown: 0, actors_total: 4, actors_canonical: 1 },
      next_lever: { action: "inspect_dirty_worktrees", label: "Dirty Worktrees", detail: "2 Worktrees haben echte Git-Aenderungen.", tone: "amber", count: 2, target: "/control/ops?filter=dirty", mutation: "none" },
      levers: [
        { action: "inspect_dirty_worktrees", label: "Dirty Worktrees", detail: "2 Worktrees haben echte Git-Aenderungen.", tone: "amber", count: 2, target: "/control/ops?filter=dirty", mutation: "none" },
      ],
      worktrees: [
        { id: "kanban:t_123", path_label: "kanban:t_123", branch: "kanban/t_123", head: "def456", relation: "kanban", task_hint: "t_123", state: "dirty", locked: true, prunable: false, detached: false, dirty_count: 3, untracked_count: 1, status_checked: true, orphaned: true },
      ],
      actors: [
        { role: "kanban_worker", label: "Kanban Worker", count: 1, cpu_percent: 0, rss_mb: 0, oldest_age_seconds: 500, source: "canonical", confidence: "high", stale_count: 0, target: "/control/flow", controllable: false },
        { role: "codex", label: "Codex", count: 2, cpu_percent: 12.5, rss_mb: 512, oldest_age_seconds: 120, source: "process", confidence: "medium", stale_count: 0, target: "/control/ops", controllable: false },
      ],
      errors: [],
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
  { path: "/control/pressure", probe: "Pressure" },
  { path: "/control/ops", probe: "Ops Radar" },
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

  for (const viewport of [
    { name: "Desktop", size: { width: 1440, height: 1000 } },
    { name: "Mobile", size: { width: 390, height: 844 } },
  ]) {
    test(`Statistik MotherLedger rendert responsive ohne Secret-Leak (${viewport.name})`, async ({ page }) => {
      await page.setViewportSize(viewport.size);
      await mockControlApis(page);
      const watch = watchPage(page);

      await page.goto("/control/statistik");

      await expect(page.getByText("Statistik").filter({ visible: true }).first()).toBeVisible({ timeout: 15_000 });
      await expect(page.getByText("MotherLedger").filter({ visible: true }).first()).toBeVisible();
      await expect(page.getByText("USD inkl. Cache").filter({ visible: true }).first()).toBeVisible();
      await expect(page.getByText("Stats Mother Ledger").filter({ visible: true }).first()).toBeVisible();

      if (viewport.name === "Desktop") {
        await expect(page.getByRole("table", { name: "MotherLedger Desktop" })).toBeVisible();
        await expect(page.getByText("MotherLedger Mobile")).toHaveCount(0);
      } else {
        await expect(page.locator(".sb-ledger-cards")).toBeVisible();
        await expect(page.getByRole("table", { name: "MotherLedger Desktop" })).toHaveCount(0);
      }

      const coderWorker = page.getByRole("button", { name: /coder.*\$0\.04|\$0\.04.*coder/ }).filter({ visible: true }).first();
      await expect(coderWorker).toBeVisible();
      await coderWorker.click();
      await expect(page.getByText("#837").filter({ visible: true })).toBeVisible();
      await expect(page.getByText("metered · 5m · Neuralwatt —").filter({ visible: true })).toBeVisible();

      for (const secretMarker of [/\/home\//, /\.env\b/, /OPENAI_API_KEY/, /ANTHROPIC_API_KEY/, /sk-[A-Za-z0-9]/, /\.worktrees\//, /cmdline/]) {
        await expect(page.getByText(secretMarker)).toHaveCount(0);
      }
      watch.assertClean();
    });

    test(`Pressure-Tab zeigt kompakte Lesefakten ohne Rohpfade (${viewport.name})`, async ({ page }) => {
      await page.setViewportSize(viewport.size);
      await mockControlApis(page);

      await page.goto("/control/pressure");

      await expect(page.getByText("Pressure").filter({ visible: true }).first()).toBeVisible({ timeout: 15_000 });
      await expect(page.getByText("Busy").filter({ visible: true }).first()).toBeVisible();
      await expect(page.getByText("Last").filter({ visible: true }).first()).toBeVisible();
      await expect(page.getByText("CPU").filter({ visible: true }).first()).toBeVisible();
      await expect(page.getByText("RAM").filter({ visible: true }).first()).toBeVisible();
      await expect(page.getByText("Tailnet").filter({ visible: true }).first()).toBeVisible();
      await expect(page.getByText("Nächster Hebel").filter({ visible: true }).first()).toBeVisible();
      await expect(page.getByText("Tests laufen").filter({ visible: true }).first()).toBeVisible();
      await expect(page.getByText("pytest").filter({ visible: true }).first()).toBeVisible();
      await expect(page.getByText("/home/")).toHaveCount(0);
      await expect(page.getByText("run_tests_parallel.py")).toHaveCount(0);
    });
  }

  for (const viewport of [
    { name: "Desktop", size: { width: 1440, height: 1000 } },
    { name: "Mobile", size: { width: 390, height: 844 } },
  ]) {
    test(`Ops-Radar zeigt echte Hebel und keine Rohdaten (${viewport.name})`, async ({ page }) => {
      await page.setViewportSize(viewport.size);
      await mockControlApis(page);

      await page.goto("/control/ops");

      await expect(page.getByText("Ops Radar").filter({ visible: true }).first()).toBeVisible({ timeout: 15_000 });
      await expect(page.getByText("86 total").filter({ visible: true }).first()).toBeVisible();
      await expect(page.getByText("60 locked").filter({ visible: true }).first()).toBeVisible();
      await expect(page.getByText("Top-Hebel").filter({ visible: true }).first()).toBeVisible();
      await expect(page.getByText("Dirty Worktrees").filter({ visible: true }).first()).toBeVisible();
      await expect(page.getByText("Worktree-Ledger").filter({ visible: true }).first()).toBeVisible();
      await expect(page.getByText("Actor Map").filter({ visible: true }).first()).toBeVisible();
      await expect(page.getByText("Kanban Worker").filter({ visible: true }).first()).toBeVisible();
      await expect(page.getByText("read-only").filter({ visible: true }).first()).toBeVisible();
      await expect(page.getByText("/home/")).toHaveCount(0);
      await expect(page.getByText("cmdline")).toHaveCount(0);
      await expect(page.getByText(".worktrees/")).toHaveCount(0);
      await expect(page.getByText(/\b(stop|kill|update)\b/i)).toHaveCount(0);
    });
  }

});
