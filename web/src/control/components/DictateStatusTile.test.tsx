import { renderToStaticMarkup } from "react-dom/server";
import { describe, expect, it } from "vitest";
import type { DictateStatusResponse } from "../lib/schemas";
import { DictateStatusTile } from "./DictateStatusTile";

const status: DictateStatusResponse = {
  schema: "hermes-dictate-status-v1",
  connected: true,
  last_contact_at: Math.floor(Date.now() / 1000),
  app_version: "1.0",
  engine: "on_device",
  language: "german",
  style: "formal",
  surface: "overlay",
  microphone_permission: true,
  service_enabled: true,
  last_error: null,
  dictations: 12,
  failures: 1,
  retries: 2,
  busy: 1,
  success_rate_percent: 92.3,
  latency_ms: 840,
  latency_p50_ms: 760,
  latency_p95_ms: 1150,
  apk: { name: "hermes-dictate.apk", url: "/api/artifacts/hermes-dictate.apk", size: 3, mtime: 1 },
};

describe("DictateStatusTile", () => {
  it("renders bounded operational metadata and the authenticated APK action", () => {
    const html = renderToStaticMarkup(<DictateStatusTile status={status} loading={false} error={null} />);

    expect(html).toContain("Hermes Diktat");
    expect(html).toContain("kein Audio, kein Transkript");
    expect(html).toContain("on device");
    expect(html).toContain("Deutsch");
    expect(html).toContain("formal");
    expect(html).toContain("840");
    expect(html).toContain("92.3");
    expect(html).toContain("Latenz p95");
    expect(html).toContain("APK laden");
    expect(html).toContain("min-h-12");
    expect(html).not.toContain("must never appear");
  });
});
