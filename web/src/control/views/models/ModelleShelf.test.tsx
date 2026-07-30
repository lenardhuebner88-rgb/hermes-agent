// @vitest-environment jsdom
import { afterEach, describe, expect, it, vi } from "vitest";
import { cleanup, fireEvent, render, screen, waitFor, within } from "@testing-library/react";

const { fetchJSONMock } = vi.hoisted(() => ({ fetchJSONMock: vi.fn() }));

vi.mock("@/lib/api", async () => {
  const actual = await vi.importActual<typeof import("@/lib/api")>("@/lib/api");
  return { ...actual, fetchJSON: fetchJSONMock };
});

import { ModelleShelf, type ModelsResponse } from "./ModelleShelf";

const RESPONSE: ModelsResponse = {
  updated: "2026-07-22T07:30:00+02:00",
  pulse: [],
  guides: [],
  models: [
    {
      id: "anthropic/claude-opus-4.8",
      provider: "anthropic",
      family: "claude-opus",
      context: "1M",
      price_in: 5,
      price_out: 25,
      created: "2026-05-27",
      guide_family: "claude-opus",
      scores: [{
        suite: "arena-overall",
        score: 1473.4,
        unit: " pts",
        source: "lmarena",
        as_of: "2026-07-21",
        claimed_by_provider: false,
        source_name: "LMArena (Style Control)",
        source_url: "https://huggingface.co/datasets/lmarena-ai/leaderboard-dataset",
      }],
    },
    {
      id: "google/gemini-3.6-flash",
      provider: "google",
      family: "gemini",
      context: "1M",
      price_in: 1,
      price_out: 6,
      created: "2026-07-21",
      guide_family: "gemini",
      scores: [],
    },
    {
      id: "openai/gpt-5.6-luna-pro",
      provider: "openai",
      family: "gpt5-codex",
      context: "1M",
      price_in: 1,
      price_out: 6,
      created: "2026-07-09",
      guide_family: "gpt5-codex",
      scores: [],
    },
    {
      id: "moonshotai/kimi-k2-thinking",
      provider: "moonshotai",
      family: "kimi",
      context: "262k",
      price_in: 0.6,
      price_out: 2.5,
      created: "2025-11-06",
      guide_family: "kimi",
      scores: [{
        suite: "swe-bench-verified",
        score: 71.3,
        unit: "%",
        source: "seed",
        as_of: "2025-11-04",
        claimed_by_provider: true,
        source_name: "Provider-Angabe (kuratiert)",
        source_url: "",
      }],
    },
  ],
};

afterEach(() => {
  cleanup();
  vi.clearAllMocks();
});

describe("ModelleShelf: ehrliche Benchmark-Abdeckung", () => {
  it("zeigt gescorete Modelle zuerst und klappt ungescorete Modelle standardmäßig ein", async () => {
    fetchJSONMock.mockResolvedValue(RESPONSE);
    render(<ModelleShelf />);

    expect(await screen.findByText("anthropic/claude-opus-4.8")).toBeTruthy();
    expect(screen.getByText("Gescoret")).toBeTruthy();
    expect(screen.getByText("1 Modell")).toBeTruthy();

    const disclosure = screen.getByText("Keine unabhängigen Benchmarks").closest("details");
    expect(disclosure).toBeTruthy();
    expect(disclosure?.hasAttribute("open")).toBe(false);
    expect(within(disclosure as HTMLElement).getByText("3 Modelle")).toBeTruthy();
    expect(within(disclosure as HTMLElement).getByText("google/gemini-3.6-flash")).toBeTruthy();
    expect(within(disclosure as HTMLElement).getByText("openai/gpt-5.6-luna-pro")).toBeTruthy();
    expect(within(disclosure as HTMLElement).getByText("moonshotai/kimi-k2-thinking")).toBeTruthy();
    expect(within(disclosure as HTMLElement).getByText("Provider-Angabe")).toBeTruthy();
    expect(screen.queryByText("Noch keine gequellten Benchmarks.")).toBeNull();
  });

  it("rendert im Benchmark-Tab nur Modelle mit Scores und nennt die Abdeckung", async () => {
    fetchJSONMock.mockResolvedValue(RESPONSE);
    render(<ModelleShelf />);

    await screen.findByText("anthropic/claude-opus-4.8");
    fireEvent.click(screen.getByRole("button", { name: "Modelle Benchmarks" }));

    await waitFor(() => {
      expect(screen.getByText(/1 von 4 unabhängig gescoret/)).toBeTruthy();
    });
    expect(screen.getByText("anthropic/claude-opus-4.8")).toBeTruthy();
    expect(screen.getByText("moonshotai/kimi-k2-thinking")).toBeTruthy();
    expect(screen.queryByText("google/gemini-3.6-flash")).toBeNull();
    expect(screen.queryByText("openai/gpt-5.6-luna-pro")).toBeNull();
  });
});
