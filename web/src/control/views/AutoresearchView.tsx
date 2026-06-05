import { useEffect, useMemo, useState } from "react";
import { Archive, ArrowDown, CheckCheck, ClipboardCheck, FlaskConical, GitPullRequestArrow, ListChecks, Play, Radar, RotateCw, SearchCode, Settings2, ShieldCheck, Sparkles, Square, Target, X } from "lucide-react";
import { Button } from "@nous-research/ui/ui/components/button";
import { Spinner } from "@nous-research/ui/ui/components/spinner";
import { cn } from "@/lib/utils";
import { fetchJSON } from "@/lib/api";
import type { AuxiliaryModelsResponse, ModelOptionsResponse } from "@/lib/api";
import { ModelPickerDialog } from "@/components/ModelPickerDialog";
import { useAutoresearchRuns, useAutoresearchStatus, useDeepAudit, useTestFoundry, type DeepAuditFinding, type useProposals } from "../hooks/useControlData";
import { getAutoresearchActionPlan, type AutoresearchActionHint } from "../lib/autoresearchActionPlan";
import { AUTORESEARCH_ADVANCED_GUIDE, type AutoresearchAdvancedGuideItem } from "../lib/autoresearchAdvanced";
import { getAutoresearchActivityCard, type AutoresearchActivityCard } from "../lib/autoresearchActivity";
import { fmtClock } from "../lib/derive";
import { AUTORESEARCH_AREAS, clampLoopIterations, clearProposalSelection, codeWeaknessBusyKey, describeArea, describeAutoresearchBusy, describeLoopStatus, formatResearchTokens, formatRunTime, hasResearchCounters, parseMinUseCount, rankAutoresearchReviewQueue, readLastRunCounters, runLaneLabel, runLaneTone, runModelLabel, runVetoedCount, selectVisibleProposals, severityDistribution, severityTone, shouldShowResearchErrorBadge, splitAutoresearchProposals, summarizeProposalRoi, summarizeRecentRuns, sumRunTokens, toggleProposalSelection } from "../lib/autoresearch";
import { getAutoresearchKeyboardAction } from "../lib/autoresearchKeyboard";
import { AUTORESEARCH_SECTION_NAV, type AutoresearchSectionNavItem } from "../lib/autoresearchNavigation";
import { getAutoresearchRecommendation } from "../lib/autoresearchRecommendation";
import { getAutoresearchReadiness, type AutoresearchReadinessSummary } from "../lib/autoresearchReadiness";
import { getAutoresearchResolvedSummary, type AutoresearchResolvedSummary } from "../lib/autoresearchResolvedSummary";
import { filterAutoresearchQueueByMode, getAutoresearchEmptyQueueModeGuidance, getAutoresearchQueueModeSummary, type AutoresearchEmptyQueueModeGuidance, type AutoresearchQueueMode } from "../lib/autoresearchQueueMode";
import { canApplyAllOpenSkillProposals, canBatchConfirmAutoresearchSelection, describeTopCardMode, getAutoresearchDecisionGuide, getAutoresearchQueueActionSummary, getBatchSafeVisibleProposalIds, proposalNeedsManualReview, type AutoresearchDecisionGuide, type AutoresearchQueueActionSummary } from "../lib/autoresearchDecisionGuide";
import { getAutoresearchReviewFlow, type AutoresearchReviewFlow } from "../lib/autoresearchReviewFlow";
import { getAdvancedRunChecklist, getDeepAuditGuidance, getResearchLoopGuidance, getResearchLoopPreset, getResearchLoopStartChecklist, getResearchLoopStartControl, getResearchLoopStartSummary, getSelectedResearchLoopPresetId, RESEARCH_LOOP_PRESETS, getTestFoundryGuidance, type AutoresearchAdvancedRunChecklist, type AutoresearchRunGuidance, type ResearchLoopPresetId, type ResearchLoopStartChecklist, type ResearchLoopStartSummary } from "../lib/autoresearchRunGuidance";
import { getAutoresearchLastRunBrief, getAutoresearchRunCard, getAutoresearchRunSummary, type AutoresearchRunCard, type AutoresearchRunSummary } from "../lib/autoresearchRunSummary";
import { getProposalOperatorBrief } from "../lib/autoresearchProposalBrief";
import { de } from "../i18n/de";
import type { Density } from "../hooks/useDensity";
import type { AutoresearchRun, ToneName } from "../lib/types";
import { StatusPill, ToneCallout } from "../components/atoms";
import { ProposalCard } from "../components/ProposalCard";

type ProposalStore = ReturnType<typeof useProposals>;
type LaneModelSlot = "skills_hub" | "code_audit" | "test_hardening";
type PruneMessage = { tone: "emerald" | "amber" | "red"; text: string };

const LANE_MODEL_SLOTS: readonly { task: LaneModelSlot; lane: string; label: string; hint: string }[] = [
  { task: "skills_hub", lane: "Skill+Code", label: "Skills Hub", hint: "Skill- und Code-Lane" },
  { task: "code_audit", lane: "Deep-Audit", label: "Code Audit", hint: "Deep-Audit-Lane" },
  { task: "test_hardening", lane: "Test-Foundry", label: "Test Hardening", hint: "Test-Foundry-Lane" },
];

