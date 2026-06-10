import { describe, expect, it, vi } from "vitest";
import { renderToStaticMarkup } from "react-dom/server";
import { LanesPanel } from "./LanesView";
import type { LanesResponse } from "./lanes/api";

const fixture: LanesResponse = {
  count: 2,
  active_id: "lane_1",
  profiles: [
    { name: "coder", worker_runtime: "hermes", default_model: "gpt-5.5", description: "" },
    { name: "premium", worker_runtime: "claude-cli", default_model: "claude-fable-5", description: "" },
  ],
  lanes: [
    {
      id: "lane_1",
      name: "api-standard",
      active: true,
      builtin: true,
      created_at: 0,
      updated_at: 0,
      profiles: {
        coder: { worker_runtime: "hermes", model: "gpt-5.5" },
      },
    },
    {
      id: "lane_2",
      name: "max-abo",
      active: false,
      builtin: true,
      created_at: 0,
      updated_at: 0,
      profiles: {
        premium: { worker_runtime: "claude-cli", model: "claude-fable-5" },
      },
    },
  ],
};

const noopActions = {
  onActivate: vi.fn(),
  onDelete: vi.fn(),
  onSave: vi.fn(),
};

describe("LanesPanel", () => {
  it("renders both lane presets with the active marker on the active lane", () => {
    const html = renderToStaticMarkup(
      <LanesPanel data={fixture} busy={false} actions={noopActions} onCreate={vi.fn()} />,
    );
    expect(html).toContain("api-standard");
    expect(html).toContain("max-abo");
    expect(html).toContain("Aktiv");
    // The inactive lane offers activation; the active one must not.
    expect(html).toContain("Aktivieren");
    expect(html).toContain("claude-fable-5");
  });

  it("sorts profile rows and renders runtime selects", () => {
    const html = renderToStaticMarkup(
      <LanesPanel data={fixture} busy={false} actions={noopActions} onCreate={vi.fn()} />,
    );
    expect(html).toContain("claude-cli");
    expect(html).toContain("hermes");
    // Editor inputs carry the lane's mapped profile names.
    expect(html).toContain('value="coder"');
    expect(html).toContain('value="premium"');
  });

  it("renders the empty state when no lanes exist", () => {
    const html = renderToStaticMarkup(
      <LanesPanel
        data={{ lanes: [], count: 0, active_id: null, profiles: [] }}
        busy={false}
        actions={noopActions}
        onCreate={vi.fn()}
      />,
    );
    expect(html).toContain("Keine Lanes");
  });
});
