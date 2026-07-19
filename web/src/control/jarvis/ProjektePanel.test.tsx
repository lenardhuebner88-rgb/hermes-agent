// @vitest-environment jsdom
/**
 * ProjektePanel — „PROJEKTE" der Jarvis-Shell (S2.6): rendert die echten
 * ProjectCards über dieselben Hooks/Polling-Keys wie ProjekteView (der
 * fetchJSON-Mock speist alle drei Endpoints durch den echten pollingStore,
 * Payloads im Backend-Format aus hermes_cli/projects_overview.py).
 * Erwartungen: Attention-Sortierung, Ampel-Badge/Grund-Chips, Kanban-Zähler,
 * Commit-/Live-Meta, Link-Ziele auf die Klassik, Empty-/Error-/Loading-State.
 */
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { cleanup, configure, render, screen } from "@testing-library/react";
import { MemoryRouter } from "react-router-dom";

import { _resetPollingStore } from "../hooks/pollingStore";

configure({ asyncUtilTimeout: 5000 });

const fetchJSONMock = vi.hoisted(() => vi.fn());

vi.mock("@/lib/api", async () => {
  const actual = await vi.importActual<typeof import("@/lib/api")>("@/lib/api");
  return {
    ...actual,
    fetchJSON: fetchJSONMock,
  };
});

import { ProjektePanel } from "./ProjektePanel";

// ── Fixtures im echten Backend-Shape (projects_overview.py) ────────────────
function fixtureProject(overrides: Record<string, unknown> = {}) {
  return {
    slug: "hermes-infra",
    name: "Hermes Infra",
    repo_path: "/home/piet/.hermes/hermes-agent",
    parent: null,
    kanban_project: "hermes",
    links: [{ label: "Control-Dashboard", url: "/control" }],
    last_commit: {
      hash: "9d8fa62d8",
      message: "jarvis: Shell-Einzug I — Fragen + Projekte",
      author: "kimi",
      committed_at: 1784237915,
      age_seconds: 600,
      attribution: null,
    },
    kanban: { open: 3, running: 1, blocked: 0, review: 2, done_7d: 41, needs_input: 0 },
    loops: { active: 0, packs: [] },
    errors: [],
    ...overrides,
  };
}

function fixtureAgent(overrides: Record<string, unknown> = {}) {
  return {
    kind: "kimi",
    label: "work:2 kimi",
    task: null,
    project: "hermes-infra",
    since: 1784238000,
    source: "tmux",
    tmux_session: "work",
    tmux_window: "2",
    tmux_window_name: "kimi",
    assignee: null,
    operator: null,
    session_id: null,
    task_id: null,
    ...overrides,
  };
}

/** URL-Dispatch für die drei Projekte-Endpoints (Envelope = Backend-Vertrag). */
function mockEndpoints({
  projects = [],
  agents = [],
  sessions = [],
  projectsError,
}: {
  projects?: Array<Record<string, unknown>>;
  agents?: Array<Record<string, unknown>>;
  sessions?: Array<Record<string, unknown>>;
  projectsError?: string;
} = {}) {
  fetchJSONMock.mockImplementation((url: string) => {
    if (url === "/api/projects") {
      return projectsError
        ? Promise.reject(new Error(projectsError))
        : Promise.resolve({ generated_at: 1784240000, registry_errors: [], projects });
    }
    if (url === "/api/projects/agents") {
      return Promise.resolve({ generated_at: 1784240000, errors: [], agents });
    }
    if (url === "/api/projects/sessions") {
      return Promise.resolve({ generated_at: 1784240000, errors: [], sessions });
    }
    return Promise.reject(new Error(`unexpected fetch: ${url}`));
  });
}

beforeEach(() => {
  _resetPollingStore();
  mockEndpoints();
});

afterEach(() => {
  cleanup();
  vi.clearAllMocks();
  _resetPollingStore();
});

function renderPanel() {
  return render(
    <MemoryRouter>
      <ProjektePanel />
    </MemoryRouter>,
  );
}

