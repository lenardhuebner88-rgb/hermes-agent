// @vitest-environment jsdom

import { readFileSync } from "node:fs";
import path from "node:path";
import { cleanup, configure, fireEvent, render, screen, waitFor } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import { PlanTab } from "./PlanTab";
import type { PlanSpecRecord } from "./shared";
import { extractIngestError } from "../../lib/fleetHub";
import { de } from "../../i18n/de";

const src = readFileSync(path.resolve(import.meta.dirname, "PlanTab.tsx"), "utf8");

// Voll-Suite-Last kann waitFor über den Default (1s) hinaus bouncen — großzügiger
// Timeout hält den Ingest-Roundtrip deterministisch.
configure({ asyncUtilTimeout: 5000 });

describe("PlanTab profile approvals", () => {
  it("uses profile selection and sends assignee overrides instead of lane model ids", () => {
    expect(src).toContain("assigneeOverrides");
    expect(src).toContain("<ProfileSelect");
    expect(src).toContain("lanesCatalog?.profiles");
    expect(src).not.toContain("setLaneModels");
    expect(src).not.toContain("lane_models");
  });

  it("shows profile-specific accessible copy", () => {
    expect(src).toContain("Profil-Select (je Lane)");
    expect(src).toContain("Profil für Lane");
  });
});

describe("PlanTab provider usage wiring", () => {
  it("uses the shared provider/window model and keeps detail plus freshness", () => {
    expect(src).toContain("usageProviderLabel(prov)");
    expect(src).toContain("sortedUsageWindows(prov)");
    expect(src).toContain("windowLabelDe(w)");
    expect(src).toContain("w.detail");
    expect(src).toContain("staleUsageSignalLabel(prov");
    expect(src).toContain("setInterval(() => setNowMs(Date.now()), 60_000)");
    expect(src).not.toContain("renderedAtMs");
    expect(src).not.toContain("prov.title || prov.provider");
  });
});

describe("PlanTab composer collapse", () => {
  afterEach(() => {
    cleanup();
    window.localStorage.clear();
  });

  it("starts collapsed and remembers the expanded state", () => {
    const first = renderPlanTab();
    const toggle = screen.getByRole("button", { name: "Plan-Composer aufklappen" });
    expect(toggle.getAttribute("aria-expanded")).toBe("false");
    expect(screen.queryByLabelText("Plan-Text")).toBeNull();

    fireEvent.click(toggle);
    expect(screen.getByRole("button", { name: "Plan-Composer einklappen" }).getAttribute("aria-expanded")).toBe("true");
    expect(window.localStorage.getItem("fleet-plan-composer-expanded")).toBe("true");
    first.unmount();

    renderPlanTab();
    expect(screen.getByRole("button", { name: "Plan-Composer einklappen" }).getAttribute("aria-expanded")).toBe("true");
  });
});

describe("PlanTab ingest wiring (source pins)", () => {
  it("posts to the ingest route with the spec path", () => {
    expect(src).toContain('"/api/plugins/kanban/planspecs/ingest"');
    expect(src).toContain("JSON.stringify({ path: ps.path })");
  });

  it("drives release-eligibility off effectiveRootId, not the raw prop", () => {
    // AC-6: solange ps.kanban_root_task_id gesetzt ist, ist effectiveRootId
    // identisch — der Bestandspfad bleibt unverändert. Der Button-Disabled darf
    // nach lokalem Ingest nicht mehr am rohen Prop hängen.
    expect(src).toContain("const effectiveRootId = ps.kanban_root_task_id ?? ingestedRootId");
    expect(src).not.toContain("|| !ps.kanban_root_task_id");
  });
});

