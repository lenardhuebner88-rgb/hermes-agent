import { useEffect, useMemo, useState } from "react";
import {
  Check,
  ClipboardCopy,
  TriangleAlert,
  Workflow,
} from "lucide-react";
import { Button } from "@nous-research/ui/ui/components/button";
import { Spinner } from "@nous-research/ui/ui/components/spinner";

import { cn } from "@/lib/utils";
import {
  useHermesRecentResults,
  useHermesWorkers,
  useMetricsLite,
  useOrchestrationBacklog,
  useOrchestrationBacklogDetail,
  useProposals,
  useSystemHealth,
} from "../hooks/useControlData";
import type { Density } from "../hooks/useDensity";
import {
  buildAgentOpsDispatchPrompt,
  buildAgentOpsSnapshot,
  buildFourAgentLaunchBrief,
  buildMorningBrief,
  type DispatchCandidate,
  type ProjectLane,
} from "../lib/agentOps";
import { fmtAge, fmtDur, nowSec } from "../lib/derive";
import { buildCommissionPrompt } from "../lib/orchestration";
import type { KanbanResult, ToneName } from "../lib/types";
import type { DotKind } from "../lib/tones";
import { FleetPanel, FleetEmptyState, KpiTile, SignalChip, SignalLabel, signalToneFromLegacy, type SignalTone } from "../components/leitstand";
import { Eyebrow } from "../components/primitives";
import { SystemHealthStrip } from "../components/SystemHealthStrip";

function toneBorder(tone: ToneName): string {
  const signal = signalToneFromLegacy(tone);
  if (signal === "ok") return "border-status-ok/30 bg-status-ok/10";
  if (signal === "warn") return "border-status-warn/30 bg-status-warn/10";
  if (signal === "alert") return "border-status-alert/30 bg-status-alert/10";
  return "border-line bg-surface-2";
}

function statusTone(status: string): SignalTone {
  if (status === "healthy") return "ok";
  if (status === "degraded" || status === "unknown") return "warn";
  return "alert";
}

function clockLabel(epochSec: number): string {
  return new Date(epochSec * 1000).toLocaleTimeString("de-DE", { hour: "2-digit", minute: "2-digit" });
}

function readinessTone(ok: boolean, warn: boolean): DotKind {
  if (ok) return "ready";
  return warn ? "warn" : "error";
}

function CopyButton({ text, label, copiedLabel = "Kopiert" }: { text: string; label: string; copiedLabel?: string }) {
  const [copied, setCopied] = useState(false);
  const copy = async () => {
    try {
      if (navigator.clipboard?.writeText) {
        await navigator.clipboard.writeText(text);
      } else {
        const target = document.createElement("textarea");
        target.value = text;
        target.setAttribute("readonly", "true");
        target.style.position = "fixed";
        target.style.left = "-9999px";
        target.style.top = "0";
        document.body.appendChild(target);
        target.select();
        const ok = document.execCommand("copy");
        document.body.removeChild(target);
        if (!ok) throw new Error("Clipboard fallback failed");
      }
      setCopied(true);
      window.setTimeout(() => setCopied(false), 1600);
    } catch {
      /* clipboard blocked */
    }
  };

  return (
    <Button
      outlined
      size="sm"
      className="min-h-12"
      onClick={copy}
      prefix={copied ? <Check className="h-4 w-4" /> : <ClipboardCopy className="h-4 w-4" />}
    >
      {copied ? copiedLabel : label}
    </Button>
  );
}

function CandidateCard({
  candidate,
  prompt,
}: {
  candidate: DispatchCandidate;
  prompt: string;
}) {
  return (
    <article className="rounded-card border border-line bg-surface-2 p-3">
      <div className="flex items-start justify-between gap-3">
        <div className="min-w-0">
          <div className="flex flex-wrap items-center gap-2">
            <SignalChip tone={signalToneFromLegacy(candidate.tone)} label={candidate.action} />
            <span className="text-micro text-ink-2">{candidate.priority}</span>
            <span className="text-micro text-ink-2">{candidate.project}</span>
          </div>
          <h3 className="mt-2 line-clamp-2 text-sec font-semibold leading-snug text-ink">{candidate.title}</h3>
          <p className="mt-1 truncate font-data text-micro text-ink-3">{candidate.id} · {candidate.owner}</p>
        </div>
        <CopyButton text={prompt} label="Prompt" />
      </div>
    </article>
  );
}

