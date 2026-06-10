import { isActionable } from "./autoresearch";
import { workerHealth } from "./derive";
import {
  contractDriftCount,
  isStaleProof,
  nextActionForItem,
  projectFromRoot,
  readiness,
} from "./orchestration";
import type { OrchestrationBacklogResponse, OrchestrationItem } from "./schemas";
import type {
  HealthStatus,
  KanbanResult,
  MetricsLiteResponse,
  Proposal,
  SystemHealthResponse,
  ToneName,
  Worker,
} from "./types";

type ContractHealth = OrchestrationBacklogResponse["contract_health"];

export type CandidateKind = "dispatch" | "plan_gate" | "review";

export interface DispatchCandidate {
  id: string;
  title: string;
  project: string;
  priority: string;
  owner: string;
  action: string;
  kind: CandidateKind;
  tone: ToneName;
  item: OrchestrationItem;
}

export interface ProjectLane {
  project: string;
  activeItems: number;
  activeWorkers: number;
  ready: number;
  blocked: number;
  doing: number;
  review: number;
  highRisk: number;
  staleProof: number;
  unowned: number;
  nextAction: string;
}

export interface AgentOpsIntervention {
  id: string;
  tone: ToneName;
  title: string;
  detail: string;
  target: string;
}

export type AgentOpsDecisionKind = "launch" | "gate" | "unblock" | "review" | "hold" | "shape";

export interface AgentOpsDecision {
  kind: AgentOpsDecisionKind;
  tone: ToneName;
  title: string;
  detail: string;
  target: string;
}

export interface AgentOpsGap {
  id: string;
  tone: ToneName;
  label: string;
  count: number;
  detail: string;
  target: string;
}

export interface AgentOpsSnapshot {
  checkedAt: number;
  activeWorkers: number;
  healthyWorkers: number;
  parallelTarget: number;
  parallelSlotsFree: number;
  recommendedLaunches: number;
  completedRuns: number;
  verifiedResults: number;
  dispatchReady: number;
  planGates: number;
  blockedItems: number;
  reviewItems: number;
  staleProofItems: number;
  highRiskItems: number;
  unownedItems: number;
  openProposals: number;
  testingProposals: number;
  gatePassed: number;
  gateFailed: number;
  gateRunning: number;
  gatePassRate: number;
  systemStatus: HealthStatus | "unknown";
  errorRate: number;
  worstP95Ms: number;
  contractDrift: number;
  operatorDecision: AgentOpsDecision;
  readinessGaps: AgentOpsGap[];
  dispatchCandidates: DispatchCandidate[];
  projectLanes: ProjectLane[];
  interventions: AgentOpsIntervention[];
}

const PRIORITY_RANK: Record<string, number> = {
  high: 0,
  urgent: 0,
  medium: 1,
  med: 1,
  low: 2,
};

const PARALLEL_AGENT_TARGET = 4;

function priorityRank(priority: string): number {
  return PRIORITY_RANK[priority] ?? 3;
}

function candidateKind(item: OrchestrationItem, allItems: ReadonlyArray<OrchestrationItem>): CandidateKind | null {
  const r = readiness(item, allItems);
  if (item.status === "todo" && r.state === "ready" && item.planGate) return "plan_gate";
  if (item.status === "todo" && r.state === "ready") return "dispatch";
  if (item.status === "review") return "review";
  return null;
}

function candidateTone(kind: CandidateKind): ToneName {
  if (kind === "dispatch") return "emerald";
  if (kind === "plan_gate") return "amber";
  return "cyan";
}

function candidateRank(kind: CandidateKind): number {
  if (kind === "dispatch") return 0;
  if (kind === "plan_gate") return 1;
  return 2;
}

export function selectDispatchCandidates(
  items: ReadonlyArray<OrchestrationItem>,
  limit = 4,
): DispatchCandidate[] {
  return items
    .filter((item) => item.status !== "done")
    .map((item): DispatchCandidate | null => {
      const kind = candidateKind(item, items);
      if (!kind) return null;
      return {
        id: item.id,
        title: item.title,
        project: projectFromRoot(item.root),
        priority: item.priority || "n/a",
        owner: item.owner?.trim() || "unowned",
        action: nextActionForItem(item, items),
        kind,
        tone: candidateTone(kind),
        item,
      };
    })
    .filter((item): item is DispatchCandidate => item !== null)
    .sort((a, b) => (
      candidateRank(a.kind) - candidateRank(b.kind) ||
      priorityRank(a.priority) - priorityRank(b.priority) ||
      a.item.created.localeCompare(b.item.created) ||
      a.id.localeCompare(b.id)
    ))
    .slice(0, limit);
}

