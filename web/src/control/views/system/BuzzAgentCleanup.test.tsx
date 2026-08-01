// @vitest-environment jsdom
/**
 * BuzzAgentCleanup (BAC-3) — A-Wartungsleiste + C-Quittung + Bestätigungs-
 * dialog in /control/system. Fixtures im echten Backend-Format
 * (hermes_cli/buzz_agent_cleanup.py, schema_version=1). Kernzusagen:
 * exakter POST ohne Unit-Namen/Auswahl (AC-6), Dialog nennt alle Ziele +
 * wörtliche Warnung (AC-4), 409 mündet in denselben Status-Poll (AC-5),
 * unbekannte Werte werden nie zu 0 erfunden (AC-3), alle Zustände sind
 * unterscheidbar (AC-7).
 */
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { cleanup, fireEvent, render, screen, within } from "@testing-library/react";

import { _resetPollingStore } from "../../hooks/pollingStore";

const fetchJSONMock = vi.hoisted(() => vi.fn());

vi.mock("@/lib/api", async () => {
  const actual = await vi.importActual<typeof import("@/lib/api")>("@/lib/api");
  return { ...actual, fetchJSON: fetchJSONMock };
});

import { BuzzAgentCleanup, BUZZ_CLEANUP_WARNING, buzzCgroupSum, buzzUnitProgress } from "./BuzzAgentCleanup";

// ── Fixtures im echten Backend-Shape (buzz_agent_cleanup.py) ───────────────

const UNITS = ["buzz-agent@alpha.service", "buzz-agent@beta.service"];

/** Quittung schema_version=1, exakt wie _base_receipt/persist sie schreibt. */
function receiptFixture(overrides: Record<string, unknown> = {}) {
  return {
    schema_version: 1,
    run_id: "f6b63342-c0b1-4e4e-9b52-2f6f0d2b1a10",
    started_at: "2026-08-01T02:00:00+00:00",
    finished_at: "2026-08-01T02:06:37+00:00",
    targets: [...UNITS],
    mode: "all",
    before: {
      "buzz-agent@alpha.service": { main_pid: 4101, memory_current: 654_311_424, cgroup_process_count: 41, active_state: "active" },
      "buzz-agent@beta.service": { main_pid: 4202, memory_current: 512_000_000, cgroup_process_count: 22, active_state: "active" },
    },
    after: {
      "buzz-agent@alpha.service": { main_pid: 8811, memory_current: 41_943_040, cgroup_process_count: 0, active_state: "active" },
      "buzz-agent@beta.service": { main_pid: 8822, memory_current: 39_845_888, cgroup_process_count: 0, active_state: "active" },
    },
    total_memory_current_before: 1_166_311_424,
    total_memory_current_after: 81_788_928,
    failed_units: [],
    exit_code: 0,
    ...overrides,
  };
}

function statusFixture(overrides: Record<string, unknown> = {}) {
  return {
    target_sets_equal: true,
    units: [...UNITS],
    config_stems: ["alpha", "beta"],
    units_without_config: [],
    configs_without_unit: [],
    total_memory_current: 1_166_311_424,
    unit_metrics: {
      "buzz-agent@alpha.service": { main_pid: 4101, memory_current: 654_311_424, cgroup_process_count: 41, active_state: "active" },
      "buzz-agent@beta.service": { main_pid: 4202, memory_current: 512_000_000, cgroup_process_count: 22, active_state: "active" },
    },
    next_timer_run: new Date(Date.now() + 6 * 3_600_000 + 12 * 60_000).toISOString(),
    running: false,
    last_receipt: null,
    ...overrides,
  };
}

let currentStatus: Record<string, unknown>;
let postHandler: (init?: RequestInit) => Promise<unknown>;

function postCalls() {
  return fetchJSONMock.mock.calls.filter((call) => call[0] === "/api/buzz-agent-cleanup/run");
}

beforeEach(() => {
  _resetPollingStore();
  fetchJSONMock.mockReset();
  currentStatus = statusFixture();
  postHandler = () => Promise.resolve({ accepted: true, targets: [...UNITS] });
  fetchJSONMock.mockImplementation((url: string, init?: RequestInit) => {
    if (url === "/api/buzz-agent-cleanup/status") return Promise.resolve(currentStatus);
    if (url === "/api/buzz-agent-cleanup/run") return postHandler(init);
    return Promise.reject(new Error(`unexpected fetch ${url}`));
  });
});

