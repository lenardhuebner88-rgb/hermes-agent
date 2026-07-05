// @vitest-environment jsdom

import { cleanup, render, screen } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import { AutoReleaseTile } from "./AutoReleaseTile";

function jsonResponse(body: unknown) {
  return new Response(JSON.stringify(body), {
    status: 200,
    headers: { "Content-Type": "application/json" },
  });
}

describe("AutoReleaseTile", () => {
  const fetchMock = vi.fn();

  beforeEach(() => {
    vi.clearAllMocks();
    Object.defineProperty(window, "__HERMES_SESSION_TOKEN__", {
      configurable: true,
      value: "test-token",
    });
    vi.stubGlobal("fetch", fetchMock);
  });

  afterEach(() => {
    cleanup();
    vi.unstubAllGlobals();
  });

  it("renders the kill-switch-off chip and empty state when nothing has released yet", async () => {
    fetchMock.mockResolvedValueOnce(jsonResponse({
      autonomous: false,
      max_tier_autonomous: "review",
      recent: [],
      anchors: [],
    }));

    render(<AutoReleaseTile />);

    expect(await screen.findByText("Kill-Switch AUS")).toBeTruthy();
    expect(screen.getByText("Noch keine autonomen Releases")).toBeTruthy();
  });

  it("renders a rolled_back outcome badge and the latest anchor", async () => {
    fetchMock.mockResolvedValueOnce(jsonResponse({
      autonomous: true,
      max_tier_autonomous: "gate",
      recent: [
        {
          task_id: "t_abc123",
          created_at: 1782508076,
          payload: { outcome: "rolled_back", detail: "post-deploy live test red" },
        },
      ],
      anchors: ["release/pre-deploy/20260701T000000", "release/pre-deploy/20260705T000000"],
    }));

    render(<AutoReleaseTile />);

    expect(await screen.findByText("AUTONOM (≤ gate)")).toBeTruthy();
    expect(screen.getByText("rolled_back")).toBeTruthy();
    expect(screen.getByText("t_abc123")).toBeTruthy();
    expect(screen.getByText("Anker: release/pre-deploy/20260705T000000")).toBeTruthy();
  });

  it("renders deployed and held_critical outcome tones distinctly", async () => {
    fetchMock.mockResolvedValueOnce(jsonResponse({
      autonomous: true,
      max_tier_autonomous: "gate",
      recent: [
        {
          task_id: "t_deployed1",
          created_at: 1782508076,
          payload: { outcome: "deployed" },
        },
        {
          task_id: "t_heldcrit1",
          created_at: 1782508000,
          payload: { outcome: "held_critical" },
        },
      ],
      anchors: [],
    }));

    render(<AutoReleaseTile />);

    expect(await screen.findByText("deployed")).toBeTruthy();
    expect(screen.getByText("held_critical")).toBeTruthy();
  });

  it("fails soft with a muted status line when the fetch rejects", async () => {
    fetchMock.mockRejectedValueOnce(new Error("network down"));

    render(<AutoReleaseTile />);

    expect(await screen.findByText("Status nicht erreichbar")).toBeTruthy();
  });
});
