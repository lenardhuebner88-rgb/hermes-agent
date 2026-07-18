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
      error: null as string | null,
      errorObj: null,
      isStale: false,
      lastUpdated: 1782508000,
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

vi.mock("../../hooks/taskBodyOnDemand", async () => {
  const actual = await vi.importActual<typeof import("../../hooks/taskBodyOnDemand")>("../../hooks/taskBodyOnDemand");
  return {
    ...actual,
    useTaskBodyOnDemand: vi.fn(() => hookState.taskBody),
    useTaskDeliverablesOnDemand: vi.fn(() => ({ data: { deliverables: [] }, loading: false, error: null })),
  };
});
vi.mock("../../hooks/workersBoard", async () => {
  const actual = await vi.importActual<typeof import("../../hooks/workersBoard")>("../../hooks/workersBoard");
  return {
    ...actual,
    useWorkerActivity: vi.fn(() => ({ data: { events: [] }, loading: false, error: null })),
  };
});
vi.mock("../../hooks/reviewVerdicts", async () => {
  const actual = await vi.importActual<typeof import("../../hooks/reviewVerdicts")>("../../hooks/reviewVerdicts");
  return {
    ...actual,
    useHermesReviewVerdicts: vi.fn(() => ({ data: { reviews: [] }, loading: false, error: null })),
  };
});
vi.mock("../../hooks/planSpecsLanes", async () => {
  const actual = await vi.importActual<typeof import("../../hooks/planSpecsLanes")>("../../hooks/planSpecsLanes");
  return {
    ...actual,
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

  it("verlinkt state.db-Worker-Sessions nicht als tmux-Terminal", () => {
    const workerSessionId = "20260717_233938_4da4c4";
    const tmuxSessionNames = new Set(["work"]);
    expect(tmuxSessionNames.has(workerSessionId)).toBe(false);

    const html = renderToStaticMarkup(
      <UebersichtTab
        now={1782508100}
        task={{ id: "t1", title: "T", status: "running" }}
        latestRun={{
          profile: "coder",
          status: "running",
          worker_session_id: workerSessionId,
        }}
        elapsedSec={10}
        deliverables={[]}
      />,
    );

    expect(html).not.toContain("/control/agent-terminals");
    expect(html).not.toContain("Im Terminal öffnen");
  });

  it("beschriftet Task-Lane und Laufprofil getrennt", () => {
    const html = renderToStaticMarkup(
      <UebersichtTab
        now={1782508100}
        task={{ id: "t1", title: "T", status: "running", assignee: "premium", body: null }}
        latestRun={{ profile: "premium", status: "running", runtime_seconds: 60 }}
        elapsedSec={60}
        deliverables={[]}
      />,
    );

    expect(html).toContain("Task-Lane");
    expect(html).toContain("Laufprofil");
    expect(html).toContain("premium");
    expect(html).toContain("Assignee");
    expect(html).toContain("Modell");
    expect(html).toContain("Modell unbekannt – Telemetrie fehlt");
  });

  it("rendert lange Taskbeschreibungen mit Wortumbruch im einzigen Sheet-Scroller", () => {
    const body = `## Auftrag\n${"x".repeat(500)}ENDE`;
    const html = renderToStaticMarkup(
      <UebersichtTab
        now={1782508100}
        task={{ id: "t1", title: "T", status: "running", assignee: "coder", body }}
        latestRun={{ profile: "coder", status: "running", runtime_seconds: 60 }}
        elapsedSec={60}
        deliverables={[]}
      />,
    );

    expect(html).not.toContain("overflow-y-auto");
    expect(html).not.toContain("max-h-40");
    expect(html).toContain("wrap-anywhere");
    expect(html).toContain("whitespace-pre-wrap");
    expect(html).toContain("ENDE");
    expect(html).not.toContain("mask-image");
  });

  it("names a contaminated runtime explicitly", () => {
    const html = renderToStaticMarkup(
      <UebersichtTab
        now={1782508100}
        task={{ id: "t1", title: "T", status: "running", assignee: "coder", body: null }}
        latestRun={{ profile: "coder", status: "running", runtime_seconds: Number.NaN }}
        elapsedSec={Number.NaN}
        deliverables={[]}
      />,
    );

    expect(html).toContain("Dauer ungültig");
  });

  it("names the lossless latest run state instead of inventing running or done", () => {
    const html = renderToStaticMarkup(
      <UebersichtTab
        now={1782508100}
        task={{ id: "t1", title: "T", status: "review", assignee: "verifier", body: null }}
        latestRun={{ profile: "verifier", status: "completed", runtime_seconds: 60 }}
        elapsedSec={60}
        deliverables={[]}
      />,
    );

    expect(html).toContain("Laufstatus");
    expect(html).toContain("Abgeschlossen (completed)");
  });

  it("shows former board-inline metadata and guards adversarial timestamps", () => {
    render(
      <UebersichtTab
        now={1_783_800_300}
        task={{
          id: "t_timebad", title: "Adversarial time card", status: "running", body: null,
          assignee: "premium-reviewer", priority: 7,
          created_at: 1_783_800_200, started_at: 1_783_800_100, completed_at: 1_783_800_050,
          archived_at: 1_783_800_250, due_at: 1_783_800_300 + 86_400,
          last_heartbeat_at: 1_783_800_300 * 1000,
        }}
        latestRun={null}
        elapsedSec={null}
        deliverables={[]}
      />,
    );

    expect(screen.getByText("Assignee")).toBeTruthy();
    expect(screen.getAllByText("premium-reviewer")).toHaveLength(2);
    expect(screen.getByText("Priorität")).toBeTruthy();
    expect(screen.getByText("7")).toBeTruthy();
    for (const label of ["Erstellt", "Gestartet", "Fertig", "Archiviert", "Fällig", "Heartbeat"]) {
      expect(screen.getByText(label)).toBeTruthy();
    }
    expect(screen.getByText("Zeit ungültig")).toBeTruthy();
    expect(screen.getByText(/zukünftig/)).toBeTruthy();
    expect(screen.getByText(/Start liegt vor Anlage/)).toBeTruthy();
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

  it("discloses a vanished task refresh while retaining the last detail", () => {
    hookState.taskBody.error = "404: task t_reassign not found";
    hookState.taskBody.isStale = true;
    renderDrawer();

    expect(screen.getByText("Task-Detail")).toBeTruthy();
    expect(screen.getByTitle("404: task t_reassign not found")).toBeTruthy();
    expect(screen.getByText("Task falsch profiliert")).toBeTruthy();

    hookState.taskBody.error = null;
    hookState.taskBody.isStale = false;
  });

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

  it("renders a scheduled task without runs with a neutral model-route badge", () => {
    const original = hookState.taskBody.data;
    (hookState.taskBody as { data: unknown }).data = {
      task: {
        id: "t_no_run",
        title: "Noch nicht gestarteter Task",
        status: "scheduled",
        assignee: "coder",
        body: null,
      },
      runs: [],
    };
    try {
      renderDrawer();

      const badge = screen.getByText("Noch kein Run");
      expect(badge.className).not.toContain("text-status-warn");
      expect(screen.queryByText("Modell unbekannt – Telemetrie fehlt")).toBeNull();
    } finally {
      (hookState.taskBody as { data: unknown }).data = original;
    }
  });

  it("bietet für einen laufenden Task keine deterministisch abgelehnte Profiländerung an", () => {
    const original = hookState.taskBody.data.task.status;
    hookState.taskBody.data.task.status = "running";
    try {
      renderDrawer();

      expect(screen.queryByLabelText("Zielprofil")).toBeNull();
      expect(screen.queryByRole("button", { name: "Profil ändern" })).toBeNull();
    } finally {
      hookState.taskBody.data.task.status = original;
    }
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
