// @vitest-environment jsdom

import { cleanup, fireEvent, render, screen, waitFor } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import { LaneQuickSwitch } from "./LaneQuickSwitch";
import { loadLanes, smokeCheckLaneConfig, updateLane } from "../lanes/api";

vi.mock("../lanes/api", async (importOriginal) => {
  const actual = await importOriginal<typeof import("../lanes/api")>();
  return {
    ...actual,
    loadLanes: vi.fn(),
    smokeCheckLaneConfig: vi.fn(),
    updateLane: vi.fn(),
  };
});

const models = [
  { id: "openai/gpt-4.1-mini", label: "GPT 4.1 Mini", runtime: "hermes" as const, provider: "openrouter", group: "API" },
  { id: "qwen/qwen3.7-max", label: "Qwen 3.7 Max", runtime: "hermes" as const, provider: "openrouter", group: "API" },
  { id: "qwen/qwen3.7-max", label: "Qwen 3.7 Max", runtime: "hermes" as const, provider: "neuralwatt", group: "API" },
];

function lanes(updatedAt: number, model = "openai/gpt-4.1-mini") {
  return {
    lanes: [
      {
        id: "fast",
        name: "Fast lane",
        active: true,
        builtin: false,
        created_at: 1,
        updated_at: updatedAt,
        profiles: {
          coder: {
            worker_runtime: "hermes" as const,
            provider: "openrouter",
            model,
          },
        },
      },
    ],
    count: 1,
    active_id: "fast",
    profiles: [
      {
        name: "coder",
        worker_runtime: "hermes" as const,
        default_provider: "openrouter",
        default_model: "openai/gpt-4.1-mini",
        fallback_providers: [],
        description: "Coder lane",
      },
    ],
    models,
  };
}

describe("LaneQuickSwitch", () => {
  beforeEach(() => {
    vi.mocked(loadLanes).mockReset();
    vi.mocked(smokeCheckLaneConfig).mockReset();
    vi.mocked(updateLane).mockReset();
  });

  afterEach(() => {
    cleanup();
  });

  it("runs spawn-check and saves the full profiles map via PUT", async () => {
    vi.mocked(loadLanes)
      .mockResolvedValueOnce(lanes(1))
      .mockResolvedValueOnce(lanes(1))
      .mockResolvedValueOnce(lanes(2, "qwen/qwen3.7-max"));
    vi.mocked(smokeCheckLaneConfig).mockResolvedValue({
      status: "healthy",
      dispatcher_path: "hermes",
      resolved_model: "qwen/qwen3.7-max",
    });
    vi.mocked(updateLane).mockResolvedValue({ lane: lanes(2).lanes[0] });

    render(<LaneQuickSwitch />);

    await screen.findByText("Fast lane");
    fireEvent.change(screen.getByLabelText("Modell"), { target: { value: "hermes|openrouter|qwen/qwen3.7-max" } });
    fireEvent.click(screen.getByRole("button", { name: "Modell speichern" }));

    await waitFor(() => expect(smokeCheckLaneConfig).toHaveBeenCalledWith("coder", {
      worker_runtime: "hermes",
      provider: "openrouter",
      model: "qwen/qwen3.7-max",
    }));
    await waitFor(() => expect(updateLane).toHaveBeenCalledWith("fast", {
      profiles: {
        coder: {
          worker_runtime: "hermes",
          provider: "openrouter",
          model: "qwen/qwen3.7-max",
          fallback_providers: [],
        },
      },
    }));
    expect(await screen.findByText("Lane gespeichert; gilt ab dem nächsten Worker-Spawn.")).toBeTruthy();
  });

  it("reloads and refuses to overwrite when the active lane changed concurrently", async () => {
    vi.mocked(loadLanes)
      .mockResolvedValueOnce(lanes(1))
      .mockResolvedValueOnce(lanes(2, "openai/gpt-4.1-mini"));

    render(<LaneQuickSwitch />);

    await screen.findByText("Fast lane");
    fireEvent.change(screen.getByLabelText("Modell"), { target: { value: "hermes|openrouter|qwen/qwen3.7-max" } });
    fireEvent.click(screen.getByRole("button", { name: "Modell speichern" }));

    expect(await screen.findByText("Aktive Lane wurde parallel geändert — neu geladen. Bitte Auswahl prüfen und erneut speichern.")).toBeTruthy();
    expect(smokeCheckLaneConfig).not.toHaveBeenCalled();
    expect(updateLane).not.toHaveBeenCalled();
  });
});
