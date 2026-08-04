// @vitest-environment jsdom
import { renderToStaticMarkup } from "react-dom/server";
import { MemoryRouter } from "react-router-dom";
import { act, cleanup, fireEvent, render, renderHook, screen, waitFor, within } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";
import { LoopsGrid, type LoopsGridProps } from "./LoopsView";
import { formatLoopTimestamp, useLoopNowMs } from "../lib/loopTime";
// Vite ?raw: Quelltext der Komponente für den Zero-Network-Font-Guard (W3-5).
import loopsViewSource from "./LoopsView.tsx?raw";
import { deriveRingSegments, deriveRingTicks } from "../lib/loopRing";
import { LoopsResponseSchema } from "../lib/schemas";
import { de } from "../i18n/de";
import type { LoopDetailResponse, LoopFilesResponse, LoopHeartbeatCurrent, LoopModelsResponse, LoopPack, LoopPackSummary } from "../lib/types";

const t = de.loops;

const getLoopNightOverrides = vi.fn(async (pack: string) => ({ pack, overrides: {} as Record<string, string> }));
const putLoopNightOverrides = vi.fn(async (pack: string, overrides: Record<string, string>) => ({
  pack,
  overrides,
  ok: true,
}));

vi.mock("../hooks/loops", async (importOriginal) => {
  const actual = await importOriginal<typeof import("../hooks/loops")>();
  return {
    ...actual,
    getLoopNightOverrides: (pack: string) => getLoopNightOverrides(pack),
    putLoopNightOverrides: (pack: string, overrides: Record<string, string>) =>
      putLoopNightOverrides(pack, overrides),
  };
});

// Ohne explizites afterEach(cleanup) akkumulieren mehrfache render()-Aufrufe
// (screen/within) im selben Testfile den DOM — belegt beim Hinzufügen der
// W3-5-Touch-Target-Tests unten (Cross-Test-Kollision auf "Planung
// überspringen"). Etabliertes Muster in diesem Repo, s. leitstand.test.tsx.
afterEach(() => {
  vi.useRealTimers();
  vi.unstubAllGlobals();
  cleanup();
  getLoopNightOverrides.mockReset();
  putLoopNightOverrides.mockReset();
  getLoopNightOverrides.mockImplementation(async (pack: string) => ({ pack, overrides: {} }));
  putLoopNightOverrides.mockImplementation(async (pack: string, overrides: Record<string, string>) => ({
    pack,
    overrides,
    ok: true,
  }));
});

describe("Loops live clock", () => {
  it("advances independently of API polling", () => {
    vi.useFakeTimers();
    vi.setSystemTime(new Date("2026-07-13T01:00:00Z"));
    const { result } = renderHook(() => useLoopNowMs());
    const before = result.current;
    act(() => {
      vi.advanceTimersByTime(2_000);
    });
    expect(result.current).toBe(before + 2_000);
    vi.useRealTimers();
  });
});

// Manifest-Felder (phases/stop/params/description/stability/type) sind aus dem
// ECHTEN Payload geerntet — via TestClient(app).get("/api/loops") gegen die
// tatsächlichen loops/packs/builder-reviewer + loops/packs/doc-sweep Manifeste
// (2026-07-02). Nur die Laufzeit-Felder (running/queue/commits_ahead/heartbeat)
// sind für die Szenarien hier überschrieben, weil aktuell live kein Loop läuft.
// Die heartbeat-Form selbst (current/last-Felder) ist gegen den ECHTEN
// TestClient(app).get("/api/loops")-Payload nach dem Schreiben von
// state/heartbeat.json geerntet (siehe schemas.test.ts).
const runningPipeline: LoopPack = {
  name: "builder-reviewer",
  type: "pipeline",
  // "source" ist ein neueres Feld (control_loops.py:220, Nachtschicht-Redesign) —
  // beide Fixture-Packs sind reale kuratierte Manifeste unter loops/packs/.
  source: "repo",
  repo: "/home/piet/.hermes/hermes-agent",
  repo_exists: true,
  base_branch: "main",
  // land_remote/land_push/land_gates: builder-reviewer/pack.yaml setzt keins
  // davon → Loader-Defaults (piet-fork/true/null), real geerntet (2026-07-16).
  land_remote: "piet-fork",
  land_push: true,
  land_gates: null,
  description: "Fable plant Schwachstellen-Fixes, Sonnet baut, Fable verifiziert adversarial",
  stability: "stable",
  phases: {
    plan: { engine: "claude", model: "claude-fable-5", timeout: 2400 },
    build: { engine: "claude", model: "claude-sonnet-5", timeout: 3600 },
    verify: { engine: "claude", model: "claude-fable-5", timeout: 2400 },
  },
  stop: { max_rounds: 12, max_hours: 7, fail_streak: 2, dry_rounds: 2 },
  params: { max_plans: "8", focus: "Hermes-Board/Kanban-Robustheit" },
  running: true,
  heartbeat: {
    current: { phase: "build", engine: "claude", model: "claude-sonnet-5", started_at: "2026-07-02T23:00:00Z", timeout: 3600, round: 1 },
    last: [
      { phase: "build", engine: "claude", model: "claude-sonnet-5", secs: 512, rc: 0, at: "2026-07-02T22:00:00Z" },
      { phase: "verify", engine: "claude", model: "claude-fable-5", secs: 178, rc: 1, at: "2026-07-02T22:10:00Z" },
    ],
  },
  stop_requested: false,
  queue: { "00-planned": 1, "10-building": 2, "20-verified": 7, "30-landed": 3, "90-bounced": 0 },
  commits_ahead: 0,
  timer_enabled: true,
  timer_schedule: "23:37",
  timer_next_run: "2026-07-09T21:37:00Z",
  token_usage: { total_tokens: 370, metered_cost_eur: 0, billing: "subscription" },
};

// Gleiches Manifest, aber zwischen zwei Phasen (heartbeat.current == null,
// obwohl running=true) — der Übergangs-Zustand aus der Aufgabenbeschreibung.
const betweenPhasesPipeline: LoopPack = {
  ...runningPipeline,
  name: "builder-reviewer-between",
  heartbeat: { current: null, last: runningPipeline.heartbeat!.last },
};

const idleSweepWithCommits: LoopPack = {
  name: "doc-sweep",
  type: "sweep",
  source: "repo",
  repo: "/home/piet/.hermes/hermes-agent",
  repo_exists: true,
  base_branch: "main",
  // doc-sweep/pack.yaml setzt ebenfalls keins der drei Felder → dieselben Defaults.
  land_remote: "piet-fork",
  land_push: true,
  land_gates: null,
  description: "Pro Runde EINE Doku-Drift zwischen Repo-Doku und Code-Verhalten finden und die Doku korrigieren",
  stability: "experimental",
  phases: { round: { engine: "claude", model: "claude-sonnet-5", timeout: 2400 } },
  stop: { max_rounds: 10, max_hours: 7, fail_streak: 2, dry_rounds: 2 },
  params: { fokus: "AGENTS.md, README*, CLAUDE.md (Repo), docs/, plugins/*/README" },
  running: false,
  heartbeat: null,
  stop_requested: false,
  queue: null,
  commits_ahead: 4,
  timer_enabled: false,
  timer_schedule: "02:07",
  timer_next_run: null,
};

// Läuft UND hat unverdaute Commits — Land darf trotzdem nicht auftauchen
// (running unterdrückt es, egal was commits_ahead sagt).
const runningWithCommits: LoopPack = {
  ...runningPipeline,
  name: "running-with-commits",
  commits_ahead: 2,
};

// Real failure shape captured from ht-ux-polish after PASS_ID_MISMATCH:
// history-only fix+revert commits remain ahead, but no plan reached 20-verified.
const bouncedPipelineWithHistoryOnlyCommits: LoopPack = {
  ...runningPipeline,
  name: "bounced-history-only",
  running: false,
  heartbeat: {
    current: null,
    last: [
      { phase: "verify", engine: "codex", model: "gpt-5.6-sol", secs: 583, rc: 0, at: "2026-07-13T00:56:17Z" },
    ],
  },
  queue: { "00-planned": 0, "10-building": 0, "20-verified": 0, "30-landed": 0, "90-bounced": 1 },
  commits_ahead: 2,
};

// Idle, aber ohne Commits — Land darf nicht auftauchen.
const idleNoCommits: LoopPack = {
  ...idleSweepWithCommits,
  name: "idle-no-commits",
  commits_ahead: 0,
};

// Per Werkstatt dupliziertes Pack (source="custom") — für den Source-Badge.
const customPack: LoopPack = {
  ...idleSweepWithCommits,
  name: "doc-sweep-copy",
  source: "custom",
  commits_ahead: 0,
};

const brokenPack: LoopPack = {
  name: "broken-pack",
  error: "ManifestError: phases fehlt in pack.yaml",
};

const models: LoopModelsResponse = {
  engines: {
    claude: { label: "Claude (Abo)", models: ["claude-fable-5", "claude-opus-4-8", "claude-sonnet-5", "claude-haiku-4-5"] },
    kimi: { label: "Kimi (Coding-Abo)", models: ["kimi-code/kimi-for-coding", "k3"] },
    codex: { label: "Codex (ChatGPT-Abo)", models: ["gpt-5.5", "gpt-5.3-codex"] },
    neuralwatt: { label: "NeuralWatt (Abo)", models: ["glm-5.2", "kimi-k2.7-code", "qwen3.6-35b-fast"] },
    "alibaba-token-plan": { label: "Alibaba Token Plan", models: ["qwen3.8-max-preview"] },
  },
};

