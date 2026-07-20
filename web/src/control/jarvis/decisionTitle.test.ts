/**
 * decisionTitle — S7.6: clientseitige Titel-Destillation (Fallback, wenn der
 * Server kein `summary` liefert). Muster aus gateway/pa_watcher.py
 * (briefing_title, S6.3) plus PlanSpec-Slug-Präfix, Cap ≤80 Zeichen.
 */
import { describe, expect, it } from "vitest";

import type { PaInboxItem } from "@/lib/api";

import { DECISION_TITLE_MAX, decisionAge, decisionHeadline, decisionTitle, needsOperatorKey } from "./decisionTitle";

function taskItem(
  overrides: Partial<Extract<PaInboxItem, { type: "held_task" | "freigabe_gate" }>> = {},
): PaInboxItem {
  return {
    type: "held_task",
    id: "t_abc123",
    card_id: "t_abc123",
    title: "Release-Kette hält auf Operator",
    status: "blocked",
    freigabe: null,
    block_radius: 0,
    ts: 1753000000,
    ...overrides,
  };
}

describe("decisionTitle (Destillation nach briefing_title)", () => {
  it("strippt das PlanSpec-Slug-Präfix (Piets Pain-Fall)", () => {
    expect(
      decisionTitle(
        "PlanSpec GATE-GREEN-KANBAN-LIFECYCLE-REGRESSION-FIX: Green-Gate-Ursachenfix: die live-reproduzierten Fehler",
      ),
    ).toBe("Green-Gate-Ursachenfix: die live-reproduzierten Fehler");
  });

  it("strippt Task-/Gate-Präfixe wie das Briefing", () => {
    expect(decisionTitle("Task t_abc123: Release-Kette bauen")).toBe("Release-Kette bauen");
    expect(decisionTitle("Gate bei Task t_abc123: Green-Gate fixen")).toBe("Green-Gate fixen");
  });

  it("entfernt Status-Suffixe (— completed / — blocked:gate)", () => {
    expect(decisionTitle("Release-Kette — completed")).toBe("Release-Kette");
    expect(decisionTitle("Worker wartet — blocked:gate")).toBe("Worker wartet");
  });

  it("entfernt Task-IDs und Beleg-Pfade", () => {
    expect(decisionTitle("Fix für t_0123456789abcdef im Ziel")).toBe("Fix für im Ziel");
    expect(decisionTitle("Fehler in /home/piet/.hermes/run.py gemeldet")).toBe("Fehler in gemeldet");
  });

  it("nimmt bei mehrzeiligen Titeln nur die erste Zeile (planspec.ingest)", () => {
    expect(
      decisionTitle("PlanSpec als gehaltene Kette ingesten?\nDraft: `draft_x`\nValidate: WARN"),
    ).toBe("PlanSpec als gehaltene Kette ingesten?");
  });

  it("kappt auf ≤80 Zeichen mit Ellipse", () => {
    const out = decisionTitle(`PlanSpec GATE-XYZ: ${"x".repeat(200)}`);
    expect(out.length).toBeLessThanOrEqual(DECISION_TITLE_MAX);
    expect(out.endsWith("…")).toBe(true);
  });

  it("leerer Titel → Fallback wie im Briefing", () => {
    expect(decisionTitle("")).toBe("Ereignis");
    expect(decisionTitle("   \n  ")).toBe("Ereignis");
    expect(decisionTitle(null)).toBe("Ereignis");
  });

  it("kurze Klartext-Titel bleiben unverändert", () => {
    expect(decisionTitle("Soll ich den Branch mergen?")).toBe("Soll ich den Branch mergen?");
    expect(decisionTitle("Release-Kette hält auf Operator")).toBe("Release-Kette hält auf Operator");
  });
});

describe("decisionHeadline (summary gewinnt, sonst Destillation)", () => {
  it("Server-summary hat Vorrang", () => {
    expect(
      decisionHeadline(taskItem({ title: "PlanSpec GATE-X: langer Roh-Titel", summary: "Landung freigeben" })),
    ).toBe("Landung freigeben");
  });

  it("ohne summary → destillierter Roh-Titel", () => {
    expect(decisionHeadline(taskItem({ title: "Task t_abc123: Release-Kette bauen" }))).toBe(
      "Release-Kette bauen",
    );
  });

  it("leeres summary → destillierter Roh-Titel", () => {
    expect(decisionHeadline(taskItem({ title: "Release — completed", summary: "  " }))).toBe("Release");
  });
});

describe("decisionAge (Alter-Badge)", () => {
  const NOW = 1_753_000_000;

  it("seit Xs/Xm/Xh/Xd je Alter", () => {
    expect(decisionAge(NOW - 45, NOW)).toBe("seit 45s");
    expect(decisionAge(NOW - 5 * 60, NOW)).toBe("seit 5m");
    expect(decisionAge(NOW - 5 * 3600, NOW)).toBe("seit 5h");
    expect(decisionAge(NOW - 3 * 86_400, NOW)).toBe("seit 3d");
  });

  it("Zukunft und Unsinn → null (Badge entfällt still)", () => {
    expect(decisionAge(NOW + 3600, NOW)).toBeNull();
    expect(decisionAge(0, NOW)).toBeNull();
    expect(decisionAge(Number.NaN, NOW)).toBeNull();
  });
});

describe("needsOperatorKey (🔑-Badge)", () => {
  it("freigabe=operator auf held_task/freigabe_gate → true", () => {
    expect(needsOperatorKey(taskItem({ freigabe: "operator" }))).toBe(true);
    expect(
      needsOperatorKey(taskItem({ type: "freigabe_gate", freigabe: "operator" })),
    ).toBe(true);
  });

  it("ohne/andere Freigabe → false", () => {
    expect(needsOperatorKey(taskItem())).toBe(false);
    expect(needsOperatorKey(taskItem({ freigabe: "auto" }))).toBe(false);
  });
});
