// @vitest-environment jsdom
import { renderToStaticMarkup } from "react-dom/server";
import { fireEvent, render, screen } from "@testing-library/react";
import { describe, expect, it, vi } from "vitest";
import { LoopsGrid, type LoopsGridProps } from "./LoopsView";
import { deriveRingSegments, deriveRingTicks } from "../lib/loopRing";
import { de } from "../i18n/de";
import type { LoopDetailResponse, LoopFilesResponse, LoopHeartbeatCurrent, LoopModelsResponse, LoopPack, LoopPackSummary } from "../lib/types";

const t = de.loops;

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
    current: { phase: "build", engine: "claude", model: "claude-sonnet-5", started_at: "2026-07-02T23:00:00", timeout: 3600 },
    last: [
      { phase: "build", engine: "claude", model: "claude-sonnet-5", secs: 512, rc: 0, at: "2026-07-02T22:00:00" },
      { phase: "verify", engine: "claude", model: "claude-fable-5", secs: 178, rc: 1, at: "2026-07-02T22:10:00" },
    ],
  },
  stop_requested: false,
  queue: { "00-planned": 1, "10-building": 2, "20-verified": 7, "30-landed": 3, "90-bounced": 0 },
  commits_ahead: 0,
  timer_enabled: true,
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
};

// Läuft UND hat unverdaute Commits — Land darf trotzdem nicht auftauchen
// (running unterdrückt es, egal was commits_ahead sagt).
const runningWithCommits: LoopPack = {
  ...runningPipeline,
  name: "running-with-commits",
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
    kimi: { label: "Kimi (Coding-Abo)", models: ["kimi-code/kimi-for-coding"] },
    codex: { label: "Codex (ChatGPT-Abo)", models: ["gpt-5.5", "gpt-5.3-codex"] },
    neuralwatt: { label: "NeuralWatt (Abo) — geplant, kein Adapter", models: [] },
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
  onSaveFile: vi.fn(),
  onDuplicate: vi.fn(),
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

describe("LoopsGrid", () => {
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

  it("renders a manifest-error pack as an error card, not a crash", () => {
    const html = renderGrid([brokenPack]);
    expect(html).toContain("broken-pack");
    expect(html).toContain(t.manifestError);
    expect(html).toContain("ManifestError: phases fehlt in pack.yaml");
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

describe("LoopsGrid — Live-Phase-Chip (heartbeat)", () => {
  it("shows the current phase/model/elapsed duration for a running pack with heartbeat.current", () => {
    const startedMs = Date.parse(runningPipeline.heartbeat!.current!.started_at);
    const nowMs = startedMs + 8 * 60_000; // 8 Minuten seit Phasenstart
    const html = renderGrid([runningPipeline], { nowMs });
    expect(html).toContain(t.heartbeatCurrent("build", "claude-sonnet-5", "8m"));
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

  it("hides Land while the pack is running, even with commits_ahead > 0", () => {
    const html = renderGrid([runningWithCommits]);
    expect(html).not.toContain(t.actions.land);
  });

  it("hides Land for an idle pack without commits_ahead", () => {
    const html = renderGrid([idleNoCommits]);
    expect(html).not.toContain(t.actions.land);
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
    const html = renderGrid([runningPipeline], { workshopOpenPack: "builder-reviewer", files: repoFiles });
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
    ledger_tail: [
      "- 2026-07-03 07:14 R1 ✅ P1-repo-housekeeper-dead-code-sweep.md verified (a1b2c3d4e) [build 812s · verify 340s]",
      "# LEDGER — ein fremdes/kaputtes Format, das nicht crashen darf",
    ],
    queue_entries: null,
    commits: [],
    overrides: {},
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

  const NOW = Date.parse("2026-07-03T08:00:00");

  it("zählt verify der VORHERIGEN Runde nicht als done, wenn Runde 2 in build steht", () => {
    const pack = withHeartbeat(
      { phase: "build", engine: "claude", model: "claude-sonnet-5", started_at: "2026-07-03T07:55:00", timeout: 3600 },
      [
        // Runde 1 (komplett, alles grün):
        hbEntry("plan", 0, "2026-07-03T06:00:00"),
        hbEntry("build", 0, "2026-07-03T06:20:00"),
        hbEntry("verify", 0, "2026-07-03T06:40:00"),
        // Runde 2 (nur plan bisher):
        hbEntry("plan", 0, "2026-07-03T07:50:00"),
      ],
    );
    const segs = deriveRingSegments(pack, NOW);
    expect(segs.find((s) => s.key === "plan")?.state).toBe("done");
    expect(segs.find((s) => s.key === "build")?.state).toBe("current");
    expect(segs.find((s) => s.key === "verify")?.state).toBe("pending");
  });

  it("startet mit leerem Ring, wenn gerade eine neue Runde plant (History = Vergangenheit)", () => {
    const pack = withHeartbeat(
      { phase: "plan", engine: "claude", model: "claude-fable-5", started_at: "2026-07-03T07:59:00", timeout: 2400 },
      [hbEntry("plan", 0, "2026-07-03T06:00:00"), hbEntry("build", 0, "2026-07-03T06:20:00"), hbEntry("verify", 0, "2026-07-03T06:40:00")],
    );
    const segs = deriveRingSegments(pack, NOW);
    expect(segs.find((s) => s.key === "plan")?.state).toBe("current");
    expect(segs.find((s) => s.key === "build")?.state).toBe("pending");
    expect(segs.find((s) => s.key === "verify")?.state).toBe("pending");
  });

  it("zeigt im Leerlauf das Ergebnis der LETZTEN Runde (Fenster ab letztem plan)", () => {
    const pack = withHeartbeat(null, [
      hbEntry("verify", 1, "2026-07-03T05:00:00"), // ältere, rote Runde — zählt nicht
      hbEntry("plan", 0, "2026-07-03T06:00:00"),
      hbEntry("build", 0, "2026-07-03T06:20:00"),
      hbEntry("verify", 0, "2026-07-03T06:40:00"),
    ]);
    const segs = deriveRingSegments(pack, NOW);
    expect(segs.every((s) => s.state === "done")).toBe(true);
  });

  it("bleibt konservativ pending, wenn kein plan-Eintrag im Fenster liegt", () => {
    const pack = withHeartbeat(null, [hbEntry("build", 0, "2026-07-03T06:20:00"), hbEntry("verify", 0, "2026-07-03T06:40:00")]);
    const segs = deriveRingSegments(pack, NOW);
    expect(segs.every((s) => s.state === "pending")).toBe(true);
  });
});

describe("deriveRingTicks — nur der hintere zusammenhängende round-Block", () => {
  const hbEntry = (phase: string, rc: number) =>
    ({ phase, engine: "claude", model: "claude-sonnet-5", secs: 100, rc, at: "2026-07-03T06:00:00" });

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
};

describe("LoopStartForm — SKIP_PLAN-Override", () => {
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