afterEach(() => {
  cleanup();
  _resetPollingStore();
});

async function renderLoaded() {
  render(<BuzzAgentCleanup />);
  await screen.findByRole("button", { name: "Agenten aufräumen" });
}

describe("A-Wartungsleiste (AC-2)", () => {
  it("zeigt Zielmenge, MemoryCurrent, nächsten Lauf und genau eine primäre Aktion", async () => {
    await renderLoaded();
    expect(screen.getByText("Zielmenge")).toBeTruthy();
    expect(screen.getByText("2")).toBeTruthy();
    expect(screen.getByText("MemoryCurrent")).toBeTruthy();
    expect(screen.getByText("1,1 GB")).toBeTruthy();
    expect(screen.getByText("Nächster Lauf")).toBeTruthy();
    expect(screen.getByText(/in 6h 1\dm/)).toBeTruthy();
    // genau eine primäre Aktion
    expect(screen.getAllByRole("button", { name: "Agenten aufräumen" })).toHaveLength(1);
  });

  it("primäre Aktion erfüllt das 44px-Touchziel (AC-8)", async () => {
    await renderLoaded();
    const action = screen.getByRole("button", { name: "Agenten aufräumen" });
    expect(action.className).toContain("min-h-[44px]");
  });
});

describe("Bestätigungsdialog (AC-4)", () => {
  it("nennt alle serverseitigen Ziele, die wörtliche Warnung und sperrt den Start bis zur Bestätigung", async () => {
    await renderLoaded();
    fireEvent.click(screen.getByRole("button", { name: "Agenten aufräumen" }));
    const dialog = await screen.findByRole("dialog", { name: "Buzz-Agenten aufräumen bestätigen" });

    for (const unit of UNITS) {
      expect(within(dialog).getByText(unit)).toBeTruthy();
    }
    expect(within(dialog).getByText(BUZZ_CLEANUP_WARNING)).toBeTruthy();
    expect(within(dialog).getByText(/2 Ziele · seriell · Readiness-Gate aktiv/)).toBeTruthy();

    const confirm = within(dialog).getByRole("button", { name: "Start bestätigen" });
    expect(confirm).toHaveProperty("disabled", true);
    fireEvent.click(confirm);
    expect(postCalls()).toHaveLength(0);

    fireEvent.click(within(dialog).getByRole("checkbox"));
    expect(confirm).toHaveProperty("disabled", false);
    expect(within(dialog).getByRole("checkbox").closest("label")?.className).toContain("min-h-[44px]");
  });

  it("Abbrechen schließt ohne POST", async () => {
    await renderLoaded();
    fireEvent.click(screen.getByRole("button", { name: "Agenten aufräumen" }));
    const dialog = await screen.findByRole("dialog");
    fireEvent.click(within(dialog).getByRole("button", { name: "Abbrechen" }));
    expect(screen.queryByRole("dialog")).toBeNull();
    expect(postCalls()).toHaveLength(0);
  });
});

describe("Start-Request (AC-6)", () => {
  it("sendet exakt POST ohne Body, ohne Unit-Namen, ohne Auswahl", async () => {
    await renderLoaded();
    fireEvent.click(screen.getByRole("button", { name: "Agenten aufräumen" }));
    const dialog = await screen.findByRole("dialog");
    fireEvent.click(within(dialog).getByRole("checkbox"));
    fireEvent.click(within(dialog).getByRole("button", { name: "Start bestätigen" }));
    await vi.waitFor(() => expect(postCalls()).toHaveLength(1));

    const calls = postCalls();
    expect(calls).toHaveLength(1);
    const [url, init] = calls[0] as [string, RequestInit];
    expect(url).toBe("/api/buzz-agent-cleanup/run");
    expect(init.method).toBe("POST");
    expect("body" in init).toBe(false);
    expect(init.headers == null || !("Content-Type" in (init.headers as Record<string, string>))).toBe(true);
    // kein Ziel-Parameter in der URL
    expect(url).not.toContain("unit");
    expect(url).not.toContain("target");
  });
});

