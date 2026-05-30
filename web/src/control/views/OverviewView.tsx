import { useState } from "react";
import { AlertTriangle, Bot, Check, ClipboardCopy, FlaskConical, Shield } from "lucide-react";
import { Button } from "@nous-research/ui/ui/components/button";
import { useNavigate } from "react-router-dom";
import { cn } from "@/lib/utils";
import { useHermesWorkers, useSystemHealth } from "../hooks/useControlData";
import { isActionable } from "../lib/autoresearch";
import { agentIsProblem, agentLabel, agentTone, buildOverview, freshness, nowSec, workerHealth } from "../lib/derive";
import { de } from "../i18n/de";
import type { AgentLive, Proposal } from "../lib/types";
import { StatusPill } from "../components/atoms";
import { SystemHealthStrip } from "../components/SystemHealthStrip";

type Focus = "hermes" | "openclaw" | "proposals" | "warnings" | null;

interface Props {
  proposals: Proposal[];
  agents: AgentLive[];
  proposalsLoading?: boolean;
  proposalsError?: string | null;
  proposalsLastUpdated?: number | null;
  agentsLastUpdated?: number | null;
  agentsError?: string | null;
}

export function OverviewView({ proposals, agents, proposalsLoading, proposalsError, proposalsLastUpdated, agentsLastUpdated, agentsError }: Props) {
  const navigate = useNavigate();
  const workers = useHermesWorkers();
  const health = useSystemHealth();
  const now = nowSec();
  const overview = buildOverview(workers.data?.workers ?? [], agents, proposals, now);
  const [focus, setFocus] = useState<Focus>(null);
  const [copied, setCopied] = useState(false);

  const title = overview.allHealthy ? de.overview.healthyTitle : de.overview.warnTitle(overview.warnings.length);
  const proposalValue = proposalsError ? "Fehler" : proposalsLoading && proposals.length === 0 ? "unbekannt" : String(overview.openProposals);

  // E1: per-source freshness. usePolling pausiert bei document.hidden → "stale"
  // ist hier eine Warnung (pausiert/veraltet), kein Fehler, kein stilles 0.
  const sources = [
    { label: de.overview.sourceHermes, fresh: freshness(workers.lastUpdated, 5000, now), err: workers.error },
    { label: de.overview.sourceOpenClaw, fresh: freshness(agentsLastUpdated ?? null, 5000, now), err: agentsError },
    { label: de.overview.sourceAutoresearch, fresh: freshness(proposalsLastUpdated ?? null, 6000, now), err: proposalsError },
  ];

  const openProposals = proposals.filter(isActionable);
  const problemWorkers = overview.warnings.filter((w) => w.kind === "hermes");
  const problemAgents = agents.filter(agentIsProblem);

  const toggle = (next: Focus) => setFocus((cur) => (cur === next ? null : next));

  const copyDiagnostics = async () => {
    const lines = [
      `Hermes Control — Diagnose (${freshness(now, 0, now).label})`,
      `${de.overview.sourceHermes}: ${overview.hermesRunning}/${overview.hermesTotal} laufen · ${sources[0].fresh.stale ? "stale" : "frisch"} (${sources[0].fresh.label})`,
      `${de.overview.sourceOpenClaw}: ${overview.ocActive}/${overview.ocTotal} aktiv · ${sources[1].fresh.stale ? "stale" : "frisch"} (${sources[1].fresh.label})`,
      `${de.overview.sourceAutoresearch}: ${overview.openProposals} offen · ${sources[2].fresh.stale ? "stale" : "frisch"} (${sources[2].fresh.label})`,
      `Warnungen (${overview.warnings.length}):`,
      ...overview.warnings.map((w) => w.kind === "hermes"
        ? `- [hermes] ${w.worker.task_title} — ${workerHealth(w.worker, now).label}`
        : `- [openclaw] ${w.agent.name} — ${agentLabel(w.agent)}`),
    ];
    try {
      await navigator.clipboard.writeText(lines.join("\n"));
      setCopied(true);
      window.setTimeout(() => setCopied(false), 1500);
    } catch {
      /* clipboard blocked (e.g. headless) — silent, non-critical */
    }
  };

  return (
    <div className="space-y-5">
      <SystemHealthStrip data={health.data} error={health.error} now={now} />

      <section className="hc-card p-5 sm:p-6">
        <div className="flex flex-col gap-4 lg:flex-row lg:items-end lg:justify-between">
          <div><p className="hc-eyebrow">System nominal</p><h2 className="mt-2 text-3xl font-semibold tracking-normal text-white">{title}</h2><p className="mt-2 max-w-2xl hc-soft">Hermes-Worker, OpenClaw-Agenten und Autoresearch laufen hier in einer Sicht zusammen.</p></div>
          <div className="flex items-center gap-2">
            <Button outlined size="sm" onClick={copyDiagnostics} className="gap-2">{copied ? <Check className="h-4 w-4" /> : <ClipboardCopy className="h-4 w-4" />}{copied ? de.overview.copied : de.overview.copyDiagnostics}</Button>
            <StatusPill tone={overview.allHealthy ? "emerald" : "amber"} label={overview.allHealthy ? "Ruhig" : "Aufmerksamkeit"} dot={overview.allHealthy ? "live" : "warn"} size="md" />
          </div>
        </div>
      </section>

      {/* E1: Datenfrische je Quelle */}
      <section className="hc-card p-4">
        <p className="hc-eyebrow mb-2">{de.overview.sources}</p>
        <div className="grid gap-2 sm:grid-cols-3">
          {sources.map((s) => (
            <div key={s.label} className={cn("flex items-center justify-between rounded-lg border px-3 py-2 text-sm", s.err || s.fresh.stale ? "border-amber-500/30 bg-amber-500/5" : "border-white/10")}>
              <span className="hc-soft">{s.label}</span>
              <span className={cn("hc-mono text-xs", s.err || s.fresh.stale ? "text-amber-200" : "hc-dim")} title={s.err ?? (s.fresh.stale ? de.overview.stalePaused : undefined)}>
                {s.err ? "Fehler" : s.fresh.stale ? de.overview.staleWarn(s.fresh.label.replace("vor ", "")) : s.fresh.label}
              </span>
            </div>
          ))}
        </div>
      </section>

      {/* E2: Kacheln sind Drilldown-Toggles */}
      <section className="grid gap-3 md:grid-cols-4">
        <Tile icon={<Bot />} label="Hermes laufen" value={`${overview.hermesRunning}/${overview.hermesTotal}`} active={focus === "hermes"} onClick={() => toggle("hermes")} />
        <Tile icon={<Shield />} label="OpenClaw aktiv" value={`${overview.ocActive}/${overview.ocTotal}`} active={focus === "openclaw"} onClick={() => toggle("openclaw")} />
        <Tile icon={<FlaskConical />} label={de.overview.proposals} value={proposalValue} active={focus === "proposals"} onClick={() => toggle("proposals")} />
        <Tile icon={<AlertTriangle />} label={de.overview.warnings} value={String(overview.warnings.length)} active={focus === "warnings"} onClick={() => toggle("warnings")} />
      </section>

      {focus === "hermes" ? (
        <Drilldown title="Hermes-Worker" tab="/control/hermes" navigate={navigate} empty={de.overview.noProblemWorkers} items={problemWorkers.map((w) => ({ key: w.worker.run_id, label: w.worker.task_title, pill: <StatusPill tone={workerHealth(w.worker, now).tone} label={workerHealth(w.worker, now).label} dot={workerHealth(w.worker, now).dot} /> }))} />
      ) : null}
      {focus === "openclaw" ? (
        <Drilldown title="OpenClaw-Agenten" tab="/control/openclaw" navigate={navigate} empty={de.overview.noProblemAgents} items={problemAgents.map((a) => ({ key: a.id, label: `${a.emoji} ${a.name}`, pill: <StatusPill tone={agentTone(a)} label={agentLabel(a)} dot="warn" /> }))} />
      ) : null}
      {focus === "proposals" ? (
        <Drilldown title={de.overview.proposals} tab="/control/autoresearch" navigate={navigate} empty={de.overview.noOpenProposals} items={openProposals.map((p) => ({ key: p.id, label: p.title ?? p.target, pill: <StatusPill tone={p.mode === "code" ? "violet" : "cyan"} label={p.mode === "code" ? "Code" : "Skill"} /> }))} />
      ) : null}

      {/* Default-Panel: Braucht-Aufmerksamkeit (auch sichtbar bei focus="warnings") */}
      {focus === null || focus === "warnings" ? (
        <section className="hc-card p-4">
          <h3 className="mb-3 text-lg font-semibold text-white">{de.overview.needsAttention}</h3>
          {overview.warnings.length === 0 ? <p className="text-sm hc-soft">{de.overview.nothingUrgent}</p> : <div className="space-y-2">{overview.warnings.map((warning) => warning.kind === "hermes" ? <button key={warning.worker.run_id} type="button" onClick={() => navigate("/control/hermes")} className="flex w-full items-center justify-between rounded-lg border border-white/10 px-3 py-2 text-left text-sm hover:bg-white/5"><span>{warning.worker.task_title}</span><StatusPill tone={workerHealth(warning.worker, now).tone} label={workerHealth(warning.worker, now).label} dot={workerHealth(warning.worker, now).dot} /></button> : <button key={warning.agent.id} type="button" onClick={() => navigate("/control/openclaw")} className="flex w-full items-center justify-between rounded-lg border border-white/10 px-3 py-2 text-left text-sm hover:bg-white/5"><span>{warning.agent.emoji} {warning.agent.name}: {warning.agent.escalationNote || agentLabel(warning.agent)}</span><StatusPill tone={agentTone(warning.agent)} label={agentLabel(warning.agent)} dot="warn" /></button>)}</div>}
        </section>
      ) : null}
    </div>
  );
}