export function buildProjectLanes(
  items: ReadonlyArray<OrchestrationItem>,
  workers: ReadonlyArray<Worker>,
  nowSec: number,
): ProjectLane[] {
  const lanes = new Map<string, ProjectLane>();
  const itemById = new Map(items.map((item) => [item.id, item]));

  const ensure = (project: string): ProjectLane => {
    const existing = lanes.get(project);
    if (existing) return existing;
    const lane: ProjectLane = {
      project,
      activeItems: 0,
      activeWorkers: 0,
      ready: 0,
      blocked: 0,
      doing: 0,
      review: 0,
      highRisk: 0,
      staleProof: 0,
      unowned: 0,
      nextAction: "Einordnen",
    };
    lanes.set(project, lane);
    return lane;
  };

  for (const item of items) {
    if (item.status === "done") continue;
    const lane = ensure(projectFromRoot(item.root));
    const r = readiness(item, items);
    lane.activeItems += 1;
    if (r.state === "ready") lane.ready += 1;
    if (r.state === "blocked" || item.status === "blocked") lane.blocked += 1;
    if (item.status === "doing") lane.doing += 1;
    if (item.status === "review") lane.review += 1;
    if (item.priority === "high" || item.priority === "urgent") lane.highRisk += 1;
    if (isStaleProof(item, nowSec)) lane.staleProof += 1;
    if ((item.status === "doing" || item.status === "review") && !item.owner?.trim()) lane.unowned += 1;
  }

  for (const worker of workers) {
    const item = itemById.get(worker.task_id);
    const lane = ensure(item ? projectFromRoot(item.root) : "Hermes");
    lane.activeWorkers += 1;
  }

  for (const lane of lanes.values()) {
    const next = selectDispatchCandidates(
      items.filter((item) => projectFromRoot(item.root) === lane.project),
      1,
    )[0];
    lane.nextAction = next?.action ?? (
      lane.blocked > 0 ? "Blocker klaeren" :
      lane.review > 0 ? "Proof pruefen" :
      lane.doing > 0 ? "Fortschritt pruefen" :
      "Priorisieren"
    );
  }

  return [...lanes.values()].sort((a, b) => (
    b.blocked - a.blocked ||
    b.highRisk - a.highRisk ||
    b.activeWorkers - a.activeWorkers ||
    b.ready - a.ready ||
    b.activeItems - a.activeItems ||
    a.project.localeCompare(b.project)
  ));
}

function metricsSummary(metrics: MetricsLiteResponse | null): { errorRate: number; worstP95Ms: number } {
  if (!metrics || metrics.error) return { errorRate: 0, worstP95Ms: 0 };
  const groups = Object.values(metrics.groups);
  const requests = groups.reduce((sum, group) => sum + group.count, 0);
  const errors = groups.reduce((sum, group) => sum + group.error_count, 0);
  return {
    errorRate: requests > 0 ? errors / requests : 0,
    worstP95Ms: groups.reduce((max, group) => Math.max(max, group.p95_ms), 0),
  };
}

function gateSummary(proposals: ReadonlyArray<Proposal>): {
  testingProposals: number;
  gatePassed: number;
  gateFailed: number;
  gateRunning: number;
  gatePassRate: number;
} {
  // Gates nur auf noch OFFENEN Proposals zaehlen (proposed/testing). applied/skipped
  // tragen oft ein altes gate.phase (failed/crashed/passed) aus einem frueheren Lauf —
  // das sind keine aktuellen Gate-Resultate und wuerden sonst dauerhaft "rot" zaehlen.
  const openProposals = proposals.filter(
    (proposal) => proposal.status === "proposed" || proposal.status === "testing",
  );
  const gates = openProposals.map((proposal) => proposal.gate).filter((gate): gate is NonNullable<Proposal["gate"]> => Boolean(gate));
  const gatePassed = gates.filter((gate) => gate.phase === "passed").length;
  const gateFailed = gates.filter((gate) => gate.phase === "failed" || gate.phase === "crashed").length;
  const gateRunning = gates.filter((gate) => gate.phase === "running").length;
  const completed = gatePassed + gateFailed;
  return {
    testingProposals: proposals.filter((proposal) => proposal.status === "testing").length,
    gatePassed,
    gateFailed,
    gateRunning,
    gatePassRate: completed > 0 ? gatePassed / completed : 1,
  };
}

