import { useEffect, useMemo, useState } from "react";
import {
  AlertTriangle,
  Bot,
  Check,
  ClipboardCopy,
  GitBranch,
  ShieldCheck,
  Workflow,
  Zap,
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
import { StatusPill, ToneCallout } from "../components/atoms";
import { SystemHealthStrip } from "../components/SystemHealthStrip";

function toneBorder(tone: ToneName): string {
  return {
    emerald: "border-emerald-500/25 bg-emerald-500/10",
    cyan: "border-cyan-500/25 bg-cyan-500/10",
    sky: "border-sky-500/25 bg-sky-500/10",
    indigo: "border-indigo-400/25 bg-indigo-400/10",
    amber: "border-amber-500/25 bg-amber-500/10",
    rose: "border-rose-500/25 bg-rose-500/10",
    red: "border-red-500/25 bg-red-500/10",
    zinc: "border-zinc-600/25 bg-zinc-600/10",
    violet: "border-[var(--hc-accent-border)] bg-[var(--hc-accent-wash)]",
  }[tone];
}

function statusTone(status: string): ToneName {
  if (status === "healthy") return "emerald";
  if (status === "degraded" || status === "unknown") return "amber";
  return "red";
}

function clockLabel(epochSec: number): string {
  return new Date(epochSec * 1000).toLocaleTimeString("de-DE", { hour: "2-digit", minute: "2-digit" });
}

function Tile({ icon, label, value, tone = "cyan", sub }: { icon: React.ReactNode; label: string; value: string; tone?: ToneName; sub?: string }) {
  return (
    <div className={cn("min-w-0 rounded-lg border px-3 py-3", toneBorder(tone))}>
      <div className="flex items-center justify-between gap-3">
        <span className="text-zinc-200">{icon}</span>
        <span className="hc-mono text-lg font-semibold text-white">{value}</span>
      </div>
      <p className="mt-2 truncate text-xs font-medium uppercase tracking-normal hc-dim">{label}</p>
      {sub ? <p className="mt-1 truncate text-xs hc-soft">{sub}</p> : null}
    </div>
  );
}

function readinessTone(ok: boolean, warn: boolean): ToneName {
  if (ok) return "emerald";
  return warn ? "amber" : "red";
}

function SignalRow({ label, value, tone, detail }: { label: string; value: string; tone: ToneName; detail: string }) {
  return (
    <div className={cn("rounded-lg border px-3 py-2", toneBorder(tone))}>
      <div className="flex items-center justify-between gap-3">
        <span className="truncate text-sm font-medium text-white">{label}</span>
        <span className="hc-mono text-sm text-zinc-100">{value}</span>
      </div>
      <p className="mt-1 line-clamp-2 text-xs hc-soft">{detail}</p>
    </div>
  );
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
    <article className="rounded-lg border border-[var(--hc-border)] bg-white/[.025] p-3">
      <div className="flex items-start justify-between gap-3">
        <div className="min-w-0">
          <div className="flex flex-wrap items-center gap-2">
            <StatusPill tone={candidate.tone} label={candidate.action} />
            <span className="rounded-full border border-white/10 px-2 py-0.5 text-xs hc-soft">{candidate.priority}</span>
            <span className="rounded-full border border-white/10 px-2 py-0.5 text-xs hc-soft">{candidate.project}</span>
          </div>
          <h3 className="mt-2 line-clamp-2 text-sm font-semibold leading-snug text-white">{candidate.title}</h3>
          <p className="mt-1 truncate hc-mono text-[11px] hc-dim">{candidate.id} · {candidate.owner}</p>
        </div>
        <CopyButton text={prompt} label="Prompt" />
      </div>
    </article>
  );
}