export function AutoresearchView({ density, store }: { density: Density; store: ProposalStore }) {
  const status = useAutoresearchStatus();
  const runs = useAutoresearchRuns();
  const deepAudit = useDeepAudit();
  const testFoundry = useTestFoundry();
  const split = useMemo(() => splitAutoresearchProposals(store.proposals), [store.proposals]);
  const open = split.actionable;
  const reverted = split.reverted;
  const applied = split.applied;
  const skipped = split.skipped;
  const [queueMode, setQueueMode] = useState<AutoresearchQueueMode>("all");
  const distribution = useMemo(() => severityDistribution(open), [open]);
  const queueModeSummary = useMemo(() => getAutoresearchQueueModeSummary(open, queueMode), [open, queueMode]);
  const filteredOpen = useMemo(() => filterAutoresearchQueueByMode(open, queueMode), [open, queueMode]);
  const emptyQueueModeGuidance = useMemo(() => getAutoresearchEmptyQueueModeGuidance(queueModeSummary), [queueModeSummary]);
  const filteredDistribution = useMemo(() => severityDistribution(filteredOpen), [filteredOpen]);
  const relevanceQueue = useMemo(() => rankAutoresearchReviewQueue(filteredOpen, 10), [filteredOpen]);
  const queueProposalIds = useMemo(() => [...relevanceQueue.shortlist, ...relevanceQueue.backlog].map((item) => item.proposal.id), [relevanceQueue.backlog, relevanceQueue.shortlist]);
  // BLOCKER FIX: "Sichtbare auswählen" must only target the shortlist the
  // operator actually sees, never the backlog hidden in the collapsed <details>.
  const visibleProposalIds = useMemo(() => relevanceQueue.shortlist.map((item) => item.proposal.id), [relevanceQueue.shortlist]);
  const visibleProposals = useMemo(() => relevanceQueue.shortlist.map((item) => item.proposal), [relevanceQueue.shortlist]);
  const batchSafeVisibleProposalIds = useMemo(() => getBatchSafeVisibleProposalIds(visibleProposals), [visibleProposals]);
  const manualReviewVisibleCount = visibleProposalIds.length - batchSafeVisibleProposalIds.length;
  const [selectedProposalIds, setSelectedProposalIds] = useState<Set<string>>(() => new Set());
  const statusTone = status.data?.state === "crashed" ? "red" : status.data?.heartbeat_fresh ? "cyan" : "amber";
  const loop = describeLoopStatus(status.data);
  const recommendation = useMemo(
    () => getAutoresearchRecommendation({
      state: status.data?.state,
      openCount: open.length,
      revertedCount: reverted.length,
      loopRunning: loop.running,
      routeStatus: status.data?.route_status,
    }),
    [loop.running, open.length, reverted.length, status.data?.route_status, status.data?.state],
  );
  const highPriorityCount = distribution.bySeverity.critical + distribution.bySeverity.high;
  const filteredHighPriorityCount = filteredDistribution.bySeverity.critical + filteredDistribution.bySeverity.high;
  const topProposal = relevanceQueue.shortlist[0]?.proposal ?? null;
  const topCardMode = topProposal ? describeTopCardMode(topProposal) : null;
  const topProposalBrief = useMemo(() => topProposal ? getProposalOperatorBrief(topProposal) : null, [topProposal]);
  const [maxIterations, setMaxIterations] = useState("2");
  const [area, setArea] = useState("all");
  const [focus, setFocus] = useState("recommended_sections");
  const [minUseCount, setMinUseCount] = useState("");
  const [deepAuditSubsystem, setDeepAuditSubsystem] = useState("");
  const [deepAuditFocus, setDeepAuditFocus] = useState("");
  const [deepAuditMessage, setDeepAuditMessage] = useState<string | null>(null);
  const [testFoundryTarget, setTestFoundryTarget] = useState("");
  const [testFoundryApply, setTestFoundryApply] = useState(false);
  const [testFoundryMessage, setTestFoundryMessage] = useState<string | null>(null);
  const [loopBusy, setLoopBusy] = useState<"start" | "stop" | null>(null);
  const [loopMessage, setLoopMessage] = useState<string | null>(null);
  const [pruneBusy, setPruneBusy] = useState(false);
  const [pruneMessage, setPruneMessage] = useState<PruneMessage | null>(null);
  const [bulkRevertedBusy, setBulkRevertedBusy] = useState(false);
  const selectedIds = useMemo(() => queueProposalIds.filter((id) => selectedProposalIds.has(id)), [queueProposalIds, selectedProposalIds]);
  const selectedProposals = useMemo(() => {
    const byId = new Map([...relevanceQueue.shortlist, ...relevanceQueue.backlog].map((item) => [item.proposal.id, item.proposal]));
    return selectedIds.flatMap((id) => {
      const proposal = byId.get(id);
      return proposal ? [proposal] : [];
    });
  }, [relevanceQueue.backlog, relevanceQueue.shortlist, selectedIds]);
  const selectedManualReviewCount = useMemo(() => selectedProposals.filter(proposalNeedsManualReview).length, [selectedProposals]);
  const reviewFlow = useMemo(
    () => getAutoresearchReviewFlow({
      openCount: filteredOpen.length,
      decidedCount: applied.length + skipped.length + reverted.length,
      selectedCount: selectedIds.length,
      visibleCount: visibleProposalIds.length,
      batchSafeVisibleCount: batchSafeVisibleProposalIds.length,
      highPriorityCount: filteredHighPriorityCount,
      selectedManualReviewCount,
      backlogCount: relevanceQueue.summary.remaining,
      revertedCount: reverted.length,
      topTitle: topProposal?.title?.trim() || topProposal?.target,
    }),
    [applied.length, batchSafeVisibleProposalIds.length, filteredHighPriorityCount, filteredOpen.length, relevanceQueue.summary.remaining, reverted.length, selectedIds.length, selectedManualReviewCount, skipped.length, topProposal?.target, topProposal?.title, visibleProposalIds.length],
  );
  const decisionGuide = useMemo(
    () => getAutoresearchDecisionGuide({
      visibleProposals,
      selectedProposals,
      openCount: filteredOpen.length,
      selectedCount: selectedIds.length,
      backlogCount: relevanceQueue.summary.remaining,
      revertedCount: reverted.length,
      topTitle: topProposal?.title?.trim() || topProposal?.target,
    }),
    [filteredOpen.length, relevanceQueue.summary.remaining, reverted.length, selectedIds.length, selectedProposals, topProposal?.target, topProposal?.title, visibleProposals],
  );
  const queueActionSummary = useMemo(
    () => getAutoresearchQueueActionSummary({
      visibleCount: visibleProposalIds.length,
      batchSafeVisibleCount: batchSafeVisibleProposalIds.length,
      manualReviewVisibleCount,
      selectedCount: selectedIds.length,
      selectedManualReviewCount,
    }),
    [batchSafeVisibleProposalIds.length, manualReviewVisibleCount, selectedIds.length, selectedManualReviewCount, visibleProposalIds.length],
  );
  const batchBusy = store.busy === "confirm-batch";
  const openSkillManualReviewCount = useMemo(() => store.openSkillProposals.filter(proposalNeedsManualReview).length, [store.openSkillProposals]);
  const deepAuditRunning = deepAudit.status?.state === "running";
  const testFoundryRunning = testFoundry.status?.state === "running";
  const routeOk = loop.routeTone === "emerald";
  const canApplyAllOpenSkills = canApplyAllOpenSkillProposals({
    openSkillProposals: store.openSkillProposals,
    busy: !!store.busy,
  });
  const canConfirmSelection = canBatchConfirmAutoresearchSelection({
    selectedCount: selectedIds.length,
    selectedManualReviewCount,
    busy: batchBusy,
  });
  const anyActionBusy = !!store.busy || !!loopBusy || pruneBusy || bulkRevertedBusy || deepAudit.busy || testFoundry.busy || deepAuditRunning || testFoundryRunning;
  const actionPlan = useMemo(
    () => getAutoresearchActionPlan({
      routeOk,
      loopRunning: loop.running,
      openCount: open.length,
      highPriorityCount,
      openSkillCount: store.openSkillProposals.length,
      openSkillManualReviewCount,
      revertedCount: reverted.length,
      storeBusy: !!store.busy,
      pruneBusy,
    }),
    [highPriorityCount, loop.running, open.length, openSkillManualReviewCount, pruneBusy, reverted.length, routeOk, store.busy, store.openSkillProposals.length],
  );
  const busyNotice = describeAutoresearchBusy(store.busy);
  const latestActivity = store.activity[0] ?? null;
  const deepAuditHasFindings = (deepAudit.findings?.findings.length ?? 0) > 0 || (deepAudit.findings?.proposals.length ?? 0) > 0;
  const advancedNeedsAttention = deepAuditRunning || testFoundryRunning || deepAuditHasFindings || !!deepAudit.error || !!testFoundry.error || !!deepAuditMessage || !!testFoundryMessage;
  const [advancedOpen, setAdvancedOpen] = useState(false);
  const effectiveAdvancedOpen = advancedNeedsAttention || advancedOpen;
  const effectiveDeepAuditSubsystem = deepAuditSubsystem || deepAudit.subsystems[0] || "";
  const effectiveTestFoundryTarget = testFoundryTarget || testFoundry.targets[0] || "";
  const deepAuditGuidance = useMemo(
    () => getDeepAuditGuidance({ subsystem: effectiveDeepAuditSubsystem, running: deepAuditRunning }),
    [deepAuditRunning, effectiveDeepAuditSubsystem],
  );
  const deepAuditChecklist = useMemo(
    () => getAdvancedRunChecklist({
      kind: "deep-audit",
      target: effectiveDeepAuditSubsystem,
      running: deepAuditRunning,
      busy: deepAudit.busy,
    }),
    [deepAudit.busy, deepAuditRunning, effectiveDeepAuditSubsystem],
  );
  const testFoundryGuidance = useMemo(
    () => getTestFoundryGuidance({ target: effectiveTestFoundryTarget, running: testFoundryRunning, autoApply: testFoundryApply }),
    [effectiveTestFoundryTarget, testFoundryApply, testFoundryRunning],
  );
  const testFoundryChecklist = useMemo(
    () => getAdvancedRunChecklist({
      kind: "test-foundry",
      target: effectiveTestFoundryTarget,
      running: testFoundryRunning,
      busy: testFoundry.busy,
      autoApply: testFoundryApply,
    }),
    [effectiveTestFoundryTarget, testFoundry.busy, testFoundryApply, testFoundryRunning],
  );
  const readiness = useMemo(
    () => getAutoresearchReadiness({
      state: status.data?.state,
      routeStatus: status.data?.route_status,
      heartbeatFresh: status.data?.heartbeat_fresh,
      loopRunning: loop.running,
      openCount: open.length,
      highPriorityCount,
      busy: anyActionBusy,
    }),
    [anyActionBusy, highPriorityCount, loop.running, open.length, status.data?.heartbeat_fresh, status.data?.route_status, status.data?.state],
  );
  const researchLoopGuidance = useMemo(
    () => getResearchLoopGuidance({ running: loop.running, routeOk, maxIterations: clampLoopIterations(Number(maxIterations)), area: describeArea(area) }),
    [area, loop.running, maxIterations, routeOk],
  );
  const researchLoopStart = useMemo(
    () => getResearchLoopStartControl({ running: loop.running, busy: !!loopBusy, routeOk }),
    [loop.running, loopBusy, routeOk],
  );
  const selectedLoopPresetId = useMemo(
    () => getSelectedResearchLoopPresetId({ area, focus, maxIterations, minUseCount }),
    [area, focus, maxIterations, minUseCount],
  );
  const researchLoopStartSummary = useMemo(
    () => getResearchLoopStartSummary({
      selectedPresetId: selectedLoopPresetId,
      areaLabel: describeArea(area),
      focus,
      maxIterations: clampLoopIterations(Number(maxIterations)),
      minUseCount: parseMinUseCount(minUseCount),
    }),
    [area, focus, maxIterations, minUseCount, selectedLoopPresetId],
  );
  const researchLoopStartChecklist = useMemo(
    () => getResearchLoopStartChecklist({
      routeOk,
      running: loop.running,
      busy: !!loopBusy,
      selectedPresetId: selectedLoopPresetId,
      maxIterations: clampLoopIterations(Number(maxIterations)),
      openCount: open.length,
      highPriorityCount,
    }),
    [highPriorityCount, loop.running, loopBusy, maxIterations, open.length, routeOk, selectedLoopPresetId],
  );
  const resolvedSummary = useMemo(
    () => getAutoresearchResolvedSummary({ reverted, applied, skipped }),
    [applied, reverted, skipped],
  );
  const applyLoopPreset = (presetId: ResearchLoopPresetId) => {
    const preset = getResearchLoopPreset(presetId);
    setArea(preset.area);
    setFocus(preset.focus);
    setMinUseCount(preset.minUseCount);
    setMaxIterations(preset.maxIterations);
  };

  const startLoop = async () => {
    setLoopBusy("start");
    setLoopMessage(null);
    try {
      const body: Record<string, unknown> = { area: area.trim() || "all", focus: focus.trim() || "recommended_sections", mode: "dry-run", confirm: false, max_iterations: clampLoopIterations(Number(maxIterations)) };
      const muc = parseMinUseCount(minUseCount);
      if (muc !== null) body.min_use_count = muc;
      const result = await fetchJSON<{ request_id?: string; pid?: number }>("/api/autoresearch/trigger", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify(body),
      });
      setLoopMessage(`Research-Loop gestartet${result.request_id ? ` · ${result.request_id}` : ""}`);
      await status.reload();
    } catch (e) {
      setLoopMessage(`Loop-Start fehlgeschlagen: ${e instanceof Error ? e.message : String(e)}`);
    } finally {
      setLoopBusy(null);
    }
  };

  const stopLoop = async () => {
    setLoopBusy("stop");
    setLoopMessage(null);
    try {
      const result = await fetchJSON<{ ok?: boolean; detail?: string }>("/api/autoresearch/stop", { method: "POST" });
      setLoopMessage(result.detail || "Stop-Signal gesendet");
      await status.reload();
    } catch (e) {
      setLoopMessage(`Stop fehlgeschlagen: ${e instanceof Error ? e.message : String(e)}`);
    } finally {
      setLoopBusy(null);
    }
  };

  const startDeepAudit = async () => {
    if (!effectiveDeepAuditSubsystem) return;
    setDeepAuditMessage(null);
    try {
      const result = await deepAudit.trigger(effectiveDeepAuditSubsystem, deepAuditFocus.trim(), 12);
      setDeepAuditMessage(`Deep-Audit gestartet${result.request_id ? ` · ${result.request_id}` : ""}`);
    } catch (e) {
      setDeepAuditMessage(`Deep-Audit fehlgeschlagen: ${e instanceof Error ? e.message : String(e)}`);
    }
  };

  const startTestFoundry = async () => {
    if (!effectiveTestFoundryTarget) return;
    setTestFoundryMessage(null);
    try {
      const result = await testFoundry.trigger(effectiveTestFoundryTarget, testFoundryApply);
      setTestFoundryMessage(`Test-Foundry gestartet${result.pid ? ` · PID ${result.pid}` : ""}`);
    } catch (e) {
      setTestFoundryMessage(`Test-Foundry fehlgeschlagen: ${e instanceof Error ? e.message : String(e)}`);
    }
  };

  const scrollTo = (id: string) => {
    document.getElementById(id)?.scrollIntoView({ behavior: "smooth", block: "start" });
  };
  const focusProposal = (proposalId: string) => {
    document.getElementById(`autoresearch-proposal-${proposalId}`)?.scrollIntoView({ behavior: "smooth", block: "center" });
  };

  const runPrimaryRecommendation = () => {
    if (recommendation.kind === "review") {
      scrollTo("autoresearch-queue");
      return;
    }
    if (recommendation.kind === "monitor" || recommendation.kind === "recover" || recommendation.kind === "inspect") {
      scrollTo("autoresearch-loop");
      return;
    }
    void store.generate();
  };

  const toggleSelection = (proposalId: string, selected: boolean) => {
    setSelectedProposalIds((current) => toggleProposalSelection(current, proposalId, selected));
  };

  const selectQueue = () => setSelectedProposalIds(selectVisibleProposals(batchSafeVisibleProposalIds));
  const clearSelection = () => setSelectedProposalIds(clearProposalSelection());
  const confirmSelected = async () => {
    await store.confirmBatch(selectedIds);
  };

  const selectOrFocusTopProposal = () => {
    if (!topProposal) return;
    if (proposalNeedsManualReview(topProposal)) {
      focusProposal(topProposal.id);
      return;
    }
    toggleSelection(topProposal.id, true);
  };

  useEffect(() => {
    const onKey = (event: KeyboardEvent) => {
      const target = event.target as HTMLElement | null;
      if (target?.closest("input,textarea,[contenteditable='true'],[role='dialog']")) return;
      const top = relevanceQueue.shortlist.find((item) => item.proposal.status === "proposed")?.proposal ?? null;
      const action = getAutoresearchKeyboardAction({
        key: event.key,
        hasTopProposal: !!top,
        hasVisibleProposals: visibleProposalIds.length > 0,
        hasSelection: selectedIds.length > 0,
      });
      if (!action) return;
      event.preventDefault();
      if (action === "select-top" && top) {
        if (proposalNeedsManualReview(top)) {
          focusProposal(top.id);
        } else {
          setSelectedProposalIds((current) => toggleProposalSelection(current, top.id, true));
        }
      }
      if (action === "select-visible") setSelectedProposalIds(selectVisibleProposals(batchSafeVisibleProposalIds));
      if (action === "clear-selection") setSelectedProposalIds(clearProposalSelection());
    };
    window.addEventListener("keydown", onKey);
    return () => window.removeEventListener("keydown", onKey);
  }, [batchSafeVisibleProposalIds, relevanceQueue.shortlist, selectedIds.length, visibleProposalIds]);

  const pruneAutoresearch = async () => {
    setPruneBusy(true);
    setPruneMessage(null);
    try {
      const result = await fetchJSON<{ ok?: boolean; archived?: number; auto_skipped?: number; detail?: string }>("/api/autoresearch/prune", { method: "POST" });
      setPruneMessage({
        tone: result.ok === false ? "amber" : "emerald",
        text: de.autoresearch.pruneResult(result.archived ?? 0, result.auto_skipped ?? 0),
      });
      await Promise.all([store.reload(), runs.reload()]);
    } catch (e) {
      setPruneMessage({ tone: "red", text: `${de.autoresearch.pruneFailed}: ${e instanceof Error ? e.message : String(e)}` });
    } finally {
      setPruneBusy(false);
    }
  };

  const skipAllReverted = async () => {
    if (reverted.length === 0) return;
    setBulkRevertedBusy(true);
    try {
      for (const proposal of reverted) {
        await store.skip(proposal);
      }
    } finally {
      setBulkRevertedBusy(false);
    }
  };

  const runReviewFlowPrimary = () => {
    if (open.length > 0 && filteredOpen.length === 0) {
      return;
    }
    if (reviewFlow.primaryAction === "confirm-selection") {
      void confirmSelected();
      return;
    }
    if (reviewFlow.primaryAction === "select-visible") {
      selectQueue();
      return;
    }
    if (reviewFlow.primaryAction === "clear-selection") {
      clearSelection();
      return;
    }
    if (reviewFlow.primaryAction === "archive-reverted") {
      void skipAllReverted();
      return;
    }
    if (reviewFlow.primaryAction === "generate") {
      void store.generate();
      return;
    }
    selectOrFocusTopProposal();
  };

  return (
    <div className="space-y-5">
      <section className="hc-card overflow-hidden border-[var(--hc-border-strong)]">
        <div className="grid gap-0 xl:grid-cols-[minmax(0,1.05fr)_minmax(420px,.95fr)]">
          <div className="space-y-5 p-4 sm:p-6">
            <div className="flex flex-wrap items-center gap-2">
              {status.loading ? <Spinner /> : <StatusPill tone={statusTone} label={status.data?.state ?? "unbekannt"} dot={loop.running ? "live" : status.data?.state === "crashed" ? "error" : "idle"} />}
              <StatusPill tone={loop.routeTone} label={`Route ${status.data?.route_status ?? "unbekannt"}`} dot={loop.routeTone === "emerald" ? "ready" : "warn"} />
              <StatusPill tone={recommendation.tone} label={recommendation.eyebrow} />
              <span className="rounded-full border border-white/10 px-2.5 py-1 text-xs hc-soft">{loop.iterationLabel}</span>
            </div>
            <div>
              <p className="hc-eyebrow">Autoresearch Cockpit</p>
              <h1 className="mt-2 max-w-3xl text-2xl font-semibold leading-tight text-white sm:text-3xl">
                {recommendation.title}
              </h1>
              <p className="mt-3 max-w-2xl text-sm leading-6 hc-soft sm:text-base sm:leading-7">
                {recommendation.detail}
              </p>
              {topProposal ? (
                <div className="mt-3 max-w-3xl rounded-lg border border-white/10 bg-white/[.03] px-3 py-3 text-sm text-white">
                  <div className="flex flex-col gap-3 sm:flex-row sm:items-start sm:justify-between">
                    <div className="min-w-0">
                      <div className="flex flex-wrap items-center gap-2">
                        <p className="hc-eyebrow">Als Erstes</p>
                        {topProposalBrief ? <StatusPill tone={topProposalBrief.tone} label={topProposalBrief.label} /> : null}
                        {topCardMode ? <StatusPill tone={topCardMode.tone} label={topCardMode.label} /> : null}
                      </div>
                      <h2 className="mt-2 text-base font-semibold leading-6 text-white">{topProposalBrief?.title ?? "Nächste Karte prüfen."}</h2>
                      <p className="mt-1 text-sm leading-6 hc-soft">{topProposalBrief?.summary ?? topProposal.title?.trim() ?? topProposal.target}</p>
                      {topCardMode ? <p className="mt-1 text-xs leading-5 hc-dim">{topCardMode.detail}</p> : null}
                    </div>
                    <Button outlined className="hc-hit shrink-0 justify-center" onClick={() => focusProposal(topProposal.id)} prefix={<ArrowDown className="h-4 w-4" />}>
                      Top-Karte öffnen
                    </Button>
                  </div>
                  {topProposalBrief ? (
                    <div className="mt-3 grid gap-2 sm:grid-cols-3">
                      {topProposalBrief.facts.map((fact) => (
                        <div key={fact.label} className={cn("min-w-0 rounded-md border px-2.5 py-2", reviewStepToneClass(fact.tone))}>
                          <p className="text-[10px] font-semibold uppercase tracking-[.12em] hc-dim">{fact.label}</p>
                          <p className="mt-1 line-clamp-2 text-xs leading-5 hc-soft" title={fact.value}>{fact.value}</p>
                        </div>
                      ))}
                    </div>
                  ) : null}
                </div>
              ) : null}
              {status.error ? <p className="mt-2 text-sm text-red-200">{status.error}</p> : null}
            </div>
            <ReadinessPanel summary={readiness} />
            <div className="grid gap-3 sm:grid-cols-4">
              <Metric label="Offen" value={String(open.length)} />
              <Metric label="Hoch+" value={String(highPriorityCount)} />
              <Metric label="Zurückgerollt" value={String(reverted.length)} />
              <Metric label="Letzter Lauf" value={runs.data?.runs?.[0] ? formatRunTime(runs.data.runs[0].at) : "-"} />
            </div>
            <div className="flex flex-col gap-2 sm:flex-row sm:flex-wrap">
              <Button className="hc-hit" onClick={runPrimaryRecommendation} disabled={recommendation.kind === "generate" && !!store.busy} prefix={recommendation.kind === "review" ? <ArrowDown className="h-4 w-4" /> : recommendation.kind === "monitor" || recommendation.kind === "recover" || recommendation.kind === "inspect" ? <Radar className="h-4 w-4" /> : store.busy === "generate" ? <Spinner /> : <Sparkles className="h-4 w-4" />}>
                {recommendation.primaryLabel}
              </Button>
            </div>
            <CockpitSectionNav items={AUTORESEARCH_SECTION_NAV} onJump={scrollTo} />
          </div>
          <div className="border-t border-[var(--hc-border)] bg-black/20 p-4 sm:p-5 xl:border-l xl:border-t-0">
            <div className="grid gap-3 md:grid-cols-2">
              <OperatorActionCard
                icon={<Sparkles className="h-5 w-5" />}
                eyebrow="Schnell"
                hint={actionPlan.generate}
                title="Skill-Vorschläge holen"
                body="Sofort neue Kandidaten aus genutzten Skills erzeugen."
                button={<Button className="hc-hit w-full justify-center" onClick={store.generate} disabled={!!store.busy} title={de.autoresearch.generateHint} prefix={store.busy === "generate" ? <Spinner /> : <RotateCw className="h-4 w-4" />}>Vorschläge erzeugen</Button>}
              />
              <OperatorActionCard
                icon={<FlaskConical className="h-5 w-5" />}
                eyebrow="Code"
                hint={actionPlan.scan}
                title="Schwächen finden"
                body="Findet Code-Risiken und legt gegatete Vorschläge an."
                button={
                  <div className="space-y-2">
                    <Button outlined className="hc-hit w-full justify-center" onClick={() => store.generateCodeWeaknesses("incremental")} disabled={!!store.busy} title={de.autoresearch.scanScopeHintChanged} prefix={store.busy === codeWeaknessBusyKey("incremental") ? <Spinner /> : <FlaskConical className="h-4 w-4" />}>
                      Geänderte Dateien scannen
                    </Button>
                    <details className="rounded-lg border border-white/10 bg-black/20 p-2">
                      <summary className="cursor-pointer text-xs font-semibold text-white">Mehr Umfang</summary>
                      <div className="mt-2 grid grid-cols-2 gap-2 text-sm">
                        <Button outlined className="hc-hit justify-center" onClick={() => store.generateCodeWeaknesses("full")} disabled={!!store.busy} title={de.autoresearch.scanScopeHintFull} prefix={store.busy === codeWeaknessBusyKey("full") ? <Spinner /> : <FlaskConical className="h-4 w-4" />}>
                          {de.autoresearch.scanScopeFull}
                        </Button>
                        <Button outlined className="hc-hit justify-center" onClick={() => store.generateCodeWeaknesses("deep")} disabled={!!store.busy} title={de.autoresearch.deepScanHint} prefix={store.busy === codeWeaknessBusyKey("deep") ? <Spinner /> : <SearchCode className="h-4 w-4" />}>
                          {de.autoresearch.scanScopeDeep}
                        </Button>
                      </div>
                    </details>
                  </div>
                }
              />
              <OperatorActionCard
                icon={<ShieldCheck className="h-5 w-5" />}
                eyebrow="Review"
                hint={actionPlan.applySkills}
                title={openSkillManualReviewCount > 0 ? "Erst Review öffnen" : "Sichere Skills übernehmen"}
                body={openSkillManualReviewCount > 0
                  ? `${openSkillManualReviewCount} Skill-Vorschläge brauchen Einzelreview. Sammelübernahme bleibt gesperrt.`
                  : "Nur batch-sichere Skill-Vorschläge gesammelt übernehmen; Code läuft einzeln durchs Gate."}
                button={openSkillManualReviewCount > 0 ? (
                  <Button outlined className="hc-hit w-full justify-center" onClick={() => scrollTo("autoresearch-queue")} disabled={store.openSkillProposals.length === 0} title="Öffnet die Queue, damit riskante Skill-Vorschläge einzeln geprüft werden." prefix={<ArrowDown className="h-4 w-4" />}>
                    Review öffnen ({store.openSkillProposals.length})
                  </Button>
                ) : (
                  <Button outlined className="hc-hit w-full justify-center" onClick={store.applyAll} disabled={!canApplyAllOpenSkills} title={canApplyAllOpenSkills ? de.autoresearch.applyAllHint : "Keine batch-sicheren Skill-Vorschläge offen."} prefix={<GitPullRequestArrow className="h-4 w-4" />}>
                    {de.autoresearch.applyAll} ({store.openSkillProposals.length})
                  </Button>
                )}
              />
              <OperatorActionCard
                icon={<Archive className="h-5 w-5" />}
                eyebrow="Pflege"
                hint={actionPlan.prune}
                title="Queue aufräumen"
                body="Archiviert Erledigtes und entfernt alte Kandidaten nach Backend-Regeln."
                button={<Button outlined className="hc-hit w-full justify-center" onClick={() => void pruneAutoresearch()} disabled={!!store.busy || pruneBusy} title={de.autoresearch.pruneHint} prefix={pruneBusy ? <Spinner /> : <Archive className="h-4 w-4" />}>{de.autoresearch.prune}</Button>}
              />
            </div>
          </div>
        </div>
      </section>

      {pruneMessage ? <ToneCallout tone={pruneMessage.tone}>{pruneMessage.text}</ToneCallout> : null}

      {store.loading && open.length === 0 ? <ToneCallout tone="violet">Quelle wird geprüft...</ToneCallout> : null}
      {store.error ? <ToneCallout tone="red">{store.error}</ToneCallout> : null}
      {busyNotice ? <ToneCallout tone="violet"><Spinner />{busyNotice}</ToneCallout> : null}
      {latestActivity ? (
        <ToneCallout tone={latestActivity.tone}>
          <span className="font-semibold">Letzte Aktion:</span> <span className="hc-mono hc-dim">{fmtClock(latestActivity.at)}</span> {latestActivity.text}
        </ToneCallout>
      ) : null}

      <section id="autoresearch-queue" className="scroll-mt-6 space-y-3">
        <div className="flex flex-col gap-2 sm:flex-row sm:items-end sm:justify-between">
          <div>
            <p className="hc-eyebrow">Relevanz-Queue</p>
            <h2 className="text-lg font-semibold text-white">Top {relevanceQueue.summary.shown} von {relevanceQueue.summary.total} Vorschlägen</h2>
            <p className="mt-1 text-sm hc-soft">{open.length} offen · <span className="underline decoration-dotted underline-offset-2" title={de.autoresearch.revertedExplain}>{de.autoresearch.revertedCount(reverted.length)}</span></p>
            {open.length > 0 ? (
              <div className="mt-2 flex flex-wrap items-center gap-1.5">
                <span className="text-xs hc-soft">{de.autoresearch.distributionHeading}:</span>
                {distribution.bySeverity.critical > 0 ? <StatusPill tone="red" label={`${de.autoresearch.severityCritical} ${distribution.bySeverity.critical}`} /> : null}
                {distribution.bySeverity.high > 0 ? <StatusPill tone="amber" label={`${de.autoresearch.severityHigh} ${distribution.bySeverity.high}`} /> : null}
                {distribution.bySeverity.medium > 0 ? <StatusPill tone="sky" label={`${de.autoresearch.severityMedium} ${distribution.bySeverity.medium}`} /> : null}
                {distribution.bySeverity.low > 0 ? <StatusPill tone="zinc" label={`${de.autoresearch.severityLow} ${distribution.bySeverity.low}`} /> : null}
              </div>
            ) : null}
          </div>
          <div className="flex flex-col gap-2 sm:items-end">
            <QueueModePicker summary={queueModeSummary} activeMode={queueMode} onChange={setQueueMode} />
            {store.loading ? <Spinner /> : null}
            <div className="flex flex-wrap items-center gap-2">
              <span className="text-sm hc-soft">{de.autoresearch.selectedCount(selectedIds.length)}</span>
              <Button outlined className="hc-hit" onClick={selectQueue} disabled={batchSafeVisibleProposalIds.length === 0 || batchBusy} title={manualReviewVisibleCount > 0 ? "Markiert nur sichtbare Vorschläge ohne Code, Hoch+-Risiko oder Safety-Bezug." : undefined} prefix={<ListChecks className="h-4 w-4" />}>
                {manualReviewVisibleCount > 0 ? `Sichere markieren (${batchSafeVisibleProposalIds.length})` : de.autoresearch.selectAllVisible}
              </Button>
              <Button outlined className="hc-hit" onClick={clearSelection} disabled={selectedIds.length === 0 || batchBusy} prefix={<X className="h-4 w-4" />}>
                {de.autoresearch.clearSelection}
              </Button>
              <Button className="hc-hit" onClick={() => void confirmSelected()} disabled={!canConfirmSelection} title={selectedManualReviewCount > 0 ? "Riskante Auswahl einzeln prüfen oder Auswahl leeren." : undefined} prefix={batchBusy ? <Spinner /> : <CheckCheck className="h-4 w-4" />}>
                {de.autoresearch.batchConfirm}
              </Button>
            </div>
            <QueueActionSummaryPanel summary={queueActionSummary} />
          </div>
        </div>
        {open.length > 0 && filteredOpen.length === 0 ? null : (
          <ReviewFlowPanel
            flow={reviewFlow}
            busy={batchBusy || bulkRevertedBusy || !!store.busy}
            onPrimary={runReviewFlowPrimary}
          />
        )}
        {open.length > 0 && filteredOpen.length === 0 ? null : <DecisionGuidePanel guide={decisionGuide} />}
        {open.length === 0 && !store.loading ? <Empty icon={<FlaskConical className="h-5 w-5" />} text="Keine offenen Vorschläge." /> : null}
        {open.length > 0 && filteredOpen.length === 0 && !store.loading && emptyQueueModeGuidance ? (
          <EmptyQueueModePanel guidance={emptyQueueModeGuidance} onChangeMode={setQueueMode} />
        ) : null}
        <div className="grid gap-4">
          {relevanceQueue.shortlist.map((item) => (
            <ProposalCard
              key={item.proposal.id}
              proposal={item.proposal}
              priorityGroup={item.group}
              density={density}
              busy={store.busy === item.proposal.id}
              selectable
              batchSelectable={!proposalNeedsManualReview(item.proposal)}
              selected={selectedProposalIds.has(item.proposal.id)}
              batchStatus={store.batchConfirmById[item.proposal.id]}
              onSelectedChange={(proposal, selected) => toggleSelection(proposal.id, selected)}
              onApply={store.apply}
              onSkip={store.skip}
            />
          ))}
        </div>
        {relevanceQueue.backlog.length > 0 ? (
          <details className="hc-card p-4">
            <summary className="cursor-pointer text-sm font-medium text-white">Weitere Vorschläge ({relevanceQueue.summary.remaining}) anzeigen</summary>
            <div className="mt-4 grid gap-4">
              {relevanceQueue.backlog.map((item) => (
                <ProposalCard
                  key={item.proposal.id}
                  proposal={item.proposal}
                  priorityGroup={item.group}
                  density={density}
                  busy={store.busy === item.proposal.id}
                  selectable
                  batchSelectable={!proposalNeedsManualReview(item.proposal)}
                  selected={selectedProposalIds.has(item.proposal.id)}
                  batchStatus={store.batchConfirmById[item.proposal.id]}
                  onSelectedChange={(proposal, selected) => toggleSelection(proposal.id, selected)}
                  onApply={store.apply}
                  onSkip={store.skip}
                />
              ))}
            </div>
          </details>
        ) : null}
      </section>

      <section id="autoresearch-loop" className="hc-card scroll-mt-6 p-4 sm:p-5">
        <div className="flex flex-col gap-4 lg:flex-row lg:items-start lg:justify-between">
          <div className="min-w-0 flex-1 space-y-3">
            <div className="flex items-center justify-between gap-3">
              <div><p className="hc-eyebrow">Iterativer Research-Loop</p><h2 className="mt-1 text-lg font-semibold text-white">{loop.running ? `Iteration ${loop.iterationLabel}` : "kein Lauf aktiv"}</h2></div>
              <span className="hc-mono text-xs hc-soft">Heartbeat {loop.heartbeatLabel}</span>
            </div>
            <div className="h-2 overflow-hidden rounded-full bg-white/10"><div className="h-full rounded-full bg-[var(--hc-accent)]" style={{ width: `${loop.progressPercent}%` }} /></div>
            <div className="grid gap-3 text-sm sm:grid-cols-3">
              <Metric label="Letzter Schritt" value={loop.stepLabel} />
              <Metric label="Letzte Bewertung" value={loop.evalLabel} />
              <Metric label="Request" value={status.data?.request_id || "-"} />
            </div>
            {loop.routeHint ? <ToneCallout tone="amber">{loop.routeHint}: {status.data?.route_status ?? "unbekannt"}</ToneCallout> : null}
            <LastRun status={status.data} latestRun={runs.data?.runs?.[0] ?? null} />
            {loopMessage ? <ToneCallout tone={loopMessage.includes("fehlgeschlagen") ? "red" : "emerald"}>{loopMessage}</ToneCallout> : null}
          </div>
          <div className="flex min-w-56 flex-col gap-2 rounded-lg border border-white/10 bg-white/[.03] p-3">
            <RunGuidanceCard guidance={researchLoopGuidance} />
            <LoopPresetPicker
              selectedId={selectedLoopPresetId}
              disabled={loop.running || !!loopBusy}
              onSelect={applyLoopPreset}
            />
            <details className="rounded-lg border border-white/10 bg-black/20 p-2" open={!selectedLoopPresetId}>
              <summary className="cursor-pointer text-xs font-semibold text-white">Feinsteuerung {selectedLoopPresetId ? "" : "· eigene Werte"}</summary>
              <div className="mt-3 flex flex-col gap-2">
                <label className="text-xs hc-soft" htmlFor="loop-area">{de.autoresearch.triggerArea}</label>
                <select id="loop-area" value={area} onChange={(event) => setArea(event.target.value)} className="hc-hit rounded-lg border border-white/10 bg-black/30 px-3 text-sm text-white outline-none focus:border-[var(--hc-accent-border)]">
                  {AUTORESEARCH_AREAS.map((a) => <option key={a.value} value={a.value} className="bg-[#16181d] text-white">{a.value} — {a.scope}</option>)}
                </select>
                <label className="text-xs hc-soft" htmlFor="loop-focus">{de.autoresearch.triggerFocus}</label>
                <input id="loop-focus" type="text" inputMode="text" pattern="[a-z0-9][a-z0-9_-]*" placeholder={de.autoresearch.triggerFocusPlaceholder} value={focus} onChange={(event) => setFocus(event.target.value)} className="hc-hit rounded-lg border border-white/10 bg-black/30 px-3 text-sm text-white outline-none focus:border-[var(--hc-accent-border)]" />
                <p className="-mt-1 text-[11px] hc-dim">{de.autoresearch.triggerFocusHint}</p>
                <label className="text-xs hc-soft" htmlFor="loop-min-use">{de.autoresearch.triggerMinUse}</label>
                <input id="loop-min-use" type="number" min={1} step={1} placeholder={de.autoresearch.triggerMinUsePlaceholder} value={minUseCount} onChange={(event) => setMinUseCount(event.target.value)} className="hc-hit rounded-lg border border-white/10 bg-black/30 px-3 text-sm text-white outline-none focus:border-[var(--hc-accent-border)]" />
                <label className="text-xs hc-soft" htmlFor="loop-iterations">Max. Iterationen</label>
                <input id="loop-iterations" type="number" min={1} max={50} value={maxIterations} onChange={(event) => setMaxIterations(event.target.value)} className="hc-hit rounded-lg border border-white/10 bg-black/30 px-3 text-sm text-white outline-none focus:border-[var(--hc-accent-border)]" />
              </div>
            </details>
            <TargetingPreview summary={researchLoopStartSummary} />
            <StartChecklistPanel checklist={researchLoopStartChecklist} />
            <Button className="hc-hit" onClick={startLoop} disabled={researchLoopStart.disabled} title={researchLoopStart.title} prefix={loopBusy === "start" ? <Spinner /> : <Play className="h-4 w-4" />}>{researchLoopStart.label}</Button>
            <Button outlined className="hc-hit" onClick={stopLoop} disabled={!loop.running || !!loopBusy} prefix={loopBusy === "stop" ? <Spinner /> : <Square className="h-4 w-4" />}>Stop</Button>
          </div>
        </div>
      </section>

      <details id="autoresearch-advanced" className="scroll-mt-6 space-y-4 border-t border-white/10 pt-4" open={effectiveAdvancedOpen} onToggle={(event) => {
        if (!advancedNeedsAttention) setAdvancedOpen(event.currentTarget.open);
      }}>
        <summary className="hc-hit flex cursor-pointer list-none items-center justify-between gap-3 rounded-lg border border-white/10 bg-white/[.03] px-3 py-2 text-left marker:hidden">
          <span className="min-w-0">
            <span className="hc-eyebrow">Erweitert</span>
            <span className="mt-1 block text-sm font-semibold text-white">Modelle, Deep-Audit und Test-Foundry</span>
            <span className="mt-0.5 block text-xs leading-5 hc-soft">Für gezielte Spezialläufe und Modellzuweisung. Der normale Ablauf bleibt oben: Queue prüfen, dann Probelauf starten.</span>
          </span>
          <StatusPill tone={advancedNeedsAttention ? "amber" : "zinc"} label={advancedNeedsAttention ? "Aufmerksamkeit" : "Optional"} />
        </summary>
        <div className="space-y-4">
          <AdvancedGuidePanel items={AUTORESEARCH_ADVANCED_GUIDE} />
          <LaneModelPanel />

          <section id="autoresearch-deep-audit" className="hc-card scroll-mt-6 p-4 sm:p-5">
            <div className="flex flex-col gap-4 lg:flex-row lg:items-start lg:justify-between">
              <div className="min-w-0 flex-1 space-y-3">
                <div>
                  <p className="hc-eyebrow">Deep-Audit</p>
                  <h2 className="mt-1 text-lg font-semibold text-white">Subsystem-Audit</h2>
                  <p className="mt-1 max-w-2xl text-sm hc-soft">Teuer: ca. 1-2 Mio Token pro Lauf. Startet nur per Klick und schreibt keine Code-Änderungen.</p>
                </div>
                <div className="grid gap-3 sm:grid-cols-3">
                  <Metric label="Status" value={deepAudit.status?.state ?? (deepAudit.loading ? "lädt" : "idle")} />
                  <Metric label="Subsystem" value={deepAudit.status?.subsystem ?? (effectiveDeepAuditSubsystem || "-")} />
                  <Metric label="Findings" value={String(deepAudit.findings?.findings.length ?? 0)} />
                </div>
                {deepAudit.error ? <ToneCallout tone="red">{deepAudit.error}</ToneCallout> : null}
                {deepAuditMessage ? <ToneCallout tone={deepAuditMessage.includes("fehlgeschlagen") ? "red" : "emerald"}>{deepAuditMessage}</ToneCallout> : null}
              </div>
              <div className="flex min-w-64 flex-col gap-2 rounded-lg border border-white/10 bg-white/[.03] p-3">
                <RunGuidanceCard guidance={deepAuditGuidance} />
                <label className="text-xs hc-soft" htmlFor="deep-audit-subsystem">Subsystem</label>
                <select id="deep-audit-subsystem" value={effectiveDeepAuditSubsystem} onChange={(event) => setDeepAuditSubsystem(event.target.value)} className="hc-hit rounded-lg border border-white/10 bg-black/30 px-3 text-sm text-white outline-none focus:border-[var(--hc-accent-border)]">
                  {deepAudit.subsystems.map((name) => <option key={name} value={name} className="bg-[#16181d] text-white">{name}</option>)}
                </select>
                <label className="text-xs hc-soft" htmlFor="deep-audit-focus">Focus</label>
                <input id="deep-audit-focus" value={deepAuditFocus} onChange={(event) => setDeepAuditFocus(event.target.value)} placeholder="optional" className="hc-hit rounded-lg border border-white/10 bg-black/30 px-3 text-sm text-white outline-none focus:border-[var(--hc-accent-border)]" />
                <CodeAuditSlotPicker />
                <AdvancedRunChecklistPanel checklist={deepAuditChecklist} />
                <Button className="hc-hit" onClick={() => void startDeepAudit()} disabled={deepAudit.loading || deepAudit.busy || deepAuditRunning || !effectiveDeepAuditSubsystem} prefix={deepAudit.busy || deepAuditRunning ? <Spinner /> : <SearchCode className="h-4 w-4" />}>
                  Deep-Audit starten
                </Button>
              </div>
            </div>
            <DeepAuditFindings findings={deepAudit.findings?.findings ?? []} proposals={deepAudit.findings?.proposals ?? []} />
          </section>

          <section id="autoresearch-test-foundry" className="hc-card scroll-mt-6 p-4 sm:p-5">
            <div className="flex flex-col gap-4 lg:flex-row lg:items-start lg:justify-between">
              <div className="min-w-0 flex-1 space-y-3">
                <div>
                  <p className="hc-eyebrow">Test-Foundry</p>
                  <h2 className="mt-1 text-lg font-semibold text-white">Mutation-Test-Härtung</h2>
                  <p className="mt-1 max-w-2xl text-sm hc-soft">Härtet die Test-Suite via Mutation-Testing; Läufe können einige Minuten dauern.</p>
                </div>
                <div className="grid gap-3 sm:grid-cols-3">
                  <Metric label="Status" value={testFoundry.status?.state ?? (testFoundry.loading ? "lädt" : "idle")} />
                  <Metric label="Target" value={testFoundry.status?.target ?? (effectiveTestFoundryTarget || "-")} />
                  <Metric label="PID" value={testFoundry.status?.pid ? String(testFoundry.status.pid) : "-"} />
                </div>
                <ToneCallout tone={testFoundryApply ? "amber" : "cyan"}>
                  {testFoundryApply
                    ? "Auto-Apply ist an: validierte Tests werden auf dem separaten Branch f-test-foundry committet, nie auf main."
                    : "Auto-Apply ist aus: Test-Foundry erzeugt nur Vorschläge in der Queue."}
                </ToneCallout>
                {testFoundry.error ? <ToneCallout tone="red">{testFoundry.error}</ToneCallout> : null}
                {testFoundryMessage ? <ToneCallout tone={testFoundryMessage.includes("fehlgeschlagen") ? "red" : "emerald"}>{testFoundryMessage}</ToneCallout> : null}
              </div>
              <div className="flex min-w-64 flex-col gap-2 rounded-lg border border-white/10 bg-white/[.03] p-3">
                <RunGuidanceCard guidance={testFoundryGuidance} />
                <label className="text-xs hc-soft" htmlFor="test-foundry-target">Target</label>
                <select id="test-foundry-target" value={effectiveTestFoundryTarget} onChange={(event) => setTestFoundryTarget(event.target.value)} className="hc-hit rounded-lg border border-white/10 bg-black/30 px-3 text-sm text-white outline-none focus:border-[var(--hc-accent-border)]">
                  {testFoundry.targets.map((name) => <option key={name} value={name} className="bg-[#16181d] text-white">{name}</option>)}
                </select>
                <TestHardeningSlotPicker />
                <label className="flex cursor-pointer items-start gap-2 rounded-lg border border-white/10 bg-black/20 p-2 text-sm text-white">
                  <input
                    type="checkbox"
                    checked={testFoundryApply}
                    onChange={(event) => setTestFoundryApply(event.target.checked)}
                    className="mt-0.5 h-4 w-4 accent-[var(--hc-accent)]"
                  />
                  <span>
                    <span className="block font-medium">Auto-Apply</span>
                    <span className="block text-xs hc-soft">Beweis-gegatet auf Branch f-test-foundry; main bleibt unberührt.</span>
                  </span>
                </label>
                <AdvancedRunChecklistPanel checklist={testFoundryChecklist} />
                <Button className="hc-hit" onClick={() => void startTestFoundry()} disabled={testFoundry.loading || testFoundry.busy || testFoundryRunning || !effectiveTestFoundryTarget} prefix={testFoundry.busy || testFoundryRunning ? <Spinner /> : <FlaskConical className="h-4 w-4" />}>
                  Test-Foundry starten
                </Button>
              </div>
            </div>
          </section>
        </div>
      </details>

      {resolvedSummary ? (
        <ResolvedQueueSummaryPanel
          summary={resolvedSummary}
          archiveBusy={bulkRevertedBusy}
          archiveDisabled={!!store.busy || bulkRevertedBusy}
          onArchiveReverted={() => void skipAllReverted()}
        />
      ) : null}

      {reverted.length > 0 ? (
        <details className="space-y-3 border-t border-white/10 pt-4">
          <summary className="cursor-pointer text-lg font-semibold text-white" title={de.autoresearch.revertedExplain}>{de.autoresearch.revertedSummary(reverted.length)}</summary>
          <div className="mt-2">
            <p className="text-sm hc-soft">{de.autoresearch.revertedExplain}</p>
          </div>
          <div className="mt-3 grid gap-3 opacity-85">{reverted.map((proposal) => <ProposalCard key={proposal.id} proposal={proposal} density={density} onApply={store.apply} onSkip={store.skip} />)}</div>
        </details>
      ) : null}

      {applied.length > 0 ? (
        <details className="space-y-3"><summary className="cursor-pointer text-lg font-semibold text-white">Erledigt ({applied.length})</summary><div className="mt-3 grid gap-3">{applied.map((proposal) => <ProposalCard key={proposal.id} proposal={proposal} density={density} onApply={store.apply} onSkip={store.skip} />)}</div></details>
      ) : null}

      {skipped.length > 0 ? (
        <details className="space-y-3"><summary className="cursor-pointer text-lg font-semibold text-white">Übersprungen ({skipped.length})</summary><div className="mt-3 grid gap-3">{skipped.map((proposal) => <ProposalCard key={proposal.id} proposal={proposal} density={density} onApply={store.apply} onSkip={store.skip} />)}</div></details>
      ) : null}

      <RecentRuns runs={runs.data?.runs ?? []} proposals={store.proposals} />

      <section className="hc-card p-4">
        <h2 className="mb-3 text-base font-semibold text-white">{de.autoresearch.activity}</h2>
        {store.activity.length === 0 ? <p className="text-sm hc-soft">Noch keine Aktion in dieser Ansicht.</p> : (
          <div className="space-y-2">
            {store.activity.map((entry) => (
              <ActivityTimelineItem
                key={`${entry.at}-${entry.text}`}
                at={entry.at}
                card={getAutoresearchActivityCard(entry)}
              />
            ))}
          </div>
        )}
      </section>
    </div>
  );
}

