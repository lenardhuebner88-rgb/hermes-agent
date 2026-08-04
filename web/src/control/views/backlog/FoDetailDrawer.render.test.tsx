// @vitest-environment jsdom
import { cleanup, render, screen } from "@testing-library/react";
import { afterEach, describe, expect, it } from "vitest";

import type { BacklogDetail, BacklogItem } from "../../lib/schemas";
import { FoDetailDrawer } from "./FoDetailDrawer";

afterEach(cleanup);

function item(overrides: Partial<BacklogItem> = {}): BacklogItem {
  return {
    id: "0042",
    title: "Einkaufslisten zusammenführen",
    status: "next",
    owner: "piet",
    risk: "low",
    area: "lists",
    updated: "2026-08-01",
    lane: null,
    result: null,
    stale: false,
    excerpt: "Kurze Beschreibung",
    source_path: "backlog/items/0042-einkaufslisten.md",
    ...overrides,
  };
}

function detail(overrides: Partial<BacklogDetail> = {}): BacklogDetail {
  return {
    id: "0042",
    title: "Einkaufslisten zusammenführen",
    status: "next",
    owner: "piet",
    risk: "low",
    area: "lists",
    updated: "2026-08-01",
    lane: null,
    result: null,
    stale: false,
    body: "",
    decision: ["Wir bündeln alle Listen in einer Ansicht."],
    acceptance_criteria: ["Alle Listen sind in einer Ansicht sichtbar."],
    proofs: ["Screenshot der kombinierten Liste."],
    blockers: ["Abhängigkeit vom Sync-Job."],
    next_action: "Spec lesen und Umsetzung vorbereiten.",
    source_path: "backlog/items/0042-einkaufslisten.md",
    source_ref: "git:origin/main",
    links: [],
    ...overrides,
  };
}

describe("FoDetailDrawer German copy", () => {
  it("renders metric, next-action and section labels in German", () => {
    const { container } = render(
      <FoDetailDrawer item={item()} detail={detail()} loading={false} onClose={() => undefined} />,
    );

    // Metrik-Block: deutsche Labels statt Risk/Area/Owner.
    expect(screen.getByText("Status")).toBeTruthy();
    expect(screen.getByText("Risiko")).toBeTruthy();
    expect(screen.getByText("Bereich")).toBeTruthy();
    expect(screen.getByText("Verantwortlich")).toBeTruthy();

    // Next-Action-Block: deutsche Überschrift.
    expect(screen.getByText("Nächster Schritt")).toBeTruthy();

    // Alle vier SectionLines-Überschriften deutsch.
    expect(screen.getByText("Entscheidung / Warum jetzt")).toBeTruthy();
    expect(screen.getByText("Akzeptanzkriterien")).toBeTruthy();
    expect(screen.getByText("Aktuelle Belege / Letzter Beleg")).toBeTruthy();
    expect(screen.getByText("Blocker")).toBeTruthy();

    // Negative Kontrollprobe: keine englischen Labels mehr im Drawer.
    expect(container.textContent).not.toMatch(/Next Action|Acceptance Criteria|Why now|Last Proof|Owner/);
  });
});