function ProjectLaneRow({ lane }: { lane: ProjectLane }) {
  const pressure = lane.blocked + lane.highRisk + lane.staleProof;
  return (
    <tr className="border-t border-line align-top">
      <td className="px-3 py-2">
        <div className="min-w-0">
          <p className="truncate text-sec font-medium text-ink">{lane.project}</p>
          <p className="mt-0.5 text-micro text-ink-3"><span className="font-data tabular-nums text-ink-2">{lane.activeItems}</span> Tasks · <span className="font-data tabular-nums text-ink-2">{lane.activeWorkers}</span> Worker</p>
        </div>
      </td>
      <td className="px-3 py-2"><span className="font-data text-sec tabular-nums text-status-ok">{lane.ready}</span></td>
      <td className="px-3 py-2"><span className={cn("font-data text-sec tabular-nums", lane.blocked ? "text-status-alert" : "text-ink-3")}>{lane.blocked}</span></td>
      <td className="hidden px-3 py-2 md:table-cell"><span className="font-data text-sec tabular-nums text-ink">{lane.doing}</span></td>
      <td className="hidden px-3 py-2 md:table-cell"><span className="font-data text-sec tabular-nums text-ink">{lane.review}</span></td>
      <td className="hidden px-3 py-2 lg:table-cell"><span className={cn("font-data text-sec tabular-nums", pressure ? "text-status-warn" : "text-ink-3")}>{pressure}</span></td>
      <td className="px-3 py-2"><p className="line-clamp-2 text-sec text-ink">{lane.nextAction}</p></td>
    </tr>
  );
}

function RecentResultRow({ result, now }: { result: KanbanResult; now: number }) {
  return (
    <article className="rounded-card border border-line bg-surface-2 p-3">
      <div className="flex flex-wrap items-center gap-2">
        <SignalChip tone="ok" label="Done" />
        <span className="font-data text-sec tabular-nums text-ink-2">{fmtDur(result.duration_seconds)} · vor {fmtAge(result.ended_at, now)}</span>
        <a
          href={`/control/runs/${result.run_id}`}
          className="ml-auto inline-flex min-h-12 items-center text-sec text-live underline-offset-2 hover:text-bronze-hi hover:underline"
        >
          Timeline
        </a>
      </div>
      <h3 className="mt-2 line-clamp-2 text-sec font-semibold text-ink">{result.task_title}</h3>
      {result.summary_preview || result.summary ? (
        <p className="mt-2 line-clamp-3 text-sec text-ink-2">{result.summary_preview || result.summary}</p>
      ) : null}
      {result.residual_risk ? <div className="mt-2"><SignalLabel tone="warn" label={`Restrisiko: ${result.residual_risk}`} /></div> : null}
    </article>
  );
}