const noopHandlers = {
  onSetPendingStop: vi.fn(),
  onSetPendingLand: vi.fn(),
  onToggleDetail: vi.fn(),
  onToggleWorkshop: vi.fn(),
  onOpenStart: vi.fn(),
  onCloseStart: vi.fn(),
  onSubmitStart: vi.fn(),
  onStop: vi.fn(),
  onLand: vi.fn(),
  onToggleTimer: vi.fn(),
  onSaveTimerSchedule: vi.fn(),
  onSaveFile: vi.fn(),
  onDuplicate: vi.fn(),
  onToggleAutomation: vi.fn(),
  onPreview: vi.fn(),
};

function renderGrid(packs: LoopPack[], overrides: Partial<LoopsGridProps> = {}) {
  return renderToStaticMarkup(
    <LoopsGrid
      packs={packs}
      models={models}
      selectedPack={null}
      detail={null}
      detailLoading={false}
      detailError={null}
      busyPack={null}
      actionErrorByPack={{}}
      landNoteByPack={{}}
      startOpenPack={null}
      pendingStopPack={null}
      pendingLandPack={null}
      workshopOpenPack={null}
      files={null}
      filesLoading={false}
      filesError={null}
      fileSaveBusy={false}
      fileSaveError={null}
      duplicateBusy={false}
      duplicateError={null}
      {...noopHandlers}
      {...overrides}
    />,
  );
}

/** Klappt den Konfigurationsblock einer Karte auf.
 *
 *  Timer und Nachtmodelle liegen seit dem Verdichtungs-Umbau (28.07., Punkt 1)
 *  in einem zugeklappten Disclosure — dessen Kinder rendert AnimatePresence
 *  nicht, solange es zu ist. Tests, die an diese Controls wollen, muessen es
 *  also oeffnen; genau das tut ein Operator auch. */
function openConfig(container: HTMLElement) {
  const trigger = within(container).getAllByText(/· Modelle$/)[0].closest("button");
  if (!trigger) throw new Error("Konfigurations-Disclosure nicht gefunden");
  fireEvent.click(trigger);
}

function renderInteractiveGrid(packs: LoopPack[], overrides: Partial<LoopsGridProps> = {}) {
  return render(
    <LoopsGrid
      packs={packs}
      models={models}
      selectedPack={null}
      detail={null}
      detailLoading={false}
      detailError={null}
      busyPack={null}
      actionErrorByPack={{}}
      landNoteByPack={{}}
      startOpenPack={null}
      pendingStopPack={null}
      pendingLandPack={null}
      workshopOpenPack={null}
      files={null}
      filesLoading={false}
      filesError={null}
      fileSaveBusy={false}
      fileSaveError={null}
      duplicateBusy={false}
      duplicateError={null}
      {...noopHandlers}
      {...overrides}
    />,
  );
}

describe("LoopsGrid", () => {
  it("renders aggregate tokens and honest zero metered subscription spend from the real ledger shape", () => {
    const html = renderGrid([runningPipeline]);
    expect(html).toContain(t.tokenUsage(370));
    expect(html).toContain(t.subscriptionZeroMetered);
  });
  it("groups packs by repository and shows the bound base branch", () => {
    const healthTrack: LoopPack = {
      ...idleSweepWithCommits,
      name: "health-track-ux",
      repo: "/home/piet/projects/health-track",
      base_branch: "main",
    };
    const html = renderGrid([runningPipeline, healthTrack]);

    expect(html).toContain("hermes-agent");
    expect(html).toContain("health-track");
    expect(html).toContain("main");
  });

  it("shows state-based mobile progress with round and phases but no invented percentage", () => {
    renderInteractiveGrid([runningPipeline], { nowMs: Date.parse("2026-07-02T23:14:30Z") });
    const progress = screen.getByTestId("loop-mobile-progress");

    expect(progress.textContent).toContain("Runde 1 / 12");
    expect(progress.textContent).toContain("Plan");
    expect(progress.textContent).toContain("Build");
    expect(progress.textContent).toContain("Verify");
    expect(progress.textContent).not.toContain("%");
  });

  it("renders a running pipeline pack with live status, stability/type badges and queue counts", () => {
    const html = renderGrid([runningPipeline]);
    expect(html).toContain("builder-reviewer");
    expect(html).toContain(t.stabilityStable);
    expect(html).toContain(t.typePipeline);
    expect(html).toContain(t.statusRunning);
    expect(html).toContain(t.queuePlanned);
    expect(html).toContain(t.queueBuilding);
    expect(html).toContain(t.queueVerified);
    expect(html).toContain(t.queueLanded);
    // 90-bounced ist 0 in dieser Fixture — der rote "n abgeprallt"-Chip zeigt
    // sich erst bei bounced>0 (kein Alarm-Chip fuer "nichts ist passiert"),
    // siehe eigener Test unten.
    expect(html).not.toContain(t.queueBounced);
    // 20-verified count (7) und 30-landed count (3) muessen sichtbar sein, nicht nur das Label.
    expect(html).toMatch(/>7</);
    expect(html).toMatch(/>3</);
  });

  it("shows the red 'n abgeprallt' chip only when the bounced stage is non-empty", () => {
    const withBounced: LoopPack = {
      ...runningPipeline,
      queue: { "00-planned": 1, "10-building": 2, "20-verified": 7, "30-landed": 3, "90-bounced": 2 },
    };
    const html = renderGrid([withBounced]);
    expect(html).toContain(`2 ${t.queueBounced}`);
  });

  it("renders an idle experimental sweep pack with an 'unverdaute Commits' badge", () => {
    const html = renderGrid([idleSweepWithCommits]);
    expect(html).toContain("doc-sweep");
    expect(html).toContain(t.stabilityExperimental);
    expect(html).toContain(t.typeSweep);
    expect(html).toContain(t.statusIdle);
    expect(html).toContain(t.commitsAhead(4));
    // sweep hat keine Queue → keine Queue-Stat-Kacheln fuer dieses Pack.
    expect(html).not.toContain(t.queueBuilding);
  });

  it("shows the land remote when land_push is true (default builder-reviewer/doc-sweep config)", () => {
    const html = renderGrid([runningPipeline]);
    expect(html).toContain((runningPipeline as LoopPackSummary).land_remote);
    expect(html).not.toContain(t.landNoAutoPush);
  });

  it("shows a neutral 'kein Auto-Push' hint instead of the remote when land_push is false", () => {
    const noAutoPush: LoopPack = { ...idleSweepWithCommits, name: "ht-locked", land_push: false };
    const html = renderGrid([noAutoPush]);
    expect(html).toContain(t.landNoAutoPush);
    expect(html).not.toContain((idleSweepWithCommits as LoopPackSummary).land_remote);
  });

  it("shows the land-gates count with the commands as a tooltip when land_gates is set, nothing when null", () => {
    const withGates: LoopPack = {
      ...idleSweepWithCommits,
      name: "gated",
      land_gates: ["npm run gate", "pytest -q"],
    };
    const html = renderGrid([withGates]);
    expect(html).toContain(t.landGatesCount(2));
    expect(html).toContain('title="npm run gate, pytest -q"');

    const withoutGates = renderGrid([idleSweepWithCommits]);
    expect(withoutGates).not.toContain("Land-Gate");
  });

  it("renders a manifest-error pack as an error card, not a crash", () => {
    const html = renderGrid([brokenPack]);
    expect(html).toContain("broken-pack");
    expect(html).toContain(t.manifestError);
    expect(html).toContain("ManifestError: phases fehlt in pack.yaml");
  });

  it("surfaces a probe error on an otherwise healthy pack", () => {
    // control_loops.py degrades a failed git probe into summary["error"] and
    // still returns a full summary. Without this the card renders a confident
    // "0 offene Commits" and the operator never learns the probe timed out.
    const probeFailed: LoopPack = {
      ...idleNoCommits,
      name: "probe-failed",
      error: "git-Probe fehlgeschlagen: Command timed out after 30 seconds",
    };

    const html = renderGrid([probeFailed]);

    expect(html).toContain("probe-failed");
    expect(html).toContain("git-Probe fehlgeschlagen");
    // Still the normal card, not the manifest-error card.
    expect(html).not.toContain(t.manifestError);
  });

  it("renders all three scenarios together without throwing", () => {
    const html = renderGrid([runningPipeline, idleSweepWithCommits, brokenPack]);
    expect(html).toContain("builder-reviewer");
    expect(html).toContain("doc-sweep");
    expect(html).toContain("broken-pack");
  });

  it("shows the empty state when no packs are configured", () => {
    const html = renderGrid([]);
    expect(html).toContain(t.empty);
  });
});

