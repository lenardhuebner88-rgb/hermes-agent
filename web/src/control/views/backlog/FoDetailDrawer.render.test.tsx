// @vitest-environment jsdom
import { cleanup, render, screen } from "@testing-library/react";
import { afterEach, describe, expect, it } from "vitest";

import type { BacklogDetail, BacklogItem } from "../../lib/schemas";
import { FoDetailDrawer } from "./FoDetailDrawer";

const item: BacklogItem = {
  id: "0042",
  title: "Listen-Ansicht glattziehen",
  status: "next",
  owner: "claude",
  risk: "medium",
  area: "lists",
  updated: "2026-07-28",
  lane: null,
  result: null,
  stale: false,
  excerpt: "Kurze Beschreibung",
  source_path: "backlog/items/0042-listen.md",
  readiness: "ready",
};

const detail: BacklogDetail = {
  id: "0042",
  title: "Listen-Ansicht glattziehen",
  status: "next",
  owner: "claude",
  risk: "medium",
  area: "lists",
  updated: "2026-07-28",
  lane: null,
  result: null,
  stale: false,
  readiness: "ready",
  body: "Body",
  decision: ["Wir bleiben bei einer Spalte."],
  acceptance_criteria: ["Liste rendert ohne Sprung."],
  proofs: ["vitest-Lauf vom 2026-07-28 grün."],
  blockers: ["Wartet auf API-Freigabe."],
  next_action: "Spec finalisieren.",
  source_path: "backlog/items/0042-listen.md",
  source_ref: "git:origin/main",
  links: [],
};

afterEach(cleanup);

describe("FoDetailDrawer German copy", () => {
  it("labels metrics, next action, and sections in German via de.backlog.*", () => {
    const { container } = render(<FoDetailDrawer item={item} detail={detail} loading={false} onClose={() => undefined} />);

    // Metrik-Block
    expect(screen.getByText("Risiko")).toBeTruthy();
    expect(screen.getByText("Bereich")).toBeTruthy();
    expect(screen.queryByText("Risk")).toBeNull();
    expect(screen.queryByText("Area")).toBeNull();

    // Next-Action-Block
    expect(screen.getByText("Nächster Schritt")).toBeTruthy();

    // SectionLines-Überschriften
    expect(screen.getByText("Akzeptanzkriterien")).toBeTruthy();
    expect(screen.getByText("Blocker")).toBeTruthy();

    // Negative Kontrollprobe: kein englischer Label-Rest im Drawer-Body
    expect(container.textContent).not.toMatch(/Next Action|Acceptance Criteria|Why now|Last Proof/);
  });
});
