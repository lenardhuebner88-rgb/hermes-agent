import { describe, expect, it } from "vitest";
import { readFileSync } from "node:fs";
import { fileURLToPath } from "node:url";
import { renderToStaticMarkup } from "react-dom/server";
import { MemoryRouter } from "react-router-dom";
import { CATEGORY_LABEL, countByCategory, dedupeById, filterBriefings, groupBySeries, newestBriefing, newestPerCategory, seriesNeighbors, sortItems } from "./BibliothekView.helpers";
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

const src = readFileSync(fileURLToPath(new URL("./BibliothekView.tsx", import.meta.url)), "utf8");

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

describe("Vault-Provenienz-Regal", () => {
  it("Vault-ProvenanceShelf ist weiterhin verfügbar und rendert im Regal-Look", () => {
    expect(src).toContain("VaultProvenanceShelf");
    expect(src).toContain("SectionHeader");
    expect(src).toContain("ListRow");
    expect(src).toContain("SubtabChips");
    expect(src).toContain("DrawerShell");
    expect(src).not.toMatch(/#[0-9a-fA-F]{3,8}\b/);
  });

  // Die frühere Gegenprobe (OverviewView enthält keine Provenienz mehr) ist nach
  // dem Abriss (S5) gegenstandslos: OverviewView wurde komplett entfernt, die
  // Vault-Provenienz lebt ausschließlich hier in der Bibliothek (Test oben).
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
    const html = renderToStaticMarkup(<MemoryRouter><LesesaalBody /></MemoryRouter>);
    expect(html).toContain('aria-label="Suche in Titel + Text …"');
  });
});

describe("LesesaalBody Erst-Lade-Skeleton (B1)", () => {
  it("zeigt SkeletonCard (aria-busy) statt leerer Fläche wenn data noch null ist und kein error vorliegt", () => {
    // renderToStaticMarkup führt keinen useEffect aus → data bleibt null, error bleibt null
    const html = renderToStaticMarkup(<MemoryRouter><LesesaalBody /></MemoryRouter>);
    expect(html).toContain('aria-busy="true"');
  });
});

describe("BibliothekView ARIA Tab/Panel-Verdrahtung (B3, angepasst für S2/S3)", () => {
  it("Briefings-Modus (default, keine URL-Params): alle Tab-Buttons tragen aria-controls; alle Panels sind gemountet, Wissen und Lesesaal sind hidden", () => {
    const html = renderToStaticMarkup(<MemoryRouter><BibliothekView /></MemoryRouter>);
    // Alle Tab-Buttons haben aria-controls
    expect(html).toContain('aria-controls="bibliothek-panel-briefings"');
    expect(html).toContain('aria-controls="bibliothek-panel-wissen"');
    expect(html).toContain('aria-controls="bibliothek-panel-lesesaal"');
    // S3: alle Panels bleiben IMMER gemountet (nur `hidden` schaltet um) —
    // sonst verwirft der Moduswechsel den Zustand des jeweils anderen Modus.
    expect(html).toContain('id="bibliothek-panel-briefings"');
    expect(html).toContain('id="bibliothek-panel-wissen"');
    expect(html).toContain('id="bibliothek-panel-lesesaal"');
    expect(html).toContain('role="tabpanel"');
    expect(html).not.toMatch(/id="bibliothek-panel-briefings"[^>]*hidden/);
    expect(html).toMatch(/id="bibliothek-panel-wissen"[^>]*hidden/);
    expect(html).toMatch(/id="bibliothek-panel-lesesaal"[^>]*hidden/);
  });

  it("Lesesaal-Modus über ?mode=lesesaal (S2, Deep-Link): Briefings und Wissen sind hidden, Lesesaal nicht", () => {
    const html = renderToStaticMarkup(
      <MemoryRouter initialEntries={["/control/bibliothek?mode=lesesaal"]}>
        <BibliothekView />
      </MemoryRouter>,
    );
    expect(html).toMatch(/id="bibliothek-panel-briefings"[^>]*hidden/);
    expect(html).toMatch(/id="bibliothek-panel-wissen"[^>]*hidden/);
    expect(html).not.toMatch(/id="bibliothek-panel-lesesaal"[^>]*hidden/);
  });

  it("unbekannter mode-Wert fällt sicher auf Briefings zurück", () => {
    const html = renderToStaticMarkup(
      <MemoryRouter initialEntries={["/control/bibliothek?mode=quatsch"]}>
        <BibliothekView />
      </MemoryRouter>,
    );
    expect(html).not.toMatch(/id="bibliothek-panel-briefings"[^>]*hidden/);
    expect(html).toMatch(/id="bibliothek-panel-wissen"[^>]*hidden/);
    expect(html).toMatch(/id="bibliothek-panel-lesesaal"[^>]*hidden/);
  });
});