function ProjectLaneRow({ lane }: { lane: ProjectLane }) {
  const pressure = lane.blocked + lane.highRisk + lane.staleProof;
  return (
    <tr className="border-t border-[var(--hc-border)] align-top">
      <td className="px-3 py-2">
        <div className="min-w-0">
          <p className="truncate text-sm font-medium text-white">{lane.project}</p>
          <p className="mt-0.5 text-[11px] hc-mono hc-dim">{lane.activeItems} Tasks · {lane.activeWorkers} Worker</p>
        </div>
      </td>
      <td className="px-3 py-2"><span className="hc-mono text-sm text-emerald-200">{lane.ready}</span></td>
      <td className="px-3 py-2"><span className={cn("hc-mono text-sm", lane.blocked ? "text-red-200" : "text-zinc-400")}>{lane.blocked}</span></td>
      <td className="hidden px-3 py-2 md:table-cell"><span className="hc-mono text-sm text-violet-200">{lane.doing}</span></td>
      <td className="hidden px-3 py-2 md:table-cell"><span className="hc-mono text-sm text-cyan-200">{lane.review}</span></td>
      <td className="hidden px-3 py-2 lg:table-cell"><span className={cn("hc-mono text-sm", pressure ? "text-amber-200" : "text-zinc-400")}>{pressure}</span></td>
      <td className="px-3 py-2"><p className="line-clamp-2 text-sm text-zinc-100">{lane.nextAction}</p></td>
    </tr>
  );
}

