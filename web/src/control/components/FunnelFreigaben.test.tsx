import { describe, expect, it, vi } from "vitest";
import { renderToStaticMarkup } from "react-dom/server";
import { FreigabenList, type FunnelDraft } from "./FunnelFreigaben";

const draft = (over: Partial<FunnelDraft> = {}): FunnelDraft => ({
  id: "t_aaa",
  title: "Essensplan-Rückblick",
  created_by: "family",
  assignee: "coder-claude",
  completed_at: 1_781_170_000,
  draft_excerpt: "# Draft\nInhalt des Drafts",
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
      />,
    );
    expect(html).toContain("Essensplan-Rückblick");
    expect(html).toContain("Familie");
    expect(html).toContain("Freigeben → bauen");
    expect(html).toContain("Verwerfen");
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
      />,
    );
    expect(html).toContain("Bestätigen");
    expect(html).toContain("Discord-Idee");
    expect(html).toContain("Inhalt des Drafts");
  });
});
