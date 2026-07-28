import { describe, it, expect } from "vitest";
import { buildDecisionInbox, inboxSummary } from "./decisionInbox";
import { liveAutoresearchMutationFixture } from "./proposalGroups.live.fixture";
import type { Proposal } from "./types";
import type { BacklogItem } from "./schemas";
import {
  KanbanDecisionKindSchema,
  OperatorEscalationPayloadSchema,
} from "./schemas";
import type { AgentOpsIntervention } from "./agentOps";

const NOW = Math.floor(Date.parse("2026-06-05T00:00:00Z") / 1000);

describe("OperatorEscalationPayloadSchema", () => {
  it("preserves the backend escalation class", () => {
    const payload = OperatorEscalationPayloadSchema.parse({
      task: { id: "t_escalated" },
      escalation_class: "capacity",
      why_now: "iteration budget exhausted",
      attempts_already_made: 2,
      evidence: {},
      recommended_human_action: "raise the limit",
      blocked_action_boundary: [],
    });

    expect(payload?.escalation_class).toBe("capacity");
  });
});

function proposal(over: Partial<Proposal> & { id: string }): Proposal {
  return {
    target: "docs/x.md",
    section: null,
    rationale_plain: "because",
    diff_before_after: "",
    mode: "skill",
    status: "proposed",
    ...over,
  };
}

function foItem(over: Partial<BacklogItem> & { id: string }): BacklogItem {
  return {
    title: `Task ${over.id}`,
    status: "later",
    owner: "claude",
    risk: "low",
    area: "lists",
    updated: "2026-06-01",
    lane: null,
    result: null,
    stale: false,
    excerpt: undefined,
    source_path: `backlog/items/${over.id}-task.md`,
    ...over,
  };
}

function intervention(over: Partial<AgentOpsIntervention> & { id: string }): AgentOpsIntervention {
  return { tone: "amber", title: `IV ${over.id}`, detail: "needs you", target: "/control/orchestrator", ...over };
}

