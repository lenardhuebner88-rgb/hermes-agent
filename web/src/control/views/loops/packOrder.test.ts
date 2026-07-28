import { describe, expect, it } from "vitest";

import { ATTENTION_RANK, attentionRank, sortPacksByAttention } from "./packOrder";
import type { LoopPackSummary } from "../../lib/types";

/** Minimal-Pack in der echten Summary-Form; jeder Test variiert nur das Feld,
 *  um das es geht. Die Feldnamen stammen aus control_loops.py `_pack_summary`. */
function pack(name: string, over: Partial<LoopPackSummary> = {}): LoopPackSummary {
  return {
    name,
    type: "pipeline",
    source: "repo",
    repo: "/home/piet/.hermes/hermes-agent",
    repo_exists: true,
    base_branch: "main",
    land_remote: "piet-fork",
    land_push: true,
    land_gates: null,
    description: "",
    stability: "stable",
    phases: {},
    stop: {},
    params: {},
    autoland: false,
    autoland_capable: true,
    running: false,
    heartbeat: null,
    stop_requested: false,
    queue: { "00-planned": 0, "10-building": 0, "20-verified": 0, "30-landed": 0, "90-bounced": 0 },
    commits_ahead: 0,
    timer_enabled: false,
    timer_schedule: "23:37",
    timer_next_run: null,
    token_usage: {},
    ...over,
  } as LoopPackSummary;
}

describe("attentionRank", () => {
  it("stellt ein laufendes Pack ganz nach vorn", () => {
    expect(attentionRank(pack("x", { running: true }))).toBe(ATTENTION_RANK.running);
  });

  it("erkennt landbare Arbeit (Commits UND verifizierter Plan)", () => {
    const p = pack("x", {
      commits_ahead: 2,
      queue: { "00-planned": 0, "10-building": 0, "20-verified": 1, "30-landed": 0, "90-bounced": 0 },
    });
    expect(attentionRank(p)).toBe(ATTENTION_RANK.landable);
  });

  it("stuft Commits OHNE verifizierten Plan als unverifiziert ein, nicht als landbar", () => {
    expect(attentionRank(pack("x", { commits_ahead: 3 }))).toBe(ATTENTION_RANK.unsettled);
  });

  it("zaehlt ein Sweep-Pack mit Commits als landbar — es hat keine Plan-Queue", () => {
    // Regression: die erste Fassung verlangte einen verifizierten Plan und
    // stufte damit jeden Sweep als unverifiziert ein, obwohl die Karte einen
    // Landen-Knopf zeigte.
    const sweep = pack("doc-sweep", { type: "sweep", queue: null, commits_ahead: 1 });
    expect(attentionRank(sweep)).toBe(ATTENTION_RANK.landable);
  });

  it("verlangt beim Pipeline-Pack weiterhin den verifizierten Plan (Gegenprobe)", () => {
    const pipe = pack("x", { type: "pipeline", commits_ahead: 1 });
    expect(attentionRank(pipe)).toBe(ATTENTION_RANK.unsettled);
  });

  it("erkennt einen steckengebliebenen Build auch ohne Commits", () => {
    const p = pack("x", {
      queue: { "00-planned": 0, "10-building": 1, "20-verified": 0, "30-landed": 0, "90-bounced": 0 },
    });
    expect(attentionRank(p)).toBe(ATTENTION_RANK.unsettled);
  });

  it("erkennt ein verwaistes Pack", () => {
    expect(attentionRank(pack("x", { repo_exists: false }))).toBe(ATTENTION_RANK.broken);
  });

  it("laesst ein ruhiges Pack ruhig (Gegenprobe — sonst waere jeder Rang dringend)", () => {
    expect(attentionRank(pack("x"))).toBe(ATTENTION_RANK.quiet);
  });

  it("laesst ein laufendes Pack vorn, auch wenn sein Repo fehlt", () => {
    expect(attentionRank(pack("x", { running: true, repo_exists: false }))).toBe(ATTENTION_RANK.running);
  });
});

describe("sortPacksByAttention", () => {
  it("ordnet laufend → landbar → unverifiziert → kaputt → ruhig", () => {
    const sorted = sortPacksByAttention([
      pack("ruhig"),
      pack("kaputt", { repo_exists: false }),
      pack("unverifiziert", { commits_ahead: 1 }),
      pack("laeuft", { running: true }),
      pack("landbar", {
        commits_ahead: 1,
        queue: { "00-planned": 0, "10-building": 0, "20-verified": 2, "30-landed": 0, "90-bounced": 0 },
      }),
    ]);
    expect(sorted.map((p) => p.name)).toEqual([
      "laeuft", "landbar", "unverifiziert", "kaputt", "ruhig",
    ]);
  });

  it("sortiert gleichrangige Packs alphabetisch, damit ruhige Karten nicht wandern", () => {
    const sorted = sortPacksByAttention([pack("zulu"), pack("alpha"), pack("mike")]);
    expect(sorted.map((p) => p.name)).toEqual(["alpha", "mike", "zulu"]);
  });

  it("laesst die Eingabe unveraendert (keine In-place-Mutation der Poll-Antwort)", () => {
    const input = [pack("zulu"), pack("alpha")];
    sortPacksByAttention(input);
    expect(input.map((p) => p.name)).toEqual(["zulu", "alpha"]);
  });
});
