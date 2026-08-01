// @vitest-environment jsdom

import { cleanup, fireEvent, render, screen } from "@testing-library/react";
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

  it("renders title plus short id and opens the task detail", async () => {
    const onOpenTask = vi.fn();
    fetchMock.mockResolvedValueOnce(jsonResponse({
      autonomous: true,
      max_tier_autonomous: "gate",
      recent: [
        {
          task_id: "t_abc123",
          task_title: "Release-Pipeline reparieren",
          created_at: 1782508076,
          payload: { outcome: "rolled_back", detail: "post-deploy live test red" },
        },
      ],
      anchors: ["release/pre-deploy/20260701T000000", "release/pre-deploy/20260705T000000"],
    }));

    render(<AutoReleaseTile onOpenTask={onOpenTask} />);

    expect(await screen.findByText("AUTONOM (≤ gate)")).toBeTruthy();
    expect(screen.getByText("rolled_back")).toBeTruthy();
    expect(screen.getByText("Release-Pipeline reparieren")).toBeTruthy();
    expect(screen.getByText("t_abc123")).toBeTruthy();
    fireEvent.click(screen.getByRole("button", { name: /Release-Pipeline reparieren/ }));
    expect(onOpenTask).toHaveBeenCalledWith("t_abc123");
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

  it("compact keeps the mode/count contract when no held event exists", async () => {
    fetchMock.mockResolvedValueOnce(jsonResponse({
      autonomous: true,
      max_tier_autonomous: "review",
      recent: [
        { task_id: "t_dep1", created_at: 1782508076, payload: { outcome: "deployed" } },
        { task_id: "t_rb1", created_at: 1782508000, payload: { outcome: "rolled_back" } },
      ],
      anchors: [],
    }));

    render(<AutoReleaseTile compact />);

    expect(await screen.findByText("AUTONOM ≤ review")).toBeTruthy();
    expect(screen.getByText("2 letzte")).toBeTruthy();
    expect(screen.queryByText("Gehalten")).toBeNull();
  });

  it("compact shows the held chain with title, id, age and reason from the real payload shape", async () => {
    const now = Math.floor(Date.now() / 1000);
    fetchMock.mockResolvedValueOnce(jsonResponse({
      autonomous: true,
      max_tier_autonomous: "review",
      recent: [
        {
          task_id: "t_chain42",
          task_title: "Release-Kette warten",
          created_at: now - 3600,
          payload: { outcome: "held_live_test", detail: "ui-real requires operator approval" },
        },
      ],
      anchors: [],
    }));

    render(<AutoReleaseTile compact />);

    expect(await screen.findByText("Gehalten")).toBeTruthy();
    expect(screen.getByText("Release-Kette warten")).toBeTruthy();
    expect(screen.getByText(/t_chain42 · vor 1h/)).toBeTruthy();
    expect(screen.getByText("ui-real requires operator approval")).toBeTruthy();
  });

  it("compact picks the newest held_ event even when a later deployed event exists", async () => {
    const now = Math.floor(Date.now() / 1000);
    fetchMock.mockResolvedValueOnce(jsonResponse({
      autonomous: true,
      max_tier_autonomous: "review",
      recent: [
        { task_id: "t_newest", task_title: "Späteres Deploy", created_at: now - 60, payload: { outcome: "deployed" } },
        { task_id: "t_mid", task_title: "Mittlere Kette", created_at: now - 7200, payload: { outcome: "held_live_test", detail: "ui-real requires operator approval" } },
        { task_id: "t_old", task_title: "Alte Kette", created_at: now - 86400, payload: { outcome: "held_critical", detail: "critical red" } },
      ],
      anchors: [],
    }));

    render(<AutoReleaseTile compact />);

    expect(await screen.findByText("Mittlere Kette")).toBeTruthy();
    expect(screen.getByText(/t_mid · vor 2h/)).toBeTruthy();
    expect(screen.queryByText("Späteres Deploy")).toBeNull();
    expect(screen.queryByText("Alte Kette")).toBeNull();
  });

  it("compact falls back to id and outcome when title and detail are missing", async () => {
    const now = Math.floor(Date.now() / 1000);
    fetchMock.mockResolvedValueOnce(jsonResponse({
      autonomous: true,
      max_tier_autonomous: "review",
      recent: [
        { task_id: "t_bare9", created_at: now - 120, payload: { outcome: "held_critical" } },
      ],
      anchors: [],
    }));

    render(<AutoReleaseTile compact />);

    expect(await screen.findByText("t_bare9")).toBeTruthy();
    expect(screen.getByText(/t_bare9 · vor 2m/)).toBeTruthy();
    expect(screen.getByText("held_critical")).toBeTruthy();
  });
});
