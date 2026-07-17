// @vitest-environment jsdom
import { afterEach, describe, expect, it } from "vitest";
import { cleanup, fireEvent, render, screen } from "@testing-library/react";
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
    const { container } = render(
      <SessionsSection
        sessions={[CHILD, ROOT]}
        projectNames={{ "hermes-infra": "Hermes Infra" }}
        now={1784240000}
      />,
    );
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
    render(
      <SessionsSection sessions={[ROOT, ENDED]} projectNames={{}} now={1784240000} />,
    );
    expect(screen.getByText("Hauptsession")).toBeTruthy();
    expect(screen.queryByText("Alte Session")).toBeNull();

    fireEvent.click(screen.getByRole("button", { name: /Alle/ }));
    expect(screen.getByText("Alte Session")).toBeTruthy();
    expect(screen.getByText("beendet")).toBeTruthy();
  });

  it("shows only live sessions under Aktiv", () => {
    render(
      <SessionsSection
        sessions={[ROOT, makeSession({ id: "idle", label: "Idle Session", is_active: false })]}
        projectNames={{}}
        now={1784240000}
      />,
    );
    fireEvent.click(screen.getByRole("button", { name: /Aktiv/ }));
    expect(screen.getByText("Hauptsession")).toBeTruthy();
    expect(screen.queryByText("Idle Session")).toBeNull();
  });

  it("renders the doctrine-shaped empty state per filter", () => {
    render(<SessionsSection sessions={[]} projectNames={{}} now={1784240000} />);
    expect(screen.getByText("Keine offenen Sessions.")).toBeTruthy();
  });
});
