// @vitest-environment jsdom

import { cleanup, fireEvent, render, screen } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import type { PlanSpecsResponse } from "../../lib/schemas";
import type { PlanSpecRecord } from "./shared";
import { PlanTab } from "./PlanTab";

function plan(overrides: Partial<PlanSpecRecord> = {}): PlanSpecRecord {
  return {
    path: "vault/03-Agents/Codex/plans/ready.md",
    agent: "codex",
    filename: "ready.md",
    topic: "Board und Plan fertigstellen",
    status: "open",
    freigabe: "operator",
    live_test_depth: "smoke",
    binding: true,
    subtask_count: 9,
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
    action_state: "ready",
    action_reason: "Übergabe kann geprüft werden.",
    dependency_count: 10,
    finding_count: 0,
    target_board: "hermes-agent",
    next_action: "preview_ingest",
    ...overrides,
  };
}

const summary: NonNullable<PlanSpecsResponse["summary"]> = {
  draft: 7,
  ready: 5,
  held: 3,
  handed_off: 2,
  running: 4,
  blocked: 1,
  completed: 11,
  archived: 13,
  total_matching: 46,
  observed_at: 1_785_000_000,
};

function renderPlanTab({
  plans = [plan()],
  onShowDetail = vi.fn(),
  readOnly = false,
  stale = false,
}: {
  plans?: PlanSpecRecord[];
  onShowDetail?: (item: PlanSpecRecord) => void;
  readOnly?: boolean;
  stale?: boolean;
} = {}) {
  return render(
    <PlanTab
      allPlanspecs={plans}
      summary={summary}
      onApproveSuccess={vi.fn()}
      onShowDetail={onShowDetail}
      readOnly={readOnly}
      stale={stale}
    />,
  );
}

