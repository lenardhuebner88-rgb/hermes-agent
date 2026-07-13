// @vitest-environment jsdom

import { cleanup, fireEvent, render, screen, waitFor } from "@testing-library/react";
import { renderToStaticMarkup } from "react-dom/server";
import { afterEach, describe, expect, it, vi } from "vitest";

const { fetchJSONMock, hookState } = vi.hoisted(() => ({
  fetchJSONMock: vi.fn(),
  hookState: {
    taskBody: {
      data: {
        task: {
          id: "t_reassign",
          title: "Task falsch profiliert",
          status: "blocked",
          assignee: "coder",
          body: null,
        },
        runs: [],
      },
      loading: false,
      error: null,
    },
    lanesCatalog: {
      data: {
        lanes: [],
        count: 2,
        active_id: null,
        models: [],
        profiles: [
          { name: "coder", worker_runtime: "hermes", default_model: null, default_provider: null, description: "", locked: false, locked_reason: null },
          { name: "verifier", worker_runtime: "hermes", default_model: null, default_provider: null, description: "", locked: false, locked_reason: null },
        ],
      },
      loading: false,
      error: null,
      reload: vi.fn(),
    },
  },
}));

vi.mock("@/lib/api", async () => {
  const actual = await vi.importActual<typeof import("@/lib/api")>("@/lib/api");
  return { ...actual, fetchJSON: fetchJSONMock };
});

vi.mock("../../hooks/useControlData", async () => {
  const actual = await vi.importActual<typeof import("../../hooks/useControlData")>("../../hooks/useControlData");
  return {
    ...actual,
    useTaskBodyOnDemand: vi.fn(() => hookState.taskBody),
    useTaskDeliverablesOnDemand: vi.fn(() => ({ data: { deliverables: [] }, loading: false, error: null })),
    useWorkerActivity: vi.fn(() => ({ data: { events: [] }, loading: false, error: null })),
    useHermesReviewVerdicts: vi.fn(() => ({ data: { reviews: [] }, loading: false, error: null })),
    useLanesCatalog: vi.fn(() => hookState.lanesCatalog),
  };
});

import { AktivitaetTab, NodeDetailDrawer, UebersichtTab } from "./NodeDetailDrawer";

// Events im echten activity-Format (GET /tasks/{id}/activity → {events: [{id, kind, note, at}]}).
const baseEvents = [
  { id: 1, kind: "review_skipped_deterministic", note: null, at: 1782508000 },
  { id: 2, kind: "review_deferred_to_tip", note: "Kette läuft weiter", at: 1782507900 },
  { id: 3, kind: "claimed", note: null, at: 1782507800 },
];

describe("AktivitaetTab (NodeDetailDrawer)", () => {
  afterEach(() => {
    cleanup();
    vi.clearAllMocks();
  });

  it("maps review_skipped_deterministic to a positive-toned, human-readable chip", () => {
    render(<AktivitaetTab events={baseEvents} now={1782508100} loading={false} />);

    expect(screen.getByText("Gates-verifiziert (Review übersprungen)")).toBeTruthy();
    expect(screen.queryByText("review_skipped_deterministic")).toBeNull();
  });

  it("maps review_deferred_to_tip to a neutral-toned, human-readable chip", () => {
    render(<AktivitaetTab events={baseEvents} now={1782508100} loading={false} />);

    expect(screen.getByText("Urteil am Kettenende")).toBeTruthy();
    expect(screen.queryByText("review_deferred_to_tip")).toBeNull();
  });

  it("still renders unmapped kinds raw, unaffected by the new mapping", () => {
    render(<AktivitaetTab events={baseEvents} now={1782508100} loading={false} />);

    expect(screen.getByText("claimed")).toBeTruthy();
  });

  it("names a contaminated event timestamp instead of rendering a plausible dash", () => {
    render(<AktivitaetTab events={[{ id: 9, kind: "claimed", note: null, at: Number.NaN }]} now={1782508100} loading={false} />);

    expect(screen.getByText("Zeit ungültig")).toBeTruthy();
  });
});