// W3-5: 22 undersized controls auf /control/loops (11 Nachttimer-Checkboxen +
// 11 Logbuch-Disclosure-Trigger, gemessen via scripts/visual-verify.sh —
// height/width < 24 CSS px). Fix = feste size-12/min-h-12-Klassen statt
// box-content+padding (Chromium ignoriert Padding-Hitbox-Tricks bei nativen
// Checkboxen — an dieser Stelle live verifiziert, nicht nur angenommen).
// Verwaistes Pack (2026-07-28): zwei Packs zeigten auf einen geloeschten
// Worktree (`.../codex-loops-health-track-audit-20260712/...`), sahen im Tab
// voellig normal aus und waeren erst beim Start gestorben — der Runner prueft
// `pack.repo.is_dir()` und wirft "Pack-Repo existiert nicht", was als
// kryptischer 502 "Loop-Unit sofort gescheitert" im UI ankam. Der Tab sagt es
// jetzt vorher UND sperrt den Start, statt in die Sackgasse laufen zu lassen.
// Verdichtung des Bedien-Flusses (Entwurf 28.07., Punkte 1-5). Der Tab war
// nicht kaputt — er war zu weitlaeufig: bei 1366x900 passte keine einzige Karte
// vollstaendig auf den Schirm, und der Start-Knopf lag unter dem
// Konfigurationsblock.
describe("LoopsGrid — verdichteter Bedien-Fluss", () => {
  it("haelt Timer und Nachtmodelle zugeklappt (Punkt 1)", () => {
    const { container } = renderInteractiveGrid([runningPipeline]);
    // Zugeklappt = gar nicht im DOM (AnimatePresence), nicht nur unsichtbar.
    expect(within(container).queryByLabelText(`${t.timerLabel} builder-reviewer`)).toBeNull();
  });

  it("zeigt den Timer-Zustand schon in der zugeklappten Zeile (Punkt 1)", () => {
    // Sonst muesste man zum Nachsehen jede Karte einzeln aufklappen.
    const withTimer = { ...runningPipeline, timer_enabled: true, timer_schedule: "22:45" };
    expect(renderGrid([withTimer])).toContain(t.configSummary("22:45"));
    expect(renderGrid([{ ...runningPipeline, timer_enabled: false }]))
      .toContain(t.configSummary(t.configTimerOff));
  });

  it("gibt der zugeklappten Zeile den Touch-Boden (min-h-12)", () => {
    const { container } = renderInteractiveGrid([runningPipeline]);
    const trigger = within(container).getAllByText(/· Modelle$/)[0];
    expect(trigger.className).toMatch(/\bmin-h-12\b/);
  });

  it("stellt den Start-Knopf VOR den Konfigurationsblock (Punkt 2)", () => {
    // Positionsvergleich im Markup: der Knopf muss frueher kommen als die
    // Konfig-Zeile, sonst liegt er wieder unterhalb und ausserhalb des Schirms.
    const html = renderGrid([{ ...runningPipeline, running: false }]);
    expect(html.indexOf(t.actions.start)).toBeGreaterThan(-1);
    expect(html.indexOf(t.actions.start)).toBeLessThan(html.indexOf("· Modelle"));
  });

  it("zeigt bei laufendem Pack den Stopp-Knopf oben statt des Starts (Punkt 2)", () => {
    const html = renderGrid([runningPipeline]);
    expect(html).toContain(t.actions.stop);
    expect(html.indexOf(t.actions.stop)).toBeLessThan(html.indexOf("· Modelle"));
  });

  it("zeigt die Queue als Zeile und nur gefuellte Stufen (Punkt 3)", () => {
    const busy = {
      ...runningPipeline,
      queue: { "00-planned": 2, "10-building": 0, "20-verified": 1, "30-landed": 0, "90-bounced": 0 },
    };
    const html = renderGrid([busy]);
    expect(html).toContain(t.queuePlanned);
    expect(html).toContain(t.queueVerified);
    // Leere Stufen tauchen nicht mehr als Kasten mit 0 auf.
    expect(html).not.toContain(t.queueLanded);
  });

  it("sagt bei leerer Queue 'Queue leer' statt vier Nullen (Punkt 3)", () => {
    const empty = {
      ...runningPipeline,
      queue: { "00-planned": 0, "10-building": 0, "20-verified": 0, "30-landed": 0, "90-bounced": 0 },
    };
    const html = renderGrid([empty]);
    expect(html).toContain(t.queueEmpty);
    expect(html).not.toContain(t.queueBuilding);
  });

  it("sortiert das laufende Pack nach oben (Punkt 4)", () => {
    // Kartenreihenfolge aus dem DOM lesen, NICHT per indexOf ueber das ganze
    // Markup: "doc-sweep" kommt auch im Repo-Filter und im Lagebild vor, und
    // ein indexOf-Vergleich blieb deshalb gruen, selbst wenn die Sortierung
    // ausgehaengt war (als Tautologie belegt und behoben, 28.07.).
    const { container } = renderInteractiveGrid([
      { ...runningPipeline, running: false },
      { ...idleSweepWithCommits, running: true },
    ]);
    // Nur die Pack-KARTEN, nicht das Lagebild: das laufende Pack wird oben im
    // Hero ebenfalls genannt, ein Namensvergleich ueber den ganzen Baum haette
    // also den Hero gemessen statt die Reihenfolge.
    // Eine Pack-Karte ist eine Blatt-<section>, die die Konfigurationszeile
    // traegt — das Lagebild oben ist ebenfalls eine rounded-2xl-<section> und
    // nennt das laufende Pack, wuerde also mitgezaehlt.
    const namesInOrder = Array.from(container.querySelectorAll("section"))
      .filter((node) => !node.querySelector("section") && (node.textContent ?? "").includes("· Modelle"))
      .map((card) => card.querySelector(".truncate")?.textContent?.trim())
      .filter((name): name is string => Boolean(name));
    expect(namesInOrder).toEqual(["doc-sweep", "builder-reviewer"]);
  });
});

describe("LoopsGrid — verwaistes Pack (totes repo)", () => {
  const orphan = { ...runningPipeline, name: "orphan-pack", running: false, repo_exists: false };

  it("markiert ein Pack mit fehlendem Repo sichtbar", () => {
    const html = renderGrid([orphan]);
    expect(html).toContain(t.repoMissing);
  });

  it("laesst ein intaktes Pack unmarkiert (Gegenprobe — sonst markiert der Test alles)", () => {
    const html = renderGrid([{ ...runningPipeline, running: false }]);
    expect(html).not.toContain(t.repoMissing);
  });

  it("sperrt den Start-Knopf und nennt den Grund", () => {
    const { container } = renderInteractiveGrid([orphan]);
    const start = within(container).getByText(t.actions.start).closest("button");
    expect(start).not.toBeNull();
    expect(start!.disabled).toBe(true);
    expect(start!.title).toContain(orphan.repo);
  });

  it("laesst den Start-Knopf bei intaktem Repo bedienbar (Gegenprobe)", () => {
    const { container } = renderInteractiveGrid([{ ...runningPipeline, running: false }]);
    const start = within(container).getByText(t.actions.start).closest("button");
    expect(start!.disabled).toBe(false);
  });
});

describe("LoopsGrid — Touch-Target-Boden (W3-5)", () => {
  it("gibt der Nachttimer-Checkbox eine size-12-Klickfläche (vorher h-5 w-5 = 18.8px, unter dem WCAG-2.5.8-Boden)", () => {
    // Gegen ein schema-validiertes Pack (echte Manifest-Form), nicht nur das
    // handgeschriebene Fixture-Objekt direkt.
    const parsed = LoopsResponseSchema.parse({ packs: [runningPipeline] }).packs[0] as LoopPack;
    const { container } = renderInteractiveGrid([parsed]);
    openConfig(container);
    const checkbox = within(container).getByLabelText(`${t.timerLabel} ${parsed.name}`) as HTMLInputElement;
    expect(checkbox.className).toMatch(/\bsize-12\b/);
  });

  it("gibt dem Logbuch-Disclosure-Trigger eine min-h-12-Zeile (vorher 23.3px, unter dem WCAG-2.5.8-Boden)", () => {
    const { container } = renderInteractiveGrid([runningPipeline]);
    const summary = within(container).getByText(t.actions.detail);
    expect(summary.className).toMatch(/\bmin-h-12\b/);
  });

  it("gibt der SKIP_PLAN-Checkbox eine size-12-Klickfläche (vorher unbemaßt = Browser-Default ~13px)", () => {
    const { container } = renderInteractiveGrid([idlePipeline], { startOpenPack: idlePipeline.name });
    const checkbox = within(container).getByLabelText(t.skipPlanLabel) as HTMLInputElement;
    expect(checkbox.className).toMatch(/\bsize-12\b/);
  });
});

// W3-5: der lokale View-eigene Mono-Font-Fork ist raus — Daten-Spans tragen
// jetzt die geteilte font-data-Klasse (IBM Plex Mono aus theme.css), Prosa/
// Labels haben kein fontFamily-Inline-Style mehr. Pinnt die
// Klassifizierungs-Entscheidung als Regressionsschutz.
describe("LoopsGrid — Mono-Konsolidierung (W3-5)", () => {
  it("gibt Queue-Stufen-Zählern die geteilte font-data-Klasse statt eines lokalen Mono-Inline-Styles", () => {
    const { container } = renderInteractiveGrid([runningPipeline]);
    const count = within(container).getByText("7"); // 20-verified count aus der Fixture
    expect(count.className).toMatch(/\bfont-data\b/);
    expect(count.style.fontFamily).toBe("");
  });

  it("lässt eine Prosa-Zeile (Telemetrie-Satz) ohne font-data-Klasse/fontFamily-Override", () => {
    // Der Satz erscheint zweimal (Hero + Karte, beide "zwischen Phasen") —
    // beide Stellen sind Prosa, also gilt die Erwartung für alle Treffer.
    const { container } = renderInteractiveGrid([betweenPhasesPipeline]);
    const lines = within(container).getAllByText(t.heartbeatBetweenPhases);
    expect(lines.length).toBeGreaterThan(0);
    for (const line of lines) {
      expect(line.className).not.toMatch(/\bfont-data\b/);
      expect(line.style.fontFamily).toBe("");
    }
  });

  it("überschreibt den Mono-Default der Dependency für alle sichtbaren Aktionslabels mit Display-Schrift", () => {
    const { container } = renderInteractiveGrid([runningPipeline, idleSweepWithCommits]);
    // Der "Zeit speichern"-Knopf liegt seit dem Verdichtungs-Umbau im
    // Konfigurations-Disclosure — aufklappen, sonst prueft der Test ihn nicht mit.
    openConfig(container);
    for (const action of [t.actions.stop, t.actions.start, t.actions.workshop, t.timerSave]) {
      const buttons = within(container).getAllByRole("button", { name: action });
      expect(buttons.length).toBeGreaterThan(0);
      for (const button of buttons) expect(button.className).toContain("!font-display");
    }
  });
});

