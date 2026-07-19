// @vitest-environment jsdom
/**
 * PlanspecDraft (S3.3-FE) — Beweisbare Invarianten des /plan-Draft-Flows:
 *  1. „/plan <idee>" in der Frag-Leiste → POST draft (Request-Shape inkl.
 *     engine/model der Switcher-Wahl), KEIN normaler Chat-Turn; pending-
 *     Zustand „JARVIS PLANT"; danach Draft-Card im Thread (client-intern).
 *  2. Card-Render je Validate-Status: CLEAN (grün, Button aktiv), WARN
 *     (amber + Findings offen), BLOCK (rot + Findings + Button disabled +
 *     Erklärung). Slices als id · title · lane · deps; Planspec-Text im
 *     <details>.
 *  3. Propose: CLEAN/WARN → POST propose → Erfolgs-Hinweis mit #question_id
 *     + sofortiger Inbox-Refresh (die PLANSPEC-Card taucht im Wartet-Panel
 *     auf). BLOCK → kein POST möglich.
 *  4. Fehlerpfade: 422 (detail-Objekt {error, engine_output}) und 400
 *     (BLOCK/stale beim Propose) landen lesbar an der Card — nie still.
 *  5. „/plan" ohne Idee → Usage-Hinweis (role=alert), kein API-Call.
 * Payload-Shapes: exakt der Wire aus hermes_cli/pa_planspec.py (S3.3).
 */
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { cleanup, configure, fireEvent, render, screen } from "@testing-library/react";
import { MemoryRouter } from "react-router-dom";

import type { PaChatMessage, PaEnginesResponse, PaInboxItem, PaPlanspecDraft } from "@/lib/api";
import { _resetPollingStore } from "../hooks/pollingStore";
import { _resetEngineChoice, setEngineChoice } from "./engineSelection";

// Voll-Suite-Last kann waitFor über den Default (1s) hinaus bouncen
// (gleiche Vorsicht wie JarvisChat.test.tsx).
configure({ asyncUtilTimeout: 5000 });

const listPaMessagesMock = vi.hoisted(() => vi.fn());
const sendPaMessageMock = vi.hoisted(() => vi.fn());
const getPaTurnMock = vi.hoisted(() => vi.fn());
const getPaEnginesMock = vi.hoisted(() => vi.fn());
const getPaInboxMock = vi.hoisted(() => vi.fn());
const draftPlanspecMock = vi.hoisted(() => vi.fn());
const proposePlanspecMock = vi.hoisted(() => vi.fn());

vi.mock("@/lib/api", async () => {
  const actual = await vi.importActual<typeof import("@/lib/api")>("@/lib/api");
  return {
    ...actual,
    api: {
      ...actual.api,
      listPaMessages: listPaMessagesMock,
      sendPaMessage: sendPaMessageMock,
      getPaTurn: getPaTurnMock,
      getPaEngines: getPaEnginesMock,
      getPaInbox: getPaInboxMock,
      draftPlanspec: draftPlanspecMock,
      proposePlanspec: proposePlanspecMock,
    },
  };
});

import { JarvisChat } from "./JarvisChat";
import { WartetPanel } from "./WartetPanel";

const ROSTER: PaEnginesResponse = {
  default_engine: "sol",
  engines: [
    { engine: "sol", models: ["gpt-5.6-sol"], default_model: "gpt-5.6-sol", supports_images: true },
    {
      engine: "claude",
      models: ["opus-4.8", "fable-5"],
      default_model: "opus-4.8",
      supports_images: false,
    },
  ],
};

function makeDraft(overrides: Partial<PaPlanspecDraft> = {}): PaPlanspecDraft {
  return {
    draft_id: "draft_0123456789abcdef01234567",
    planspec_text:
      "---\nfreigabe: operator\nlive_test_depth: contract\n---\n\nZiel und Grenzen.\n",
    validation: { status: "CLEAN", findings: [] },
    slices: [
      { id: "S1", title: "Endpoint und Tests implementieren", lane: "coder", deps: [] },
      { id: "S2", title: "Verhalten unabhängig verifizieren", lane: "verifier", deps: ["S1"] },
    ],
    ...overrides,
  };
}

const WARN_FINDING =
  "over-decomposed: 3 fully independent subtasks share web/src/control/ with no dependency edges";