function Drilldown({ title, tab, navigate, items, empty }: { title: string; tab: string; navigate: (path: string) => void; items: Array<{ key: string; label: string; pill: React.ReactNode }>; empty: string }) {
  return (
    <section className="hc-card p-4">
      <div className="mb-3 flex items-center justify-between">
        <h3 className="text-lg font-semibold text-white">{title}</h3>
        <Button ghost size="xs" onClick={() => navigate(tab)}>{de.overview.openTab}</Button>
      </div>
      {items.length === 0 ? <p className="text-sm hc-soft">{empty}</p> : (
        <div className="space-y-2">
          {items.map((it) => (
            <button key={it.key} type="button" onClick={() => navigate(tab)} className="flex w-full items-center justify-between rounded-lg border border-white/10 px-3 py-2 text-left text-sm hover:bg-white/5">
              <span className="min-w-0 truncate">{it.label}</span>{it.pill}
            </button>
          ))}
        </div>
      )}
    </section>
  );
}

function Tile({ icon, label, value, active, onClick }: { icon: React.ReactNode; label: string; value: string; active?: boolean; onClick: () => void }) {
  return <Button outlined onClick={onClick} aria-pressed={active} className={cn("hc-card hc-hit flex h-auto items-center justify-start gap-3 p-4 text-left", active && "ring-1 ring-[var(--hc-accent-border)]")}><span className="text-[var(--hc-accent-text)]">{icon}</span><span><span className="block text-sm hc-soft">{label}</span><span className="block text-2xl font-semibold text-white">{value}</span></span></Button>;
}
