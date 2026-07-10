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
      source: "kanban_subscription_tokens",
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
  it("rendert das Zwei-Balken-Cockpit mit Engpass, deutschen Labels und Fußzeile", () => {
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

    // Operator-Direktive: alle drei Abos als gleichwertige Karten — Kimi ist jetzt
    // eine eigene Karte (kein Engpass, §8), nicht mehr in einer Strichel-Fußzeile.
    expect(html).toContain("Kimi");
    // Kimi trägt hier ein weekly-Fenster (5 %) → wird als Karte gerendert.
    expect(html).toContain("5 %");
    // Keine zusammengeworfene "Ohne Fenster-Limit"-Geisterkarte, solange nur Abos da sind.
    expect(html).not.toContain("Ohne Fenster-Limit");
  });

  it("gibt Kimi ohne Provider-Fenster eine ehrliche Leerzustand-Karte (Operator-Direktive: 3 gleichwertige Abos)", () => {
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

  it("hält OpenRouter (kein Abo-Fenster, $-Guthaben) in der ehrlich beschrifteten Fußzeile", () => {
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
          details: ["Guthaben: 12.00 USD"],
        },
      ],
    };
    const html = renderToStaticMarkup(<AccountUsageTile usage={u} loading={false} error={null} />);
    expect(html).toContain("Ohne Fenster-Limit");
    expect(html).toContain("OpenRouter");
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
});
