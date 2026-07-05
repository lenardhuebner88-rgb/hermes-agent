// @vitest-environment jsdom
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { cleanup, fireEvent, render, screen, waitFor } from "@testing-library/react";

const { captureMock, fetchJSONMock } = vi.hoisted(() => ({
  captureMock: vi.fn(),
  fetchJSONMock: vi.fn(),
}));

vi.mock("@/lib/api", async () => {
  const actual = await vi.importActual<typeof import("@/lib/api")>("@/lib/api");
  return {
    ...actual,
    api: { captureAgentTerminalWindow: captureMock },
    fetchJSON: fetchJSONMock,
  };
});

import { TerminalHandoffPanel } from "./TerminalHandoffPanel";

const target = { session: "hermes-agents", window: "hermes" };

function renderPanel(getSelection: () => string = () => "echo hello\nworld") {
  return render(
    <TerminalHandoffPanel target={target} getSelection={getSelection} onClose={() => {}} />,
  );
}

beforeEach(() => {
  vi.clearAllMocks();
  captureMock.mockResolvedValue({ content: "tmux scrollback line" });
});

afterEach(() => {
  cleanup();
});

describe("TerminalHandoffPanel", () => {
  it("does nothing on mount and capturing a selection fires NO handoff network call (AC-2/AC-7 invariant)", async () => {
    renderPanel();
    // Rendering opens optional tooling only — no create/validate/ingest/dispatch.
    expect(fetchJSONMock).not.toHaveBeenCalled();
    expect(captureMock).not.toHaveBeenCalled();

    fireEvent.click(screen.getByRole("button", { name: "Auswahl übernehmen" }));

    // Selection capture is purely client-side: still zero network, nothing dispatched.
    await waitFor(() => {
      const ta = screen.getByLabelText("PlanSpec-Draft") as HTMLTextAreaElement;
      expect(ta.value).toContain("echo hello");
    });
    expect(fetchJSONMock).not.toHaveBeenCalled();
    expect(captureMock).not.toHaveBeenCalled();
  });

  it("captures the last N lines via the existing capture API (AC-1)", async () => {
    renderPanel();
    // Switch source to "last N lines" (second radio) and set N.
    fireEvent.click(screen.getAllByRole("radio")[1]);
    fireEvent.change(screen.getByRole("spinbutton"), { target: { value: "120" } });
    fireEvent.click(screen.getByRole("button", { name: "Letzte 120 Zeilen" }));

    await waitFor(() => expect(captureMock).toHaveBeenCalledWith("hermes-agents", "hermes", -120));
    await waitFor(() => {
      const ta = screen.getByLabelText("PlanSpec-Draft") as HTMLTextAreaElement;
      expect(ta.value).toContain("tmux scrollback line");
    });
    expect(fetchJSONMock).not.toHaveBeenCalled();
  });

  it("validates the draft via the existing validator endpoint and shows the result (AC-4/AC-6)", async () => {
    fetchJSONMock.mockResolvedValue({
      ok: false,
      disposition: "invalid",
      findings: ["taskgraph_hints.binding must be true"],
      would_block: true,
      freigabe: "operator",
    });
    renderPanel();
    fireEvent.click(screen.getByRole("button", { name: "Auswahl übernehmen" }));
    await waitFor(() => screen.getByLabelText("PlanSpec-Draft"));

    fireEvent.click(screen.getByRole("button", { name: /^Validieren/ }));

    await waitFor(() => expect(fetchJSONMock).toHaveBeenCalled());
    const [url, init] = fetchJSONMock.mock.calls[0];
    expect(url).toBe("/api/plugins/kanban/planspecs/validate");
    expect(JSON.parse(init.body)).toMatchObject({ slug: expect.any(String), content: expect.stringContaining("freigabe: operator") });
    expect(await screen.findByText(/Validate-Ergebnis: invalid/)).toBeTruthy();
    expect(screen.getByText(/taskgraph_hints.binding must be true/)).toBeTruthy();
  });

  it("ingests via the existing ingest path and surfaces the chain/task IDs (AC-4/AC-6)", async () => {
    fetchJSONMock.mockResolvedValue({
      ok: true,
      root_task_id: "t_root9",
      child_ids: ["t_c1", "t_c2"],
      subtask_count: 2,
      freigabe: "operator",
      live_test_depth: "smoke",
      rubric_warnings: [],
    });
    renderPanel();
    fireEvent.click(screen.getByRole("button", { name: "Auswahl übernehmen" }));
    await waitFor(() => screen.getByLabelText("PlanSpec-Draft"));

    fireEvent.click(screen.getByRole("button", { name: /^Ingest/ }));

    await waitFor(() => expect(fetchJSONMock).toHaveBeenCalledWith(
      "/api/plugins/kanban/planspecs/ingest-draft",
      expect.objectContaining({ method: "POST" }),
    ));
    expect(await screen.findByText(/t_root9/)).toBeTruthy();
    expect(screen.getByText(/t_c1, t_c2/)).toBeTruthy();
    expect(screen.queryByText(/Rubrik-Warnung/)).toBeNull();
  });

  it("shows rubric warnings after ingest when the endpoint reports non-empty rubric_warnings", async () => {
    fetchJSONMock.mockResolvedValue({
      ok: true,
      root_task_id: "t_root10",
      child_ids: ["t_c1"],
      subtask_count: 1,
      freigabe: "operator",
      live_test_depth: "smoke",
      rubric_warnings: ["done-when fehlt in Slice 'Compile children'", "kein AC referenziert"],
    });
    renderPanel();
    fireEvent.click(screen.getByRole("button", { name: "Auswahl übernehmen" }));
    await waitFor(() => screen.getByLabelText("PlanSpec-Draft"));

    fireEvent.click(screen.getByRole("button", { name: /^Ingest/ }));

    expect(await screen.findByText(/Ingested mit 2 Rubrik-Warnungen/)).toBeTruthy();
    expect(screen.getByText("done-when fehlt in Slice 'Compile children'")).toBeTruthy();
    expect(screen.getByText("kein AC referenziert")).toBeTruthy();
  });

  it("creates a Kanban triage task via the existing task API with triage=true (AC-5)", async () => {
    fetchJSONMock.mockResolvedValue({ task: { id: "t_triage1", status: "triage" } });
    renderPanel();
    fireEvent.click(screen.getByRole("button", { name: /Kanban-Triage/ }));
    fireEvent.click(screen.getByRole("button", { name: "Auswahl übernehmen" }));
    await waitFor(() => screen.getByLabelText("Triage-Aufgabentext"));

    fireEvent.click(screen.getByRole("button", { name: /Triage-Task anlegen/ }));

    await waitFor(() => expect(fetchJSONMock).toHaveBeenCalled());
    const [url, init] = fetchJSONMock.mock.calls[0];
    expect(url).toBe("/api/plugins/kanban/tasks");
    expect(JSON.parse(init.body)).toMatchObject({ triage: true });
    expect(await screen.findByText(/t_triage1/)).toBeTruthy();
  });

  it("dispatch preview ALWAYS uses dry_run=true — never a live dispatch (AC-2/AC-6)", async () => {
    fetchJSONMock.mockResolvedValue({ spawned: [["t_a", "coder", ""]], promoted: [], reclaimed: [] });
    renderPanel();

    fireEvent.click(screen.getByRole("button", { name: /dispatch --dry-run/ }));

    await waitFor(() => expect(fetchJSONMock).toHaveBeenCalled());
    // Every dispatch call this panel can make is a dry-run preview.
    for (const [url] of fetchJSONMock.mock.calls) {
      if (String(url).includes("/dispatch")) {
        expect(String(url)).toContain("dry_run=true");
      }
    }
    expect(await screen.findByText(/Würde dispatchen: 1/)).toBeTruthy();
  });
});