function Metric({ label, value }: { label: string; value: string }) {
  return <div className="rounded-lg border border-white/10 bg-white/[.03] px-3 py-2"><p className="text-xs hc-dim">{label}</p><p className="hc-mono truncate text-sm font-semibold text-white">{value}</p></div>;
}

function OperatorActionCard({ icon, eyebrow, hint, title, body, button }: { icon: React.ReactNode; eyebrow: string; hint: AutoresearchActionHint; title: string; body: string; button: React.ReactNode }) {
  return (
    <article className="flex min-h-[188px] flex-col justify-between rounded-lg border border-white/10 bg-white/[.035] p-3">
      <div>
        <div className="mb-3 flex items-center justify-between gap-3">
          <span className="grid h-10 w-10 place-items-center rounded-md border border-[var(--hc-accent-border)] bg-[var(--hc-accent-wash)] text-[var(--hc-accent-text)]">
            {icon}
          </span>
          <span className="flex flex-wrap justify-end gap-1.5">
            <span className="rounded-full border border-white/10 px-2 py-0.5 text-[11px] font-medium hc-soft">{eyebrow}</span>
            <StatusPill tone={hint.tone} label={hint.label} />
          </span>
        </div>
        <h3 className="text-sm font-semibold text-white">{title}</h3>
        <p className="mt-1 text-xs leading-5 hc-soft">{body}</p>
        <div className="mt-3 rounded-md border border-white/10 bg-black/20 px-2 py-1.5 text-xs leading-5 hc-soft">
          <p><span className="font-semibold text-white">Warum:</span> {hint.reason}</p>
          <p className="mt-1"><span className="font-semibold text-white">Danach:</span> {hint.after}</p>
        </div>
      </div>
      <div className="mt-3">{button}</div>
    </article>
  );
}

