import { describe, expect, it, vi } from "vitest";
import { renderToStaticMarkup } from "react-dom/server";
import { LanesEditor } from "./LanesView";
import type { LanesResponse } from "./lanes/api";

const fixture: LanesResponse = {
  count: 2,
  active_id: "lane_1",
  profiles: [
    {
      name: "coder",
      worker_runtime: "hermes",
      default_model: "gpt-5.5",
      description: "",
      kanban_spawn_health: "healthy",
    },
    {
      name: "premium",
      worker_runtime: "claude-cli",
      default_model: "claude-fable-5",
      description: "",
      kanban_spawn_health: { status: "unhealthy", reason: "claude-cli Login fehlt" },
    },
  ],
  models: [
    { id: "claude-fable-5", label: "Claude Fable 5", runtime: "claude-cli", group: "Claude (Max-Abo)" },
    { id: "claude-opus-4-8", label: "Claude Opus 4.8", runtime: "claude-cli", group: "Claude (Max-Abo)" },
    { id: "gpt-5.5", label: "GPT-5.5", runtime: "hermes", group: "API-Modelle" },
    { id: "qwen/qwen3.7-max", label: "Qwen 3.7 Max", runtime: "hermes", group: "API-Modelle" },
  ],
  lanes: [
    {
      id: "lane_1",
      name: "api-standard",
      active: true,
      builtin: true,
      created_at: 0,
      updated_at: 0,
      profiles: {
        coder: { worker_runtime: "hermes", model: "gpt-5.5" },
      },
    },
    {
      id: "lane_2",
      name: "max-abo",
      active: false,
      builtin: true,
      created_at: 0,
      updated_at: 0,
      profiles: {
        premium: { worker_runtime: "claude-cli", model: "claude-fable-5" },
      },
    },
  ],
};

const noopActions = {
  onSelect: vi.fn(),
  onApply: vi.fn(),
  onCreate: vi.fn(),
  onDelete: vi.fn(),
};

