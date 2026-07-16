import { renderToStaticMarkup } from "react-dom/server";
import { MemoryRouter } from "react-router-dom";
import { describe, expect, it, vi } from "vitest";

import type { InboxItem } from "../lib/decisionInbox";
import type {
  useFixRedispatch,
  useRepairDeliverable,
  useVetoEscalation,
} from "../hooks/useControlData";

const noopMutation = {
  busyId: null,
  doneIds: {},
  errorById: {},
  run: async () => undefined,
};

const hooks = vi.hoisted(() => {
  const poll = <T,>(data: T) => ({ data, loading: false, error: null, isStale: false, lastUpdated: 1 });
  return {
    poll,
    useDecisionInbox: vi.fn(),
    useSystemHealth: vi.fn(),
    useAccountUsage: vi.fn(),
    useDictateStatus: vi.fn(),
    useHermesWorkers: vi.fn(),
    useHermesTodayDigest: vi.fn(),
    useBoard: vi.fn(),
    useHermesRunsDaily: vi.fn(),
    useStrategistCount: vi.fn(),
    useFixRedispatch: vi.fn(),
    useRepairDeliverable: vi.fn(),
    useVetoEscalation: vi.fn(),
    useCaptureTask: vi.fn(),
  };
});

vi.mock("../hooks/useControlData", () => ({
  useDecisionInbox: hooks.useDecisionInbox,
  useSystemHealth: hooks.useSystemHealth,
  useAccountUsage: hooks.useAccountUsage,
  useDictateStatus: hooks.useDictateStatus,
  useHermesWorkers: hooks.useHermesWorkers,
  useHermesTodayDigest: hooks.useHermesTodayDigest,
  useBoard: hooks.useBoard,
  useHermesRunsDaily: hooks.useHermesRunsDaily,
  useStrategistCount: hooks.useStrategistCount,
  useFixRedispatch: hooks.useFixRedispatch,
  useRepairDeliverable: hooks.useRepairDeliverable,
  useVetoEscalation: hooks.useVetoEscalation,
  useCaptureTask: hooks.useCaptureTask,
}));

vi.mock("../components/fleet/FlowCapture", () => ({
  FlowCapture: () => <div data-testid="flow-capture" />,
}));

import { CommandHome, TopDecision } from "./CommandHome";

