// @vitest-environment jsdom
import { existsSync, readFileSync } from "node:fs";
import path from "node:path";
import { cleanup, fireEvent, render, screen } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { LanesView } from "./LanesView";
import type { LanesResponse } from "./lanes/api";

// The REAL captured live payload (3 lanes, 10 profiles, 55 models). The data
// boundary (loadLanes + mutations + probes) is mocked; every pure helper
// (editorRows/applyChoice/filterSinnvoll/…) runs for real against this payload,
// so the assertions below are about rendered behaviour, never about a mock
// returning what we told it to (no vi.fn-as-both-SUT-and-expectation).
//
// Loaded via process.cwd() (not `new URL(…, import.meta.url)`): in the jsdom
// environment import.meta.url is http://, which readFileSync rejects. The gate
// runs vitest from web/, with a repo-root fallback for direct invocations.
function loadFixture(): LanesResponse {
  const candidates = [
    path.join(process.cwd(), "src/control/views/lanes/__fixtures__/lanes-live.json"),
    path.join(process.cwd(), "web/src/control/views/lanes/__fixtures__/lanes-live.json"),
  ];
  const file = candidates.find((candidate) => existsSync(candidate));
  if (!file) throw new Error(`lanes-live.json fixture not found from ${process.cwd()}`);
  return JSON.parse(readFileSync(file, "utf8")) as LanesResponse;
}

const fixture = loadFixture();

const loadLanesMock = vi.fn(async () => fixture);
const activateLaneMock = vi.fn(async (id: string) => ({ lane: { ...fixture.lanes[0], id } }));
const createLaneMock = vi.fn(async (name: string) => ({ lane: { ...fixture.lanes[0], id: "lane_new", name } }));
const persistLaneModelsMock = vi.fn(async (_profiles: unknown) => ({
  written: [],
  failed: [],
  lanes: fixture.lanes,
  active_id: fixture.active_id,
}));
const runModelProbeMock = vi.fn(async (_input: unknown) => ({ provider: "openai-codex", model: "gpt-5.6-sol", status: "ok" as const }));
const runCatalogProbeMock = vi.fn(async (_input: unknown) => ({ results: [], truncated: false }));

vi.mock("./lanes/api", async (importOriginal) => {
  const actual = await importOriginal<typeof import("./lanes/api")>();
  return {
    ...actual,
    loadLanes: () => loadLanesMock(),
    activateLane: (id: string) => activateLaneMock(id),
    createLane: (name: string, _profiles: unknown) => createLaneMock(name),
    persistLaneModels: (profiles: unknown) => persistLaneModelsMock(profiles),
    runModelProbe: (input: unknown) => runModelProbeMock(input),
    runCatalogProbe: (input: unknown) => runCatalogProbeMock(input),
  };
});

let consoleError: ReturnType<typeof vi.spyOn>;

beforeEach(() => {
  consoleError = vi.spyOn(console, "error").mockImplementation(() => {});
});

afterEach(() => {
  cleanup();
  vi.clearAllMocks();
  consoleError.mockRestore();
});

describe("LanesView greenfield (rendered against the real live fixture)", () => {
  it("renders the lane bar with 3 cards and marks the active lane", async () => {
    render(<LanesView density="airy" />);
    await screen.findAllByText("api-standard");
    expect(screen.getAllByText("max-abo").length).toBeGreaterThan(0);
    expect(screen.getAllByText("Premium").length).toBeGreaterThan(0);
    // active lane carries the „Aktiv" eyebrow
    expect(screen.getAllByText("Aktiv").length).toBeGreaterThan(0);
    // ghost card for a new lane
    expect(screen.getAllByText("Neue Lane").length).toBeGreaterThan(0);
    expect(loadLanesMock).toHaveBeenCalledTimes(1);
  });

  it("renders one matrix row per catalog profile (10)", async () => {
    render(<LanesView density="airy" />);
    await screen.findAllByText("coder");
    for (const profile of [
      "admin",
      "coder",
      "critic",
      "family-ui",
      "fo-brain",
      "premium",
      "research",
      "reviewer",
      "scout",
      "verifier",
    ]) {
      expect(screen.getAllByText(profile).length).toBeGreaterThan(0);
    }
    // SaveBar persists + activates, with the spawn hint
    expect(screen.getAllByText("Speichern + aktivieren").length).toBeGreaterThan(0);
    expect(screen.getAllByText("wirkt ab dem nächsten Spawn").length).toBeGreaterThan(0);
  });

  it("disables Reasoning with a hint when the model has no support (fixture has none)", async () => {
    render(<LanesView density="airy" />);
    // the captured payload predates reasoning_support → every row is disabled + hinted
    const hints = await screen.findAllByText("Modell hat keinen Reasoning-Knopf");
    expect(hints.length).toBeGreaterThanOrEqual(10);
  });

  it("shows the Rauch and Kompass subtabs", async () => {
    render(<LanesView density="airy" />);
    await screen.findAllByText("Rauch");
    expect(screen.getAllByText("Kompass").length).toBeGreaterThan(0);
  });

  it("renders the Rauch panel with the catalog-probe CTA for the sinnvoll set", async () => {
    render(<LanesView density="airy" />);
    // fixture has no sinnvoll field → curated heuristic = the 5 claude-cli models
    await screen.findAllByText("Katalog messen · 5 sinnvolle Modelle");
    // empty-state doctrine: situation → bewertung → aktion (no ok-green)
    expect(screen.getAllByText("Noch keine Messungen").length).toBeGreaterThan(0);
  });

  it("switches to the Kompass and renders the fit ranking for a role", async () => {
    render(<LanesView density="airy" />);
    await screen.findAllByText("Rauch");
    fireEvent.click(screen.getAllByText("Kompass")[0]);
    await screen.findAllByText("Top-Modelle für diese Rolle");
    // role subtabs render against the curated set
    expect(screen.getAllByText("Coder").length).toBeGreaterThan(0);
    expect(screen.getAllByText("Reviewer").length).toBeGreaterThan(0);
  });

  it("logs no console errors while loading and rendering", async () => {
    render(<LanesView density="airy" />);
    await screen.findAllByText("api-standard");
    expect(consoleError).not.toHaveBeenCalled();
  });
});
