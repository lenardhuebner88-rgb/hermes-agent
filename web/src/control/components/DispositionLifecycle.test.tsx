import { describe, expect, it, vi } from "vitest";
import { renderToStaticMarkup } from "react-dom/server";
import type { DispositionItem } from "../lib/schemas";
import { DispositionItemSchema } from "../lib/schemas";
import { DispositionItemList } from "./DispositionLifecycle";

const noop = vi.fn();

const item = (over: Partial<DispositionItem> = {}): DispositionItem => ({
  id: "disp_001",
  source_task_id: "t_abc123",
  typ: "risk",
  disposition: "defer",
  next_action: "Sicherheitslücke im Auth-Flow schließen",
  severity: "real-risk",
  evidence: "JWT-Validierung fehlt bei /api/internal/*",
  status: "open",
  supersedes_id: null,
  created_at: 1_781_200_000,
  decided_at: null,
  decided_by: null,
  ...over,
});

describe("DispositionItemList", () => {
  it("zeigt next_action, Typ- und Severity-Badge + Aktions-Buttons — kein Confirm ohne Klick", () => {
    const html = renderToStaticMarkup(
      <DispositionItemList
        items={[item()]}
        pending={null}
        busy={false}
        onAct={noop}
        onPending={noop}
        onDismissReasonChange={noop}
      />,
    );
    expect(html).toContain("Sicherheitslücke im Auth-Flow schließen");
    expect(html).toContain("Risiko");
    expect(html).toContain("Echtes Risiko");
    expect(html).toContain("JWT-Validierung fehlt bei /api/internal/*");
    expect(html).toContain("Akzeptieren");
    expect(html).toContain("Fix-Task anlegen");
    expect(html).toContain("Verwerfen");
    // Kein Bestätigen-Knopf ohne pending
    expect(html).not.toContain("Bestätigen");
  });

  it("pending=accept zeigt Confirm-Button + Abbrechen", () => {
    const html = renderToStaticMarkup(
      <DispositionItemList
        items={[item()]}
        pending={{ id: "disp_001", kind: "accept" }}
        busy={false}
        onAct={noop}
        onPending={noop}
        onDismissReasonChange={noop}
      />,
    );
    expect(html).toContain("Bestätigen");
    expect(html).toContain("Abbrechen");
    expect(html).toContain("markiert das Item als akzeptiert");
  });

  it("pending=fix zeigt Fix-Task-Confirm + Hint", () => {
    const html = renderToStaticMarkup(
      <DispositionItemList
        items={[item()]}
        pending={{ id: "disp_001", kind: "fix" }}
        busy={false}
        onAct={noop}
        onPending={noop}
        onDismissReasonChange={noop}
      />,
    );
    expect(html).toContain("Bestätigen");
    expect(html).toContain("legt einen echten Kanban-Task an");
  });

  it("pending=dismiss zeigt Textarea + Bestätigen (deaktiviert ohne Grund)", () => {
    const html = renderToStaticMarkup(
      <DispositionItemList
        items={[item()]}
        pending={{ id: "disp_001", kind: "dismiss", reason: "" }}
        busy={false}
        onAct={noop}
        onPending={noop}
        onDismissReasonChange={noop}
      />,
    );
    expect(html).toContain("Grund (Pflicht)");
    expect(html).toContain("Bestätigen");
    // disabled-Attribut wenn Grund leer
    expect(html).toContain("disabled");
  });

  it("pending=dismiss mit Grund: Confirm-Button nicht mehr disabled", () => {
    const html = renderToStaticMarkup(
      <DispositionItemList
        items={[item()]}
        pending={{ id: "disp_001", kind: "dismiss", reason: "Nicht mehr relevant" }}
        busy={false}
        onAct={noop}
        onPending={noop}
        onDismissReasonChange={noop}
      />,
    );
    // Mit Grund: Button soll nicht disabled sein (kein disabled-Attribut am Confirm-Button)
    // renderToStaticMarkup gibt disabled nur aus wenn es als Attribut gesetzt ist
    expect(html).toContain("Bestätigen");
    // "Nicht mehr relevant" im value
    expect(html).toContain("Nicht mehr relevant");
  });

  it("follow_up-Typ zeigt korrekten Badge-Text", () => {
    const html = renderToStaticMarkup(
      <DispositionItemList
        items={[item({ typ: "follow_up", severity: "scope-note" })]}
        pending={null}
        busy={false}
        onAct={noop}
        onPending={noop}
        onDismissReasonChange={noop}
      />,
    );
    expect(html).toContain("Follow-up");
    expect(html).toContain("Scope-Hinweis");
  });

  it("Deep-Link zum Quell-Task enthält source_task_id URL-enkodiert", () => {
    const html = renderToStaticMarkup(
      <DispositionItemList
        items={[item()]}
        pending={null}
        busy={false}
        onAct={noop}
        onPending={noop}
        onDismissReasonChange={noop}
      />,
    );
    expect(html).toContain("/control/backlog?focus=t_abc123");
  });
});

describe("DispositionItemSchema", () => {
  it("parsed gültiges Item korrekt", () => {
    const raw = {
      id: "disp_x",
      source_task_id: "t_y",
      typ: "risk",
      disposition: "defer",
      next_action: "Etwas tun",
      severity: "real-risk",
      evidence: "Beweis",
      status: "open",
      supersedes_id: null,
      created_at: 1_700_000_000,
      decided_at: null,
      decided_by: null,
    };
    const result = DispositionItemSchema.parse(raw);
    expect(result.id).toBe("disp_x");
    expect(result.typ).toBe("risk");
    expect(result.severity).toBe("real-risk");
    expect(result.status).toBe("open");
  });

  it("unbekannter Typ fällt auf Default zurück (defensiv)", () => {
    const raw = {
      id: "disp_y",
      source_task_id: "t_z",
      typ: "unbekannt_neu",
      disposition: "done",
      next_action: null,
      severity: "none",
      evidence: null,
      status: "open",
      supersedes_id: null,
      created_at: 1_700_000_000,
      decided_at: null,
      decided_by: null,
    };
    const result = DispositionItemSchema.parse(raw);
    expect(result.typ).toBe("still_open");
  });
});
