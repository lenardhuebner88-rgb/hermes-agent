import { describe, expect, it, vi } from "vitest";
import { renderToStaticMarkup } from "react-dom/server";
import type { ReactNode } from "react";
import { DraftEditDialog, FreigabenList, type FunnelDraft } from "./FunnelFreigaben";
import { funnelDraftEditRequest } from "./funnelDraftEditRequest";

vi.mock("./Overlay", () => ({
  Overlay: ({
    ariaLabel,
    children,
    closeDisabled,
    maxWidthClassName,
  }: {
    ariaLabel: string;
    children: ReactNode;
    closeDisabled?: boolean;
    maxWidthClassName?: string;
  }) => (
    <div data-overlay="true" data-aria-label={ariaLabel} data-close-disabled={String(Boolean(closeDisabled))} data-max-width={maxWidthClassName ?? ""}>
      {children}
    </div>
  ),
}));

const draft = (over: Partial<FunnelDraft> = {}): FunnelDraft => ({
  id: "t_aaa",
  title: "Essensplan-Rückblick",
  created_by: "family",
  assignee: "coder-claude",
  completed_at: 1_781_170_000,
  draft_excerpt: "# Draft\nInhalt des Drafts",
  draft_text: "# Draft\nInhalt des vollständigen Drafts",
  operator_edited: false,
  ...over,
});

const noop = vi.fn();

describe("FreigabenList (Funnel-Freigaben)", () => {
  it("zeigt Titel, Quelle und Freigeben-Knopf — Confirm erst nach Klick", () => {
    const html = renderToStaticMarkup(
      <FreigabenList
        drafts={[draft()]}
        pending={null}
        openId={null}
        busy={false}
        onAct={noop}
        onPending={noop}
        onToggleOpen={noop}
        onEdit={noop}
      />,
    );
    expect(html).toContain("Essensplan-Rückblick");
    expect(html).toContain("Familie");
    expect(html).toContain("Freigeben → bauen");
    expect(html).toContain("Verwerfen");
    expect(html).toContain("Bearbeiten / Feedback");
    expect(html).not.toContain("Bestätigen");
  });

  it("pending zeigt Confirm + Hint; offener Draft rendert den Markdown-Text", () => {
    const html = renderToStaticMarkup(
      <FreigabenList
        drafts={[draft({ created_by: "discord-idee" })]}
        pending={{ id: "t_aaa", kind: "approve" }}
        openId="t_aaa"
        busy={false}
        onAct={noop}
        onPending={noop}
        onToggleOpen={noop}
        onEdit={noop}
      />,
    );
    expect(html).toContain("Bestätigen");
    expect(html).toContain("Discord-Idee");
    expect(html).toContain("Inhalt des vollständigen Drafts");
  });

  it("zeigt Operator-Edit-Badge, wenn der Draft bearbeitet wurde", () => {
    const html = renderToStaticMarkup(
      <FreigabenList
        drafts={[draft({ operator_edited: true })]}
        pending={null}
        openId={null}
        busy={false}
        onAct={noop}
        onPending={noop}
        onToggleOpen={noop}
        onEdit={noop}
      />,
    );
    expect(html).toContain("Operator-Edit gespeichert");
  });
});

describe("DraftEditDialog", () => {
  it("rendert editierbare Plan-Spec plus Save/Revision/Build-Aktionen", () => {
    const html = renderToStaticMarkup(
      <DraftEditDialog
        draft={draft()}
        editText="# Draft\nInhalt des vollständigen Drafts"
        operatorNote="Bitte ACs ergänzen"
        busy={false}
        onEditTextChange={noop}
        onOperatorNoteChange={noop}
        onClose={noop}
        onSave={noop}
        onRevise={noop}
        onBuild={noop}
      />,
    );
    expect(html).toContain("Mein Input / Änderungswunsch");
    expect(html).toContain("Speichern");
    expect(html).toContain("Überarbeiten lassen");
    expect(html).toContain("Finale Version bauen");
    expect(html).toContain("Inhalt des vollständigen Drafts");
    expect(html).toContain("Bitte ACs ergänzen");
    expect(html).toContain('data-overlay="true"');
    expect(html).toContain('data-max-width="max-w-3xl"');
  });

  it("sperrt Overlay-Close während Save/Revision/Build busy ist", () => {
    const html = renderToStaticMarkup(
      <DraftEditDialog
        draft={draft()}
        editText="# Draft\nInhalt des vollständigen Drafts"
        operatorNote=""
        busy={true}
        onEditTextChange={noop}
        onOperatorNoteChange={noop}
        onClose={noop}
        onSave={noop}
        onRevise={noop}
        onBuild={noop}
      />,
    );

    expect(html).toContain('data-close-disabled="true"');
    expect(html).toContain("disabled");
  });

  it("rendert einen konkreten Fehler im Popup statt nur hinter dem Overlay", () => {
    const html = renderToStaticMarkup(
      <DraftEditDialog
        draft={draft()}
        editText="# Draft\nInhalt des vollständigen Drafts"
        operatorNote="Bitte ACs ergänzen"
        error="409: draft_text darf nicht leer sein"
        busy={false}
        onEditTextChange={noop}
        onOperatorNoteChange={noop}
        onClose={noop}
        onSave={noop}
        onRevise={noop}
        onBuild={noop}
      />,
    );
    expect(html).toContain("409: draft_text darf nicht leer sein");
  });
});

describe("funnelDraftEditRequest", () => {
  it("baut den PATCH-Request für Bearbeiten inklusive Operator-Kommentar", () => {
    const [url, init] = funnelDraftEditRequest("t_aaa", "# Plan\nUmsetzen", "Bitte ACs ergänzen");

    expect(url).toBe("/api/plugins/kanban/funnel/drafts/t_aaa");
    expect(init.method).toBe("PATCH");
    expect(JSON.parse(String(init.body))).toEqual({
      draft_text: "# Plan\nUmsetzen",
      operator_note: "Bitte ACs ergänzen",
    });
  });
});