describe("Laufender Cleanup (AC-5)", () => {
  it("zeigt Fortschritt je Unit und Gesamtfortschritt aus der PID-Basis", async () => {
    postHandler = () => {
      // Backend arbeitet seriell: alpha ist bereits neu gestartet (neue PID,
      // wenig RSS), beta läuft noch mit alter PID.
      currentStatus = statusFixture({
        running: true,
        unit_metrics: {
          "buzz-agent@alpha.service": { main_pid: 8811, memory_current: 41_943_040, cgroup_process_count: 0, active_state: "active" },
          "buzz-agent@beta.service": { main_pid: 4202, memory_current: 512_000_000, cgroup_process_count: 22, active_state: "active" },
        },
      });
      return Promise.resolve({ accepted: true, targets: [...UNITS] });
    };
    await renderLoaded();
    fireEvent.click(screen.getByRole("button", { name: "Agenten aufräumen" }));
    const dialog = await screen.findByRole("dialog");
    fireEvent.click(within(dialog).getByRole("checkbox"));
    fireEvent.click(within(dialog).getByRole("button", { name: "Start bestätigen" }));

    await screen.findByText(/1\/2 neu gestartet/);
    expect(screen.getByText("neu gestartet")).toBeTruthy();
    expect(screen.getByText("ausstehend")).toBeTruthy();
    // während des Laufs kein zweiter Start-Knopf
    expect(screen.queryByRole("button", { name: "Agenten aufräumen" })).toBeNull();
  });

  it("409 wird als bereits laufender Cleanup dargestellt und startet nichts zweites", async () => {
    postHandler = () => {
      currentStatus = statusFixture({ running: true });
      return Promise.reject(new Error('409: {"detail":{"code":"already_running"}}'));
    };
    await renderLoaded();
    fireEvent.click(screen.getByRole("button", { name: "Agenten aufräumen" }));
    const dialog = await screen.findByRole("dialog");
    fireEvent.click(within(dialog).getByRole("checkbox"));
    fireEvent.click(within(dialog).getByRole("button", { name: "Start bestätigen" }));

    await screen.findByText(/läuft bereits/);
    // derselbe Status-Poll übernimmt: laufende Ansicht ohne Start-Basis
    await screen.findByText(/Live-Status \(Start-Basis unbekannt\)/);
    expect(postCalls()).toHaveLength(1);
  });
});

describe("C-Quittung (AC-3)", () => {
  it("zeigt Zeitpunkt, Erfolgsquote, MemoryCurrent vorher/nachher, Cgroup-Zahlen und überlebt den Reload (kommt aus dem GET)", async () => {
    currentStatus = statusFixture({ last_receipt: receiptFixture() });
    await renderLoaded();
    expect(screen.getByText("Letzte Quittung")).toBeTruthy();
    expect(screen.getByText("Voller Erfolg")).toBeTruthy();
    expect(screen.getByText(/\d{2}\.\d{2}\.\d{4}/)).toBeTruthy();
    expect(screen.getByText("2/2 neu gestartet")).toBeTruthy();
    expect(screen.getByText(/1,1 GB → 78 MB/)).toBeTruthy();
    expect(screen.getByText(/−93 %/)).toBeTruthy();
    expect(screen.getByText("63 → 0")).toBeTruthy();
  });

  it("Teilerfolg nennt die fehlgeschlagenen Units (AC-7)", async () => {
    currentStatus = statusFixture({
      last_receipt: receiptFixture({ failed_units: ["buzz-agent@beta.service"], exit_code: 2 }),
    });
    await renderLoaded();
    expect(screen.getByText("Teilerfolg")).toBeTruthy();
    expect(screen.getByText("1/2 neu gestartet")).toBeTruthy();
    expect(screen.getByText("Fehlgeschlagen")).toBeTruthy();
    expect(screen.getByText("buzz-agent@beta.service")).toBeTruthy();
  });

  it("Fehler-Quittung zeigt Fehlercode statt erfundener Metriken (AC-7)", async () => {
    currentStatus = statusFixture({
      last_receipt: receiptFixture({
        mode: undefined,
        exit_code: 1,
        error: { code: "target_mismatch" },
        total_memory_current_before: null,
        total_memory_current_after: null,
      }),
    });
    await renderLoaded();
    expect(screen.getByText("Fehler")).toBeTruthy();
    expect(screen.getByText("target_mismatch")).toBeTruthy();
    // unbekannte Summen bleiben unbekannt — kein erfundenes "0 MB"
    const unknowns = screen.getAllByText(/unbekannt/);
    expect(unknowns.length).toBeGreaterThan(0);
    expect(screen.queryByText(/0 MB → 0 MB/)).toBeNull();
  });

  it("erfindet keine Nullen, wenn Messwerte fehlen", async () => {
    currentStatus = statusFixture({
      total_memory_current: null,
      next_timer_run: null,
    });
    await renderLoaded();
    const tiles = screen.getAllByText("unbekannt");
    expect(tiles.length).toBeGreaterThanOrEqual(2);
    expect(screen.queryByText("0 MB")).toBeNull();
  });
});

