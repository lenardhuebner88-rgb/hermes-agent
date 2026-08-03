// @vitest-environment jsdom

import { cleanup, render, screen, waitFor, within } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

const { fetchJSONMock } = vi.hoisted(() => ({ fetchJSONMock: vi.fn() }));

vi.mock("@/lib/api", async () => {
  const actual = await vi.importActual<typeof import("@/lib/api")>("@/lib/api");
  return { ...actual, fetchJSON: fetchJSONMock };
});

import { _resetPollingStore } from "../../hooks/pollingStore";
import { parseOrThrow } from "../../lib/schemas";
import { AgentHistoryResponseSchema } from "../../lib/schemas/runsDigestRollup";
import { AgentHistoryPanel } from "./AgentHistoryPanel";

const PAYLOAD = {
  days: 30,
  generated_at: 1_775_300_000,
  agents: [
    {
      profile: "coder",
      series: [
        { date: "2026-08-01", runs: 2, errors: 0, error_rate: 0, cost_usd: 0.12 },
        { date: "2026-08-02", runs: 4, errors: 1, error_rate: 0.25, cost_usd: 0.48 },
      ],
      totals: { runs: 6, errors: 1, error_rate: 1 / 6, cost_usd: 0.6 },
    },
    {
      profile: "reviewer",
      series: [{ date: "2026-08-02", runs: 9, errors: 0, error_rate: 0, cost_usd: 0.9 }],
      totals: { runs: 9, errors: 0, error_rate: 0, cost_usd: 0.9 },
    },
  ],
};

beforeEach(() => {
  _resetPollingStore();
  fetchJSONMock.mockReset();
});

afterEach(() => {
  cleanup();
  _resetPollingStore();
});

describe("AgentHistoryPanel", () => {
  it("rendert nur den Verlauf des geöffneten Profils mit Summen und Tageswerten", async () => {
    fetchJSONMock.mockResolvedValue(PAYLOAD);

    render(<AgentHistoryPanel profile="coder" />);

    expect(screen.getByRole("status").textContent).toContain("Agent-Verlauf wird geladen");
    const panel = await screen.findByRole("region", { name: "Agent-Verlauf (30 Tage)" });
    const totals = within(panel).getByLabelText("Summen Agent-Verlauf");
    expect(totals.textContent).toContain("Läufe gesamt6");
    expect(totals.textContent).toContain("Kosten gesamt$0,60");

    const firstDay = within(panel).getByLabelText("Verlauf 2026-08-01");
    expect(firstDay.textContent).toContain("2 Läufe");
    expect(firstDay.textContent).toContain("Fehler 0 %");
    expect(firstDay.textContent).toContain("$0,12");

    const secondDay = within(panel).getByLabelText("Verlauf 2026-08-02");
    expect(secondDay.textContent).toContain("4 Läufe");
    expect(secondDay.textContent).toContain("Fehler 25 %");
    expect(secondDay.textContent).toContain("$0,48");
    expect(within(secondDay).getByText("Fehler 25 %").className).toContain("text-status-alert");
    expect(panel.textContent).not.toContain("9 Läufe");
    expect(fetchJSONMock).toHaveBeenCalledWith("/api/fleet/agent-history?days=30");
  });

  it("zeigt den vorgeschriebenen Leerzustand für ein Profil ohne Serie", async () => {
    fetchJSONMock.mockResolvedValue({
      days: 30,
      generated_at: PAYLOAD.generated_at,
      agents: [{
        profile: "coder",
        series: [],
        totals: { runs: 0, errors: 0, error_rate: 0, cost_usd: 0 },
      }],
    });

    render(<AgentHistoryPanel profile="coder" />);

    expect(await screen.findByText("Kein Verlauf in den letzten 30 Tagen")).toBeTruthy();
  });

  it("zeigt Lade- und Fehlerzustand ohne ungefangenen Render-Fehler", async () => {
    let rejectRequest: ((reason?: unknown) => void) | undefined;
    fetchJSONMock.mockReturnValue(new Promise((_resolve, reject) => {
      rejectRequest = reject;
    }));

    render(<AgentHistoryPanel profile="coder" />);
    expect(screen.getByRole("status").textContent).toContain("Agent-Verlauf wird geladen");

    rejectRequest?.(new Error("500: kaputt"));
    await waitFor(() => {
      expect(screen.getByRole("alert").textContent).toContain("Agent-Verlauf konnte nicht geladen werden");
    });
  });
});

describe("AgentHistoryResponseSchema", () => {
  it.each([
    ["fehlendes Pflichtfeld", { ...PAYLOAD, generated_at: undefined }],
    ["unerwartetes Feld", { ...PAYLOAD, interne_notiz: "darf nicht verschwinden" }],
  ])("weist %s über parseOrThrow ab", (_label, invalid) => {
    expect(() => parseOrThrow(AgentHistoryResponseSchema, invalid, "fleet/agent-history"))
      .toThrow(/entspricht nicht dem Vertrag/);
  });
});
