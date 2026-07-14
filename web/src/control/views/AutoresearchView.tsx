import { useEffect, useMemo, useRef, useState } from "react";
import { useSearchParams } from "react-router-dom";
import { Spinner } from "@nous-research/ui/ui/components/spinner";
import { TriangleAlert } from "lucide-react";
import { fetchJSON } from "@/lib/api";
import { useAutoresearchRuns, useAutoresearchStatus, useDeepAudit, useTestFoundry, type useProposals } from "../hooks/useControlData";
import { getAutoresearchActionPlan } from "../lib/autoresearchActionPlan";
import { getAutoresearchActivityCard } from "../lib/autoresearchActivity";
import { AUTORESEARCH_SECTION_NAV } from "../lib/autoresearchNavigation";
import { getAutoresearchRecommendation } from "../lib/autoresearchRecommendation";
import { getAutoresearchReadiness } from "../lib/autoresearchReadiness";
import { getAutoresearchResolvedSummary } from "../lib/autoresearchResolvedSummary";
import { filterAutoresearchQueueByMode, getAutoresearchEmptyQueueModeGuidance, getAutoresearchQueueModeSummary, type AutoresearchQueueMode } from "../lib/autoresearchQueueMode";
import { canApplyAllOpenSkillProposals, canBatchConfirmAutoresearchSelection, describeTopCardMode, getAutoresearchDecisionGuide, getAutoresearchQueueActionSummary, getBatchSafeVisibleProposalIds, proposalNeedsManualReview } from "../lib/autoresearchDecisionGuide";
import { getAutoresearchReviewFlow } from "../lib/autoresearchReviewFlow";
import { getAdvancedRunChecklist, getDeepAuditGuidance, getResearchLoopGuidance, getResearchLoopPreset, getResearchLoopStartChecklist, getResearchLoopStartControl, getResearchLoopStartSummary, getSelectedResearchLoopPresetId, getTestFoundryGuidance, type ResearchLoopPresetId } from "../lib/autoresearchRunGuidance";
import { getTestFoundryResultSummary } from "../lib/autoresearchTestFoundrySummary";
import { getProposalOperatorBrief } from "../lib/autoresearchProposalBrief";
import { rankAutoresearchProposalGroups } from "../lib/proposalGroups";
import { de } from "../i18n/de";
import type { Density } from "../hooks/useDensity";
import { StaleBadge } from "../components/atoms";
import { SignalLabel, signalToneFromLegacy } from "../components/leitstand";
import { AutoresearchHero } from "./autoresearch/AutoresearchHero";
import { ProposalQueue } from "./autoresearch/ProposalQueue";
import { LoopControls } from "./autoresearch/LoopControls";
import { AdvancedSection } from "./autoresearch/AdvancedSection";
import { ResolvedQueues } from "./autoresearch/ResolvedQueues";
import { RunsList } from "./autoresearch/RunsList";
import { ActivityTimelineItem, LatestActivityPanel } from "./autoresearch/panels";
import { clampLoopIterations, clearProposalSelection, describeArea, describeAutoresearchBusy, describeLoopStatus, formatRunTime, parseMinUseCount, rankAutoresearchReviewQueue, readLastRunCounters, selectVisibleProposals, severityDistribution, shouldShowResearchErrorBadge, splitAutoresearchProposals, toggleProposalSelection } from "../lib/autoresearch";
import { getAutoresearchKeyboardAction } from "../lib/autoresearchKeyboard";

export { DeepAuditFindings, LatestActivityPanel } from "./autoresearch/panels";