function RecentResultRow({ result, now }: { result: KanbanResult; now: number }) {
  return (
    <article className="rounded-lg border border-[var(--hc-border)] bg-white/[.025] p-3">
      <div className="flex flex-wrap items-center gap-2">
        <StatusPill tone="emerald" label="Done" dot="ready" />
        <span className="hc-mono text-xs hc-soft">{fmtDur(result.duration_seconds)} · vor {fmtAge(result.ended_at, now)}</span>
      </div>
      <h3 className="mt-2 line-clamp-2 text-sm font-semibold text-white">{result.task_title}</h3>
      {result.summary_preview || result.summary ? (
        <p className="mt-2 line-clamp-3 text-sm hc-soft">{result.summary_preview || result.summary}</p>
      ) : null}
      {result.residual_risk ? <p className="mt-2 text-xs text-amber-200">Restrisiko: {result.residual_risk}</p> : null}
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

      <section className="hc-card flex flex-col gap-3 p-4 sm:flex-row sm:items-end sm:justify-between">
        <div className="min-w-0">
          <p className="hc-eyebrow">Agenten-Leitstand</p>
          <h2 className="mt-1 text-xl font-semibold text-white">Arbeitsstroeme</h2>
          <p className="mt-1 text-sm hc-soft">
            {snapshot.dispatchReady} sofort beauftragbar · {snapshot.activeWorkers} aktiv · Stand {clockLabel(snapshot.checkedAt)}
          </p>
        </div>
        <div className="flex flex-wrap items-center gap-2">
          {loading ? <span className="inline-flex items-center gap-2 text-sm hc-soft"><Spinner />Aktualisiert</span> : null}
          <StatusPill tone={statusTone(snapshot.systemStatus)} label={snapshot.systemStatus} dot={snapshot.systemStatus === "healthy" ? "live" : "warn"} />
          <CopyButton text={startBrief} label="4-Agenten Brief" />
          <CopyButton text={brief} label="Brief" />
        </div>
      </section>

      {sourceErrors.length ? (
        <ToneCallout tone="amber">{sourceErrors.join(" · ")}</ToneCallout>
      ) : null}

      <section className={cn("grid gap-3 sm:grid-cols-2", gridCols)}>
        <Tile icon={<Bot className="h-5 w-5" />} label="Worker gesund" value={`${snapshot.healthyWorkers}/${snapshot.activeWorkers}`} tone={snapshot.healthyWorkers === snapshot.activeWorkers ? "emerald" : "amber"} sub={`${snapshot.parallelSlotsFree}/${snapshot.parallelTarget} Slots frei`} />
        <Tile icon={<Zap className="h-5 w-5" />} label="Start jetzt" value={String(snapshot.recommendedLaunches)} tone={snapshot.recommendedLaunches ? "emerald" : "zinc"} sub={`${snapshot.dispatchReady} ready | ${snapshot.planGates} Gates`} />
        <Tile icon={<AlertTriangle className="h-5 w-5" />} label="Blocker/Drift" value={`${snapshot.blockedItems}/${snapshot.contractDrift}`} tone={snapshot.blockedItems || snapshot.contractDrift ? "amber" : "emerald"} sub={`${snapshot.staleProofItems} stale proof`} />
        <Tile icon={<ShieldCheck className="h-5 w-5" />} label="Proof Gate" value={`${Math.round(snapshot.gatePassRate * 100)}%`} tone={snapshot.gatePassRate < 0.8 || snapshot.gateFailed ? "amber" : "emerald"} sub={`${snapshot.verifiedResults}/${snapshot.completedRuns} Receipts`} />
      </section>

      <section className={cn("rounded-lg border p-4", toneBorder(snapshot.operatorDecision.tone))}>
        <div className="flex flex-col gap-3 sm:flex-row sm:items-start sm:justify-between">
          <div className="min-w-0">
            <p className="hc-eyebrow">Naechster Operator-Schritt</p>
            <h3 className="mt-1 text-xl font-semibold text-white">{snapshot.operatorDecision.title}</h3>
            <p className="mt-2 max-w-3xl text-sm hc-soft">{snapshot.operatorDecision.detail}</p>
          </div>
          <div className="flex shrink-0 flex-wrap items-center gap-2">
            <StatusPill tone={snapshot.operatorDecision.tone} label={snapshot.operatorDecision.kind} dot={snapshot.operatorDecision.tone === "emerald" ? "ready" : "warn"} />
            <CopyButton text={startBrief} label="Startbrief" />
          </div>
        </div>
      </section>

      <section className="grid gap-4 xl:grid-cols-[minmax(0,.9fr)_minmax(0,1.1fr)]">
        <div className="hc-card p-4">
          <div className="flex items-start justify-between gap-3">
            <div>
              <p className="hc-eyebrow">Parallel Session Safety</p>
              <h3 className="mt-1 text-lg font-semibold text-white">{parallelReady ? "Gruen fuer kontrollierten Fan-out" : "Erst Leitplanken klaeren"}</h3>
            </div>
            <StatusPill tone={parallelReady ? "emerald" : "amber"} label={parallelReady ? "ready" : "inspect"} dot={parallelReady ? "ready" : "warn"} />
          </div>
          <div className="mt-3 grid gap-2 sm:grid-cols-2">
            <SignalRow label="Isolation" value={snapshot.parallelSlotsFree > 0 ? "Kapazitaet" : "voll"} tone={readinessTone(snapshot.parallelSlotsFree > 0, snapshot.activeWorkers < snapshot.parallelTarget + 2)} detail="Neue Workstreams nur mit eigenem Branch/Worktree oder sauber bestaetigtem exklusivem Root starten." />
            <SignalRow label="Locks" value={snapshot.healthyWorkers === snapshot.activeWorkers ? "stabil" : "prüfen"} tone={snapshot.healthyWorkers === snapshot.activeWorkers ? "emerald" : "amber"} detail="Worker mit altem Heartbeat, ablaufender Lease oder fremder Dirty-Arbeit zuerst untersuchen." />
            <SignalRow label="Backlog" value={`${snapshot.dispatchReady} ready`} tone={snapshot.dispatchReady ? "emerald" : "zinc"} detail="Dispatch nur fuer ready Tasks ohne offene Dependencies; Plan-Gates bleiben Entscheidungsarbeit." />
            <SignalRow label="Risk" value={`${snapshot.highRiskItems} high`} tone={snapshot.highRiskItems > 3 || snapshot.blockedItems ? "amber" : "emerald"} detail="Viele High-Risk- oder blockierte Tasks senken die sinnvolle Parallelitaet trotz freier Slots." />
          </div>
        </div>

        <div className="hc-card p-4">
          <div className="flex items-start justify-between gap-3">
            <div>
              <p className="hc-eyebrow">Harness & Proof</p>
              <h3 className="mt-1 text-lg font-semibold text-white">Evidenz vor Merge/Restart</h3>
            </div>
            <StatusPill tone={proofRate >= 0.8 && snapshot.gateFailed === 0 ? "emerald" : "amber"} label={`${Math.round(proofRate * 100)}% receipts`} />
          </div>
          <div className="mt-3 grid gap-2 sm:grid-cols-2">
            <SignalRow label="Receipts" value={`${snapshot.verifiedResults}/${snapshot.completedRuns}`} tone={proofRate >= 0.8 ? "emerald" : "amber"} detail="Letzte Ergebnisse mit expliziter Verifikation; Done ohne Proof bleibt Review-Arbeit." />
            <SignalRow label="Proposal Gates" value={`${snapshot.gatePassed}/${gateTotal || 0}`} tone={snapshot.gateFailed ? "red" : snapshot.gateRunning ? "cyan" : "emerald"} detail={`${snapshot.gateRunning} laufend, ${snapshot.gateFailed} fehlgeschlagen; Autoresearch-Code nur nach gruenem Gate anwenden.`} />
            <SignalRow label="API Budget" value={`${(snapshot.errorRate * 100).toFixed(1)}%`} tone={snapshot.errorRate > 0.05 ? "red" : snapshot.worstP95Ms > 1000 ? "amber" : "emerald"} detail={`p95 ${Math.round(snapshot.worstP95Ms)}ms; bei Druck keine zusaetzlichen Agentenwellen starten.`} />
            <SignalRow label="Operator Load" value={`${snapshot.openProposals} offen`} tone={snapshot.openProposals > 6 || snapshot.unownedItems > 0 ? "amber" : "emerald"} detail={`${snapshot.unownedItems} unowned Tasks; erst Queue klaeren, dann Fan-out erhoehen.`} />
          </div>
        </div>
      </section>

      <section className="hc-card p-4">
        <div className="flex flex-col gap-3 sm:flex-row sm:items-start sm:justify-between">
          <div>
            <p className="hc-eyebrow">Readiness-Luecken</p>
            <h3 className="mt-1 text-lg font-semibold text-white">Was parallele Arbeit gerade begrenzt</h3>
          </div>
          <CopyButton text={startBrief} label="Startbrief" />
        </div>
        {snapshot.readinessGaps.length === 0 ? (
          <div className="mt-3 rounded-lg border border-emerald-500/20 bg-emerald-500/10 p-3 text-sm text-emerald-100">Keine begrenzenden Signale.</div>
        ) : (
          <div className="mt-3 grid gap-2 md:grid-cols-2 xl:grid-cols-4">
            {snapshot.readinessGaps.map((gap) => (
              <a key={gap.id} href={gap.target} className={cn("rounded-lg border px-3 py-2 hover:bg-white/[.04]", toneBorder(gap.tone))}>
                <div className="flex items-center justify-between gap-3">
                  <span className="truncate text-sm font-medium text-white">{gap.label}</span>
                  <span className="hc-mono text-sm text-zinc-100">{gap.count}</span>
                </div>
                <p className="mt-1 line-clamp-2 text-xs hc-soft">{gap.detail}</p>
              </a>
            ))}
          </div>
        )}
      </section>

      <section className="grid gap-4 xl:grid-cols-[minmax(0,1.2fr)_minmax(320px,.8fr)]">
        <div className="space-y-3">
          <div className="flex items-end justify-between gap-3">
            <div>
              <p className="hc-eyebrow">Parallel Dispatch</p>
              <h3 className="mt-1 text-lg font-semibold text-white">Naechste 4 Arbeitsstroeme</h3>
            </div>
            <a href="/control/orchestrator" className="rounded-md border border-white/10 px-3 py-1.5 text-sm hc-soft hover:bg-white/5">Orchestrator</a>
          </div>
          {snapshot.dispatchCandidates.length === 0 ? (
            <div className="rounded-lg border border-[var(--hc-border)] bg-white/[.025] p-4 text-sm hc-soft">Keine beauftragbaren Kandidaten.</div>
          ) : (
            <div className="grid gap-3 md:grid-cols-2">
              {snapshot.dispatchCandidates.map((candidate) => {
                const detail = details.detailById[candidate.id];
                const prompt = detail ? buildCommissionPrompt(detail) : buildAgentOpsDispatchPrompt(candidate.item);
                return <CandidateCard key={candidate.id} candidate={candidate} prompt={prompt} />;
              })}
            </div>
          )}
        </div>

        <div className="space-y-3">
          <div>
            <p className="hc-eyebrow">Interventionen</p>
            <h3 className="mt-1 text-lg font-semibold text-white">Operator Queue</h3>
          </div>
          {snapshot.interventions.length === 0 ? (
            <div className="rounded-lg border border-emerald-500/20 bg-emerald-500/10 p-4 text-sm text-emerald-100">Keine Eingriffe offen.</div>
          ) : (
            <div className="space-y-2">
              {snapshot.interventions.map((item) => (
                <a key={item.id} href={item.target} className={cn("flex items-center justify-between gap-3 rounded-lg border px-3 py-2 text-sm", toneBorder(item.tone))}>
                  <span className="min-w-0">
                    <span className="block truncate font-medium text-white">{item.title}</span>
                    <span className="block truncate text-xs hc-soft">{item.detail}</span>
                  </span>
                  <Workflow className="h-4 w-4 shrink-0" />
                </a>
              ))}
            </div>
          )}
        </div>
      </section>

      <section className="hc-card overflow-hidden">
        <div className="flex items-center justify-between gap-3 border-b border-[var(--hc-border)] px-3 py-2">
          <div>
            <p className="hc-eyebrow">Projekt-Lanes</p>
            <h3 className="mt-1 text-sm font-semibold text-white">Kapazitaet, Risiko, naechster Schritt</h3>
          </div>
          <GitBranch className="h-4 w-4 hc-dim" />
        </div>
        <div className="overflow-x-auto">
          <table className="w-full table-fixed text-left">
            <thead className="bg-white/[.025] text-[10px] uppercase tracking-wide hc-dim">
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
                <tr><td colSpan={7} className="px-3 py-4 text-center text-sm hc-dim">Keine Projekt-Lanes.</td></tr>
              ) : (
                snapshot.projectLanes.map((lane) => <ProjectLaneRow key={lane.project} lane={lane} />)
              )}
            </tbody>
          </table>
        </div>
      </section>

      <section className="space-y-3">
        <div className="flex items-end justify-between gap-3">
          <div>
            <p className="hc-eyebrow">Receipts</p>
            <h3 className="mt-1 text-lg font-semibold text-white">Letzte Ergebnisse</h3>
          </div>
          <a href="/control/hermes" className="rounded-md border border-white/10 px-3 py-1.5 text-sm hc-soft hover:bg-white/5">Hermes</a>
        </div>
        {(results.data?.results ?? []).length === 0 ? (
          <div className="rounded-lg border border-[var(--hc-border)] bg-white/[.025] p-4 text-sm hc-soft">Keine Ergebnisse im Zeitraum.</div>
        ) : (
          <div className="grid gap-3 md:grid-cols-2 xl:grid-cols-3">
            {(results.data?.results ?? []).slice(0, 6).map((result) => <RecentResultRow key={result.run_id} result={result} now={now} />)}
          </div>
        )}
      </section>
    </div>
  );
}