describe("PlanTab register", () => {
  beforeEach(() => {
    window.localStorage.clear();
    vi.stubGlobal("fetch", vi.fn().mockResolvedValue(new Response(JSON.stringify({
      autonomous: true,
      max_tier_autonomous: "review",
      recent: [],
      anchors: [],
    }), { status: 200, headers: { "Content-Type": "application/json" } })));
  });

  afterEach(() => {
    cleanup();
    window.localStorage.clear();
    vi.unstubAllGlobals();
  });

  it("starts with a compact composer and remembers expansion", () => {
    const first = renderPlanTab();
    const toggle = screen.getByRole("button", { name: "Neuer Plan" });
    expect(toggle.getAttribute("aria-expanded")).toBe("false");
    expect(screen.queryByLabelText("Plan-Text")).toBeNull();

    fireEvent.click(toggle);
    expect(screen.getByRole("button", { name: "Composer schließen" }).getAttribute("aria-expanded")).toBe("true");
    expect(window.localStorage.getItem("fleet-plan-composer-expanded")).toBe("true");
    first.unmount();

    renderPlanTab();
    expect(screen.getByRole("button", { name: "Composer schließen" }).getAttribute("aria-expanded")).toBe("true");
  });

  it("uses the limit-independent server summary once in the register", () => {
    renderPlanTab();

    expect(screen.queryByLabelText("Plan-Kennzahlen")).toBeNull();
    expect(screen.getByRole("tab", { name: "Offen22" })).toBeTruthy();
    expect(screen.getByRole("tab", { name: "Im Board7" })).toBeTruthy();
    expect(screen.getByRole("tab", { name: "Erledigt24" })).toBeTruthy();
  });

  it("filters by workflow segment and full-text search without shortening the label", () => {
    const longTopic = "PlanSpec mit vollständig lesbarem, sehr langem Titel für den Operator";
    renderPlanTab({
      plans: [
        plan({ topic: longTopic }),
        plan({
          path: "vault/03-Agents/Codex/plans/held.md",
          filename: "held.md",
          topic: "Gehaltene Migration",
          action_state: "held",
          target_board: "family-organizer",
        }),
      ],
    });

    expect(screen.getByText(longTopic).textContent).toBe(longTopic);
    fireEvent.click(screen.getByRole("tab", { name: "Gehalten3" }));
    expect(screen.getByText("Gehaltene Migration")).toBeTruthy();
    expect(screen.queryByText(longTopic)).toBeNull();

    fireEvent.click(screen.getByRole("tab", { name: "Offen22" }));
    fireEvent.change(screen.getByLabelText("PlanSpecs durchsuchen"), { target: { value: "family-organizer" } });
    expect(screen.getByText("Gehaltene Migration")).toBeTruthy();
    expect(screen.queryByText(longTopic)).toBeNull();
  });

  it("sortiert stabil nach Zustand, Befunden, Agent und Board", () => {
    const plans = [
      plan({ path: "/plans/ready-first.md", topic: "Bereit zuerst", agent: "zeta", target_board: "alpha" }),
      plan({ path: "/plans/done.md", topic: "Erledigt", action_state: "completed", agent: "beta", target_board: "epsilon" }),
      plan({ path: "/plans/ready-second.md", topic: "Bereit danach", agent: "alpha", target_board: "gamma" }),
      plan({ path: "/plans/attention.md", topic: "Klärung", action_state: "blocked", agent: "delta", target_board: "beta" }),
      plan({ path: "/plans/findings.md", topic: "Mit Befunden", action_state: "draft", finding_count: 2, agent: "gamma", target_board: "delta" }),
    ];
    renderPlanTab({ plans });

    const orderedTopics = () => Array.from(
      screen.getByRole("list", { name: "PlanSpecs" }).querySelectorAll(".fleet-plan-row-title"),
      (node) => node.textContent,
    );

    expect(orderedTopics()).toEqual(["Klärung", "Bereit zuerst", "Bereit danach", "Mit Befunden"]);

    fireEvent.change(screen.getByRole("combobox", { name: "Pläne sortieren" }), { target: { value: "findings" } });
    expect(orderedTopics()).toEqual(["Mit Befunden", "Bereit zuerst", "Bereit danach", "Klärung"]);

    fireEvent.change(screen.getByRole("combobox", { name: "Pläne sortieren" }), { target: { value: "agent" } });
    expect(orderedTopics()).toEqual(["Bereit danach", "Klärung", "Mit Befunden", "Bereit zuerst"]);

    fireEvent.change(screen.getByRole("combobox", { name: "Pläne sortieren" }), { target: { value: "board" } });
    expect(orderedTopics()).toEqual(["Bereit zuerst", "Klärung", "Mit Befunden", "Bereit danach"]);

    // Geschlossene Pläne sortieren sich im Erledigt-Register weiterhin ein.
    fireEvent.click(screen.getByRole("tab", { name: "Erledigt24" }));
    expect(orderedTopics()).toEqual(["Erledigt"]);
  });

  it("blendet geschlossene Pläne aus der Hauptansicht aus — Zähler bleibt, Register erreichbar", () => {
    renderPlanTab({
      plans: [
        plan({ topic: "Offener Plan" }),
        plan({
          path: "vault/03-Agents/Codex/plans/shipped.md",
          filename: "shipped.md",
          topic: "Ausgelieferter Plan",
          open: false,
          status: "shipped",
          closed_reason: "SHIPPED 2026-06-19 (89527c494)",
          action_state: "completed",
        }),
        plan({
          path: "vault/03-Agents/Codex/plans/archived.md",
          filename: "archived.md",
          topic: "Archivierter Plan",
          open: false,
          status: "archived",
          closed_reason: "archived 2026-05-30",
          action_state: "archived",
        }),
      ],
    });

    // Hauptansicht („Offen"): keine geschlossenen Pläne …
    expect(screen.getByText("Offener Plan")).toBeTruthy();
    expect(screen.queryByText("Ausgelieferter Plan")).toBeNull();
    expect(screen.queryByText("Archivierter Plan")).toBeNull();
    // … aber der Zähler am Erledigt-Register beweist: nichts ist gelöscht.
    expect(screen.getByRole("tab", { name: "Erledigt24" })).toBeTruthy();

    // Über das vorhandene Register erreichbar.
    fireEvent.click(screen.getByRole("tab", { name: "Erledigt24" }));
    expect(screen.getByText("Ausgelieferter Plan")).toBeTruthy();
    expect(screen.getByText("Archivierter Plan")).toBeTruthy();
    expect(screen.queryByText("Offener Plan")).toBeNull();
  });

  it("erklärt den Bereit-Zähler neben den wartenden Freigaben (Hermes-Default startet ohne)", () => {
    renderPlanTab({
      plans: [
        plan({ topic: "Wartet auf Operator", freigabe: "operator" }),
        plan({
          path: "vault/03-Agents/Codex/plans/hermes-default.md",
          filename: "hermes-default.md",
          topic: "Hermes-Default ohne Operator-Gate",
          freigabe: "GO",
        }),
      ],
    });

    expect(screen.getByText(
      "Bereit heißt übergabereif: 1 wartet auf deine Freigabe · 1 startet ohne Operator-Freigabe (z. B. Hermes-Defaults).",
    )).toBeTruthy();
  });

  it("opens the selected PlanSpec detail from the row", () => {
    const onShowDetail = vi.fn();
    const item = plan();
    renderPlanTab({ plans: [item], onShowDetail });

    fireEvent.click(screen.getByRole("button", { name: /Board und Plan fertigstellen/ }));
    expect(onShowDetail).toHaveBeenCalledWith(item);
  });

  it("keeps foreign boards read-only and removes transition affordances", () => {
    renderPlanTab({ readOnly: true });

    expect(screen.getByText("Fremd-Board · nur lesen")).toBeTruthy();
    expect(screen.queryByRole("button", { name: "Neuer Plan" })).toBeNull();
    expect(screen.queryByLabelText("Auto-Release-Status")).toBeNull();
    expect(screen.getByRole("listitem")).toBeTruthy();
  });
});