describe("Zustände (AC-7)", () => {
  it("Loading ist als eigener Zustand erkennbar", () => {
    fetchJSONMock.mockImplementation(() => new Promise(() => {}));
    render(<BuzzAgentCleanup />);
    expect(screen.getByText(/wird geladen/)).toBeTruthy();
    expect(screen.queryByRole("button", { name: "Agenten aufräumen" })).toBeNull();
  });

  it("Zielmengen-Differenz sperrt den Start und nennt die Differenz", async () => {
    currentStatus = statusFixture({
      target_sets_equal: false,
      units_without_config: ["buzz-agent@orphan.service"],
      configs_without_unit: ["ghost"],
    });
    await renderLoaded();
    const action = screen.getByRole("button", { name: "Agenten aufräumen" });
    expect(action).toHaveProperty("disabled", true);
    expect(screen.getByText(/Zielmenge unklar/)).toBeTruthy();
    expect(screen.getByText(/buzz-agent@orphan\.service/)).toBeTruthy();
    expect(screen.getByText(/ghost/)).toBeTruthy();
  });

  it("Nullmenge sperrt den Start mit eigener Ansage", async () => {
    currentStatus = statusFixture({ units: [], config_stems: [], total_memory_current: 0, unit_metrics: {} });
    await renderLoaded();
    const action = screen.getByRole("button", { name: "Agenten aufräumen" });
    expect(action).toHaveProperty("disabled", true);
    expect(screen.getByText("Keine Buzz-Agenten gefunden.")).toBeTruthy();
  });

  it("Ladefehler zeigt Fehler und Retry statt leerer Fläche", async () => {
    fetchJSONMock.mockImplementation((url: string) =>
      url === "/api/buzz-agent-cleanup/status"
        ? Promise.reject(new Error("500: boom"))
        : Promise.reject(new Error("unexpected")),
    );
    render(<BuzzAgentCleanup />);
    await screen.findByText(/nicht lesbar/);
    expect(screen.getByRole("button", { name: "Erneut versuchen" })).toBeTruthy();
  });
});

describe("Ableitungen (pure)", () => {
  it("buzzUnitProgress klassifiziert ehrlich", () => {
    const active = { main_pid: 100, memory_current: 1, cgroup_process_count: 0, active_state: "active" };
    expect(buzzUnitProgress({ ...active, main_pid: 200 }, 100)).toBe("done");
    expect(buzzUnitProgress(active, 100)).toBe("pending");
    expect(buzzUnitProgress({ ...active, active_state: "activating" }, 100)).toBe("restarting");
    expect(buzzUnitProgress(active, undefined)).toBe("unknown");
    expect(buzzUnitProgress(undefined, 100)).toBe("pending");
  });

  it("buzzCgroupSum summiert nur vollständige Messreihen", () => {
    const snaps = {
      "a.service": { main_pid: 1, memory_current: 1, cgroup_process_count: 5, active_state: "active" },
      "b.service": { main_pid: 2, memory_current: 1, cgroup_process_count: null, active_state: "active" },
    };
    expect(buzzCgroupSum(snaps, ["a.service", "b.service"])).toBeNull();
    expect(buzzCgroupSum(snaps, ["a.service"])).toBe(5);
  });
});
