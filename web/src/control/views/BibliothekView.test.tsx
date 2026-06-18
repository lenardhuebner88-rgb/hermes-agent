import { describe, expect, it } from "vitest";
import { renderToStaticMarkup } from "react-dom/server";
import { CATEGORY_LABEL, countByCategory, groupBySeries, newestPerCategory, seriesNeighbors } from "./BibliothekView.helpers";
import {
  BibliothekView,
  ItemRow,
  LesesaalBody,
  ReadingView,
  SavedSearchShelf,
  TopicFollowCard,
  TopicFollowSection,
  type LibraryItem,
  type LibrarySavedSearch,
  type LibraryTopic,
} from "./BibliothekView";

const item = (over: Partial<LibraryItem>): LibraryItem => ({
  id: "x",
  category: "news",
  series_id: "s",
  series: "Serie",
  title: "T",
  ts: 0,
  preview: "",
  source_ref: "",
  series_meta: "",
  ...over,
});

describe("groupBySeries (Bibliothek-Regal)", () => {
  it("gruppiert nach series_id, Reihenfolge in der Gruppe bleibt (neueste zuerst), frischste Serie zuerst", () => {
    const items = [
      item({ id: "wm2", series_id: "p/wm", series: "WM Morgenbrief", ts: 200 }),
      item({ id: "ki1", series_id: "p/ki", series: "KI Modell-Brief", ts: 150 }),
      item({ id: "wm1", series_id: "p/wm", series: "WM Morgenbrief", ts: 100 }),
    ];
    const shelves = groupBySeries(items);
    expect(shelves.map((s) => s.series)).toEqual(["WM Morgenbrief", "KI Modell-Brief"]);
    expect(shelves[0].items.map((i) => i.id)).toEqual(["wm2", "wm1"]);
    expect(shelves[1].items).toHaveLength(1);
  });
});

describe("newestPerCategory (Frontpage-Auswahl)", () => {
  it("nimmt je Kategorie nur das erste (= neueste) Item, Reihenfolge der Erst-Treffer bleibt", () => {
    const top = newestPerCategory([
      item({ id: "n2", category: "news", ts: 300 }),
      item({ id: "n1", category: "news", ts: 200 }),
      item({ id: "r2", category: "recherchen", ts: 250 }),
      item({ id: "r1", category: "recherchen", ts: 150 }),
    ]);
    expect(top.map((i) => i.id)).toEqual(["n2", "r2"]);
  });

  it("liefert leere Liste für leere Eingabe", () => {
    expect(newestPerCategory([])).toEqual([]);
  });
});

describe("countByCategory (Chip-Zähler)", () => {
  it("zählt Einträge je Kategorie", () => {
    const counts = countByCategory([
      item({ category: "news" }),
      item({ category: "news" }),
      item({ category: "recherchen" }),
    ]);
    expect(counts).toEqual({ news: 2, recherchen: 1 });
  });
});

describe("seriesNeighbors (Vor/Zurück in der Serie)", () => {
  const series = [
    item({ id: "c", series_id: "s", ts: 300 }),
    item({ id: "b", series_id: "s", ts: 200 }),
    item({ id: "a", series_id: "s", ts: 100 }),
    item({ id: "x", series_id: "other", ts: 999 }),
  ];

  it("liefert keine Nachbarn ohne aktuelles Item", () => {
    expect(seriesNeighbors(series, null)).toEqual({ prev: null, next: null });
  });

  it("findet in der Mitte ältere (prev) und neuere (next) Ausgabe derselben Serie", () => {
    const n = seriesNeighbors(series, series[1]);
    expect(n.prev?.id).toBe("a");
    expect(n.next?.id).toBe("c");
  });

  it("hat am neuesten Eintrag kein next, am ältesten kein prev", () => {
    expect(seriesNeighbors(series, series[0]).next).toBeNull();
    expect(seriesNeighbors(series, series[0]).prev?.id).toBe("b");
    expect(seriesNeighbors(series, series[2]).prev).toBeNull();
    expect(seriesNeighbors(series, series[2]).next?.id).toBe("b");
  });

  it("ignoriert Items anderer Serien (other-Serie hat nur sich selbst)", () => {
    expect(seriesNeighbors(series, series[3])).toEqual({ prev: null, next: null });
  });
});

