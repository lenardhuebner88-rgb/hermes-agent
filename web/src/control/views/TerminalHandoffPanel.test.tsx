// @vitest-environment jsdom
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { cleanup, configure, fireEvent, render, screen, waitFor } from "@testing-library/react";

// Unter Voll-Suite-Last fällt die async Capture-/Validate-/Ingest-Kette (React-
// Re-Render nach Mock-Resolve) hinter das Standard-budget zurück. Die Datei hat
// absichtlich ein 30-s-Testbudget für mehrere sequentielle async-Schritte; das
// waitFor-Budget muss dieses Budget unter Voll-Suite-CPU-Druck ebenfalls nutzen.
// Damit bleibt der Timeout file-spezifisch, ohne Assertions abzuschwächen.
configure({ asyncUtilTimeout: 30000 });
vi.setConfig({ testTimeout: 30000 });

const { captureMock, fetchJSONMock } = vi.hoisted(() => ({
  captureMock: vi.fn(),
  fetchJSONMock: vi.fn(),
}));

vi.mock("@/lib/api", () => {
  return {
    api: { captureAgentTerminalWindow: captureMock },
    fetchJSON: fetchJSONMock,
  };
});

// This behavior suite does not exercise the shared animated primitive library
// or Lucide's icon implementation. Keep those large UI-only imports out of the
// worker so full-suite CPU pressure cannot delay this file's async assertions.
vi.mock("../components/primitives", () => ({
  Eyebrow: ({ children }: { children: React.ReactNode }) => <p>{children}</p>,
}));

vi.mock("lucide-react", () => {
  const Icon = () => null;
  return {
    AlertTriangle: Icon,
    CheckCircle2: Icon,
    ClipboardList: Icon,
    FileText: Icon,
    Play: Icon,
    X: Icon,
  };
});

import { TerminalHandoffPanel } from "./TerminalHandoffPanel";

const target = { session: "hermes-agents", window: "hermes", terminal_run_id: "tr_wave2" };

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
    const draft = screen.getByLabelText("PlanSpec-Draft") as HTMLTextAreaElement;
    await waitFor(() => expect(draft.value).not.toBe(""));

    fireEvent.click(screen.getByRole("button", { name: /^Validieren/ }));

    await waitFor(() => expect(fetchJSONMock).toHaveBeenCalled());
    const [url, init] = fetchJSONMock.mock.calls[0];
    expect(url).toBe("/api/plugins/kanban/planspecs/validate");
    expect(JSON.parse(init.body)).toMatchObject({
      terminal_run_id: "tr_wave2",
      draft: { body: expect.stringContaining("freigabe: operator") },
    });
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
    const draft = screen.getByLabelText("PlanSpec-Draft") as HTMLTextAreaElement;
    await waitFor(() => expect(draft.value).not.toBe(""));

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
    const draft = screen.getByLabelText("PlanSpec-Draft") as HTMLTextAreaElement;
    await waitFor(() => expect(draft.value).not.toBe(""));

    fireEvent.click(screen.getByRole("button", { name: /^Ingest/ }));

    expect(await screen.findByText(/Ingested mit 2 Rubrik-Warnungen/)).toBeTruthy();
    expect(screen.getByText("done-when fehlt in Slice 'Compile children'")).toBeTruthy();
    expect(screen.getByText("kein AC referenziert")).toBeTruthy();
  });

  it("creates a held Kanban task via the existing task API (AC-5)", async () => {
    fetchJSONMock.mockResolvedValue({ task: { id: "t_triage1", status: "triage" } });
    renderPanel();
    fireEvent.click(screen.getByRole("button", { name: /Kanban-Triage/ }));
    fireEvent.click(screen.getByRole("button", { name: "Auswahl übernehmen" }));
    await waitFor(() => screen.getByLabelText("Triage-Aufgabentext"));

    fireEvent.click(screen.getByRole("button", { name: /Triage-Task anlegen/ }));

    await waitFor(() => expect(fetchJSONMock).toHaveBeenCalled());
    const [url, init] = fetchJSONMock.mock.calls[0];
    expect(url).toBe("/api/plugins/kanban/tasks");
    expect(JSON.parse(init.body)).toMatchObject({
      status: "scheduled",
      freigabe: "operator",
      live_test_depth: "contract",
    });
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

  it("submits an isolated-write candidate only through the held intake endpoint", async () => {
    fetchJSONMock.mockResolvedValue({
      root_task_id: "t_root",
      intake_task_id: "t_intake",
      imported_commit: "a".repeat(40),
      idempotent: false,
    });
    render(
      <TerminalHandoffPanel
        target={{ ...target, terminal_run_id: "tr_candidate" }}
        getSelection={() => ""}
        onClose={() => {}}
      />,
    );

    fireEvent.click(screen.getByRole("button", { name: "Kandidaten gehalten einreichen" }));

    await waitFor(() => expect(fetchJSONMock).toHaveBeenCalledTimes(1));
    const [url, options] = fetchJSONMock.mock.calls[0];
    expect(url).toBe("/api/plugins/kanban/terminal-candidates/submit");
    expect(JSON.parse(options.body)).toEqual({ terminal_run_id: "tr_candidate" });
    expect(await screen.findByText(/Root t_root/)).toBeTruthy();
    expect(fetchJSONMock.mock.calls.some(([calledUrl]) =>
      String(calledUrl).includes("integrate"),
    )).toBe(false);
  });
});
