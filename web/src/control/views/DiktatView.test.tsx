// @vitest-environment jsdom

import { renderToStaticMarkup } from "react-dom/server";
import { cleanup, fireEvent, render, screen, waitFor } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import type { DictateStatusResponse } from "../lib/schemas";
import {
  DICTATE_ERROR_HELP,
  DictionaryEditorPanel,
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

  it("compares versions numerically, not lexically", () => {
    const withLatest = (v: string) => [{ name: `hermes-dictate-${v}-abc.apk`, size: 1, mtime: 1 }];
    // "1.10" is NEWER than "1.9" — no hint despite string inequality.
    expect(updateHint({ ...status, app_version: "1.10" }, withLatest("1.9"))).toBeNull();
    expect(updateHint({ ...status, app_version: "1.9" }, withLatest("1.10"))).toContain("1.10");
    // Longer-but-equal prefixes: 1.3 vs 1.3.1 is an update; 1.3.1 vs 1.3 is not.
    expect(updateHint({ ...status, app_version: "1.3" }, withLatest("1.3.1"))).toContain("1.3.1");
    expect(updateHint({ ...status, app_version: "1.3.1" }, withLatest("1.3"))).toBeNull();
    // Unparseable dev builds never trigger the hint.
    expect(updateHint({ ...status, app_version: "dev" }, withLatest("1.3"))).toBeNull();
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

  // Diktat Stufe 11: the top-level `status` fixture above deliberately omits
  // `history`/`today` (the pre-Stufe-11 shape) — proving the whole page still
  // renders fine (trend degrades to its own empty state, nothing else breaks).
  it("renders the trend's empty state when the status has no history/today (backward compat)", () => {
    expect(html).toContain("Noch keine Tagesdaten");
    expect(html).toContain("kommt mit dem ersten aktiven Diktat-Tag");
    expect(html).toContain("Systemweites Diktat"); // rest of the page unaffected
  });

  it("wires history/today through to the trend block when present", () => {
    const withTrend = renderToStaticMarkup(
      <DiktatBody
        status={{
          ...status,
          history: [
            { date: "2026-07-14", dictations: 25, failures: 2, retries: 1, busy: 0, success_rate_percent: 92.6, latency_p50_ms: 750, latency_p95_ms: 1380 },
          ],
          today: { date: "2026-07-15", dictations: 6, failures: 0, retries: 0, busy: 0, success_rate_percent: 100, latency_p50_ms: 690, latency_p95_ms: 950 },
        }}
        statusLoading={false}
        statusError={null}
        artifacts={artifacts}
        artifactsError={null}
        sha256={null}
      />,
    );
    expect(withTrend).not.toContain("Noch keine Tagesdaten");
    expect(withTrend).toContain("2026-07-14");
    expect(withTrend).toContain("92.6%");
    expect(withTrend).toContain("heute");
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

// Harvested from tests/hermes_cli/test_web_server.py
// test_dictate_personalization_round_trips_real_german_dictionary — real
// server-verified line shapes (umlauts, a `#`-comment line, a snippet whose
// replacement carries a literal "\n" the app expands at insert time), not a
// hand-picked synthetic string.
const REAL_DICTIONARY_RULES =
  "# Eigennamen (Müller, Schäfer)\n" +
  "her mess => Hermes\n" +
  "plan speak => PlanSpec\n" +
  "kanban bord => Kanban Board";
const REAL_SNIPPET_RULES = "meine adresse => Musterweg 12\\n12345 Beispielstadt";

const PERSONALIZATION_DOC = {
  schema: "hermes-dictate-personalization-v1",
  exists: true,
  dictionary_rules: REAL_DICTIONARY_RULES,
  snippet_rules: REAL_SNIPPET_RULES,
  revision: 3,
  updated_at: "2026-07-15T10:22:00+00:00",
  updated_by: "app",
};

function jsonResponse(body: unknown, status = 200) {
  return new Response(JSON.stringify(body), {
    status,
    headers: { "Content-Type": "application/json" },
  });
}

describe("DictionaryEditorPanel", () => {
  const fetchMock = vi.fn();

  beforeEach(() => {
    vi.clearAllMocks();
    Object.defineProperty(window, "__HERMES_SESSION_TOKEN__", {
      configurable: true,
      value: "test-token",
    });
    vi.stubGlobal("fetch", fetchMock);
  });

  afterEach(() => {
    cleanup();
    vi.unstubAllGlobals();
  });

  it("lädt den geladenen Stand (echtes Datenformat) und zählt Regel-Zeilen korrekt", async () => {
    fetchMock.mockResolvedValueOnce(jsonResponse(PERSONALIZATION_DOC));
    render(<DictionaryEditorPanel />);

    const dictionary = (await screen.findByLabelText("Wörterbuch")) as HTMLTextAreaElement;
    expect(dictionary.value).toBe(REAL_DICTIONARY_RULES);
    // 4 lines: 1 comment + 3 rule lines — the comment must NOT count.
    expect(screen.getByText("3/250 Regeln")).toBeTruthy();

    const snippets = screen.getByLabelText("Snippets") as HTMLTextAreaElement;
    expect(snippets.value).toBe(REAL_SNIPPET_RULES);
    expect(screen.getByText("1/250 Regeln")).toBeTruthy();

    expect(screen.getByText(/Zuletzt geändert/)).toBeTruthy();
  });

  it("Save bleibt disabled bis dirty; erfolgreicher PUT sendet den exakten Payload", async () => {
    const edited = REAL_DICTIONARY_RULES + "\nneu => Neu";
    fetchMock
      .mockResolvedValueOnce(jsonResponse(PERSONALIZATION_DOC))
      .mockResolvedValueOnce(jsonResponse({ ...PERSONALIZATION_DOC, revision: 4, dictionary_rules: edited }));

    render(<DictionaryEditorPanel />);
    const dictionary = (await screen.findByLabelText("Wörterbuch")) as HTMLTextAreaElement;
    const saveButton = screen.getByRole("button", { name: "Speichern" }) as HTMLButtonElement;
    expect(saveButton.disabled).toBe(true);

    fireEvent.change(dictionary, { target: { value: edited } });
    expect(saveButton.disabled).toBe(false);

    fireEvent.click(saveButton);
    await screen.findByText("Gespeichert · Stand r4");

    expect(fetchMock).toHaveBeenNthCalledWith(
      2,
      "/api/dictate/personalization",
      expect.objectContaining({ method: "PUT" }),
    );
    const [, putOptions] = fetchMock.mock.calls[1];
    expect(JSON.parse(String(putOptions?.body))).toEqual({
      dictionary_rules: edited,
      snippet_rules: REAL_SNIPPET_RULES,
      base_revision: 3,
      source: "dashboard",
    });
    expect((screen.getByRole("button", { name: "Speichern" }) as HTMLButtonElement).disabled).toBe(true);
  });

  it("409: zeigt den Konflikt-Hinweis, „Serverstand laden“ übernimmt Inhalt + base_revision", async () => {
    const conflictDoc = {
      ...PERSONALIZATION_DOC,
      revision: 5,
      dictionary_rules: "server => gewonnen",
      snippet_rules: "",
    };
    fetchMock
      .mockResolvedValueOnce(jsonResponse(PERSONALIZATION_DOC))
      .mockResolvedValueOnce(jsonResponse(conflictDoc, 409))
      .mockResolvedValueOnce(jsonResponse({ ...conflictDoc, revision: 6 }));

    render(<DictionaryEditorPanel />);
    const dictionary = (await screen.findByLabelText("Wörterbuch")) as HTMLTextAreaElement;
    fireEvent.change(dictionary, { target: { value: "lokal => geändert" } });
    fireEvent.click(screen.getByRole("button", { name: "Speichern" }));

    await screen.findByText(/Inzwischen geändert/);
    expect(screen.getByText(/verwirft deine ungespeicherten Änderungen/)).toBeTruthy();

    fireEvent.click(screen.getByRole("button", { name: "Serverstand laden" }));
    await waitFor(() =>
      expect((screen.getByLabelText("Wörterbuch") as HTMLTextAreaElement).value).toBe("server => gewonnen"),
    );

    // base_revision advanced to the conflict document's revision (5), proven
    // by the next successful save's payload.
    fireEvent.change(screen.getByLabelText("Wörterbuch"), { target: { value: "server => gewonnen\nzweite => Regel" } });
    fireEvent.click(screen.getByRole("button", { name: "Speichern" }));
    await screen.findByText("Gespeichert · Stand r6");
    const [, putOptions] = fetchMock.mock.calls[2];
    expect(JSON.parse(String(putOptions?.body))).toMatchObject({ base_revision: 5 });
  });

  it("400 invalid_rules: zeigt Feldname und Zeilennummern aus detail.lines", async () => {
    fetchMock.mockResolvedValueOnce(jsonResponse(PERSONALIZATION_DOC)).mockResolvedValueOnce(
      jsonResponse(
        {
          detail: {
            error: "invalid_rules",
            field: "dictionary_rules",
            lines: [3],
            reason: "trigger must be 1-120 chars and replacement 1-2000 chars (trimmed)",
          },
        },
        400,
      ),
    );

    render(<DictionaryEditorPanel />);
    const dictionary = (await screen.findByLabelText("Wörterbuch")) as HTMLTextAreaElement;
    fireEvent.change(dictionary, { target: { value: REAL_DICTIONARY_RULES + "\n" + "x".repeat(121) + " => y" } });
    fireEvent.click(screen.getByRole("button", { name: "Speichern" }));

    const message = await screen.findByText(/ungültige Zeile/);
    expect(message.textContent).toContain("Wörterbuch");
    expect(message.textContent).toContain("3");
    expect(message.textContent).toContain("trigger must be 1-120 chars");
  });

  it("exists:false rendert einen Leerzustand ohne Fehler-Look", async () => {
    fetchMock.mockResolvedValueOnce(
      jsonResponse({
        schema: "hermes-dictate-personalization-v1",
        exists: false,
        dictionary_rules: "",
        snippet_rules: "",
        revision: 0,
        updated_at: null,
        updated_by: null,
      }),
    );

    render(<DictionaryEditorPanel />);
    await screen.findByText(/Noch kein gespeichertes Wörterbuch/);
    expect((screen.getByLabelText("Wörterbuch") as HTMLTextAreaElement).value).toBe("");
    expect(screen.queryByText(/nicht erreichbar/)).toBeNull();
  });

  it("GET-Fehler zeigt Fehlerhinweis + Retry, der neu lädt", async () => {
    fetchMock.mockRejectedValueOnce(new Error("network down")).mockResolvedValueOnce(jsonResponse(PERSONALIZATION_DOC));

    render(<DictionaryEditorPanel />);
    // "network down" only appears in the outer <span> (the sibling <strong>
    // wraps just the "nicht erreichbar" label) — an unambiguous single match,
    // unlike a substring both nodes would share.
    await screen.findByText(/network down/);

    fireEvent.click(screen.getByRole("button", { name: "Erneut versuchen" }));
    await screen.findByLabelText("Wörterbuch");
    expect(fetchMock).toHaveBeenCalledTimes(2);
  });
});
