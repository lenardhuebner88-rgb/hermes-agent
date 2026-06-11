import { describe, expect, it, vi } from "vitest";
import { renderToStaticMarkup } from "react-dom/server";
import { LanesEditor } from "./LanesView";
import type { LanesResponse } from "./lanes/api";

const fixture: LanesResponse = {
  count: 2,
  active_id: "lane_1",
  profiles: [
    { name: "coder", worker_runtime: "hermes", default_model: "gpt-5.5", description: "" },
    { name: "premium", worker_runtime: "claude-cli", default_model: "claude-fable-5", description: "" },
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

  it("Übernehmen ist auf der aktiven Lane ohne Änderungen deaktiviert (zeigt Aktiv)", () => {
    const html = renderToStaticMarkup(
      <LanesEditor data={fixture} lane={fixture.lanes[0]} busy={false} actions={noopActions} />,
    );
    expect(html).toMatch(/disabled[^>]*>.*Aktiv/s);
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