describe("ItemRow (Render, RunTimelineView-Muster)", () => {
  it("zeigt Titel und markiert nur Einträge nach dem letzten Besuch als neu", () => {
    const fresh = renderToStaticMarkup(
      <ItemRow item={item({ title: "WM Morgenbrief — Ausgabe 11.06.", ts: 200 })} unreadSince={100} onOpen={() => {}} />,
    );
    expect(fresh).toContain("WM Morgenbrief — Ausgabe 11.06.");
    expect(fresh).toContain("neu");
    const seen = renderToStaticMarkup(
      <ItemRow item={item({ title: "Alt", ts: 50 })} unreadSince={100} onOpen={() => {}} />,
    );
    expect(seen).toContain("Alt");
    expect(seen).not.toContain("neu");
  });
});

describe("Receipts-Regal (S2)", () => {
  it("hat ein eigenes Chip-Label und rendert Receipt-Items wie jede Serie", () => {
    expect(CATEGORY_LABEL.receipts).toBe("Receipts");
    const row = renderToStaticMarkup(
      <ItemRow
        item={item({ category: "receipts", series: "Claude-Code", title: "Receipt — Härtungs-Lauf", ts: 200 })}
        unreadSince={100}
        onOpen={() => {}}
      />,
    );
    expect(row).toContain("Receipt — Härtungs-Lauf");
  });
});

describe("Themen-Follows und Smart Shelves", () => {
  const topics: LibraryTopic[] = [
    { id: "ki-modelle", title: "KI-Modelle", followed: false, subscribed: false, seeded: true, created_at: 0, updated_at: 0 },
    { id: "wm-2026-deutschland", title: "WM 2026 Deutschland", followed: true, subscribed: true, seeded: true, created_at: 1, updated_at: 2 },
    { id: "hermes-dashboard", title: "Hermes Dashboard", followed: false, subscribed: false, seeded: true, created_at: 0, updated_at: 0 },
    { id: "langfuse-langsmith", title: "Langfuse/LangSmith", followed: false, subscribed: false, seeded: true, created_at: 0, updated_at: 0 },
  ];

  it("rendert die vier Beispielthemen als followbare deutsche Chips/Karten", () => {
    const html = renderToStaticMarkup(<TopicFollowSection topics={topics} onToggle={() => {}} pendingTopicId={null} />);
    for (const label of ["KI-Modelle", "WM 2026 Deutschland", "Hermes Dashboard", "Langfuse/LangSmith"]) {
      expect(html).toContain(label);
    }
    expect(html).toContain("Thema folgen");
    expect(html).toContain("Folge ich");
    expect(html).toContain("Entfolgen");
    expect(html).toContain("Beobachtungsliste");
  });

  it("macht den Follow-Zustand nach neu geladenen Topic-Daten sichtbar", () => {
    const before = renderToStaticMarkup(<TopicFollowCard topic={topics[0]} onToggle={() => {}} pending={false} />);
    expect(before).toContain("Thema folgen");
    expect(before).not.toContain("Folge ich");

    const afterFollow = renderToStaticMarkup(<TopicFollowCard topic={{ ...topics[0], followed: true, subscribed: true }} onToggle={() => {}} pending={false} />);
    expect(afterFollow).toContain("Folge ich");
    expect(afterFollow).toContain("Entfolgen");

    const afterUnfollowReload = renderToStaticMarkup(<TopicFollowCard topic={{ ...topics[0], followed: false, subscribed: false }} onToggle={() => {}} pending={false} />);
    expect(afterUnfollowReload).toContain("Thema folgen");
    expect(afterUnfollowReload).not.toContain("Folge ich");
  });
});

