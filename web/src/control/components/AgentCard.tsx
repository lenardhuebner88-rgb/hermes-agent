import { AlertTriangle, Radio } from "lucide-react";
import { cn } from "@/lib/utils";
import { agentColorVar, priorityLabel, priorityTone } from "../lib/tones";
import { STUCK_HEARTBEAT_S, agentLabel, agentTone, fmtAge } from "../lib/derive";
import type { Density } from "../hooks/useDensity";
import type { AgentLive, AgentTask } from "../lib/types";
import { de } from "../i18n/de";
import { StatusPill, ToneCallout } from "./atoms";

interface Props {
  agent: AgentLive;
  density: Density;
  now: number;
  onOpenDrilldown?: () => void;
}

// MC marks each metric live | derived | fallback | unavailable. Anything but
// "live" means the value is a guess, not ground truth — flag it so the operator
// doesn't trust an estimated metric (E4 heartbeat → F1: all four metrics).
const TRUTH_LABEL: Record<string, string> = {
  derived: "abgeleitet", fallback: "geschätzt", unavailable: "unbekannt",
};
function truthHint(t?: string | null): string | null {
  return t && t !== "live" ? TRUTH_LABEL[t] ?? t : null;
}

export function AgentCard({ agent, density, now, onOpenDrilldown }: Props) {
  const tone = agentTone(agent);
  const colorVar = agentColorVar[agent.id] ?? "--hc-accent";
  const heartbeat = agent.fleetHealth.heartbeat ?? agent.lastActive;
  const heartbeatText = heartbeat ? fmtAge(heartbeat, now) : "-";
  const heartbeatAge = heartbeat ? now - heartbeat : null;
  const heartbeatStale = heartbeatAge != null && heartbeatAge > STUCK_HEARTBEAT_S;
  const lastActiveText = agent.lastActive ? fmtAge(agent.lastActive, now) : null;
  const problem = agent.stuckSignal || agent.status === "offline";
  const taskTruth = truthHint(agent.currentTaskTruth);
  const showDrilldownButton = Boolean(onOpenDrilldown && hasDrilldownContent(agent));

  return (
    <article className={cn("hc-card space-y-4 p-4", density === "compact" && "p-3", problem && "border-amber-500/35 shadow-[0_0_0_1px_rgba(245,158,11,.12)]")}>
      <div className="flex items-start justify-between gap-3">
        <div className="flex min-w-0 gap-3">
          <div className="grid h-12 w-12 shrink-0 place-items-center rounded-xl border text-2xl" style={{ borderColor: `color-mix(in srgb, var(${colorVar}) 45%, transparent)`, background: `color-mix(in srgb, var(${colorVar}) 18%, transparent)` }}>
            {agent.emoji}
          </div>
          <div className="min-w-0">
            <div className="flex flex-wrap items-center gap-2">
              <h3 className="text-base font-semibold leading-tight text-white">{agent.name}</h3>
              <span className="rounded-full border border-white/10 px-2 py-0.5 text-xs hc-soft">{agent.roleLabel}</span>
            </div>
            <p className="mt-1 line-clamp-2 text-sm hc-soft">{agent.roleSummary}</p>
            {lastActiveText ? <p className="mt-0.5 text-xs hc-dim">zuletzt aktiv vor {lastActiveText}{agent.load ? ` · ${agent.load} in Queue${agent.loadSource ? ` (${agent.loadSource})` : ""}` : ""}{agent.activityPulse > 0 ? ` · Puls ${agent.activityPulse}` : ""}</p> : null}
          </div>
        </div>
        <StatusPill tone={tone} label={agentLabel(agent)} dot={problem ? "warn" : agent.status === "ready" ? "ready" : agent.status === "idle" ? "idle" : "live"} />
      </div>

      <div className="rounded-lg border border-white/10 bg-white/[.03] p-3">
        <p className="text-xs hc-dim">Aktuelle Aufgabe</p>
        <p className="mt-1 line-clamp-2 text-sm font-medium text-white">{agent.fleetHealth.currentTask || "Keine aktive Aufgabe"}{taskTruth ? <span className="ml-1 text-[10px] font-normal hc-dim">({taskTruth})</span> : null}</p>
      </div>

      <div className="grid grid-cols-2 gap-3 text-sm md:grid-cols-4">
        <Metric label="Heartbeat" value={heartbeatText} sub={truthHint(agent.heartbeatTruth)} warn={problem || heartbeatStale} />
        <Metric label="Throughput" value={agent.fleetHealth.throughput || "0/h"} sub={truthHint(agent.throughputTruth)} />
        <Metric label="Tool" value={agent.fleetHealth.currentTool || "-"} sub={truthHint(agent.currentToolTruth)} />
        <Metric label="Modell" value={agent.model || "unbekannt"} />
      </div>

      <div className="grid grid-cols-4 gap-2">
        <QueueCounter label="Wartet" count={agent.tasks.queued.length} tasks={agent.tasks.queued} />
        <QueueCounter label="Aktiv" count={agent.tasks.active.length} tasks={agent.tasks.active} />
        <QueueCounter label="Review" count={agent.tasks.review.length} tasks={agent.tasks.review} />
        <QueueCounter label="Fertig" count={agent.tasks.recentDone.length} tasks={agent.tasks.recentDone} />
      </div>

      {problem ? <ToneCallout tone="amber"><AlertTriangle className="mr-2 inline h-4 w-4" />{agent.escalationNote || (agent.status === "offline" ? "Agent meldet sich nicht." : "Stuck-Signal aktiv.")}</ToneCallout> : null}

      {agent.fleetHealth.lastOutput ? (
        <div className="flex gap-2 rounded-lg border border-white/10 bg-black/20 px-3 py-2 text-xs hc-soft">
          <Radio className="mt-0.5 h-3.5 w-3.5 shrink-0" />
          <span className="line-clamp-2">{agent.fleetHealth.lastOutput}</span>
        </div>
      ) : null}

      {showDrilldownButton ? (
        <button
          type="button"
          aria-label={de.openclaw.drilldownOpen}
          className="w-full rounded-lg border border-white/10 bg-white/[.03] px-3 py-2 text-sm font-medium text-zinc-200 transition hover:bg-white/[.06] focus:outline-none focus:ring-2 focus:ring-[var(--hc-accent-border)]"
          onClick={(event) => {
            event.stopPropagation();
            onOpenDrilldown?.();
          }}
        >
          {de.openclaw.drilldownOpen}
        </button>
      ) : null}
    </article>
  );
}