function buildOperatorDecision(input: {
  systemStatus: HealthStatus | "unknown";
  errorRate: number;
  activeWorkers: number;
  parallelTarget: number;
  dispatchReady: number;
  recommendedLaunches: number;
  planGates: number;
  blockedItems: number;
  reviewItems: number;
  contractDrift: number;
}): AgentOpsDecision {
  if (input.systemStatus === "offline") {
    return {
      kind: "hold",
      tone: "red",
      title: "Dispatch stoppen",
      detail: "Hermes ist offline. Erst System-Health stabilisieren, dann Agenten starten.",
      target: "/control",
    };
  }
  if (input.systemStatus === "degraded" || input.errorRate > 0.05) {
    return {
      kind: "hold",
      tone: "amber",
      title: "Dispatch halten",
      detail: `System ${input.systemStatus}, API-Fehler ${(input.errorRate * 100).toFixed(1)}%. Erst Druck klaeren.`,
      target: "/control",
    };
  }
  if (input.recommendedLaunches > 0) {
    const driftNote = input.contractDrift ? `, ${input.contractDrift} Drift-Signale parallel klaeren` : "";
    return {
      kind: "launch",
      tone: "emerald",
      title: "Startfenster offen",
      detail: `${input.recommendedLaunches} von ${input.parallelTarget} Agenten jetzt sinnvoll beauftragbar (${input.activeWorkers} aktiv${driftNote}).`,
      target: "/control/orchestrator",
    };
  }
  if (input.planGates > 0) {
    return {
      kind: "gate",
      tone: "amber",
      title: "Plan-Gates entscheiden",
      detail: `${input.planGates} Tasks warten auf eine Operator-Entscheidung statt Umsetzung.`,
      target: "/control/orchestrator",
    };
  }
  if (input.blockedItems > 0) {
    return {
      kind: "unblock",
      tone: "red",
      title: "Blocker loesen",
      detail: `${input.blockedItems} Tasks sind durch Dependencies oder Status blockiert.`,
      target: "/control/orchestrator",
    };
  }
  if (input.reviewItems > 0) {
    return {
      kind: "review",
      tone: "cyan",
      title: "Proofs pruefen",
      detail: `${input.reviewItems} Tasks sind im Review und brauchen Belegpruefung.`,
      target: "/control/orchestrator",
    };
  }
  if (input.activeWorkers >= input.parallelTarget) {
    return {
      kind: "hold",
      tone: "cyan",
      title: "Kapazitaet voll",
      detail: `${input.activeWorkers}/${input.parallelTarget} Agenten laufen. Erst Ergebnisse abwarten.`,
      target: "/control/flow",
    };
  }
  if (input.contractDrift > 0) {
    return {
      kind: "gate",
      tone: "amber",
      title: "Backlog-Vertrag saeubern",
      detail: `${input.contractDrift} Contract-Drift-Signale verhindern verlaessliche Priorisierung.`,
      target: "/control/orchestrator",
    };
  }
  return {
    kind: "shape",
    tone: "zinc",
    title: "Backlog schaerfen",
    detail: "Keine sofort dispatchbare Arbeit gefunden. Tasks in todo bringen oder Dependencies schliessen.",
    target: "/control/orchestrator",
  };
}

