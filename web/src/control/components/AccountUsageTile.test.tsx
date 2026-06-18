import { renderToStaticMarkup } from "react-dom/server";
import { describe, expect, it } from "vitest";
import { AccountUsageTile } from "./AccountUsageTile";
import type { AccountUsageResponse } from "../lib/types";

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
    expect(html).toContain("⚠ Engpass");
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

    // Nebenfenster + Extra-Usage im Details-Collapse
    expect(html).toContain("Details");
    expect(html).toContain("Sonnet-Woche");
    expect(html).toContain("Extra usage: 417.00 / 2500.00 EUR");

    // Kimi = lokale Schätzung → Fußzeile, nie als hartes Limit (§8)
    expect(html).toContain("Ohne Fenster-Limit");
    expect(html).toContain("Kimi");
    expect(html).toContain("Kimi 7d tokens: 1.0M across 12 runs");
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