describe("LoopsGrid — frei einstellbarer Nachttimer", () => {
  it("formats the backend ISO instant in the operator timezone", () => {
    expect(formatLoopTimestamp("2026-07-09T21:37:00Z", "Europe/Berlin")).toBe("Do., 09.07., 23:37 MESZ");
  });

  it("zeigt gespeicherte lokale Uhrzeit und den echten nächsten Lauf", () => {
    const { container } = renderInteractiveGrid([runningPipeline]);
    openConfig(container);
    const view = within(container);
    const input = view.getByLabelText(`${t.timerTimeLabel} builder-reviewer`) as HTMLInputElement;
    expect(input.value).toBe("23:37");
    expect(container.textContent).not.toContain("2026-07-09T21:37:00Z");
  });

  it("aktiviert Speichern erst nach einer gültigen Änderung und reicht die Uhrzeit weiter", () => {
    const onSaveTimerSchedule = vi.fn();
    const { container } = renderInteractiveGrid([idleSweepWithCommits], { onSaveTimerSchedule });
    openConfig(container);
    const input = within(container).getByLabelText(`${t.timerTimeLabel} doc-sweep`) as HTMLInputElement;
    const timeControls = input.parentElement;
    expect(timeControls).not.toBeNull();
    const save = within(timeControls!).getByRole("button", { name: t.timerSave }) as HTMLButtonElement;
    expect(input.value).toBe("02:07");
    expect(save.disabled).toBe(true);

    fireEvent.change(input, { target: { value: "03:45" } });
    expect(save.disabled).toBe(false);
    fireEvent.click(save);
    expect(onSaveTimerSchedule).toHaveBeenCalledWith("doc-sweep", "03:45");
  });

  it("erklärt bei deaktiviertem Timer, welche Uhrzeit beim Aktivieren gilt", () => {
    const { container } = renderInteractiveGrid([idleSweepWithCommits]);
    openConfig(container);
    const input = within(container).getByLabelText(`${t.timerTimeLabel} doc-sweep`);
    const timerPanel = input.parentElement?.parentElement?.parentElement;
    expect(timerPanel).not.toBeNull();
    expect(within(timerPanel!).getByText(t.timerDisabledHint("02:07"))).toBeTruthy();
  });
});

describe("LoopsGrid — Nachtmodelle", () => {
  // Idle-Pipeline mit plan/build/verify — Night-Modellwahl pro Phase + Rollenwechsel.
  const nightPipeline: LoopPack = {
    ...runningPipeline,
    name: "builder-reviewer-idle",
    running: false,
    heartbeat: null,
    commits_ahead: 0,
  };

  it("lädt Night-Overrides und speichert Diffs über denselben LoopModelPicker", async () => {
    getLoopNightOverrides.mockResolvedValueOnce({
      pack: "builder-reviewer-idle",
      overrides: { PHASE_PLAN_ENGINE: "kimi", PHASE_PLAN_MODEL: "k3" },
    });
    putLoopNightOverrides.mockResolvedValueOnce({
      pack: "builder-reviewer-idle",
      overrides: {
        PHASE_PLAN_ENGINE: "kimi",
        PHASE_PLAN_MODEL: "k3",
        PHASE_BUILD_ENGINE: "alibaba-token-plan",
        PHASE_BUILD_MODEL: "qwen3.8-max-preview",
      },
      ok: true,
    });

    const { container } = renderInteractiveGrid([nightPipeline]);
    openConfig(container);
    await waitFor(() => {
      expect(getLoopNightOverrides).toHaveBeenCalledWith("builder-reviewer-idle");
    });

    const night = within(container).getByTestId("night-models-builder-reviewer-idle");
    expect(within(night).getByText(t.nightModelsTitle)).toBeTruthy();
    // Reuses LoopModelPicker trigger labels (chooseModel(phase)).
    const planTrigger = within(night).getByRole("button", { name: t.chooseModel("plan") });
    expect(planTrigger.textContent).toContain("k3");

    const buildTrigger = within(night).getByRole("button", { name: t.chooseModel("build") });
    fireEvent.click(buildTrigger);
    const option = await within(night).findByRole("button", { name: /qwen3\.8-max-preview/i });
    fireEvent.click(option);

    const saveBtn = within(night).getByRole("button", { name: t.nightModelsSave });
    await waitFor(() => {
      expect((saveBtn as HTMLButtonElement).disabled).toBe(false);
    });
    fireEvent.click(saveBtn);

    await waitFor(() => {
      expect(putLoopNightOverrides).toHaveBeenCalled();
    });
    const [packName, payload] = putLoopNightOverrides.mock.calls.at(-1)!;
    expect(packName).toBe("builder-reviewer-idle");
    expect(payload.PHASE_PLAN_ENGINE).toBe("kimi");
    expect(payload.PHASE_PLAN_MODEL).toBe("k3");
    expect(payload.PHASE_BUILD_ENGINE).toBe("alibaba-token-plan");
    expect(payload.PHASE_BUILD_MODEL).toBe("qwen3.8-max-preview");
    await waitFor(() => {
      expect(within(night).getByRole("status").textContent).toContain(t.nightModelsSaved);
    });
  });

  it("zeigt Manual-Land-Hinweis bei Engine-Rollenwechsel und Reset löscht die Ressource", async () => {
    getLoopNightOverrides.mockResolvedValueOnce({
      pack: "builder-reviewer-idle",
      overrides: { PHASE_PLAN_ENGINE: "kimi", PHASE_PLAN_MODEL: "k3" },
    });
    putLoopNightOverrides.mockResolvedValueOnce({ pack: "builder-reviewer-idle", overrides: {}, ok: true });

    const { container } = renderInteractiveGrid([nightPipeline]);
    openConfig(container);
    await waitFor(() => {
      expect(within(container).getByTestId("night-models-manual-land-builder-reviewer-idle")).toBeTruthy();
    });

    const night = within(container).getByTestId("night-models-builder-reviewer-idle");
    expect(within(night).getByTestId("night-models-manual-land-builder-reviewer-idle").textContent).toContain(
      t.nightModelsManualLand,
    );

    const resetBtn = within(night).getByRole("button", { name: t.nightModelsReset });
    expect((resetBtn as HTMLButtonElement).disabled).toBe(false);
    fireEvent.click(resetBtn);
    await waitFor(() => {
      expect(putLoopNightOverrides).toHaveBeenCalledWith("builder-reviewer-idle", {});
    });
  });

  it("zeigt Load-Fehler aus dem GET", async () => {
    getLoopNightOverrides.mockRejectedValueOnce(new Error("boom"));
    const { container } = renderInteractiveGrid([nightPipeline]);
    openConfig(container);
    await waitFor(() => {
      const night = within(container).getByTestId("night-models-builder-reviewer-idle");
      expect(within(night).getByRole("alert").textContent).toContain(t.nightModelsLoadFailed);
    });
  });
});

describe("LoopsGrid — Live-Phase-Chip (heartbeat)", () => {
  it("shows the current phase/model/elapsed duration for a running pack with heartbeat.current", () => {
    const startedMs = Date.parse(runningPipeline.heartbeat!.current!.started_at);
    const nowMs = startedMs + 8 * 60_000; // 8 Minuten seit Phasenstart
    const html = renderGrid([runningPipeline], { nowMs });
    expect(html).toContain(t.heartbeatCurrent("build", "claude-sonnet-5", "8m"));
  });

  it("discloses a current heartbeat older than 30 seconds as last-known telemetry", () => {
    const startedMs = Date.parse(runningPipeline.heartbeat!.current!.started_at);
    const html = renderGrid([runningPipeline], { nowMs: startedMs + 31_000 });
    expect(html.split(t.heartbeatStale("31s")).length - 1).toBe(2); // hero + card
  });

  it.each([
    ["garbage", "not-a-date"],
    ["missing timezone", "2026-07-13T08:00:00"],
    ["millisecond number", Date.parse("2026-07-13T08:00:00Z")],
    ["future", "2026-07-13T09:00:00Z"],
  ])("discloses an invalid %s phase timestamp instead of claiming seit 0s", (_label, startedAt) => {
    const malformed = {
      ...runningPipeline,
      heartbeat: {
        ...runningPipeline.heartbeat!,
        current: { ...runningPipeline.heartbeat!.current!, started_at: startedAt },
      },
    } as unknown as LoopPack;
    const html = renderGrid([malformed], { nowMs: Date.parse("2026-07-13T08:00:00Z") });
    expect(html).toContain("Zeitstempel ungültig");
    expect(html).not.toContain(t.heartbeatCurrent("build", "claude-sonnet-5", "0s"));
  });

  it("shows 'zwischen Phasen' when running but heartbeat.current is null", () => {
    const html = renderGrid([betweenPhasesPipeline]);
    expect(html).toContain(t.heartbeatBetweenPhases);
    expect(html).not.toContain(t.heartbeatCurrent("build", "claude-sonnet-5", "8m"));
  });

  it("renders the last ≤5 phases as duration-history chips, most recent first, marked by rc", () => {
    const html = renderGrid([runningPipeline]);
    expect(html).toContain("build 512s ✓");
    expect(html).toContain("verify 178s ✗");
  });

  it("shows no heartbeat chip for an idle pack", () => {
    const html = renderGrid([idleSweepWithCommits]);
    expect(html).not.toContain(t.heartbeatBetweenPhases);
  });
});

