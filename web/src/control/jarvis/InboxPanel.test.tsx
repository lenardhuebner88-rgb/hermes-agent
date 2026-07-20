// @vitest-environment jsdom
/**
 * InboxPanel — volle Inbox-Ansicht (S2.4) mit den Approval-Cards für
 * pa_action:
 *  1. Approval-Card zeigt Kategorie, lesbares Ziel aus action_payload
 *     (tmux.send_keys → session:window), Keys-Vorschau und reason;
 *     Buttons aus den Wire-Options (Ausführen/Ablehnen).
 *  2. Ausführen → POST answer "1" über den gefakten answer-Endpoint →
 *     Hinweis „Evidenz im Chat" + Inbox-Refresh (Karte verschwindet über die
 *     Server-Wahrheit). Ablehnen → "2".
 *  3. 409 (stale/double-tap) → Refresh + Stale-Hinweis statt Fehlerzeile.
 *  4. Sonstige Fehler → inline Fehlerzeile an der Karte, nie still.
 *  5. question → Optionen + Klassik-Link; held/freigabe → Board-Link.
 * Item-Shapes: exakt der /api/pa/inbox-Wire (build_inbox in pa_chat.py).
 */
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { cleanup, configure, fireEvent, render, screen } from "@testing-library/react";
import { MemoryRouter } from "react-router-dom";

import type { PaInboxItem } from "@/lib/api";

configure({ asyncUtilTimeout: 5000 });

const answerAgentQuestionMock = vi.hoisted(() => vi.fn());

vi.mock("@/lib/api", async () => {
  const actual = await vi.importActual<typeof import("@/lib/api")>("@/lib/api");
  return {
    ...actual,
    api: {
      ...actual.api,
      answerAgentQuestion: answerAgentQuestionMock,
    },
  };
});

import { InboxPanel } from "./InboxPanel";

const PA_ACTION_ITEM: PaInboxItem = {
  type: "pa_action",
  id: "q77",
  question_id: 77,
  title: "PA-Aktion ausführen: tmux.send_keys? — Kimi-Window wartet seit 20 min",
  kind: "pa_action",
  category: "tmux.send_keys",
  action_payload: {
    version: 1,
    category: "tmux.send_keys",
    payload: { session: "work", window: "kimi", keys: "mach weiter mit dem Gate" },
    reason: "Kimi-Window wartet seit 20 min",
  },
  options: [
    { nr: 1, label: "Ausführen", recommended: false },
    { nr: 2, label: "Ablehnen", recommended: false },
  ],
  block_radius: 1,
  ts: 1753000000,
};

/** S3.3-FE: planspec.ingest-Card — Titel = build_ingest_question-Text (S3.3),
 *  Payload trägt die draft_id statt eines tmux-Ziels. */
const PLANSPEC_ACTION_ITEM: PaInboxItem = {
  type: "pa_action",
  id: "q88",
  question_id: 88,
  title:
    "PlanSpec als gehaltene Kette ingesten?\nDraft: `draft_0123456789abcdef01234567`\n" +
    "Validate: WARN (1 Findings)\nGates: freigabe=operator · live_test_depth=contract\n" +
    "Slices (2):\n- `S1` [coder] Endpoint und Tests implementieren · deps: —\n" +
    "- `S2` [verifier] Verhalten unabhängig verifizieren · deps: S1\n" +
    "- Validate-Finding: operator-visible warning\n" +
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
  ts: 1753001000,
};

const QUESTION_ITEM: PaInboxItem = {
  type: "question",
  id: "q101",
  question_id: 101,
  title: "Soll ich den Branch mergen?",
  kind: "claude",
  options: [
    { nr: 1, label: "Ja, mergen", recommended: true },
    { nr: 2, label: "Nein, warten", recommended: false },
  ],
  block_radius: 1,
  ts: 1752990000,
};

const HELD_ITEM: PaInboxItem = {
  type: "held_task",
  id: "t_abc123",
  card_id: "t_abc123",
  title: "Release-Kette jarvis — hält auf Operator",
  status: "blocked",
  freigabe: null,
  block_radius: 4,
  ts: 1752980000,
};

const GATE_ITEM: PaInboxItem = {
  type: "freigabe_gate",
  id: "t_def456",
  card_id: "t_def456",
  title: "Sprint-3 Landung — freigabe: operator",
  status: "scheduled",
  freigabe: "operator",
  block_radius: 2,
  ts: 1752970000,
};

