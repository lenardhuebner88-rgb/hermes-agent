import { describe, expect, it, vi } from "vitest";
import { renderToStaticMarkup } from "react-dom/server";
import { MemoryRouter } from "react-router-dom";
import {
  RunTimelinePanel,
  RunTimelineView,
  type RunTimelineResponse,
  type TimelineItem,
} from "./RunTimelineView";
import { eventTone } from "./RunTimelineView.helpers";

// View-Ebene: der Store-Hook wird gemockt, die drei Zustände (ladend / Fehler
// mit Retry / Daten) werden am statischen Markup geprüft.
const mockPolling = vi.hoisted(() => ({
  state: {
    data: null as RunTimelineResponse | null,
    error: null as string | null,
    loading: true,
    reload: vi.fn(),
  },
}));

vi.mock("../hooks/internal", () => ({
  usePolling: () => mockPolling.state,
}));

function item(partial: Partial<TimelineItem> & { kind: string; at: number }): TimelineItem {
  return {
    source: "event",
    payload: null,
    offset_seconds: 0,
    delta_seconds: 0,
    ...partial,
  };
}

function fixture(items: TimelineItem[]): RunTimelineResponse {
  return {
    run: {
      id: 42,
      task_id: "t_abc",
      profile: "coder",
      status: "done",
      outcome: "completed",
      error: null,
      summary: "fertig",
      started_at: 1000,
      ended_at: 1300,
      duration_seconds: 300,
    },
    items,
    count: items.length,
    truncated: false,
  };
}

describe("eventTone", () => {
  it("maps the plan's color code: grün=ok, rot=error, gelb=retry, grau=blocked", () => {
    expect(eventTone({ kind: "spawned", payload: null })).toBe("emerald");
    expect(eventTone({ kind: "completed", payload: null })).toBe("emerald");
    expect(eventTone({ kind: "crashed", payload: null })).toBe("red");
    expect(eventTone({ kind: "timed_out", payload: null })).toBe("red");
    expect(eventTone({ kind: "reclaimed", payload: null })).toBe("amber");
    expect(eventTone({ kind: "blocked", payload: null })).toBe("zinc");
    expect(eventTone({ kind: "heartbeat", payload: null })).toBe("zinc");
  });

  it("colours run_ended by its outcome payload", () => {
    expect(eventTone({ kind: "run_ended", payload: { outcome: "completed" } })).toBe("emerald");
    expect(eventTone({ kind: "run_ended", payload: { outcome: "crashed" } })).toBe("red");
    expect(eventTone({ kind: "run_ended", payload: { outcome: "blocked" } })).toBe("zinc");
  });
});

describe("RunTimelinePanel", () => {
  it("renders items in given order with offsets and run frame", () => {
    const html = renderToStaticMarkup(
      <RunTimelinePanel
        data={fixture([
          item({ kind: "run_started", at: 1000, offset_seconds: 0, source: "run" }),
          item({ kind: "spawned", at: 1005, offset_seconds: 5, delta_seconds: 5 }),
          item({
            kind: "commented", at: 1200, offset_seconds: 200, delta_seconds: 195,
            payload: { preview: "Zwischenstand gepostet" },
          }),
          item({
            kind: "run_ended", at: 1300, offset_seconds: 300, delta_seconds: 100,
            source: "run", payload: { outcome: "completed" },
          }),
        ])}
      />,
    );
    expect(html).toContain("Run #42");
    expect(html).toContain("t_abc");
    // Sorted rendering: spawned offset appears before commented offset.
    expect(html.indexOf("+5s")).toBeGreaterThan(-1);
    expect(html.indexOf("+5s")).toBeLessThan(html.indexOf("+3m20s"));
    expect(html).toContain("Zwischenstand gepostet");
    expect(html).toContain("spawned");
    expect(html).toContain("run_ended");
  });

  it("renders 200 events without choking and shows the empty state otherwise", () => {
    const many = Array.from({ length: 200 }, (_, i) =>
      item({ kind: "heartbeat", at: 1000 + i, offset_seconds: i, delta_seconds: i ? 1 : 0 }),
    );
    const html = renderToStaticMarkup(<RunTimelinePanel data={fixture(many)} />);
    expect(html.split("heartbeat").length - 1).toBeGreaterThanOrEqual(200);

    const empty = renderToStaticMarkup(<RunTimelinePanel data={fixture([])} />);
    expect(empty).toContain("Keine Events");
  });
});

describe("RunTimelineView (drei Zustände über usePolling)", () => {
  function renderView() {
    return renderToStaticMarkup(
      <MemoryRouter>
        <RunTimelineView />
      </MemoryRouter>,
    );
  }

  it("zeigt beim Erst-Load einen Skeleton, weder Leer-Zustand noch Fehler", () => {
    mockPolling.state = { data: null, error: null, loading: true, reload: vi.fn() };
    const html = renderView();
    expect(html).toContain('aria-busy="true"');
    expect(html).not.toContain("Keine Events");
    expect(html).not.toContain('role="alert"');
  });

  it("zeigt bei Fehler die ErrorNote mit Retry statt eines passiven Banners", () => {
    mockPolling.state = { data: null, error: "network down", loading: false, reload: vi.fn() };
    const html = renderView();
    expect(html).toContain('role="alert"');
    expect(html).toContain("network down");
    expect(html).toContain("Erneut laden");
    expect(html).not.toContain('aria-busy="true"');
  });

  it("rendert bei Daten das Panel und keinen Lade- oder Fehlerzustand", () => {
    mockPolling.state = { data: fixture([]), error: null, loading: false, reload: vi.fn() };
    const html = renderView();
    expect(html).toContain("Run #42");
    expect(html).toContain("Keine Events");
    expect(html).not.toContain('aria-busy="true"');
    expect(html).not.toContain('role="alert"');
  });
});