describe("UebersichtTab mobile Lesbarkeit und Runtime-Semantik", () => {
  afterEach(() => {
    cleanup();
    vi.clearAllMocks();
  });

  it("beschriftet Task-Lane und Laufprofil getrennt", () => {
    const html = renderToStaticMarkup(
      <UebersichtTab
        task={{ id: "t1", title: "T", status: "running", assignee: "premium", body: null }}
        latestRun={{ profile: "premium", status: "running", runtime_seconds: 60 }}
        elapsedSec={60}
        deliverables={[]}
      />,
    );

    expect(html).toContain("Task-Lane");
    expect(html).toContain("Laufprofil");
    expect(html).toContain("premium");
    expect(html).not.toContain("Assignee");
    expect(html).not.toContain("Modell");
  });

  it("rendert lange Taskbeschreibungen mit Wortumbruch und eigener Scrollfläche statt hartem Abschneiden", () => {
    const body = `## Auftrag\n${"x".repeat(500)}ENDE`;
    const html = renderToStaticMarkup(
      <UebersichtTab
        task={{ id: "t1", title: "T", status: "running", assignee: "coder", body }}
        latestRun={{ profile: "coder", status: "running", runtime_seconds: 60 }}
        elapsedSec={60}
        deliverables={[]}
      />,
    );

    expect(html).toContain("overflow-y-auto");
    expect(html).toContain("wrap-anywhere");
    expect(html).toContain("whitespace-pre-wrap");
    expect(html).toContain("ENDE");
    expect(html).not.toContain("mask-image");
  });

  it("names a contaminated runtime explicitly", () => {
    const html = renderToStaticMarkup(
      <UebersichtTab
        task={{ id: "t1", title: "T", status: "running", assignee: "coder", body: null }}
        latestRun={{ profile: "coder", status: "running", runtime_seconds: Number.NaN }}
        elapsedSec={Number.NaN}
        deliverables={[]}
      />,
    );

    expect(html).toContain("Dauer ungültig");
  });
});

describe("NodeDetailDrawer Reassign", () => {
  afterEach(() => {
    cleanup();
    vi.clearAllMocks();
  });

  function renderDrawer(onChanged = vi.fn()) {
    render(
      <NodeDetailDrawer
        taskId="t_reassign"
        chainNodes={[{ id: "t_reassign", level: 0 } as never]}
        now={1782508100}
        onClose={vi.fn()}
        onChanged={onChanged}
      />,
    );
  }

  it("nutzt DrawerShell mit Dialog-Semantik und schließt per Escape", () => {
    const onClose = vi.fn();
    render(
      <NodeDetailDrawer
        taskId="t_reassign"
        chainNodes={[{ id: "t_reassign", level: 0 } as never]}
        now={1782508100}
        onClose={onClose}
      />,
    );

    const dialog = screen.getByRole("dialog", { name: "Task t_reassign Details" });
    expect(dialog.getAttribute("aria-modal")).toBe("true");
    fireEvent.keyDown(window, { key: "Escape" });
    expect(onClose).toHaveBeenCalledTimes(1);
  });

  it("armt Reassign und POSTet das echte Payload-Format", async () => {
    const onChanged = vi.fn();
    fetchJSONMock.mockResolvedValueOnce({ ok: true, task_id: "t_reassign", assignee: "verifier" });
    renderDrawer(onChanged);

    fireEvent.change(screen.getByLabelText("Zielprofil"), { target: { value: "verifier" } });
    fireEvent.click(screen.getByRole("button", { name: "Profil ändern" }));
    expect(fetchJSONMock).not.toHaveBeenCalled();

    fireEvent.click(screen.getByRole("button", { name: "Bestätigen" }));

    await waitFor(() => expect(fetchJSONMock).toHaveBeenCalledTimes(1));
    const [url, options] = fetchJSONMock.mock.calls[0];
    expect(url).toBe("/api/plugins/kanban/tasks/t_reassign/reassign");
    expect(options.method).toBe("POST");
    expect(JSON.parse(String(options.body))).toEqual({
      profile: "verifier",
      reclaim_first: false,
      reason: "Fleet Cockpit: Profil geändert",
    });
    await waitFor(() => expect(onChanged).toHaveBeenCalledTimes(1));
    expect(screen.getByText("Profil geändert: verifier")).toBeTruthy();
  });

  it("zeigt 409-Guard-Details wörtlich inline", async () => {
    fetchJSONMock.mockRejectedValueOnce(new Error(
      "409: {\"detail\":\"cannot reassign t_reassign: unknown id, or still running\"}",
    ));
    renderDrawer();

    fireEvent.change(screen.getByLabelText("Zielprofil"), { target: { value: "verifier" } });
    fireEvent.click(screen.getByRole("button", { name: "Profil ändern" }));
    fireEvent.click(screen.getByRole("button", { name: "Bestätigen" }));

    expect(await screen.findByText("cannot reassign t_reassign: unknown id, or still running")).toBeTruthy();
  });
});

describe("NodeDetailDrawer dependency action guard", () => {
  it("derives the Starten guard from authoritative parent states", () => {
    const original = hookState.taskBody.data;
    (hookState.taskBody as { data: unknown }).data = {
      task: { id: "t_child", title: "Child", status: "todo", assignee: "coder", body: null },
      runs: [],
      links: {
        parents: ["t_parent"],
        children: [],
        parent_states: [{ id: "t_parent", title: "Blocking parent", status: "running" }],
        child_states: [],
      },
    };
    try {
      render(
        <NodeDetailDrawer
          taskId="t_child"
          chainNodes={[]}
          now={1782508100}
          onClose={() => undefined}
        />,
      );
      expect(screen.queryByRole("button", { name: "Starten" })).toBeNull();
      expect(screen.getByText(/Blocking parent.*running/)).toBeTruthy();
    } finally {
      (hookState.taskBody as { data: unknown }).data = original;
    }
  });
});