describe("PlanTab — Befund-Echo, Stale-Hinweis, Leerzustände", () => {
  beforeEach(() => {
    window.localStorage.clear();
    vi.stubGlobal("fetch", vi.fn().mockResolvedValue(new Response(JSON.stringify({
      autonomous: true,
      max_tier_autonomous: "review",
      recent: [],
      anchors: [],
    }), { status: 200, headers: { "Content-Type": "application/json" } })));
  });

  afterEach(() => {
    cleanup();
    window.localStorage.clear();
    vi.unstubAllGlobals();
  });

  it("zeigt kein Befund-Meta in der Zeile, wenn der Zähler nur das Abschluss-Echo trägt (Live-Form 2026-08-04)", () => {
    renderPlanTab({
      plans: [
        plan({
          topic: "Nur Echo",
          open: false,
          status: "done",
          closed_reason: "closed status: done",
          action_state: "completed",
          finding_count: 1,
          ingest_findings: ["closed status: done"],
        }),
        plan({
          path: "vault/03-Agents/Codex/plans/real-finding.md",
          filename: "real-finding.md",
          topic: "Echter Befund",
          open: false,
          status: "done",
          closed_reason: "closed status: done",
          action_state: "completed",
          finding_count: 2,
          ingest_findings: ["closed status: done", "scope-less code subtask: ER-S2"],
        }),
      ],
    });

    fireEvent.click(screen.getByRole("tab", { name: "Erledigt24" }));
    const echoRow = screen.getByRole("button", { name: /Nur Echo/ });
    expect(echoRow.textContent).not.toContain("Befunde");
    const realRow = screen.getByRole("button", { name: /Echter Befund/ });
    expect(realRow.textContent).toContain("1 Befunde");
  });

  it("meldet einen fehlgeschlagenen Poll als letzten erfolgreichen Stand statt still zu bleiben", () => {
    renderPlanTab({ stale: true });
    expect(screen.getByText("Aktualisierung fehlgeschlagen · es wird der letzte erfolgreiche Stand gezeigt.")).toBeTruthy();

    cleanup();
    renderPlanTab({ stale: false });
    expect(screen.queryByText("Aktualisierung fehlgeschlagen · es wird der letzte erfolgreiche Stand gezeigt.")).toBeNull();
  });

  it("unterscheidet leere Segmente von Suchfehlschlägen", () => {
    // Live-Lage 2026-08-04: 399 abgeschlossene Pläne, nichts offen.
    renderPlanTab({
      plans: [
        plan({
          topic: "Abgeschlossener Plan",
          open: false,
          status: "done",
          closed_reason: "closed status: done",
          action_state: "completed",
        }),
      ],
    });

    // Default „Offen": kein Filter aktiv — das darf nicht wie ein
    // Suchfehlschlag („Keine Treffer") aussehen.
    expect(screen.getByText("Nichts offen")).toBeTruthy();
    expect(screen.getByText("Abgeschlossene Pläne liegen im Register „Erledigt“.")).toBeTruthy();

    // Anderes leeres Register nennt seinen Namen.
    fireEvent.click(screen.getByRole("tab", { name: "Bereit5" }));
    expect(screen.getByText("Nichts in „Bereit“")).toBeTruthy();

    // Erst eine Suche ohne Treffer ist „Keine Treffer".
    fireEvent.change(screen.getByLabelText("PlanSpecs durchsuchen"), { target: { value: "gibtsnicht" } });
    expect(screen.getByText("Keine Treffer")).toBeTruthy();
    expect(screen.getByText("Suche oder Register anpassen.")).toBeTruthy();
  });

  it("unterdrückt Grundtexte, die nur den Endzustand wiederholen — echte Gründe bleiben (I2)", () => {
    renderPlanTab({
      plans: [
        plan({
          topic: "Echo-Grund",
          open: false,
          status: "done",
          closed_reason: "closed status: done",
          action_state: "completed",
          action_reason: "Die PlanSpec ist abgeschlossen.",
        }),
        plan({
          path: "vault/03-Agents/Codex/plans/blocked.md",
          filename: "blocked.md",
          topic: "Echter Grund",
          action_state: "blocked",
          action_reason: "Lane premium nicht auflösbar.",
        }),
      ],
    });

    // Blockierte Zeile in „Offen": der echte Grund bleibt sichtbar.
    const realRow = screen.getByRole("button", { name: /Echter Grund/ });
    expect(realRow.textContent).toContain("Lane premium nicht auflösbar.");

    // Erledigte Zeile: die Abschluss-Floskel wiederholt nur den Chip.
    fireEvent.click(screen.getByRole("tab", { name: "Erledigt24" }));
    const echoRow = screen.getByRole("button", { name: /Echo-Grund/ });
    expect(echoRow.textContent).not.toContain("abgeschlossen");
  });

  it("zeigt fehlendes action_state als sichtbaren Datenfehler statt zu raten (V1)", () => {
    renderPlanTab({
      plans: [
        plan({
          topic: "Ohne Zustand geschlossen",
          action_state: undefined,
          open: false,
          status: "done",
          closed_reason: "closed status: done",
        }),
        plan({
          path: "vault/03-Agents/Codex/plans/rooted.md",
          filename: "rooted.md",
          topic: "Ohne Zustand mit Wurzel",
          action_state: undefined,
          kanban_root_task_id: "t_1",
          kanban_state: "queued",
          kanban_root_status: "scheduled",
        }),
      ],
    });

    // Beide erscheinen in „Offen" als das, was sie sind: Datensätze ohne
    // Backend-Klassifikation. Der entfernte Fallback hätte sie als
    // „Übergeben"/„Gehalten" (Wurzel) bzw. unsichtbar im Erledigt-Fach gemalt.
    expect(screen.getAllByText("ohne Zustand").length).toBe(2);

    fireEvent.click(screen.getByRole("tab", { name: "Im Board7" }));
    expect(screen.queryByText("Ohne Zustand mit Wurzel")).toBeNull();
    fireEvent.click(screen.getByRole("tab", { name: "Entwurf7" }));
    expect(screen.queryByText("Ohne Zustand geschlossen")).toBeNull();
  });

  it("zeigt den Kettenlauf als eigenes Zeilen-Datum neben dem Plan-Chip (V2)", () => {
    renderPlanTab({
      plans: [
        plan({
          topic: "Plan mit laufender Kette",
          action_state: "running",
          kanban_root_task_id: "t_root1",
          kanban_state: "running",
        }),
      ],
    });

    const row = screen.getByRole("button", { name: /Plan mit laufender Kette/ });
    expect(row.textContent).toContain("Kette: läuft");
    // … und der Chip bleibt der Plan-Zustand, keine Wortmischung.
    expect(row.textContent).toContain("Läuft");
  });
});
