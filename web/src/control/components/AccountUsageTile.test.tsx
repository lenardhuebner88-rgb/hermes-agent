import { renderToStaticMarkup } from "react-dom/server";
import { describe, expect, it } from "vitest";
import { AccountUsageTile } from "./AccountUsageTile";
import type { AccountUsageResponse } from "../lib/types";
import { DEFAULT_STATS_CONFIG, type StatsFieldConfig } from "../lib/statsFields";

const usage: AccountUsageResponse = {
  cache_ttl_seconds: 60,
  providers: [
    {
      provider: "anthropic",
      available: true,
      source: "oauth",
      fetched_at: "2026-01-01T00:00:00+00:00",
      title: "Account limits",
      plan: null,
      cached: false,
      unavailable_reason: null,
      windows: [
        { label: "Current session", window_key: "session", used_percent: 24, reset_at: null, detail: null },
        { label: "Current week", window_key: "weekly", used_percent: 62, reset_at: null, detail: null },
        { label: "Sonnet week", window_key: "sonnet_week", used_percent: 17, reset_at: null, detail: null },
      ],
      details: ["Extra usage: 417.00 / 2500.00 EUR"],
    },
    {
      provider: "openai-codex",
      available: true,
      source: "usage_api",
      fetched_at: "2026-01-01T00:00:00+00:00",
      title: "Account limits",
      plan: "Pro",
      cached: false,
      unavailable_reason: null,
      windows: [
        { label: "Session", window_key: "session", used_percent: 0, reset_at: null, detail: null },
        { label: "Weekly", window_key: "weekly", used_percent: 96, reset_at: null, detail: null },
      ],
      details: [],
    },
    {
      provider: "kimi",
      available: true,
      source: "usage_api",
      fetched_at: "2026-01-01T00:00:00+00:00",
      title: "Kimi subscription tokens",
      plan: null,
      cached: false,
      unavailable_reason: null,
      windows: [
        { label: "Kimi 7d", window_key: "weekly", used_percent: 5, reset_at: null, detail: "1.0M / 20.0M tokens" },
      ],
      details: ["Kimi 7d tokens: 1.0M across 12 runs"],
    },
  ],
};

