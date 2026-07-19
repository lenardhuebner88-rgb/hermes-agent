// @vitest-environment jsdom
/**
 * SessionsPanel — „SESSIONS" der Jarvis-Shell (S3.10): Spawn-Baum aus den
 * SessionsSection-Daten über denselben Polling-Keys wie die Klassik.
 * Erwartungen: Strip-Zähler, Filter-Chips (Offen/Aktiv/Verwaist/Alle),
 * Spawn-Baum-Einzug, Terminal-Deep-Link aus den strukturierten Feldern,
 * Kill über das KLASSIK-Sheet (SessionKillSheet → gleicher terminate-POST),
 * kein Kill ohne eindeutiges Agent-Match, Empty-/Error-States.
 */
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { cleanup, configure, render, screen, within } from "@testing-library/react";
import { MemoryRouter } from "react-router-dom";

import { _resetPollingStore } from "../hooks/pollingStore";

configure({ asyncUtilTimeout: 5000 });

const fetchJSONMock = vi.hoisted(() => vi.fn());
const apiMock = vi.hoisted(() => ({
  terminateAgentTerminalWindow: vi.fn(() => Promise.resolve({ ok: true })),
}));

vi.mock("@/lib/api", async () => {
  const actual = await vi.importActual<typeof import("@/lib/api")>("@/lib/api");
  return {
    ...actual,
    api: { ...actual.api, terminateAgentTerminalWindow: apiMock.terminateAgentTerminalWindow },
    fetchJSON: fetchJSONMock,
  };
});

import { SessionsPanel } from "./SessionsPanel";

const NOW = Math.floor(Date.now() / 1000);

// ── Fixtures im echten Backend-Shape (projects_overview.py) ────────────────
function fixtureSession(overrides: Record<string, unknown> = {}) {
  return {
    id: "s1",
    label: "work:2 kimi",
    source: "cli",
    model: "kimi-k3",
    started_at: NOW - 3600,
    ended_at: null,
    end_reason: null,
    is_open: true,
    is_active: true,
    stale_open: false,
    last_active: NOW - 30,
    message_count: 12,
    tokens: 45000,
    project: "hermes-infra",
    spawn_kind: null,
    spawned_by_id: null,
    spawned_by_label: null,
    tmux_session: "work",
    tmux_window: "2",
    tmux_window_name: "kimi",
    ...overrides,
  };
}