function ReadinessPanel({ summary }: { summary: AutoresearchReadinessSummary }) {
  return (
    <section className={cn("rounded-lg border p-3", reviewStepToneClass(summary.tone))} aria-label="Autoresearch Betriebsstatus">
      <div className="flex flex-col gap-3 lg:flex-row lg:items-start lg:justify-between">
        <div className="min-w-0">
          <div className="flex flex-wrap items-center gap-2">
            <p className="hc-eyebrow">Betriebsstatus</p>
            <StatusPill tone={summary.tone} label={summary.label} />
          </div>
          <h2 className="mt-2 text-base font-semibold text-white">{summary.title}</h2>
          <p className="mt-1 max-w-3xl text-sm leading-6 hc-soft">{summary.detail}</p>
          <p className="mt-2 text-sm text-white"><span className="font-semibold">Jetzt sinnvoll:</span> {summary.next}</p>
        </div>
        <div className="grid shrink-0 grid-cols-2 gap-1.5 sm:grid-cols-5 lg:min-w-[420px]">
          {summary.facts.map((fact) => (
            <div key={fact.label} className={cn("rounded-md border px-2 py-1.5", reviewStepToneClass(fact.tone))}>
              <p className="text-[9px] font-semibold uppercase tracking-[.12em] hc-dim">{fact.label}</p>
              <p className="mt-1 truncate text-sm font-semibold text-white">{fact.value}</p>
            </div>
          ))}
        </div>
      </div>
    </section>
  );
}