describe("buildDecisionInbox", () => {
  it("aggregates all three surfaces into one list", () => {
    const items = buildDecisionInbox({
      proposals: [proposal({ id: "p1", severity: "high" })],
      foItems: [foItem({ id: "0001", status: "blocked" })],
      foNowSec: NOW,
      interventions: [intervention({ id: "iv1" })],
    });
    expect(items.map((i) => i.surface).sort()).toEqual(["autoresearch", "family", "orchestrator"]);
  });

  it("ranks higher-severity / blocked items to the top", () => {
    const items = buildDecisionInbox({
      proposals: [proposal({ id: "low", severity: "low" })],
      foItems: [foItem({ id: "0001", status: "blocked" })],
      foNowSec: NOW,
      interventions: [],
    });
    expect(items[0].surface).toBe("family"); // blocked (90) outranks a low proposal (40)
    expect(items[0].weight).toBeGreaterThan(items[1].weight);
  });

  it("skips non-actionable proposals and later/done FO items", () => {
    const items = buildDecisionInbox({
      proposals: [proposal({ id: "applied", status: "applied", severity: "critical" })],
      foItems: [foItem({ id: "0001", status: "later" }), foItem({ id: "0002", status: "done" })],
      foNowSec: NOW,
      interventions: [],
    });
    expect(items).toHaveLength(0);
  });

  it("only includes explicit operator decisions and caps Autoresearch at three", () => {
    const decisions = Array.from({ length: 5 }, (_, index) => proposal({
      id: `decision-${index}`,
      target: `docs/${index}.md`,
      rank_score: 100 - index,
      operator_action_required: true,
      decision_state: "needs_operator",
      delivery_state: "none",
    }));
    const items = buildDecisionInbox({
      proposals: [
        ...decisions,
        proposal({ id: "delivery", status: "pooled", operator_action_required: false, decision_state: "accepted", delivery_state: "queued" }),
      ],
      foItems: [], foNowSec: NOW, interventions: [],
    });

    expect(items).toHaveLength(3);
    expect(items.every((item) => item.surface === "autoresearch")).toBe(true);
    expect(items.some((item) => item.key.includes("delivery"))).toBe(false);
  });

  it("treats an unowned active FO item as a decision even when status is later", () => {
    const items = buildDecisionInbox({
      proposals: [],
      foItems: [foItem({ id: "0001", status: "later", owner: "unassigned" })],
      foNowSec: NOW,
      interventions: [],
    });
    expect(items).toHaveLength(1);
    expect(items[0].surface).toBe("family");
  });

  it("deep-links each row to its exact item, not the bare tab", () => {
    const items = buildDecisionInbox({
      proposals: [proposal({ id: "p1", severity: "high" })],
      foItems: [foItem({ id: "0001", status: "blocked" })],
      foNowSec: NOW,
      interventions: [],
    });
    const ar = items.find((i) => i.surface === "autoresearch");
    const fo = items.find((i) => i.surface === "family");
    expect(ar?.target).toBe("/control/autoresearch?focus=p1");
    expect(fo?.target).toBe("/control/backlog?focus=0001");
  });

  it("encodes item ids that contain URL-special characters", () => {
    const items = buildDecisionInbox({
      proposals: [proposal({ id: "a/b c", severity: "high" })],
      foItems: [],
      foNowSec: NOW,
      interventions: [],
    });
    expect(items[0].target).toBe("/control/autoresearch?focus=a%2Fb%20c");
  });

  it("does NOT double-count proposals via the open-proposals summary intervention", () => {
    // The open-proposals intervention summarizes the SAME proposals already
    // enumerated per-item, so folding it would inflate the count. It must be
    // dropped here while a genuine orchestrator summary (blocked-items) stays.
    const items = buildDecisionInbox({
      proposals: [proposal({ id: "p1", severity: "high" }), proposal({ id: "p2", severity: "low" })],
      foItems: [],
      foNowSec: NOW,
      interventions: [
        intervention({ id: "open-proposals", tone: "cyan", target: "/control/autoresearch" }),
        intervention({ id: "blocked-items", tone: "red" }),
      ],
    });
    const summary = inboxSummary(items);
    // 2 duplicate proposals collapse to 1 Autoresearch group + 1 real
    // orchestrator summary = 2, NOT 3/4.
    expect(summary).toEqual({ total: 2, autoresearch: 1, family: 0, orchestrator: 1, kanban: 0 });
    expect(items.some((i) => i.key === "orch:open-proposals")).toBe(false);
    expect(items.some((i) => i.key === "orch:blocked-items")).toBe(true);
  });

  it("is deterministic for equal weights (stable key tiebreak)", () => {
    const input = {
      proposals: [proposal({ id: "b", target: "docs/b.md", severity: "high" }), proposal({ id: "a", target: "docs/a.md", severity: "high" })],
      foItems: [],
      foNowSec: NOW,
      interventions: [],
    };
    const first = buildDecisionInbox(input).map((i) => i.key);
    const second = buildDecisionInbox(input).map((i) => i.key);
    expect(first).toEqual(second);
    expect(first).toEqual(["ar:a", "ar:b"]);
  });
});

describe("inboxSummary", () => {
  it("counts per surface", () => {
    const summary = inboxSummary(
      buildDecisionInbox({
        proposals: [proposal({ id: "p1", severity: "high" }), proposal({ id: "p2", target: "docs/y.md", severity: "low" })],
        foItems: [foItem({ id: "0001", status: "blocked" })],
        foNowSec: NOW,
        interventions: [intervention({ id: "iv1" })],
      }),
    );
    expect(summary).toEqual({ total: 4, autoresearch: 2, family: 1, orchestrator: 1, kanban: 0 });
  });

  it("counts Autoresearch groups instead of cards against the live fixture", () => {
    const items = buildDecisionInbox({
      proposals: liveAutoresearchMutationFixture,
      foItems: [],
      foNowSec: NOW,
      interventions: [],
    });
    const summary = inboxSummary(items);

    expect(liveAutoresearchMutationFixture).toHaveLength(30);
    expect(summary).toEqual({ total: 2, autoresearch: 2, family: 0, orchestrator: 0, kanban: 0 });
    expect(items.some((item) => item.title.includes("28 Vorschläge"))).toBe(true);
  });

  it("counts the kanban surface", () => {
    const summary = inboxSummary(
      buildDecisionInbox({
        proposals: [],
        foItems: [],
        foNowSec: NOW,
        interventions: [],
        kanbanDecisions: [
          { kind: "review_rejected", task_id: "t1", title: "T1", reason: "RC", age_seconds: 10, suggested_command: "hermes kanban show t1" },
          { kind: "sticky_blocked", task_id: "t2", title: "T2", reason: "stuck", age_seconds: 99, suggested_command: "hermes kanban unblock t2" },
        ],
      }),
    );
    expect(summary).toEqual({ total: 2, autoresearch: 0, family: 0, orchestrator: 0, kanban: 2 });
  });
});