describe("extractIngestError", () => {
  it("liest findings aus dem 400-FastAPI-Envelope", () => {
    const err = new Error('400: {"detail":{"findings":["Slice A ohne done-when","Slice B ohne files"]}}');
    expect(extractIngestError(err, "fallback")).toBe("Slice A ohne done-when · Slice B ohne files");
  });

  it("liest findings von der obersten Ebene", () => {
    const err = new Error('400: {"findings":["nur eins"]}');
    expect(extractIngestError(err, "fallback")).toBe("nur eins");
  });

  it("nutzt detail als String, wenn keine findings vorliegen", () => {
    const err = new Error('500: {"detail":"interner Fehler"}');
    expect(extractIngestError(err, "fallback")).toBe("interner Fehler");
  });

  it("fällt auf die rohe Meldung zurück, wenn der Body kein JSON ist", () => {
    const err = new Error("network timeout after 8000ms");
    expect(extractIngestError(err, "fallback")).toBe("network timeout after 8000ms");
  });
});

const notIngestedSpec: PlanSpecRecord = {
  path: "vault/03-Agents/Claude/plans/2026-07-07-ingest-demo.md",
  agent: "claude",
  filename: "2026-07-07-ingest-demo.md",
  topic: "Ingest-Demo",
  status: "open",
  freigabe: "operator",
  live_test_depth: null,
  binding: true,
  subtask_count: 1,
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

// Regression-Fixture (AC-5): dieselbe PlanSpec, aber bereits ingestiert —
// kanban_root_task_id gesetzt, Kette queued. Freigabe wartet weiter auf den
// Operator (freigabe="operator"), darf also den Freigeben-Modus zeigen und NIE
// den Ingest-Button. freigabe bleibt "operator" (≠ "complete") → kein signierter
// Parked-Chain, der Freigeben-Button (nicht "Kette starten") erscheint.
const ingestedSpec: PlanSpecRecord = {
  ...notIngestedSpec,
  path: "vault/03-Agents/Claude/plans/2026-07-07-ingested-demo.md",
  filename: "2026-07-07-ingested-demo.md",
  topic: "Ingested-Demo",
  kanban_root_task_id: "t_existing_root",
  kanban_root_status: "queued",
  kanban_state: "queued",
  kanban_child_total: 3,
};

function jsonResponse(body: unknown, status = 200): Response {
  return new Response(JSON.stringify(body), {
    status,
    headers: { "Content-Type": "application/json" },
  });
}

function renderPlanTab(specs: PlanSpecRecord[] = [notIngestedSpec]) {
  return render(
    <PlanTab
      allPlanspecs={specs}
      costs={null}
      lanesCatalog={null}
      accountUsage={null}
      onApproveSuccess={vi.fn()}
      onShowDetail={vi.fn()}
    />,
  );
}

describe("PlanTab ingest behaviour", () => {
  const fetchMock = vi.fn();
  // Pro Test austauschbar: die Antwort der /planspecs/ingest-Route.
  let ingestResponder: () => Response;

  beforeEach(() => {
    vi.clearAllMocks();
    Object.defineProperty(window, "__HERMES_SESSION_TOKEN__", {
      configurable: true,
      value: "test-token",
    });
    ingestResponder = () => jsonResponse({ root_task_id: "t_new", child_ids: ["t_c1"] });
    fetchMock.mockImplementation((url: string) => {
      const u = String(url);
      if (u.includes("/planspecs/ingest")) return Promise.resolve(ingestResponder());
      if (u.includes("/planspecs/detail")) return Promise.resolve(jsonResponse({ goal: "Demo", subtasks: [] }));
      // release-status und alles Übrige: harmlose leere Antwort.
      return Promise.resolve(jsonResponse({}));
    });
    vi.stubGlobal("fetch", fetchMock);
  });

  afterEach(() => {
    cleanup();
    vi.unstubAllGlobals();
  });

  it("AC-1/AC-2: zeigt einen aktiven Ingest-Button plus Hinweistext statt eines Freigeben-Buttons", async () => {
    renderPlanTab();
    const ingestBtn = await screen.findByRole("button", { name: de.fleet.planIngestKarte });
    expect((ingestBtn as HTMLButtonElement).disabled).toBe(false);
    expect(screen.getByText(de.fleet.planIngestHinweis)).toBeTruthy();
    expect(screen.queryByRole("button", { name: de.fleet.planFreigeben })).toBeNull();
  });

  it("AC-3/AC-4: klickt Ingest → POST { path } und wechselt bei Erfolg in den Freigabe-Modus", async () => {
    renderPlanTab();
    fireEvent.click(await screen.findByRole("button", { name: de.fleet.planIngestKarte }));

    // AC-4: Karte wechselt in den Freigabe-Modus — Freigeben-Button aktiv.
    const freigeben = await screen.findByRole("button", { name: de.fleet.planFreigeben });
    expect((freigeben as HTMLButtonElement).disabled).toBe(false);

    // AC-3: korrekter Endpoint + Body.
    const ingestCall = fetchMock.mock.calls.find(([u]) => String(u).includes("/planspecs/ingest"));
    expect(ingestCall).toBeTruthy();
    expect(String(ingestCall![0])).toBe("/api/plugins/kanban/planspecs/ingest");
    const opts = ingestCall![1] as RequestInit;
    expect(opts.method).toBe("POST");
    expect(JSON.parse(String(opts.body))).toEqual({ path: notIngestedSpec.path });
  });

  it("AC-5: zeigt bei einem 400 die findings aus der Response an", async () => {
    ingestResponder = () =>
      jsonResponse({ detail: { findings: ["Slice A ohne done-when", "Slice B ohne files"] } }, 400);
    renderPlanTab();
    fireEvent.click(await screen.findByRole("button", { name: de.fleet.planIngestKarte }));

    await waitFor(() => expect(screen.getByText(/Slice A ohne done-when/)).toBeTruthy());
    // Bleibt im Ingest-Modus — kein Freigeben-Button.
    expect(screen.queryByRole("button", { name: de.fleet.planFreigeben })).toBeNull();
  });

  it("AC-5 (Regression): Karte mit gesetztem kanban_root_task_id zeigt weiter Freigeben, keinen Ingest-Button", async () => {
    renderPlanTab([ingestedSpec]);

    // Freigeben-Modus: aktiver Freigeben-Button, kein Ingest-Button/-Hinweis.
    const freigeben = await screen.findByRole("button", { name: de.fleet.planFreigeben });
    expect((freigeben as HTMLButtonElement).disabled).toBe(false);
    expect(screen.queryByRole("button", { name: de.fleet.planIngestKarte })).toBeNull();
    expect(screen.queryByText(de.fleet.planIngestHinweis)).toBeNull();

    // Reine Anzeige (kein Klick) darf die Ingest-Route nie berühren.
    expect(fetchMock.mock.calls.some(([u]) => String(u).includes("/planspecs/ingest"))).toBe(false);
  });

  it("selects a pending PlanSpec when the click lands on its CSS-ellipsis label", () => {
    const longTopic = "Ein vollständiger PlanSpec-Name, der deutlich länger als zweiundzwanzig Zeichen ist";
    renderPlanTab([
      { ...notIngestedSpec, topic: longTopic },
      { ...ingestedSpec, path: "/tmp/second-plan.md", topic: "Zweiter Plan" },
    ]);

    const longLabel = screen.getByText(longTopic, { selector: ".fleet-kchip-label" });
    expect(longLabel.textContent).toBe(longTopic);

    const secondLabel = screen.getByText("Zweiter Plan", { selector: ".fleet-kchip-label" });
    const secondChip = secondLabel.closest("button");
    expect(secondChip?.getAttribute("aria-pressed")).toBe("false");

    // Der Klick muss auf dem Label-Kind landen: stopPropagation im früheren
    // ExpandableText maskierte genau diesen realen Tap-Pfad.
    fireEvent.click(secondLabel);
    expect(secondChip?.getAttribute("aria-pressed")).toBe("true");
    expect(secondLabel.getAttribute("aria-expanded")).toBeNull();
  });
});
