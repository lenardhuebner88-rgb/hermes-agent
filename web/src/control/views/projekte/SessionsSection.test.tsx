// @vitest-environment jsdom
import { afterEach, describe, expect, it } from "vitest";
import { cleanup, fireEvent, render, screen } from "@testing-library/react";
import { MemoryRouter } from "react-router-dom";
import type { ProjectSession } from "../../lib/schemas";

import { SessionsSection } from "./SessionsSection";

function makeSession(overrides: Partial<ProjectSession> & { id: string }): ProjectSession {
  return {
    label: overrides.id,
    source: "cli",
    model: "kimi-k2",
    started_at: 1784230000,
    ended_at: null,
    end_reason: null,
    is_open: true,
    is_active: false,
    stale_open: false,
    last_active: 1784239000,
    message_count: 5,
    tokens: 2100,
    project: null,
    spawn_kind: null,
    spawned_by_id: null,
    spawned_by_label: null,
    ...overrides,
  };
}

/** MemoryRouter: Zeilen mit tmux-Adresse rendern seit Stage 12 einen
 *  react-router Link (Terminal-Deep-Link) — der braucht Router-Kontext. */
function renderSection(
  sessions: ProjectSession[],
  projectNames: Readonly<Record<string, string>> = {},
  errors: ReadonlyArray<string> = [],
) {
  return render(
    <MemoryRouter>
      <SessionsSection
        sessions={sessions}
        projectNames={projectNames}
        now={1784240000}
        errors={errors}
      />
    </MemoryRouter>,
  );
}

const ROOT = makeSession({
  id: "root",
  label: "Hauptsession",
  is_active: true,
  project: "hermes-infra",
});
const CHILD = makeSession({
  id: "child",
  label: "Subagent Lauf",
  spawn_kind: "delegate",
  spawned_by_id: "root",
  spawned_by_label: "Hauptsession",
});
const ENDED = makeSession({
  id: "ended",
  label: "Alte Session",
  is_open: false,
  ended_at: 1784238000,
  end_reason: "user_exit",
});

afterEach(() => cleanup());

describe("SessionsSection", () => {
  it("nests the spawned child under its parent with the spawn label", () => {
    const { container } = renderSection([CHILD, ROOT], { "hermes-infra": "Hermes Infra" });
    const html = container.innerHTML;
    expect(html).toContain("Hauptsession");
    expect(html).toContain("Subagent Lauf");
    expect(html.indexOf("Hauptsession")).toBeLessThan(html.indexOf("Subagent Lauf"));
    expect(html).toContain("von Hauptsession · Subagent");
    expect(html).toContain("(1× gespawnt)");
    // Indent marker: the child row carries a margin-left style.
    const rows = container.querySelectorAll("li");
    const childRow = Array.from(rows).find((row) => row.textContent?.includes("Subagent Lauf"));
    expect(childRow?.getAttribute("style") ?? "").toContain("margin-left");
  });

  it("defaults to the open filter and hides ended sessions until Alle is picked", () => {
    renderSection([ROOT, ENDED]);
    expect(screen.getByText("Hauptsession")).toBeTruthy();
    expect(screen.queryByText("Alte Session")).toBeNull();

    fireEvent.click(screen.getByRole("button", { name: /Alle/ }));
    expect(screen.getByText("Alte Session")).toBeTruthy();
    expect(screen.getByText("beendet")).toBeTruthy();
  });

  it("shows only live sessions under Aktiv", () => {
    renderSection([ROOT, makeSession({ id: "idle", label: "Idle Session", is_active: false })]);
    fireEvent.click(screen.getByRole("button", { name: /Aktiv/ }));
    expect(screen.getByText("Hauptsession")).toBeTruthy();
    expect(screen.queryByText("Idle Session")).toBeNull();
  });

  it("renders the doctrine-shaped empty state per filter", () => {
    renderSection([]);
    expect(screen.getByText("Keine offenen Sessions.")).toBeTruthy();
  });

  it("surfaces payload source errors inline while still rendering rows", () => {
    // Reales Fehlerformat aus build_sessions_payload (Stufe 1.1 Degradation).
    renderSection([ROOT], {}, ["sessions-tmux: tmux command failed"]);
    expect(screen.getByText("Session-Quellen unvollständig")).toBeTruthy();
    expect(screen.getByText("sessions-tmux: tmux command failed")).toBeTruthy();
    // Zeilen bleiben trotz Degradation sichtbar.
    expect(screen.getByText("Hauptsession")).toBeTruthy();
  });

  it("renders no source-error box without payload errors", () => {
    renderSection([ROOT]);
    expect(screen.queryByText("Session-Quellen unvollständig")).toBeNull();
  });

  it("links sessions carrying a tmux address to the terminal deep link, window optional", () => {
    const withTmux = makeSession({
      id: "tmux1",
      label: "Kimi Work",
      is_active: true,
      tmux_session: "work",
      tmux_window: "3",
    });
    const noWindow = makeSession({
      id: "tmux2",
      label: "Nur Session",
      tmux_session: "scratch",
      tmux_window: null,
    });
    renderSection([ROOT, withTmux, noWindow]);

    const withWindowLink = screen.getByRole("link", { name: "Terminal öffnen: Kimi Work" });
    expect(withWindowLink.getAttribute("href")).toBe("/control/agent-terminals?session=work&window=3");
    // Fensterlos: der window-Parameter entfällt komplett.
    const noWindowLink = screen.getByRole("link", { name: "Terminal öffnen: Nur Session" });
    expect(noWindowLink.getAttribute("href")).toBe("/control/agent-terminals?session=scratch");
    // Zeilen ohne tmux-Adresse bekommen die Affordance nie.
    expect(screen.queryByRole("link", { name: "Terminal öffnen: Hauptsession" })).toBeNull();
  });
});