type ProposalStore = ReturnType<typeof useProposals>;
type PruneMessage = { tone: "emerald" | "amber" | "red"; text: string };

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
  const delivery = split.delivery;
  const integrated = split.integrated;
  const history = split.history;
  const [queueMode, setQueueMode] = useState<AutoresearchQueueMode>("all");
  const distribution = useMemo(() => severityDistribution(open), [open]);
  const queueModeSummary = useMemo(() => getAutoresearchQueueModeSummary(open, queueMode), [open, queueMode]);
  const filteredOpen = useMemo(() => filterAutoresearchQueueByMode(open, queueMode), [open, queueMode]);
  const emptyQueueModeGuidance = useMemo(() => getAutoresearchEmptyQueueModeGuidance(queueModeSummary), [queueModeSummary]);
  const filteredDistribution = useMemo(() => severityDistribution(filteredOpen), [filteredOpen]);
  const relevanceQueue = useMemo(() => rankAutoresearchReviewQueue(filteredOpen, 3), [filteredOpen]);
  const proposalGroupQueue = useMemo(() => rankAutoresearchProposalGroups(filteredOpen, 3), [filteredOpen]);
  const queueProposalIds = useMemo(() => [...relevanceQueue.shortlist, ...relevanceQueue.backlog].map((item) => item.proposal.id), [relevanceQueue.backlog, relevanceQueue.shortlist]);
  // BLOCKER FIX: "Sichtbare auswählen" must only target the shortlist the
  // operator actually sees, never the backlog hidden in the collapsed disclosure.
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
    busy: !!store.busy,
  });
  const selectionControlsBusy = batchBusy || !!store.busy;
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
  const latestActivityCard = useMemo(() => latestActivity ? getAutoresearchActivityCard(latestActivity) : null, [latestActivity]);
  const deepAuditHasFindings = (deepAudit.findings?.findings.length ?? 0) > 0 || (deepAudit.findings?.proposals.length ?? 0) > 0;
  const testFoundryResultSummary = useMemo(() => getTestFoundryResultSummary(testFoundry.status?.last_run), [testFoundry.status?.last_run]);
  const advancedNeedsAttention = deepAuditRunning || testFoundryRunning || deepAuditHasFindings || !!testFoundryResultSummary || !!deepAudit.error || !!testFoundry.error || !!deepAuditMessage || !!testFoundryMessage;
  const lastRunCounters = readLastRunCounters(status.data?.last_run);
  const showResearchErrorBadge = shouldShowResearchErrorBadge(lastRunCounters.researchErrors);
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

  // Deep-link from the Decision Inbox: /control/autoresearch?focus=<id> scrolls
  // straight to that proposal card. Ref-guarded so it fires once per id (the 6s
  // poll re-runs this effect) and waits until the proposal has actually loaded.
  const [focusParams] = useSearchParams();
  const focusId = focusParams.get("focus");
  const consumedFocusRef = useRef<string | null>(null);
  useEffect(() => {
    if (!focusId || consumedFocusRef.current === focusId) return;
    if (!store.proposals.some((p) => p.id === focusId)) return;
    consumedFocusRef.current = focusId;
    focusProposal(focusId);
  }, [focusId, store.proposals]);

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
      <AutoresearchHero
        status={status}
        statusTone={statusTone}
        loop={loop}
        recommendation={recommendation}
        topProposal={topProposal}
        topCardMode={topCardMode}
        topProposalBrief={topProposalBrief}
        readiness={readiness}
        openCount={open.length}
        highPriorityCount={highPriorityCount}
        revertedCount={reverted.length}
        lastRunLabel={runs.data?.runs?.[0] ? formatRunTime(runs.data.runs[0].at) : "-"}
        sectionNavItems={AUTORESEARCH_SECTION_NAV}
        actionPlan={actionPlan}
        storeBusy={store.busy}
        openSkillCount={store.openSkillProposals.length}
        openSkillManualReviewCount={openSkillManualReviewCount}
        canApplyAllOpenSkills={canApplyAllOpenSkills}
        pruneBusy={pruneBusy}
        alert={{
          show: advancedNeedsAttention || showResearchErrorBadge,
          deepAuditRunning,
          testFoundryRunning,
          deepAuditError: deepAudit.error,
          testFoundryError: testFoundry.error,
          deepAuditMessage,
          testFoundryMessage,
          researchErrorBadge: showResearchErrorBadge,
        }}
        onPrimary={runPrimaryRecommendation}
        onFocusProposal={focusProposal}
        onJump={scrollTo}
        onGenerate={store.generate}
        onGenerateCodeWeaknesses={store.generateCodeWeaknesses}
        onApplyAll={store.applyAll}
        onPrune={() => void pruneAutoresearch()}
      />

      {pruneMessage ? (
        <div className={`flex items-start gap-2 rounded-card border px-3 py-2 text-sec ${pruneMessage.tone === "red" ? "border-status-alert/30 bg-status-alert/10 text-status-alert" : "border-line bg-surface-2 text-ink-2"}`}>
          {pruneMessage.tone === "red" ? <TriangleAlert aria-hidden className="mt-0.5 size-4 shrink-0" /> : <SignalLabel tone={signalToneFromLegacy(pruneMessage.tone)} label={pruneMessage.tone === "emerald" ? "Erledigt" : "Hinweis"} />}
          <span>{pruneMessage.text}</span>
        </div>
      ) : null}
      {store.loading && open.length === 0 ? <div className="rounded-card border border-line bg-surface-2 px-3 py-2 text-sec text-ink-2">Quelle wird geprüft...</div> : null}
      {store.error ? <div className="flex items-start gap-2 rounded-card border border-status-alert/30 bg-status-alert/10 px-3 py-2 text-sec text-status-alert"><TriangleAlert aria-hidden className="mt-0.5 size-4 shrink-0" />{store.error}</div> : null}
      {busyNotice ? <div className="flex items-center gap-2 rounded-card border border-line bg-surface-2 px-3 py-2 text-sec text-ink-2"><Spinner />{busyNotice}</div> : null}
      {latestActivity && latestActivityCard ? <LatestActivityPanel at={latestActivity.at} card={latestActivityCard} /> : null}

      <ProposalQueue
        density={density}
        focusId={focusId}
        openCount={open.length}
        revertedCount={reverted.length}
        filteredOpenCount={filteredOpen.length}
        storeLoading={store.loading}
        storeBusy={store.busy}
        batchBusy={batchBusy}
        selectionControlsBusy={selectionControlsBusy}
        bulkRevertedBusy={bulkRevertedBusy}
        selectedProposalIds={selectedProposalIds}
        selectedIds={selectedIds}
        selectedManualReviewCount={selectedManualReviewCount}
        batchSafeVisibleProposalIds={batchSafeVisibleProposalIds}
        manualReviewVisibleCount={manualReviewVisibleCount}
        canConfirmSelection={canConfirmSelection}
        distribution={distribution}
        relevanceQueue={relevanceQueue}
        proposalGroupQueue={proposalGroupQueue}
        queueModeSummary={queueModeSummary}
        queueMode={queueMode}
        emptyQueueModeGuidance={emptyQueueModeGuidance}
        reviewFlow={reviewFlow}
        decisionGuide={decisionGuide}
        queueActionSummary={queueActionSummary}
        batchConfirmById={store.batchConfirmById}
        onQueueModeChange={setQueueMode}
        onSelectQueue={selectQueue}
        onClearSelection={clearSelection}
        onConfirmSelected={() => void confirmSelected()}
        onRunReviewFlowPrimary={runReviewFlowPrimary}
        onToggleSelection={toggleSelection}
        onApply={store.apply}
        onSkip={store.skip}
        onSkipBatch={store.skipBatch}
        onConfirmBatch={store.confirmBatch}
      />

      <LoopControls
        loop={loop}
        status={status.data}
        latestRun={runs.data?.runs?.[0] ?? null}
        routeOk={routeOk}
        loopBusy={loopBusy}
        loopMessage={loopMessage}
        area={area}
        focus={focus}
        minUseCount={minUseCount}
        maxIterations={maxIterations}
        selectedLoopPresetId={selectedLoopPresetId}
        researchLoopGuidance={researchLoopGuidance}
        researchLoopStart={researchLoopStart}
        researchLoopStartSummary={researchLoopStartSummary}
        researchLoopStartChecklist={researchLoopStartChecklist}
        onAreaChange={setArea}
        onFocusChange={setFocus}
        onMinUseCountChange={setMinUseCount}
        onMaxIterationsChange={setMaxIterations}
        onApplyPreset={applyLoopPreset}
        onStartLoop={() => void startLoop()}
        onStopLoop={() => void stopLoop()}
      />

      <AdvancedSection
        open={effectiveAdvancedOpen}
        needsAttention={advancedNeedsAttention}
        deepAudit={deepAudit}
        deepAuditRunning={deepAuditRunning}
        effectiveDeepAuditSubsystem={effectiveDeepAuditSubsystem}
        deepAuditFocus={deepAuditFocus}
        deepAuditMessage={deepAuditMessage}
        deepAuditGuidance={deepAuditGuidance}
        deepAuditChecklist={deepAuditChecklist}
        testFoundry={testFoundry}
        testFoundryRunning={testFoundryRunning}
        effectiveTestFoundryTarget={effectiveTestFoundryTarget}
        testFoundryApply={testFoundryApply}
        testFoundryMessage={testFoundryMessage}
        testFoundryResultSummary={testFoundryResultSummary}
        testFoundryGuidance={testFoundryGuidance}
        testFoundryChecklist={testFoundryChecklist}
        onToggle={(nextOpen) => {
          if (!advancedNeedsAttention) setAdvancedOpen(nextOpen);
        }}
        onDeepAuditSubsystemChange={setDeepAuditSubsystem}
        onDeepAuditFocusChange={setDeepAuditFocus}
        onStartDeepAudit={() => void startDeepAudit()}
        onTestFoundryTargetChange={setTestFoundryTarget}
        onTestFoundryApplyChange={setTestFoundryApply}
        onStartTestFoundry={() => void startTestFoundry()}
      />

      <ResolvedQueues
        summary={resolvedSummary}
        reverted={reverted}
        delivery={delivery}
        integrated={integrated}
        history={history}
        metrics={store.data?.metrics ?? null}
        density={density}
        archiveBusy={bulkRevertedBusy}
        archiveDisabled={!!store.busy || bulkRevertedBusy}
        onArchiveReverted={() => void skipAllReverted()}
        onApply={store.apply}
        onSkip={store.skip}
      />

      {runs.error ? <div className="flex items-start gap-2 rounded-card border border-status-alert/30 bg-status-alert/10 px-3 py-2 text-sec text-status-alert"><TriangleAlert aria-hidden className="mt-0.5 size-4 shrink-0" />{runs.error}</div> : null}
      {runs.isStale || runs.error ? (
        <div className="flex justify-end">
          <StaleBadge isStale={runs.isStale} lastUpdated={runs.lastUpdated} errorObj={runs.errorObj} error={runs.error} />
        </div>
      ) : null}
      <RunsList runs={runs.data?.runs ?? []} proposals={store.proposals} loading={runs.loading && !runs.data} />

      <section className="rounded-panel border border-line bg-surface-1 p-4">
        <h2 className="mb-3 text-base font-semibold text-ink">{de.autoresearch.activity}</h2>
        {store.activity.length === 0 ? <p className="text-sm text-ink-2">Noch keine Aktion in dieser Ansicht.</p> : (
          <div className="space-y-2">
            {store.activity.map((entry) => <ActivityTimelineItem key={`${entry.at}-${entry.text}`} at={entry.at} card={getAutoresearchActivityCard(entry)} />)}
          </div>
        )}
      </section>
    </div>
  );
}
