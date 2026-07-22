// @vitest-environment jsdom
//
// Verhaltenstests für BibliothekCorrectionEditor (P6b) gegen den realen
// Backend-Vertrag: PUT {item_id, fields, reason, confirm:true} →
// {correction, provenance} (ohne ok); POST revoke {item_id, reason,
// confirm:true} → {correction}; Correction-Record mit fields-Objekt,
// original-Record und history-Einträgen {at, action, fields, reason, actor}.
// fetchJSON per vi.hoisted gemockt wie in BibliothekView.render.test.tsx.
import { afterEach, describe, expect, it, vi } from "vitest";
import { cleanup, fireEvent, render, screen, waitFor, within } from "@testing-library/react";

const { fetchJSONMock } = vi.hoisted(() => ({ fetchJSONMock: vi.fn() }));

vi.mock("@/lib/api", async () => {
  const actual = await vi.importActual<typeof import("@/lib/api")>("@/lib/api");
  return { ...actual, fetchJSON: fetchJSONMock };
});

import {
  BibliothekCorrectionEditor,
  type CorrectionFields,
  type CorrectionOriginal,
  type LibraryCorrection,
} from "./BibliothekCorrectionEditor";
import type { LibraryProvenance } from "./BibliothekView.helpers";

afterEach(() => {
  cleanup();
  vi.clearAllMocks();
});

const ITEM_ID = "cron::main::5a2a54ac3dae::x.md";

/** Ursprüngliche (automatisch hergeleitete) Provenanz des Items. */
const ORIGINAL = {
  producer: "Hermes",
  path: "Cron",
  status: "partial",
  chain: {
    auftraggeber: "Cron-Job morning-digest",
    delegation: "Hermes",
    autor: "gpt-5.6-sol",
    review: "",
    ablage: "",
  },
};

/** Wirksame Provenanz OHNE Korrektur = exakt das Original (konsistente Fixture). */
const PRISTINE: LibraryProvenance = {
  producer: ORIGINAL.producer,
  path: ORIGINAL.path,
  status: ORIGINAL.status,
  chain: { ...ORIGINAL.chain },
  refs: [],
};

/** Aktuell wirksame Provenienz = Original + aktive Overrides. */
const EFFECTIVE: LibraryProvenance = {
  producer: "Hermes",
  path: "Task",
  status: "partial",
  chain: {
    auftraggeber: "Piet",
    delegation: "Hermes",
    autor: "gpt-5.6-sol",
    review: "Claude",
    ablage: "",
  },
  refs: ["cron:5a2a54ac3dae"],
};

/** Server-Record enthält NUR aktive Keys (partiell). */
const ACTIVE_FIELDS: CorrectionFields = {
  path: "Task",
  auftraggeber: "Piet",
  review: "Claude",
};

const CORRECTION: LibraryCorrection = {
  item_id: ITEM_ID,
  fields: ACTIVE_FIELDS,
  original: ORIGINAL,
  reason: "Task-Kette, nicht Cron",
  actor: "operator",
  created_at: "2026-07-20T09:00:00+02:00",
  updated_at: "2026-07-21T10:15:00+02:00",
  active: true,
  history: [
    { at: "2026-07-20T09:00:00+02:00", action: "set", fields: { path: "Task", auftraggeber: "Piet" }, reason: "Task-Kette, nicht Cron", actor: "operator" },
    { at: "2026-07-21T10:15:00+02:00", action: "set", fields: { review: "Claude" }, reason: "Review nachgetragen", actor: "operator" },
  ],
};

function renderEditor(over: {
  provenance?: LibraryProvenance;
  correction?: LibraryCorrection | null;
  onChanged?: (c: LibraryCorrection | null) => void | Promise<void>;
} = {}) {
  render(
    <BibliothekCorrectionEditor
      itemId={ITEM_ID}
      provenance={over.provenance ?? EFFECTIVE}
      correction={over.correction ?? null}
      onChanged={over.onChanged}
    />,
  );
}