function buildReadinessGaps(input: {
  systemStatus: HealthStatus | "unknown";
  errorRate: number;
  worstP95Ms: number;
  dispatchReady: number;
  parallelSlotsFree: number;
  planGates: number;
  blockedItems: number;
  reviewItems: number;
  staleProofItems: number;
  unownedItems: number;
  contractDrift: number;
  openProposals: number;
  testingProposals: number;
  gateRunning: number;
  gateFailed: number;
}): AgentOpsGap[] {
  const gaps: AgentOpsGap[] = [];
  if (input.systemStatus !== "healthy") {
    gaps.push({
      id: "system-health",
      tone: input.systemStatus === "offline" ? "red" : "amber",
      label: "System-Health",
      count: 1,
      detail: input.systemStatus,
      target: "/control",
    });
  }
  if (input.errorRate > 0.05) {
    gaps.push({
      id: "api-errors",
      tone: "red",
      label: "API-Fehler",
      count: Math.round(input.errorRate * 100),
      detail: "Fehlerquote ueber 5%",
      target: "/control",
    });
  } else if (input.worstP95Ms > 1000) {
    gaps.push({
      id: "api-latency",
      tone: "amber",
      label: "API-Latenz",
      count: Math.round(input.worstP95Ms),
      detail: "p95 ueber 1000ms",
      target: "/control",
    });
  }
  if (input.parallelSlotsFree === 0 && input.dispatchReady > 0) {
    gaps.push({
      id: "capacity",
      tone: "cyan",
      label: "Kapazitaet",
      count: 0,
      detail: "Ready Tasks vorhanden, aber kein freier Agenten-Slot.",
      target: "/control/flow",
    });
  }
  if (input.contractDrift > 0) {
    gaps.push({
      id: "contract-drift",
      tone: "amber",
      label: "Contract Drift",
      count: input.contractDrift,
      detail: "Status, Prioritaeten oder Zaehlsumme passen nicht zum Vertrag.",
      target: "/control/orchestrator",
    });
  }
  if (input.blockedItems > 0) {
    gaps.push({
      id: "blocked-items",
      tone: "red",
      label: "Blocker",
      count: input.blockedItems,
      detail: "Dependencies oder Status blockieren Dispatch.",
      target: "/control/orchestrator",
    });
  }
  if (input.planGates > 0) {
    gaps.push({
      id: "plan-gates",
      tone: "amber",
      label: "Plan-Gates",
      count: input.planGates,
      detail: "Operator-Entscheidungen vor Umsetzung offen.",
      target: "/control/orchestrator",
    });
  }
  if (input.reviewItems > 0) {
    gaps.push({
      id: "review-items",
      tone: "cyan",
      label: "Reviews",
      count: input.reviewItems,
      detail: "Proof/Receipt muss geprueft werden.",
      target: "/control/orchestrator",
    });
  }
  if (input.unownedItems > 0) {
    gaps.push({
      id: "unowned-items",
      tone: "amber",
      label: "Owner fehlen",
      count: input.unownedItems,
      detail: "Laufende Tasks (doing/review) ohne Owner — niemand steht drauf.",
      target: "/control/orchestrator",
    });
  }
  if (input.staleProofItems > 0) {
    gaps.push({
      id: "stale-proof",
      tone: "rose",
      label: "Proof fehlt",
      count: input.staleProofItems,
      detail: "Aeltere aktive Tasks ohne letzten Beleg.",
      target: "/control/orchestrator",
    });
  }
  if (input.openProposals > 0) {
    gaps.push({
      id: "open-proposals",
      tone: "cyan",
      label: "Autoresearch",
      count: input.openProposals,
      detail: "Gepruefte Verbesserungen warten auf Entscheidung.",
      target: "/control/autoresearch",
    });
  }
  if (input.testingProposals > 0 || input.gateRunning > 0) {
    gaps.push({
      id: "running-gates",
      tone: "violet",
      label: "Tests laufen",
      count: input.testingProposals + input.gateRunning,
      detail: "Code-Proposals sind noch im Gate.",
      target: "/control/autoresearch",
    });
  }
  if (input.gateFailed > 0) {
    gaps.push({
      id: "failed-gates",
      tone: "red",
      label: "Gate rot",
      count: input.gateFailed,
      detail: "Fehlgeschlagene Code-Gates brauchen Review.",
      target: "/control/autoresearch",
    });
  }
  if (input.dispatchReady === 0 && input.planGates === 0 && input.blockedItems === 0) {
    gaps.push({
      id: "no-ready-work",
      tone: "zinc",
      label: "Ready Queue",
      count: 0,
      detail: "Keine sofort dispatchbare Arbeit im Orchestrator.",
      target: "/control/orchestrator",
    });
  }
  return gaps.slice(0, 8);
}