describe("LoopsGrid — Land-Button-Sichtbarkeit", () => {
  it("shows Land for an idle pack with unverdaute commits", () => {
    const html = renderGrid([idleSweepWithCommits]);
    expect(html).toContain(t.actions.land);
  });

  it("uses action styling instead of status green for Land and its confirmation", () => {
    const { unmount } = renderInteractiveGrid([idleSweepWithCommits]);
    const landButton = screen.getByRole("button", { name: t.actions.land });
    expect(landButton.getAttribute("style") ?? "").not.toContain("var(--ln-ok)");
    unmount();

    renderInteractiveGrid([idleSweepWithCommits], { pendingLandPack: idleSweepWithCommits.name });
    const confirmButton = screen.getByRole("button", { name: t.confirmYes });
    expect(confirmButton.getAttribute("style") ?? "").not.toContain("var(--ln-ok)");
  });

  it("hides Land while the pack is running, even with commits_ahead > 0", () => {
    const html = renderGrid([runningWithCommits]);
    expect(html).not.toContain(t.actions.land);
  });

  it("hides Land for an idle pack without commits_ahead", () => {
    const html = renderGrid([idleNoCommits]);
    expect(html).not.toContain(t.actions.land);
  });

  it("hides Land for a pipeline whose ahead commits have no verified plan", () => {
    const html = renderGrid([bouncedPipelineWithHistoryOnlyCommits]);
    expect(html).not.toContain(t.actions.land);
    expect(html).not.toContain(t.commitsAhead(2));
    expect(html).toContain(t.commitsUnverified(2));
  });

  it("labels an idle pipeline with a stranded building plan as interrupted", () => {
    const stranded = {
      ...bouncedPipelineWithHistoryOnlyCommits,
      name: "stranded-build",
      queue: { "00-planned": 0, "10-building": 1, "20-verified": 0, "30-landed": 0, "90-bounced": 0 },
      commits_ahead: 0,
    };
    const html = renderGrid([stranded]);
    expect(html).toContain(t.statusInterrupted);
    expect(html).not.toContain(t.statusIdle);
  });
});

describe("LoopsGrid — Werkstatt-Panel", () => {
  const repoFiles: LoopFilesResponse = {
    pack: "builder-reviewer",
    source: "repo",
    files: [
      { name: "pack.yaml", content: "name: builder-reviewer\ntype: pipeline\n", editable: false },
      { name: "build.md", content: "PHASE=build STATE={{STATE_DIR}}\n", editable: false },
    ],
  };
  const customFiles: LoopFilesResponse = {
    pack: "doc-sweep",
    source: "custom",
    files: [
      { name: "pack.yaml", content: "name: doc-sweep\ntype: sweep\n", editable: true },
      { name: "round.md", content: "PHASE=round STATE={{STATE_DIR}}\n", editable: true },
    ],
  };

  it("renders repo-pack files read-only with the 'via Git ändern' hint, no Save button", () => {
    const files: LoopFilesResponse = {
      pack: "builder-reviewer",
      source: "repo",
      files: [{ name: "pack.yaml", content: "name: builder-reviewer\n", editable: false }],
    };
    const html = renderGrid([runningPipeline], { workshopOpenPack: "builder-reviewer", files });
    expect(html).toContain(t.workshopReadOnly);
    expect(html).not.toContain(t.workshopSave);
    expect(html).toContain("pack.yaml");
  });

  it("renders custom-pack files editable with a Save button, no read-only hint", () => {
    const html = renderGrid([idleSweepWithCommits], { workshopOpenPack: "doc-sweep", files: customFiles });
    expect(html).toContain(t.workshopSave);
    expect(html).not.toContain(t.workshopReadOnly);
  });

  it("does not render the Werkstatt panel for a pack whose workshop isn't open", () => {
    const html = renderGrid([runningPipeline], { workshopOpenPack: null, files: repoFiles });
    expect(html).not.toContain(t.workshopReadOnly);
    expect(html).not.toContain("build.md");
  });
});

describe("LoopsGrid — Nachtschicht-Redesign: Loop-Ring (Signatur-Element)", () => {
  it("renders a running-state ring for a running pipeline pack", () => {
    const html = renderGrid([runningPipeline]);
    expect(html).toMatch(/data-testid="loop-ring"[^>]*data-state="running"/);
  });

  it("renders an idle-state ring for an idle pack", () => {
    const html = renderGrid([idleSweepWithCommits]);
    expect(html).toMatch(/data-testid="loop-ring"[^>]*data-state="idle"/);
  });

  it("renders an error-state ring for a manifest-error pack", () => {
    const html = renderGrid([brokenPack]);
    expect(html).toMatch(/data-testid="loop-ring"[^>]*data-state="error"/);
  });
});

describe("LoopsGrid — Nachtschicht-Redesign: Source-Badge (control_loops.py:220)", () => {
  it("shows the Repo badge for a curated pack", () => {
    const html = renderGrid([runningPipeline]);
    expect(html).toContain(t.sourceRepo);
  });

  it("shows the Custom badge for a duplicated pack", () => {
    const html = renderGrid([customPack]);
    expect(html).toContain(t.sourceCustom);
  });
});

describe("LoopsGrid — Nachtschicht-Redesign: Lagebild-Hero", () => {
  it("shows the sleeping-crew statement when no pack is running", () => {
    const html = renderGrid([idleSweepWithCommits, idleNoCommits]);
    expect(html).toContain(t.heroSleeping);
    expect(html).toMatch(/data-testid="loops-hero"[^>]*data-state="sleeping"/);
  });

  it("shows the running pack's name and phase in the hero when a pack is running", () => {
    const html = renderGrid([runningPipeline]);
    expect(html).toMatch(/data-testid="loops-hero"[^>]*data-state="running"/);
    // Pack-Name erscheint doppelt (Hero + Karte) — mindestens einmal reicht als Beleg.
    expect(html).toContain("builder-reviewer");
    expect(html).not.toContain(t.heroSleeping);
  });

  it("shows a '+n weitere laufen' chip when more than one pack is running", () => {
    const secondRunning: LoopPack = { ...runningPipeline, name: "second-runner" };
    const html = renderGrid([runningPipeline, secondRunning]);
    expect(html).toContain(t.heroMoreRunning(1));
  });

  it("reports the timer count in the sleeping hero from timer_enabled packs", () => {
    const withTimer: LoopPack = { ...idleSweepWithCommits, timer_enabled: true };
    const html = renderGrid([withTimer]);
    expect(html).toContain(t.heroTimerActive(1));
  });

  it("shows 'Kein Timer aktiv' when no idle pack has its timer enabled", () => {
    const html = renderGrid([idleNoCommits]);
    expect(html).toContain(t.heroTimerNone);
  });
});

describe("LoopsGrid — Nachtschicht-Redesign: Logbuch (Ledger-Timeline)", () => {
  // Zeile 1:1 aus loops/runner.py::LoopRunner.ledger() rekonstruiert (siehe
  // loopLedger.ts-Kommentar) — verifiziert der Parser hier im UI-Kontext.
  const detail: LoopDetailResponse = {
    ...(runningPipeline as LoopPackSummary),
    escalations_tail: [],
    ledger_tail: [
      "- 2026-07-03 07:14 R1 ✅ P1-repo-housekeeper-dead-code-sweep.md verified (a1b2c3d4e) [build 812s · verify 340s]",
      "# LEDGER — ein fremdes/kaputtes Format, das nicht crashen darf",
    ],
    queue_entries: null,
    commits: [],
    overrides: {},
    phase_usage: [
      { ts: "2026-07-13T01:00:00Z", round: 1, phase: "build", engine: "xai", model: "grok-4.5", total_tokens: 270, input_tokens: 220, cached_input_tokens: 180, output_tokens: 50, reasoning_tokens: 40, billing: "subscription", metered_cost_eur: 0 },
    ],
  };

  it("renders a parsed ledger line's raw text and round/phase chips inside the open Logbuch disclosure", () => {
    const html = renderGrid([runningPipeline], { selectedPack: "builder-reviewer", detail });
    expect(html).toContain("P1-repo-housekeeper-dead-code-sweep.md verified (a1b2c3d4e)");
    expect(html).toContain("R1");
    expect(html).toContain("verify");
  });

  it("renders an unparsable ledger line verbatim instead of crashing", () => {
    const html = renderGrid([runningPipeline], { selectedPack: "builder-reviewer", detail });
    expect(html).toContain("# LEDGER — ein fremdes/kaputtes Format, das nicht crashen darf");
  });

  it("renders per-round phase tokens in detail", () => {
    const html = renderGrid([runningPipeline], { selectedPack: "builder-reviewer", detail });
    expect(html).toContain("R1 · build · grok-4.5 · 270 Tokens · Abo · €0 gemessen");
  });

  it("loads a bounced queue file on expand and renders its body read-only", async () => {
    const bouncedDetail: LoopDetailResponse = {
      ...detail,
      queue_entries: { "90-bounced": ["P2-reader-feedback.md"] },
    };
    const fetchMock = vi.fn().mockResolvedValue(new Response(JSON.stringify({
      pack: "builder-reviewer",
      stage: "90-bounced",
      filename: "P2-reader-feedback.md",
      content: "## Verifier-Evidence\nFAIL: bounce reason remains visible",
    }), { status: 200, headers: { "Content-Type": "application/json" } }));
    vi.stubGlobal("fetch", fetchMock);

    renderInteractiveGrid([runningPipeline], { selectedPack: "builder-reviewer", detail: bouncedDetail });
    const fileTrigger = screen.getByRole("button", { name: /P2-reader-feedback\.md/ });
    fireEvent.click(fileTrigger);

    expect(await screen.findByText(/FAIL: bounce reason remains visible/)).toBeTruthy();
    expect(fetchMock).toHaveBeenCalledWith(
      "/api/loops/builder-reviewer/queue/90-bounced/P2-reader-feedback.md",
      expect.objectContaining({ credentials: "include" }),
    );
    const panel = document.getElementById(fileTrigger.getAttribute("aria-controls") ?? "");
    expect(panel).not.toBeNull();
    expect(within(panel as HTMLElement).queryByRole("textbox")).toBeNull();
    expect(within(panel as HTMLElement).queryByRole("button", { name: /speichern|save/i })).toBeNull();
  });
});

