import { describe, expect, it, vi } from "vitest";
import { renderToStaticMarkup } from "react-dom/server";
import { readFileSync } from "node:fs";
import { LanesEditor } from "./LanesView";
import {
  authSmokeButtonLabel,
  authSmokeDisabled,
  authSmokeRenderableResults,
  laneAuthSmokeTone,
} from "./lanes/authSmoke";
import type { LaneAuthSmokeResult, LaneAuthSmokeScope, LaneAuthSmokeSummary, LanesResponse } from "./lanes/api";

const fixture: LanesResponse = {
  count: 2,
  active_id: "lane_1",
  profiles: [
    {
      name: "coder",
      worker_runtime: "hermes",
      default_model: "gpt-5.5",
      default_provider: "openai-codex",
      fallback_providers: [{ provider: "openai-codex", model: "gpt-5.5" }],
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
    { id: "claude-fable-5", label: "Claude Fable 5", runtime: "claude-cli", group: "Claude (Max-Abo)", provider: null, locked: true },
    { id: "claude-opus-4-8", label: "Claude Opus 4.8", runtime: "claude-cli", group: "Claude (Max-Abo)", provider: null, locked: true },
    { id: "gpt-5.5", label: "GPT-5.5", runtime: "hermes", group: "OpenAI Codex", provider: "openai-codex" },
    { id: "glm-5.2-fast", label: "GLM 5.2 Fast", runtime: "hermes", group: "Neuralwatt", provider: "neuralwatt" },
    { id: "qwen/qwen3.7-max", label: "Qwen 3.7 Max", runtime: "hermes", group: "OpenRouter", provider: "openrouter" },
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
        coder: {
          worker_runtime: "hermes",
          provider: "openrouter",
          model: "qwen/qwen3.7-max",
          fallback_providers: [{ provider: "openai-codex", model: "gpt-5.5" }],
        },
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
  onPersist: vi.fn(async () => {}),
  onCreate: vi.fn(),
  onDelete: vi.fn(),
  onImportOpenRouterModels: vi.fn(async () => ({
    admitted: ["xiaomi/mimo-v2.5"],
    configured: ["xiaomi/mimo-v2.5"],
    results: [{ id: "xiaomi/mimo-v2.5", status: "admitted" as const, reason: "Smoke ok; added to config" }],
  })),
  onRunAuthSmoke: vi.fn(async () => ({ ok: true, lane_id: "lane_1", source: "lanes-auth-smoke" as const, results: [] })),
};

describe("LanesEditor (routing cards)", () => {
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

  it("rendert mobile Role-Cards mit Provider zuerst und Modell-Suche", () => {
    const html = renderToStaticMarkup(
      <LanesEditor data={fixture} lane={fixture.lanes[0]} busy={false} actions={noopActions} />,
    );
    expect(html).toContain("Lane-Zustand");
    expect(html).toContain("api-standard");
    expect(html).toContain("1 Override");
    // Rollen aus dem Katalog + nicht-technische Hinweise
    expect(html).toContain("Schreibt Code");
    expect(html).toContain("Schwere Spezialfälle");
    // Provider-first + model datalist
    expect(html).toContain("OpenRouter");
    expect(html).toContain("OpenAI Codex");
    expect(html).toContain("Qwen 3.7 Max");
    expect(html).toContain("OpenRouter-IDs");
    expect(html).toContain("Smoken &amp; aufnehmen");
    expect(html).toContain("Standard (Claude Fable 5)");
    expect(html).toContain("list=\"lane-model-modell-f-r-coder\"");
  });

  it("zeigt pro Rolle einen Worker-Check-Button", () => {
    const html = renderToStaticMarkup(
      <LanesEditor data={fixture} lane={fixture.lanes[0]} busy={false} actions={noopActions} />,
    );
    expect(html.match(/Worker-Check/g)).toHaveLength(2);
  });

  it("zeigt manuellen Auth-Check mit Live-Status und Reasoning", () => {
    const authSmokeResults: LaneAuthSmokeResult[] = [
      {
        role: "coder",
        profile: "coder",
        runtime: "hermes",
        requested_provider: "openrouter",
        requested_model: "qwen/qwen3.7-max",
        observed_provider: "openrouter",
        observed_model: "qwen/qwen3.7-max",
        response_exact: true,
        fallback_activated: false,
        auth_ok: true,
        status: "ok",
        reason: "requested openrouter/qwen/qwen3.7-max; observed openrouter/qwen/qwen3.7-max; exact response",
      },
    ];
    const html = renderToStaticMarkup(
      <LanesEditor data={fixture} lane={fixture.lanes[0]} busy={false} actions={noopActions} initialAuthSmokeResults={authSmokeResults} />,
    );

    expect(html).toContain("Auth prüfen");
    expect(html).toContain("Live OK");
    expect(html).toContain("Antwort exakt");
    expect(html).toContain("requested openrouter/qwen/qwen3.7-max");
    expect(html).toContain("observed openrouter/qwen/qwen3.7-max");
  });

  it("macht Auth-Smoke bei ungespeicherten Änderungen ehrlich", () => {
    expect(authSmokeButtonLabel(false, false)).toBe("Auth prüfen");
    expect(authSmokeButtonLabel(false, true)).toBe("Auth prüft...");
    expect(authSmokeButtonLabel(true, false)).toBe("Gespeicherte Lane prüfen");
    expect(authSmokeDisabled({ busy: false, running: false, hasLaneId: true, dirty: true })).toBe(true);
  });

  it("blendet alte Auth-Smoke-Ergebnisse waehrend neuem Lauf oder Fehler aus", () => {
    const oldResults: LaneAuthSmokeResult[] = [
      {
        role: "coder",
        profile: "coder",
        runtime: "hermes",
        requested_provider: "neuralwatt",
        requested_model: "glm-5.2-fast",
        observed_provider: "neuralwatt",
        observed_model: "glm-5.2-fast",
        response_exact: true,
        fallback_activated: false,
        auth_ok: true,
        status: "ok",
      },
    ];

    expect(authSmokeRenderableResults(oldResults, { running: true, error: null })).toEqual([]);
    expect(authSmokeRenderableResults(oldResults, { running: false, error: "network failed" })).toEqual([]);
    expect(authSmokeRenderableResults(oldResults, { running: false, error: null })).toEqual(oldResults);
  });

  it("markiert blockierende Auth-Smoke-Status rot", () => {
    expect(laneAuthSmokeTone("auth_error")).toBe("red");
    expect(laneAuthSmokeTone("quota_or_rate_limit")).toBe("red");
    expect(laneAuthSmokeTone("timeout")).toBe("red");
    expect(laneAuthSmokeTone("config_error")).toBe("red");
    expect(laneAuthSmokeTone("error")).toBe("red");
  });

  it("zeigt Auth-Smoke als Operator-Entscheidung mit Scope und Fallback-Badge", () => {
    const authSmokeResults: LaneAuthSmokeResult[] = [
      {
        role: "coder",
        profile: "coder",
        runtime: "hermes",
        requested_provider: "neuralwatt",
        requested_model: "glm-5.2-fast",
        observed_provider: "neuralwatt",
        observed_model: "glm-5.2-fast",
        response_exact: true,
        fallback_activated: false,
        auth_ok: true,
        status: "ok",
        reason: "requested neuralwatt/glm-5.2-fast; observed neuralwatt/glm-5.2-fast; exact response",
      },
      {
        role: "research",
        profile: "research",
        runtime: "hermes",
        requested_provider: "gemini",
        requested_model: "gemini-3.5-flash",
        observed_provider: "openai-codex",
        observed_model: "gpt-5.4",
        response_exact: true,
        fallback_activated: true,
        auth_ok: false,
        status: "quota_or_rate_limit",
        error_class: "quota_or_rate_limit",
        reason: "requested gemini/gemini-3.5-flash; observed openai-codex/gpt-5.4; exact response; fallback activated; error_class=quota_or_rate_limit",
      },
      {
        role: "premium",
        profile: "premium",
        runtime: "claude-cli",
        requested_provider: "",
        requested_model: "claude-opus-4-8",
        response_exact: false,
        fallback_activated: false,
        auth_ok: false,
        status: "skipped",
        reason: "unsupported runtime for auth smoke",
      },
    ];
    const summary: LaneAuthSmokeSummary = {
      decision: "blocked",
      safe_to_activate: false,
      ok_count: 1,
      blocking_roles: ["research"],
      fallback_roles: ["research"],
      skipped_roles: ["premium"],
      checked_role_count: 3,
      total_role_count: 5,
      truncated: false,
      recommended_next_action: "Research zuerst reparieren oder bewusst auf ein funktionierendes Modell umstellen.",
    };
    const scope: LaneAuthSmokeScope = {
      requested_roles: [],
      checked_role_count: 3,
      total_role_count: 5,
      truncated: false,
      role_limit: 12,
    };
    const html = renderToStaticMarkup(
      <LanesEditor
        data={fixture}
        lane={fixture.lanes[0]}
        busy={false}
        actions={noopActions}
        initialAuthSmokeResults={authSmokeResults}
        initialAuthSmokeSummary={summary}
        initialAuthSmokeScope={scope}
      />,
    );

    expect(html).toContain("Lane blockiert");
    expect(html).toContain("1 OK");
    expect(html).toContain("1 blockiert");
    expect(html).toContain("1 Fallback");
    expect(html).toContain("1 übersprungen");
    expect(html).toContain("2 nicht geprüft");
    expect(html).toContain("3/5 Rollen geprüft");
    expect(html).toContain("Fallback aktiv");
    expect(html).toContain("Exakte Antwort über Fallback");
    expect(html).not.toContain("premium</span><span class=\"block\">Antwort nicht exakt");
  });

  it("warnt bei identischem Primary/Fallback, sperrt den Speicher-Knopf aber nicht mehr", () => {
    const badLane = {
      ...fixture.lanes[1],
      active: false,
      profiles: {
        coder: {
          worker_runtime: "hermes" as const,
          provider: "openrouter",
          model: "qwen/qwen3.7-max",
          fallback_providers: [{ provider: "openrouter", model: "qwen/qwen3.7-max" }],
        },
      },
    };
    const html = renderToStaticMarkup(
      <LanesEditor data={fixture} lane={badLane} busy={false} actions={noopActions} />,
    );
    expect(html).toContain("Primary and fallback are identical.");
    expect(html).toMatch(/\b\d+ Hinweis(?:e)?\b/);
    expect(html).toContain("Dauerhaft speichern");
  });

  it("lässt den Speicher-Knopf trotz Fallback-fehlt-Warnung aktiv (critic/qwen-Default)", () => {
    const warningLane = {
      ...fixture.lanes[1],
      active: false,
      profiles: {
        coder: {
          worker_runtime: "hermes" as const,
          provider: "openrouter",
          model: "qwen/qwen3.7-max",
          fallback_providers: [],
        },
      },
    };
    const html = renderToStaticMarkup(
      <LanesEditor data={fixture} lane={warningLane} busy={false} actions={noopActions} />,
    );
    expect(html).toContain("Fallback fehlt.");
    expect(html).toMatch(/\b\d+ Hinweis(?:e)?\b/);
    expect(html).toContain("Dauerhaft speichern");
  });

  it("zeigt in der Standardansicht ein gruppiertes Dropdown pro Rolle", () => {
    const html = renderToStaticMarkup(
      <LanesEditor data={fixture} lane={fixture.lanes[0]} busy={false} actions={noopActions} />,
    );
    expect(html).toContain("Modelle pro Rolle");
    expect(html).toContain("Standard (Claude Fable 5)");
    expect(html).toContain("Standard (GPT-5.5)");
    expect(html).toContain("<optgroup");
    expect(html).toContain("Claude (Max-Abo)");
    expect(html).toContain("OpenAI Codex");
    expect(html).toContain("Neuralwatt");
    expect(html).toContain("GLM 5.2 Fast · Neuralwatt");
    expect(html).toContain('value="hermes|glm-5.2-fast"');
  });

  it("begrenzt Rollen-Selects auf Desktop-Breite statt volle Panelbreite (sparse-empty-chrome)", () => {
    const html = renderToStaticMarkup(
      <LanesEditor data={fixture} lane={fixture.lanes[0]} busy={false} actions={noopActions} />,
    );
    // sm:max-w-[28rem] auf CONTROL_CLASS — greift ab Desktop, Mobile bleibt w-full.
    expect(html).toContain("sm:max-w-[28rem]");
    expect(html).toContain("w-full rounded-card border border-line bg-surface-2");
  });

  it("erklärt im Standard-Modus aktiven Lane-Override getrennt von dauerhaftem Profil-Default", () => {
    const html = renderToStaticMarkup(
      <LanesEditor data={fixture} lane={fixture.lanes[0]} busy={false} actions={noopActions} />,
    );
    expect(html).toContain("Aktiv in Lane: OpenRouter / Qwen 3.7 Max");
    expect(html).toContain("Dauerhafte Profil-Konfiguration: OpenAI Codex / GPT-5.5");
  });

  it("bietet einen Erweitert-Collapse für Provider/Fallbacks/Presets", () => {
    const html = renderToStaticMarkup(
      <LanesEditor data={fixture} lane={fixture.lanes[0]} busy={false} actions={noopActions} />,
    );
    expect(html).toContain("Erweitert");
    expect(html).toContain("Rollen &amp; Modelle");
    expect(html).toContain("Fallbacks");
    expect(html).toContain("OpenRouter-IDs");
  });

  it("zeigt im Standard-Modus den primären 'Dauerhaft speichern'-Knopf", () => {
    const html = renderToStaticMarkup(
      <LanesEditor data={fixture} lane={fixture.lanes[1]} busy={false} actions={noopActions} />,
    );
    expect(html).toContain("Dauerhaft speichern");
    // The primary save button is in the simple-view panel and must be enabled.
    const simplePanel = (html.split("Modelle pro Rolle")[1] ?? "").split("Erweitert")[0] ?? "";
    expect(simplePanel).toContain("Dauerhaft speichern");
    expect(simplePanel).not.toContain('disabled=""');
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
    // coder healthy → gemeinsames OK-Signal; premium unhealthy → Warn-Signal + Hinweis
    expect(html).toContain("bg-status-ok");
    expect(html).toContain("bg-status-warn");
    expect(html).toContain("Spawn-Bereitschaft: bereit");
    expect(html).toContain("Spawn-Bereitschaft: gestört");
  });

  it("zeigt Override-Chip mit Modell-Label bzw. Standard-Chip pro Rolle", () => {
    const html = renderToStaticMarkup(
      <LanesEditor data={fixture} lane={fixture.lanes[0]} busy={false} actions={noopActions} />,
    );
    // coder hat einen Lane-Eintrag mit Provider/Model/Fallback → aktiver Override
    expect(html).toContain("Lane-Override");
    expect(html).toContain("OpenRouter / Qwen 3.7 Max");
    // premium ohne Lane-Eintrag → expliziter Standard-Zustand
    expect(html).toContain("Profil-Default");
  });

  it("markiert OpenRouter-Rollen sichtbar als metered (Provider sonst unsichtbar)", () => {
    const html = renderToStaticMarkup(
      <LanesEditor data={fixture} lane={fixture.lanes[0]} busy={false} actions={noopActions} />,
    );
    // coder läuft in lane_1 über openrouter → metered-Markierung sichtbar
    expect(html).toContain("OpenRouter (metered)");
  });

  it("zeigt keine metered-Markierung, wenn keine Rolle über OpenRouter läuft", () => {
    const html = renderToStaticMarkup(
      <LanesEditor data={fixture} lane={fixture.lanes[1]} busy={false} actions={noopActions} />,
    );
    // lane_2 (max-abo): coder fällt auf openai-codex-Default, premium auf claude-cli
    expect(html).not.toContain("OpenRouter (metered)");
  });

  it("zeigt Claude-Max-Routing fuer normale Rollen klar und nicht als OpenRouter", () => {
    const cloudMaxData: LanesResponse = {
      ...fixture,
      lanes: [{
        ...fixture.lanes[0],
        profiles: {
          coder: { worker_runtime: "claude-cli", provider: null, model: "claude-opus-4-8", fallback_providers: [] },
        },
      }],
    };

    const html = renderToStaticMarkup(
      <LanesEditor data={cloudMaxData} lane={cloudMaxData.lanes[0]} busy={false} actions={noopActions} />,
    );

    expect(html).toContain("Claude Max / claude -p");
    expect(html).toContain("Aktiv in Lane: Claude Opus 4.8");
    expect(html).toMatch(/Provider [^"]*coder" disabled=""/);
    expect(html).not.toContain("OpenRouter (metered)");
    expect(html).not.toContain("Fallback fehlt");
    expect(html).not.toContain("Fallback hinzuf");
  });

  it("zeigt Fallback-Kette, Claude-Max-Badge und Preview-only Config", () => {
    const html = renderToStaticMarkup(
      <LanesEditor data={fixture} lane={fixture.lanes[0]} busy={false} actions={noopActions} />,
    );
    expect(html).toContain("Fallbacks");
    expect(html).toContain("Sicheren Fallback hinzuf");
    expect(html).toContain("Claude Max / claude -p");
    expect(html).toContain("Dauerhaft setzen (Preview)");
    expect(html).toContain("Preview · würde ändern");
    expect(html).toContain("fallback_providers:");
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
        default_provider: "openai-codex",
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
    // Shared status signals render in both the simple view and the advanced collapse.
    expect((html.match(/bg-status-ok/g) ?? []).length).toBeGreaterThanOrEqual(8);
    expect((html.match(/bg-status-warn/g) ?? []).length).toBeGreaterThanOrEqual(8);
    expect((html.match(/bg-ink-3/g) ?? []).length).toBeGreaterThanOrEqual(8);
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

  it.each([
    { name: "aktive Lane mit Standard-, Override-, Metered- und Fallback-Zweigen", lane: fixture.lanes[0], pendingDelete: null },
    { name: "inaktive Lane mit Claude-Lock und Lösch-Bestätigung", lane: fixture.lanes[1], pendingDelete: "lane_2" },
  ])("rendert $name ohne Legacy-Vokabular oder untergroße Ziele", ({ lane, pendingDelete }) => {
    const html = renderToStaticMarkup(
      <LanesEditor
        data={fixture}
        lane={lane}
        busy={false}
        actions={noopActions}
        initialPendingDelete={pendingDelete}
      />,
    );

    for (const legacy of [
      "cyan-", "emerald-", "sky-", "teal-", "zinc-", "slate-", "indigo-",
      "amber-", "red-", "rose-", "violet-", "white/", "black/", "StatusPill", "ToneCallout",
    ]) {
      expect(html).not.toContain(legacy);
    }
    expect(html).not.toContain("min-h-11");
    expect(html).not.toContain("sm:min-h-0");
    expect(html).not.toContain("sm:min-h-9");
    expect(html).toContain("min-h-12");
  });

  it("hält die Lanes-Quelle frei von lokaler Legacy- und Status-Komponenten-Vokabel", () => {
    const source = readFileSync(new URL("./LanesView.tsx", import.meta.url), "utf8");
    for (const legacy of [
      "hc-", "cyan-", "emerald-", "sky-", "teal-", "zinc-", "slate-", "indigo-",
      "amber-", "red-", "rose-", "violet-", "white/", "black/", "StatusPill", "ToneCallout", "toneClasses",
    ]) {
      expect(source).not.toContain(legacy);
    }
    expect(source).toContain("size-12");
  });
});
