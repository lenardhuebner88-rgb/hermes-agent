import { useEffect, useMemo, useState } from "react";
import { CheckCheck, FlaskConical, GitPullRequestArrow, ListChecks, Play, RotateCw, Square, Target, X } from "lucide-react";
import { Button } from "@nous-research/ui/ui/components/button";
import { Spinner } from "@nous-research/ui/ui/components/spinner";
import { cn } from "@/lib/utils";
import { fetchJSON } from "@/lib/api";
import { useAutoresearchRuns, useAutoresearchStatus, type useProposals } from "../hooks/useControlData";
import { fmtClock } from "../lib/derive";
import { AUTORESEARCH_AREAS, clampLoopIterations, clearProposalSelection, codeWeaknessBusyKey, describeArea, describeLoopStatus, filterBySeverityThreshold, formatResearchTokens, formatRunTime, hasResearchCounters, parseMinUseCount, pruneProposalSelection, rankAutoresearchReviewQueue, readLastRunCounters, runLaneLabel, runLaneTone, selectVisibleProposals, severityDistribution, shouldShowResearchErrorBadge, splitAutoresearchProposals, summarizeRecentRuns, sumRunTokens, toggleProposalSelection } from "../lib/autoresearch";
import type { CodeWeaknessScope } from "../lib/autoresearch";
import { KEYMAP } from "../lib/keymap";
import { de } from "../i18n/de";
import type { Density } from "../hooks/useDensity";
import type { AutoresearchRun } from "../lib/types";
import { StatusPill, ToneCallout } from "../components/atoms";
import { ProposalCard } from "../components/ProposalCard";

type ProposalStore = ReturnType<typeof useProposals>;

