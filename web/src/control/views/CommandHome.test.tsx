import { renderToStaticMarkup } from "react-dom/server";
import { describe, expect, it } from "vitest";
import { AccountUsageTile } from "./CommandHome";
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
      plan: "Max",
      cached: false,
      unavailable_reason: null,
      windows: [
        { label: "5h", used_percent: 82.4, reset_at: "2026-01-01T05:00:00+00:00", detail: null },
        { label: "Weekly", used_percent: null, reset_at: null, detail: "Limit unbekannt" },
      ],
      details: ["Resets rollierend"],
    },
    {
      provider: "openai-codex",
      available: false,
      source: null,
      fetched_at: "2026-01-01T00:00:00+00:00",
      title: "Account limits",
      plan: null,
      cached: false,
      unavailable_reason: "usage_unavailable",
      windows: [],
      details: [],
    },
  ],
};

describe("AccountUsageTile", () => {
  it("rendert Gauges, Details und unbekannte Limits fail-soft", () => {
    const html = renderToStaticMarkup(<AccountUsageTile usage={usage} loading={false} error={null} />);

    expect(html).toContain("Abo-Limits");
    expect(html).toContain("anthropic");
    expect(html).toContain("Max");
    expect(html).toContain("82% genutzt");
    expect(html).toContain("18% frei");
    expect(html).toContain("Limit unbekannt");
    expect(html).toContain("Resets rollierend");
    expect(html).toContain("openai-codex");
    expect(html).toContain("Nicht verfügbar");
  });

  it("zeigt ein kompaktes Ladegerüst, bevor usePolling Daten liefert", () => {
    const html = renderToStaticMarkup(<AccountUsageTile usage={null} loading error={null} />);

    expect(html).toContain("Abo-Limits");
    expect(html).toContain("lädt");
  });
});