describe("Gespeicherte Suchen und aggregierte Themenseite", () => {
  it("zeigt gespeicherte Suchen als Smart-Shelf-Liste", () => {
    const searches: LibrarySavedSearch[] = [
      { id: "ss_1", name: "KI Modelle täglich", title: "KI Modelle täglich", query: "frontier model releases", topic_tags: ["KI-Modelle"], person_tags: ["Piet"], created_at: 1, updated_at: 2 },
    ];
    const html = renderToStaticMarkup(<SavedSearchShelf searches={searches} onApply={() => {}} />);
    expect(html).toContain("Smart Shelves");
    expect(html).toContain("KI Modelle täglich");
    expect(html).toContain("frontier model releases");
    expect(html).toContain("KI-Modelle");
  });

  it("gruppiert aggregierte Treffer aus mindestens zwei Serien/Sources", () => {
    const shelves = groupBySeries([
      item({ id: "cron1", series_id: "profile:research/ki", series: "KI Modell-Brief", source_ref: "cron:research/ki", ts: 300 }),
      item({ id: "research1", series_id: "research", series: "Recherchen", source_ref: "task:t_123", ts: 200 }),
    ]);
    expect(shelves.map((s) => s.series)).toEqual(["KI Modell-Brief", "Recherchen"]);
    expect(shelves.flatMap((s) => s.items.map((i) => i.source_ref))).toEqual(["cron:research/ki", "task:t_123"]);
  });
});

describe("LesesaalBody (Such-Input Accessibility)", () => {
  it("das Such-Input trägt aria-label mit demselben Text wie den placeholder", () => {
    const html = renderToStaticMarkup(<LesesaalBody />);
    expect(html).toContain('aria-label="Suche in Titel + Text …"');
  });
});

describe("LesesaalBody Erst-Lade-Skeleton (B1)", () => {
  it("zeigt SkeletonCard (aria-busy) statt leerer Fläche wenn data noch null ist und kein error vorliegt", () => {
    // renderToStaticMarkup führt keinen useEffect aus → data bleibt null, error bleibt null
    const html = renderToStaticMarkup(<LesesaalBody />);
    expect(html).toContain('aria-busy="true"');
  });
});

describe("BibliothekView ARIA Tab/Panel-Verdrahtung (B3)", () => {
  it("Wissen-Modus (default): beide Tab-Buttons tragen aria-controls; nur Wissen-Panel im DOM", () => {
    // Default-Render: mode="wissen"
    const html = renderToStaticMarkup(<BibliothekView />);
    // Beide Tab-Buttons haben aria-controls
    expect(html).toContain('aria-controls="bibliothek-panel-wissen"');
    expect(html).toContain('aria-controls="bibliothek-panel-lesesaal"');
    // Nur das aktive Panel ist gemountet — Wissen-Panel vorhanden
    expect(html).toContain('id="bibliothek-panel-wissen"');
    expect(html).toContain('role="tabpanel"');
    // Lesesaal-Panel ist NICHT im DOM (Conditional bleibt erhalten)
    expect(html).not.toContain('id="bibliothek-panel-lesesaal"');
  });
});

describe("ReadingView Lade-Platzhalter (B2)", () => {
  const readingItem = item({ id: "r1", title: "Testausgabe" });
  const noNeighbors = { prev: null, next: null };

  it("zeigt SkeletonCard (aria-busy) statt plain '…' solange detail noch fehlt und kein error vorliegt", () => {
    // renderToStaticMarkup führt keinen useEffect aus → detail bleibt null, error bleibt null
    const html = renderToStaticMarkup(
      <ReadingView item={readingItem} neighbors={noNeighbors} onNavigate={() => {}} onBack={() => {}} />,
    );
    expect(html).toContain('aria-busy="true"');
    expect(html).not.toContain(">…<");
  });
});
