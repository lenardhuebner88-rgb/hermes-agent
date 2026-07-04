import { expect, test, type Page } from "@playwright/test";

/**
 * AC-4 headless screenshot for task t_63d01ead.
 *
 * Mocks /api/** (same shapes as control-smoke.spec.ts::mockControlApis)
 * but with a POPULATED 7+ day runs/daily series so the Fertig-24h
 * Sparkline renders. Navigates to /control/fleet at 390px and asserts:
 * sparkline SVG visible, no horizontal overflow, no console errors.
 */

const NOW = Math.floor(Date.now() / 1000);
const DAY = 86400;

function daySeries(n: number) {
  const out: { date: string; done_tasks: number; total_tasks: number }[] = [];
  for (let i = n - 1; i >= 0; i--) {
    const d = new Date((NOW - i * DAY) * 1000);
    out.push({
      date: d.toISOString().slice(0, 10),
      done_tasks: 3 + ((n - i) * 7) % 22,
      total_tasks: 3 + ((n - i) * 7) % 22 + 4,
    });
  }
  return out;
}

async function mockApis(page: Page) {
  await page.addInitScript(() => {
    (window as unknown as { __HERMES_SESSION_TOKEN__?: string }).__HERMES_SESSION_TOKEN__ = "ac4-mock";
    const NativeWebSocket = window.WebSocket;
    class MockSock extends EventTarget {
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
      readyState = 0;
      constructor(url: string | URL) {
        super();
        this.url = String(url);
        window.setTimeout(() => {
          this.readyState = 1;
          this.dispatchEvent(new Event("open"));
        }, 0);
      }
      close() {
        this.readyState = 3;
        this.dispatchEvent(new CloseEvent("close"));
      }
      send() {}
    }
    window.WebSocket = new Proxy(NativeWebSocket, {
      construct(t, args) {
        return new MockSock(args[0] as string | URL);
      },
    }) as unknown as typeof WebSocket;
  });

  await page.route("**/*.{woff,woff2}", async (route) => {
    await route.fulfill({ status: 200, contentType: "font/woff2", body: "" });
  });

  await page.route("**/api/**", async (route) => {
    const path = new URL(route.request().url()).pathname;

    // ── agent-terminals ──
    if (path.startsWith("/api/agent-terminals")) {
      const w = { session: "work", window: "codex", active: true, pane_id: "%7", pid: 4242, command: "codex", cwd: "/home/piet/project", dead: false, title: "codex" };
      const body = path === "/api/agent-terminals/capabilities" ? { tmux_available: true, hermes_tui_available: true, hermes_binary: "hermes", reason: null, agents: { codex: { available: true, binary: "codex", reason: null } }, workdirs: [{ key: "home", label: "Zuhause (~)", path: "~" }] }
        : path === "/api/agent-terminals/sessions" ? { sessions: ["work"] }
        : path === "/api/agent-terminals/windows" ? { windows: [w] }
        : path === "/api/agent-terminals/overview" ? { now: NOW, windows: [{ ...w, state: "laeuft", state_source: "heuristic", tail: "running", question: null, last_activity: NOW }] }
        : path === "/api/agent-terminals/capture" ? { content: "running" }
        : path === "/api/agent-terminals/show" ? { window: w }
        : path === "/api/agent-terminals/attach-metadata" ? { metadata: { target: "work:codex", attach_argv: ["tmux", "attach-session", "-t", "work:codex"] } }
        : path === "/api/agent-terminals/handoff-draft" ? { draft: { target: "work:codex", content: "# handoff" } }
        : path === "/api/agent-terminals/terminate" ? { ok: true }
        : { ok: true };
      await route.fulfill({ contentType: "application/json", body: JSON.stringify(body) });
      return;
    }

    // ── FleetView-critical: runs/daily mit POPULATED 7+ days ──
    let body: unknown;
    if (path === "/api/dashboard/plugins" || path === "/api/dashboard/plugins/") {
      body = [];
    } else if (path === "/api/skills") {
      body = [{ name: "code-review", description: "Review", category: "dev", enabled: true }];
    } else if (path === "/api/tools/toolsets") {
      body = [{ name: "terminal", description: "Terminal", enabled: true, tools: ["terminal"] }];
    } else if (path === "/api/account-usage") {
      body = { cache_ttl_seconds: 60, providers: [] };
    } else if (path === "/api/health-status") {
      body = { overall: "healthy", subsystems: { gateway: { status: "healthy" }, autoresearch: { status: "healthy" }, kanban_db: { status: "healthy" } } };
    } else if (path.includes("/workers/active")) {
      body = { workers: [], count: 0, checked_at: NOW };
    } else if (path.includes("/kanban/board")) {
      body = { columns: [], now: NOW };
    } else if (path.includes("/runs/windowed-rollup")) {
      body = { schema: "kanban-windowed-rollup-v1", since_hours: 168, now: NOW, completed_roots: 0, roots: [] };
    } else if (path.includes("/runs/today-digest")) {
      body = { count: 0, items: [] };
    } else if (path.includes("/runs/daily")) {
      // AC-1: populated 30-day series so Sparkline has >=7 points.
      body = { days: 30, now: NOW, series: daySeries(30) };
    } else if (path.includes("/runs/costs")) {
      body = { now: NOW, window_label: "7d", items: [], total_cost_usd: 0, total_cost_eur: 0 };
    } else if (path.includes("/runs/issues")) {
      body = { now: NOW, issues: [] };
    } else if (path.includes("/reliability")) {
      body = { now: NOW, windows: [] };
    } else if (path.includes("/planspecs")) {
      body = { now: NOW, specs: [] };
    } else if (path.includes("/lanes/catalog") || path.includes("/lanes")) {
      body = { now: NOW, lanes: [] };
    } else if (path.includes("/kanban/epics")) {
      body = { now: NOW, epics: [] };
    } else if (path.includes("/kanban/stats/board")) {
      body = { now: NOW, columns: [] };
    } else if (path.includes("/kanban/review-verdicts") || path.includes("/review-verdicts")) {
      body = { now: NOW, items: [] };
    } else if (path.includes("/blocked-completions")) {
      body = { now: NOW, items: [] };
    } else if (path.includes("/autoresearch/proposals") || path.includes("/autoresearch/runs")) {
      body = { proposals: [], count: 0, runs: [] };
    } else if (path.includes("/strategist/")) {
      body = { proposals: [], count: 0, harvest: null, propose: null, running: false, status: "idle", runs: [], outcomes: [] };
    } else if (path.includes("/family-organizer/backlog")) {
      body = { items: [] };
    } else if (path.includes("/orchestration/backlog")) {
      body = { items: [] };
    } else if (path.includes("/metrics-lite")) {
      body = { schema: "hermes-metrics-lite-v1", checked_at: NOW, uptime_seconds: 60, groups: {} };
    } else if (path === "/api/pressure-status") {
      body = { schema: "hermes-pressure-v1", checked_at: NOW, overall: "ok", cause: null, recommendation: null, host: { cpu_percent: 10, load_avg: [0.5, 0.4, 0.3], cpu_count: 12, memory_percent: 40 }, dashboard: { pid: 1, rss_mb: 100, cpu_percent: 1, cpu_weight: 100, cpu_quota: "max", tasks_current: 0 }, pressure_sources: [], access: { tailnet: "direct", api_latency_ms: 10, detail: "ok" }, token_pressure: { class: "unknown", pct: null, updated_at: null }, errors: [] };
    } else if (path === "/api/operator-inventory") {
      body = { schema: "hermes-operator-inventory-v1", checked_at: NOW, summary: { worktrees_total: 0, worktrees_locked: 0, worktrees_dirty: 0, worktrees_prunable: 0, worktrees_orphaned: 0, worktrees_status_unknown: 0, actors_total: 0, actors_canonical: 0 }, next_lever: null, levers: [], worktrees: [], actors: [], errors: [] };
    } else if (path.includes("/loops")) {
      body = { loops: [] };
    } else if (path.includes("/cron/observability")) {
      body = { now: NOW, jobs: [] };
    } else if (path.includes("/decision-queue")) {
      body = { now: NOW, items: [] };
    } else if (path.includes("/library/items")) {
      body = { items: [] };
    } else if (path.includes("/vault/provenance")) {
      body = { now: NOW, items: [] };
    } else if (path.includes("/system/health")) {
      body = { now: NOW, checks: [] };
    } else if (path.includes("/flow/triage-failures") || path.includes("/flow/funnel-drafts") || path.includes("/disposition")) {
      body = { items: [] };
    } else if (path.includes("/subscription/token-burn")) {
      body = { now: NOW, items: [], total: 0 };
    } else if (path.includes("/chain/completion") || path.includes("/chain/costs")) {
      body = { now: NOW, items: [] };
    } else if (path.includes("/runs/recent")) {
      body = { runs: [] };
    } else if (path.includes("/runs/summary")) {
      body = { now: NOW, summary: {} };
    } else {
      body = {};
    }
    await route.fulfill({ contentType: "application/json", body: JSON.stringify(body) });
  });
}

