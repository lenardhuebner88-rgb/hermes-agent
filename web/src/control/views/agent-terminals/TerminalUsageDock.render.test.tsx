// @vitest-environment jsdom
import { afterEach, describe, expect, it, vi } from "vitest";
import { cleanup, render, screen } from "@testing-library/react";

import type { AccountUsageProvider, AccountUsageResponse } from "../../lib/types";

const useAccountUsageMock = vi.hoisted(() => vi.fn());

vi.mock("../../hooks/useControlData", async () => {
  const actual = await vi.importActual<typeof import("../../hooks/useControlData")>("../../hooks/useControlData");
  return { ...actual, useAccountUsage: useAccountUsageMock };
});

import { TerminalUsageDock } from "./TerminalUsageDock";

afterEach(() => {
  cleanup();
  vi.clearAllMocks();
});

// Field names/nesting mirror the real /api/account-usage payload
// (AccountUsageResponseSchema in lib/schemas.ts) — used_percent/window_key/
// reset_at, not invented shapes, same fixture style as
// AgentTerminalsView.render.test.tsx's getAccountUsage mock.
function provider(overrides: Partial<AccountUsageProvider>): AccountUsageProvider {
  return {
    provider: "openai-codex",
    available: true,
    source: "oauth",
    fetched_at: null,
    title: "ChatGPT",
    plan: "Plus",
    windows: [],
    details: [],
    unavailable_reason: null,
    cached: false,
    ...overrides,
  };
}

function loadState(data: AccountUsageResponse | null, patch: Partial<{ loading: boolean; error: string | null }> = {}) {
  return { data, loading: false, error: null, lastUpdated: null, reload: vi.fn(), updateData: vi.fn(), ...patch };
}

describe("TerminalUsageDock", () => {
  it("colors a near-exhausted window with the alert token, not a legacy cyan/emerald literal (real data shape)", () => {
    useAccountUsageMock.mockReturnValue(
      loadState({
        cache_ttl_seconds: 60,
        providers: [
          provider({
            provider: "openai-codex",
            title: "ChatGPT",
            windows: [
              { label: "5h", window_key: "five_hour", used_percent: 92, reset_at: "2026-07-10T18:00:00Z", detail: null },
              { label: "Weekly", window_key: "weekly", used_percent: 40, reset_at: null, detail: null },
            ],
          }),
        ],
      }),
    );

    const { container } = render(<TerminalUsageDock open onClose={() => {}} />);

    // 92% (>=90) renders the status-alert token, matching the same red/amber/ok
    // thresholds AccountUsageTile's limitTone() already uses for this exact
    // account-usage domain.
    expect(container.querySelector(".bg-status-alert")).not.toBeNull();
    expect(screen.getByText("92%")).toBeTruthy();

    const html = container.innerHTML;
    for (const legacy of ["cyan-300", "cyan-200", "emerald-300", "amber-300", "red-400", "ink-dim", "hc-eyebrow", "rounded-[16px]"]) {
      expect(html).not.toContain(legacy);
    }
  });

  it("marks an unavailable provider idle with a visible 'aus' label (no color-only status) instead of the legacy white/20 dot", () => {
    useAccountUsageMock.mockReturnValue(
      loadState({
        cache_ttl_seconds: 60,
        providers: [provider({ provider: "kimi", title: "Kimi", available: false, unavailable_reason: "kein Login" })],
      }),
    );

    const { container } = render(<TerminalUsageDock open onClose={() => {}} />);

    expect(screen.getByText("aus")).toBeTruthy();
    const dot = container.querySelector(".hc-led-idle");
    expect(dot).not.toBeNull();
    expect(dot?.className).not.toContain("white/20");
  });

  it("renders the Usage eyebrow via the shared Eyebrow primitive, not the mono hc-eyebrow compat class", () => {
    useAccountUsageMock.mockReturnValue(loadState({ cache_ttl_seconds: 60, providers: [] }));

    const { container } = render(<TerminalUsageDock open onClose={() => {}} />);

    const eyebrow = screen.getByText("Usage");
    expect(eyebrow.className).toContain("font-display");
    expect(container.innerHTML).not.toContain("hc-eyebrow");
  });

  it("renders loading skeletons while usage is still loading and no providers are cached yet, without legacy classes", () => {
    useAccountUsageMock.mockReturnValue(loadState(null, { loading: true }));

    const { container } = render(<TerminalUsageDock open onClose={() => {}} />);

    expect(screen.getByLabelText("Usage wird geladen")).toBeTruthy();
    expect(container.querySelectorAll(".hc-skeleton")).toHaveLength(3);

    const html = container.innerHTML;
    for (const legacy of ["cyan-300", "cyan-200", "emerald-300", "amber-300", "red-400", "ink-dim", "hc-eyebrow", "rounded-[16px]"]) {
      expect(html).not.toContain(legacy);
    }
  });

  it("renders the error message when the usage fetch fails, without legacy classes", () => {
    useAccountUsageMock.mockReturnValue(loadState(null, { error: "network down" }));

    const { container } = render(<TerminalUsageDock open onClose={() => {}} />);

    expect(screen.getByText("Usage konnte nicht geladen werden.")).toBeTruthy();

    const html = container.innerHTML;
    for (const legacy of ["cyan-300", "cyan-200", "emerald-300", "amber-300", "red-400", "ink-dim", "hc-eyebrow", "rounded-[16px]"]) {
      expect(html).not.toContain(legacy);
    }
  });
});