export function buildInterventions(input: {
  workers: ReadonlyArray<Worker>;
  proposals: ReadonlyArray<Proposal>;
  items: ReadonlyArray<OrchestrationItem>;
  contractHealth?: ContractHealth | null;
  systemHealth: SystemHealthResponse | null;
  metrics: MetricsLiteResponse | null;
  nowSec: number;
}): AgentOpsIntervention[] {
  const interventions: AgentOpsIntervention[] = [];
  const drift = contractDriftCount(input.contractHealth);
  const { errorRate, worstP95Ms } = metricsSummary(input.metrics);

  if (input.systemHealth && input.systemHealth.overall !== "healthy") {
    interventions.push({
      id: "system-health",
      tone: input.systemHealth.overall === "offline" ? "red" : "amber",
      title: "System-Health",
      detail: input.systemHealth.overall,
      target: "/control",
    });
  }

  for (const worker of input.workers) {
    const health = workerHealth(worker, input.nowSec);
    if (health.key === "healthy") continue;
    interventions.push({
      id: `worker-${worker.run_id}`,
      tone: health.tone,
      title: worker.task_title,
      detail: health.label,
      target: "/control/flow",
    });
  }

  if (drift > 0) {
    interventions.push({
      id: "contract-drift",
      tone: "amber",
      title: "Orchestrator Contract Drift",
      detail: `${drift} Signale`,
      target: "/control/orchestrator",
    });
  }

  const blocked = input.items.filter((item) => item.status !== "done" && readiness(item, input.items).state === "blocked");
  if (blocked.length > 0) {
    interventions.push({
      id: "blocked-items",
      tone: "red",
      title: "Dependencies blockieren",
      detail: `${blocked.length} Tasks`,
      target: "/control/orchestrator",
    });
  }

  const planGates = input.items.filter((item) => item.status === "todo" && item.planGate && readiness(item, input.items).state === "ready");
  if (planGates.length > 0) {
    interventions.push({
      id: "plan-gates",
      tone: "amber",
      title: "Plan-Gates offen",
      detail: `${planGates.length} Entscheidungen`,
      target: "/control/orchestrator",
    });
  }

  const openProposals = input.proposals.filter(isActionable);
  if (openProposals.length > 0) {
    interventions.push({
      id: "open-proposals",
      tone: "cyan",
      title: "Autoresearch Vorschlaege",
      detail: `${openProposals.length} offen`,
      target: "/control/autoresearch",
    });
  }

  if (errorRate > 0.05) {
    interventions.push({
      id: "metrics-error-rate",
      tone: "red",
      title: "API Fehlerquote",
      detail: `${(errorRate * 100).toFixed(1)}%`,
      target: "/control",
    });
  } else if (worstP95Ms > 1000) {
    interventions.push({
      id: "metrics-latency",
      tone: "amber",
      title: "API Latenz",
      detail: `p95 ${Math.round(worstP95Ms)}ms`,
      target: "/control",
    });
  }

  return interventions.slice(0, 8);
}