// ── Runden-Fenster der Ring-Ableitung (Codex-Review-Befund 2026-07-03) ───────
// `heartbeat.last` ist ein rollierendes 20er-Fenster OHNE Runden-IDs — alte
// build/verify/round-Einträge früherer Runden dürfen nicht als Fortschritt der
// aktuellen Runde erscheinen. Runden-Grenze Pipeline: der letzte `plan`-Eintrag.
describe("deriveRingSegments — nur die aktuelle Runde zählt", () => {
  const hbEntry = (phase: string, rc: number, at: string) =>
    ({ phase, engine: "claude", model: "claude-sonnet-5", secs: 100, rc, at });

  const withHeartbeat = (current: LoopHeartbeatCurrent | null, last: ReturnType<typeof hbEntry>[]): LoopPackSummary => ({
    ...(runningPipeline as LoopPackSummary),
    heartbeat: { current, last },
  });

  const NOW = Date.parse("2026-07-03T08:00:00Z");

  it("zählt verify der VORHERIGEN Runde nicht als done, wenn Runde 2 in build steht", () => {
    const pack = withHeartbeat(
      { phase: "build", engine: "claude", model: "claude-sonnet-5", started_at: "2026-07-03T07:55:00Z", timeout: 3600 },
      [
        // Runde 1 (komplett, alles grün):
        hbEntry("plan", 0, "2026-07-03T06:00:00Z"),
        hbEntry("build", 0, "2026-07-03T06:20:00Z"),
        hbEntry("verify", 0, "2026-07-03T06:40:00Z"),
        // Runde 2 (nur plan bisher):
        hbEntry("plan", 0, "2026-07-03T07:50:00Z"),
      ],
    );
    const segs = deriveRingSegments(pack, NOW);
    expect(segs.find((s) => s.key === "plan")?.state).toBe("done");
    expect(segs.find((s) => s.key === "build")?.state).toBe("current");
    expect(segs.find((s) => s.key === "verify")?.state).toBe("pending");
  });

  it("startet mit leerem Ring, wenn gerade eine neue Runde plant (History = Vergangenheit)", () => {
    const pack = withHeartbeat(
      { phase: "plan", engine: "claude", model: "claude-fable-5", started_at: "2026-07-03T07:59:00Z", timeout: 2400 },
      [hbEntry("plan", 0, "2026-07-03T06:00:00Z"), hbEntry("build", 0, "2026-07-03T06:20:00Z"), hbEntry("verify", 0, "2026-07-03T06:40:00Z")],
    );
    const segs = deriveRingSegments(pack, NOW);
    expect(segs.find((s) => s.key === "plan")?.state).toBe("current");
    expect(segs.find((s) => s.key === "build")?.state).toBe("pending");
    expect(segs.find((s) => s.key === "verify")?.state).toBe("pending");
  });

  it("zeigt im Leerlauf das Ergebnis der LETZTEN Runde (Fenster ab letztem plan)", () => {
    const pack = withHeartbeat(null, [
      hbEntry("verify", 1, "2026-07-03T05:00:00Z"), // ältere, rote Runde — zählt nicht
      hbEntry("plan", 0, "2026-07-03T06:00:00Z"),
      hbEntry("build", 0, "2026-07-03T06:20:00Z"),
      hbEntry("verify", 0, "2026-07-03T06:40:00Z"),
    ]);
    const segs = deriveRingSegments(pack, NOW);
    expect(segs.every((s) => s.state === "done")).toBe(true);
  });

  it("bleibt konservativ pending, wenn kein plan-Eintrag im Fenster liegt", () => {
    const pack = withHeartbeat(null, [hbEntry("build", 0, "2026-07-03T06:20:00Z"), hbEntry("verify", 0, "2026-07-03T06:40:00Z")]);
    const segs = deriveRingSegments(pack, NOW);
    expect(segs.every((s) => s.state === "pending")).toBe(true);
  });
});

describe("deriveRingTicks — nur der hintere zusammenhängende round-Block", () => {
  const hbEntry = (phase: string, rc: number) =>
    ({ phase, engine: "claude", model: "claude-sonnet-5", secs: 100, rc, at: "2026-07-03T06:00:00Z" });

  it("zählt rounds vor einer Pipeline-Phase nicht mit", () => {
    const pack: LoopPackSummary = {
      ...(idleSweepWithCommits as LoopPackSummary),
      heartbeat: {
        current: null,
        last: [hbEntry("round", 0), hbEntry("plan", 0), hbEntry("round", 0), hbEntry("round", 0)],
      },
    };
    expect(deriveRingTicks(pack).done).toBe(2);
  });

  it("zählt fehlgeschlagene rounds im Block nicht als done, bricht den Block aber nicht ab", () => {
    const pack: LoopPackSummary = {
      ...(idleSweepWithCommits as LoopPackSummary),
      heartbeat: { current: null, last: [hbEntry("round", 0), hbEntry("round", 1), hbEntry("round", 0)] },
    };
    expect(deriveRingTicks(pack).done).toBe(2);
  });
});

// Idle-Pipeline für das Start-Formular: SKIP_PLAN gibt es nur bei Packs MIT
// Planungsphase — Sweeps zeigen die Checkbox gar nicht erst.
const idlePipeline: LoopPack = {
  ...runningPipeline,
  running: false,
  heartbeat: null,
  timer_enabled: false,
  timer_next_run: null,
};