function ResolvedQueueSummaryPanel({ summary, archiveBusy, archiveDisabled, onArchiveReverted }: { summary: AutoresearchResolvedSummary; archiveBusy: boolean; archiveDisabled: boolean; onArchiveReverted: () => void }) {
  return (
    <section className={cn("rounded-lg border p-3", reviewStepToneClass(summary.tone))} aria-label="Autoresearch Abschluss und Aufräumen">
      <div className="flex flex-col gap-3 lg:flex-row lg:items-start lg:justify-between">
        <div className="min-w-0">
          <div className="flex flex-wrap items-center gap-2">
            <p className="hc-eyebrow">Abschluss & Aufräumen</p>
            <StatusPill tone={summary.tone} label={summary.label} />
          </div>
          <h2 className="mt-2 text-base font-semibold text-white">{summary.title}</h2>
          <p className="mt-1 max-w-3xl text-sm leading-6 hc-soft">{summary.detail}</p>
          <p className="mt-2 text-sm text-white"><span className="font-semibold">Jetzt sinnvoll:</span> {summary.next}</p>
        </div>
        <div className="grid shrink-0 gap-2 sm:grid-cols-3 lg:min-w-[360px]">
          {summary.facts.map((fact) => (
            <div key={fact.label} className={cn("rounded-md border px-3 py-2", reviewStepToneClass(fact.tone))}>
              <p className="text-[10px] font-semibold uppercase tracking-[.14em] hc-dim">{fact.label}</p>
              <p className="mt-1 text-sm font-semibold text-white">{fact.value}</p>
            </div>
          ))}
          {summary.archiveLabel ? (
            <Button outlined className="hc-hit sm:col-span-3" onClick={onArchiveReverted} disabled={archiveDisabled} prefix={archiveBusy ? <Spinner /> : <Archive className="h-4 w-4" />}>
              {summary.archiveLabel}
            </Button>
          ) : null}
        </div>
      </div>
    </section>
  );
}

function CockpitSectionNav({ items, onJump }: { items: readonly AutoresearchSectionNavItem[]; onJump: (id: string) => void }) {
  const iconFor = (kind: AutoresearchSectionNavItem["kind"]) => {
    switch (kind) {
      case "review":
        return <ClipboardCheck className="h-4 w-4" />;
      case "run":
        return <Radar className="h-4 w-4" />;
      case "history":
        return <ListChecks className="h-4 w-4" />;
      case "advanced":
        return <Settings2 className="h-4 w-4" />;
    }
  };

  return (
    <nav aria-label="Autoresearch Bereiche" className="grid gap-2 sm:grid-cols-2 xl:grid-cols-4">
      {items.map((item) => (
        <button
          key={item.id}
          type="button"
          onClick={() => onJump(item.id)}
          className="hc-hit rounded-lg border border-white/10 bg-white/[.03] px-3 py-2 text-left transition hover:border-[var(--hc-accent-border)] hover:bg-[var(--hc-accent-wash)]"
        >
          <span className="flex items-center gap-2 text-sm font-semibold text-white">
            {iconFor(item.kind)}
            {item.label}
          </span>
          <span className="mt-0.5 block text-xs leading-5 hc-soft">{item.detail}</span>
        </button>
      ))}
    </nav>
  );
}

function AdvancedGuidePanel({ items }: { items: readonly AutoresearchAdvancedGuideItem[] }) {
  const iconFor = (kind: AutoresearchAdvancedGuideItem["kind"]) => {
    switch (kind) {
      case "models":
        return <Settings2 className="h-4 w-4" />;
      case "deep-audit":
        return <SearchCode className="h-4 w-4" />;
      case "test-foundry":
        return <FlaskConical className="h-4 w-4" />;
    }
  };

  return (
    <section className="rounded-lg border border-white/10 bg-white/[.025] p-3">
      <div className="mb-3 flex flex-wrap items-center justify-between gap-2">
        <div>
          <p className="hc-eyebrow">Vor Erweitert</p>
          <h2 className="text-base font-semibold text-white">Nur öffnen, wenn der normale Probelauf nicht reicht.</h2>
        </div>
        <StatusPill tone="zinc" label="Optional" />
      </div>
      <div className="grid gap-3 lg:grid-cols-3">
        {items.map((item) => (
          <article key={item.kind} className={cn("rounded-lg border p-3", reviewStepToneClass(item.tone))}>
            <div className="flex items-start justify-between gap-3">
              <span className="grid h-8 w-8 shrink-0 place-items-center rounded-md border border-white/10 bg-black/20 text-white">
                {iconFor(item.kind)}
              </span>
              <StatusPill tone={item.tone} label={item.label} />
            </div>
            <h3 className="mt-3 text-sm font-semibold text-white">{item.title}</h3>
            <div className="mt-2 space-y-1 text-xs leading-5 hc-soft">
              <p><span className="font-semibold text-white">Wann:</span> {item.when}</p>
              <p><span className="font-semibold text-white">Aufwand:</span> {item.cost}</p>
              <p><span className="font-semibold text-white">Sicherheit:</span> {item.safety}</p>
            </div>
          </article>
        ))}
      </div>
    </section>
  );
}

function ActivityTimelineItem({ at, card }: { at: number; card: AutoresearchActivityCard }) {
  return (
    <article className={cn("rounded-lg border px-3 py-2", reviewStepToneClass(card.tone))}>
      <div className="flex flex-col gap-2 sm:flex-row sm:items-start sm:justify-between">
        <div className="min-w-0">
          <div className="flex flex-wrap items-center gap-2">
            <span className="hc-mono text-xs hc-dim">{fmtClock(at)}</span>
            <StatusPill tone={card.tone} label={card.label} />
          </div>
          <h3 className="mt-1 text-sm font-semibold text-white">{card.title}</h3>
          <p className="mt-1 text-sm leading-6 hc-soft">{card.detail}</p>
        </div>
        <p className="max-w-sm text-xs leading-5 hc-dim"><span className="font-semibold text-white/80">Danach:</span> {card.next}</p>
      </div>
    </article>
  );
}

function ReviewFlowPanel({ flow, busy, onPrimary }: { flow: AutoresearchReviewFlow; busy: boolean; onPrimary: () => void }) {
  const icon = flow.primaryAction === "confirm-selection"
    ? <CheckCheck className="h-4 w-4" />
    : flow.primaryAction === "select-visible"
      ? <ListChecks className="h-4 w-4" />
      : flow.primaryAction === "clear-selection"
        ? <X className="h-4 w-4" />
        : flow.primaryAction === "archive-reverted"
          ? <Archive className="h-4 w-4" />
          : flow.primaryAction === "generate"
            ? <Sparkles className="h-4 w-4" />
            : <ClipboardCheck className="h-4 w-4" />;

  return (
    <div className="rounded-lg border border-[var(--hc-accent-border)] bg-[var(--hc-accent-wash)] p-3">
      <div className="flex flex-col gap-3 lg:flex-row lg:items-center lg:justify-between">
        <div className="min-w-0 flex-1">
          <div className="flex flex-wrap items-center gap-2">
            <p className="hc-eyebrow text-[var(--hc-accent-text)]">Review-Flow</p>
            <StatusPill tone={flow.tone} label={flow.progressLabel} />
          </div>
          <h3 className="mt-2 text-base font-semibold text-white">{flow.title}</h3>
          <p className="mt-1 max-w-3xl text-sm leading-6 hc-soft">{flow.detail}</p>
          <div className="mt-3 h-2 overflow-hidden rounded-full bg-black/30">
            <div className="h-full rounded-full bg-[var(--hc-accent)]" style={{ width: `${flow.progressPercent}%` }} />
          </div>
        </div>
        <div className="grid shrink-0 gap-2 sm:grid-cols-3 lg:min-w-[360px]">
          {flow.steps.map((step) => (
            <div key={step.label} className={cn("rounded-md border px-3 py-2", reviewStepToneClass(step.tone))}>
              <p className="text-[10px] font-semibold uppercase tracking-[.14em] hc-dim">{step.label}</p>
              <p className="mt-1 text-sm font-semibold text-white">{step.value}</p>
            </div>
          ))}
          <Button className="hc-hit sm:col-span-3" onClick={onPrimary} disabled={busy} prefix={busy ? <Spinner /> : icon}>
            {flow.primaryLabel}
          </Button>
        </div>
      </div>
    </div>
  );
}

function QueueActionSummaryPanel({ summary }: { summary: AutoresearchQueueActionSummary }) {
  return (
    <div className={cn("max-w-xl rounded-lg border p-3 text-left", reviewStepToneClass(summary.tone))}>
      <div className="flex flex-col gap-3 sm:flex-row sm:items-start sm:justify-between">
        <div className="min-w-0">
          <p className="hc-eyebrow">Auswahlwirkung</p>
          <h3 className="mt-1 text-sm font-semibold text-white">{summary.title}</h3>
          <div className="mt-2 space-y-1 text-xs leading-5 hc-soft">
            <p>{summary.batchLine}</p>
            <p>{summary.manualLine}</p>
            <p className="text-white/90">{summary.confirmLine}</p>
          </div>
        </div>
        <div className="grid shrink-0 grid-cols-3 gap-1.5 sm:min-w-64">
          {summary.facts.map((fact) => (
            <div key={fact.label} className={cn("rounded-md border px-2 py-1.5", reviewStepToneClass(fact.tone))}>
              <p className="text-[9px] font-semibold uppercase tracking-[.12em] hc-dim">{fact.label}</p>
              <p className="mt-1 text-sm font-semibold text-white">{fact.value}</p>
            </div>
          ))}
        </div>
      </div>
    </div>
  );
}

