// @vitest-environment jsdom

import { cleanup, fireEvent, render, screen, waitFor, within } from "@testing-library/react";
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

import { AktivitaetTab, NodeDetailContent, NodeDetailDrawer, UebersichtTab } from "./NodeDetailDrawer";
import { useTaskBodyOnDemand } from "../../hooks/taskBodyOnDemand";
import { useHermesReviewVerdicts } from "../../hooks/reviewVerdicts";
import { useWorkerActivity } from "../../hooks/workersBoard";

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

  /** Row-local KV assertion: label and value must share the same .fleet-kv group. */
  function kvValue(label: string): string {
    const key = screen.getByText(label, { exact: true });
    expect(key.className).toContain("fleet-kv-k");
    const row = key.closest(".fleet-kv");
    expect(row).toBeTruthy();
    const value = row!.querySelector(".fleet-kv-v");
    expect(value).toBeTruthy();
    return (value!.textContent ?? "").trim();
  }

  it("beschriftet Task-Lane, Laufprofil und Modell als getrennte KV-Zeilen", () => {
    // Assignee ≠ profile ≠ active_model, damit vertauschtes Mapping sichtbar wird.
    render(
      <UebersichtTab
        now={1782508100}
        task={{ id: "t1", title: "T", status: "running", assignee: "premium", body: null }}
        latestRun={{
          profile: "coder-frontend",
          status: "running",
          runtime_seconds: 60,
          active_model: "anthropic/claude-opus-5",
        }}
        elapsedSec={60}
        deliverables={[]}
      />,
    );

    expect(kvValue("Task-Lane")).toBe("premium");
    expect(kvValue("Laufprofil")).toBe("coder-frontend");
    expect(kvValue("Modell")).toBe("anthropic/claude-opus-5");
    // Modellroute bleibt eigenes Feld (Telemetrie-Warnung, kein Ersatz für Modell-KV).
    expect(screen.getByText("Modell unbekannt – Telemetrie fehlt")).toBeTruthy();
  });

  it("zeigt Modell als — wenn keine Modelltelemetrie vorliegt und mischt keine Lane/Profil-Werte ein", () => {
    render(
      <UebersichtTab
        now={1782508100}
        task={{ id: "t2", title: "T2", status: "running", assignee: "premium", body: null, model_override: null }}
        latestRun={{
          profile: "coder-frontend",
          status: "running",
          runtime_seconds: 30,
          active_model: null,
          requested_model: null,
        }}
        elapsedSec={30}
        deliverables={[]}
      />,
    );

    expect(kvValue("Task-Lane")).toBe("premium");
    expect(kvValue("Laufprofil")).toBe("coder-frontend");
    const modelVal = kvValue("Modell");
    expect(modelVal).toBe("—");
    expect(modelVal).not.toContain("premium");
    expect(modelVal).not.toContain("coder-frontend");
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

    expect(screen.getByText("Task-Lane")).toBeTruthy();
    expect(screen.getAllByText("premium-reviewer")).toHaveLength(1);
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

  it("lädt Fremd-Board-Details explizit und unterdrückt alle Steuerungsaktionen", () => {
    const task = hookState.taskBody.data.task as typeof hookState.taskBody.data.task & {
      operator_question?: boolean;
    };
    const original = task.operator_question;
    task.operator_question = true;
    try {
      render(
        <NodeDetailDrawer
          taskId="t_reassign"
          board="archive"
          readOnly
          chainNodes={[]}
          now={1782508100}
          onClose={vi.fn()}
        />,
      );

      expect(useTaskBodyOnDemand).toHaveBeenCalledWith("t_reassign", "archive");
      expect(useWorkerActivity).toHaveBeenCalledWith("t_reassign", "archive");
      expect(useHermesReviewVerdicts).toHaveBeenCalledWith("archive");
      expect(screen.queryByRole("button", { name: "Profil ändern" })).toBeNull();
      expect(screen.queryByText("Antwort senden")).toBeNull();
    } finally {
      task.operator_question = original;
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

// ─── LK-3: Ketten-Kontext in der Detailansicht ────────────────────────────

describe("LK-3 Ketten-Kontext in der Detailansicht", () => {
  afterEach(() => {
    cleanup();
    vi.clearAllMocks();
  });

  const CHAIN_CONTEXT = {
    root_id: "t_chain0",
    chain_identifier: "LK-SEED Demo-Kette",
    chain_state: "laeuft",
    position: 2,
    total: 5,
    previous_station: { id: "t_chain1", title: "Erste Station" },
    next_station: { id: "t_chain3", title: "Dritte Station" },
  };

  function renderUebersicht(task: Record<string, unknown>, onNavigateTask?: (id: string) => void) {
    return render(
      <UebersichtTab
        now={1_783_800_300}
        task={task as never}
        latestRun={null}
        elapsedSec={null}
        deliverables={[]}
        onNavigateTask={onNavigateTask}
      />,
    );
  }

  it("zeigt Kette, Kettenzustand und Position im Kopf der Ansicht (AC-1)", () => {
    renderUebersicht(
      { id: "t_chain2", title: "Zweite Station", status: "running", body: null, chain_context: CHAIN_CONTEXT },
      vi.fn(),
    );
    const section = screen.getByLabelText("Ketten-Kontext");
    expect(within(section).getByText("Läuft · Station 2 von 5")).toBeTruthy();
    expect(within(section).getByText("LK-SEED Demo-Kette")).toBeTruthy();
  });

  it("zeigt die Stationsfolge als kompakte Leiste mit hervorgehobener aktueller Station (AC-2)", () => {
    renderUebersicht(
      { id: "t_chain2", title: "Zweite Station", status: "running", body: null, chain_context: CHAIN_CONTEXT },
      vi.fn(),
    );
    const strip = screen.getByRole("list", { name: "Stationsfolge" });
    const segments = within(strip).getAllByRole("listitem");
    expect(segments).toHaveLength(5);
    expect(segments[0].className).toContain("is-done");
    expect(segments[1].className).toContain("is-now");
    expect(segments[1].getAttribute("aria-current")).toBe("step");
    expect(segments[2].className).toContain("is-open");
    expect(segments[4].className).toContain("is-open");
  });

  it("Vorgänger und Nachfolger sind ohne Listen-Umweg erreichbar (AC-3)", () => {
    const onNavigate = vi.fn();
    renderUebersicht(
      { id: "t_chain2", title: "Zweite Station", status: "running", body: null, chain_context: CHAIN_CONTEXT },
      onNavigate,
    );
    fireEvent.click(screen.getByRole("button", { name: /Erste Station/ }));
    expect(onNavigate).toHaveBeenCalledWith("t_chain1");
    fireEvent.click(screen.getByRole("button", { name: /Dritte Station/ }));
    expect(onNavigate).toHaveBeenCalledWith("t_chain3");
  });

  it("entfällt vollständig bei einer Karte ohne Kette — keine leere Hülle (AC-4)", () => {
    renderUebersicht({ id: "t1", title: "Solo-Task", status: "running", body: null });
    expect(screen.queryByLabelText("Ketten-Kontext")).toBeNull();
    expect(screen.queryByRole("list", { name: "Stationsfolge" })).toBeNull();
  });

  it("rendert keine Kette der Länge eins (AC-4)", () => {
    renderUebersicht(
      {
        id: "t1", title: "Solo", status: "running", body: null,
        chain_context: { ...CHAIN_CONTEXT, position: 1, total: 1, previous_station: null, next_station: null },
      },
      vi.fn(),
    );
    expect(screen.queryByLabelText("Ketten-Kontext")).toBeNull();
  });

  it("navigiert ohne externen Handler intern zur Nachbarstation (AC-3, Desktop-Pane)", () => {
    const original = hookState.taskBody.data;
    (hookState.taskBody as { data: unknown }).data = {
      ...original,
      task: { id: "t_chain2", title: "Zweite Station", status: "running", body: null, chain_context: CHAIN_CONTEXT },
    };
    try {
      render(
        <NodeDetailContent
          taskId="t_chain2"
          chainNodes={[]}
          now={1_783_800_300}
          onClose={() => {}}
        />,
      );
      fireEvent.click(screen.getByRole("button", { name: /Dritte Station/ }));
      expect(vi.mocked(useTaskBodyOnDemand).mock.calls.some((call) => call[0] === "t_chain3")).toBe(true);
    } finally {
      (hookState.taskBody as { data: unknown }).data = original;
    }
  });

  it("behält nach dem Ketten-Einbau alle heutigen Angaben der Detailansicht (AC-5/AC-6)", () => {
    render(
      <UebersichtTab
        now={1_783_800_300}
        task={{
          id: "t_full",
          title: "Volle Karte",
          status: "blocked",
          assignee: "premium",
          body: "Auftragsbeschreibung mit Inhalt",
          review_tier: "review",
          block_reason: "Wartet auf Operator",
          priority: 5,
          branch_name: "kanban/t_full",
          workspace_path: "/tmp/ws/t_full",
          model_override: "k3",
          acceptance_criteria: ["Kriterium eins", "Kriterium zwei"],
          created_at: 1_783_800_000,
          started_at: 1_783_800_100,
          completed_at: null,
          archived_at: null,
          due_at: 1_783_900_000,
          last_heartbeat_at: 1_783_800_200,
          diagnostics: [{ kind: "warn", severity: "warn", title: "Befund X", detail: "Detail Y" }],
          chain_context: CHAIN_CONTEXT,
        } as never}
        latestRun={{
          // MUSS sich von task.assignee ("premium") unterscheiden: die
          // Laufprofil-Zeile zeigt dieses Feld, die Task-Lane-Zeile den
          // assignee. Sind beide gleich, findet das getByText("premium")
          // weiter unten zwei Knoten und wirft "Found multiple elements".
          profile: "coder-frontend",
          status: "running",
          runtime_seconds: 90,
          cost_usd: 0.42,
          requested_model: "k3",
          active_model: "claude-sonnet",
          input_tokens: 1200,
          output_tokens: 3400,
        } as never}
        elapsedSec={90}
        deliverables={[]}
        onNavigateTask={vi.fn()}
      />,
    );
    // Ketten-Kontext (neu)
    expect(screen.getByLabelText("Ketten-Kontext")).toBeTruthy();
    // Status + Review-Tier
    expect(screen.getByText("blocked")).toBeTruthy();
    expect(screen.getByText("review")).toBeTruthy();
    // Blockiergrund
    expect(screen.getByText(/Wartet auf Operator/)).toBeTruthy();
    // Zuweisung, Modell, Laufzeit, Kosten
    expect(screen.getByText("Task-Lane")).toBeTruthy();
    expect(screen.getByText("premium")).toBeTruthy();
    expect(screen.getByText("Laufprofil")).toBeTruthy();
    expect(screen.getByText("claude-sonnet")).toBeTruthy();
    expect(screen.getByText("Laufzeit")).toBeTruthy();
    expect(screen.getByText("Kosten")).toBeTruthy();
    expect(screen.getByText("$0,42")).toBeTruthy();
    // Branch, Modell-Route, Tokens, Workspace
    expect(screen.getByText("Branch")).toBeTruthy();
    expect(screen.getByText(/kanban\/t_full/)).toBeTruthy();
    expect(screen.getByText("Modellroute")).toBeTruthy();
    expect(screen.getByText("Tokens")).toBeTruthy();
    expect(screen.getByText(/1,2k/)).toBeTruthy();
    expect(screen.getByText(/3,4k/)).toBeTruthy();
    expect(screen.getByText("Workspace")).toBeTruthy();
    // Zeitstempel
    for (const label of ["Erstellt", "Gestartet", "Fällig", "Heartbeat"]) {
      expect(screen.getByText(label)).toBeTruthy();
    }
    // Priorität, Diagnostik, Body, Abnahmekriterien
    expect(screen.getByText("Priorität")).toBeTruthy();
    expect(screen.getByText("Diagnostik")).toBeTruthy();
    expect(screen.getByText("Befund X")).toBeTruthy();
    expect(screen.getByText("Detail Y")).toBeTruthy();
    expect(screen.getByText("Beschreibung")).toBeTruthy();
    expect(screen.getByText(/Auftragsbeschreibung mit Inhalt/)).toBeTruthy();
    expect(screen.getByText("Acceptance-Criteria")).toBeTruthy();
    expect(screen.getByText("Kriterium eins")).toBeTruthy();
    expect(screen.getByText("Kriterium zwei")).toBeTruthy();
  });
});