export function AgentOpsView({ density }: { density: Density }) {
  const workers = useHermesWorkers();
  const results = useHermesRecentResults();
  const health = useSystemHealth();
  const metrics = useMetricsLite();
  const proposals = useProposals();
  const backlog = useOrchestrationBacklog();
  const details = useOrchestrationBacklogDetail();
  const now = nowSec();

  const snapshot = useMemo(() => buildAgentOpsSnapshot({
    workers: workers.data?.workers ?? [],
    results: results.data?.results ?? [],
    proposals: proposals.proposals,
    orchestrationItems: backlog.data?.items ?? [],
    contractHealth: backlog.data?.contract_health,
    systemHealth: health.data,
    metrics: metrics.data,
    nowSec: backlog.data?.checked_at ?? now,
  }), [
    workers.data,
    results.data,
    proposals.proposals,
    backlog.data,
    health.data,
    metrics.data,
    now,
  ]);

  const candidateIds = snapshot.dispatchCandidates.map((candidate) => candidate.id).join("|");
  useEffect(() => {
    for (const candidate of snapshot.dispatchCandidates) {
      if (!details.detailById[candidate.id]) void details.fetch(candidate.id);
    }
  // detailById changes after every fetch; candidateIds is the stable trigger.
  // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [candidateIds, details.fetch]);

  const brief = useMemo(() => buildMorningBrief(snapshot), [snapshot]);
  const startBrief = useMemo(() => buildFourAgentLaunchBrief(snapshot), [snapshot]);
  const loading = workers.loading || results.loading || proposals.loading || backlog.loading || health.loading || metrics.loading;
  const sourceErrors = [
    workers.error ? `Worker: ${workers.error}` : "",
    results.error ? `Results: ${results.error}` : "",
    proposals.error ? `Autoresearch: ${proposals.error}` : "",
    backlog.error ? `Orchestrator: ${backlog.error}` : "",
    health.error ? `Health: ${health.error}` : "",
    metrics.error ? `Metrics: ${metrics.error}` : "",
  ].filter(Boolean);
  const gridCols = density === "compact" ? "xl:grid-cols-4" : "lg:grid-cols-4";
  const proofRate = snapshot.completedRuns > 0 ? snapshot.verifiedResults / snapshot.completedRuns : 1;
  const gateTotal = snapshot.gatePassed + snapshot.gateFailed;
  const parallelReady = snapshot.operatorDecision.kind === "launch";

  return (
    <div className="space-y-5">
      <SystemHealthStrip data={health.data} error={health.error} now={now} metrics={metrics.data} />

      <section className="flex flex-col gap-3 rounded-panel border border-line bg-surface-1 p-4 sm:flex-row sm:items-end sm:justify-between">
        <div className="min-w-0">
          <Eyebrow>Agenten-Leitstand</Eyebrow>
          <h2 className="mt-1 font-display text-h2 font-semibold text-ink">Arbeitsstroeme</h2>
          <p className="mt-1 text-sec text-ink-2">
            {snapshot.dispatchReady} sofort beauftragbar · {snapshot.activeWorkers} aktiv · Stand {clockLabel(snapshot.checkedAt)}
          </p>
        </div>
        <div className="flex flex-wrap items-center gap-2">
          {loading ? <span className="inline-flex items-center gap-2 text-sec text-ink-2"><Spinner />Aktualisiert</span> : null}
          <SignalChip tone={statusTone(snapshot.systemStatus)} label={snapshot.systemStatus} />
          <CopyButton text={startBrief} label="4-Agenten Brief" />
          <CopyButton text={brief} label="Brief" />
        </div>
      </section>

      {sourceErrors.length ? (
        <div className="flex items-start gap-2 rounded-card border border-status-warn/30 bg-status-warn/10 px-3 py-2 text-sec text-status-warn"><TriangleAlert aria-hidden className="mt-0.5 size-4 shrink-0" />{sourceErrors.join(" · ")}</div>
      ) : null}

      <section className={cn("grid gap-3 grid-cols-2 sm:grid-cols-4", gridCols)}>
        <KpiTile label="Worker gesund" value={`${snapshot.healthyWorkers}/${snapshot.activeWorkers}`} delta={`${snapshot.parallelSlotsFree}/${snapshot.parallelTarget} Slots frei`} dot={snapshot.healthyWorkers === snapshot.activeWorkers ? "ready" : "warn"} />
        <KpiTile label="Start jetzt" value={String(snapshot.recommendedLaunches)} delta={`${snapshot.dispatchReady} ready · ${snapshot.planGates} Gates`} dot={snapshot.recommendedLaunches ? "ready" : "idle"} />
        <KpiTile label="Blocker/Drift" value={`${snapshot.blockedItems}/${snapshot.contractDrift}`} delta={`${snapshot.staleProofItems} stale proof`} dot={snapshot.blockedItems || snapshot.contractDrift ? "warn" : "ready"} />
        <KpiTile label="Proof Gate" value={`${Math.round(snapshot.gatePassRate * 100)}%`} delta={`${snapshot.verifiedResults}/${snapshot.completedRuns} Receipts`} dot={snapshot.gatePassRate < 0.8 || snapshot.gateFailed ? "warn" : "ready"} />
      </section>

      <section className={cn("rounded-card border p-4", toneBorder(snapshot.operatorDecision.tone))}>
        <div className="flex flex-col gap-3 sm:flex-row sm:items-start sm:justify-between">
          <div className="min-w-0">
            <Eyebrow>Naechster Operator-Schritt</Eyebrow>
            <h3 className="mt-1 font-display text-h2 font-semibold text-ink">{snapshot.operatorDecision.title}</h3>
            <p className="mt-2 max-w-3xl text-sec text-ink-2">{snapshot.operatorDecision.detail}</p>
          </div>
          <div className="flex shrink-0 flex-wrap items-center gap-2">
            <SignalChip tone={signalToneFromLegacy(snapshot.operatorDecision.tone)} label={snapshot.operatorDecision.kind} />
            <CopyButton text={startBrief} label="Startbrief" />
          </div>
        </div>
      </section>

      <section className="grid gap-3 xl:grid-cols-[minmax(0,.9fr)_minmax(0,1.1fr)]">
        <FleetPanel
          eyebrow="Parallel Session Safety"
          meta={parallelReady ? "Gruen fuer kontrollierten Fan-out" : "Erst Leitplanken klaeren"}
        >
          <div className="mb-3 flex justify-end">
            <SignalChip tone={parallelReady ? "ok" : "warn"} label={parallelReady ? "ready" : "inspect"} />
          </div>
          <div className="grid gap-2 sm:grid-cols-2">
            <KpiTile label="Isolation" value={snapshot.parallelSlotsFree > 0 ? "Kapazitaet" : "voll"} dot={readinessTone(snapshot.parallelSlotsFree > 0, snapshot.activeWorkers < snapshot.parallelTarget + 2)} delta="Neue Workstreams nur mit eigenem Branch/Worktree oder sauber bestaetigtem exklusivem Root starten." />
            <KpiTile label="Locks" value={snapshot.healthyWorkers === snapshot.activeWorkers ? "stabil" : "prüfen"} dot={snapshot.healthyWorkers === snapshot.activeWorkers ? "ready" : "warn"} delta="Worker mit altem Heartbeat, ablaufender Lease oder fremder Dirty-Arbeit zuerst untersuchen." />
            <KpiTile label="Backlog" value={`${snapshot.dispatchReady} ready`} dot={snapshot.dispatchReady ? "ready" : "idle"} delta="Dispatch nur fuer ready Tasks ohne offene Dependencies; Plan-Gates bleiben Entscheidungsarbeit." />
            <KpiTile label="Risk" value={`${snapshot.highRiskItems} high`} dot={snapshot.highRiskItems > 3 || snapshot.blockedItems ? "warn" : "ready"} delta="Viele High-Risk- oder blockierte Tasks senken die sinnvolle Parallelitaet trotz freier Slots." />
          </div>
        </FleetPanel>

        <FleetPanel
          eyebrow="Harness & Proof"
          meta={`Evidenz vor Merge/Restart · ${Math.round(proofRate * 100)}% receipts`}
        >
          <div className="mb-3 flex justify-end">
            <SignalChip tone={proofRate >= 0.8 && snapshot.gateFailed === 0 ? "ok" : "warn"} label={`${Math.round(proofRate * 100)}% receipts`} />
          </div>
          <div className="grid gap-2 sm:grid-cols-2">
            <KpiTile label="Receipts" value={`${snapshot.verifiedResults}/${snapshot.completedRuns}`} dot={proofRate >= 0.8 ? "ready" : "warn"} delta="Letzte Ergebnisse mit expliziter Verifikation; Done ohne Proof bleibt Review-Arbeit." />
            <KpiTile label="Proposal Gates" value={`${snapshot.gatePassed}/${gateTotal || 0}`} dot={snapshot.gateFailed ? "error" : snapshot.gateRunning ? "live" : "ready"} delta={`${snapshot.gateRunning} laufend, ${snapshot.gateFailed} fehlgeschlagen; Autoresearch-Code nur nach gruenem Gate anwenden.`} />
            <KpiTile label="API Budget" value={`${(snapshot.errorRate * 100).toFixed(1)}%`} dot={snapshot.errorRate > 0.05 ? "error" : snapshot.worstP95Ms > 1000 ? "warn" : "ready"} delta={`p95 ${Math.round(snapshot.worstP95Ms)}ms; bei Druck keine zusaetzlichen Agentenwellen starten.`} />
            <KpiTile label="Operator Load" value={`${snapshot.openProposals} offen`} dot={snapshot.openProposals > 6 || snapshot.unownedItems > 0 ? "warn" : "ready"} delta={`${snapshot.unownedItems} unowned Tasks; erst Queue klaeren, dann Fan-out erhoehen.`} />
          </div>
        </FleetPanel>
      </section>

      <FleetPanel
        eyebrow="Readiness-Luecken"
        meta="Was parallele Arbeit gerade begrenzt"
      >
        <div className="mb-3 flex justify-end">
          <CopyButton text={startBrief} label="Startbrief" />
        </div>
        {snapshot.readinessGaps.length === 0 ? (
          <FleetEmptyState ok title="Keine begrenzenden Signale." desc="Parallele Arbeit ist gerade unblockiert." />
        ) : (
          <div className="grid gap-2 md:grid-cols-2 xl:grid-cols-4">
            {snapshot.readinessGaps.map((gap) => (
              <a key={gap.id} href={gap.target} className="flex min-h-12 flex-col justify-center rounded-card border border-line bg-surface-2 px-3 py-2 hover:border-live hover:bg-surface-3">
                <div className="flex items-center justify-between gap-3">
                  <SignalLabel tone={signalToneFromLegacy(gap.tone)} label={gap.label} />
                  <span className="font-data text-sec tabular-nums text-ink">{gap.count}</span>
                </div>
                <p className="mt-1 line-clamp-2 text-sec text-ink-2">{gap.detail}</p>
              </a>
            ))}
          </div>
        )}
      </FleetPanel>

      <section className="grid gap-3 xl:grid-cols-[minmax(0,1.2fr)_minmax(320px,.8fr)]">
        <FleetPanel
          eyebrow="Parallel Dispatch"
          meta="Naechste 4 Arbeitsstroeme"
        >
          <div className="mb-3 flex justify-end">
            <a href="/control/orchestrator" className="inline-flex min-h-12 items-center rounded-card border border-line px-3 py-1.5 text-sec text-live hover:border-live hover:bg-live/10">Orchestrator</a>
          </div>
          {snapshot.dispatchCandidates.length === 0 ? (
            <FleetEmptyState title="Keine beauftragbaren Kandidaten." desc="Der Dispatch hat derzeit keinen nächsten Auftrag." />
          ) : (
            <div className="grid gap-3 md:grid-cols-2">
              {snapshot.dispatchCandidates.map((candidate) => {
                const detail = details.detailById[candidate.id];
                const prompt = detail ? buildCommissionPrompt(detail) : buildAgentOpsDispatchPrompt(candidate.item);
                return <CandidateCard key={candidate.id} candidate={candidate} prompt={prompt} />;
              })}
            </div>
          )}
        </FleetPanel>

        <FleetPanel
          eyebrow="Interventionen"
          meta="Operator Queue"
        >
          {snapshot.interventions.length === 0 ? (
            <FleetEmptyState ok title="Keine Eingriffe offen." desc="Keine Tasks brauchen gerade einen Operator-Schritt." />
          ) : (
            <div className="space-y-2">
              {snapshot.interventions.map((item) => (
                <a key={item.id} href={item.target} className="flex min-h-12 items-center justify-between gap-3 rounded-card border border-line bg-surface-2 px-3 py-2 text-sec hover:border-live hover:bg-surface-3">
                  <span className="min-w-0">
                    <SignalLabel tone={signalToneFromLegacy(item.tone)} label={item.title} />
                    <span className="block truncate text-sec text-ink-2">{item.detail}</span>
                  </span>
                  <Workflow className="h-4 w-4 shrink-0" />
                </a>
              ))}
            </div>
          )}
        </FleetPanel>
      </section>

      <FleetPanel
        eyebrow="Projekt-Lanes"
        meta="Kapazitaet, Risiko, naechster Schritt"
      >
        <div className="-mx-1 overflow-x-auto">
          <table className="w-full table-fixed text-left">
            <thead className="bg-surface-2 font-display text-micro uppercase tracking-wide text-ink-3">
              <tr>
                <th className="w-[28%] px-3 py-2">Projekt</th>
                <th className="w-[9%] px-3 py-2">Ready</th>
                <th className="w-[9%] px-3 py-2">Blocked</th>
                <th className="hidden w-[9%] px-3 py-2 md:table-cell">Doing</th>
                <th className="hidden w-[9%] px-3 py-2 md:table-cell">Review</th>
                <th className="hidden w-[10%] px-3 py-2 lg:table-cell">Pressure</th>
                <th className="w-[26%] px-3 py-2">Next</th>
              </tr>
            </thead>
            <tbody>
              {snapshot.projectLanes.length === 0 ? (
                <tr><td colSpan={7} className="px-3 py-4 text-center text-sec text-ink-3">Keine Projekt-Lanes.</td></tr>
              ) : (
                snapshot.projectLanes.map((lane) => <ProjectLaneRow key={lane.project} lane={lane} />)
              )}
            </tbody>
          </table>
        </div>
      </FleetPanel>

      <FleetPanel
        eyebrow="Receipts"
        meta="Letzte Ergebnisse"
      >
        <div className="mb-3 flex justify-end">
          <a href="/control/fleet" className="inline-flex min-h-12 items-center rounded-card border border-line px-3 py-1.5 text-sec text-live hover:border-live hover:bg-live/10">Fleet</a>
        </div>
        {(results.data?.results ?? []).length === 0 ? (
          <FleetEmptyState title="Keine Ergebnisse im Zeitraum." desc="Noch keine abgeschlossenen Runs zu zeigen." />
        ) : (
          <div className="grid gap-3 md:grid-cols-2 xl:grid-cols-3">
            {(results.data?.results ?? []).slice(0, 6).map((result) => <RecentResultRow key={result.run_id} result={result} now={now} />)}
          </div>
        )}
      </FleetPanel>
    </div>
  );
}