function QueueModePicker({ summary, activeMode, onChange }: { summary: ReturnType<typeof getAutoresearchQueueModeSummary>; activeMode: AutoresearchQueueMode; onChange: (mode: AutoresearchQueueMode) => void }) {
  return (
    <div className="max-w-3xl rounded-lg border border-white/10 bg-white/[.025] p-2 text-left">
      <div className="grid gap-1.5 sm:grid-cols-4">
        {summary.options.map((option) => {
          const active = option.id === activeMode;
          return (
            <button
              key={option.id}
              type="button"
              onClick={() => onChange(option.id)}
              aria-pressed={active}
              className={cn(
                "hc-hit rounded-md border px-2 py-1.5 text-left transition",
                active ? "border-[var(--hc-accent-border)] bg-[var(--hc-accent-wash)]" : "border-white/10 bg-black/20 hover:bg-white/[.04]",
              )}
            >
              <span className="flex items-center justify-between gap-2">
                <span className="text-xs font-semibold text-white">{option.label}</span>
                <StatusPill tone={option.tone} label={String(option.count)} />
              </span>
            </button>
          );
        })}
      </div>
      <p className="mt-2 text-xs leading-5 hc-soft"><span className="font-semibold text-white">{summary.active.label}:</span> {summary.active.detail}</p>
    </div>
  );
}

function EmptyQueueModePanel({ guidance, onChangeMode }: { guidance: AutoresearchEmptyQueueModeGuidance; onChangeMode: (mode: AutoresearchQueueMode) => void }) {
  return (
    <div className={cn("rounded-lg border p-3", reviewStepToneClass(guidance.tone))}>
      <div className="flex flex-col gap-3 lg:flex-row lg:items-center lg:justify-between">
        <div className="min-w-0">
          <div className="flex flex-wrap items-center gap-2">
            <p className="hc-eyebrow">Filter leer</p>
            <StatusPill tone={guidance.tone} label={guidance.label} />
          </div>
          <h3 className="mt-2 text-base font-semibold text-white">{guidance.title}</h3>
          <p className="mt-1 max-w-3xl text-sm leading-6 hc-soft">{guidance.detail}</p>
        </div>
        <Button outlined className="hc-hit shrink-0 justify-center" onClick={() => onChangeMode(guidance.primaryMode)} prefix={<ListChecks className="h-4 w-4" />}>
          {guidance.primaryLabel}
        </Button>
      </div>
      <div className="mt-3 grid gap-2 sm:grid-cols-4">
        {guidance.facts.map((fact) => (
          <div key={fact.label} className={cn("rounded-md border px-2.5 py-2", reviewStepToneClass(fact.tone))}>
            <p className="text-[10px] font-semibold uppercase tracking-[.12em] hc-dim">{fact.label}</p>
            <p className="mt-1 text-sm font-semibold text-white">{fact.value}</p>
          </div>
        ))}
      </div>
    </div>
  );
}

function DecisionGuidePanel({ guide }: { guide: AutoresearchDecisionGuide }) {
  const icon = guide.tone === "emerald"
    ? <ShieldCheck className="h-4 w-4" />
    : guide.tone === "cyan"
      ? <ClipboardCheck className="h-4 w-4" />
      : guide.tone === "amber"
        ? <Target className="h-4 w-4" />
        : <X className="h-4 w-4" />;

  return (
    <div className={cn("rounded-lg border p-3", reviewStepToneClass(guide.tone))}>
      <div className="flex flex-col gap-3 lg:flex-row lg:items-center lg:justify-between">
        <div className="min-w-0">
          <div className="flex flex-wrap items-center gap-2">
            <span className="grid h-8 w-8 place-items-center rounded-md border border-white/10 bg-black/20 text-white">
              {icon}
            </span>
            <p className="hc-eyebrow">Heute tun</p>
            <StatusPill tone={guide.tone} label={guide.primaryLabel} />
          </div>
          <h3 className="mt-2 text-base font-semibold text-white">{guide.headline}</h3>
          <p className="mt-1 max-w-3xl text-sm leading-6 hc-soft">{guide.summary}</p>
          <p className="mt-2 text-sm text-white"><span className="font-semibold">Nächster sicherer Schritt:</span> {guide.next}</p>
        </div>
        <div className="grid shrink-0 gap-2 sm:grid-cols-3 lg:min-w-[360px]">
          {guide.facts.map((fact) => (
            <div key={fact.label} className={cn("rounded-md border px-3 py-2", reviewStepToneClass(fact.tone))}>
              <p className="text-[10px] font-semibold uppercase tracking-[.14em] hc-dim">{fact.label}</p>
              <p className="mt-1 text-sm font-semibold text-white">{fact.value}</p>
            </div>
          ))}
        </div>
      </div>
    </div>
  );
}

function RunGuidanceCard({ guidance }: { guidance: AutoresearchRunGuidance }) {
  return (
    <div className="rounded-lg border border-white/10 bg-black/20 p-3">
      <div className="mb-2 flex items-center justify-between gap-2">
        <p className="hc-eyebrow">Vor dem Start</p>
        <StatusPill tone={guidance.tone} label={guidance.label} />
      </div>
      <div className="grid gap-2 text-xs leading-5 hc-soft">
        <p><span className="font-semibold text-white">Wofür:</span> {guidance.outcome}</p>
        <p><span className="font-semibold text-white">Kosten:</span> {guidance.cost}</p>
        <p><span className="font-semibold text-white">Sicherheit:</span> {guidance.safety}</p>
      </div>
    </div>
  );
}

function LoopPresetPicker({ selectedId, disabled, onSelect }: { selectedId: ResearchLoopPresetId | null; disabled: boolean; onSelect: (presetId: ResearchLoopPresetId) => void }) {
  return (
    <div className="space-y-2">
      <div className="flex items-center justify-between gap-2">
        <div>
          <p className="hc-eyebrow">Start-Preset</p>
          <p className="mt-0.5 text-xs hc-soft">Wähle zuerst den Zweck, nicht die Technik.</p>
        </div>
        <StatusPill tone={selectedId ? "emerald" : "amber"} label={selectedId ? "Preset aktiv" : "Eigene Werte"} />
      </div>
      <div className="grid gap-2">
        {RESEARCH_LOOP_PRESETS.map((preset) => {
          const selected = preset.id === selectedId;
          return (
            <button
              key={preset.id}
              type="button"
              onClick={() => onSelect(preset.id)}
              disabled={disabled}
              title={preset.title}
              aria-label={`${preset.label}: ${preset.summary} ${preset.cost}.`}
              aria-pressed={selected}
              className={cn(
                "hc-hit min-h-[118px] rounded-lg border px-3 py-2.5 text-left transition disabled:cursor-not-allowed disabled:opacity-60",
                selected ? "border-[var(--hc-accent-border)] bg-[var(--hc-accent-wash)]" : "border-white/10 bg-black/20 hover:bg-white/[.04]",
              )}
            >
              <span className="flex items-start justify-between gap-3">
                <span className="min-w-0">
                  <span className="block text-sm font-semibold text-white">{preset.label}</span>
                  <span className="mt-0.5 block text-xs font-medium text-white/85">{preset.operatorTitle}</span>
                </span>
                <span className={cn("shrink-0 rounded-full border px-2 py-0.5 text-[11px] font-medium", selected ? "border-[var(--hc-accent-border)] text-[var(--hc-accent-text)]" : "border-white/10 hc-soft")}>{preset.badge}</span>
              </span>
              <span className="mt-2 block text-xs leading-5 hc-soft">{preset.operatorFit}</span>
              <span className="mt-1 block text-xs leading-5 hc-dim">{preset.operatorResult}</span>
              <span className="mt-2 block hc-mono text-[11px] hc-dim">{preset.cost}</span>
            </button>
          );
        })}
      </div>
    </div>
  );
}

function reviewStepToneClass(tone: ToneName): string {
  switch (tone) {
    case "emerald":
      return "border-emerald-500/20 bg-emerald-500/10";
    case "cyan":
      return "border-cyan-500/20 bg-cyan-500/10";
    case "amber":
      return "border-amber-500/20 bg-amber-500/10";
    case "violet":
      return "border-[var(--hc-accent-border)] bg-[var(--hc-accent-wash)]";
    case "red":
      return "border-red-500/20 bg-red-500/10";
    default:
      return "border-white/10 bg-black/20";
  }
}

function LaneModelPanel() {
  const [aux, setAux] = useState<AuxiliaryModelsResponse | null>(null);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);
  const [pickerTask, setPickerTask] = useState<LaneModelSlot | null>(null);
  const [savingTask, setSavingTask] = useState<LaneModelSlot | null>(null);
  const [refreshKey, setRefreshKey] = useState(0);

  const loadAux = async () => {
    setLoading(true);
    setError(null);
    try {
      setAux(await fetchJSON<AuxiliaryModelsResponse>("/api/model/auxiliary"));
    } catch (e) {
      setError(e instanceof Error ? e.message : String(e));
    } finally {
      setLoading(false);
    }
  };

  useEffect(() => {
    queueMicrotask(() => void loadAux());
  }, []);

  const assignmentFor = (task: LaneModelSlot) => aux?.tasks.find((item) => item.task === task) ?? null;
  const pickerSlot = pickerTask ? LANE_MODEL_SLOTS.find((slot) => slot.task === pickerTask) ?? null : null;

  const loadOptionsForPicker = async (): Promise<ModelOptionsResponse> => {
    const options = await fetchJSON<ModelOptionsResponse>("/api/model/options");
    const current = pickerTask ? assignmentFor(pickerTask) : null;
    if (!current?.provider || current.provider === "auto") return options;
    return {
      ...options,
      provider: current.provider,
      model: current.model,
      providers: options.providers?.map((provider) => ({ ...provider, is_current: provider.slug === current.provider })),
    };
  };

  return (
    <section className="hc-card p-4 sm:p-5">
      <div className="mb-3 flex flex-col gap-2 sm:flex-row sm:items-start sm:justify-between">
        <div>
          <p className="hc-eyebrow">{de.autoresearch.laneModelsEyebrow}</p>
          <h2 className="text-base font-semibold text-white">{de.autoresearch.laneModelsHeading}</h2>
        </div>
        {loading ? <Spinner /> : null}
      </div>
      {error ? <ToneCallout tone="red">{de.autoresearch.laneModelsFailed}: {error}</ToneCallout> : null}
      <div className="grid gap-3 md:grid-cols-3">
        {LANE_MODEL_SLOTS.map((slot) => {
          const assignment = assignmentFor(slot.task);
          const isAuto = !assignment?.provider || assignment.provider === "auto";
          const value = isAuto ? de.autoresearch.laneModelAuto : `${assignment?.provider}${assignment?.model ? ` · ${assignment.model}` : ""}`;
          return (
            <div key={slot.task} className="rounded-lg border border-white/10 bg-white/[.03] p-3">
              <div className="mb-2 flex items-start justify-between gap-3">
                <div className="min-w-0">
                  <p className="flex items-center gap-1.5 text-sm font-semibold text-white"><Settings2 className="h-3.5 w-3.5" />{slot.lane}</p>
                  <p className="mt-1 text-xs hc-dim">{slot.hint}</p>
                </div>
                <StatusPill tone={isAuto ? "zinc" : "cyan"} label={isAuto ? "Auto" : slot.label} />
              </div>
              <p className="hc-mono min-h-5 truncate text-xs hc-soft" title={value}>{value}</p>
              <Button outlined className="hc-hit mt-3 w-full" onClick={() => setPickerTask(slot.task)} disabled={loading || !!savingTask} prefix={savingTask === slot.task ? <Spinner /> : <Settings2 className="h-4 w-4" />}>
                {de.autoresearch.laneModelChange}
              </Button>
            </div>
          );
        })}
      </div>
      {pickerTask && pickerSlot ? (
        <ModelPickerDialog
          key={`${pickerTask}-${refreshKey}`}
          loader={loadOptionsForPicker}
          alwaysGlobal
          title={de.autoresearch.laneModelPickerTitle(pickerSlot.lane)}
          onApply={async ({ provider, model }) => {
            setSavingTask(pickerTask);
            try {
              await fetchJSON<unknown>("/api/model/set", {
                method: "POST",
                headers: { "Content-Type": "application/json" },
                body: JSON.stringify({ scope: "auxiliary", task: pickerTask, provider, model }),
              });
              await loadAux();
              setRefreshKey((value) => value + 1);
            } finally {
              setSavingTask(null);
            }
          }}
          onClose={() => setPickerTask(null)}
        />
      ) : null}
    </section>
  );
}

function TargetingPreview({ summary }: { summary: ResearchLoopStartSummary }) {
  return (
    <div className="rounded-lg border border-[var(--hc-accent-border)] bg-[var(--hc-accent-wash)] px-3 py-2 text-xs text-[var(--hc-accent-text)]">
      <div className="flex items-start gap-2">
        <Target className="mt-0.5 h-3.5 w-3.5 shrink-0" />
        <div className="min-w-0">
          <p className="font-semibold">{summary.title}</p>
          <p className="mt-1.5 leading-5">{summary.scope}</p>
          <p className="mt-0.5 leading-5">{summary.detail}</p>
          <p className="mt-1.5 leading-5"><span className="font-semibold">Aufwand:</span> {summary.cost}</p>
          <p className="mt-0.5 leading-5"><span className="font-semibold">Sicherheit:</span> {summary.safety}</p>
          <p className="mt-1 hc-mono text-[11px] opacity-75">{summary.technicalLabel}</p>
        </div>
      </div>
    </div>
  );
}