const BLOCK_FINDING = "PA-PlanSpecs müssen freigabe: operator verwenden";

/** Inbox-Item, wie es das Backend nach dem Propose liefert (pa_chat.py
 *  build_inbox; Titel = build_ingest_question-Text aus S3.3). */
const PLANSPEC_INBOX_ITEM: PaInboxItem = {
  type: "pa_action",
  id: "q123",
  question_id: 123,
  title:
    "PlanSpec als gehaltene Kette ingesten?\nDraft: `draft_0123456789abcdef01234567`\n" +
    "Validate: CLEAN (0 Findings)\nGates: freigabe=operator · live_test_depth=contract\n" +
    "Slices (2):\n- `S1` [coder] Endpoint und Tests implementieren · deps: —\n" +
    "- `S2` [verifier] Verhalten unabhängig verifizieren · deps: S1\n" +
    "Grund: Validierten PlanSpec-Entwurf als gehaltene Kette anlegen",
  kind: "pa_action",
  category: "planspec.ingest",
  action_payload: {
    version: 1,
    category: "planspec.ingest",
    payload: { draft_id: "draft_0123456789abcdef01234567" },
    reason: "Validierten PlanSpec-Entwurf als gehaltene Kette anlegen",
  },
  options: [
    { nr: 1, label: "Ausführen", recommended: false },
    { nr: 2, label: "Ablehnen", recommended: false },
  ],
  block_radius: 1,
  ts: 1753000000,
};

beforeEach(() => {
  _resetPollingStore();
  _resetEngineChoice();
  listPaMessagesMock.mockImplementation(async () => ({
    messages: [] as PaChatMessage[],
    next_before_id: null,
  }));
  sendPaMessageMock.mockResolvedValue({ turn_id: "turn_unused" });
  getPaTurnMock.mockResolvedValue({
    turn_id: "turn_unused",
    status: "done",
    reply: null,
    engine: "sol",
    model: "gpt-5.6-sol",
    ts: 1753000000,
    error: null,
  });
  getPaEnginesMock.mockResolvedValue(ROSTER);
  getPaInboxMock.mockResolvedValue({ generated_at: 1753000000, items: [], errors: [] });
  draftPlanspecMock.mockResolvedValue(makeDraft());
  proposePlanspecMock.mockResolvedValue({ question_id: 123 });
});

afterEach(() => {
  cleanup();
  vi.clearAllMocks();
  _resetPollingStore();
  _resetEngineChoice();
});

/** Schnelle Turn-Poll-Kadenz statt der produktiven 1,5 s. */
function renderChat() {
  return render(<JarvisChat turnPollIntervalMs={25} />);
}

async function submitPlan(idea: string) {
  const input = await screen.findByLabelText("Nachricht an Jarvis");
  fireEvent.change(input, { target: { value: idea } });
  fireEvent.click(screen.getByLabelText("Nachricht senden"));
}