function renderPanel(items: PaInboxItem[] = [PA_ACTION_ITEM]) {
  const onClose = vi.fn();
  const onRefresh = vi.fn().mockResolvedValue(undefined);
  const onHint = vi.fn();
  const utils = render(
    <MemoryRouter>
      <InboxPanel items={items} onClose={onClose} onRefresh={onRefresh} onHint={onHint} />
    </MemoryRouter>,
  );
  return { onClose, onRefresh, onHint, ...utils };
}

beforeEach(() => {
  answerAgentQuestionMock.mockResolvedValue({ ok: true, verified: true, executed: true });
});

afterEach(() => {
  cleanup();
  vi.clearAllMocks();
});

describe("InboxPanel (/api/pa/inbox-Items)", () => {
  it("Approval-Card: Ziel sichtbar, Payload und reason erst hinter Details", async () => {
    renderPanel();

    const card = await screen.findByTestId("jv-appr-q77");
    expect(card.textContent).toContain("PA-AKTION");
    expect(card.textContent).toContain("tmux.send_keys");
    // S6: Ziel bleibt für die schnelle Daumen-Entscheidung direkt sichtbar.
    expect(screen.getByTestId("jv-appr-target-q77").textContent).toBe(
      "tmux.send_keys → work:kimi",
    );
    const details = screen.getByText("Grund & Payload").closest("details");
    expect(details?.open).toBe(false);

    fireEvent.click(screen.getByText("Grund & Payload"));
    expect(details?.open).toBe(true);
    expect(screen.getByText("Tasten: mach weiter mit dem Gate")).toBeTruthy();
    expect(screen.getByText("Kimi-Window wartet seit 20 min")).toBeTruthy();
    expect(screen.getByRole("button", { name: "Ausführen" })).toBeTruthy();
    expect(screen.getByRole("button", { name: "Ablehnen" })).toBeTruthy();
  });

  it("fokussiert den Schließen-Button und hält Tab-Fokus im Drawer", async () => {
    renderPanel();

    const close = await screen.findByRole("button", { name: "Inbox-Ansicht schließen" });
    expect(document.activeElement).toBe(close);

    fireEvent.keyDown(close, { key: "Tab", shiftKey: true });
    expect(document.activeElement).toBe(screen.getByRole("button", { name: "Ablehnen" }));
  });

  it("S3.3-FE: planspec.ingest-Card — PLANSPEC-Chip, Zielzeile = draft_id, gleicher Flow", async () => {
    const { onRefresh, onHint } = renderPanel([PLANSPEC_ACTION_ITEM]);

    const card = await screen.findByTestId("jv-appr-q88");
    // PLANSPEC-Chip statt des generischen PA-AKTION-Chips.
    expect(card.textContent).toContain("PLANSPEC");
    expect(card.textContent).not.toContain("PA-AKTION");
    expect(card.textContent).toContain("planspec.ingest");
    // Zielzeile = draft_id (kein tmux-Target, keine Keys-Zeile).
    expect(screen.getByTestId("jv-appr-target-q88").textContent).toBe(
      "planspec.ingest → draft_0123456789abcdef01234567",
    );
    expect(card.querySelectorAll(".jv-appr-keys")).toHaveLength(0);
    // Der kategoriespezifische Card-Text (Slices/Validate) steht im Titel.
    expect(card.textContent).toContain("Validate: WARN (1 Findings)");
    expect(card.textContent).toContain("`S2` [verifier] Verhalten unabhängig verifizieren");

    // Gleicher Ausführen-Flow über den bestehenden answer-Endpoint.
    fireEvent.click(screen.getByRole("button", { name: "Ausführen" }));
    await vi.waitFor(() => {
      expect(answerAgentQuestionMock).toHaveBeenCalledWith(88, "1");
    });
    await vi.waitFor(() => {
      expect(onHint).toHaveBeenCalledWith("✓ Ausgeführt — Evidenz im Chat");
    });
    expect(onRefresh).toHaveBeenCalled();
  });

  it("Ausführen → answer '1' mit answered_by operator → Evidenz-Hinweis + Refresh", async () => {
    const { onRefresh, onHint } = renderPanel();

    fireEvent.click(await screen.findByRole("button", { name: "Ausführen" }));

    await vi.waitFor(() => {
      expect(answerAgentQuestionMock).toHaveBeenCalledWith(77, "1");
    });
    await vi.waitFor(() => {
      expect(onHint).toHaveBeenCalledWith("✓ Ausgeführt — Evidenz im Chat");
    });
    expect(onRefresh).toHaveBeenCalled();
  });

  it("Ablehnen → answer '2' → Ablehn-Hinweis + Refresh", async () => {
    answerAgentQuestionMock.mockResolvedValue({ ok: true, verified: true, executed: false });
    const { onRefresh, onHint } = renderPanel();

    fireEvent.click(await screen.findByRole("button", { name: "Ablehnen" }));

    await vi.waitFor(() => {
      expect(answerAgentQuestionMock).toHaveBeenCalledWith(77, "2");
    });
    await vi.waitFor(() => {
      expect(onHint).toHaveBeenCalledWith("Abgelehnt — Evidenz im Chat");
    });
    expect(onRefresh).toHaveBeenCalled();
  });

  it("fehlgeschlagene Ausführung (verified=false) → eigener Hinweis", async () => {
    answerAgentQuestionMock.mockResolvedValue({ ok: true, verified: false, executed: true });
    const { onHint } = renderPanel();

    fireEvent.click(await screen.findByRole("button", { name: "Ausführen" }));

    await vi.waitFor(() => {
      expect(onHint).toHaveBeenCalledWith("Ausführung fehlgeschlagen — Evidenz im Chat");
    });
  });

  it("409 (stale/double-tap) → Inbox-Refresh + Stale-Hinweis, keine Fehlerzeile", async () => {
    answerAgentQuestionMock.mockRejectedValue(
      new Error('409: {"detail":{"ok":false,"reason":"not-open"}}'),
    );
    const { onRefresh, onHint } = renderPanel();

    fireEvent.click(await screen.findByRole("button", { name: "Ausführen" }));

    await vi.waitFor(() => {
      expect(onHint).toHaveBeenCalledWith("Bereits erledigt — Liste aktualisiert");
    });
    expect(onRefresh).toHaveBeenCalled();
    expect(screen.queryByRole("alert")).toBeNull();
  });

  it("sonstiger Antwort-Fehler → inline Fehlerzeile an der Karte, kein Refresh", async () => {
    answerAgentQuestionMock.mockRejectedValue(new Error('503: {"detail":"store locked"}'));
    const { onRefresh, onHint } = renderPanel();

    fireEvent.click(await screen.findByRole("button", { name: "Ausführen" }));

    const alert = await screen.findByRole("alert");
    expect(alert.textContent).toContain("Antwort fehlgeschlagen.");
    expect(alert.textContent).toContain("store locked");
    expect(onRefresh).not.toHaveBeenCalled();
    expect(onHint).not.toHaveBeenCalled();
  });

  it("question-Karte: Optionen read-only + Antwort-Link in die Klassik", async () => {
    renderPanel([QUESTION_ITEM]);

    const card = await screen.findByTestId("jv-inbox-q-q101");
    expect(card.textContent).toContain("Soll ich den Branch mergen?");
    expect(card.textContent).toContain("Ja, mergen");
    expect(card.textContent).toContain("EMPFOHLEN");
    const link = screen.getByRole("link", { name: /Frage beantworten: Soll ich den Branch/ });
    expect(link.getAttribute("href")).toBe("/control/projekte-klassisch");
  });

  it("held/freigabe-Karten: Status/Freigabe + Board-Link mit card_id", async () => {
    renderPanel([HELD_ITEM, GATE_ITEM]);

    const held = await screen.findByTestId("jv-inbox-t-t_abc123");
    expect(held.textContent).toContain("HELD");
    expect(held.textContent).toContain("blocked");
    const gate = await screen.findByTestId("jv-inbox-t-t_def456");
    expect(gate.textContent).toContain("FREIGABE");
    expect(gate.textContent).toContain("freigabe: operator");

    const links = await screen.findAllByRole("link", { name: /Zum Board:/ });
    expect(links.map((link) => link.getAttribute("href"))).toEqual([
      "/control/fleet?task=t_abc123",
      "/control/fleet?task=t_def456",
    ]);
  });

  it("ESC und × schließen die Ansicht", async () => {
    const { onClose } = renderPanel();

    await screen.findByTestId("jv-appr-q77");
    fireEvent.keyDown(window, { key: "Escape" });
    expect(onClose).toHaveBeenCalledTimes(1);

    fireEvent.click(screen.getByRole("button", { name: "Inbox-Ansicht schließen" }));
    expect(onClose).toHaveBeenCalledTimes(2);
  });
});
