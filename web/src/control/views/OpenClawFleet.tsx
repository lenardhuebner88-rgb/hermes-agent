import { useEffect, useState } from "react";
import { Shield } from "lucide-react";
import { Spinner } from "@nous-research/ui/ui/components/spinner";
import { cn } from "@/lib/utils";
import { useOpenClawAgents } from "../hooks/useControlData";
import { agentIsProblem, agentSortRank, nowSec } from "../lib/derive";
import { KEYMAP } from "../lib/keymap";
import type { Density } from "../hooks/useDensity";
import { AgentCard } from "../components/AgentCard";
import { StatusPill, ToneCallout } from "../components/atoms";

export function OpenClawFleet({ density }: { density: Density }) {
  const agents = useOpenClawAgents();
  const now = nowSec();
  const [selected, setSelected] = useState(0);
  const list = (agents.data?.agents ?? []).slice().sort((a, b) => agentSortRank(b) - agentSortRank(a));
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
    };
    window.addEventListener("keydown", onKey);
    return () => window.removeEventListener("keydown", onKey);
  }, [list.length]);

  return (
    <div className="space-y-5">
      <section className="hc-card flex flex-col gap-2 p-4 sm:flex-row sm:items-center sm:justify-between">
        <div className="flex items-center gap-3"><div><p className="hc-eyebrow">OpenClaw-Worker</p><h2 className="mt-1 text-xl font-semibold text-white">{active}/{list.length} Agenten aktiv</h2></div>{problems > 0 ? <StatusPill tone="amber" label={`${problems} gestaucht/offline`} dot="warn" /> : null}</div>
        {agents.loading ? <Spinner /> : <span className="text-sm hc-soft">Read-only aus Mission Control · {agents.data?.updatedAt ? new Date(agents.data.updatedAt * 1000).toLocaleTimeString("de-DE", { hour: "2-digit", minute: "2-digit" }) : "kein Zeitstempel"}</span>}
      </section>
      {agents.error ? <ToneCallout tone="red">{agents.error}</ToneCallout> : null}
      {agents.data?.error ? <ToneCallout tone="amber">MC nicht erreichbar: {agents.data.error}</ToneCallout> : null}
      {list.length === 0 && !agents.loading ? <div className="hc-card flex items-center gap-3 p-4 text-sm hc-soft"><Shield className="h-5 w-5" />MC nicht erreichbar oder keine Agenten gemeldet.</div> : null}
      <div className={cn("grid gap-4", density === "compact" ? "xl:grid-cols-2" : "lg:grid-cols-2")}>
        {list.map((agent, index) => <div key={agent.id} aria-selected={activeIndex === index} className={cn(activeIndex === index && "rounded-xl ring-1 ring-[var(--hc-accent-border)]")}><AgentCard agent={agent} density={density} now={now} /></div>)}
      </div>
    </div>
  );
}
