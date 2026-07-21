// @vitest-environment jsdom
/**
 * ProjekteChip — G3: rendert echte Counts über denselben Datenpfad wie die
 * Klassik (fetchJSON-Mock speist /api/projects + /api/projects/agents +
 * /api/projects/sessions durch den echten pollingStore, Payloads im
 * Backend-Format). Erwartungen: Chip „Projekte · n · ⚠ k" (⚠ nur bei Alarmen),
 * Popover listet NUR Alarm-Projekte + „alle zeigen" → Klassik, ESC und
 * Outside-Click schließen (Fokus zurück an den Chip), Degraded-Mode bei
 * Fetch-Fehler („Projekte · –" + Fehlerzeile, bleibt bedienbar).
 */
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { cleanup, configure, fireEvent, render, screen, within } from "@testing-library/react";
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

import { ProjekteChip } from "./ProjekteChip";

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

function renderChip() {
  return render(
    <MemoryRouter>
      <ProjekteChip />
    </MemoryRouter>,
  );
}

/** Drei Projekte, genau eines davon alert (blockiert + Frage). */
function mixedProjects() {
  return [
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
  ];
}

describe("ProjekteChip (echte Daten über die Bestands-Hooks)", () => {
  it("rendert echte Counts: Projekte · n und ⚠ k für Alarme", async () => {
    mockEndpoints({ projects: mixedProjects() });
    renderChip();

    const button = await screen.findByRole("button");
    expect(button.textContent).toContain("Projekte · 3");
    expect(button.textContent).toContain("⚠ 1");
  });

  it("kein ⚠-Segment ohne Alarme", async () => {
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
          kanban: { open: 0, running: 0, blocked: 0, review: 0, done_7d: 0, needs_input: 0 },
          loops: { active: 1, packs: [] },
        }),
      ],
    });
    renderChip();

    const button = await screen.findByRole("button");
    expect(button.textContent).toContain("Projekte · 2");
    expect(button.textContent).not.toContain("⚠");
  });

  it("Popover listet NUR Alarm-Projekte (Gründe deutsch) plus alle zeigen → Klassik", async () => {
    mockEndpoints({ projects: mixedProjects() });
    renderChip();

    fireEvent.click(await screen.findByRole("button"));
    const dialog = await screen.findByRole("dialog");

    // Nur das Alarm-Projekt erscheint — aktive/ruhige nicht.
    expect(within(dialog).getByText("Hermes Infra")).toBeTruthy();
    expect(within(dialog).queryByText("Health Track")).toBeNull();
    expect(within(dialog).queryByText("Oma-Galerie")).toBeNull();
    // Deutsche Gründe aus computeAttention (1 Frage + 2 blocked).
    expect(within(dialog).getByText("1 Frage")).toBeTruthy();
    expect(within(dialog).getByText("2 blocked")).toBeTruthy();

    // Drilldown-Zeile des Alarms + „alle zeigen" gehen beide auf die Klassik.
    const row = within(dialog).getByRole("link", {
      name: "Projekt öffnen: Hermes Infra — klassische Ansicht",
    });
    expect(row.getAttribute("href")).toBe("/control/projekte-klassisch");
    const showAll = within(dialog).getByRole("link", { name: "alle zeigen" });
    expect(showAll.getAttribute("href")).toBe("/control/projekte-klassisch");
  });

  it("Leerzustand (keine Alarme): keine Eingriffe nötig + alle zeigen", async () => {
    mockEndpoints({
      projects: [
        fixtureProject({
          slug: "oma-galerie",
          name: "Oma-Galerie",
          kanban: null,
          loops: { active: 0, packs: [] },
        }),
      ],
    });
    renderChip();

    fireEvent.click(await screen.findByRole("button"));
    const dialog = await screen.findByRole("dialog");
    expect(within(dialog).getByText("Keine Eingriffe nötig")).toBeTruthy();
    expect(within(dialog).getByRole("link", { name: "alle zeigen" })).toBeTruthy();
  });

  it("ESC schließt das Popover und gibt den Fokus an den Chip zurück", async () => {
    mockEndpoints({ projects: mixedProjects() });
    renderChip();

    const button = await screen.findByRole("button");
    fireEvent.click(button);
    expect(await screen.findByRole("dialog")).toBeTruthy();
    expect(button.getAttribute("aria-expanded")).toBe("true");

    fireEvent.keyDown(document.body, { key: "Escape" });
    expect(screen.queryByRole("dialog")).toBeNull();
    expect(button.getAttribute("aria-expanded")).toBe("false");
    expect(document.activeElement).toBe(button);
  });

  it("Outside-Click schließt das Popover", async () => {
    mockEndpoints({ projects: mixedProjects() });
    renderChip();

    fireEvent.click(await screen.findByRole("button"));
    expect(await screen.findByRole("dialog")).toBeTruthy();

    fireEvent.mouseDown(document.body);
    expect(screen.queryByRole("dialog")).toBeNull();
  });

  it("Degraded-Mode bei Fetch-Fehler: Projekte · – bleibt bedienbar (Fehlerzeile)", async () => {
    mockEndpoints({ projectsError: "network timeout after 20000ms" });
    renderChip();

    const button = await screen.findByRole("button");
    await screen.findByText("Projekte · –");
    expect(button.textContent).not.toContain("⚠");

    fireEvent.click(button);
    const dialog = await screen.findByRole("dialog");
    const alert = within(dialog).getByRole("alert");
    expect(alert.textContent).toContain("Projekte konnten nicht geladen werden.");
    // Trotz Fehler bedienbar: „alle zeigen" bleibt erreichbar.
    expect(within(dialog).getByRole("link", { name: "alle zeigen" })).toBeTruthy();
  });
});