function installCommandHomeFixtures() {
  const item: InboxItem = {
    key: "fix-t_held",
    surface: "kanban",
    title: "Held Task braucht Fix-Redispatch",
    why: "status=held · worker asked for operator decision",
    nextAction: "Fix redispatch",
    tone: "red",
    target: "/control/fleet?task=t_held",
    weight: 99,
    fixTaskId: "t_held",
  };
  // Zweiter Eintrag landet in der Queue unter dem Top-Decision-Hero (rest =
  // items.slice(1)) — braucht es, damit DecisionRow (mit dem 44px-"Öffnen:"-
  // Chevron) überhaupt rendert.
  const queued: InboxItem = {
    key: "autoresearch-t_review",
    surface: "autoresearch",
    title: "Autoresearch-Vorschlag wartet auf Review",
    why: "contradiction · medium",
    nextAction: "Prüfen & entscheiden",
    tone: "amber",
    target: "/control/autoresearch?task=t_review",
    weight: 60,
  };
  hooks.useDecisionInbox.mockReturnValue({
    items: [item, queued],
    summary: { total: 2, autoresearch: 1, family: 0, orchestrator: 0, kanban: 1 },
    snapshot: { items: [], generated_at: 1, stale_after_seconds: 60, source: "fixture" },
    worstTone: "red",
    loading: false,
    refreshing: false,
    sourceErrors: [],
  });
  hooks.useSystemHealth.mockReturnValue(hooks.poll({
    schema: "hermes-health-v1",
    generated_at: "2026-07-05T12:00:00Z",
    overall: "degraded",
    subsystems: {
      gateway: { status: "healthy", detail: "ok" },
      autoresearch: { status: "healthy", detail: "ok" },
      kanban_db: { status: "degraded", detail: "1 held" },
      kanban_dispatcher: { status: "healthy", detail: "ok" },
      scheduler: { status: "healthy", detail: "ok" },
    },
  }));
  hooks.useAccountUsage.mockReturnValue(hooks.poll({
    providers: [{
      provider: "openai",
      title: "OpenAI",
      plan: "pro",
      available: true,
      source: "fixture",
      fetched_at: "2026-07-05T12:00:00Z",
      windows: [{ label: "weekly", window_key: "week", used_percent: 83, reset_at: null, detail: "fixture" }],
      details: [],
      unavailable_reason: null,
      cached: false,
    }],
    cache_ttl_seconds: 300,
  }));
  hooks.useDictateStatus.mockReturnValue(hooks.poll({
    schema: "hermes-dictate-status-v1",
    connected: true,
    last_contact_at: 1,
    app_version: "1.0",
    engine: "on_device",
    language: "german",
    style: "formal",
    surface: "overlay",
    microphone_permission: true,
    service_enabled: true,
    last_error: null,
    dictations: 3,
    failures: 0,
    retries: 0,
    busy: 0,
    success_rate_percent: 100,
    latency_ms: 700,
    latency_p50_ms: 700,
    latency_p95_ms: 700,
    apk: null,
  }));
  hooks.useHermesWorkers.mockReturnValue(hooks.poll({ workers: [], count: 0, cap: 3, checked_at: 1 }));
  hooks.useHermesTodayDigest.mockReturnValue(hooks.poll({ schema: "kanban-today-digest-v1", generated_at: "2026-07-05T12:00:00Z", shipped_today: 3, lead_time_p50_seconds: null, by_assignee: [] }));
  hooks.useBoard.mockReturnValue(hooks.poll({ columns: [{ name: "held", tasks: [{ id: "t_held", title: "Held", status: "held" }] }], generated_at: 1 }));
  hooks.useHermesRunsDaily.mockReturnValue(hooks.poll({ days: 14, series: [] }));
  hooks.useStrategistCount.mockReturnValue(hooks.poll({ count: 0 }));
  hooks.useFixRedispatch.mockReturnValue(noopMutation);
  hooks.useRepairDeliverable.mockReturnValue(noopMutation);
  hooks.useVetoEscalation.mockReturnValue(noopMutation);
  hooks.useCaptureTask.mockReturnValue({ create: async () => "t_new", loading: false, error: null });
}

describe("TopDecision", () => {
  it("keeps long decision title and reason available while clamping the visible card", () => {
    const item: InboxItem = {
      key: "decision-1",
      surface: "autoresearch",
      title: "Skill-Schwäche in family-organizer-ui-polish: widersprüchliche Anweisung mit sehr langem Kontext",
      why: "contradiction · critical · mehrere Belege im Autoresearch-Report",
      nextAction: "Prüfen & entscheiden",
      tone: "red",
      target: "/control/autoresearch",
      weight: 95,
    };
    const html = renderToStaticMarkup(
      <TopDecision
        item={item}
        onOpen={() => undefined}
        fix={noopMutation as unknown as ReturnType<typeof useFixRedispatch>}
        repair={noopMutation as unknown as ReturnType<typeof useRepairDeliverable>}
        veto={noopMutation as unknown as ReturnType<typeof useVetoEscalation>}
      />,
    );

    expect(html).toContain(`title="${item.title}"`);
    expect(html).toContain(`title="${item.why}"`);
    expect(html).toContain("line-clamp-3");
    expect(html).toContain("sm:line-clamp-2");
  });
});