describe("PlanspecDraft (/plan-Flow, Wire aus pa_planspec.py)", () => {
  it("„/plan <idee>“ → draft-POST ohne Switcher-Wahl, kein Chat-Turn, CLEAN-Card komplett", async () => {
    renderChat();

    await submitPlan("/plan mache einen Health-Report");

    // Kein Chat-Turn, nur der Draft-Endpoint.
    expect(draftPlanspecMock).toHaveBeenCalledWith("mache einen Health-Report", undefined);
    expect(sendPaMessageMock).not.toHaveBeenCalled();

    // User-Bubble mit der /plan-Eingabe + Card mit allen Wire-Feldern.
    expect(await screen.findByText("/plan mache einen Health-Report")).toBeTruthy();
    const card = await screen.findByTestId("jv-plan-draft_0123456789abcdef01234567");
    expect(card.textContent).toContain("PLANSPEC-ENTWURF");
    expect(card.textContent).toContain("VALIDATE: CLEAN");
    expect(card.textContent).toContain("draft_0123456789abcdef01234567");
    // Slices: id · title · lane · deps.
    expect(card.textContent).toContain("Endpoint und Tests implementieren");
    expect(card.textContent).toContain("[verifier]");
    expect(card.textContent).toContain("deps: S1");
    // Planspec-Text liegt im (eingeklappten) <details>.
    expect(card.textContent).toContain("freigabe: operator");
    // Provenienz-Badge: Roster-Default (keine Switcher-Wahl).
    expect(card.textContent).toContain("sol · gpt-5.6-sol");
    // CLEAN → Propose-Button aktiv.
    const button = screen.getByRole("button", { name: "Als Approval einreichen" });
    expect((button as HTMLButtonElement).disabled).toBe(false);
  });

  it("Switcher-Wahl reist als engine+model im draft-POST mit (Badge zeigt die Wahl)", async () => {
    renderChat();
    await screen.findByLabelText("Nachricht an Jarvis");
    setEngineChoice({ engine: "claude", model: "opus-4.8" });

    await submitPlan("/plan baue ein Dashboard-Panel");

    expect(draftPlanspecMock).toHaveBeenCalledWith("baue ein Dashboard-Panel", {
      engine: "claude",
      model: "opus-4.8",
    });
    const card = await screen.findByTestId("jv-plan-draft_0123456789abcdef01234567");
    expect(card.textContent).toContain("claude · opus-4.8");
  });

  it("pending: „JARVIS PLANT“ während des Drafts, verschwindet mit der Card", async () => {
    let resolveDraft!: (draft: PaPlanspecDraft) => void;
    draftPlanspecMock.mockImplementation(
      () => new Promise<PaPlanspecDraft>((resolve) => { resolveDraft = resolve; }),
    );
    renderChat();

    await submitPlan("/plan etwas Langsames");

    expect(await screen.findByRole("status", { name: "Jarvis plant …" })).toBeTruthy();
    resolveDraft(makeDraft());
    expect(await screen.findByTestId("jv-plan-draft_0123456789abcdef01234567")).toBeTruthy();
    expect(screen.queryByRole("status", { name: "Jarvis plant …" })).toBeNull();
  });

  it("WARN-Card: amber-Status + Findings offen sichtbar, Propose erlaubt", async () => {
    draftPlanspecMock.mockResolvedValue(
      makeDraft({ validation: { status: "WARN", findings: [WARN_FINDING] } }),
    );
    renderChat();

    await submitPlan("/plan drei unabhängige Panels");

    const card = await screen.findByTestId("jv-plan-draft_0123456789abcdef01234567");
    expect(card.textContent).toContain("VALIDATE: WARN");
    expect(card.querySelector(".jv-plan-status")?.className).toContain("jv-st-warn");
    // Findings sind VOR dem Tap sichtbar (details offen), nicht versteckt.
    const details = card.querySelector("details");
    expect(details?.open).toBe(true);
    expect(card.textContent).toContain(WARN_FINDING);
    const button = screen.getByRole("button", { name: "Als Approval einreichen" });
    expect((button as HTMLButtonElement).disabled).toBe(false);
  });

  it("BLOCK-Card: Button disabled + Erklärung + Findings, kein propose-POST", async () => {
    draftPlanspecMock.mockResolvedValue(
      makeDraft({ validation: { status: "BLOCK", findings: [BLOCK_FINDING] } }),
    );
    renderChat();

    await submitPlan("/plan autonome Kette ohne Operator");

    const card = await screen.findByTestId("jv-plan-draft_0123456789abcdef01234567");
    expect(card.textContent).toContain("VALIDATE: BLOCK");
    expect(card.querySelector(".jv-plan-status")?.className).toContain("jv-st-block");
    expect(card.textContent).toContain(BLOCK_FINDING);
    expect(card.textContent).toContain("nicht einreichbar");
    const button = screen.getByRole("button", { name: "Als Approval einreichen" });
    expect((button as HTMLButtonElement).disabled).toBe(true);
    fireEvent.click(button);
    expect(proposePlanspecMock).not.toHaveBeenCalled();
  });

  it("Propose-Erfolg: Hinweis mit #question_id, Inbox-Refresh — Card taucht im Wartet-Panel auf", async () => {
    // Inbox liefert die PLANSPEC-Card, sobald das Propose durch ist — der
    // Hook stößt nach dem Erfolg refresh("pa/inbox") an (geteilter Store).
    let proposed = false;
    proposePlanspecMock.mockImplementation(async () => {
      proposed = true;
      return { question_id: 123 };
    });
    getPaInboxMock.mockImplementation(async () => ({
      generated_at: 1753000000,
      items: proposed ? [PLANSPEC_INBOX_ITEM] : [],
      errors: [],
    }));
    render(
      <MemoryRouter>
        <WartetPanel />
        <JarvisChat turnPollIntervalMs={25} />
      </MemoryRouter>,
    );

    await submitPlan("/plan mache einen Health-Report");
    fireEvent.click(await screen.findByRole("button", { name: "Als Approval einreichen" }));

    expect(proposePlanspecMock).toHaveBeenCalledWith("draft_0123456789abcdef01234567");
    // Erfolgs-Hinweis auf der Card (Brief-Form „Zur Bestätigung in der Inbox (#id)").
    expect(
      await screen.findByText("Eingereicht — zur Bestätigung in der Inbox (#123)"),
    ).toBeTruthy();
    // Der Button ist durch den Hinweis ersetzt (idempotent, kein Doppel-POST).
    expect(screen.queryByRole("button", { name: "Als Approval einreichen" })).toBeNull();
    // Inbox-Refresh: über den initialen Poll hinaus erneut geladen …
    await vi.waitFor(() => {
      expect(getPaInboxMock.mock.calls.length).toBeGreaterThanOrEqual(2);
    });
    // … und die PLANSPEC-Approval-Card steht jetzt in der Inbox (Wartet-Panel).
    expect(
      await screen.findByText(/PlanSpec als gehaltene Kette ingesten\?/),
    ).toBeTruthy();
  });

  it("422 (Engine ohne YAML-Frontmatter): detail.error lesbar an der Card, nie still", async () => {
    draftPlanspecMock.mockRejectedValue(
      new Error(
        '422: {"detail":{"error":"Engine-Ausgabe enthält keine PlanSpec-YAML-Frontmatter","engine_output":"Hier ist dein Plan: …"}}',
      ),
    );
    renderChat();

    await submitPlan("/plan kaputte Engine");

    const alert = await screen.findByText(/PlanSpec-Entwurf fehlgeschlagen\./);
    expect(alert.closest(".jv-bubble-error")).toBeTruthy();
    expect(alert.textContent).toContain("Engine-Ausgabe enthält keine PlanSpec-YAML-Frontmatter");
    // Kein Rohtext-JSON und kein propose-Button an einer Fehler-Card.
    expect(alert.textContent).not.toContain("engine_output");
    expect(screen.queryByRole("button", { name: "Als Approval einreichen" })).toBeNull();
  });

  it("400 beim Propose (BLOCK/stale): Fehlerzeile an der Card, Retry bleibt möglich", async () => {
    proposePlanspecMock.mockRejectedValue(
      new Error('400: {"detail":"BLOCK: Validator blockiert den Draft"}'),
    );
    renderChat();

    await submitPlan("/plan mache einen Health-Report");
    fireEvent.click(await screen.findByRole("button", { name: "Als Approval einreichen" }));

    const alert = await screen.findByRole("alert");
    expect(alert.textContent).toContain("Einreichung fehlgeschlagen.");
    expect(alert.textContent).toContain("BLOCK: Validator blockiert den Draft");
    // Kein stiller Fehler, kein falscher Erfolg — der Button bleibt (Retry).
    expect(screen.queryByText(/Eingereicht — zur Bestätigung/)).toBeNull();
    const button = screen.getByRole("button", { name: "Als Approval einreichen" });
    expect((button as HTMLButtonElement).disabled).toBe(false);
  });

  it("„/plan“ ohne Idee → Usage-Hinweis (role=alert), kein API-Call", async () => {
    renderChat();

    await submitPlan("/plan");

    const alert = await screen.findByRole("alert");
    expect(alert.textContent).toContain("braucht eine Idee");
    expect(draftPlanspecMock).not.toHaveBeenCalled();
    expect(sendPaMessageMock).not.toHaveBeenCalled();
  });

  it("„/plane …“ ist KEIN Plan-Kommando → normaler Chat-Turn", async () => {
    renderChat();

    await submitPlan("/plane ticket nach nowhere");

    expect(draftPlanspecMock).not.toHaveBeenCalled();
    expect(sendPaMessageMock).toHaveBeenCalledWith("/plane ticket nach nowhere", undefined, undefined);
  });
});