function hasDrilldownContent(agent: AgentLive): boolean {
  const drilldown = agent.drilldown;
  if (!drilldown) return false;
  return Boolean(
    drilldown.highlights.length ||
    drilldown.decisions.length ||
    drilldown.timeline.length ||
    drilldown.artifacts.length ||
    drilldown.sources.length
  );
}

function Metric({ label, value, sub, warn }: { label: string; value: string; sub?: string | null; warn?: boolean }) {
  return <div className={cn("rounded-lg border border-white/10 bg-white/[.03] px-3 py-2", warn && "border-amber-500/30 bg-amber-500/10 text-amber-100")}><p className="text-xs hc-dim">{label}</p><p className="hc-mono truncate text-sm font-semibold">{value}{sub ? <span className="ml-1 text-[10px] font-normal hc-dim">({sub})</span> : null}</p></div>;
}

function QueueCounter({ label, count, tasks }: { label: string; count: number; tasks: AgentTask[] }) {
  const top = tasks[0];
  return (
    <div className="min-h-16 rounded-lg border border-white/10 bg-white/[.03] px-2 py-2 text-center" title={top?.title}>
      <p className="hc-mono text-lg font-semibold text-white">{count}</p>
      <p className="text-[11px] hc-soft">{label}</p>
      {top ? <p className={cn("mt-1 truncate rounded-full border px-1 text-[10px]", priorityTone[top.priority] === "rose" ? "border-rose-500/25 text-rose-200" : priorityTone[top.priority] === "amber" ? "border-amber-500/25 text-amber-200" : "border-zinc-600/25 text-zinc-300")}>{priorityLabel[top.priority]}</p> : null}
    </div>
  );
}
