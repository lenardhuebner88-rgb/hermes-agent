import { renderToStaticMarkup } from "react-dom/server";
import { describe, expect, it, vi } from "vitest";
import { LoopsGrid } from "./LoopsView";
import { de } from "../i18n/de";
import type { LoopModelsResponse, LoopPack } from "../lib/types";

const t = de.loops;

// Manifest-Felder (phases/stop/params/description/stability/type) sind aus dem
// ECHTEN Payload geerntet — via TestClient(app).get("/api/loops") gegen die
// tatsächlichen loops/packs/builder-reviewer + loops/packs/doc-sweep Manifeste
// (2026-07-02). Nur die Laufzeit-Felder (running/queue/commits_ahead) sind für
// die Szenarien hier überschrieben, weil aktuell live kein Loop läuft.
const runningPipeline: LoopPack = {
  name: "builder-reviewer",
  type: "pipeline",
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
  stop_requested: false,
  queue: { "00-planned": 1, "10-building": 2, "20-verified": 7, "90-bounced": 0 },
  commits_ahead: 0,
  timer_enabled: true,
};

const idleSweepWithCommits: LoopPack = {
  name: "doc-sweep",
  type: "sweep",
  description: "Pro Runde EINE Doku-Drift zwischen Repo-Doku und Code-Verhalten finden und die Doku korrigieren",
  stability: "experimental",
  phases: { round: { engine: "claude", model: "claude-sonnet-5", timeout: 2400 } },
  stop: { max_rounds: 10, max_hours: 7, fail_streak: 2, dry_rounds: 2 },
  params: { fokus: "AGENTS.md, README*, CLAUDE.md (Repo), docs/, plugins/*/README" },
  running: false,
  stop_requested: false,
  queue: null,
  commits_ahead: 4,
  timer_enabled: false,
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
  onToggleDetail: vi.fn(),
  onOpenStart: vi.fn(),
  onCloseStart: vi.fn(),
  onSubmitStart: vi.fn(),
  onStop: vi.fn(),
  onToggleTimer: vi.fn(),
};

function renderGrid(packs: LoopPack[]) {
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
      startOpenPack={null}
      pendingStopPack={null}
      {...noopHandlers}
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
    expect(html).toContain(t.queueBounced);
    // 20-verified count (7) muss sichtbar sein, nicht nur das Label.
    expect(html).toMatch(/>7</);
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
