import { useEffect, useState } from "react";
import { CheckCircle2, Send, X, XCircle } from "lucide-react";
import { Spinner } from "@nous-research/ui/ui/components/spinner";
import { cn } from "@/lib/utils";
import { fetchJSON } from "@/lib/api";
import { fmtClockTime } from "../lib/derive";
import type { AgentLive, Drilldown } from "../lib/types";
import { de } from "../i18n/de";
import { StatusPill, ToneCallout } from "./atoms";

interface Props {
  agent: AgentLive | null;
  onClose: () => void;
}

type PingStatus = "idle" | "pending" | "success" | "error";

export function AgentDrilldownDrawer({ agent, onClose }: Props) {
  const [ping, setPing] = useState<{ agentId: string; status: PingStatus; error: string | null } | null>(null);

  useEffect(() => {
    if (!agent) return;

    const onKeyDown = (event: KeyboardEvent) => {
      if (event.key === "Escape") onClose();
    };

    window.addEventListener("keydown", onKeyDown);
    return () => window.removeEventListener("keydown", onKeyDown);
  }, [agent, onClose]);

  if (!agent) return null;

  const drilldown = agent.drilldown;
  const empty = isDrilldownEmpty(drilldown);
  const pingState = ping?.agentId === agent.id ? ping.status : "idle";
  const pingError = ping?.agentId === agent.id ? ping.error : null;
  const pingPending = pingState === "pending";

  const pingAgent = async () => {
    if (pingPending) return;
    setPing({ agentId: agent.id, status: "pending", error: null });
    try {
      const result = await fetchJSON<{ ok?: boolean; detail?: string }>(
        `/api/openclaw/agents/${encodeURIComponent(agent.id)}/ping`,
        { method: "POST" },
      );
      if (result.ok === false) {
        setPing({ agentId: agent.id, status: "error", error: result.detail || de.openclaw.pingError });
        return;
      }
      setPing({ agentId: agent.id, status: "success", error: null });
    } catch (e) {
      setPing({ agentId: agent.id, status: "error", error: e instanceof Error ? e.message : String(e) });
    }
  };

  return (
    <div className="fixed inset-0 z-50">
      <button
        type="button"
        className="absolute inset-0 bg-black/60"
        aria-label={de.openclaw.close}
        onClick={onClose}
      />
      <aside
        role="dialog"
        aria-modal="true"
        aria-labelledby="agent-drilldown-title"
        className={cn(
          "hc-card absolute right-0 top-0 h-full w-full max-w-md overflow-y-auto rounded-none border-y-0 border-l p-5 shadow-2xl",
          "border-[var(--hc-border)]",
        )}
      >
        <header className="flex items-start justify-between gap-3 border-b border-[var(--hc-border)] pb-4">
          <div className="min-w-0">
            <div className="flex min-w-0 items-center gap-3">
              <span className="grid h-11 w-11 shrink-0 place-items-center rounded-xl border border-white/10 bg-white/[.04] text-2xl">
                {agent.emoji}
              </span>
              <div className="min-w-0">
                <h2 id="agent-drilldown-title" className="truncate text-lg font-semibold text-white">
                  {agent.name}
                </h2>
                <div className="mt-1">
                  <StatusPill tone="violet" label={agent.roleLabel} size="sm" />
                </div>
              </div>
            </div>
            <div className="mt-3 space-y-1.5">
              <button
                type="button"
                aria-label={de.openclaw.pingAction}
                disabled={pingPending}
                className="inline-flex min-h-9 items-center gap-2 rounded-lg border border-white/10 bg-white/[.03] px-3 py-1.5 text-sm font-medium text-zinc-200 transition hover:bg-white/[.07] focus:outline-none focus:ring-2 focus:ring-[var(--hc-accent-border)] disabled:cursor-not-allowed disabled:opacity-60"
                onClick={() => void pingAgent()}
              >
                {pingPending ? <Spinner /> : pingState === "success" ? <CheckCircle2 className="h-4 w-4 text-emerald-300" /> : pingState === "error" ? <XCircle className="h-4 w-4 text-red-300" /> : <Send className="h-4 w-4" />}
                {pingPending ? de.openclaw.pingPending : de.openclaw.pingAction}
              </button>
              {pingState === "success" ? (
                <p className="flex min-w-0 items-center gap-1.5 text-xs text-emerald-200"><CheckCircle2 className="h-3.5 w-3.5 shrink-0" /><span className="min-w-0 break-words">{de.openclaw.pingSuccess}</span></p>
              ) : pingState === "error" ? (
                <p className="flex min-w-0 items-start gap-1.5 text-xs text-red-200"><XCircle className="mt-px h-3.5 w-3.5 shrink-0" /><span className="min-w-0 break-words">{pingError ? `${de.openclaw.pingError}: ${pingError}` : de.openclaw.pingError}</span></p>
              ) : null}
            </div>
          </div>
          <button
            type="button"
            className="grid h-9 w-9 shrink-0 place-items-center rounded-lg border border-white/10 bg-white/[.03] text-zinc-200 hover:bg-white/[.07]"
            aria-label={de.openclaw.close}
            onClick={onClose}
          >
            <X className="h-4 w-4" />
          </button>
        </header>

        <div className="space-y-5 py-5">
          {empty ? <p className="rounded-lg border border-white/10 bg-white/[.03] px-3 py-2 text-sm hc-soft">{de.openclaw.drilldownEmpty}</p> : null}

          {drilldown?.highlights?.length ? (
            <Section title={de.openclaw.sectionHighlights}>
              <div className="space-y-2">
                {drilldown.highlights.map((highlight, index) => (
                  <ToneCallout key={`${index}-${highlight}`} tone="violet">
                    <p className="whitespace-pre-wrap break-words font-medium">{highlight}</p>
                  </ToneCallout>
                ))}
              </div>
            </Section>
          ) : null}

          {drilldown?.decisions?.length ? (
            <Section title={de.openclaw.sectionDecisions}>
              <div className="space-y-2">
                {drilldown.decisions.map((decision, index) => (
                  <div key={decision.id ?? `${index}-${decision.label}`} className="rounded-lg border border-white/10 bg-white/[.03] px-3 py-2">
                    <p className="text-sm font-semibold text-white">{decision.label}</p>
                    <p className="mt-1 whitespace-pre-wrap break-words text-sm hc-soft">{decision.detail}</p>
                  </div>
                ))}
              </div>
            </Section>
          ) : null}

          {drilldown?.timeline?.length ? (
            <Section title={de.openclaw.sectionTimeline}>
              <div className="space-y-2">
                {drilldown.timeline.map((item, index) => (
                  <div key={item.id ?? `${index}-${item.at}-${item.label}`} className="rounded-lg border border-white/10 bg-white/[.03] px-3 py-2">
                    <div className="flex flex-wrap items-center gap-2">
                      <span className="rounded-full border border-white/10 px-2 py-0.5 text-xs hc-mono hc-dim">
                        {fmtClockTime(item.at)}
                      </span>
                      {item.kind ? <span className="rounded-full border border-white/10 px-2 py-0.5 text-xs hc-soft">{item.kind}</span> : null}
                      <p className="min-w-0 flex-1 text-sm font-medium text-white">{item.label}</p>
                    </div>
                    {item.detail ? <p className="mt-1 whitespace-pre-wrap break-words text-sm hc-soft">{item.detail}</p> : null}
                  </div>
                ))}
              </div>
            </Section>
          ) : null}

          {drilldown?.artifacts?.length ? (
            <Section title={de.openclaw.sectionArtifacts}>
              <div className="space-y-2">
                {drilldown.artifacts.map((artifact, index) => (
                  <div key={`${index}-${artifact.label}-${artifact.value}`} className="rounded-lg border border-white/10 bg-white/[.03] px-3 py-2">
                    <p className="text-sm font-semibold text-white">{artifact.label}</p>
                    <p className="mt-1 whitespace-pre-wrap break-words text-sm hc-mono">{artifact.value}</p>
                    {artifact.source ? <p className="mt-1 text-xs hc-dim">{artifact.source}</p> : null}
                  </div>
                ))}
              </div>
            </Section>
          ) : null}

          {drilldown?.sources?.length ? (
            <Section title={de.openclaw.sectionSources}>
              <div className="flex flex-wrap gap-2">
                {drilldown.sources.map((source, index) => (
                  <span key={`${index}-${source}`} className="rounded-full border border-white/10 bg-white/[.03] px-2.5 py-1 text-xs hc-soft">
                    {source}
                  </span>
                ))}
              </div>
            </Section>
          ) : null}
        </div>
      </aside>
    </div>
  );
}

function Section({ title, children }: { title: string; children: React.ReactNode }) {
  return (
    <section className="space-y-2">
      <h3 className="text-xs font-semibold uppercase tracking-wide hc-dim">{title}</h3>
      {children}
    </section>
  );
}

function isDrilldownEmpty(drilldown?: Drilldown): boolean {
  if (!drilldown) return true;
  return (
    drilldown.highlights.length === 0 &&
    drilldown.decisions.length === 0 &&
    drilldown.timeline.length === 0 &&
    drilldown.artifacts.length === 0 &&
    drilldown.sources.length === 0
  );
}