describe("LanesEditor (einfache Modell-Schaltung)", () => {
  it("zeigt Preset-Dropdown mit aktiv-Markierung und Übernehmen-Button", () => {
    const html = renderToStaticMarkup(
      <LanesEditor data={fixture} lane={fixture.lanes[1]} busy={false} actions={noopActions} />,
    );
    expect(html).toContain("api-standard");
    expect(html).toContain("max-abo");
    expect(html).toContain("(aktiv)");
    // inaktives Preset gewählt → ein Klick übernimmt es
    expect(html).toContain("Übernehmen");
  });

  it("rendert pro Rolle genau ein Modell-Dropdown mit sprechenden Namen", () => {
    const html = renderToStaticMarkup(
      <LanesEditor data={fixture} lane={fixture.lanes[0]} busy={false} actions={noopActions} />,
    );
    // Rollen aus dem Katalog + nicht-technische Hinweise
    expect(html).toContain("Schreibt Code");
    expect(html).toContain("Schwere Spezialfälle");
    // Gruppierte, sprechende Modell-Optionen statt Freitext
    expect(html).toContain("Claude (Max-Abo)");
    expect(html).toContain("Claude Fable 5");
    expect(html).toContain("Qwen 3.7 Max");
    expect(html).toContain("Standard (Claude Fable 5)");
    // Kein roher Runtime-Select mehr
    expect(html).not.toContain(">Runtime<");
  });

  it("zeigt pro Rolle einen Worker-Check-Button", () => {
    const html = renderToStaticMarkup(
      <LanesEditor data={fixture} lane={fixture.lanes[0]} busy={false} actions={noopActions} />,
    );
    expect(html.match(/Worker-Check/g)).toHaveLength(2);
  });

  it("warnt und blockiert Übernehmen bei offensichtlich falscher Runtime-/Modell-Kombination", () => {
    const badLane = {
      ...fixture.lanes[1],
      active: false,
      profiles: {
        coder: { worker_runtime: "hermes" as const, model: "claude-fable-5" },
      },
    };
    const html = renderToStaticMarkup(
      <LanesEditor data={fixture} lane={badLane} busy={false} actions={noopActions} />,
    );
    expect(html).toContain("Worker-/Modell-Kombination passt nicht");
    expect(html).toMatch(/disabled[^>]*>.*Übernehmen/s);
  });

  it("Übernehmen ist auf der aktiven Lane ohne Änderungen deaktiviert (zeigt Aktiv)", () => {
    const html = renderToStaticMarkup(
      <LanesEditor data={fixture} lane={fixture.lanes[0]} busy={false} actions={noopActions} />,
    );
    expect(html).toMatch(/disabled[^>]*>.*Aktiv/s);
  });

  it("zeigt passive Spawn-Bereitschaft pro Rolle als Dot + Klartext-Hinweis", () => {
    const html = renderToStaticMarkup(
      <LanesEditor data={fixture} lane={fixture.lanes[0]} busy={false} actions={noopActions} />,
    );
    // coder healthy → grüner Dot; premium unhealthy → Warn-Dot + Hinweis
    expect(html).toContain("hc-led-live");
    expect(html).toContain("hc-led-warn");
    expect(html).toContain("Spawn-Bereitschaft: bereit");
    expect(html).toContain("Spawn-Bereitschaft: gestört");
  });

  it("zeigt Override-Chip mit Modell-Label bzw. Standard-Chip pro Rolle", () => {
    const html = renderToStaticMarkup(
      <LanesEditor data={fixture} lane={fixture.lanes[0]} busy={false} actions={noopActions} />,
    );
    // coder hat einen Lane-Eintrag (hermes|gpt-5.5) → aktiver Override
    expect(html).toContain("Override · GPT-5.5");
    // premium ohne Lane-Eintrag → expliziter Standard-Zustand
    expect(html).toContain(">Standard<");
  });

  it("fasst Bereitschaft + Overrides im Panel-Meta zusammen", () => {
    const html = renderToStaticMarkup(
      <LanesEditor data={fixture} lane={fixture.lanes[0]} busy={false} actions={noopActions} />,
    );
    expect(html).toContain("1/2 bereit · 1 Override");
  });

  it("skaliert: 12 Rollen bleiben als Dot-Spalte mit Summen-Meta scannbar", () => {
    const crowded: LanesResponse = {
      ...fixture,
      profiles: Array.from({ length: 12 }, (_, i) => ({
        name: `worker-${i}`,
        worker_runtime: "hermes" as const,
        default_model: "gpt-5.5",
        description: "",
        kanban_spawn_health:
          i % 3 === 0
            ? ("healthy" as const)
            : i % 3 === 1
              ? { status: "unhealthy" as const, reason: "Spawn-Probe rot" }
              : undefined,
      })),
      lanes: [{ ...fixture.lanes[0], profiles: {} }],
    };
    const html = renderToStaticMarkup(
      <LanesEditor data={crowded} lane={crowded.lanes[0]} busy={false} actions={noopActions} />,
    );
    expect((html.match(/hc-led-live/g) ?? []).length).toBe(4);
    expect((html.match(/hc-led-warn/g) ?? []).length).toBe(4);
    expect((html.match(/hc-led-idle/g) ?? []).length).toBe(4);
    expect(html).toContain("4/12 bereit · 0 Overrides");
  });

  it("Presets-Liste nennt den Override-Umfang jedes Presets", () => {
    const html = renderToStaticMarkup(
      <LanesEditor data={fixture} lane={fixture.lanes[0]} busy={false} actions={noopActions} />,
    );
    // beide Fixture-Lanes mappen je genau 1 Profil
    expect((html.match(/1 Override</g) ?? []).length).toBeGreaterThanOrEqual(2);
  });

  it("Presets-Sektion: anlegen + Löschen mit Inline-Confirm", () => {
    const html = renderToStaticMarkup(
      <LanesEditor data={fixture} lane={fixture.lanes[0]} busy={false} actions={noopActions} />,
    );
    expect(html).toContain("Auswahl als Preset speichern");
    expect(html).toContain("Name für neues Preset");
    expect(html).not.toContain("wirklich löschen?");

    const armed = renderToStaticMarkup(
      <LanesEditor
        data={fixture}
        lane={fixture.lanes[0]}
        busy={false}
        actions={noopActions}
        initialPendingDelete="lane_2"
      />,
    );
    expect(armed).toContain("wirklich löschen?");
    expect(armed).toContain("Bestätigen");
    expect(armed).toContain("Abbrechen");
  });
});