describe("LoopStartForm — SKIP_PLAN-Override", () => {
  it("filters the complete model catalog and applies engine plus model to one phase", async () => {
    const onSubmitStart = vi.fn();
    renderInteractiveGrid([idlePipeline], {
      startOpenPack: idlePipeline.name,
      onSubmitStart,
    });

    fireEvent.click(screen.getByRole("button", { name: "Modell für build auswählen" }));
    fireEvent.change(screen.getByRole("searchbox", { name: "Modelle durchsuchen" }), {
      target: { value: "kimi-k2.7" },
    });
    fireEvent.click(screen.getByRole("button", { name: /kimi-k2\.7-code/ }));
    const buildTrigger = screen.getByRole("button", { name: "Modell für build auswählen" });
    expect(buildTrigger.getAttribute("aria-expanded")).toBe("false");
    await waitFor(() => expect(document.activeElement).toBe(buildTrigger));
    fireEvent.click(screen.getByRole("button", { name: t.submitStart }));

    expect(onSubmitStart).toHaveBeenCalledWith(idlePipeline.name, {
      PHASE_PLAN_ENGINE: "claude",
      PHASE_PLAN_MODEL: "claude-fable-5",
      PHASE_BUILD_ENGINE: "neuralwatt",
      PHASE_BUILD_MODEL: "kimi-k2.7-code",
      PHASE_VERIFY_ENGINE: "claude",
      PHASE_VERIFY_MODEL: "claude-fable-5",
    });
  });

  it("exposes the active repository filter to assistive technology", () => {
    const healthTrack: LoopPack = {
      ...idleSweepWithCommits,
      name: "health-track-ux",
      repo: "/home/piet/projects/health-track",
    };
    renderInteractiveGrid([idlePipeline, healthTrack]);

    const filter = screen.getByRole("button", { name: "health-track" });
    expect(filter.getAttribute("aria-pressed")).toBe("false");
    fireEvent.click(filter);
    expect(filter.getAttribute("aria-pressed")).toBe("true");
    expect(screen.queryByText("builder-reviewer")).toBeNull();
    expect(screen.getByText("health-track-ux")).not.toBeNull();
  });

  it("zeigt die Checkbox bei Sweep-Packs (keine Planungsphase) gar nicht", () => {
    render(
      <LoopsGrid
        packs={[idleSweepWithCommits]}
        models={models}
        selectedPack={null}
        detail={null}
        detailLoading={false}
        detailError={null}
        busyPack={null}
        actionErrorByPack={{}}
        landNoteByPack={{}}
        startOpenPack={idleSweepWithCommits.name}
        pendingStopPack={null}
        pendingLandPack={null}
        workshopOpenPack={null}
        files={null}
        filesLoading={false}
        filesError={null}
        fileSaveBusy={false}
        fileSaveError={null}
        duplicateBusy={false}
        duplicateError={null}
        {...noopHandlers}
      />,
    );
    expect(screen.queryByLabelText(t.skipPlanLabel)).toBeNull();
  });

  it("zeigt für Autoland nur den UI-Laufvertrag und keine gefährlichen Overrides", () => {
    const parsed = LoopsResponseSchema.parse({
      packs: [{ ...idlePipeline, name: "dashboard-experience", autoland: true }],
    }).packs[0] as LoopPack;
    const html = renderGrid([parsed], { startOpenPack: parsed.name });

    expect(html).toContain("Einmaliger Laufvertrag");
    expect(html).not.toContain(t.skipPlanLabel);
    expect(html).not.toContain(t.paramLabel);
  });

  it("sendet den Autoland-Laufvertrag mit frei gewählten UI-Budgets", () => {
    const onSubmitStart = vi.fn();
    const pack = { ...idlePipeline, name: "dashboard-experience", autoland: true };
    render(
      <LoopsGrid
        packs={[pack]}
        models={models}
        selectedPack={null}
        detail={null}
        detailLoading={false}
        detailError={null}
        busyPack={null}
        actionErrorByPack={{}}
        landNoteByPack={{}}
        startOpenPack={pack.name}
        pendingStopPack={null}
        pendingLandPack={null}
        workshopOpenPack={null}
        files={null}
        filesLoading={false}
        filesError={null}
        fileSaveBusy={false}
        fileSaveError={null}
        duplicateBusy={false}
        duplicateError={null}
        {...noopHandlers}
        onSubmitStart={onSubmitStart}
      />,
    );
    const roundInputs = screen.getAllByLabelText(t.maxRoundsLabel);
    const hourInputs = screen.getAllByLabelText(t.maxHoursLabel);
    fireEvent.change(roundInputs[roundInputs.length - 1], { target: { value: "15" } });
    fireEvent.change(hourInputs[hourInputs.length - 1], { target: { value: "4" } });
    const submitButtons = screen.getAllByRole("button", { name: t.submitStart });
    fireEvent.click(submitButtons[submitButtons.length - 1]);

    expect(onSubmitStart).toHaveBeenCalledWith(pack.name, {
      MAX_ROUNDS: "15",
      MAX_HOURS: "4",
      PHASE_PLAN_ENGINE: "claude",
      PHASE_PLAN_MODEL: "claude-fable-5",
      PHASE_BUILD_ENGINE: "claude",
      PHASE_BUILD_MODEL: "claude-sonnet-5",
      PHASE_VERIFY_ENGINE: "claude",
      PHASE_VERIFY_MODEL: "claude-fable-5",
    });
  });

  it("setzt SKIP_PLAN=1 in overrides, wenn die Checkbox angehakt ist", () => {
    const onSubmitStart = vi.fn();
    render(
      <LoopsGrid
        packs={[idlePipeline]}
        models={models}
        selectedPack={null}
        detail={null}
        detailLoading={false}
        detailError={null}
        busyPack={null}
        actionErrorByPack={{}}
        landNoteByPack={{}}
        startOpenPack={idlePipeline.name}
        pendingStopPack={null}
        pendingLandPack={null}
        workshopOpenPack={null}
        files={null}
        filesLoading={false}
        filesError={null}
        fileSaveBusy={false}
        fileSaveError={null}
        duplicateBusy={false}
        duplicateError={null}
        {...noopHandlers}
        onSubmitStart={onSubmitStart}
      />,
    );
    fireEvent.click(screen.getByLabelText(t.skipPlanLabel));
    const submitButtons = screen.getAllByRole("button", { name: t.submitStart });
    fireEvent.click(submitButtons[submitButtons.length - 1]);
    expect(onSubmitStart).toHaveBeenCalledTimes(1);
    const [, overrides] = onSubmitStart.mock.calls[0];
    expect(overrides.SKIP_PLAN).toBe("1");
  });

  it("lässt SKIP_PLAN weg, wenn die Checkbox nicht angehakt ist", () => {
    const onSubmitStart = vi.fn();
    render(
      <LoopsGrid
        packs={[idlePipeline]}
        models={models}
        selectedPack={null}
        detail={null}
        detailLoading={false}
        detailError={null}
        busyPack={null}
        actionErrorByPack={{}}
        landNoteByPack={{}}
        startOpenPack={idlePipeline.name}
        pendingStopPack={null}
        pendingLandPack={null}
        workshopOpenPack={null}
        files={null}
        filesLoading={false}
        filesError={null}
        fileSaveBusy={false}
        fileSaveError={null}
        duplicateBusy={false}
        duplicateError={null}
        {...noopHandlers}
        onSubmitStart={onSubmitStart}
      />,
    );
    const submitButtons = screen.getAllByRole("button", { name: t.submitStart });
    fireEvent.click(submitButtons[submitButtons.length - 1]);
    expect(onSubmitStart).toHaveBeenCalledTimes(1);
    const [, overrides] = onSubmitStart.mock.calls[0];
    expect(overrides.SKIP_PLAN).toBeUndefined();
  });
});

describe("Loops — kein Remote-Font-Loader (W3-5 Director-Fix, Codex-P1-gehärtet)", () => {
  it("Quelltext-Guard: LoopsView enthält keinen Google-Fonts-Loader mehr (W1-A: zero network font requests)", () => {
    // Quell-Ebene statt Komponenten-Mount: der gelöschte Loader lebte in
    // LoopsView (nicht LoopsGrid) — ein DOM-Test auf LoopsGrid wäre gegen
    // Wiedereinführung blind (Codex-P1). Der String-Guard fängt jede Stelle
    // der Datei, unabhängig vom Komponentenpfad.
    const src = loopsViewSource;
    expect(src).not.toContain("fonts.googleapis");
    expect(src).not.toContain("useNightFontInjection");
  });

  it("DOM-Guard: gerendertes Grid injiziert keinen Font-Link", () => {
    renderInteractiveGrid([runningPipeline]);
    expect(document.getElementById("loops-night-font")).toBeNull();
    const links = Array.from(document.querySelectorAll("link[rel='stylesheet']"));
    expect(links.filter((l) => (l.getAttribute("href") ?? "").includes("fonts.googleapis"))).toHaveLength(0);
  });
});

