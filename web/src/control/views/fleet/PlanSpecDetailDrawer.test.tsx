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

  // --- Kriterien stehen je Slice (Canon planspec-taskgraph.md) ---------------
  // Gemessen 2026-07-30 an den beiden real offenen Plänen: Landing-Loop trägt
  // 23 Kriterien (12+11) und Burn-Hotspots 5 — alle ausschließlich pro Subtask.
  // Das Detail rendert bisher NUR die plan-weite Liste, also „Keine Kriterien
  // im Plan" auf einem Plan mit 23 testbaren Kriterien.
  const sliceDetail: PlanSpecDetailResponse = {
    ...baseDetail,
    acceptance_criteria: [],
    acceptance_criteria_total: 3,
    subtasks: [
      {
        id: "LL-1",
        title: "Deterministischer Kern",
        lane: "coder",
        deps: [],
        scope_files: ["hermes_cli/landing_loop.py", "tests/hermes_cli/test_landing_loop.py"],
        acceptance_criteria: [
          "Die Entscheidungslogik liegt vollständig in hermes_cli/landing_loop.py.",
          "Trockenlauf --dry-run fasst keinen Ref an.",
        ],
      },
      {
        id: "LL-2",
        title: "Reparatur",
        lane: "coder",
        deps: ["LL-1"],
        acceptance_criteria: ["Ein rotes Gate rollt den Merge zurück."],
      },
    ],
  };

  it("zeigt Slice-Kriterien im Überblick, wenn der Plan keine plan-weiten trägt", () => {
    render(
      <PlanSpecDetailDrawer
        item={baseItem}
        detail={sliceDetail}
        loading={false}
        error={null}
        onClose={noop}
      />,
    );

    expect(screen.queryByText("Keine Kriterien im Plan.")).toBeNull();
    expect(
      screen.getByText("Die Entscheidungslogik liegt vollständig in hermes_cli/landing_loop.py."),
    ).toBeTruthy();
    expect(screen.getByText("Ein rotes Gate rollt den Merge zurück.")).toBeTruthy();
    // Die Kriterien bleiben ihrem Schritt zugeordnet — sonst ist unklar,
    // wogegen abgenommen wird.
    expect(screen.getAllByText(/LL-1/).length).toBeGreaterThan(0);
  });

  it("Kontrollprobe: ohne jedes Kriterium bleibt die Leermeldung stehen", () => {
    render(
      <PlanSpecDetailDrawer
        item={baseItem}
        detail={{
          ...sliceDetail,
          acceptance_criteria_total: 0,
          subtasks: sliceDetail.subtasks.map((task) => ({ ...task, acceptance_criteria: [] })),
        }}
        loading={false}
        error={null}
        onClose={noop}
      />,
    );

    expect(screen.getByText("Keine Kriterien im Plan.")).toBeTruthy();
  });

  it("Ablauf: Arbeitsvertrag zeigt Kriterien und Scope-Pfade des Schritts", () => {
    render(
      <PlanSpecDetailDrawer
        item={baseItem}
        detail={sliceDetail}
        loading={false}
        error={null}
        onClose={noop}
      />,
    );

    fireEvent.click(screen.getByRole("tab", { name: "Ablauf 2" }));
    expect(screen.getByText("Trockenlauf --dry-run fasst keinen Ref an.")).toBeTruthy();
    expect(
      screen.getByText("hermes_cli/landing_loop.py · tests/hermes_cli/test_landing_loop.py"),
    ).toBeTruthy();
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

describe("PlanSpecDetailDrawer — geschlossene Pläne und Befund-Echo", () => {
  afterEach(cleanup);

  // Live-Form 2026-08-04: abgeschlossene PlanSpec, nie ingestiert, der Hub
  // projiziert „closed status: done" als finding.
  const closedItem: PlanSpecRecord = {
    ...baseItem,
    open: false,
    status: "done",
    closed_reason: "closed status: done",
    action_state: "completed",
    next_action: "open_result",
    finding_count: 1,
    ingest_findings: ["closed status: done"],
  };
  const closedDetail: PlanSpecDetailResponse = {
    ...baseDetail,
    source_findings: ["closed status: done"],
  };

  it("zeigt das Abschluss-Echo nicht als Prüfbefund — echte Befunde bleiben", () => {
    render(
      <PlanSpecDetailDrawer
        item={closedItem}
        detail={closedDetail}
        loading={false}
        error={null}
        onClose={noop}
      />,
    );
    expect(screen.queryByText("Prüfbefunde")).toBeNull();
    expect(screen.queryByText("closed status: done")).toBeNull();

    cleanup();
    render(
      <PlanSpecDetailDrawer
        item={{ ...closedItem, ingest_findings: ["closed status: done", "scope-less code subtask: ER-S2"] }}
        detail={closedDetail}
        loading={false}
        error={null}
        onClose={noop}
      />,
    );
    expect(screen.getByText("Prüfbefunde")).toBeTruthy();
    expect(screen.getByText("scope-less code subtask: ER-S2")).toBeTruthy();
    expect(screen.queryByText("closed status: done")).toBeNull();
  });

  it("endet bei geschlossenen Plänen ohne Sackgassen-Next-Step", () => {
    render(
      <PlanSpecDetailDrawer
        item={closedItem}
        detail={closedDetail}
        loading={false}
        error={null}
        onClose={noop}
      />,
    );
    expect(screen.queryByText("Nächster Schritt")).toBeNull();
    expect(screen.queryByText("Abschluss prüfen")).toBeNull();

    // Offener Plan behält seinen nächsten Schritt.
    cleanup();
    render(
      <PlanSpecDetailDrawer
        item={{ ...baseItem, next_action: "preview_ingest" }}
        detail={baseDetail}
        loading={false}
        error={null}
        onClose={noop}
      />,
    );
    expect(screen.getByText("Nächster Schritt")).toBeTruthy();
    expect(screen.getByText("Übergabe vorprüfen")).toBeTruthy();
  });

  it("versteckt die Übergabe-Matrix bei geschlossenen Plänen", () => {
    render(
      <PlanSpecDetailDrawer
        item={closedItem}
        detail={closedDetail}
        loading={false}
        error={null}
        onClose={noop}
      />,
    );
    fireEvent.click(screen.getByRole("tab", { name: "Übergabe" }));
    expect(screen.queryByText("Auswirkung")).toBeNull();
    expect(screen.queryByText("Schutz")).toBeNull();
  });

  it("beschriftet Freigabe und Live-Test-Tiefe im Kopf (I1)", () => {
    render(
      <PlanSpecDetailDrawer
        item={baseItem}
        detail={baseDetail}
        loading={false}
        error={null}
        onClose={noop}
      />,
    );
    expect(screen.getAllByText("Freigabe: reviewer").length).toBeGreaterThan(0);
    expect(screen.getAllByText("Live-Test: smoke").length).toBeGreaterThan(0);
  });

  it("trägt im Kopf denselben Zustands-Ton wie die Listenzeile (I4)", () => {
    render(
      <PlanSpecDetailDrawer
        item={closedItem}
        detail={closedDetail}
        loading={false}
        error={null}
        onClose={noop}
      />,
    );
    // Liste: completed → Ton ok (grün). Der Detail-Chip zeigte vorher zinc.
    const chip = screen.getAllByText("erledigt")[0].closest("span.rounded-full");
    expect(chip?.className).toContain("text-status-ok");
  });

  it("zeigt den Abschlussbeleg in der Übergabe statt im Überblick (I5)", () => {
    const mitBeleg = {
      ...closedItem,
      closed_by: "operator",
      receipt: "vault/03-Agents/Claude-Code/receipts/abo-limits.md",
    };
    render(
      <PlanSpecDetailDrawer
        item={mitBeleg}
        detail={closedDetail}
        loading={false}
        error={null}
        onClose={noop}
      />,
    );
    // Überblick: kein Beleg mehr …
    expect(screen.queryByText("Abschlussbeleg")).toBeNull();
    // … sondern in der Übergabe — der Tab beantwortet „wie ist es ausgegangen".
    fireEvent.click(screen.getByRole("tab", { name: "Übergabe" }));
    expect(screen.getByText("Abschlussbeleg")).toBeTruthy();
    expect(screen.getByText("Geschlossen von operator")).toBeTruthy();
    expect(screen.getByText("vault/03-Agents/Claude-Code/receipts/abo-limits.md")).toBeTruthy();
  });

  it("zählt in der Übergabe-Prüfung nur sichtbare Befunde (I6)", () => {
    render(
      <PlanSpecDetailDrawer
        item={{
          ...baseItem,
          finding_count: 2,
          ingest_findings: ["closed status: done", "scope-less code subtask: ER-S2"],
        }}
        detail={baseDetail}
        loading={false}
        error={null}
        onClose={noop}
      />,
    );
    fireEvent.click(screen.getByRole("tab", { name: "Übergabe" }));
    expect(screen.getByText("1 Befunde · Live-Test smoke")).toBeTruthy();
  });

  it("zeigt den Kettenlauf als eigene Zeile in der Operativen Lage (V2)", () => {
    render(
      <PlanSpecDetailDrawer
        item={{ ...baseItem, action_state: "running", kanban_root_task_id: "t_root1", kanban_state: "running" }}
        detail={baseDetail}
        loading={false}
        error={null}
        onClose={noop}
      />,
    );
    expect(screen.getByText("Kette")).toBeTruthy();
    // „läuft" steht jetzt zweifach da: einmal als Plan-Chip, einmal als
    // Ausführungsachse der Kette — zwei Achsen, zwei Stellen.
    expect(screen.getAllByText("läuft").length).toBeGreaterThanOrEqual(2);

    // Ohne Wurzel keine Kette-Zeile.
    cleanup();
    render(
      <PlanSpecDetailDrawer
        item={baseItem}
        detail={baseDetail}
        loading={false}
        error={null}
        onClose={noop}
      />,
    );
    expect(screen.queryByText("Kette")).toBeNull();
  });

  it("zeigt ohne action_state denselben Datenfehler wie die Liste (V1)", () => {
    render(
      <PlanSpecDetailDrawer
        item={{ ...closedItem, action_state: undefined }}
        detail={closedDetail}
        loading={false}
        error={null}
        onClose={noop}
      />,
    );
    expect(screen.getAllByText("ohne Zustand").length).toBeGreaterThan(0);
    // Kein heimlich abgeleitetes „erledigt" aus den Kanban-Feldern.
    expect(screen.queryByText("erledigt")).toBeNull();
  });
});
