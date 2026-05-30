import { useState } from "react";
import { AlertTriangle, X } from "lucide-react";
import { Button } from "@nous-research/ui/ui/components/button";
import { buildOpenClawAlerts } from "../lib/derive";
import type { AgentLive } from "../lib/types";
import { de } from "../i18n/de";
import { StatusPill } from "./atoms";

interface Props {
  agents: AgentLive[];
}

export function OpenClawAlertBanner({ agents }: Props) {
  const [dismissed, setDismissed] = useState(false);
  const { critical, warning, criticalCount, warningCount } = buildOpenClawAlerts(agents);
  const total = criticalCount + warningCount;

  if (dismissed || total === 0) return null;

  return (
    <section role="alert" className="hc-card border-amber-500/35 bg-amber-500/[.06] p-4 shadow-[0_0_0_1px_rgba(245,158,11,.10)]">
      <div className="flex flex-col gap-4 sm:flex-row sm:items-start sm:justify-between">
        <div className="flex min-w-0 gap-3">
          <div className="grid h-10 w-10 shrink-0 place-items-center rounded-lg border border-amber-500/30 bg-amber-500/10 text-amber-200">
            <AlertTriangle className="h-5 w-5" />
          </div>
          <div className="min-w-0 space-y-3">
            <div>
              <p className="hc-eyebrow">{de.openclaw.alertEyebrow}</p>
              <h3 className="mt-1 text-base font-semibold text-white">{de.openclaw.alertTitle}</h3>
            </div>
            <div className="flex flex-wrap gap-2">
              <StatusPill tone="red" label={`${criticalCount} ${de.openclaw.alertCritical}`} dot={criticalCount > 0 ? "error" : undefined} />
              <StatusPill tone="amber" label={`${warningCount} ${de.openclaw.alertWarnings}`} dot={warningCount > 0 ? "warn" : undefined} />
            </div>
            <div className="grid gap-2 text-sm md:grid-cols-2">
              {criticalCount > 0 ? <AlertGroup label={de.openclaw.alertCriticalAgents} names={critical.map((agent) => agent.name)} /> : null}
              {warningCount > 0 ? <AlertGroup label={de.openclaw.alertWarningAgents} names={warning.map((agent) => agent.name)} /> : null}
            </div>
          </div>
        </div>
        <Button
          ghost
          size="icon"
          aria-label={de.openclaw.alertDismiss}
          className="self-end sm:self-start"
          onClick={() => setDismissed(true)}
        >
          <X className="h-4 w-4" />
        </Button>
      </div>
    </section>
  );
}

function AlertGroup({ label, names }: { label: string; names: string[] }) {
  return (
    <div className="rounded-lg border border-white/10 bg-black/20 px-3 py-2">
      <p className="text-xs hc-dim">{label}</p>
      <p className="mt-1 break-words text-sm font-medium text-white">{names.join(", ")}</p>
    </div>
  );
}
