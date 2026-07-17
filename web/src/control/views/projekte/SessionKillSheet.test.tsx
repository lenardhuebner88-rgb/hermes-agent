// @vitest-environment jsdom
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { cleanup, fireEvent, render, screen, waitFor } from "@testing-library/react";
import type { ProjectAgent } from "../../lib/schemas";

const apiMock = vi.hoisted(() => ({
  terminateAgentTerminalWindow: vi.fn(),
}));

vi.mock("@/lib/api", async () => {
  const actual = await vi.importActual<typeof import("@/lib/api")>("@/lib/api");
  return { ...actual, api: apiMock };
});

import { SessionKillSheet } from "./SessionKillSheet";

const KILLABLE_AGENT: ProjectAgent = {
  kind: "kimi",
  label: "work:2 kimi",
  task: null,
  project: "hermes-infra",
  since: 1784238000,
  source: "tmux",
  tmux_session: "work",
  tmux_window: "2",
  assignee: null,
  operator: null,
};

function renderSheet(overrides: Partial<Parameters<typeof SessionKillSheet>[0]> = {}) {
  const props = {
    agent: KILLABLE_AGENT,
    projectName: "Hermes Infra",
    now: 1784238000 + 3600 * 35,
    onClose: vi.fn(),
    onKilled: vi.fn(),
    ...overrides,
  };
  render(<SessionKillSheet {...props} />);
  return props;
}

describe("SessionKillSheet", () => {
  beforeEach(() => {
    apiMock.terminateAgentTerminalWindow.mockReset();
  });
  afterEach(() => cleanup());

  it("names the exact victim: label, tmux target, project, warning", () => {
    renderSheet();
    expect(screen.getByText("work:2 kimi")).toBeTruthy();
    expect(screen.getByText("tmux work:2")).toBeTruthy();
    expect(screen.getByText("Hermes Infra")).toBeTruthy();
    expect(screen.getByText(/Das tmux-Fenster wird geschlossen/)).toBeTruthy();
    expect(screen.getByRole("button", { name: "Session beenden" })).toBeTruthy();
    expect(screen.getByRole("button", { name: "Abbrechen" })).toBeTruthy();
  });

  it("confirm terminates with (session, window, external=true), then closes + reloads", async () => {
    apiMock.terminateAgentTerminalWindow.mockResolvedValue({ ok: true });
    const props = renderSheet();
    fireEvent.click(screen.getByRole("button", { name: "Session beenden" }));
    await waitFor(() =>
      expect(apiMock.terminateAgentTerminalWindow).toHaveBeenCalledWith("work", "2", true),
    );
    await waitFor(() => expect(props.onKilled).toHaveBeenCalledTimes(1));
    expect(props.onClose).toHaveBeenCalledTimes(1);
  });

  it("cancel closes without touching the API", () => {
    const props = renderSheet();
    fireEvent.click(screen.getByRole("button", { name: "Abbrechen" }));
    expect(props.onClose).toHaveBeenCalledTimes(1);
    expect(apiMock.terminateAgentTerminalWindow).not.toHaveBeenCalled();
  });

  it("keeps the sheet open and surfaces the server detail on failure", async () => {
    apiMock.terminateAgentTerminalWindow.mockRejectedValueOnce(
      new Error('409: {"detail":"window work:2 is not a dashboard-managed agent window"}'),
    );
    const props = renderSheet();
    fireEvent.click(screen.getByRole("button", { name: "Session beenden" }));
    await waitFor(() =>
      expect(screen.getByRole("alert").textContent).toContain(
        "window work:2 is not a dashboard-managed agent window",
      ),
    );
    expect(screen.getByRole("alert").textContent).toContain("Session konnte nicht beendet werden.");
    expect(props.onClose).not.toHaveBeenCalled();
    expect(props.onKilled).not.toHaveBeenCalled();
    // Buttons usable again after the failure (busy cleared).
    expect((screen.getByRole("button", { name: "Session beenden" }) as HTMLButtonElement).disabled).toBe(false);
  });

  it("ignores Escape/backdrop-close while the terminate request is in flight (Fable obs. 3)", async () => {
    let resolveTerminate: ((value: { ok: boolean }) => void) | undefined;
    apiMock.terminateAgentTerminalWindow.mockImplementationOnce(
      () => new Promise((resolve) => { resolveTerminate = resolve; }),
    );
    const props = renderSheet();
    fireEvent.click(screen.getByRole("button", { name: "Session beenden" }));
    // In flight: Escape must NOT close (error feedback would be lost).
    fireEvent.keyDown(window, { key: "Escape" });
    expect(props.onClose).not.toHaveBeenCalled();
    resolveTerminate?.({ ok: true });
    await waitFor(() => expect(props.onKilled).toHaveBeenCalledTimes(1));
    expect(props.onClose).toHaveBeenCalledTimes(1); // success-path close
  });

  it("renders nothing for a row without a structured kill target", () => {
    const { container } = render(
      <SessionKillSheet
        agent={{ ...KILLABLE_AGENT, tmux_session: null, tmux_window: null }}
        projectName={null}
        now={1784238000}
        onClose={vi.fn()}
        onKilled={vi.fn()}
      />,
    );
    expect(container.innerHTML).toBe("");
    expect(apiMock.terminateAgentTerminalWindow).not.toHaveBeenCalled();
  });
});