function StartChecklistPanel({ checklist }: { checklist: ResearchLoopStartChecklist }) {
  return (
    <div className={cn("rounded-lg border p-3", reviewStepToneClass(checklist.tone))}>
      <div className="mb-2 flex items-start justify-between gap-3">
        <div className="min-w-0">
          <p className="hc-eyebrow">Start-Check</p>
          <h3 className="mt-1 text-sm font-semibold text-white">{checklist.title}</h3>
          <p className="mt-1 text-xs leading-5 hc-soft">{checklist.detail}</p>
        </div>
        <StatusPill tone={checklist.tone} label={checklist.label} />
      </div>
      <div className="grid gap-2">
        {checklist.items.map((item) => (
          <div key={item.label} className="rounded-md border border-white/10 bg-black/20 px-2.5 py-2">
            <div className="flex items-center justify-between gap-2">
              <p className="text-[10px] font-semibold uppercase tracking-[.12em] hc-dim">{item.label}</p>
              <StatusPill tone={item.tone} label={item.value} />
            </div>
            <p className="mt-1 text-xs leading-5 hc-soft">{item.detail}</p>
          </div>
        ))}
      </div>
    </div>
  );
}

function AdvancedRunChecklistPanel({ checklist }: { checklist: AutoresearchAdvancedRunChecklist }) {
  return (
    <div className={cn("rounded-lg border p-2.5", reviewStepToneClass(checklist.tone))}>
      <div className="mb-2 flex items-start justify-between gap-2">
        <div className="min-w-0">
          <p className="hc-eyebrow">Start-Check</p>
          <h3 className="mt-1 text-sm font-semibold text-white">{checklist.title}</h3>
          <p className="mt-1 text-xs leading-5 hc-soft">{checklist.detail}</p>
        </div>
        <StatusPill tone={checklist.tone} label={checklist.label} />
      </div>
      <div className="grid gap-1.5">
        {checklist.items.map((item) => (
          <div key={item.label} className="rounded-md border border-white/10 bg-black/20 px-2 py-1.5">
            <div className="flex items-center justify-between gap-2">
              <p className="text-[9px] font-semibold uppercase tracking-[.12em] hc-dim">{item.label}</p>
              <StatusPill tone={item.tone} label={item.value} />
            </div>
            <p className="mt-1 text-xs leading-5 hc-soft">{item.detail}</p>
          </div>
        ))}
      </div>
    </div>
  );
}

function LastRun({ status, latestRun }: { status: ReturnType<typeof useAutoresearchStatus>["data"]; latestRun: AutoresearchRun | null }) {
  const receipt = status?.last_receipt;
  const note = status?.note;
  const lastRun = status?.last_run;
  const objectRun = lastRun && typeof lastRun === "object" ? lastRun as Record<string, unknown> : null;
  const proposed = typeof objectRun?.proposed === "number" ? objectRun.proposed : null;
  const kept = typeof objectRun?.kept === "number" ? objectRun.kept : null;
  const reverted = typeof objectRun?.reverted === "number" ? objectRun.reverted : null;
  const stopped = objectRun?.stopped === true ? "Signal erhalten" : null;
  const brief = getAutoresearchLastRunBrief({ lastRun, latestRun, receipt, note });
  const rawPayload = formatLastRunRawPayload(lastRun);
  // f-autoresearch-tab-driver: surface the observability counters so "0 proposed"
  // reads as converged-healthy vs broken, plus the real MiniMax token spend.
  const counters = readLastRunCounters(lastRun);
  const showCounters = hasResearchCounters(counters);
  const showErrorBadge = shouldShowResearchErrorBadge(counters.researchErrors);

  return (
    <div className={cn("rounded-lg border p-3", reviewStepToneClass(brief.tone))}>
      <div className="flex flex-col gap-3 lg:flex-row lg:items-start lg:justify-between">
        <div className="min-w-0">
          <div className="flex flex-wrap items-center gap-2">
            <p className="hc-eyebrow">Letzter Lauf</p>
            <StatusPill tone={brief.tone} label={brief.label} />
          </div>
          <h3 className="mt-2 text-sm font-semibold text-white">{brief.title}</h3>
          <p className="mt-1 text-sm leading-6 hc-soft">{brief.detail}</p>
          <p className="mt-2 text-sm text-white"><span className="font-semibold">Nächster Schritt:</span> {brief.next}</p>
        </div>
        <div className="grid shrink-0 grid-cols-2 gap-1.5 sm:grid-cols-4 lg:min-w-[360px]">
          {brief.facts.map((fact) => (
            <div key={fact.label} className={cn("rounded-md border px-2 py-1.5", reviewStepToneClass(fact.tone))}>
              <p className="text-[9px] font-semibold uppercase tracking-[.12em] hc-dim">{fact.label}</p>
              <p className="mt-1 text-sm font-semibold text-white">{fact.value}</p>
            </div>
          ))}
        </div>
      </div>
      {brief.rawLine || rawPayload || proposed !== null || kept !== null || reverted !== null || showCounters || stopped || receipt || note ? (
        <details className="mt-3 rounded-md border border-white/10 bg-black/20 px-3 py-2 text-xs hc-soft">
          <summary className="cursor-pointer font-semibold text-white">Technische Details</summary>
          {brief.rawLine ? <p className="mt-2"><span className="text-white">Rohstatus:</span> {brief.rawLine}</p> : null}
          {rawPayload ? <pre className="mt-2 max-h-40 overflow-auto rounded-md border border-white/10 bg-black/30 p-2 hc-mono text-[11px] leading-5 text-white/80">{rawPayload}</pre> : null}
          {proposed !== null || kept !== null || reverted !== null ? <p className="mt-1 hc-mono">proposed={proposed ?? "?"} · übernommen={kept ?? "?"} · zurückgerollt={reverted ?? "?"}</p> : null}
          {showCounters ? (
            <p className="mt-1 flex flex-wrap items-center gap-x-2 gap-y-1 hc-mono">
              <span>{de.autoresearch.skillsResearched}={counters.skillsResearched ?? "?"} · {de.autoresearch.researchErrors}={counters.researchErrors ?? "?"} · {de.autoresearch.skillsWithFindings}={counters.skillsWithFindings ?? "?"}</span>
              {showErrorBadge ? <span className="rounded-full border border-red-500/40 bg-red-500/15 px-2 py-0.5 text-xs text-red-200">{de.autoresearch.researchErrorBadge}</span> : null}
            </p>
          ) : null}
          {showCounters ? <p className="mt-1 hc-mono">{de.autoresearch.researchTokens}: {formatResearchTokens(counters.researchTokens)}</p> : null}
          {showCounters ? <p className="mt-1 text-xs hc-dim">{de.autoresearch.counterLegend}</p> : null}
          {stopped ? <p className="mt-1">Stop: {stopped}</p> : null}
          {receipt ? <p className="mt-1 truncate text-xs hc-dim" title={receipt}>Receipt: {receipt}</p> : null}
          {note ? <p className="mt-1">{note}</p> : null}
        </details>
      ) : null}
    </div>
  );
}

function formatLastRunRawPayload(value: unknown): string | null {
  if (value === null || value === undefined) return null;
  if (typeof value === "string" || typeof value === "number" || typeof value === "boolean") return String(value);

  try {
    return JSON.stringify(value, null, 2);
  } catch {
    return null;
  }
}

function Empty({ icon, text }: { icon: React.ReactNode; text: string }) {
  return <div className="hc-card flex items-center gap-3 p-4 text-sm hc-soft">{icon}<span>{text}</span></div>;
}

export function DeepAuditFindings({ findings, proposals }: { findings: DeepAuditFinding[]; proposals: string[] }) {
  if (findings.length === 0) {
    return (
      <div className="mt-4 rounded-lg border border-white/10 bg-black/20 px-3 py-2 text-sm hc-soft">
        <div className="flex flex-wrap items-center gap-2">
          <span>Noch keine Deep-Audit-Findings.</span>
          {proposals.length > 0 ? <StatusPill tone="amber" label={`${proposals.length} in Queue`} /> : null}
        </div>
        <p className="mt-1">
          {proposals.length > 0
            ? "Der Lauf hat bereits Queue-Karten gemeldet; die Detail-Findings sind in dieser Antwort nicht enthalten."
            : "Wenn der Lauf fertig ist, erscheinen hier die prüfbaren Risiken und die daraus erzeugten Queue-Karten."}
        </p>
      </div>
    );
  }
  const severityCounts = findings.reduce<Record<DeepAuditFinding["severity"], number>>((counts, finding) => {
    counts[finding.severity] += 1;
    return counts;
  }, { critical: 0, high: 0, medium: 0, low: 0 });
  const topSeverity = (["critical", "high", "medium", "low"] as const).find((severity) => severityCounts[severity] > 0) ?? "low";
  const topFinding = findings.find((finding) => finding.severity === topSeverity) ?? findings[0];

  return (
    <div className="mt-4 space-y-3">
      <section className={cn("rounded-lg border p-3", reviewStepToneClass(severityTone(topSeverity)))}>
        <div className="flex flex-col gap-3 lg:flex-row lg:items-start lg:justify-between">
          <div className="min-w-0">
            <div className="flex flex-wrap items-center gap-2">
              <p className="hc-eyebrow">Audit-Ergebnis</p>
              <StatusPill tone={severityTone(topSeverity)} label={deepAuditSeverityLabel(topSeverity)} />
              {proposals.length > 0 ? <StatusPill tone="amber" label={`${proposals.length} in Queue`} /> : null}
            </div>
            <h3 className="mt-2 text-base font-semibold text-white">
              {findings.length === 1 ? "1 prüfbares Risiko gefunden." : `${findings.length} prüfbare Risiken gefunden.`}
            </h3>
            <p className="mt-1 max-w-3xl text-sm leading-6 hc-soft">
              Wichtigster Punkt: {topFinding.title}. Der Deep-Audit schreibt keinen Code; daraus entstehen nur Review-Karten.
            </p>
            <p className="mt-2 text-sm text-white"><span className="font-semibold">Jetzt sinnvoll:</span> Queue-Karten prüfen, dann nur belegte Fixes übernehmen.</p>
          </div>
          <div className="grid shrink-0 grid-cols-2 gap-1.5 sm:grid-cols-4 lg:min-w-[360px]">
            {(["critical", "high", "medium", "low"] as const).map((severity) => (
              <div key={severity} className={cn("rounded-md border px-2 py-1.5", reviewStepToneClass(severityTone(severity)))}>
                <p className="text-[9px] font-semibold uppercase tracking-[.12em] hc-dim">{deepAuditSeverityLabel(severity)}</p>
                <p className="mt-1 text-sm font-semibold text-white">{severityCounts[severity]}</p>
              </div>
            ))}
          </div>
        </div>
      </section>
      <div className="grid gap-3">
        {findings.map((finding, index) => (
          <article key={`${finding.fileline}-${index}`} className={cn("rounded-lg border p-3", reviewStepToneClass(severityTone(finding.severity)))}>
            <div className="flex flex-col gap-3 lg:flex-row lg:items-start lg:justify-between">
              <div className="min-w-0">
                <div className="flex flex-wrap items-center gap-2">
                  <StatusPill tone={severityTone(finding.severity)} label={deepAuditSeverityLabel(finding.severity)} />
                  <StatusPill tone="cyan" label={deepAuditCategoryLabel(finding.category)} />
                </div>
                <h3 className="mt-2 text-sm font-semibold text-white">{finding.title}</h3>
                <p className="mt-1 text-sm leading-6 hc-soft">{finding.problem}</p>
                <p className="mt-2 text-sm text-white"><span className="font-semibold">Fix-Hinweis:</span> {finding.fix_hint}</p>
              </div>
              <div className="shrink-0 rounded-md border border-white/10 bg-black/20 px-2.5 py-2 lg:max-w-sm">
                <p className="text-[10px] font-semibold uppercase tracking-[.12em] hc-dim">Technische Spur</p>
                <p className="mt-1 break-all hc-mono text-xs hc-soft">{finding.fileline}</p>
              </div>
            </div>
            <details className="mt-3 rounded-md border border-white/10 bg-black/20 px-3 py-2 text-xs hc-soft">
              <summary className="cursor-pointer font-semibold text-white">Evidence anzeigen</summary>
              <blockquote className="mt-2 whitespace-pre-wrap rounded border border-white/10 bg-white/[.03] px-3 py-2 text-xs text-zinc-100">{finding.evidence}</blockquote>
            </details>
          </article>
        ))}
      </div>
    </div>
  );
}

function deepAuditSeverityLabel(severity: DeepAuditFinding["severity"]): string {
  switch (severity) {
    case "critical":
      return "Kritisch";
    case "high":
      return "Hoch";
    case "medium":
      return "Mittel";
    case "low":
      return "Niedrig";
  }
}

function deepAuditCategoryLabel(category: string): string {
  switch (category) {
    case "bug_risk":
      return "Bug-Risiko";
    case "security":
      return "Sicherheit";
    case "contradiction":
      return "Widerspruch";
    case "missing_section":
      return "Lücke";
    case "operational_risk":
      return "Betriebsrisiko";
    default:
      return category || "Audit";
  }
}

