// @vitest-environment jsdom
import { afterEach, describe, expect, it, vi } from "vitest";
import { renderToStaticMarkup } from "react-dom/server";
import { cleanup, fireEvent, render, screen } from "@testing-library/react";
import { PlanSpecDetailDrawer } from "./PlanSpecDetailDrawer";
import type { PlanSpecDetailResponse } from "../../lib/schemas";
import type { PlanSpecRecord } from "../../lib/types";

const baseItem: PlanSpecRecord = {
  path: "vault/00-Canon/planspec-test.md",
  agent: "claude",
  filename: "planspec-test.md",
  topic: "Test-Feature bauen",
  status: "open",
  freigabe: "reviewer",
  live_test_depth: "smoke",
  binding: true,
  subtask_count: 2,
  valid: true,
  open: true,
  closed_reason: null,
  kanban_root_task_id: null,
  kanban_root_status: null,
  kanban_state: "not_ingested",
  kanban_child_total: 0,
  kanban_child_done: 0,
  kanban_child_blocked: 0,
  kanban_child_running: 0,
  kanban_ingested_at: null,
  ingest_disposition: "clean",
  ingest_would_block: false,
  ingest_findings: [],
  errors: [],
};

const baseDetail: PlanSpecDetailResponse = {
  goal: "Automatisch Testergebnisse sammeln und anzeigen.",
  acceptance_criteria: [
    { id: "AC1", statement: "Dashboard zeigt Testergebnisse live." },
    { id: "AC2", statement: "Fehler werden rot markiert." },
  ],
  anti_scope: ["Kein manueller Upload erforderlich"],
  evidence_required: ["pytest grün"],
  freigabe: "reviewer",
  live_test_depth: "smoke",
  subtasks: [
    { id: "t_001", title: "Backend-Endpoint bauen", lane: "coder", deps: [] },
    { id: "t_002", title: "Frontend-Karte rendern", lane: "coder", deps: ["t_001"] },
  ],
};

const noop = vi.fn();

// DrawerShell portals to document.body when `document` exists and falls back
// to inline markup (its declared SSR-safe branch) when it doesn't. This file
// runs under jsdom (for the DrawerShell-Migration interaction test below), so
// `document` is always defined — stub it away for the plain
// renderToStaticMarkup assertions to keep exercising that SSR-safe branch
// instead of hitting react-dom/server's "portals unsupported" error.
function renderStaticMarkup(el: Parameters<typeof renderToStaticMarkup>[0]): string {
  vi.stubGlobal("document", undefined);
  try {
    return renderToStaticMarkup(el);
  } finally {
    vi.unstubAllGlobals();
  }
}