describe("CommandHome", () => {
  it("renders the attention-first cockpit from real decision-inbox and health payload shapes", () => {
    installCommandHomeFixtures();

    const html = renderToStaticMarkup(
      <MemoryRouter initialEntries={["/control"]}>
        <CommandHome density="compact" />
      </MemoryRouter>,
    );

    expect(html).toContain("Attention Inbox");
    expect(html).toContain("Held Task braucht Fix-Redispatch");
    expect(html).toContain("System-/Kosten-Puls");
    expect(html).toContain("Gateway");
    expect(html).toContain("Research");
    expect(html).toContain("Kanban");
    expect(html).toContain("Dispatcher");
    expect(html).toContain("Max Limit");
    expect(html).toContain("Hermes Diktat");
    expect(html).toContain("Quick-Jumps");
    expect(html).toContain("Fleet");
    expect(html).toContain("System");
    expect(html).toContain("Statistik");
    expect(html).toContain("Regal");
  });

  it("no longer renders its own masthead — the shell Puls-Leiste carries the route label since W3-2", () => {
    installCommandHomeFixtures();

    const html = renderToStaticMarkup(
      <MemoryRouter initialEntries={["/control"]}>
        <CommandHome density="compact" />
      </MemoryRouter>,
    );

    expect(html).not.toContain("ch-masthead");
    expect(html).not.toContain("ch-brand");
  });

  it("gives the queue row's 'Öffnen' chevron a >=44px hit-area (W3-2 touch target)", () => {
    installCommandHomeFixtures();

    const html = renderToStaticMarkup(
      <MemoryRouter initialEntries={["/control"]}>
        <CommandHome density="compact" />
      </MemoryRouter>,
    );

    expect(html).toContain("Autoresearch-Vorschlag wartet auf Review");
    // h-12/w-12 = 3rem = 45px am 15px-Root (h-11 wäre 41.25px < 44px-AC!)
    expect(html).toMatch(/aria-label="Öffnen: Autoresearch-Vorschlag wartet auf Review"[^>]*class="[^"]*\bh-12\b[^"]*\bw-12\b/);
  });

  it("gives the 'Flow öffnen' bridge control a >=44px hit-area (W3-2 touch target)", () => {
    installCommandHomeFixtures();

    const html = renderToStaticMarkup(
      <MemoryRouter initialEntries={["/control"]}>
        <CommandHome density="compact" />
      </MemoryRouter>,
    );

    expect(html).toMatch(/class="[^"]*\bmin-h-12\b[^"]*"[^>]*>Flow öffnen/);
  });

  it("gives the 'Statistik öffnen' bridge control a >=44px hit-area (W3-3 rider, same min-h-12 pattern as Flow öffnen)", () => {
    installCommandHomeFixtures();
    // StatsPulse only renders once it has a non-empty daily series (real
    // RunsDailyPoint shape, lib/schemas.ts RunsDailyPointSchema) — the
    // fixture default is an empty series.
    hooks.useHermesRunsDaily.mockReturnValue(hooks.poll({
      days: 14,
      series: [{
        date: "2026-07-04",
        done_roots: 3,
        done_roots_by_class: { nutzer: 3, haertung: 0, meta: 0 },
        done_tasks: 5,
        cost_usd: 1.2,
        input_tokens: 4000,
        output_tokens: 1200,
        runs_completed: 5,
        runs_failed: 0,
        cycle_time_p50_seconds: 300,
      }],
    }));

    const html = renderToStaticMarkup(
      <MemoryRouter initialEntries={["/control"]}>
        <CommandHome density="compact" />
      </MemoryRouter>,
    );

    expect(html).toMatch(/class="[^"]*\bmin-h-12\b[^"]*"[^>]*>Statistik öffnen/);
  });

  it("gives the 'Stratege öffnen' bridge control a >=44px hit-area (W3-3 rider, same min-h-12 pattern as Flow öffnen)", () => {
    installCommandHomeFixtures();
    // StrategistSignalTile only renders when count > 0 (real
    // StrategistCountSchema shape) — the fixture default is 0.
    hooks.useStrategistCount.mockReturnValue(hooks.poll({ count: 2 }));

    const html = renderToStaticMarkup(
      <MemoryRouter initialEntries={["/control"]}>
        <CommandHome density="compact" />
      </MemoryRouter>,
    );

    expect(html).toMatch(/class="[^"]*\bmin-h-12\b[^"]*"[^>]*>Stratege öffnen/);
  });
});
