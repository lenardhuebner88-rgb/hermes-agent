import { describe, expect, it } from "vitest";
import { renderToStaticMarkup } from "react-dom/server";
import { ResearchEntry, type ResearchCard } from "./ResearchView";
import { pickAnswer } from "./ResearchView.helpers";

// Härtung (e): pickAnswer war exportiert aber ungetestet; ResearchEntry
// bekommt Render-Tests nach RunTimelineView-Muster (statisch, initialer
// Zustand — Hooks/Polling laufen im Static-Render nicht).

const card = (over: Partial<ResearchCard>): ResearchCard => ({
  id: "t_51591401",
  title: "Public-Viewing mit Kindern — 3 Tipps",
  status: "done",
  created_at: 1781161012,
  latest_summary: null,
  ...over,
});

describe("pickAnswer", () => {
  it("letzter Kommentar gewinnt (Receipt-Muster)", () => {
    const a = pickAnswer({
      task: { id: "t", title: "x", status: "done", result: "RESULT" },
      comments: [
        { author: "research", body: "Zwischenstand", created_at: 1 },
        { author: "research", body: "Die echte Antwort.", created_at: 2 },
      ],
    });
    expect(a?.body).toBe("Die echte Antwort.");
    expect(a?.author).toBe("research");
    expect(a?.at).toBe(2);
  });

  it("ohne Kommentare zählt task.result, sonst null", () => {
    expect(
      pickAnswer({ task: { id: "t", title: "x", status: "done", result: "  R  " }, comments: [] })?.body,
    ).toBe("R");
    expect(pickAnswer({ task: { id: "t", title: "x", status: "done", result: null }, comments: [] })).toBeNull();
    expect(pickAnswer({ task: null, comments: [] })).toBeNull();
  });
});

describe("ResearchEntry (Render)", () => {
  it("zeigt Titel und Status-Chip für fertige Recherchen", () => {
    const html = renderToStaticMarkup(<ResearchEntry card={card({})} now={1781161500} />);
    expect(html).toContain("Public-Viewing mit Kindern — 3 Tipps");
    // Detail/Antwort erst nach Aufklappen — initial nicht im Markup
    expect(html).not.toContain("Frage");
  });

  it("laufende Recherche bekommt den running-Chip-Ton", () => {
    const html = renderToStaticMarkup(<ResearchEntry card={card({ status: "running" })} now={1781161500} />);
    expect(html).toContain("text-cyan-300");
  });
});