function openEditor(over: Parameters<typeof renderEditor>[0] = {}) {
  renderEditor(over);
  fireEvent.click(screen.getByRole("button", { name: /korrigieren|korrektur aktiv/i }));
}

describe("BibliothekCorrectionEditor", () => {
  it("rendert Ursprünglich (aus correction.original), wirksame Overrides und append-only-Historie", () => {
    openEditor({ correction: CORRECTION });

    // Ursprünglich kommt aus correction.original — NICHT aus der wirksamen Provenanz
    const originalCol = screen.getByText("Ursprünglich (automatisch hergeleitet)").closest("div")!;
    expect(within(originalCol as HTMLElement).getByText("Cron")).toBeTruthy();
    expect(within(originalCol as HTMLElement).getByText("Cron-Job morning-digest")).toBeTruthy();
    expect(within(originalCol as HTMLElement).queryByText("Piet")).toBeNull();

    // Formular editiert nur Overrides: aktive Overrides sind Werte, entfernte (null) sind leer
    expect((screen.getByLabelText(/^Weg/) as HTMLSelectElement).value).toBe("Task");
    expect((screen.getByLabelText(/^Auftraggeber/) as HTMLInputElement).value).toBe("Piet");
    expect((screen.getByLabelText(/^Delegation/) as HTMLInputElement).value).toBe("");
    expect((screen.getByLabelText(/^Review/) as HTMLInputElement).value).toBe("Claude");
    expect((screen.getByLabelText(/^Ablage/) as HTMLInputElement).value).toBe("");

    // Leeres Override-Feld zeigt das Original als Placeholder
    expect((screen.getByLabelText(/^Delegation/) as HTMLInputElement).placeholder).toBe("Automatisch aktuell: Hermes");
    expect((screen.getByLabelText(/^Ablage/) as HTMLInputElement).placeholder).toBe("Automatisch aktuell: unbekannt");

    // Aktive Korrektur: Akteur + Zeitstempel + Grund (operator steht in Panel UND Historie)
    expect(screen.getByText("Aktive Korrektur")).toBeTruthy();
    expect(screen.getAllByText(/operator/).length).toBeGreaterThan(0);
    expect(screen.getAllByText(/Task-Kette, nicht Cron/).length).toBeGreaterThan(0);

    // Historie vollständig sichtbar — beide Gründe und die geänderten Felder
    const historyCol = screen.getByText("Historie (append-only)").closest("div")!;
    expect(within(historyCol as HTMLElement).getByText("Task-Kette, nicht Cron")).toBeTruthy();
    expect(within(historyCol as HTMLElement).getByText("Review nachgetragen")).toBeTruthy();
    expect(within(historyCol as HTMLElement).getByText(/path: Task/)).toBeTruthy();
    expect(within(historyCol as HTMLElement).getByText(/review: Claude/)).toBeTruthy();
  });

  it("Öffnen mit aktivem Vollrecord löst KEINEN zusätzlichen Request aus", () => {
    openEditor({ correction: CORRECTION });
    expect(screen.getByText("Ursprünglich (automatisch hergeleitet)")).toBeTruthy();
    expect(fetchJSONMock).not.toHaveBeenCalled();
  });

  it("baut die Vorschau auf der heutigen Ableitung, nicht dem alten Erstsnapshot", () => {
    const changedDerived: CorrectionOriginal = {
      ...ORIGINAL,
      chain: { ...ORIGINAL.chain, delegation: "Qwen" },
    };
    openEditor({
      correction: {
        ...CORRECTION,
        fields: { path: "Task" },
        derived: changedDerived,
      },
    });

    expect((screen.getByLabelText(/^Delegation/) as HTMLInputElement).placeholder)
      .toBe("Automatisch aktuell: Qwen");
    const preview = screen.getByText("Entwurf (Server prüft vor Speichern)").closest("div")!;
    expect(within(preview as HTMLElement).getByText("Qwen")).toBeTruthy();
    // Historischer Erstsnapshot bleibt daneben unverändert nachvollziehbar.
    const originalCol = screen.getByText("Ursprünglich (automatisch hergeleitet)").closest("div")!;
    expect(within(originalCol as HTMLElement).getByText("Hermes")).toBeTruthy();
  });

  it("lädt einen vollständig zurückgenommenen Audit-Record beim Öffnen sichtbar nach", async () => {
    const reverted: LibraryCorrection = {
      ...CORRECTION,
      fields: {},
      reason: "Automatische Herleitung bestätigt",
      history: [
        ...CORRECTION.history,
        { at: "2026-07-22T15:00:00+02:00", action: "revert", fields: {}, reason: "Automatische Herleitung bestätigt", actor: "operator" },
      ],
    };
    fetchJSONMock.mockResolvedValueOnce({ correction: reverted });

    openEditor({ provenance: PRISTINE, correction: null });
    expect(await screen.findByText("Automatische Herleitung bestätigt")).toBeTruthy();
    expect(fetchJSONMock).toHaveBeenCalledWith(
      `/api/library/correction?id=${encodeURIComponent(ITEM_ID)}`,
    );
    expect(screen.queryByText("Aktive Korrektur")).toBeNull();
    expect(screen.getByText("Historie (append-only)")).toBeTruthy();
  });

  it("öffnet erst nach echter Änderung und Grund einen fokussierten Bestätigungsdialog", async () => {
    let resolvePreview!: (value: unknown) => void;
    const delayedPreview = new Promise((resolve) => { resolvePreview = resolve; });
    fetchJSONMock
      .mockResolvedValueOnce({ correction: null })
      .mockImplementationOnce(() => delayedPreview);
    const previewResponse = {
        provenance: {
          ...PRISTINE,
          chain: { ...PRISTINE.chain, auftraggeber: "Manuell: Piet" },
        },
        fields: { auftraggeber: "Manuell: Piet" },
      };
    openEditor({ provenance: PRISTINE, correction: null });
    const review = () => screen.getByRole("button", { name: "Korrektur prüfen" }) as HTMLButtonElement;
    await waitFor(() => expect((screen.getByLabelText(/^Auftraggeber/) as HTMLInputElement).disabled).toBe(false));

    // Keine Änderung am Formular → trotz Grund gesperrt.
    fireEvent.change(screen.getByLabelText("Begründung (Pflicht)"), { target: { value: "Weil es so war" } });
    expect(review().disabled).toBe(true);

    // Echte Änderung, aber Grund wieder raus (Whitespace zählt nicht) → gesperrt.
    fireEvent.change(screen.getByLabelText(/^Auftraggeber/), { target: { value: "Manuell: Piet" } });
    fireEvent.change(screen.getByLabelText("Begründung (Pflicht)"), { target: { value: "   " } });
    expect(review().disabled).toBe(true);

    // Änderung + Grund öffnet nur die zweite Stufe; noch keine Mutation.
    fireEvent.change(screen.getByLabelText("Begründung (Pflicht)"), { target: { value: "Weil es so war" } });
    expect(review().disabled).toBe(false);
    const reviewButton = review();
    reviewButton.focus();
    fireEvent.click(reviewButton);
    // Während der Server exakt dieses Payload prüft, kann der Entwurf nicht
    // mehr verändert und damit von der späteren Bestätigung entkoppelt werden.
    expect((screen.getByLabelText(/^Auftraggeber/) as HTMLInputElement).disabled).toBe(true);
    expect((screen.getByLabelText("Begründung (Pflicht)") as HTMLTextAreaElement).disabled).toBe(true);
    // Chromium verschiebt den Fokus auf BODY, sobald der während der asynchronen
    // Preview fokussierte Trigger disabled wird. Der explizite Return-Ref muss
    // ihn nach Escape trotzdem wiederherstellen.
    reviewButton.blur();
    resolvePreview(previewResponse);
    expect(await screen.findByRole("dialog", { name: "Korrektur verbindlich speichern" })).toBeTruthy();
    expect(fetchJSONMock).toHaveBeenCalledTimes(2); // Audit-GET + mutationsfreie Preview
    expect(document.activeElement).toBe(screen.getByRole("button", { name: "Abbrechen" }));
    fireEvent.keyDown(window, { key: "Escape" });
    expect(screen.queryByRole("dialog")).toBeNull();
    expect(document.activeElement).toBe(reviewButton);
  });

  it("zeigt im Dialog die servernormalisierte Alias-Vorschau", async () => {
    const normalized: LibraryProvenance = {
      ...PRISTINE,
      producer: "Codex",
      chain: { ...PRISTINE.chain, autor: "Codex" },
    };
    fetchJSONMock
      .mockResolvedValueOnce({ correction: null })
      .mockResolvedValueOnce({ provenance: normalized, fields: { autor: "Codex" } })
      .mockResolvedValueOnce({
        correction: { ...CORRECTION, fields: { autor: "Codex" }, derived: ORIGINAL },
        provenance: normalized,
      });

    openEditor({ provenance: PRISTINE, correction: null });
    await waitFor(() => expect((screen.getByLabelText(/^Autor/) as HTMLInputElement).disabled).toBe(false));
    fireEvent.change(screen.getByLabelText(/^Autor/), { target: { value: "codex" } });
    fireEvent.change(screen.getByLabelText("Begründung (Pflicht)"), { target: { value: "Alias belegt" } });
    fireEvent.click(screen.getByRole("button", { name: "Korrektur prüfen" }));

    const dialog = await screen.findByRole("dialog", { name: "Korrektur verbindlich speichern" });
    expect(within(dialog).getByText("Codex")).toBeTruthy();
    expect(fetchJSONMock).toHaveBeenNthCalledWith(2, "/api/library/correction/preview", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ item_id: ITEM_ID, fields: { autor: "codex" } }),
    });
    const confirmSave = screen.getByRole("button", { name: "Jetzt verbindlich speichern" }) as HTMLButtonElement;
    confirmSave.focus();
    fireEvent.click(confirmSave);
    await waitFor(() => expect(fetchJSONMock).toHaveBeenCalledTimes(3));
    // PUT verwendet exakt das vom Server geprüfte/kanonisierte Payload, nicht
    // erneut den Live-Draft (`codex`).
    expect(fetchJSONMock).toHaveBeenNthCalledWith(3, "/api/library/correction", {
      method: "PUT",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({
        item_id: ITEM_ID,
        fields: { autor: "Codex" },
        reason: "Alias belegt",
        confirm: true,
      }),
    });
  });

  it("Speichern sendet exakt {item_id, nur geänderte fields, reason, confirm:true} per PUT", async () => {
    const onChanged = vi.fn().mockResolvedValue(undefined);
    // Wirksam vor dem Speichern: alles original (keine aktive Korrektur)
    // Server-Record: nur die aktiven Keys
    const savedFields: CorrectionFields = {
      path: "Receipt",
      auftraggeber: "Receipt-Quelle",
    };
    const savedCorrection: LibraryCorrection = {
      item_id: ITEM_ID,
      fields: savedFields,
      original: ORIGINAL,
      reason: "Receipt laut Frontmatter",
      actor: "operator",
      created_at: "2026-07-22T12:00:00+02:00",
      updated_at: null,
      history: [
        { at: "2026-07-22T12:00:00+02:00", action: "set", fields: { path: "Receipt", auftraggeber: "Receipt-Quelle" }, reason: "Receipt laut Frontmatter", actor: "operator" },
      ],
    };
    const savedProvenance: LibraryProvenance = {
      producer: ORIGINAL.producer,
      path: "Receipt",
      status: ORIGINAL.status,
      chain: { ...ORIGINAL.chain, auftraggeber: "Receipt-Quelle" },
      refs: [],
    };
    let resolveSave!: (value: unknown) => void;
    const delayedSave = new Promise((resolve) => { resolveSave = resolve; });
    // Erst mutationsfreier Audit-GET, dann Antwort OHNE ok — exakt
    // {correction, provenance}.
    fetchJSONMock
      .mockResolvedValueOnce({ correction: null })
      .mockResolvedValueOnce({ provenance: savedProvenance, fields: savedFields })
      .mockImplementationOnce(() => delayedSave);

    openEditor({ provenance: PRISTINE, correction: null, onChanged });
    const editorClose = screen.getByRole("button", { name: "Schließen" }) as HTMLButtonElement;
    await waitFor(() => expect((screen.getByLabelText(/^Weg/) as HTMLSelectElement).disabled).toBe(false));
    fireEvent.change(screen.getByLabelText(/^Weg/), { target: { value: "Receipt" } });
    fireEvent.change(screen.getByLabelText(/^Auftraggeber/), { target: { value: "Receipt-Quelle" } });
    fireEvent.change(screen.getByLabelText(/^Delegation/), { target: { value: "  " } }); // Whitespace → null
    fireEvent.change(screen.getByLabelText("Begründung (Pflicht)"), { target: { value: "Receipt laut Frontmatter" } });
    fireEvent.click(screen.getByRole("button", { name: "Korrektur prüfen" }));
    // Der Dialog zeigt die wirksame Draft-Vorschau, schreibt aber noch nichts.
    expect(await screen.findByRole("dialog", { name: "Korrektur verbindlich speichern" })).toBeTruthy();
    expect(fetchJSONMock).toHaveBeenCalledTimes(2);
    expect(fetchJSONMock).toHaveBeenNthCalledWith(2, "/api/library/correction/preview", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({
        item_id: ITEM_ID,
        fields: { path: "Receipt", auftraggeber: "Receipt-Quelle" },
      }),
    });
    const confirmSaveWithDelay = screen.getByRole("button", { name: "Jetzt verbindlich speichern" }) as HTMLButtonElement;
    confirmSaveWithDelay.focus();
    fireEvent.click(confirmSaveWithDelay);

    // Während der Mutation bleibt der Fokus im gesperrten Dialog; er darf
    // nicht früh auf einen gleichzeitig deaktivierten Trigger zurückfallen.
    expect(screen.getByRole("dialog", { name: "Korrektur verbindlich speichern" })).toBeTruthy();
    const busySave = screen.getByRole("button", { name: "Wird gespeichert…" }) as HTMLButtonElement;
    expect(busySave.getAttribute("aria-disabled")).toBe("true");
    expect(screen.getByRole("dialog", { name: "Korrektur verbindlich speichern" }).contains(document.activeElement)).toBe(true);
    expect(document.activeElement).toBe(busySave);
    expect(editorClose.disabled).toBe(true);
    resolveSave({ correction: savedCorrection, provenance: savedProvenance });

    await waitFor(() => {
      expect(fetchJSONMock).toHaveBeenCalledTimes(3);
    });
    expect(fetchJSONMock).toHaveBeenNthCalledWith(3, "/api/library/correction", {
      method: "PUT",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({
        item_id: ITEM_ID,
        fields: {
          path: "Receipt",
          auftraggeber: "Receipt-Quelle",
        },
        reason: "Receipt laut Frontmatter",
        confirm: true,
      }),
    });

    await waitFor(() => {
      expect(screen.getByRole("status")).toBeTruthy();
    });
    expect(screen.getByRole("status").textContent).toContain("Korrektur gespeichert");

    // Editorzustand sauber nachgeführt: Formular spiegelt den Server-Record
    expect((screen.getByLabelText(/^Weg/) as HTMLSelectElement).value).toBe("Receipt");
    expect((screen.getByLabelText(/^Auftraggeber/) as HTMLInputElement).value).toBe("Receipt-Quelle");
    expect((screen.getByLabelText("Begründung (Pflicht)") as HTMLTextAreaElement).value).toBe("");
    expect(onChanged).toHaveBeenCalledWith(savedCorrection);
    expect(screen.getByText("Aktive Korrektur")).toBeTruthy();
    await waitFor(() => expect(document.activeElement).toBe(editorClose));
  });

  it("Revoke sendet exakt {item_id, reason, confirm:true} per POST und nur bei aktiven Overrides", async () => {
    const onChanged = vi.fn().mockResolvedValue(undefined);
    const reverted: LibraryCorrection = {
      ...CORRECTION,
      fields: {},
      reason: "Doch automatisch korrekt",
      history: [
        ...CORRECTION.history,
        { at: "2026-07-22T14:00:00+02:00", action: "revert", fields: {}, reason: "Doch automatisch korrekt", actor: "operator" },
      ],
    };
    let resolveRevoke!: (value: unknown) => void;
    const delayedRevoke = new Promise((resolve) => { resolveRevoke = resolve; });
    // Antwort OHNE ok — exakt {correction}; Record bleibt fürs Audit erhalten.
    fetchJSONMock.mockImplementation(() => delayedRevoke);

    openEditor({ correction: CORRECTION, onChanged });
    const editorClose = screen.getByRole("button", { name: "Schließen" }) as HTMLButtonElement;
    const revokeBtn = () => screen.getByRole("button", { name: "Rücknahme prüfen" }) as HTMLButtonElement;

    // Ohne Grund gesperrt; Prüfen öffnet erst die zweite Stufe.
    expect(revokeBtn().disabled).toBe(true);
    fireEvent.change(screen.getByLabelText("Rücknahme — Begründung (Pflicht)"), { target: { value: "Doch automatisch korrekt" } });
    expect(revokeBtn().disabled).toBe(false);

    fireEvent.click(revokeBtn());
    expect(fetchJSONMock).not.toHaveBeenCalled();
    expect(screen.getByRole("dialog", { name: "Korrektur vollständig zurücknehmen" })).toBeTruthy();
    const confirmRevoke = screen.getByRole("button", { name: "Jetzt vollständig zurücknehmen" }) as HTMLButtonElement;
    confirmRevoke.focus();
    fireEvent.click(confirmRevoke);
    expect(screen.getByRole("dialog", { name: "Korrektur vollständig zurücknehmen" })).toBeTruthy();
    const busyRevoke = screen.getByRole("button", { name: "Wird zurückgenommen…" }) as HTMLButtonElement;
    expect(busyRevoke.getAttribute("aria-disabled")).toBe("true");
    expect(screen.getByRole("dialog", { name: "Korrektur vollständig zurücknehmen" }).contains(document.activeElement)).toBe(true);
    expect(document.activeElement).toBe(busyRevoke);
    expect(editorClose.disabled).toBe(true);
    resolveRevoke({ correction: reverted });
    await waitFor(() => {
      expect(fetchJSONMock).toHaveBeenCalledTimes(1);
    });
    expect(fetchJSONMock).toHaveBeenCalledWith("/api/library/correction/revoke", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ item_id: ITEM_ID, reason: "Doch automatisch korrekt", confirm: true }),
    });

    await waitFor(() => {
      expect(screen.getByRole("status")).toBeTruthy();
    });
    expect(screen.getByRole("status").textContent).toContain("zurückgenommen");
    expect(onChanged).toHaveBeenCalledWith(null);
    // Aktive Korrektur ist nach der Rücknahme weg, Overrides sind geleert
    expect(screen.queryByText("Aktive Korrektur")).toBeNull();
    expect((screen.getByLabelText(/^Auftraggeber/) as HTMLInputElement).value).toBe("");
    expect(screen.getAllByText("Doch automatisch korrekt").length).toBeGreaterThan(0);
    await waitFor(() => expect(document.activeElement).toBe(editorClose));
  });

  it("Revoke bleibt gesperrt, wenn der Record keine aktiven Overrides mehr enthält", () => {
    openEditor({ correction: { ...CORRECTION, fields: {} } });
    expect(screen.queryByLabelText("Rücknahme — Begründung (Pflicht)")).toBeNull();
    expect(screen.queryByRole("button", { name: "Rücknahme prüfen" })).toBeNull();
    expect(fetchJSONMock).not.toHaveBeenCalled();
  });

  it("Weg-Override lässt sich über die Original-Option entfernen und wird als path:null gespeichert", async () => {
    const onChanged = vi.fn().mockResolvedValue(undefined);
    // Record nach dem Entfernen: path raus, die übrigen aktiven Keys bleiben
    const savedCorrection: LibraryCorrection = {
      ...CORRECTION,
      fields: { auftraggeber: "Piet", review: "Claude" },
      reason: "Weg zurück aufs Original",
      history: [
        ...CORRECTION.history,
        { at: "2026-07-22T13:00:00+02:00", action: "revert", fields: { path: null }, reason: "Weg zurück aufs Original", actor: "operator" },
      ],
    };
    fetchJSONMock.mockImplementation(async (url: string) => {
      if (url === "/api/library/correction/preview") {
        return { provenance: { ...EFFECTIVE, path: "Cron" }, fields: { path: null } };
      }
      if (url === "/api/library/correction") {
        return { correction: savedCorrection, provenance: EFFECTIVE };
      }
      throw new Error(`unerwarteter Aufruf: ${url}`);
    });

    openEditor({ correction: CORRECTION, onChanged });
    const weg = screen.getByLabelText(/^Weg/) as HTMLSelectElement;
    expect(weg.value).toBe("Task");
    // Erste Option entfernt den Override und zeigt das Original
    expect(weg.options[0].value).toBe("");
    expect(weg.options[0].textContent).toBe("Automatisch aktuell: Cron");

    fireEvent.change(weg, { target: { value: "" } });
    expect(weg.value).toBe("");

    fireEvent.change(screen.getByLabelText("Begründung (Pflicht)"), { target: { value: "Weg zurück aufs Original" } });
    fireEvent.click(screen.getByRole("button", { name: "Korrektur prüfen" }));
    fireEvent.click(await screen.findByRole("button", { name: "Jetzt verbindlich speichern" }));

    await waitFor(() => {
      expect(fetchJSONMock).toHaveBeenCalledTimes(2);
    });
    // PUT sendet nur den geänderten Key — entfernter Weg-Override als path:null;
    // die übrigen Overrides bleiben durch den Store-Merge erhalten.
    expect(fetchJSONMock).toHaveBeenNthCalledWith(2, "/api/library/correction", {
      method: "PUT",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({
        item_id: ITEM_ID,
        fields: {
          path: null,
        },
        reason: "Weg zurück aufs Original",
        confirm: true,
      }),
    });

    await waitFor(() => {
      expect(screen.getByRole("status")).toBeTruthy();
    });
    expect(onChanged).toHaveBeenCalledWith(savedCorrection);
    // Formular spiegelt den neuen Record: Weg-Override ist weg (leere Option)
    expect((screen.getByLabelText(/^Weg/) as HTMLSelectElement).value).toBe("");
  });

  it("zeigt Server-Fehler als Alert und lässt den Editor bedienbar", async () => {
    fetchJSONMock
      .mockResolvedValueOnce({ correction: null })
      .mockResolvedValueOnce({
        provenance: {
          ...PRISTINE,
          chain: { ...PRISTINE.chain, auftraggeber: "Manuell: Piet" },
        },
        fields: { auftraggeber: "Manuell: Piet" },
      })
      .mockRejectedValueOnce(new Error("422: reason zu kurz"));

    openEditor({ provenance: PRISTINE, correction: null });
    await waitFor(() => expect((screen.getByLabelText(/^Auftraggeber/) as HTMLInputElement).disabled).toBe(false));
    fireEvent.change(screen.getByLabelText(/^Auftraggeber/), { target: { value: "Manuell: Piet" } });
    fireEvent.change(screen.getByLabelText("Begründung (Pflicht)"), { target: { value: "x" } });
    fireEvent.click(screen.getByRole("button", { name: "Korrektur prüfen" }));
    fireEvent.click(await screen.findByRole("button", { name: "Jetzt verbindlich speichern" }));

    await waitFor(() => {
      expect(screen.getByRole("alert")).toBeTruthy();
    });
    expect(screen.getByRole("alert").textContent).toContain("422: reason zu kurz");
    // Nicht gespeichert, Formular bleibt gefüllt und erneut abschickbar
    expect((screen.getByLabelText("Begründung (Pflicht)") as HTMLTextAreaElement).value).toBe("x");
    expect((screen.getByRole("button", { name: "Korrektur prüfen" }) as HTMLButtonElement).disabled).toBe(false);
  });
});