function CodeAuditSlotPicker() {
  const [aux, setAux] = useState<AuxiliaryModelsResponse | null>(null);
  const [loading, setLoading] = useState(true);
  const [pickerOpen, setPickerOpen] = useState(false);
  const [saving, setSaving] = useState(false);
  const [refreshKey, setRefreshKey] = useState(0);

  const loadAux = async () => {
    setLoading(true);
    try {
      setAux(await fetchJSON<AuxiliaryModelsResponse>("/api/model/auxiliary"));
    } finally {
      setLoading(false);
    }
  };

  useEffect(() => {
    queueMicrotask(() => void loadAux());
  }, []);

  const assignment = aux?.tasks.find((item) => item.task === "code_audit") ?? null;
  const value = !assignment?.provider || assignment.provider === "auto" ? de.autoresearch.laneModelAuto : `${assignment.provider}${assignment.model ? ` · ${assignment.model}` : ""}`;

  const loadOptionsForPicker = async (): Promise<ModelOptionsResponse> => {
    const options = await fetchJSON<ModelOptionsResponse>("/api/model/options");
    if (!assignment?.provider || assignment.provider === "auto") return options;
    return {
      ...options,
      provider: assignment.provider,
      model: assignment.model,
      providers: options.providers?.map((provider) => ({ ...provider, is_current: provider.slug === assignment.provider })),
    };
  };

  return (
    <div className="rounded-lg border border-white/10 bg-black/20 p-2">
      <div className="mb-2 flex items-center justify-between gap-2">
        <span className="text-xs hc-soft">Modell</span>
        {loading ? <Spinner /> : <StatusPill tone={!assignment?.provider || assignment.provider === "auto" ? "zinc" : "cyan"} label="code_audit" />}
      </div>
      <p className="hc-mono truncate text-xs hc-soft" title={value}>{value}</p>
      <Button outlined className="hc-hit mt-2 w-full" onClick={() => setPickerOpen(true)} disabled={loading || saving} prefix={saving ? <Spinner /> : <Settings2 className="h-4 w-4" />}>
        {de.autoresearch.laneModelChange}
      </Button>
      {pickerOpen ? (
        <ModelPickerDialog
          key={`code-audit-${refreshKey}`}
          loader={loadOptionsForPicker}
          alwaysGlobal
          title={de.autoresearch.laneModelPickerTitle("Deep-Audit")}
          onApply={async ({ provider, model }) => {
            setSaving(true);
            try {
              await fetchJSON<unknown>("/api/model/set", {
                method: "POST",
                headers: { "Content-Type": "application/json" },
                body: JSON.stringify({ scope: "auxiliary", task: "code_audit", provider, model }),
              });
              await loadAux();
              setRefreshKey((value) => value + 1);
            } finally {
              setSaving(false);
            }
          }}
          onClose={() => setPickerOpen(false)}
        />
      ) : null}
    </div>
  );
}

function TestHardeningSlotPicker() {
  const [aux, setAux] = useState<AuxiliaryModelsResponse | null>(null);
  const [loading, setLoading] = useState(true);
  const [pickerOpen, setPickerOpen] = useState(false);
  const [saving, setSaving] = useState(false);
  const [refreshKey, setRefreshKey] = useState(0);

  const loadAux = async () => {
    setLoading(true);
    try {
      setAux(await fetchJSON<AuxiliaryModelsResponse>("/api/model/auxiliary"));
    } finally {
      setLoading(false);
    }
  };

  useEffect(() => {
    queueMicrotask(() => void loadAux());
  }, []);

  const assignment = aux?.tasks.find((item) => item.task === "test_hardening") ?? null;
  const value = !assignment?.provider || assignment.provider === "auto" ? de.autoresearch.laneModelAuto : `${assignment.provider}${assignment.model ? ` · ${assignment.model}` : ""}`;

  const loadOptionsForPicker = async (): Promise<ModelOptionsResponse> => {
    const options = await fetchJSON<ModelOptionsResponse>("/api/model/options");
    if (!assignment?.provider || assignment.provider === "auto") return options;
    return {
      ...options,
      provider: assignment.provider,
      model: assignment.model,
      providers: options.providers?.map((provider) => ({ ...provider, is_current: provider.slug === assignment.provider })),
    };
  };

  return (
    <div className="rounded-lg border border-white/10 bg-black/20 p-2">
      <div className="mb-2 flex items-center justify-between gap-2">
        <span className="text-xs hc-soft">Modell</span>
        {loading ? <Spinner /> : <StatusPill tone={!assignment?.provider || assignment.provider === "auto" ? "zinc" : "cyan"} label="test_hardening" />}
      </div>
      <p className="hc-mono truncate text-xs hc-soft" title={value}>{value}</p>
      <Button outlined className="hc-hit mt-2 w-full" onClick={() => setPickerOpen(true)} disabled={loading || saving} prefix={saving ? <Spinner /> : <Settings2 className="h-4 w-4" />}>
        {de.autoresearch.laneModelChange}
      </Button>
      {pickerOpen ? (
        <ModelPickerDialog
          key={`test-hardening-${refreshKey}`}
          loader={loadOptionsForPicker}
          alwaysGlobal
          title={de.autoresearch.laneModelPickerTitle("Test-Foundry")}
          onApply={async ({ provider, model }) => {
            setSaving(true);
            try {
              await fetchJSON<unknown>("/api/model/set", {
                method: "POST",
                headers: { "Content-Type": "application/json" },
                body: JSON.stringify({ scope: "auxiliary", task: "test_hardening", provider, model }),
              });
              await loadAux();
              setRefreshKey((value) => value + 1);
            } finally {
              setSaving(false);
            }
          }}
          onClose={() => setPickerOpen(false)}
        />
      ) : null}
    </div>
  );
}

function RecentRuns({ runs, proposals }: { runs: AutoresearchRun[]; proposals: ProposalStore["proposals"] }) {
  const totalTokens = sumRunTokens(runs);
  const recent = summarizeRecentRuns(runs, 7);
  const proposalRoi = summarizeProposalRoi(proposals, recent.tokens);
  const runSummary = getAutoresearchRunSummary({
    runs,
    acceptanceRate: proposalRoi.acceptanceRate,
    tokensPerApplied: proposalRoi.tokensPerApplied,
  });
  const runCards = runs.slice(0, 5).map((run) => ({ run, card: getAutoresearchRunCard(run) }));
  return (
    <section id="autoresearch-history" className="hc-card scroll-mt-6 p-4">
      <div className="mb-3 flex items-start justify-between gap-3">
        <div>
          <h2 className="text-base font-semibold text-white">{de.autoresearch.recentRuns}</h2>
          <p className="mt-1 text-xs hc-soft" title={de.autoresearch.roi7dAcceptedNote}>
            <span className="text-white">{de.autoresearch.roi7dHeading}:</span>{" "}
            {recent.runs > 0 ? de.autoresearch.roi7dLine(recent.runs, recent.tokens, recent.proposed, recent.scanned) : de.autoresearch.roi7dEmpty}
          </p>
          <p className="mt-1 text-xs hc-soft">
            {de.autoresearch.roi7dAcceptedLine(proposalRoi.acceptanceRate, proposalRoi.applied, proposalRoi.decided, proposalRoi.tokensPerApplied)}
          </p>
        </div>
        {totalTokens > 0 ? <span className="hc-mono text-xs hc-soft">{de.autoresearch.runsTokensTotal}: {totalTokens.toLocaleString("de-DE")}</span> : null}
      </div>
      <RunSummaryPanel summary={runSummary} />
      {runs.length === 0 ? (
        <p className="text-sm hc-soft">{de.autoresearch.recentRunsEmpty}</p>
      ) : (
        <div className="space-y-3">
          <div className="grid gap-3 lg:grid-cols-2">
            {runCards.map(({ run, card }, i) => (
              <RunHistoryCard key={`${run.at}-${run.request_id ?? i}`} run={run} card={card} />
            ))}
          </div>
          {runs.length > runCards.length ? <p className="text-xs hc-dim">Weitere {runs.length - runCards.length} ältere Läufe stehen in den Rohdaten.</p> : null}
          <details className="rounded-lg border border-white/10 bg-black/20 p-3">
            <summary className="cursor-pointer text-sm font-medium text-white">Rohdaten-Tabelle anzeigen</summary>
            <div className="mt-3 overflow-x-auto">
              <table className="w-full text-left text-sm">
                <thead className="hc-dim">
                  <tr className="border-b border-white/10">
                    <th className="py-1 pr-3 font-medium">{de.autoresearch.runsColTime}</th>
                    <th className="py-1 pr-3 font-medium">{de.autoresearch.runsColLane}</th>
                    <th className="py-1 pr-3 text-right font-medium">{de.autoresearch.runsColTokens}</th>
                    <th className="py-1 pr-3 text-right font-medium">{de.autoresearch.runsColProposed}</th>
                    <th className="py-1 pr-3 text-right font-medium">{de.autoresearch.runsColScanned}</th>
                    <th className="py-1 pr-3 text-right font-medium">{de.autoresearch.runsColErrors}</th>
                    <th className="py-1 text-right font-medium">{de.autoresearch.runsColVetoed}</th>
                  </tr>
                </thead>
                <tbody className="hc-soft">
                  {runs.map((run, i) => {
                    const model = runModelLabel(run);
                    return (
                      <tr key={`${run.at}-${run.request_id ?? i}`} className="border-b border-white/5 last:border-0">
                        <td className="py-1 pr-3 hc-mono text-xs">{formatRunTime(run.at)}</td>
                        <td className="py-1 pr-3">
                          <div className="flex min-w-40 flex-wrap items-center gap-1.5">
                            <StatusPill tone={runLaneTone(run.lane)} label={runLaneLabel(run.lane)} />
                            {model ? <span className="max-w-52 truncate rounded-full border border-zinc-600/25 bg-zinc-600/10 px-2 py-1 text-xs text-zinc-200" title={model}>{model}</span> : null}
                          </div>
                        </td>
                        <td className="py-1 pr-3 text-right hc-mono">{run.tokens ? run.tokens.toLocaleString("de-DE") : "—"}</td>
                        <td className="py-1 pr-3 text-right hc-mono">{run.proposed}</td>
                        <td className="py-1 pr-3 text-right hc-mono">{run.scanned}</td>
                        <td className={cn("py-1 pr-3 text-right hc-mono", run.errors > 0 ? "text-red-300" : "")}>{run.errors}</td>
                        <td className="py-1 text-right hc-mono text-zinc-300">{runVetoedCount(run)}</td>
                      </tr>
                    );
                  })}
                </tbody>
              </table>
            </div>
          </details>
        </div>
      )}
    </section>
  );
}

function RunHistoryCard({ run, card }: { run: AutoresearchRun; card: AutoresearchRunCard }) {
  const model = runModelLabel(run);
  return (
    <article className={cn("rounded-lg border p-3", reviewStepToneClass(card.tone))}>
      <div className="flex flex-wrap items-start justify-between gap-2">
        <div className="min-w-0">
          <div className="flex flex-wrap items-center gap-2">
            <StatusPill tone={runLaneTone(run.lane)} label={runLaneLabel(run.lane)} />
            <StatusPill tone={card.tone} label={card.label} />
            <span className="hc-mono text-xs hc-soft">{formatRunTime(run.at)}</span>
          </div>
          <h3 className="mt-2 text-sm font-semibold text-white">{card.title}</h3>
          <p className="mt-1 text-xs leading-5 hc-soft">{card.detail}</p>
          <p className="mt-2 text-xs text-white"><span className="font-semibold">Danach:</span> {card.next}</p>
        </div>
        {model ? <span className="max-w-52 truncate rounded-full border border-zinc-600/25 bg-zinc-600/10 px-2 py-1 text-xs text-zinc-200" title={model}>{model}</span> : null}
      </div>
      <div className="mt-3 grid grid-cols-2 gap-2 sm:grid-cols-5">
        {card.facts.map((fact) => (
          <div key={fact.label} className={cn("rounded-md border px-2 py-1.5", reviewStepToneClass(fact.tone))}>
            <p className="text-[9px] font-semibold uppercase tracking-[.12em] hc-dim">{fact.label}</p>
            <p className="mt-1 truncate text-sm font-semibold text-white" title={fact.value}>{fact.value}</p>
          </div>
        ))}
      </div>
    </article>
  );
}

function RunSummaryPanel({ summary }: { summary: AutoresearchRunSummary }) {
  return (
    <div className="mb-4 rounded-lg border border-white/10 bg-white/[.025] p-3">
      <div className="flex flex-col gap-3 lg:flex-row lg:items-center lg:justify-between">
        <div className="min-w-0">
          <div className="flex flex-wrap items-center gap-2">
            <p className="hc-eyebrow">Lauf-Auswertung</p>
            <StatusPill tone={summary.tone} label={summary.label} />
          </div>
          <h3 className="mt-2 text-base font-semibold text-white">{summary.title}</h3>
          <p className="mt-1 max-w-3xl text-sm leading-6 hc-soft">{summary.detail}</p>
          <p className="mt-2 text-sm text-white"><span className="font-semibold">Nächster Schritt:</span> {summary.next}</p>
        </div>
        <div className="grid shrink-0 gap-2 sm:grid-cols-5 lg:min-w-[520px]">
          {summary.facts.map((fact) => (
            <div key={fact.label} className={cn("rounded-md border px-3 py-2", reviewStepToneClass(fact.tone))}>
              <p className="text-[10px] font-semibold uppercase tracking-[.14em] hc-dim">{fact.label}</p>
              <p className="mt-1 truncate text-sm font-semibold text-white" title={fact.value}>{fact.value}</p>
            </div>
          ))}
        </div>
      </div>
    </div>
  );
}