export function buildAgentOpsSnapshot(input: {
  workers: ReadonlyArray<Worker>;
  results: ReadonlyArray<KanbanResult>;
  proposals: ReadonlyArray<Proposal>;
  orchestrationItems: ReadonlyArray<OrchestrationItem>;
  contractHealth?: ContractHealth | null;
  systemHealth: SystemHealthResponse | null;
  metrics: MetricsLiteResponse | null;
  nowSec: number;
}): AgentOpsSnapshot {
  const workerHealths = input.workers.map((worker) => workerHealth(worker, input.nowSec));
  const dispatchCandidates = selectDispatchCandidates(input.orchestrationItems, 4);
  const gates = gateSummary(input.proposals);
  const dispatchReady = input.orchestrationItems.filter((item) =>
    item.status === "todo" &&
    !item.planGate &&
    readiness(item, input.orchestrationItems).state === "ready"
  ).length;
  const planGates = input.orchestrationItems.filter((item) =>
    item.status === "todo" &&
    item.planGate &&
    readiness(item, input.orchestrationItems).state === "ready"
  ).length;
  const blockedItems = input.orchestrationItems.filter((item) =>
    item.status !== "done" &&
    (item.status === "blocked" || readiness(item, input.orchestrationItems).state === "blocked")
  ).length;
  const activeItems = input.orchestrationItems.filter((item) => item.status !== "done");
  const { errorRate, worstP95Ms } = metricsSummary(input.metrics);
  const activeWorkers = input.workers.length;
  const parallelSlotsFree = Math.max(0, PARALLEL_AGENT_TARGET - activeWorkers);
  const recommendedLaunches = Math.min(dispatchReady, parallelSlotsFree);
  const reviewItems = input.orchestrationItems.filter((item) => item.status === "review").length;
  const staleProofItems = activeItems.filter((item) => isStaleProof(item, input.nowSec)).length;
  const highRiskItems = activeItems.filter((item) => item.priority === "high" || item.priority === "urgent").length;
  // Owner-Gap nur fuer aktiv bearbeitete Tasks (doing/review) ohne Owner —
  // die ruhige Queue (backlog/todo) darf unowned sein (vereinheitlichtes Claim-Modell).
  const unownedItems = activeItems.filter(
    (item) => (item.status === "doing" || item.status === "review") && !item.owner?.trim(),
  ).length;
  const openProposals = input.proposals.filter(isActionable).length;
  const contractDrift = contractDriftCount(input.contractHealth);
  const systemStatus = input.systemHealth?.overall ?? "unknown";
  const operatorDecision = buildOperatorDecision({
    systemStatus,
    errorRate,
    activeWorkers,
    parallelTarget: PARALLEL_AGENT_TARGET,
    dispatchReady,
    recommendedLaunches,
    planGates,
    blockedItems,
    reviewItems,
    contractDrift,
  });
  const readinessGaps = buildReadinessGaps({
    systemStatus,
    errorRate,
    worstP95Ms,
    dispatchReady,
    parallelSlotsFree,
    planGates,
    blockedItems,
    reviewItems,
    staleProofItems,
    unownedItems,
    contractDrift,
    openProposals,
    testingProposals: gates.testingProposals,
    gateRunning: gates.gateRunning,
    gateFailed: gates.gateFailed,
  });

  return {
    checkedAt: input.nowSec,
    activeWorkers,
    healthyWorkers: workerHealths.filter((health) => health.key === "healthy").length,
    parallelTarget: PARALLEL_AGENT_TARGET,
    parallelSlotsFree,
    recommendedLaunches,
    completedRuns: input.results.length,
    verifiedResults: input.results.filter((result) => result.verification.length > 0).length,
    dispatchReady,
    planGates,
    blockedItems,
    reviewItems,
    staleProofItems,
    highRiskItems,
    unownedItems,
    openProposals,
    testingProposals: gates.testingProposals,
    gatePassed: gates.gatePassed,
    gateFailed: gates.gateFailed,
    gateRunning: gates.gateRunning,
    gatePassRate: gates.gatePassRate,
    systemStatus,
    errorRate,
    worstP95Ms,
    contractDrift,
    operatorDecision,
    readinessGaps,
    dispatchCandidates,
    projectLanes: buildProjectLanes(input.orchestrationItems, input.workers, input.nowSec),
    interventions: buildInterventions({
      workers: input.workers,
      proposals: input.proposals,
      items: input.orchestrationItems,
      contractHealth: input.contractHealth,
      systemHealth: input.systemHealth,
      metrics: input.metrics,
      nowSec: input.nowSec,
    }),
  };
}

