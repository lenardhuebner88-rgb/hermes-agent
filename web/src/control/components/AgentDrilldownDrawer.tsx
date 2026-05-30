import { useEffect } from "react";
import { X } from "lucide-react";
import { cn } from "@/lib/utils";
import { fmtClockTime } from "../lib/derive";
import type { AgentLive, Drilldown } from "../lib/types";
import { de } from "../i18n/de";
import { StatusPill, ToneCallout } from "./atoms";

interface Props {
  agent: AgentLive | null;
  onClose: () => void;
}

export function AgentDrilldownDrawer({ agent, onClose }: Props) {
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