function fixtureAgent(overrides: Record<string, unknown> = {}) {
  return {
    kind: "kimi",
    label: "work:2 kimi",
    task: null,
    project: "hermes-infra",
    since: NOW - 3600,
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

const TREE = [
  fixtureSession(),
  fixtureSession({
    id: "s2",
    label: "explore-agent",
    spawn_kind: "delegate",
    spawned_by_id: "s1",
    spawned_by_label: "work:2 kimi",
    is_active: false,
    tmux_session: null,
    tmux_window: null,
    tmux_window_name: null,
  }),
  fixtureSession({
    id: "s3",
    label: "verwaiste-session",
    is_active: false,
    stale_open: true,
    tmux_session: null,
    tmux_window: null,
    tmux_window_name: null,
  }),
  fixtureSession({
    id: "s4",
    label: "beendete-session",
    is_open: false,
    is_active: false,
    ended_at: NOW - 120,
    end_reason: "completed",
    tmux_session: null,
    tmux_window: null,
    tmux_window_name: null,
  }),
];

function mockEndpoints({
  sessions = TREE,
  agents = [fixtureAgent()],
  sessionErrors = [] as string[],
  sessionsError,
}: {
  sessions?: Array<Record<string, unknown>>;
  agents?: Array<Record<string, unknown>>;
  sessionErrors?: string[];
  sessionsError?: string;
} = {}) {
  fetchJSONMock.mockImplementation((url: string, init?: { method?: string; body?: string }) => {
    if (url === "/api/projects") {
      return Promise.resolve({
        generated_at: NOW,
        registry_errors: [],
        projects: [
          {
            slug: "hermes-infra",
            name: "Hermes Infra",
            repo_path: "/home/piet/.hermes/hermes-agent",
            parent: null,
            links: [],
            last_commit: null,
            kanban: null,
            loops: null,
            errors: [],
          },
        ],
      });
    }
    if (url === "/api/projects/sessions") {
      return sessionsError
        ? Promise.reject(new Error(sessionsError))
        : Promise.resolve({ generated_at: NOW, errors: sessionErrors, sessions });
    }
    if (url === "/api/projects/agents") {
      return Promise.resolve({ generated_at: NOW, errors: [], agents });
    }
    if (url === "/api/agent-terminals/terminate" && init?.method === "POST") {
      return Promise.resolve({ ok: true });
    }
    return Promise.reject(new Error(`unexpected fetch: ${url} ${init?.method ?? "GET"}`));
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

function renderPanel(open = true) {
  return render(
    <MemoryRouter>
      <SessionsPanel open={open} onToggle={() => {}} />
    </MemoryRouter>,
  );
}

describe("SessionsPanel (Spawn-Baum der Klassik im Jarvis-Look)", () => {
  it("Strip zeigt Offen/Aktiv-Zähler", async () => {
    renderPanel(false);

    // Default-Filterlogik der Klassik: offen NICHT-verwaist = s1+s2, aktiv = s1.
    expect(await screen.findByText("2 OFFEN · 1 AKTIV")).toBeTruthy();
    expect(screen.queryByRole("region", { name: "SESSIONS" })).toBeNull();
  });

  it("Default-Filter Offen: Spawn-Baum mit Einzug und Spawn-Zähler", async () => {
    const { container } = renderPanel();

    const root = await screen.findByText("work:2 kimi");
    expect(root.textContent).toContain("(1× gespawnt)");
    const child = await screen.findByText("explore-agent");
    const childRow = child.closest(".jv-srow");
    expect(childRow?.getAttribute("style")).toContain("--tree-depth: 1");
    // Verwaiste/beendete Zeilen sind im Default-Filter nicht sichtbar.
    expect(screen.queryByText("verwaiste-session")).toBeNull();
    expect(screen.queryByText("beendete-session")).toBeNull();
    expect(container.querySelectorAll(".jv-srow")).toHaveLength(2);
  });

  it("Filter-Chips schalten wie die Klassik (Verwaist/Alle)", async () => {
    const { container } = renderPanel();

    await screen.findByText("work:2 kimi");
    screen.getByRole("button", { name: /Verwaist/ }).click();
    expect(await screen.findByText("verwaiste-session")).toBeTruthy();
    expect(container.querySelectorAll(".jv-srow")).toHaveLength(1);

    screen.getByRole("button", { name: /Alle/ }).click();
    await screen.findByText("beendete-session");
    expect(container.querySelectorAll(".jv-srow")).toHaveLength(4);
  });

  it("Terminal-Deep-Link aus den strukturierten tmux-Feldern (window_name bevorzugt)", async () => {
    renderPanel();

    const link = await screen.findByRole("link", { name: "Terminal öffnen: work:2 kimi" });
    expect(link.getAttribute("href")).toBe("/control/agent-terminals?session=work&window=kimi");
  });

  it("Kill über das KLASSIK-Sheet: Match über strukturierte Felder, gleicher terminate-POST", async () => {
    renderPanel();

    const drawer = within(await screen.findByRole("region", { name: "SESSIONS" }));
    const kill = await drawer.findByRole("button", { name: "Session work:2 kimi beenden" });
    // Delegate-Zeile ohne tmux-Adresse bleibt nicht killbar.
    expect(drawer.queryByRole("button", { name: "Session explore-agent beenden" })).toBeNull();

    kill.click();
    // Das Klassik-Sheet (SessionKillSheet) öffnet sich als Dialog.
    expect(await screen.findByRole("dialog")).toBeTruthy();
    expect(screen.getByText("Session beenden?")).toBeTruthy();

    screen.getByRole("button", { name: "Session beenden" }).click();
    // Der terminate-POST läuft über api.terminateAgentTerminalWindow — gleiche
    // Argumente wie in der Klassik (session, window, external=true).
    await vi.waitFor(() => {
      expect(apiMock.terminateAgentTerminalWindow).toHaveBeenCalledWith("work", "2", true);
    });
    // Nach Erfolg lädt das Sheet sich weg (onKilled → reload beider Polls).
    await vi.waitFor(() => {
      expect(screen.queryByRole("dialog")).toBeNull();
    });
  });

  it("Ohne eindeutiges Agent-Match ist die Zeile nicht killbar", async () => {
    mockEndpoints({ agents: [] });
    renderPanel();

    await screen.findByText("work:2 kimi");
    expect(screen.queryByRole("button", { name: /beenden$/ })).toBeNull();
    // Terminal-Deep-Link bleibt davon unberührt (Zeilen-Verhalten der Klassik).
    expect(
      screen.getByRole("link", { name: "Terminal öffnen: work:2 kimi" }),
    ).toBeTruthy();
  });

  it("Quell-Degradation (errors[]) wird dezent gezeigt, der Rest gilt weiter", async () => {
    mockEndpoints({ sessionErrors: ["sessions-tmux: scan fehlgeschlagen"] });
    renderPanel();

    expect(await screen.findByText("sessions-tmux: scan fehlgeschlagen")).toBeTruthy();
    expect(screen.getByText("work:2 kimi")).toBeTruthy();
  });

  it("Fetch-Fehler → inline Fehler (role=alert), Empty je Filter", async () => {
    mockEndpoints({ sessionsError: "network timeout" });
    renderPanel();

    const alert = await screen.findByRole("alert");
    expect(alert.textContent).toContain("Sessions konnten nicht geladen werden.");
  });

  it("Empty-State im aktiven Filter (keine falsche Leere beim Laden)", async () => {
    mockEndpoints({ sessions: [] });
    renderPanel();

    expect(await screen.findByText("Keine offenen Sessions.")).toBeTruthy();
    screen.getByRole("button", { name: /Verwaist/ }).click();
    expect(
      await screen.findByText("Keine verwaist offenen Sessions — sauber."),
    ).toBeTruthy();
  });
});
