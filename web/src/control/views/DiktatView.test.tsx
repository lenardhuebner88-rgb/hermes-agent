import { renderToStaticMarkup } from "react-dom/server";
import { describe, expect, it } from "vitest";
import type { DictateStatusResponse } from "../lib/schemas";
import {
  DICTATE_ERROR_HELP,
  DiktatBody,
  apkVersion,
  dictateApks,
  fmtMegabytes,
  olderBuilds,
  updateHint,
} from "./DiktatView";

const status: DictateStatusResponse = {
  schema: "hermes-dictate-status-v1",
  connected: true,
  last_contact_at: Math.floor(Date.now() / 1000),
  app_version: "1.1",
  engine: "on_device",
  language: "german",
  style: "auto",
  surface: "ime",
  microphone_permission: true,
  service_enabled: true,
  last_error: "cloud_auth",
  dictations: 41,
  failures: 2,
  retries: 1,
  busy: 0,
  latency_ms: 900,
  success_rate_percent: 95.3,
  latency_p50_ms: 800,
  latency_p95_ms: 1900,
  apk: {
    name: "hermes-dictate-latest.apk",
    url: "/api/artifacts/hermes-dictate-latest.apk",
    size: 7_556_281,
    mtime: 1_784_206_400,
  },
};

const artifacts = [
  { name: "hermes-dictate-latest.apk", size: 7_556_281, mtime: 1_784_206_400 },
  // Byte-identical versioned twin of the latest alias — deduped from the history list.
  { name: "hermes-dictate-1.3-0322eed96.apk", size: 7_556_281, mtime: 1_784_206_399 },
  { name: "hermes-dictate-wispr-flow-87ed6d36b.apk", size: 7_464_121, mtime: 1_783_899_807 },
];

describe("dictateApks", () => {
  it("filters to dictate APKs and sorts newest first", () => {
    const mixed = [
      { name: "hermes-voice-latest.apk", size: 1, mtime: 9 },
      { name: "hermes-dictate-latest.apk.sha256", size: 1, mtime: 9 },
      { name: "hermes-dictate-old.apk", size: 1, mtime: 1 },
      { name: "hermes-dictate-new.apk", size: 1, mtime: 5 },
    ];
    expect(dictateApks(mixed).map((artifact) => artifact.name)).toEqual([
      "hermes-dictate-new.apk",
      "hermes-dictate-old.apk",
    ]);
  });
});

describe("fmtMegabytes", () => {
  it("renders one decimal MB", () => {
    expect(fmtMegabytes(7_556_281)).toBe("7.2 MB");
  });
});

describe("apkVersion / updateHint / olderBuilds", () => {
  it("parses versions from versioned names only", () => {
    expect(apkVersion("hermes-dictate-1.3-0322eed96.apk")).toBe("1.3");
    expect(apkVersion("hermes-dictate-latest.apk")).toBeNull();
    expect(apkVersion("hermes-dictate-wispr-flow-87ed6d36b.apk")).toBeNull();
  });

  it("hints only when a connected app reports an older version", () => {
    // The newest artifact is the unversioned -latest alias; the version must
    // come from the newest versioned twin behind it.
    expect(updateHint(status, artifacts)).toContain("aktuell ist 1.3");
    expect(updateHint({ ...status, app_version: "1.3" }, artifacts)).toBeNull();
    expect(updateHint({ ...status, connected: false }, artifacts)).toBeNull();
    expect(updateHint(null, artifacts)).toBeNull();
    expect(updateHint(status, [artifacts[0]])).toBeNull();
    expect(updateHint(status, null)).toBeNull();
  });

  it("drops byte-identical alias twins from the history", () => {
    expect(olderBuilds(artifacts).map((a) => a.name)).toEqual([
      "hermes-dictate-wispr-flow-87ed6d36b.apk",
    ]);
    expect(olderBuilds([])).toEqual([]);
  });
});

describe("DiktatBody", () => {
  const html = renderToStaticMarkup(
    <DiktatBody
      status={status}
      statusLoading={false}
      statusError={null}
      artifacts={artifacts}
      artifactsError={null}
      sha256={"5d6fcc1e04e2ab94ccc5f4ae981e4584b7653766994973c80ef365a87990fd05"}
    />,
  );

  it("shows header, download, sha and setup", () => {
    expect(html).toContain("Systemweites Diktat");
    expect(html).toContain("hermes-dictate-latest.apk");
    expect(html).toContain("APK laden");
    expect(html).toContain("5d6fcc1e04e2ab94ccc5f4ae981e4584");
    expect(html).toContain("Einrichtung in 6 Schritten");
    expect(html).toContain("Tastatur aktivieren");
    expect(html).toContain("Ältere Builds");
    expect(html).toContain("hermes-dictate-wispr-flow-87ed6d36b.apk");
    // The versioned twin of the latest alias is deduped from the history list.
    expect(html).not.toContain("hermes-dictate-1.3-0322eed96.apk");
  });

  it("flags an update when the connected app is older than the latest APK", () => {
    expect(html).toContain("Update verfügbar: App meldet 1.1, aktuell ist 1.3");
  });

  it("highlights the currently reported error class", () => {
    expect(html).toContain("zuletzt gemeldet:");
    expect(html).toContain(DICTATE_ERROR_HELP.cloud_auth.title);
    expect(html).toContain("border-status-alert/40");
  });

  it("never renders transcript or audio content surfaces", () => {
    expect(html).not.toContain("transcript");
    expect(html).not.toContain("Audioinhalt");
  });

  it("renders the empty state when no APK exists", () => {
    const empty = renderToStaticMarkup(
      <DiktatBody
        status={null}
        statusLoading={false}
        statusError={null}
        artifacts={[]}
        artifactsError={null}
        sha256={null}
      />,
    );
    expect(empty).toContain("Kein APK im Artefakt-Store");
    expect(empty).toContain("ohne Kontakt");
  });
});
