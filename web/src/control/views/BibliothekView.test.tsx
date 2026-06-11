import { describe, expect, it } from "vitest";
import { groupBySeries, type LibraryItem } from "./BibliothekView";

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