export function AutoresearchView({ density, store }: { density: Density; store: ProposalStore }) {
  const status = useAutoresearchStatus();
  const runs = useAutoresearchRuns();
  const split = useMemo(() => splitAutoresearchProposals(store.proposals), [store.proposals]);
  const open = split.actionable;
  const reverted = split.reverted;
  const applied = split.applied;
  const skipped = split.skipped;
  const [severityFilter, setSeverityFilter] = useState<"all" | "high">("all");
  const distribution = useMemo(() => severityDistribution(open), [open]);
  const filteredOpen = useMemo(() => (severityFilter === "high" ? filterBySeverityThreshold(open, "high") : open), [open, severityFilter]);
  const relevanceQueue = useMemo(() => rankAutoresearchReviewQueue(filteredOpen, 10), [filteredOpen]);
  const queueProposalIds = useMemo(() => [...relevanceQueue.shortlist, ...relevanceQueue.backlog].map((item) => item.proposal.id), [relevanceQueue.backlog, relevanceQueue.shortlist]);
  // BLOCKER FIX: "Sichtbare auswählen" must only target the shortlist the
  // operator actually sees, never the backlog hidden in the collapsed <details>.
  const visibleProposalIds = useMemo(() => relevanceQueue.shortlist.map((item) => item.proposal.id), [relevanceQueue.shortlist]);
  const [selectedProposalIds, setSelectedProposalIds] = useState<Set<string>>(() => new Set());
  const statusTone = status.data?.state === "crashed" ? "red" : status.data?.heartbeat_fresh ? "cyan" : "amber";
  const loop = describeLoopStatus(status.data);
  const [maxIterations, setMaxIterations] = useState("2");
  const [area, setArea] = useState("all");
  const [focus, setFocus] = useState("recommended_sections");
  const [minUseCount, setMinUseCount] = useState("");
  const [codeWeaknessScope, setCodeWeaknessScope] = useState<CodeWeaknessScope>("incremental");
  const [loopBusy, setLoopBusy] = useState<"start" | "stop" | null>(null);
  const [loopMessage, setLoopMessage] = useState<string | null>(null);
  const selectedIds = useMemo(() => queueProposalIds.filter((id) => selectedProposalIds.has(id)), [queueProposalIds, selectedProposalIds]);
  const batchBusy = store.busy === "confirm-batch";

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

  useEffect(() => {
    const onKey = (event: KeyboardEvent) => {
      const target = event.target as HTMLElement | null;
      if (target?.closest("input,textarea,[contenteditable='true'],[role='dialog']")) return;
      const top = relevanceQueue.shortlist.find((item) => item.proposal.status === "proposed")?.proposal ?? open[0];
      if (!top) return;
      const key = event.key.toLowerCase();
      if (KEYMAP.autoresearch.apply.includes(key as "a")) {
        event.preventDefault();
        void store.apply(top);
      }
      if (KEYMAP.autoresearch.skip.includes(key as "s")) {
        event.preventDefault();
        void store.skip(top);
      }
    };
    window.addEventListener("keydown", onKey);
    return () => window.removeEventListener("keydown", onKey);
  }, [open, relevanceQueue.shortlist, store]);

  useEffect(() => {
    setSelectedProposalIds((current) => {
      const next = new Set(pruneProposalSelection(current, queueProposalIds));
      return next.size === current.size ? current : next;
    });
  }, [queueProposalIds]);

  const toggleSelection = (proposalId: string, selected: boolean) => {
    setSelectedProposalIds((current) => toggleProposalSelection(current, proposalId, selected));
  };

  const selectQueue = () => setSelectedProposalIds(selectVisibleProposals(visibleProposalIds));
  const clearSelection = () => setSelectedProposalIds(clearProposalSelection());
  const confirmSelected = async () => {
    await store.confirmBatch(selectedIds);
  };

  return (
    <div className="space-y-5">
      <section className="hc-card p-4 sm:p-5">
        <div className="flex flex-col gap-4 lg:flex-row lg:items-center lg:justify-between">
          <div className="space-y-3">
            <div className="flex flex-wrap items-center gap-2">
              {status.loading ? <Spinner /> : <StatusPill tone={statusTone} label={status.data?.state ?? "unbekannt"} dot={loop.running ? "live" : status.data?.state === "crashed" ? "error" : "idle"} />}
              <StatusPill tone={loop.routeTone} label={`Route ${status.data?.route_status ?? "unbekannt"}`} dot={loop.routeTone === "emerald" ? "ready" : "warn"} />
              <span className="rounded-full border border-white/10 px-2.5 py-1 text-xs hc-soft">{loop.iterationLabel}</span>
            </div>
            <div>
              <p className="hc-eyebrow">{de.autoresearch.nextStep}</p>
              <p className="mt-1 max-w-2xl text-base leading-7 text-white">{open.length > 0 ? de.autoresearch.nextStepOpen(open.length) : reverted.length > 0 ? <span>{open.length} offen · <span className="underline decoration-dotted underline-offset-2" title={de.autoresearch.revertedExplain}>{de.autoresearch.revertedCount(reverted.length)}</span></span> : de.autoresearch.nextStepEmpty}</p>
              {status.error ? <p className="mt-2 text-sm text-red-200">{status.error}</p> : null}
            </div>
          </div>
          <div className="flex flex-col gap-2 sm:flex-row sm:flex-wrap lg:flex-col xl:flex-row xl:items-center">
            <Button className="hc-hit" onClick={store.generate} disabled={!!store.busy} title={de.autoresearch.generateHint} prefix={store.busy === "generate" ? <Spinner /> : <RotateCw className="h-4 w-4" />}>
              Vorschläge erzeugen (sofort)
            </Button>
            <div className="flex items-center gap-2">
              <div className="inline-flex overflow-hidden rounded-lg border border-white/10 text-sm">
                <button type="button" onClick={() => setCodeWeaknessScope("incremental")} title={de.autoresearch.scanScopeHintChanged} className={cn("px-3 py-1", codeWeaknessScope === "incremental" ? "bg-[var(--hc-accent)] text-white" : "hc-soft")}>
                  {de.autoresearch.scanScopeChanged}
                </button>
                <button type="button" onClick={() => setCodeWeaknessScope("full")} title={de.autoresearch.scanScopeHintFull} className={cn("px-3 py-1", codeWeaknessScope === "full" ? "bg-[var(--hc-accent)] text-white" : "hc-soft")}>
                  {de.autoresearch.scanScopeFull}
                </button>
                <button type="button" onClick={() => setCodeWeaknessScope("deep")} title={de.autoresearch.deepScanHint} className={cn("px-3 py-1", codeWeaknessScope === "deep" ? "bg-[var(--hc-accent)] text-white" : "hc-soft")}>
                  {de.autoresearch.scanScopeDeep}
                </button>
              </div>
              <Button outlined className="hc-hit" onClick={() => store.generateCodeWeaknesses(codeWeaknessScope)} disabled={!!store.busy} title={de.autoresearch.scanButtonHint} prefix={store.busy === codeWeaknessBusyKey(codeWeaknessScope) ? <Spinner /> : <FlaskConical className="h-4 w-4" />}>
                {de.autoresearch.scanButton}
              </Button>
            </div>
            <Button outlined className="hc-hit" onClick={store.applyAll} disabled={!!store.busy || store.openSkillProposals.length === 0} title={de.autoresearch.applyAllHint} prefix={<GitPullRequestArrow className="h-4 w-4" />}>
              {de.autoresearch.applyAll} ({store.openSkillProposals.length})
            </Button>
          </div>
        </div>
      </section>

      <section className="hc-card p-4 sm:p-5">
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
            <TargetingPreview area={area} focus={focus} maxIterations={maxIterations} minUseCount={minUseCount} />
            <Button outlined className="hc-hit" onClick={() => { setArea("all"); setFocus("recommended_sections"); setMinUseCount(""); setMaxIterations("2"); }} disabled={loop.running || !!loopBusy} title={de.autoresearch.presetRecommendedHint} prefix={<RotateCw className="h-4 w-4" />}>{de.autoresearch.presetRecommended}</Button>
            <Button className="hc-hit" onClick={startLoop} disabled={loop.running || !!loopBusy} prefix={loopBusy === "start" ? <Spinner /> : <Play className="h-4 w-4" />}>Research-Loop starten</Button>
            <Button outlined className="hc-hit" onClick={stopLoop} disabled={!loop.running || !!loopBusy} prefix={loopBusy === "stop" ? <Spinner /> : <Square className="h-4 w-4" />}>Stop</Button>
          </div>
        </div>
      </section>

      {store.loading && open.length === 0 ? <ToneCallout tone="violet">Quelle wird geprüft...</ToneCallout> : null}
      {store.error ? <ToneCallout tone="red">{store.error}</ToneCallout> : null}

      <section className="space-y-3">
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
            <div className="inline-flex overflow-hidden rounded-lg border border-white/10 text-sm">
              <button type="button" onClick={() => setSeverityFilter("all")} className={cn("px-3 py-1", severityFilter === "all" ? "bg-[var(--hc-accent)] text-white" : "hc-soft")}>
                {de.autoresearch.severityFilterAll}
              </button>
              <button type="button" onClick={() => setSeverityFilter("high")} className={cn("px-3 py-1", severityFilter === "high" ? "bg-[var(--hc-accent)] text-white" : "hc-soft")}>
                {de.autoresearch.severityFilterHighPlus}
              </button>
            </div>
            {store.loading ? <Spinner /> : null}
            <div className="flex flex-wrap items-center gap-2">
              <span className="text-sm hc-soft">{de.autoresearch.selectedCount(selectedIds.length)}</span>
              <Button outlined className="hc-hit" onClick={selectQueue} disabled={visibleProposalIds.length === 0 || batchBusy} prefix={<ListChecks className="h-4 w-4" />}>
                {de.autoresearch.selectAllVisible}
              </Button>
              <Button outlined className="hc-hit" onClick={clearSelection} disabled={selectedIds.length === 0 || batchBusy} prefix={<X className="h-4 w-4" />}>
                {de.autoresearch.clearSelection}
              </Button>
              <Button className="hc-hit" onClick={() => void confirmSelected()} disabled={selectedIds.length === 0 || batchBusy} prefix={batchBusy ? <Spinner /> : <CheckCheck className="h-4 w-4" />}>
                {de.autoresearch.batchConfirm}
              </Button>
            </div>
          </div>
        </div>
        {open.length === 0 && !store.loading ? <Empty icon={<FlaskConical className="h-5 w-5" />} text="Keine offenen Vorschläge." /> : null}
        <div className="grid gap-4">
          {relevanceQueue.shortlist.map((item) => (
            <ProposalCard
              key={item.proposal.id}
              proposal={item.proposal}
              priorityGroup={item.group}
              density={density}
              busy={store.busy === item.proposal.id}
              selectable
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

      {reverted.length > 0 ? (
        <details className="space-y-3 border-t border-white/10 pt-4">
          <summary className="cursor-pointer text-lg font-semibold text-white" title={de.autoresearch.revertedExplain}>{de.autoresearch.revertedSummary(reverted.length)}</summary>
          <p className="mt-1 text-sm hc-soft">{de.autoresearch.revertedExplain}</p>
          <div className="mt-3 grid gap-3 opacity-85">{reverted.map((proposal) => <ProposalCard key={proposal.id} proposal={proposal} density={density} onApply={store.apply} onSkip={store.skip} />)}</div>
        </details>
      ) : null}

      {applied.length > 0 ? (
        <details className="space-y-3"><summary className="cursor-pointer text-lg font-semibold text-white">Erledigt ({applied.length})</summary><div className="mt-3 grid gap-3">{applied.map((proposal) => <ProposalCard key={proposal.id} proposal={proposal} density={density} onApply={store.apply} onSkip={store.skip} />)}</div></details>
      ) : null}

      {skipped.length > 0 ? (
        <details className="space-y-3"><summary className="cursor-pointer text-lg font-semibold text-white">Übersprungen ({skipped.length})</summary><div className="mt-3 grid gap-3">{skipped.map((proposal) => <ProposalCard key={proposal.id} proposal={proposal} density={density} onApply={store.apply} onSkip={store.skip} />)}</div></details>
      ) : null}

      <RecentRuns runs={runs.data?.runs ?? []} />

      <section className="hc-card p-4">
        <h2 className="mb-3 text-base font-semibold text-white">{de.autoresearch.activity}</h2>
        {store.activity.length === 0 ? <p className="text-sm hc-soft">Noch keine Aktion in dieser Ansicht.</p> : <div className="space-y-2">{store.activity.map((entry) => <div key={`${entry.at}-${entry.text}`} className={cn("flex gap-3 rounded-lg border px-3 py-2 text-sm", entry.tone === "red" ? "border-red-500/20 bg-red-500/10 text-red-100" : entry.tone === "amber" ? "border-amber-500/20 bg-amber-500/10 text-amber-100" : entry.tone === "emerald" ? "border-emerald-500/20 bg-emerald-500/10 text-emerald-100" : "border-[var(--hc-accent-border)] bg-[var(--hc-accent-wash)] text-[var(--hc-accent-text)]")}><span className="hc-mono hc-dim">{fmtClock(entry.at)}</span><span>{entry.text}</span></div>)}</div>}
      </section>
    </div>
  );
}

function Metric({ label, value }: { label: string; value: string }) {
  return <div className="rounded-lg border border-white/10 bg-white/[.03] px-3 py-2"><p className="text-xs hc-dim">{label}</p><p className="hc-mono truncate text-sm font-semibold text-white">{value}</p></div>;
}

// Live, plain-language readback of the targeting inputs so the operator sees in
// normal German exactly what the loop will do before hitting Start — using the
// *effective* (clamped/parsed) values, not the raw input strings.
function TargetingPreview({ area, focus, maxIterations, minUseCount }: { area: string; focus: string; maxIterations: string; minUseCount: string }) {
  const iters = clampLoopIterations(Number(maxIterations));
  const muc = parseMinUseCount(minUseCount);
  return (
    <div className="rounded-lg border border-[var(--hc-accent-border)] bg-[var(--hc-accent-wash)] px-3 py-2 text-xs text-[var(--hc-accent-text)]">
      <p className="flex items-center gap-1.5 font-semibold"><Target className="h-3.5 w-3.5" />{de.autoresearch.targetingPreviewHeading}</p>
      <p className="mt-1.5">{de.autoresearch.targetingScans} <span className="font-semibold">{describeArea(area)}</span></p>
      <p className="mt-0.5">{de.autoresearch.targetingFocusLabel} <span className="font-semibold">{focus.trim() || "recommended_sections"}</span> · {de.autoresearch.targetingIterations(iters)}</p>
      <p className="mt-0.5">{muc !== null ? de.autoresearch.targetingMinUseValue(muc) : de.autoresearch.targetingMinUseDefault}</p>
      <p className="mt-1 opacity-80">{de.autoresearch.targetingDryRunNote}</p>
    </div>
  );
}

function LastRun({ status, latestRun }: { status: ReturnType<typeof useAutoresearchStatus>["data"]; latestRun: AutoresearchRun | null }) {
  const receipt = status?.last_receipt;
  const note = status?.note;
  const lastRun = status?.last_run;
  const lastRunText = typeof lastRun === "string" || typeof lastRun === "number" ? String(lastRun) : null;
  const objectRun = lastRun && typeof lastRun === "object" ? lastRun as Record<string, unknown> : null;
  const finishedAt = typeof objectRun?.finished_at === "string" ? objectRun.finished_at : null;
  const mode = typeof objectRun?.mode === "string" ? objectRun.mode : null;
  const proposed = typeof objectRun?.proposed === "number" ? objectRun.proposed : null;
  const kept = typeof objectRun?.kept === "number" ? objectRun.kept : null;
  const reverted = typeof objectRun?.reverted === "number" ? objectRun.reverted : null;
  const refused = typeof objectRun?.refused === "string" ? objectRun.refused : null;
  const stopped = objectRun?.stopped === true ? "Signal erhalten" : null;
  const summary = objectRun ? [mode, finishedAt ? new Date(finishedAt).toLocaleString("de-DE") : null].filter(Boolean).join(" · ") : lastRunText;
  // f-autoresearch-tab-driver: surface the observability counters so "0 proposed"
  // reads as converged-healthy vs broken, plus the real MiniMax token spend.
  const counters = readLastRunCounters(lastRun);
  const showCounters = hasResearchCounters(counters);
  const showErrorBadge = shouldShowResearchErrorBadge(counters.researchErrors);

  if (!summary && !receipt && !note && !showCounters) {
    if (latestRun) {
      return (
        <div className="rounded-lg border border-white/10 bg-black/20 px-3 py-2 text-sm hc-soft">
          <p>{de.autoresearch.lastRunFallback(latestRun.scanned, latestRun.proposed)}</p>
          {latestRun.proposed === 0 ? <p className="mt-1 text-xs hc-dim">{de.autoresearch.lastRunZeroHint}</p> : null}
        </div>
      );
    }
    return <p className="text-sm hc-soft">{de.autoresearch.lastRunEmpty}</p>;
  }
  return (
    <div className="rounded-lg border border-white/10 bg-black/20 px-3 py-2 text-sm hc-soft">
      <p><span className="text-white">Letzter Lauf:</span> {summary || "Backend liefert nur Statusnotiz"}</p>
      {proposed !== null || kept !== null || reverted !== null ? <p className="mt-1 hc-mono">proposed={proposed ?? "?"} · übernommen={kept ?? "?"} · zurückgerollt={reverted ?? "?"}</p> : null}
      {showCounters ? (
        <p className="mt-1 flex flex-wrap items-center gap-x-2 gap-y-1 hc-mono">
          <span>{de.autoresearch.skillsResearched}={counters.skillsResearched ?? "?"} · {de.autoresearch.researchErrors}={counters.researchErrors ?? "?"} · {de.autoresearch.skillsWithFindings}={counters.skillsWithFindings ?? "?"}</span>
          {showErrorBadge ? <span className="rounded-full border border-red-500/40 bg-red-500/15 px-2 py-0.5 text-xs text-red-200">{de.autoresearch.researchErrorBadge}</span> : null}
        </p>
      ) : null}
      <p className="mt-1 hc-mono">{de.autoresearch.researchTokens}: {formatResearchTokens(counters.researchTokens)}</p>
      {showCounters ? <p className="mt-1 text-xs hc-dim">{de.autoresearch.counterLegend}</p> : null}
      {refused ? <p className="mt-1">Abgelehnt: {refused}</p> : null}
      {stopped ? <p className="mt-1">{stopped}</p> : null}
      {receipt ? <p className="mt-1 truncate text-xs hc-dim" title={receipt}>Receipt: {receipt}</p> : null}
      {note ? <p className="mt-1">{note}</p> : null}
    </div>
  );
}

function Empty({ icon, text }: { icon: React.ReactNode; text: string }) {
  return <div className="hc-card flex items-center gap-3 p-4 text-sm hc-soft">{icon}<span>{text}</span></div>;
}

function RecentRuns({ runs }: { runs: AutoresearchRun[] }) {
  const totalTokens = sumRunTokens(runs);
  const recent = summarizeRecentRuns(runs, 7);
  return (
    <section className="hc-card p-4">
      <div className="mb-3 flex items-start justify-between gap-3">
        <div>
          <h2 className="text-base font-semibold text-white">{de.autoresearch.recentRuns}</h2>
          <p className="mt-1 text-xs hc-soft" title={de.autoresearch.roi7dAcceptedNote}>
            <span className="text-white">{de.autoresearch.roi7dHeading}:</span>{" "}
            {recent.runs > 0 ? de.autoresearch.roi7dLine(recent.runs, recent.tokens, recent.proposed, recent.scanned) : de.autoresearch.roi7dEmpty}
          </p>
        </div>
        {totalTokens > 0 ? <span className="hc-mono text-xs hc-soft">{de.autoresearch.runsTokensTotal}: {totalTokens.toLocaleString("de-DE")}</span> : null}
      </div>
      {runs.length === 0 ? (
        <p className="text-sm hc-soft">{de.autoresearch.recentRunsEmpty}</p>
      ) : (
        <div className="overflow-x-auto">
          <table className="w-full text-left text-sm">
            <thead className="hc-dim">
              <tr className="border-b border-white/10">
                <th className="py-1 pr-3 font-medium">{de.autoresearch.runsColTime}</th>
                <th className="py-1 pr-3 font-medium">{de.autoresearch.runsColLane}</th>
                <th className="py-1 pr-3 text-right font-medium">{de.autoresearch.runsColTokens}</th>
                <th className="py-1 pr-3 text-right font-medium">{de.autoresearch.runsColProposed}</th>
                <th className="py-1 pr-3 text-right font-medium">{de.autoresearch.runsColScanned}</th>
                <th className="py-1 text-right font-medium">{de.autoresearch.runsColErrors}</th>
              </tr>
            </thead>
            <tbody className="hc-soft">
              {runs.map((run, i) => (
                <tr key={`${run.at}-${run.request_id ?? i}`} className="border-b border-white/5 last:border-0">
                  <td className="py-1 pr-3 hc-mono text-xs">{formatRunTime(run.at)}</td>
                  <td className="py-1 pr-3"><StatusPill tone={runLaneTone(run.lane)} label={runLaneLabel(run.lane)} /></td>
                  <td className="py-1 pr-3 text-right hc-mono">{run.tokens ? run.tokens.toLocaleString("de-DE") : "—"}</td>
                  <td className="py-1 pr-3 text-right hc-mono">{run.proposed}</td>
                  <td className="py-1 pr-3 text-right hc-mono">{run.scanned}</td>
                  <td className={cn("py-1 text-right hc-mono", run.errors > 0 ? "text-red-300" : "")}>{run.errors}</td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      )}
    </section>
  );
}
