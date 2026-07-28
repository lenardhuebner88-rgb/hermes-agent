// @vitest-environment jsdom

import { cleanup, fireEvent, render, screen, waitFor } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import { _resetPollingStore } from "../hooks/pollingStore";
import { ApprovalInbox } from "./ApprovalInbox";

const api = vi.hoisted(() => ({ fetchJSON: vi.fn() }));
vi.mock("@/lib/api", () => api);

// ── Fixtures: exakt die Response-Formen der Backend-Handler ──────────────────
// Disposition: kanban_db._disposition_item_dict (hermes_cli/kanban_db.py) —
// dieselben Schlüssel, dieselben null-/Zahl-Typen wie das echte Ledger.
function dispositionItem(overrides: Record<string, unknown> = {}) {
  return {
    id: "di_0a1b2c3d",
    source_task_id: "t_src42",
    typ: "risk",
    disposition: "delegate",
    next_action: "Rollback-Plan für die Migration nachziehen",
    severity: "real-risk",
    evidence: "worker-context: Migration ohne Down-Path gemerged",
    status: "open",
    supersedes_id: null,
    created_at: 1_785_000_000,
    decided_at: null,
    decided_by: null,
    ...overrides,
  };
}

// Funnel: funnel.draft_dict (hermes_cli/funnel.py) — geliefert von
// GET /funnel/drafts als {"drafts": [draft_dict, …]}.
function funnelDraft(overrides: Record<string, unknown> = {}) {
  return {
    id: "t_draft7",
    title: "Regenradar-Widget fürs Family-Dashboard",
    created_by: "family",
    assignee: "premium",
    completed_at: 1_785_000_100,
    draft_excerpt: "## Ziel\nRegenradar-Kachel mit stündlichem Refresh…",
    draft_text: "## Ziel\nRegenradar-Kachel mit stündlichem Refresh…\n\n## Umsetzung\n…",
    operator_edited: false,
    revision_of: null,
    spend_alert: false,
    ...overrides,
  };
}

/** Routet GETs auf die beiden Listen; POSTs müssen pro Test verdrahtet werden. */
function routeLists({ drafts = [] as unknown[], items = [] as unknown[] }) {
  api.fetchJSON.mockImplementation((url: string, init?: { method?: string }) => {
    if (init?.method && init.method !== "GET") {
      return Promise.reject(new Error(`unverdrahteter ${init.method} ${url}`));
    }
    if (url.startsWith("/api/plugins/kanban/funnel/drafts")) return Promise.resolve({ drafts });
    if (url.startsWith("/api/plugins/kanban/disposition-items")) return Promise.resolve({ items });
    return Promise.reject(new Error(`unbekannte URL ${url}`));
  });
}

function postCalls() {
  return api.fetchJSON.mock.calls.filter(([, init]) => (init as { method?: string })?.method === "POST");
}