describe("buildDecisionInbox — kanban surface", () => {
  it("folds kanban decisions, ranks review_rejected above sticky, deep-links + carries the command", () => {
    const items = buildDecisionInbox({
      proposals: [],
      foItems: [],
      foNowSec: NOW,
      interventions: [],
      kanbanDecisions: [
        { kind: "sticky_blocked", task_id: "t2", title: "Blocked one", reason: "needs eyes", age_seconds: 99, suggested_command: "hermes kanban unblock t2" },
        { kind: "review_rejected", task_id: "t1", title: "Rejected one", reason: "missing tests", age_seconds: 10, suggested_command: "hermes kanban show t1" },
      ],
    });
    expect(items.map((i) => i.surface)).toEqual(["kanban", "kanban"]);
    // review_rejected (86) outranks sticky_blocked (75)
    expect(items[0].title).toBe("Rejected one");
    // Kanban-Tasks leben im Fleet, nicht im Family-Backlog: Deep-Link auf den
    // Fleet-Task-Fokus (FleetView ?task=), nie mehr /control/backlog.
    expect(items[0].target).toBe("/control/fleet?task=t1");
    expect(items[0].nextAction).toBe("hermes kanban show t1");
    expect(items[0].why).toContain("missing tests");
    // K3: nur review_rejected trägt den Inline-Resolve-Anker.
    expect(items[0].fixTaskId).toBe("t1");
    expect(items[1].fixTaskId).toBeUndefined();
    // Befund 3: das age_seconds des Gateway-Payloads reicht bis ins InboxItem
    // durch — TopDecision/Queue können "vor Xm" zeigen.
    expect(items[0].ageSeconds).toBe(10);
    expect(items[1].ageSeconds).toBe(99);
  });

  it("frames disposition_risk as follow-up risks with count + provenance, routes to Fleet", () => {
    const items = buildDecisionInbox({
      proposals: [],
      foItems: [],
      foNowSec: NOW,
      interventions: [],
      kanbanDecisions: [
        {
          kind: "disposition_risk",
          task_id: "t9",
          title: "Adversarial Review: Loop-Autoland",
          reason: "3 offene(s) Risiko(en) aus Abschluss: Modell-Gleichheitscheck entfernen",
          age_seconds: 42,
          suggested_command: null,
          risk_count: 3,
        },
      ],
    });
    expect(items).toHaveLength(1);
    expect(items[0].surface).toBe("kanban");
    // Headline nennt die Risiko-Anzahl und erweckt NICHT den Eindruck, der
    // abgeschlossene Critic-Task selbst sei neu blockiert.
    expect(items[0].title).toBe("3 offene Risiken aus Abschluss");
    // Provenienz (welcher Task) + konkrete nächste Aktion in der why-Zeile.
    expect(items[0].why).toContain("Adversarial Review: Loop-Autoland");
    expect(items[0].why).toContain("Modell-Gleichheitscheck entfernen");
    expect(items[0].nextAction).toBe("Risiken im Fleet prüfen & schließen");
    expect(items[0].tone).toBe("amber");
    expect(items[0].key).toBe("kanban:disposition_risk:t9");
    // Nie mehr Family-Backlog: Kanban-Risiken gehören ins Fleet.
    expect(items[0].target).toBe("/control/fleet?task=t9");
  });

  it("singularizes the disposition_risk headline for a single open risk", () => {
    const items = buildDecisionInbox({
      proposals: [],
      foItems: [],
      foNowSec: NOW,
      interventions: [],
      kanbanDecisions: [
        { kind: "disposition_risk", task_id: "t9", title: "Done task", reason: "1 offenes Risiko", age_seconds: 42, suggested_command: null, risk_count: 1 },
      ],
    });
    expect(items[0].title).toBe("1 offenes Risiko aus Abschluss");
  });

  it("KanbanDecisionKindSchema keeps disposition_risk (no .catch coercion to sticky_blocked)", () => {
    expect(KanbanDecisionKindSchema.parse("disposition_risk")).toBe("disposition_risk");
  });

  it("renders disposition_stale with its own lower-urgency label/tone (FRD Phase 3a reaper)", () => {
    const items = buildDecisionInbox({
      proposals: [],
      foItems: [],
      foNowSec: NOW,
      interventions: [],
      kanbanDecisions: [
        { kind: "disposition_stale", task_id: "t7", title: "Old task", reason: "2 alternde offene Items", age_seconds: 700000, suggested_command: null },
      ],
    });
    expect(items).toHaveLength(1);
    expect(items[0].surface).toBe("kanban");
    expect(items[0].why).toContain("Alterndes offenes Item");
    expect(items[0].tone).toBe("cyan");
    expect(KanbanDecisionKindSchema.parse("disposition_stale")).toBe("disposition_stale");
  });

  it("lässt das Alter leer, wenn der Payload kein age_seconds trägt (graceful)", () => {
    const items = buildDecisionInbox({
      proposals: [],
      foItems: [],
      foNowSec: NOW,
      interventions: [],
      kanbanDecisions: [
        { kind: "operator_escalation", task_id: "t1", title: "Ohne Alter", reason: "x", age_seconds: null, suggested_command: null },
      ],
    });
    expect(items[0].ageSeconds).toBeUndefined();
  });



  it("labels autoresearch and strategist escalations by source", () => {
    const items = buildDecisionInbox({
      proposals: [],
      foItems: [],
      foNowSec: NOW,
      interventions: [],
      kanbanDecisions: [
        {
          kind: "operator_escalation",
          task_id: "ar-1",
          title: "Autoresearch finding",
          reason: "needs review",
          age_seconds: 12,
          suggested_command: null,
          operator_escalation: {
            task: { id: "ar-1" },
            source: "autoresearch",
            signal_key: "silent-except",
            why_now: "route needs operator",
            attempts_already_made: 1,
            evidence: {},
            recommended_human_action: "review",
            blocked_action_boundary: [],
          },
        },
        {
          kind: "operator_escalation",
          task_id: "st-1",
          title: "Strategist hold",
          reason: "needs intent",
          age_seconds: 10,
          suggested_command: null,
          operator_escalation: {
            task: { id: "st-1" },
            source: "strategist",
            signal_key: "lever-budget",
            why_now: "hold needs operator",
            attempts_already_made: 1,
            evidence: {},
            recommended_human_action: "decide",
            blocked_action_boundary: [],
          },
        },
      ],
    });

    const ar = items.find((i) => i.key === "kanban:operator_escalation:ar-1");
    const st = items.find((i) => i.key === "kanban:operator_escalation:st-1");
    expect(ar?.why).toContain("Autoresearch-Eskalation");
    expect(ar?.why).toContain("Signal silent-except");
    expect(st?.why).toContain("Strategist-Hold");
    expect(st?.why).toContain("Signal lever-budget");
  });

  it("wires the inline veto only for autoresearch escalations with a signal", () => {
    const mk = (taskId: string, source: string, signal: string | null) => ({
      kind: "operator_escalation" as const,
      task_id: taskId,
      title: taskId,
      reason: "x",
      age_seconds: 1,
      suggested_command: null,
      operator_escalation: {
        task: { id: taskId },
        source,
        signal_key: signal,
        why_now: "",
        attempts_already_made: 1,
        evidence: {},
        recommended_human_action: "",
        blocked_action_boundary: [],
      },
    });
    const items = buildDecisionInbox({
      proposals: [],
      foItems: [],
      foNowSec: NOW,
      interventions: [],
      kanbanDecisions: [
        mk("ar-1", "autoresearch", "silent-except"),
        mk("st-1", "strategist", "lever-budget"),
        mk("ar-2", "autoresearch", null),
      ],
    });
    const at = (k: string) => items.find((i) => i.key === `kanban:operator_escalation:${k}`);
    // autoresearch + signal → vetoable via the escalation veto path
    expect(at("ar-1")?.vetoEscalationTaskId).toBe("ar-1");
    // strategist holds use the /strategist veto path, not this one
    expect(at("st-1")?.vetoEscalationTaskId).toBeUndefined();
    // autoresearch without a signal has nothing for reflect to learn → no veto
    expect(at("ar-2")?.vetoEscalationTaskId).toBeUndefined();
  });

  it("ranks operator escalation above generic blocked rows", () => {
    const items = buildDecisionInbox({
      proposals: [],
      foItems: [],
      foNowSec: NOW,
      interventions: [],
      kanbanDecisions: [
        { kind: "sticky_blocked", task_id: "t2", title: "Blocked one", reason: "needs eyes", age_seconds: 99, suggested_command: null },
        { kind: "operator_escalation", task_id: "t1", title: "Escalated one", reason: "retry ladder exhausted", age_seconds: 10, suggested_command: "hermes kanban show t1" },
      ],
    });

    expect(items[0].title).toBe("Escalated one");
    expect(items[0].tone).toBe("red");
    expect(items[0].why).toContain("Operator-Eskalation");
    expect(items[0].target).toBe("/control/fleet?task=t1");
  });

  it("surfaces no-silent-stall recovery classes with amber priority", () => {
    const items = buildDecisionInbox({
      proposals: [],
      foItems: [],
      foNowSec: NOW,
      interventions: [],
      kanbanDecisions: [
        { kind: "sticky_blocked", task_id: "t3", title: "Blocked one", reason: "needs eyes", age_seconds: 99, suggested_command: null },
        { kind: "rate_limited_loop", task_id: "t2", title: "Quota loop", reason: "429 quota", age_seconds: 10, suggested_command: "hermes kanban show t2" },
        { kind: "integration_parked", task_id: "t1", title: "Merge parked", reason: "integration parked: merge gate red", age_seconds: 8, suggested_command: "hermes kanban show t1" },
      ],
    });

    expect(items.map((i) => i.title)).toEqual(["Merge parked", "Quota loop", "Blocked one"]);
    expect(items[0].why).toContain("Integration geparkt");
    expect(items[1].why).toContain("Rate-Limit-Schleife");
    expect(items[0].tone).toBe("amber");
    expect(items[1].tone).toBe("amber");
  });

  it("integration_parked: ANSI-Gate-Ausgabe vom Live-Board wird zu einer lesbaren Zeile", () => {
    // Echter Parkgrund vom Board, 28.07.2026 (task_events id 86415, t_2bf50879):
    // 2071 Zeichen, 32 Zeilen, mit ESC-Sequenzen aus der Vitest-Ausgabe.
    const rawReason =
      "integration parked: post-merge gate failed: frontend bootstrap: exit 1\n" +
      "thod: without installing the canvas npm package\n" +
      "\n" +
      "\x1b[31m⎯⎯⎯⎯⎯⎯⎯\x1b[39m\x1b[1m\x1b[41m Failed Tests 1 \x1b[49m\x1b[22m\x1b[31m⎯⎯⎯⎯⎯⎯⎯\x1b[39m\n" +
      "\n" +
      "\x1b[41m\x1b[1m FAIL \x1b[22m\x1b[49m src/control/views/BibliothekCorrectionEditor.test.tsx\x1b[2m > \x1b[22mBibliothekCorrectionEditor\x1b[2m > \x1b[22mSpeichern sendet exakt {item_id} per PUT\n" +
      "\x1b[31m\x1b[1mAssertionError\x1b[22m: expected <button type=\"button\" …(2)></button> to be <button type=\"button\" …(3)></button> // Object.is equality\x1b[39m\n" +
      "\n" +
      "\x1b[32m- Expected\x1b[39m\n" +
      "\x1b[31m+ Received\x1b[39m\n" +
      "\n" +
      "\x1b[2m  <button\x1b[22m\n" +
      "\x1b[32m-   aria-busy=\"true\"\x1b[39m\n" +
      "\x1b[32m-   class=\"inline-flex min-h-12 items-center justify-center rounded-card border border-live/40 bg-live/10 px-4 text-sec font-medium text-bronze-hi transition hover:bg-live/15 aria-disabled:pointer-events-none aria-disabled:cursor-wait aria-disabled:opacity-50\"\x1b[39m\n" +
      "\x1b[31m+   class=\"inline-flex min-h-12 items-center justify-center rounded-card border border-line px-4 text-sec text-ink-2 transition hover:bg-surface-3 disabled:opacity-50\"\x1b[39m\n" +
      "\x1b[31m+   disabled=\"\"\x1b[39m\n" +
      "\x1b[2m    type=\"button\"\x1b[22m\n" +
      "\x1b[2m  >\x1b[22m\n" +
      "\x1b[32m-   Wird gespeichert…\x1b[39m\n" +
      "\x1b[31m+   Abbrechen\x1b[39m\n" +
      "\x1b[2m  </button>\x1b[22m\n" +
      "\n" +
      "\x1b[36m \x1b[2m❯\x1b[22m src/control/views/BibliothekCorrectionEditor.test.tsx:\x1b[2m356:36\x1b[22m\x1b[39m\n" +
      "    \x1b[90m354|\x1b[39m     \x1b[34mexpect\x1b[39m(busySave\x1b[33m.\x1b[39m\x1b[34mgetAttribute\x1b[39m(\x1b[32m\"aria-disabled\"\x1b[39m))\x1b[33m.\x1b[39m\x1b[34mtoBe\x1b[39m(\x1b[32m\"true\"\x1b[39m)\x1b[33m;\x1b[39m\n" +
      "    \x1b[90m355|\x1b[39m     expect(screen.getByRole(\"dialog\", { name: \"Korrektur verbindlich s…\n" +
      "    \x1b[90m356|\x1b[39m     \x1b[34mexpect\x1b[39m(document\x1b[33m.\x1b[39m\x1b[34mactiveElement)\x1b[33m.\x1b[39m\x1b[34mtoBe\x1b[39m(busySave)\x1b[33m;\x1b[39m\n" +
      "    \x1b[90m   |\x1b[39m                                    \x1b[31m^\x1b[39m\n" +
      "    \x1b[90m357|\x1b[39m     \x1b[34mexpect\x1b[39m(editorClose\x1b[33m.\x1b[39m\x1b[34mdisabled)\x1b[33m.\x1b[39m\x1b[34mtoBe\x1b[39m(\x1b[35mtrue\x1b[39m)\x1b[33m;\x1b[39m\n" +
      "    \x1b[90m358|\x1b[39m     resolveSave({ correction: savedCorrection, provenance: savedProven…\n" +
      "\n" +
      "\x1b[31m\x1b[2m⎯⎯⎯⎯⎯⎯⎯⎯⎯⎯⎯⎯⎯⎯⎯⎯⎯⎯⎯⎯⎯⎯⎯⎯[1/1]⎯\x1b[22m\x1b[39m";
    expect(rawReason.length).toBeGreaterThan(2000);
    const items = buildDecisionInbox({
      proposals: [],
      foItems: [],
      foNowSec: NOW,
      interventions: [],
      kanbanDecisions: [
        {
          kind: "integration_parked",
          task_id: "t_2bf50879",
          title: "Integration t_2bf50879 parken",
          reason: rawReason,
          age_seconds: 8,
          suggested_command: null,
        },
      ],
    });
    const why = items[0].why;
    // AC-1: kein Steuerzeichen im gerenderten Grund.
    // eslint-disable-next-line no-control-regex -- Test prueft explizit auf ESC-Reste
    expect(why).not.toMatch(/\x1b/);
    // AC-2: auf die erste bedeutungstragende Zeile reduziert.
    expect(why).toContain(
      "integration parked: post-merge gate failed: frontend bootstrap: exit 1",
    );
    expect(why).not.toContain("BibliothekCorrectionEditor.test.tsx");
    expect(why).not.toContain("Failed Tests");
    // Ein Eintrag flutet die Inbox nicht mehr.
    expect(why.length).toBeLessThan(400);
    // AC-6: Ton und Ziel-Link bleiben unveraendert.
    expect(items[0].tone).toBe("amber");
    expect(items[0].target).toBe("/control/fleet?task=t_2bf50879");
  });

  it("integration_parked: sehr lange einzeilige Gruende werden sichtbar gekuerzt", () => {
    const items = buildDecisionInbox({
      proposals: [],
      foItems: [],
      foNowSec: NOW,
      interventions: [],
      kanbanDecisions: [
        {
          kind: "integration_parked",
          task_id: "t1",
          title: "Merge parked",
          reason: `integration parked: ${"x".repeat(400)}`,
          age_seconds: 8,
          suggested_command: null,
        },
      ],
    });
    // AC-3: die Kuerzung ist sichtbar, der Grund endet nicht stillschweigend.
    expect(items[0].why).toMatch(/…$/);
    expect(items[0].why).not.toContain("x".repeat(200));
  });

  it("kanban: kurze saubere Gruende anderer Eintragsarten bleiben unveraendert", () => {
    const items = buildDecisionInbox({
      proposals: [],
      foItems: [],
      foNowSec: NOW,
      interventions: [],
      kanbanDecisions: [
        {
          kind: "sticky_blocked",
          task_id: "t1",
          title: "Review pruefen",
          reason: "2 Reviews laufen seit 3h ohne Verdict",
          age_seconds: 99,
          suggested_command: null,
        },
      ],
    });
    // AC-5: Label, Signal und Grund exakt wie bisher zusammengesetzt.
    expect(items[0].why).toBe(
      "Blockiert — Unblock nötig · 2 Reviews laufen seit 3h ohne Verdict",
    );
  });

  it("R1: deliverable_posted_not_completed carries the inline repair anchor and outranks sticky", () => {
    const items = buildDecisionInbox({
      proposals: [],
      foItems: [],
      foNowSec: NOW,
      interventions: [],
      kanbanDecisions: [
        { kind: "sticky_blocked", task_id: "t2", title: "Blocked one", reason: "needs eyes", age_seconds: 99, suggested_command: null },
        { kind: "deliverable_posted_not_completed", task_id: "t1", title: "Deliverable da", reason: "kanban_complete fehlt", age_seconds: 10, suggested_command: "hermes kanban show t1" },
      ],
    });
    // deliverable_posted_not_completed (84) outranks sticky_blocked (75)
    expect(items[0].title).toBe("Deliverable da");
    expect(items[0].why).toContain("Repair nötig");
    expect(items[0].repairTaskId).toBe("t1");
    // only the deliverable row gets the repair anchor; it is not a fix-run row.
    expect(items[0].fixTaskId).toBeUndefined();
    expect(items[1].repairTaskId).toBeUndefined();
  });

  it("ignores rows without a task_id and tolerates an absent kanban source", () => {
    const items = buildDecisionInbox({
      proposals: [],
      foItems: [],
      foNowSec: NOW,
      interventions: [],
      kanbanDecisions: [
        { kind: "sticky_blocked", task_id: "", title: "no id", reason: "x", age_seconds: null, suggested_command: null },
      ],
    });
    expect(items).toEqual([]);
    // Absent kanbanDecisions entirely → no crash, empty.
    expect(buildDecisionInbox({ proposals: [], foItems: [], foNowSec: NOW, interventions: [] })).toEqual([]);
  });
});
