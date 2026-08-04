// Vertragstest Landing-Pack (LL2-S5): deterministic + additive Landing-Felder.
// Backend-Vertrag: hermes_cli/control_loops.py (_landing_control_payload,
// pack_detail-Landing-Block). Geerntet gegen den echten Payload-Shape von S4.
import { describe, expect, it } from "vitest";
import {
  LoopDetailResponseSchema,
  LoopPackSummarySchema,
  LoopsResponseSchema,
} from "./loops";

const landingSummaryPayload = {
  name: "landing",
  type: "deterministic",
  source: "repo",
  repo: "/home/piet/.hermes/hermes-agent",
  repo_exists: true,
  base_branch: "main",
  description: "Event-getriebener Landing-Loop",
  stability: "experimental",
  running: false,
  stop_requested: false,
  commits_ahead: 0,
  automation_enabled: false,
  baseline_sha: "abc1234def5678",
  baseline_ok: true,
  baseline_reason: "Nightly-Full-Gate-Nachweis für aktuellen main-SHA",
  baseline_source: "nightly",
  queue_summary: { total: 2, landed: 1, cleaned: 0, parked: 0 },
  next_trigger_at: "2026-07-31T01:00:00+00:00",
  last_result: "held",
  collection_window: { opened_at: "2026-07-31T00:50:00+00:00", closes_at: "2026-07-31T01:00:00+00:00" },
  candidates: [
    { branch: "loop/t_abc123", head: "deadbeefcafe01", ahead: 3, behind: 1 },
    { branch: "loop/manual", head: null, ahead: 1, behind: 0 },
  ],
};

describe("LoopPackSummarySchema — Landing (deterministic)", () => {
  it("akzeptiert type=deterministic mit allen Landing-Feldern", () => {
    const parsed = LoopPackSummarySchema.parse(landingSummaryPayload);
    expect(parsed.type).toBe("deterministic");
    expect(parsed.automation_enabled).toBe(false);
    expect(parsed.baseline_reason).toBe(
      "Nightly-Full-Gate-Nachweis für aktuellen main-SHA",
    );
    expect(parsed.baseline_source).toBe("nightly");
    expect(parsed.queue_summary?.total).toBe(2);
    expect(parsed.candidates).toHaveLength(2);
    expect(parsed.candidates?.[1].head).toBeNull();
    expect(parsed.collection_window?.closes_at).toBe("2026-07-31T01:00:00+00:00");
  });

  it("lässt das Landing-Pack nicht mehr ins Error-Schema fallen", () => {
    const parsed = LoopsResponseSchema.parse({ packs: [landingSummaryPayload] });
    expect(parsed.packs).toHaveLength(1);
    expect("error" in parsed.packs[0]).toBe(false);
  });

  it("funktioniert auch ohne die additiven Felder (älteres Backend)", () => {
    const parsed = LoopPackSummarySchema.parse({ name: "landing", type: "deterministic" });
    expect(parsed.automation_enabled).toBeUndefined();
    expect(parsed.baseline_reason).toBeUndefined();
    expect(parsed.baseline_source).toBeUndefined();
    expect(parsed.candidates).toBeUndefined();
    expect(parsed.queue_summary).toBeUndefined();
  });

  it("bewahrt eine künftige Baseline-Quelle ohne das Pack-Array zu leeren", () => {
    const parsed = LoopsResponseSchema.parse({
      packs: [{ ...landingSummaryPayload, baseline_source: "release-gate" }],
    });
    expect(parsed.packs).toHaveLength(1);
    const pack = parsed.packs[0];
    expect("type" in pack).toBe(true);
    if (!("type" in pack)) throw new Error("Landing-Summary wurde verworfen");
    expect(pack.baseline_source).toBe("release-gate");
  });

  it("pipeline/sweep bleiben unverändert gültig", () => {
    expect(LoopPackSummarySchema.parse({ name: "p", type: "pipeline" }).type).toBe("pipeline");
    expect(LoopPackSummarySchema.parse({ name: "s", type: "sweep" }).type).toBe("sweep");
  });

  it("verwirft Müll-Felder defensiv statt zu crashen", () => {
    const parsed = LoopPackSummarySchema.parse({
      name: "landing",
      type: "deterministic",
      queue_summary: {},
      candidates: [{ branch: "loop/t_x" }],
    });
    expect(parsed.queue_summary?.total).toBeUndefined();
    expect(parsed.candidates?.[0].ahead).toBeUndefined();
  });
});

describe("LoopPackSummarySchema — Repo-Lock (control_loops._annotate_repo_lock)", () => {
  // E4 (ESCALATIONS.md): ein neues Backend-Feld ohne Schema-Eintrag wird von
  // zod still gestrippt und erreicht die UI nie — deshalb hier explizit gegen
  // den echten Payload-Shape geprüft, nicht nur "irgendein Feld parst".
  it("bewahrt repo_locked + repo_locked_by, wenn ein anderes Pack blockiert", () => {
    const parsed = LoopPackSummarySchema.parse({
      name: "fliessband",
      type: "pipeline",
      repo_locked: true,
      repo_locked_by: "nacht",
    });
    expect(parsed.repo_locked).toBe(true);
    expect(parsed.repo_locked_by).toBe("nacht");
  });

  it("bewahrt repo_locked_by=null (Halter nicht eindeutig bestimmbar)", () => {
    const parsed = LoopPackSummarySchema.parse({
      name: "fliessband",
      type: "pipeline",
      repo_locked: true,
      repo_locked_by: null,
    });
    expect(parsed.repo_locked).toBe(true);
    expect(parsed.repo_locked_by).toBeNull();
  });

  it("funktioniert ohne die Felder (kein Fremd-Lock bzw. älteres Backend)", () => {
    const parsed = LoopPackSummarySchema.parse({ name: "nacht", type: "sweep" });
    expect(parsed.repo_locked).toBeUndefined();
    expect(parsed.repo_locked_by).toBeUndefined();
  });
});

describe("LoopDetailResponseSchema — Landing-Drawer-Payload", () => {
  const detailPayload = {
    ...landingSummaryPayload,
    ledger_tail: [],
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

  it("parst den vollen Drawer-Vertrag", () => {
    const parsed = LoopDetailResponseSchema.parse(detailPayload);
    expect(parsed.gate_stages).toEqual(["baseline", "affected", "full", "post_merge"]);
    expect(parsed.recovery?.[0].state).toBe("started");
    expect(parsed.followup_pending).toBe(true);
    expect(parsed.preview?.rc).toBe(0);
  });

  it("akzeptiert preview=null (noch keine Vorschau gelaufen)", () => {
    const parsed = LoopDetailResponseSchema.parse({ ...detailPayload, preview: null });
    expect(parsed.preview).toBeNull();
  });

  it("akzeptiert fehlende Landing-Detail-Felder (älteres Backend)", () => {
    const parsed = LoopDetailResponseSchema.parse({ name: "landing", type: "deterministic" });
    expect(parsed.recovery).toBeUndefined();
    expect(parsed.gate_stages).toBeUndefined();
    expect(parsed.preview).toBeUndefined();
  });
});