export function buildAgentOpsDispatchPrompt(item: OrchestrationItem): string {
  const root = item.root || "(root aus Spec lesen)";
  return [
    "Du bist eine Orchestrator-Session auf dem Homeserver. Arbeite genau diesen Task ab.",
    `TASK: ${item.title} (${item.id})`,
    `SPEC: ~/orchestration/backlog/${item.id}.md`,
    `ROOT: ${root}`,
    "1) Spec vollstaendig lesen, inklusive gate, dependsOn, Constraints und Subtasks.",
    `2) CLAIM zuerst (erste Handlung): in ~/orchestration/backlog/${item.id}.md "owner: <deine-Session-Kennung>" + "status: doing" eintragen und NUR diese Datei committen (Mini-Claim-Commit). Bei fremdem Claim/Konflikt: STOPP, Operator melden.`,
    "3) Isoliert arbeiten: eigener Branch/Worktree oder explizit bestaetigen, dass der ROOT sauber und exklusiv ist.",
    "4) Preflight im ROOT: git status --short; aktive Locks/Worker pruefen; fremde uncommitted Arbeit nicht veraendern.",
    "5) Wenn planGate:true: nur Plan liefern und stoppen.",
    "6) Sonst umsetzen, Gate aus Spec wirklich gruen fahren, Proof/Receipt dokumentieren.",
    "7) Bei UI/Backend in Hermes: build, service restart und Live-Checks nur nach Operator-Freigabe.",
  ].join("\n");
}

export function buildFourAgentLaunchBrief(snapshot: AgentOpsSnapshot): string {
  const candidateLines = snapshot.dispatchCandidates.slice(0, snapshot.parallelTarget).map((candidate, index) =>
    `${index + 1}. ${candidate.id} | ${candidate.project} | ${candidate.action} | ${candidate.title}`
  );
  const gapLines = snapshot.readinessGaps.slice(0, 5).map((gap) =>
    `- ${gap.label}: ${gap.count} | ${gap.detail}`
  );
  return [
    "Hermes 4-Agenten Startbrief",
    `Entscheidung: ${snapshot.operatorDecision.title}`,
    `Kapazitaet: ${snapshot.activeWorkers}/${snapshot.parallelTarget} aktiv | ${snapshot.parallelSlotsFree} Slots frei | ${snapshot.recommendedLaunches} jetzt starten`,
    `System: ${snapshot.systemStatus} | API Fehler ${(snapshot.errorRate * 100).toFixed(1)}% | p95 ${Math.round(snapshot.worstP95Ms)}ms`,
    "Preflight je Session: git status --short; fremde uncommitted Arbeit nicht anfassen; Spec vollstaendig lesen.",
    "Kandidaten:",
    ...(candidateLines.length ? candidateLines : ["- keine sofort dispatchbaren Kandidaten"]),
    "Readiness-Luecken:",
    ...(gapLines.length ? gapLines : ["- keine"]),
  ].join("\n");
}

export function buildMorningBrief(snapshot: AgentOpsSnapshot): string {
  const lines = [
    "Hermes Arbeitsstroeme Brief",
    `Entscheidung: ${snapshot.operatorDecision.title}`,
    `System: ${snapshot.systemStatus}`,
    `Worker: ${snapshot.healthyWorkers}/${snapshot.activeWorkers} gesund · Slots frei: ${snapshot.parallelSlotsFree}/${snapshot.parallelTarget}`,
    `Parallel bereit: ${snapshot.dispatchReady} dispatch · ${snapshot.planGates} plan-gate · ${snapshot.blockedItems} blockiert`,
    `Proof: ${snapshot.verifiedResults}/${snapshot.completedRuns} Receipts mit Verifikation · Gates ${snapshot.gatePassed}/${snapshot.gatePassed + snapshot.gateFailed} passed`,
    `Risiko: ${snapshot.highRiskItems} high · ${snapshot.staleProofItems} stale proof · ${snapshot.unownedItems} unowned`,
    `Autoresearch: ${snapshot.openProposals} offen`,
    `API: Fehler ${(snapshot.errorRate * 100).toFixed(1)}% · p95 ${Math.round(snapshot.worstP95Ms)}ms`,
    `Contract Drift: ${snapshot.contractDrift}`,
    "Naechste Kandidaten:",
    ...snapshot.dispatchCandidates.slice(0, 4).map((candidate, index) =>
      `${index + 1}. ${candidate.id} · ${candidate.project} · ${candidate.action} · ${candidate.title}`
    ),
    "Interventionen:",
    ...(snapshot.interventions.length
      ? snapshot.interventions.map((item) => `- ${item.title}: ${item.detail}`)
      : ["- keine"]),
  ];
  return lines.join("\n");
}