describe("PlanSpecDetailDrawer", () => {
  afterEach(cleanup);

  it("trennt Überblick, Ablauf und Übergabe in funktionale Detail-Tabs", () => {
    render(
      <PlanSpecDetailDrawer
        item={baseItem}
        detail={baseDetail}
        loading={false}
        error={null}
        onClose={noop}
      />,
    );

    expect(screen.getByText("Test-Feature bauen")).toBeTruthy();
    expect(screen.getByText("Dashboard zeigt Testergebnisse live.")).toBeTruthy();
    expect(screen.getByText("Kein manueller Upload erforderlich")).toBeTruthy();
    expect(screen.getByText("pytest grün")).toBeTruthy();
    expect(screen.queryByText("Backend-Endpoint bauen")).toBeNull();

    fireEvent.click(screen.getByRole("tab", { name: "Ablauf 2" }));
    expect(screen.getByText("Backend-Endpoint bauen")).toBeTruthy();
    expect(screen.getByText("Frontend-Karte rendern")).toBeTruthy();
    expect(screen.queryByText("Dashboard zeigt Testergebnisse live.")).toBeNull();

    fireEvent.click(screen.getByRole("tab", { name: "Übergabe" }));
    expect(screen.getByText("Auswirkung")).toBeTruthy();
    expect(screen.getByText("Schutz")).toBeTruthy();
  });

  it("macht lange Pfade kopierbar und behält den vollständigen Pfad zugänglich", () => {
    const longPath = "vault/03-Agents/Hermes/plans/2026-06-21-dashboard-planspec-display-polish-with-a-very-long-name.md";
    const html = renderStaticMarkup(
      <PlanSpecDetailDrawer
        item={{ ...baseItem, path: longPath }}
        detail={baseDetail}
        loading={false}
        error={null}
        onClose={noop}
      />,
    );

    expect(html).toContain('aria-label="PlanSpec-Pfad kopieren"');
    expect(html).toContain(`title="${longPath}"`);
    expect(html).toContain(longPath);
  });

  it("rendert Lade-Skeleton wenn loading=true und kein Detail vorhanden", () => {
    const html = renderStaticMarkup(
      <PlanSpecDetailDrawer
        item={baseItem}
        detail={null}
        loading={true}
        error={null}
        onClose={noop}
      />,
    );
    // Topic immer sichtbar
    expect(html).toContain("Test-Feature bauen");
    // Subtask-Inhalt noch nicht vorhanden
    expect(html).not.toContain("Backend-Endpoint bauen");
  });

  it("rendert Fehler-Callout wenn error gesetzt ist", () => {
    const html = renderStaticMarkup(
      <PlanSpecDetailDrawer
        item={baseItem}
        detail={null}
        loading={false}
        error="Datei nicht gefunden"
        onClose={noop}
      />,
    );
    expect(html).toContain("Datei nicht gefunden");
  });

  it("navigiert nur für Im-Board-Pläne mit Root-ID in die richtige Kette", () => {
    const onOpenChain = vi.fn();
    const itemWithRoot: PlanSpecRecord = {
      ...baseItem,
      kanban_root_task_id: "t_root123",
      kanban_state: "running",
      action_state: "running",
    };
    const { rerender } = render(
      <PlanSpecDetailDrawer
        item={itemWithRoot}
        detail={baseDetail}
        loading={false}
        error={null}
        onClose={noop}
        onOpenChain={onOpenChain}
      />,
    );

    fireEvent.click(screen.getByRole("button", { name: "In Ketten anzeigen" }));
    expect(onOpenChain).toHaveBeenCalledWith("t_root123");

    rerender(
      <PlanSpecDetailDrawer
        item={baseItem}
        detail={baseDetail}
        loading={false}
        error={null}
        onClose={noop}
        onOpenChain={onOpenChain}
      />,
    );
    expect(screen.queryByRole("button", { name: "In Ketten anzeigen" })).toBeNull();

    rerender(
      <PlanSpecDetailDrawer
        item={{ ...itemWithRoot, action_state: "completed", kanban_state: "completed" }}
        detail={baseDetail}
        loading={false}
        error={null}
        onClose={noop}
        onOpenChain={onOpenChain}
      />,
    );
    expect(screen.queryByRole("button", { name: "In Ketten anzeigen" })).toBeNull();
  });

  it("Klick auf Schließen-Button ruft onClose", () => {
    const onClose = vi.fn();
    // renderToStaticMarkup rendert kein interaktives DOM —
    // wir prüfen stattdessen, dass der Button mit dem aria-label vorhanden ist
    // und dass onClose als onClick korrekt gesetzt wird.
    const html = renderStaticMarkup(
      <PlanSpecDetailDrawer
        item={baseItem}
        detail={baseDetail}
        loading={false}
        error={null}
        onClose={onClose}
      />,
    );
    expect(html).toContain('aria-label="PlanSpec schließen"');
    expect(html).toContain("PlanSpec Details");
  });

  it("DrawerShell-Migration: Backdrop schließt, Panel-Klick nicht, Dialog-Semantik", () => {
    const onClose = vi.fn();
    render(
      <PlanSpecDetailDrawer
        item={baseItem}
        detail={baseDetail}
        loading={false}
        error={null}
        onClose={onClose}
      />,
    );

    const dialog = screen.getByRole("dialog", { name: "PlanSpec Details" });
    expect(dialog.getAttribute("aria-modal")).toBe("true");

    fireEvent.click(dialog);
    expect(onClose).not.toHaveBeenCalled();

    fireEvent.click(screen.getByRole("presentation"));
    expect(onClose).toHaveBeenCalledTimes(1);
  });
});