describe("ApprovalInbox", () => {
  beforeEach(() => {
    _resetPollingStore();
    api.fetchJSON.mockReset();
  });

  afterEach(() => {
    cleanup();
    _resetPollingStore();
  });

  it("zeigt beide Rückstaus mit den echten Backend-Feldern", async () => {
    routeLists({ drafts: [funnelDraft()], items: [dispositionItem()] });
    render(<ApprovalInbox />);

    await waitFor(() => screen.getByText("Regenradar-Widget fürs Family-Dashboard"));
    // Quelle + Excerpt aus draft_dict; Deep-Link auf den Task im Fleet.
    screen.getByText("Family");
    screen.getByText(/Regenradar-Kachel mit stündlichem Refresh/);
    expect(screen.getAllByRole("link", { name: /Task ansehen/ })[0].getAttribute("href"))
      .toBe("/control/fleet?task=t_draft7");

    // Disposition: typ/severity/disposition-Labels + next_action + evidence.
    screen.getByText("Risiko");
    screen.getByText("Echtes Risiko");
    screen.getByText("Rollback-Plan für die Migration nachziehen");
    screen.getByText(/Migration ohne Down-Path gemerged/);
    expect(screen.getAllByRole("link", { name: /Task ansehen/ })[1].getAttribute("href"))
      .toBe("/control/fleet?task=t_src42");
  });

  it("Disposition accept: POST auf /accept, Zeile verschwindet erst nach Server-Reload", async () => {
    const item = dispositionItem();
    routeLists({ items: [item] });
    render(<ApprovalInbox />);
    await waitFor(() => screen.getByText("Rollback-Plan für die Migration nachziehen"));

    // Zwei-Schritt: erst scharfschalten, dann bestätigen.
    fireEvent.click(screen.getByRole("button", { name: /^Akzeptieren$/ }));
    api.fetchJSON.mockImplementation((url: string, init?: { method?: string; body?: string }) => {
      if (init?.method === "POST") {
        // Backend accept: {"item": <updated dict mit status accepted>}
        return Promise.resolve({ item: { ...item, status: "accepted", decided_at: 1_785_000_500, decided_by: "operator" } });
      }
      if (url.startsWith("/api/plugins/kanban/disposition-items")) return Promise.resolve({ items: [] });
      if (url.startsWith("/api/plugins/kanban/funnel/drafts")) return Promise.resolve({ drafts: [] });
      return Promise.reject(new Error(`unbekannte URL ${url}`));
    });
    fireEvent.click(screen.getByRole("button", { name: /Akzeptieren · Bestätigen/ }));

    await waitFor(() => expect(postCalls()).toHaveLength(1));
    expect(postCalls()[0][0]).toBe("/api/plugins/kanban/disposition-items/di_0a1b2c3d/accept");
    // Kein optimistisches Ausblenden: die Zeile geht erst mit dem Reload weg,
    // der sie server-seitig nicht mehr liefert → ruhiger Leerzustand.
    await waitFor(() => screen.getByText(/Nichts wartet auf Freigabe/));
    expect(screen.queryByText("Rollback-Plan für die Migration nachziehen")).toBeNull();
  });

  it("Disposition dismiss schickt den Grund als JSON-Body", async () => {
    routeLists({ items: [dispositionItem()] });
    render(<ApprovalInbox />);
    await waitFor(() => screen.getByText("Rollback-Plan für die Migration nachziehen"));

    fireEvent.click(screen.getByRole("button", { name: /^Verwerfen$/ }));
    fireEvent.change(screen.getByPlaceholderText("Warum wird dieses Item verworfen?"), {
      target: { value: "Bereits in t_src99 gefixt" },
    });
    api.fetchJSON.mockImplementation((url: string, init?: { method?: string }) => {
      if (init?.method === "POST") return Promise.resolve({ item: dispositionItem({ status: "dismissed" }) });
      if (url.startsWith("/api/plugins/kanban/disposition-items")) return Promise.resolve({ items: [] });
      if (url.startsWith("/api/plugins/kanban/funnel/drafts")) return Promise.resolve({ drafts: [] });
      return Promise.reject(new Error(`unbekannte URL ${url}`));
    });
    fireEvent.click(screen.getByRole("button", { name: /Verwerfen · Bestätigen/ }));

    await waitFor(() => expect(postCalls()).toHaveLength(1));
    const [url, init] = postCalls()[0] as [string, { body: string }];
    expect(url).toBe("/api/plugins/kanban/disposition-items/di_0a1b2c3d/dismiss");
    expect(JSON.parse(init.body)).toEqual({ reason: "Bereits in t_src99 gefixt" });
  });

  it("Disposition create-fix-task: POST auf /create-fix-task", async () => {
    routeLists({ items: [dispositionItem()] });
    render(<ApprovalInbox />);
    await waitFor(() => screen.getByText("Rollback-Plan für die Migration nachziehen"));

    fireEvent.click(screen.getByRole("button", { name: /^Fix-Task anlegen$/ }));
    api.fetchJSON.mockImplementation((url: string, init?: { method?: string }) => {
      if (init?.method === "POST") {
        // Backend create-fix-task: {"fix_task": …, "item": {"status": "task_created"}}
        return Promise.resolve({ fix_task: { id: "t_fix1" }, item: dispositionItem({ status: "task_created" }) });
      }
      if (url.startsWith("/api/plugins/kanban/disposition-items")) return Promise.resolve({ items: [] });
      if (url.startsWith("/api/plugins/kanban/funnel/drafts")) return Promise.resolve({ drafts: [] });
      return Promise.reject(new Error(`unbekannte URL ${url}`));
    });
    fireEvent.click(screen.getByRole("button", { name: /Fix-Task anlegen · Bestätigen/ }));

    await waitFor(() => expect(postCalls()).toHaveLength(1));
    expect(postCalls()[0][0]).toBe("/api/plugins/kanban/disposition-items/di_0a1b2c3d/create-fix-task");
  });

  it("Scope-Hinweis bietet keinen Fix-Task an", async () => {
    routeLists({ items: [dispositionItem({ severity: "scope-note" })] });
    render(<ApprovalInbox />);
    await waitFor(() => screen.getByText("Rollback-Plan für die Migration nachziehen"));
    expect(screen.queryByRole("button", { name: /Fix-Task/ })).toBeNull();
    screen.getByText(/Scope-Hinweis — kein Fix-Task nötig/);
  });

  it("Funnel approve: POST auf /approve", async () => {
    routeLists({ drafts: [funnelDraft()] });
    render(<ApprovalInbox />);
    await waitFor(() => screen.getByText("Regenradar-Widget fürs Family-Dashboard"));

    fireEvent.click(screen.getByRole("button", { name: /^Freigeben$/ }));
    api.fetchJSON.mockImplementation((url: string, init?: { method?: string }) => {
      if (init?.method === "POST") {
        // Backend approve: {"task": <neues Build-Kind>}
        return Promise.resolve({ task: { id: "t_build1", title: "Umsetzen: Regenradar-Widget fürs Family-Dashboard" } });
      }
      if (url.startsWith("/api/plugins/kanban/funnel/drafts")) return Promise.resolve({ drafts: [] });
      if (url.startsWith("/api/plugins/kanban/disposition-items")) return Promise.resolve({ items: [] });
      return Promise.reject(new Error(`unbekannte URL ${url}`));
    });
    fireEvent.click(screen.getByRole("button", { name: /Freigeben · Bestätigen/ }));

    await waitFor(() => expect(postCalls()).toHaveLength(1));
    expect(postCalls()[0][0]).toBe("/api/plugins/kanban/funnel/drafts/t_draft7/approve");
    await waitFor(() => screen.getByText(/Nichts wartet auf Freigabe/));
  });

  it("Funnel revise schickt den bearbeiteten Draft-Text", async () => {
    routeLists({ drafts: [funnelDraft()] });
    render(<ApprovalInbox />);
    await waitFor(() => screen.getByText("Regenradar-Widget fürs Family-Dashboard"));

    fireEvent.click(screen.getByRole("button", { name: /^Überarbeiten$/ }));
    const textarea = screen.getByLabelText("Draft-Text (Pflicht)") as HTMLTextAreaElement;
    // Vorbefüllt mit dem kanonischen draft_text aus draft_dict.
    expect(textarea.value).toContain("## Ziel");
    fireEvent.change(textarea, { target: { value: "## Ziel\nRadar UND Unwetterwarnungen" } });

    api.fetchJSON.mockImplementation((url: string, init?: { method?: string }) => {
      if (init?.method === "POST") {
        // Backend revise: {"task": <neuer Überarbeiten-Task>, "superseded": <alt>}
        return Promise.resolve({ task: { id: "t_rev1" }, superseded: "t_draft7" });
      }
      if (url.startsWith("/api/plugins/kanban/funnel/drafts")) return Promise.resolve({ drafts: [] });
      if (url.startsWith("/api/plugins/kanban/disposition-items")) return Promise.resolve({ items: [] });
      return Promise.reject(new Error(`unbekannte URL ${url}`));
    });
    fireEvent.click(screen.getByRole("button", { name: /Überarbeiten · Bestätigen/ }));

    await waitFor(() => expect(postCalls()).toHaveLength(1));
    const [url, init] = postCalls()[0] as [string, { body: string }];
    expect(url).toBe("/api/plugins/kanban/funnel/drafts/t_draft7/revise");
    expect(JSON.parse(init.body)).toEqual({
      draft_text: "## Ziel\nRadar UND Unwetterwarnungen",
      operator_note: "",
    });
  });

  it("Funnel dismiss: POST auf /dismiss", async () => {
    routeLists({ drafts: [funnelDraft()] });
    render(<ApprovalInbox />);
    await waitFor(() => screen.getByText("Regenradar-Widget fürs Family-Dashboard"));

    fireEvent.click(screen.getByRole("button", { name: /^Verwerfen$/ }));
    api.fetchJSON.mockImplementation((url: string, init?: { method?: string }) => {
      if (init?.method === "POST") return Promise.resolve({ ok: true, task_id: "t_draft7" });
      if (url.startsWith("/api/plugins/kanban/funnel/drafts")) return Promise.resolve({ drafts: [] });
      if (url.startsWith("/api/plugins/kanban/disposition-items")) return Promise.resolve({ items: [] });
      return Promise.reject(new Error(`unbekannte URL ${url}`));
    });
    fireEvent.click(screen.getByRole("button", { name: /Verwerfen · Bestätigen/ }));

    await waitFor(() => expect(postCalls()).toHaveLength(1));
    expect(postCalls()[0][0]).toBe("/api/plugins/kanban/funnel/drafts/t_draft7/dismiss");
  });

  it("Fehlerfall: 409 lässt die Zeile stehen und zeigt das Backend-Detail", async () => {
    routeLists({ items: [dispositionItem()] });
    render(<ApprovalInbox />);
    await waitFor(() => screen.getByText("Rollback-Plan für die Migration nachziehen"));

    fireEvent.click(screen.getByRole("button", { name: /^Akzeptieren$/ }));
    // fetchJSON wirft Error("<status>: <body>") — das 409-Detail des Backends.
    api.fetchJSON.mockImplementation((url: string, init?: { method?: string }) => {
      if (init?.method === "POST") {
        return Promise.reject(new Error('409: {"detail":"disposition item di_0a1b2c3d is not open"}'));
      }
      if (url.startsWith("/api/plugins/kanban/disposition-items")) return Promise.resolve({ items: [dispositionItem()] });
      if (url.startsWith("/api/plugins/kanban/funnel/drafts")) return Promise.resolve({ drafts: [] });
      return Promise.reject(new Error(`unbekannte URL ${url}`));
    });
    fireEvent.click(screen.getByRole("button", { name: /Akzeptieren · Bestätigen/ }));

    // Zeile bleibt stehen, Grund aus der Backend-Antwort wird angezeigt,
    // Bestätigungszustand bleibt offen (erneuter Versuch möglich).
    await waitFor(() => screen.getByRole("alert"));
    screen.getByText("disposition item di_0a1b2c3d is not open");
    screen.getByText("Rollback-Plan für die Migration nachziehen");
    screen.getByRole("button", { name: /Akzeptieren · Bestätigen/ });
    expect(screen.queryByText(/Nichts wartet auf Freigabe/)).toBeNull();
  });

  it("Leerzustand ist ein guter Zustand (ruhig, kein Fehlerkasten)", async () => {
    routeLists({ drafts: [], items: [] });
    render(<ApprovalInbox />);
    await waitFor(() => screen.getByText(/Nichts wartet auf Freigabe/));
    // Keine Aktions-Buttons, kein Alarm — nur die ruhige Ansage.
    expect(screen.queryByRole("button")).toBeNull();
    expect(screen.queryByRole("alert")).toBeNull();
  });

  it("Ladefehler der Liste wird als solcher angezeigt (nicht als Leere)", async () => {
    api.fetchJSON.mockImplementation((url: string) => {
      if (url.startsWith("/api/plugins/kanban/funnel/drafts")) return Promise.reject(new Error("503: boom"));
      if (url.startsWith("/api/plugins/kanban/disposition-items")) return Promise.resolve({ items: [] });
      return Promise.reject(new Error(`unbekannte URL ${url}`));
    });
    render(<ApprovalInbox />);
    await waitFor(() => screen.getByText(/503: boom/));
    expect(screen.queryByText(/Nichts wartet auf Freigabe/)).toBeNull();
  });
});
