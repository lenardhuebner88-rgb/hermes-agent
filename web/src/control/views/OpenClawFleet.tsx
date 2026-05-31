import { useEffect, useState } from "react";
import { Shield } from "lucide-react";
import { Spinner } from "@nous-research/ui/ui/components/spinner";
import { cn } from "@/lib/utils";
import { useOpenClawAgents, useOpenClawCronErrors, useOpenClawDispatched } from "../hooks/useControlData";
import { agentIsProblem, agentSortRank, fmtAge, nowSec, reconcileOpenClawFleet, type OpenClawFleetState } from "../lib/derive";
import { KEYMAP } from "../lib/keymap";
import { de } from "../i18n/de";
import type { Density } from "../hooks/useDensity";
import { AgentCard } from "../components/AgentCard";
import { AgentDrilldownDrawer } from "../components/AgentDrilldownDrawer";
import { CronErrorsPanel } from "../components/CronErrorsPanel";
import { OpenClawDispatchForm } from "../components/OpenClawDispatchForm";
import { OpenClawDispatchedPanel } from "../components/OpenClawDispatchedPanel";
import { OpenClawAlertBanner } from "../components/OpenClawAlertBanner";
import { StatusPill, ToneCallout } from "../components/atoms";

export function OpenClawFleet({ density }: { density: Density }) {
  const agents = useOpenClawAgents();
  const cronErrors = useOpenClawCronErrors();
  const dispatched = useOpenClawDispatched();
  const now = nowSec();
  const [selected, setSelected] = useState(0);
  const [drillId, setDrillId] = useState<string | null>(null);
  // „Stale statt leer": letzten guten Flottenstand halten, wenn ein Poll
  // leer-mit-Fehler zurückkommt (MC antwortet verzögert) → kein Aufblitzen von 0.
  const [fleet, setFleet] = useState<OpenClawFleetState | null>(null);
  useEffect(() => { setFleet((prev) => reconcileOpenClawFleet(prev, agents.data ?? null)); }, [agents.data]);
  const list = (fleet?.agents ?? []).slice().sort((a, b) => agentSortRank(b) - agentSortRank(a));
  const stale = fleet?.staleError ?? null;
  const active = list.filter((a) => ["active", "monitoring", "ready"].includes(a.status) && !a.stuckSignal).length;
  const problems = list.filter(agentIsProblem).length;
  const activeIndex = Math.min(selected, Math.max(0, list.length - 1));

  useEffect(() => {
    const onKey = (event: KeyboardEvent) => {
      const target = event.target as HTMLElement | null;
      if (target?.closest("input,textarea,[contenteditable='true'],[role='dialog']")) return;
      const key = event.key.toLowerCase();
      if (KEYMAP.list.next.includes(key as "j")) { event.preventDefault(); setSelected((idx) => Math.min(list.length - 1, idx + 1)); }
      if (KEYMAP.list.prev.includes(key as "k")) { event.preventDefault(); setSelected((idx) => Math.max(0, idx - 1)); }
      if (key === "o" || event.key === "Enter") {
        const agent = list[activeIndex];
        if (agent) {
          event.preventDefault();
          setDrillId(agent.id);
        }
      }
    };
    window.addEventListener("keydown", onKey);
    return () => window.removeEventListener("keydown", onKey);
  }, [activeIndex, list]);

  return (
    <div className="space-y-5">
      <section className="hc-card flex flex-col gap-2 p-4 sm:flex-row sm:items-center sm:justify-between">
        <div className="flex items-center gap-3"><div><p className="hc-eyebrow">OpenClaw-Worker</p><h2 className="mt-1 text-xl font-semibold text-white">{active}/{list.length} Agenten aktiv</h2></div>{problems > 0 ? <StatusPill tone="amber" label={`${problems} gestaucht/offline`} dot="warn" /> : null}{stale && list.length > 0 ? <StatusPill tone="amber" label={de.openclaw.staleBadge} dot="warn" /> : null}</div>
        {agents.loading ? <Spinner /> : <span className="text-sm hc-soft">Read-only aus Mission Control · {fleet?.updatedAt ? new Date(fleet.updatedAt * 1000).toLocaleTimeString("de-DE", { hour: "2-digit", minute: "2-digit" }) : "kein Zeitstempel"}</span>}
      </section>
      <OpenClawAlertBanner agents={list} />
      <CronErrorsPanel data={cronErrors.data} error={cronErrors.error} now={now} />
      <OpenClawDispatchForm onDispatched={() => void dispatched.reload()} />
      <OpenClawDispatchedPanel data={dispatched.data} error={dispatched.error} now={now} />
      {agents.error ? <ToneCallout tone="red">{agents.error}</ToneCallout> : null}
      {stale && list.length > 0 ? <ToneCallout tone="amber">{de.openclaw.staleFleet(fmtAge(fleet?.updatedAt ?? now, now))}: {stale}</ToneCallout> : null}
      {stale && list.length === 0 ? <ToneCallout tone="amber">{de.openclaw.unreachable}: {stale}</ToneCallout> : null}
      {list.length === 0 && !agents.loading && !stale ? <div className="hc-card flex items-center gap-3 p-4 text-sm hc-soft"><Shield className="h-5 w-5" />{de.openclaw.emptyFleet}</div> : null}
      <div className={cn("grid gap-4", density === "compact" ? "xl:grid-cols-2" : "lg:grid-cols-2")}>
        {list.map((agent, index) => <div key={agent.id} aria-selected={activeIndex === index} className={cn(activeIndex === index && "rounded-xl ring-1 ring-[var(--hc-accent-border)]")}><AgentCard agent={agent} density={density} now={now} onOpenDrilldown={() => setDrillId(agent.id)} /></div>)}
      </div>
      <AgentDrilldownDrawer agent={list.find((a) => a.id === drillId) ?? null} onClose={() => setDrillId(null)} />
    </div>
  );
}