describe("Loops — Engine-Datenpalette (W6-4)", () => {
  it("mappt Engine-Identität ausschließlich auf data-N-Tokens ohne Rohhex", () => {
    const engineColors = loopsViewSource.match(
      /const ENGINE_COLOR: Record<string, string> = \{([\s\S]*?)\};/,
    )?.[1];

    expect(engineColors).toBeTruthy();
    expect(engineColors).not.toMatch(/#[0-9a-f]{6}/i);
    expect(engineColors).toContain('claude: "var(--color-data-1)"');
    expect(engineColors).toContain('codex: "var(--color-data-4)"');
    expect(engineColors).toContain('kimi: "var(--color-data-5)"');
    expect(engineColors).toContain('hermes: "var(--color-data-2)"');
  });
});

describe("Loops — Nacht auf Graphit (W4-8)", () => {
  it("entfernt den Navy-Fork und leitet NIGHT_VARS ausschließlich aus Sheet-Tokens ab", () => {
    const bannedNavy = [
      "#060913",
      "#0D1322",
      "#141C31",
      "#1E2A47",
      "#1A2344",
      "#FFB454",
      "#34C383",
      "#E66767",
      "#C98500",
    ];
    for (const literal of bannedNavy) {
      expect(loopsViewSource.toLowerCase()).not.toContain(literal.toLowerCase());
    }

    const nightVars = loopsViewSource.match(/const NIGHT_VARS = \{([\s\S]*?)\} as React\.CSSProperties;/)?.[1];
    expect(nightVars).toBeTruthy();
    expect(nightVars).not.toMatch(/#[0-9a-f]{3,8}/i);
    for (const token of [
      "--color-surface-0",
      "--color-surface-1",
      "--color-surface-2",
      "--color-line",
      "--color-ink",
      "--color-ink-2",
      "--color-ink-3",
      "--color-bronze",
      "--color-bronze-hi",
      "--color-status-ok",
      "--color-status-alert",
      "--color-status-warn",
    ]) {
      expect(nightVars).toContain(`var(${token})`);
    }
  });
});

describe("Workshop-Datei-Tabs — Touch-Target-Boden (W3-5 Codex-P1)", () => {
  it("gibt den Datei-Tab-Buttons min-h-12 (44px-AC; py-2 ergab nur ~30px)", () => {
    const files: LoopFilesResponse = {
      pack: "builder-reviewer",
      source: "repo",
      files: [{ name: "pack.yaml", content: "name: builder-reviewer\n", editable: false }],
    };
    const html = renderGrid([runningPipeline], { workshopOpenPack: "builder-reviewer", files });
    const tab = html.match(/<button[^>]*>[^<]*pack\.yaml/);
    expect(tab).not.toBeNull();
    expect(tab![0]).toContain("min-h-12");
  });
});

// ── Landing-Pack (deterministic, LL2-S5) ─────────────────────────────────────
// Vertrag: control_loops._landing_control_payload (Summary) + pack_detail
// (Drawer). Karte: Automatik-Schalter, Queue, Baseline, Trigger, Ergebnis,
// Vorschau. Drawer: Kandidaten, Gate-Stufen, Recovery, Trigger-Historie.
const landingPack: LoopPackSummary = {
  ...idleSweepWithCommits,
  name: "landing",
  type: "deterministic",
  queue: null,
  commits_ahead: 0,
  timer_enabled: false,
  timer_next_run: null,
  automation_enabled: false,
  baseline_sha: "abc1234def5678",
  baseline_ok: true,
  baseline_reason: "Landing-Gate-Nachweis für aktuellen main-SHA",
  baseline_source: "landing",
  queue_summary: { total: 2, landed: 1, cleaned: 0, parked: 0 },
  next_trigger_at: "2026-07-31T01:00:00+00:00",
  last_result: "held",
  collection_window: null,
  candidates: [
    { branch: "loop/t_abc123", head: "deadbeefcafe01", ahead: 3, behind: 1 },
    { branch: "loop/manual", head: null, ahead: 1, behind: 0 },
  ],
} as LoopPackSummary;

const landingDetail: LoopDetailResponse = {
  ...landingPack,
  ledger_tail: [],
  escalations_tail: [],
  queue_entries: null,
  commits: [],
  overrides: {},
  phase_usage: [],
  gate_stages: ["baseline", "affected", "full", "post_merge"],
  trigger_history: ["review:t_1", "ready:t_2"],
  followup_pending: true,
  recovery: [
    {
      task_id: "t_abc123",
      fingerprint: "fp-deadbeef1234",
      failure_class: "verify",
      failing_gate: "affected",
      candidate_commit: "cafe123",
      state: "started",
      requested_at: "2026-07-31T00:00:00+00:00",
      started_at: "2026-07-31T00:01:00+00:00",
    },
  ],
  preview: {
    running: false,
    started_at: "2026-07-31T00:05:00+00:00",
    finished_at: "2026-07-31T00:06:00+00:00",
    rc: 0,
    output_tail: "Landing-Loop dry-run: 2 Kandidaten",
  },
};

/** Drawer-Inhalte enthalten react-router-Links (Ursprungskarten) — daher
 *  immer unter einem MemoryRouter rendern. */
function renderLandingDetailGrid(detail: LoopDetailResponse) {
  return renderToStaticMarkup(
    <MemoryRouter>
      <LoopsGrid
        packs={[landingPack]}
        models={models}
        selectedPack="landing"
        detail={detail}
        detailLoading={false}
        detailError={null}
        busyPack={null}
        actionErrorByPack={{}}
        landNoteByPack={{}}
        startOpenPack={null}
        pendingStopPack={null}
        pendingLandPack={null}
        workshopOpenPack={null}
        files={null}
        filesLoading={false}
        filesError={null}
        fileSaveBusy={false}
        fileSaveError={null}
        duplicateBusy={false}
        duplicateError={null}
        {...noopHandlers}
      />
    </MemoryRouter>,
  );
}

describe("LoopsGrid — Landing-Pack-Karte (LL2-S5, AC-3)", () => {
  it("rendert Automatik-Schalter aus (fail-closed Default), Queue, Baseline, Trigger, Ergebnis", () => {
    const html = renderGrid([landingPack]);
    expect(html).toContain('role="switch"');
    expect(html).toContain('aria-checked="false"');
    expect(html).toContain("Automatik: aus");
    expect(html).toContain("2 · gelandet 1 · bereinigt 0 · geparkt 0");
    expect(html).toContain("abc1234");
    expect(html).toContain("grün");
    expect(html).toContain("Landing: Landing-Gate-Nachweis für aktuellen main-SHA");
    expect(html).toContain("zurückgehalten");
    expect(html).toContain("Vorschau");
  });

  it("zeigt eingeschaltete Automatik als aria-checked mit Textlabel", () => {
    const html = renderGrid([{ ...landingPack, automation_enabled: true }]);
    expect(html).toContain('aria-checked="true"');
    expect(html).toContain("Automatik: an");
  });

  it("zeigt fehlgeschlagene Baseline als Text (nicht farballeinig)", () => {
    const html = renderGrid([{ ...landingPack, baseline_ok: false }]);
    expect(html).toContain("nicht grün");
  });

  it("erfindet für ältere Payloads keinen Baseline-Nachweis", () => {
    const html = renderGrid([
      { ...landingPack, baseline_reason: undefined, baseline_source: undefined },
    ]);
    expect(html).not.toContain("Landing-Gate-Nachweis für aktuellen main-SHA");
    expect(html).not.toContain("Nachweis</dt>");
  });

  it("Switch-Klick meldet den Zielzustand — Server-Confirm bleibt dem Container", () => {
    const onToggleAutomation = vi.fn();
    renderInteractiveGrid([landingPack], { onToggleAutomation });
    fireEvent.click(screen.getByRole("switch", { name: "Automatik" }));
    expect(onToggleAutomation).toHaveBeenCalledWith(true);
  });

  it("Vorschau-Button löst onPreview aus", () => {
    const onPreview = vi.fn();
    renderInteractiveGrid([landingPack], { onPreview });
    fireEvent.click(screen.getByRole("button", { name: /Vorschau/ }));
    expect(onPreview).toHaveBeenCalledOnce();
  });

  it("typ-badged deterministic statt das Pack fallen zu lassen", () => {
    const html = renderGrid([landingPack]);
    expect(html).toContain("Deterministisch");
  });

  it("390px-Smoke: Karte bleibt im Mobile-Stack vollständig erreichbar (AC-5)", () => {
    // jsdom wendet keine Media-Queries an; der Test pinnt den Vertrag, dass
    // die Landing-Karte keine Breiten-abhängige Conditional-Renders nutzt —
    // bei 390px stackt das Grid (Basis: einspaltig, md:grid-cols-2) und alle
    // AC-3-Elemente bleiben per Tastatur/Screenreader erreichbar.
    const originalWidth = window.innerWidth;
    Object.defineProperty(window, "innerWidth", { value: 390, configurable: true });
    try {
      renderInteractiveGrid([landingPack]);
      // getByRole wirft, wenn das Element fehlt — Anwesenheit ist damit Assertion.
      screen.getByRole("switch", { name: "Automatik" });
      screen.getByRole("button", { name: /Vorschau/ });
      // Laufstatus ist als aria-live-Region (role=status) angekündigt.
      expect(screen.getAllByRole("status").length).toBeGreaterThan(0);
    } finally {
      Object.defineProperty(window, "innerWidth", { value: originalWidth, configurable: true });
    }
  });
});

describe("LoopsGrid — Landing-Drawer (LL2-S5, AC-4)", () => {
  it("zeigt Kandidaten mit Ursprungskarten-Link, Gate-Stufen, Recovery, Trigger-Historie, Vorschau", () => {
    const html = renderLandingDetailGrid(landingDetail);
    expect(html).toContain("loop/t_abc123");
    expect(html).toContain("/control/fleet?task=t_abc123");
    expect(html).toContain("+3/−1");
    expect(html).toContain("post_merge");
    expect(html).toContain("gestartet");
    expect(html).toContain("Versuch 1");
    expect(html).toContain("fp-dead");
    expect(html).toContain("affected");
    expect(html).toContain("review:t_1");
    expect(html).toContain("Folgelauf vorgemerkt");
    expect(html).toContain("Vorschau beendet · Exit 0");
    expect(html).toContain("Landing-Loop dry-run: 2 Kandidaten");
  });

  it("bleibt ohne Landing-Daten lesbar (Empty-States)", () => {
    const html = renderLandingDetailGrid({
      ...landingDetail,
      candidates: [],
      gate_stages: [],
      recovery: [],
      trigger_history: [],
      followup_pending: false,
      preview: null,
    });
    expect(html).toContain("Keine Kandidaten in der Queue.");
    expect(html).toContain("Keine Recovery-Anfragen.");
    expect(html).toContain("Keine Trigger-Signale.");
    expect(html).toContain("Noch keine Vorschau gestartet.");
  });

  it("rendert laufende Vorschau als Statuszeile", () => {
    const html = renderLandingDetailGrid({
      ...landingDetail,
      preview: { running: true, started_at: "2026-07-31T00:05:00+00:00", output_tail: "" },
    });
    expect(html).toContain("Vorschau läuft …");
  });
});

describe("LoopDetailPanel — Eskalationen", () => {
  // Die Pack-Prompts beauftragen ESCALATIONS.md seit jeher; sichtbar war die
  // Datei nie. hermes-hardening trug so 137 Zeilen echter Out-of-Scope-Funde.
  const base = {
    ...(runningPipeline as LoopPackSummary),
    ledger_tail: [],
    queue_entries: null,
    commits: [],
    overrides: {},
    phase_usage: [],
  };

  it("renders escalation lines in the open detail panel", () => {
    const detail: LoopDetailResponse = {
      ...base,
      escalations_tail: ["## E4 — zod strippt das error-Feld", "- Schwere: mittel"],
    };
    const html = renderGrid([runningPipeline], { selectedPack: "builder-reviewer", detail });
    expect(html).toContain("E4 — zod strippt das error-Feld");
    expect(html).toContain("Eskalationen");
  });

  it("omits the section entirely when there is nothing escalated", () => {
    const detail: LoopDetailResponse = { ...base, escalations_tail: [] };
    const html = renderGrid([runningPipeline], { selectedPack: "builder-reviewer", detail });
    expect(html).not.toContain("data-loop-escalations");
  });
});