describe("AccountUsageTile", () => {
  it("rendert das Abo-Cockpit (session+weekly) mit Engpass, deutschen Labels und Fußzeile", () => {
    const html = renderToStaticMarkup(<AccountUsageTile usage={usage} loading={false} error={null} />);

    // Header + Engpass (knappstes echtes Fenster = ChatGPT-Woche 96 %, rot → ⚠)
    expect(html).toContain("Abo-Limits");
    expect(html).toContain("Engpass:");
    expect(html).toContain("lucide-triangle-alert");
    expect(html).toContain("ChatGPT / Codex");
    expect(html).toContain("96 %");

    // Karten mit deutschen Fenster-Labels + Werten
    expect(html).toContain("Claude");
    expect(html).toContain("5-Std-Fenster");
    expect(html).toContain("Diese Woche");
    expect(html).toContain("24 %");
    expect(html).toContain("62 %");
    expect(html).toContain("Pro");

    // Gauge-Semantik (a11y): jede Fenster-Zeile ist ein role="meter" mit dem
    // Prozentwert im Accessible Name — sonst kündigt der Screenreader nichts an.
    expect(html).toContain('role="meter"');
    expect(html).toContain('aria-label="5-Std-Fenster: 24 % genutzt"');
    expect(html).toContain('aria-label="Diese Woche: 62 % genutzt"');
    expect(html).toContain("grid-cols-[minmax(0,1fr)_auto]");
    expect(html).toContain("sm:grid-cols-[7rem_minmax(0,1fr)_auto]");
    expect(html).toContain("col-span-2 sm:order-2 sm:col-span-1");

    // Nebenfenster + Extra-Usage im Details-Collapse
    expect(html).toContain("Details");
    expect(html).toContain("Sonnet-Woche");
    expect(html).toContain("Extra usage: 417.00 / 2500.00 EUR");

    // Alle Abos werden als gleichwertige Karten dargestellt; Kimi kommt aus der
    // Provider-API und bleibt nicht in einer Strichel-Fußzeile hängen.
    expect(html).toContain("Kimi");
    // Kimi trägt hier ein weekly-Fenster (5 %) → wird als Karte gerendert.
    expect(html).toContain("5 %");
    // Keine zusammengeworfene "Ohne Fenster-Limit"-Geisterkarte, solange nur Abos da sind.
    expect(html).not.toContain("Ohne Fenster-Limit");
  });

  it("rendert ALLE session/weekly-Fenster als Primärbalken — inkl. modell-spezifischem Fable-Limit (Operator-Spec: 3 Balken)", () => {
    // Exakt die Fenster, die die Live-Anthropic-API (Max 20×) liefert:
    // 5h-Session, Woche, plus das modell-spezifische Wochenlimit (detail "Fable").
    const u: AccountUsageResponse = {
      cache_ttl_seconds: 60,
      providers: [
        {
          provider: "anthropic",
          available: true,
          source: "oauth_usage_api",
          fetched_at: "2026-01-01T00:00:00+00:00",
          title: "Account limits",
          plan: "Max 20×",
          cached: false,
          unavailable_reason: null,
          windows: [
            { label: "Current session", window_key: "session", used_percent: 5, reset_at: "2026-07-21T18:59:59+00:00", detail: null },
            { label: "Current week", window_key: "weekly", used_percent: 89, reset_at: "2026-07-24T03:59:59+00:00", detail: null },
            { label: "Modell-Limit", window_key: "scoped_week", used_percent: 99, reset_at: "2026-07-24T03:59:59+00:00", detail: "Fable" },
          ],
          details: [],
        },
      ],
    };
    const html = renderToStaticMarkup(
      <AccountUsageTile usage={u} loading={false} error={null} config={DEFAULT_STATS_CONFIG} />,
    );

    // Drei Primärbalken (role="meter") statt zweier — das Fable-Limit ist kein
    // Details-Collapse-Eintrag, sondern ein eigener Balken mit Modellnamen im Label.
    expect(html).toContain('aria-label="5-Std-Fenster: 5 % genutzt"');
    expect(html).toContain('aria-label="Diese Woche: 89 % genutzt"');
    expect(html).toContain('aria-label="Modell-Limit · Fable: 99 % genutzt"');
    expect(html).toContain('>Modell-Limit · Fable</span>');
    expect((html.match(/>Fable<\/span>/g) ?? []).length).toBe(0);
    expect(html).toContain("99 %");
    // Keine Nebenfenster (alles session/weekly) → kein Details-Collapse für diesen Provider.
    expect(html).not.toContain("<details");
    // Exakt drei Primärbalken — Regressionstest gegen ein 4. spurioses Meter oder
    // ein im Collapse verlorenes Fenster (Codex-Review-Fund #3).
    expect((html.match(/role="meter"/g) ?? []).length).toBe(3);
    // Das knappe Fable-Fenster (99 %) treibt den Engpass (alert) → Footer benennt es.
    expect(html).toContain("Engpass:");
  });

  it("sortiert Primärbalken session-vor-weekly auch bei umgekehrter Backend-Reihenfolge (Codex-Review-Fund #1)", () => {
    // Backend-Reihenfolge absichtlich verdreht (weekly, session, scoped) — wie Kimi
    // sie zeitweise liefert. Der alte `.find`-Code erzwang session→weekly; das neue
    // `.filter` muss das via stabilem Sort erhalten, sonst kippt die Balkenfolge.
    const u: AccountUsageResponse = {
      cache_ttl_seconds: 60,
      providers: [
        {
          provider: "anthropic",
          available: true,
          source: "oauth",
          fetched_at: "2026-01-01T00:00:00+00:00",
          title: "Account limits",
          plan: null,
          cached: false,
          unavailable_reason: null,
          windows: [
            { label: "Current week", window_key: "weekly", used_percent: 60, reset_at: null, detail: null },
            { label: "Current session", window_key: "session", used_percent: 10, reset_at: null, detail: null },
            { label: "Modell-Limit", window_key: "scoped_week", used_percent: 80, reset_at: null, detail: "Fable" },
          ],
          details: [],
        },
      ],
    };
    const html = renderToStaticMarkup(<AccountUsageTile usage={u} loading={false} error={null} config={DEFAULT_STATS_CONFIG} />);
    const iS = html.indexOf('aria-label="5-Std-Fenster: 10 % genutzt"');
    const iW = html.indexOf('aria-label="Diese Woche: 60 % genutzt"');
    const iF = html.indexOf('aria-label="Modell-Limit · Fable: 80 % genutzt"');
    expect(iS).toBeGreaterThan(-1);
    expect(iS).toBeLessThan(iW); // session vor weekly — trotz umgekehrter Backend-Reihenfolge
    expect(iW).toBeLessThan(iF); // weekly vor scoped — Backend-Reihenfolge innerhalb non-session erhalten
  });

  it("bricht window.detail NIE ab: eigene volle-Breite-Zeile, Label-Spalte bleibt kurz (Operator-Bug: 24/100 nicht weg)", () => {
    // Kimi-artiges Fenster: langes detail (verbleibende Tokens). Inline an der
    // 7rem-Spalte würde es auf Desktop zu "24/…" kürzen → eigener Sub-Line-Test.
    const u: AccountUsageResponse = {
      cache_ttl_seconds: 60,
      providers: [
        {
          provider: "kimi",
          available: true,
          source: "kanban_subscription_tokens",
          fetched_at: "2026-01-01T00:00:00+00:00",
          title: "Kimi subscription tokens",
          plan: null,
          cached: false,
          unavailable_reason: null,
          windows: [
            { label: "Kimi 7d", window_key: "weekly", used_percent: 76, reset_at: null, detail: "24/100 verbleibend" },
          ],
          details: [],
        },
      ],
    };
    const html = renderToStaticMarkup(<AccountUsageTile usage={u} loading={false} error={null} config={DEFAULT_STATS_CONFIG} />);
    // detail landet in einer dedizierten Zeile (nicht inline am kurzen Label) …
    expect(html).toContain("js-window-detail");
    expect(html).toContain("24/100 verbleibend");
    // … und die sichtbare Label-Spalte trägt NUR den Fensternamen (nicht die gekürzte Kombi).
    expect(html).toContain(">Diese Woche</span>");
    expect(html).not.toContain(">Diese Woche · 24/100 verbleibend<");
    // Der Accessible-Name behält den Qualifier (a11y).
    expect(html).toContain('aria-label="Diese Woche · 24/100 verbleibend: 76 % genutzt"');
  });

  it("gibt Kimi ohne geliefertes Fenster eine ehrliche gleichwertige Abo-Karte", () => {
    const u: AccountUsageResponse = {
      cache_ttl_seconds: 60,
      providers: [
        {
          provider: "kimi",
          available: true,
          source: "kanban_subscription_tokens",
          fetched_at: "2026-01-01T00:00:00+00:00",
          title: "Kimi subscription tokens",
          plan: null,
          cached: false,
          unavailable_reason: null,
          windows: [],
          details: [],
        },
      ],
    };
    const html = renderToStaticMarkup(<AccountUsageTile usage={u} loading={false} error={null} />);
    expect(html).toContain("Kimi");
    expect(html).toContain("keine Fensterdaten vom Provider");
    expect(html).not.toContain("Ohne Fenster-Limit");
  });

  it("rendert OpenRouter (kein Abo-Fenster, $-Guthaben) als eigene Ausgaben-Karte im Cockpit", () => {
    const u: AccountUsageResponse = {
      cache_ttl_seconds: 60,
      providers: [
        {
          provider: "openrouter",
          available: true,
          source: "credits",
          fetched_at: "2026-01-01T00:00:00+00:00",
          title: "OpenRouter",
          plan: null,
          cached: false,
          unavailable_reason: null,
          windows: [],
          details: ["Credits balance: $0.00", "API key usage: $173.38 total • $5.08 this month"],
        },
      ],
    };
    const html = renderToStaticMarkup(<AccountUsageTile usage={u} loading={false} error={null} />);
    // Eigene Karte (article) im Cockpit, $-Details als Körper — nicht in der Fußzeile.
    expect(html).toContain("OpenRouter");
    expect(html).toContain("Credits balance: $0.00");
    expect(html).toContain("API key usage: $173.38 total • $5.08 this month");
    expect(html).not.toContain("Ohne Fenster-Limit");
    expect(html).not.toContain("keine Fensterdaten vom Provider");
  });

  it("hält einen offline/leeren Nicht-Abo-Provider weiterhin in der Fußzeile", () => {
    const u: AccountUsageResponse = {
      cache_ttl_seconds: 60,
      providers: [
        {
          provider: "openrouter",
          available: false,
          source: "credits",
          fetched_at: "2026-01-01T00:00:00+00:00",
          title: "OpenRouter",
          plan: null,
          cached: false,
          unavailable_reason: "kein API-Key",
          windows: [],
          details: [],
        },
      ],
    };
    const html = renderToStaticMarkup(<AccountUsageTile usage={u} loading={false} error={null} />);
    expect(html).toContain("Ohne Fenster-Limit");
    expect(html).toContain("kein API-Key");
  });

  it("paart das Provider-% mit dem Worker-Run-Abgleich, wenn laneUsage durchgereicht wird", () => {
    const html = renderToStaticMarkup(
      <AccountUsageTile
        usage={usage}
        loading={false}
        error={null}
        laneUsage={{ chatgpt: { tokens: 60_500_000, runs: 281 } }}
      />,
    );
    expect(html).toContain("Abgleich");
    expect(html).toContain("281 Runs");
  });

  it("haelt den Cache/Live-Status in einer eigenen Header-Spalte", () => {
    const html = renderToStaticMarkup(<AccountUsageTile usage={usage} loading={false} error={null} />);

    expect(html).toContain("grid-cols-[minmax(0,1fr)_auto]");
    expect(html).toContain("min-w-0");
    expect(html).toContain("justify-self-end");
  });

  it("zeigt ein kompaktes Ladegerüst, bevor usePolling Daten liefert", () => {
    const html = renderToStaticMarkup(<AccountUsageTile usage={null} loading error={null} />);

    expect(html).toContain("Abo-Limits");
    expect(html).toContain("lädt");
  });

  it("meldet fehlende Abo-Daten fail-soft", () => {
    const empty: AccountUsageResponse = { cache_ttl_seconds: 60, providers: [] };
    const html = renderToStaticMarkup(<AccountUsageTile usage={empty} loading={false} error={null} />);
    expect(html).toContain("Limit unbekannt");
  });

  it("custom config relabels providers without a code change (AC-2)", () => {
    const cfg: StatsFieldConfig = {
      ...DEFAULT_STATS_CONFIG,
      providers: DEFAULT_STATS_CONFIG.providers.map((p) =>
        p.id === "anthropic" ? { ...p, label: "Claude (Custom-Label)" } : p,
      ),
    };
    const html = renderToStaticMarkup(<AccountUsageTile usage={usage} loading={false} error={null} config={cfg} />);

    expect(html).toContain("Claude (Custom-Label)");
    expect(html).not.toContain(">Claude<");
  });

  it("config visibility prunes a provider card → fewer DOM nodes (AC-3 measurable declutter)", () => {
    const tagCount = (html: string) => (html.match(/<[a-zA-Z]/g) ?? []).length;

    const full = renderToStaticMarkup(
      <AccountUsageTile usage={usage} loading={false} error={null} config={DEFAULT_STATS_CONFIG} />,
    );
    const hidden: StatsFieldConfig = {
      ...DEFAULT_STATS_CONFIG,
      providers: DEFAULT_STATS_CONFIG.providers.map((p) =>
        p.id === "anthropic" ? { ...p, visible: false } : p,
      ),
    };
    const lean = renderToStaticMarkup(
      <AccountUsageTile usage={usage} loading={false} error={null} config={hidden} />,
    );

    expect(full).toContain("Claude");
    expect(lean).not.toContain("Claude");
    expect(tagCount(lean)).toBeLessThan(tagCount(full));
  });

  it("kennzeichnet ein Fenster ohne Prozentwert als unbekanntes Limit (Gauge bleibt zugänglich)", () => {
    const u: AccountUsageResponse = {
      cache_ttl_seconds: 60,
      providers: [
        {
          provider: "anthropic",
          available: true,
          source: "oauth",
          fetched_at: "2026-01-01T00:00:00+00:00",
          title: "Account limits",
          plan: "Max",
          cached: false,
          unavailable_reason: null,
          windows: [
            { label: "5h", window_key: "session", used_percent: 82, reset_at: null, detail: null },
            { label: "Weekly", window_key: "weekly", used_percent: null, reset_at: null, detail: null },
          ],
          details: [],
        },
      ],
    };
    const html = renderToStaticMarkup(<AccountUsageTile usage={u} loading={false} error={null} />);
    expect(html).toContain('aria-label="5-Std-Fenster: 82 % genutzt"');
    expect(html).toContain('aria-label="Diese Woche: unbekannt"');
    expect(html).toContain('aria-valuetext="unbekannt"');
  });

  it("rendert xai mit weekly-Fenster als Grok-Abo-Karte (lane:null, Fenster zählt)", () => {
    // REAL payload shape from live /api/account-usage (SuperGrok weekly ~21 %).
    const cfg: StatsFieldConfig = {
      ...DEFAULT_STATS_CONFIG,
      providers: [
        ...DEFAULT_STATS_CONFIG.providers.filter((p) => p.id !== "xai"),
        { id: "xai", label: "Grok", lane: null, visible: true },
      ],
    };
    const u: AccountUsageResponse = {
      cache_ttl_seconds: 60,
      providers: [
        {
          provider: "xai",
          available: true,
          source: "billing_api",
          fetched_at: "2026-07-16T09:47:00+00:00",
          title: "Grok usage",
          plan: "SuperGrok",
          cached: false,
          unavailable_reason: null,
          windows: [
            {
              label: "Diese Woche",
              window_key: "weekly",
              used_percent: 21,
              reset_at: "2026-07-21T00:00:00+00:00",
              detail: null,
            },
          ],
          details: ["Stand: 2026-07-16 11:47 CEST"],
        },
      ],
    };
    const html = renderToStaticMarkup(
      <AccountUsageTile usage={u} loading={false} error={null} config={cfg} />,
    );
    // Card, not footer: article titled Grok with plan + weekly meter.
    expect(html).toContain("<article");
    expect(html).toContain("Grok");
    expect(html).toContain("SuperGrok");
    expect(html).toContain("Diese Woche");
    expect(html).toContain("21 %");
    expect(html).toContain('role="meter"');
    expect(html).toContain('aria-label="Diese Woche: 21 % genutzt"');
    expect(html).not.toContain("Ohne Fenster-Limit");
  });

  it("rendert openrouter (details only, lane null, no windows) als Ausgaben-Karte im Cockpit", () => {
    const cfg: StatsFieldConfig = {
      ...DEFAULT_STATS_CONFIG,
      providers: [
        ...DEFAULT_STATS_CONFIG.providers.filter((p) => p.id !== "openrouter"),
        { id: "openrouter", label: "OpenRouter", lane: null, visible: true },
      ],
    };
    const u: AccountUsageResponse = {
      cache_ttl_seconds: 60,
      providers: [
        {
          provider: "openrouter",
          available: true,
          source: "credits",
          fetched_at: "2026-07-16T09:47:00+00:00",
          title: "OpenRouter",
          plan: null,
          cached: false,
          unavailable_reason: null,
          windows: [],
          details: ["Guthaben: 8.50 USD"],
        },
      ],
    };
    const html = renderToStaticMarkup(
      <AccountUsageTile usage={u} loading={false} error={null} config={cfg} />,
    );
    expect(html).toContain("<article");
    expect(html).toContain("OpenRouter");
    expect(html).toContain("Guthaben: 8.50 USD");
    expect(html).not.toContain("Ohne Fenster-Limit");
  });

  it("hält Grok auch ohne Fenster als gleichwertige Abo-Karte sichtbar", () => {
    const cfg: StatsFieldConfig = {
      ...DEFAULT_STATS_CONFIG,
      providers: [
        ...DEFAULT_STATS_CONFIG.providers.filter((p) => p.id !== "xai"),
        { id: "xai", label: "Grok", lane: null, visible: true },
      ],
    };
    const u: AccountUsageResponse = {
      cache_ttl_seconds: 60,
      providers: [
        {
          provider: "xai",
          available: false,
          source: "billing_api",
          fetched_at: "2026-07-16T09:47:00+00:00",
          title: "Grok usage",
          plan: null,
          cached: false,
          unavailable_reason: "Grok OAuth nicht angemeldet",
          windows: [],
          details: [],
        },
      ],
    };
    const html = renderToStaticMarkup(
      <AccountUsageTile usage={u} loading={false} error={null} config={cfg} />,
    );
    expect(html).toContain("Grok");
    expect(html).toContain("Grok OAuth nicht angemeldet");
    expect(html).toContain("<article");
    expect(html).not.toContain("Ohne Fenster-Limit");
  });

  // REAL xai payload shape (SuperGrok weekly) + signal_at age chip semantics.
  function xaiPayload(signalAtIso: string, fetchedAtIso?: string): AccountUsageResponse {
    return {
      cache_ttl_seconds: 60,
      providers: [
        {
          provider: "xai",
          available: true,
          source: "billing_api",
          fetched_at: fetchedAtIso ?? new Date().toISOString(),
          signal_at: signalAtIso,
          title: "Grok usage",
          plan: "SuperGrok",
          cached: false,
          unavailable_reason: null,
          windows: [
            {
              label: "Diese Woche",
              window_key: "weekly",
              used_percent: 21,
              reset_at: "2026-07-21T00:00:00+00:00",
              detail: null,
            },
          ],
          details: ["Stand: 2026-07-16 11:47 CEST"],
        },
      ],
    };
  }

  const xaiCfg: StatsFieldConfig = {
    ...DEFAULT_STATS_CONFIG,
    providers: [
      ...DEFAULT_STATS_CONFIG.providers.filter((p) => p.id !== "xai"),
      { id: "xai", label: "Grok", lane: null, visible: true },
    ],
  };

  it("chip shows Stand 3d (warn) when signal_at is 3 days old", () => {
    // Pad past the exact 72h boundary: nowSec floors to whole seconds, so
    // Date.now()-3d can floor to 71h and render "Stand 2d".
    const signalAt = new Date(Date.now() - (3 * 24 + 1) * 60 * 60 * 1000).toISOString();
    const html = renderToStaticMarkup(
      <AccountUsageTile usage={xaiPayload(signalAt)} loading={false} error={null} config={xaiCfg} />,
    );
    expect(html).toContain("Stand 3d");
    // warn tone on the age chip (SignalChip CHIP.warn)
    expect(html).toMatch(/border-status-warn\/30[^"]*"[^>]*>[\s\S]*?Stand 3d/);
    expect(html).not.toContain(">Live</");
  });

  it("chip shows Live when signal_at is 2 minutes old", () => {
    const signalAt = new Date(Date.now() - 2 * 60 * 1000).toISOString();
    const html = renderToStaticMarkup(
      <AccountUsageTile usage={xaiPayload(signalAt)} loading={false} error={null} config={xaiCfg} />,
    );
    expect(html).toContain(">Live</");
    expect(html).not.toContain("Stand ");
  });

  it("chip shows Stand 5h (neutral) when signal_at is 5 hours old", () => {
    // Pad past exact 5h: nowSec floors Date.now(), so Date.now()-5h floors to 4h.
    const signalAt = new Date(Date.now() - (5 * 60 + 10) * 60 * 1000).toISOString();
    const html = renderToStaticMarkup(
      <AccountUsageTile usage={xaiPayload(signalAt)} loading={false} error={null} config={xaiCfg} />,
    );
    expect(html).toContain("Stand 5h");
    // neutral tone on the age chip (SignalChip CHIP.neutral)
    expect(html).toMatch(/border-line bg-surface-2 text-ink-2[^"]*"[^>]*>[\s\S]*?Stand 5h/);
    expect(html).not.toContain(">Live</");
  });

  it("rendert scoped_week (Modell-Limit) als Primärbalken mit Modellname im Label; detail=null ohne Trenner", () => {
    const u: AccountUsageResponse = {
      cache_ttl_seconds: 60,
      providers: [
        {
          provider: "anthropic",
          available: true,
          source: "oauth",
          fetched_at: "2026-01-01T00:00:00+00:00",
          title: "Account limits",
          plan: null,
          cached: false,
          unavailable_reason: null,
          windows: [
            { label: "Current session", window_key: "session", used_percent: 4, reset_at: null, detail: null },
            { label: "Current week", window_key: "weekly", used_percent: 82, reset_at: null, detail: null },
            { label: "Modell-Limit", window_key: "scoped_week", used_percent: 94, reset_at: null, detail: "Fable" },
            { label: "Modell-Limit", window_key: "scoped_week", used_percent: 50, reset_at: null, detail: null },
          ],
          details: [],
        },
      ],
    };
    const html = renderToStaticMarkup(<AccountUsageTile usage={u} loading={false} error={null} />);
    // Operator-Spec 2026-07-21: model-scoped caps sind Primärbalken (nicht Details-
    // Collapse) — Modellname im Label, beide scoped-Fenster gleichzeitig sichtbar.
    expect(html).toContain('aria-label="Modell-Limit · Fable: 94 % genutzt"');
    expect(html).toContain("94 %");
    // … und der detail=null-Pfad rendert ohne hängenden " · "-Trenner.
    expect(html).toContain('aria-label="Modell-Limit: 50 % genutzt"');
    expect(html).toContain("50 %");
    expect(html).not.toContain("Modell-Limit · :");
    // Alle vier Fenster sind Primärbalken → kein Details-Collapse.
    expect(html).not.toContain("<details");
  });
});