describe("BibliothekView URL-Zustand — Wiring (S2/S3, Quelltext-Beweis wie ChainVizView)", () => {
  // BibliothekView/LesesaalBody sind router-hook-gebunden (useSearchParams) —
  // dieselbe Prüf-Konvention wie ChainVizView.test.tsx für Verhalten, das erst
  // interaktiv (Klick → setSearchParams) sichtbar wird und hier zusätzlich
  // durch BibliothekView.render.test.tsx (jsdom) verhaltensgeprüft wird.
  it("Moduswechsel und Item-Schließen sind ein replace (kein Verlaufseintrag)", () => {
    expect(src).toContain('if (next === "briefings") p.delete("mode");');
    expect(src).toContain('p.set("mode", next);');
    expect(src).toContain('p.delete("item");');
    // Beide replace-Aufrufe (setMode + closeItem) sind verdrahtet.
    expect(src.match(/\}, \{ replace: true \}\)/g)?.length).toBeGreaterThanOrEqual(2);
  });

  it("Dokument-öffnen ist ein push (kein replace) — Back-Button schließt das Dokument", () => {
    expect(src).toContain('p.set("item", next.id);');
    // openItem's setSearchParams-Aufruf endet OHNE { replace: true }.
    expect(src).toContain('p.set("item", next.id);\n      return p;\n    });');
  });

  it("Deep-Link-Resolution liest den item-Param und fällt auf einen Direkt-Fetch zurück", () => {
    expect(src).toMatch(/searchParams\.get\("item"\)/);
    expect(src).toMatch(/\/api\/library\/item\?id=\$\{encodeURIComponent\(id\)\}/);
  });

  it("has_more/offset steuern \"Mehr laden\" (S6)", () => {
    expect(src).toMatch(/has_more/);
    expect(src).toMatch(/offset/);
    expect(src).toMatch(/dedupeById/);
  });

  it("Inhaltsverzeichnis nutzt extractToc/TocNav wie das Nachschlagewerk (S4)", () => {
    expect(src).toMatch(/extractToc/);
    expect(src).toMatch(/toc\.length >= 3/);
    expect(src).toMatch(/<TocNav /);
  });
});

describe("filterBriefings und newestBriefing (Briefings-First)", () => {
  it("filterBriefings liefert nur Items mit category === briefings", () => {
    const items = [
      item({ id: "b1", category: "briefings", ts: 300 }),
      item({ id: "n1", category: "news", ts: 250 }),
      item({ id: "b2", category: "briefings", ts: 200 }),
    ];
    expect(filterBriefings(items).map((i) => i.id)).toEqual(["b1", "b2"]);
  });

  it("newestBriefing liefert das erste Briefing oder null", () => {
    expect(newestBriefing([item({ id: "b1", category: "briefings" }), item({ id: "b2", category: "briefings" })])?.id).toBe("b1");
    expect(newestBriefing([item({ id: "n1", category: "news" })])).toBeNull();
  });
});

describe("sortItems (Lesesaal-Sortierung, S5)", () => {
  const items: LibraryItem[] = [
    { id: "b", category: "news", series_id: "s", series: "Serie", title: "Bananen-Digest", ts: 200, preview: "", source_ref: "", series_meta: "" },
    { id: "a", category: "news", series_id: "s", series: "Serie", title: "Apfel-Digest", ts: 100, preview: "", source_ref: "", series_meta: "" },
    { id: "c", category: "news", series_id: "s", series: "Serie", title: "Citrus-Digest", ts: 300, preview: "", source_ref: "", series_meta: "" },
  ];

  it("Neueste (Default) lässt die Server-Reihenfolge unverändert", () => {
    expect(sortItems(items, "newest").map((i) => i.id)).toEqual(["b", "a", "c"]);
  });

  it("Älteste sortiert nach ts aufsteigend", () => {
    expect(sortItems(items, "oldest").map((i) => i.id)).toEqual(["a", "b", "c"]);
  });

  it("A–Z sortiert nach Titel", () => {
    expect(sortItems(items, "az").map((i) => i.id)).toEqual(["a", "b", "c"]);
  });

  it("mutiert die Eingabe nicht (newest gibt dieselbe Referenz zurück, oldest/az kopieren)", () => {
    const copy = [...items];
    sortItems(items, "oldest");
    expect(items).toEqual(copy);
  });
});

describe("dedupeById (Mehr laden, S6)", () => {
  it("verwirft doppelte Ids beim Anhängen einer zweiten Seite, Reihenfolge bleibt stabil", () => {
    const pageOne: LibraryItem[] = [
      { id: "cron::main::abc::2026-06-10_07-00-00.md", category: "briefings", series_id: "main/abc", series: "Morning Digest", title: "Morning Digest — Ausgabe 10.06.", ts: 300, preview: "x", source_ref: "cron:abc", series_meta: "" },
      { id: "cron::main::abc::2026-06-09_07-00-00.md", category: "briefings", series_id: "main/abc", series: "Morning Digest", title: "Morning Digest — Ausgabe 09.06.", ts: 200, preview: "x", source_ref: "cron:abc", series_meta: "" },
    ];
    // Server-Rand-Overlap: Seite 2 beginnt erneut mit dem letzten Item von Seite 1.
    const pageTwo: LibraryItem[] = [
      pageOne[1],
      { id: "cron::main::abc::2026-06-08_07-00-00.md", category: "briefings", series_id: "main/abc", series: "Morning Digest", title: "Morning Digest — Ausgabe 08.06.", ts: 100, preview: "x", source_ref: "cron:abc", series_meta: "" },
    ];
    const merged = dedupeById([...pageOne, ...pageTwo]);
    expect(merged.map((i) => i.id)).toEqual([pageOne[0].id, pageOne[1].id, pageTwo[1].id]);
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
