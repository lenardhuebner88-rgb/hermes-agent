import { AlertTriangle, Bot, FlaskConical } from "lucide-react";
import { Button } from "@nous-research/ui/ui/components/button";
import { useNavigate } from "react-router-dom";
import { useHermesWorkers } from "../hooks/useControlData";
import { buildOverview, nowSec, workerHealth } from "../lib/derive";
import { de } from "../i18n/de";
import type { Proposal } from "../lib/types";
import { StatusPill } from "../components/atoms";

export function OverviewView({ proposals }: { proposals: Proposal[] }) {
  const navigate = useNavigate();
  const workers = useHermesWorkers();
  const now = nowSec();
  const overview = buildOverview(workers.data?.workers ?? [], [], proposals, now);
  const title = overview.allHealthy ? de.overview.healthyTitle : de.overview.warnTitle(overview.warnings.length);

  return (
    <div className="space-y-5">
      <section className="hc-card p-5 sm:p-6">
        <div className="flex flex-col gap-4 lg:flex-row lg:items-end lg:justify-between">
          <div><p className="hc-eyebrow">System nominal</p><h2 className="mt-2 text-3xl font-semibold tracking-normal text-white">{title}</h2><p className="mt-2 max-w-2xl hc-soft">Hermes-Worker und Autoresearch liegen hier zusammen. OpenClaw folgt in B3, sobald der Read-only-Proxy steht.</p></div>
          <StatusPill tone={overview.allHealthy ? "emerald" : "amber"} label={overview.allHealthy ? "Ruhig" : "Aufmerksamkeit"} dot={overview.allHealthy ? "live" : "warn"} size="md" />
        </div>
      </section>
      <section className="grid gap-3 md:grid-cols-3">
        <Tile icon={<Bot />} label="Hermes laufen" value={`${overview.hermesRunning}/${overview.hermesTotal}`} onClick={() => navigate("/control/hermes")} />
        <Tile icon={<FlaskConical />} label={de.overview.proposals} value={String(overview.openProposals)} onClick={() => navigate("/control/autoresearch")} />
        <Tile icon={<AlertTriangle />} label={de.overview.warnings} value={String(overview.warnings.length)} onClick={() => navigate("/control/hermes")} />
      </section>
      <section className="hc-card p-4">
        <h3 className="mb-3 text-lg font-semibold text-white">{de.overview.needsAttention}</h3>
        {overview.warnings.length === 0 ? <p className="text-sm hc-soft">{de.overview.nothingUrgent}</p> : <div className="space-y-2">{overview.warnings.map((warning) => warning.kind === "hermes" ? <button key={warning.worker.run_id} type="button" onClick={() => navigate("/control/hermes")} className="flex w-full items-center justify-between rounded-lg border border-white/10 px-3 py-2 text-left text-sm hover:bg-white/5"><span>{warning.worker.task_title}</span><StatusPill tone={workerHealth(warning.worker, now).tone} label={workerHealth(warning.worker, now).label} dot={workerHealth(warning.worker, now).dot} /></button> : null)}</div>}
      </section>
    </div>
  );
}

function Tile({ icon, label, value, onClick }: { icon: React.ReactNode; label: string; value: string; onClick: () => void }) {
  return <Button outlined onClick={onClick} className="hc-card hc-hit flex h-auto items-center justify-start gap-3 p-4 text-left"><span className="text-[var(--hc-accent-text)]">{icon}</span><span><span className="block text-sm hc-soft">{label}</span><span className="block text-2xl font-semibold text-white">{value}</span></span></Button>;
}