describe("ProjektePanel (echte Daten über die Bestands-Hooks)", () => {
  it("rendert Karten mit Name, Kanban-Zählern, Commit und Live-Meta", async () => {
    mockEndpoints({
      projects: [fixtureProject()],
      agents: [
        fixtureAgent(),
        fixtureAgent({
          kind: "claude",
          label: "claim-1",
          task: "Review der Shell-Lane",
          source: "coordination",
          tmux_session: null,
          tmux_window: null,
          operator: "piet",
        }),
      ],
    });
    renderPanel();

    expect(await screen.findByText("Hermes Infra")).toBeTruthy();
    // Kanban-Zählerzeile wie Klassik (Offen/Läuft/Blockiert/Review/Erledigt·7T).
    expect(screen.getByText("Offen").parentElement?.textContent).toContain("3");
    expect(screen.getByText("Review").parentElement?.textContent).toContain("2");
    expect(screen.getByText("Erledigt · 7T").parentElement?.textContent).toContain("41");
    // Commit + Live/Check-in-Meta.
    expect(screen.getByText(/jarvis: Shell-Einzug I/)).toBeTruthy();
    expect(screen.getByText(/1 live · 1 Check-in/)).toBeTruthy();
    // Kein Eingriff → quiet: kein Badge.
    expect(screen.queryByText("Eingriff")).toBeNull();
  });

  it("sortiert alert → active → quiet und markiert die Ampel samt Grund-Chips", async () => {
    mockEndpoints({
      projects: [
        fixtureProject({
          slug: "oma-galerie",
          name: "Oma-Galerie",
          kanban: null,
          loops: { active: 0, packs: [] },
        }),
        fixtureProject({
          slug: "health-track",
          name: "Health Track",
          kanban: { open: 2, running: 0, blocked: 0, review: 0, done_7d: 0, needs_input: 0 },
          loops: { active: 1, packs: [] },
        }),
        fixtureProject({
          slug: "hermes-infra",
          name: "Hermes Infra",
          kanban: { open: 1, running: 0, blocked: 2, review: 0, done_7d: 0, needs_input: 1 },
        }),
      ],
    });
    const { container } = renderPanel();

    await screen.findByText("Hermes Infra");
    const cards = container.querySelectorAll(".jv-pcard");
    expect(cards).toHaveLength(3);
    // Registry-Reihenfolge war quiet zuerst — die Ampel-Sortierung muss
    // (wie Klassik) alert → active → quiet liefern.
    expect(cards[0].getAttribute("data-attention")).toBe("alert");
    expect(cards[0].textContent).toContain("Hermes Infra");
    expect(cards[0].textContent).toContain("Eingriff");
    expect(cards[0].querySelector('[data-reason="needs_input"]')?.textContent).toContain("1 Frage");
    expect(cards[0].querySelector('[data-reason="blocked"]')?.textContent).toContain("2 blocked");
    expect(cards[1].getAttribute("data-attention")).toBe("active");
    expect(cards[1].textContent).toContain("Health Track");
    expect(cards[1].textContent).toContain("1 Loop aktiv");
    expect(cards[2].getAttribute("data-attention")).toBe("quiet");
    expect(cards[2].textContent).toContain("Oma-Galerie");
  });

  it("jede Karte ist ein Link auf das Klassik-Drilldown (kein neues Navigationsmodell)", async () => {
    mockEndpoints({ projects: [fixtureProject()] });
    renderPanel();

    const link = await screen.findByRole("link", {
      name: "Projekt öffnen: Hermes Infra — klassische Ansicht",
    });
    expect(link.getAttribute("href")).toBe("/control/projekte-klassisch");
  });

  it("stale-open Session hebt das Projekt auf alert (geteilte Ableitung)", async () => {
    mockEndpoints({
      projects: [
        fixtureProject({
          kanban: { open: 0, running: 0, blocked: 0, review: 0, done_7d: 0, needs_input: 0 },
        }),
      ],
      sessions: [
        {
          id: "zombie1",
          label: "Verwaiste Session",
          source: "cli",
          model: null,
          started_at: 1784100000,
          ended_at: null,
          end_reason: null,
          is_open: true,
          is_active: false,
          stale_open: true,
          last_active: 1784100100,
          message_count: 3,
          tokens: 100,
          project: "hermes-infra",
          spawn_kind: null,
          spawned_by_id: null,
          spawned_by_label: null,
        },
      ],
    });
    const { container } = renderPanel();

    await screen.findByText("Hermes Infra");
    const card = container.querySelector(".jv-pcard");
    expect(card?.getAttribute("data-attention")).toBe("alert");
    expect(card?.querySelector('[data-reason="stale_sessions"]')?.textContent).toContain("1 stale");
  });

  it("Empty-State wenn die Registry keine Projekte kennt", async () => {
    mockEndpoints({ projects: [] });
    renderPanel();

    expect(await screen.findByText("Keine Projekte registriert.")).toBeTruthy();
    expect(screen.getByText(/projects.yaml/)).toBeTruthy();
  });

  it("Fehler des Projekte-Polls → inline Fehler (role=alert), nie still", async () => {
    mockEndpoints({ projectsError: "network timeout after 20000ms" });
    renderPanel();

    const alert = await screen.findByRole("alert");
    expect(alert.textContent).toContain("Projekte konnten nicht geladen werden.");
  });

  it("Loading-State vor dem ersten Poll-Ergebnis (kein falscher Leer-Zustand)", () => {
    fetchJSONMock.mockImplementation(() => new Promise(() => {}));
    renderPanel();

    expect(screen.getByText("Lade Projekte …")).toBeTruthy();
    expect(screen.queryByText("Keine Projekte registriert.")).toBeNull();
  });
});