test("AC-4: Fertig-24h Sparkline rendert bei 390px ohne Overflow (t_63d01ead)", async ({ page }) => {
  const consoleErrors: string[] = [];
  const failed: string[] = [];
  page.on("console", (m) => {
    if (m.type() === "error") consoleErrors.push(m.text());
  });
  page.on("pageerror", (e) => consoleErrors.push(e.message));
  page.on("requestfailed", (r) => {
    const err = r.failure()?.errorText ?? "";
    if (err === "net::ERR_ABORTED") return;
    failed.push(`${r.method()} ${r.url()} ${err}`);
  });
  page.on("response", (r) => {
    if (r.status() >= 400) failed.push(`${r.status()} ${r.request().method()} ${r.url()}`);
  });

  await page.setViewportSize({ width: 390, height: 844 });
  await mockApis(page);

  await page.goto("/control/fleet", { waitUntil: "domcontentloaded" });

  // Wait for FleetView to mount + daily data to resolve → sparkline SVG.
  // The <svg> itself carries className="fleet-spark" (role="img").
  await expect(page.locator("svg.fleet-spark")).toBeVisible({ timeout: 15_000 });

  // The sparkline must not cause horizontal overflow.
  const dims = await page.evaluate(() => ({
    scrollW: document.documentElement.scrollWidth,
    clientW: document.documentElement.clientWidth,
  }));
  expect(dims.scrollW, `horizontal overflow: scrollW=${dims.scrollW} > clientW=${dims.clientW}`).toBeLessThanOrEqual(dims.clientW);

  // Zero console errors / failed requests.
  expect(consoleErrors, "console.error messages").toEqual([]);
  expect(failed, "4xx/5xx/failed requests").toEqual([]);

  await page.screenshot({
    path: "e2e/_ac4-fleet-sparkline-390.png",
    fullPage: false,
  });
});
