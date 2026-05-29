import { FlaskConical, GitPullRequestArrow, RotateCw } from "lucide-react";
import { Button } from "@nous-research/ui/ui/components/button";
import { Spinner } from "@nous-research/ui/ui/components/spinner";
import { cn } from "@/lib/utils";
import { useAutoresearchStatus, type useProposals } from "../hooks/useControlData";
import { fmtClock } from "../lib/derive";
import { de } from "../i18n/de";
import type { Density } from "../hooks/useDensity";
import { StatusPill, ToneCallout } from "../components/atoms";
import { ProposalCard } from "../components/ProposalCard";

type ProposalStore = ReturnType<typeof useProposals>;

export function AutoresearchView({ density, store }: { density: Density; store: ProposalStore }) {
  const status = useAutoresearchStatus();
  const open = store.proposals.filter((p) => p.status === "proposed");
  const done = store.proposals.filter((p) => p.status !== "proposed");
  const statusTone = status.data?.state === "crashed" ? "red" : status.data?.heartbeat_fresh ? "cyan" : "amber";

  return (
    <div className="space-y-5">
      <section className="hc-card p-4 sm:p-5">
        <div className="flex flex-col gap-4 lg:flex-row lg:items-center lg:justify-between">
          <div className="space-y-3">
            <div className="flex flex-wrap items-center gap-2">
              {status.loading ? <Spinner /> : <StatusPill tone={statusTone} label={status.data?.state ?? "unbekannt"} dot={status.data?.heartbeat_fresh ? "live" : "warn"} />}
              <span className="rounded-full border border-white/10 px-2.5 py-1 text-xs hc-soft">Iter {status.data?.iteration ?? 0}/{status.data?.max ?? 0}</span>
              <span className="rounded-full border border-white/10 px-2.5 py-1 text-xs hc-soft">Route {status.data?.route_status ?? "unbekannt"}</span>
            </div>
            <div>
              <p className="hc-eyebrow">{de.autoresearch.nextStep}</p>
              <p className="mt-1 max-w-2xl text-base leading-7 text-white">{open.length > 0 ? de.autoresearch.nextStepOpen(open.length) : de.autoresearch.nextStepEmpty}</p>
              {status.error ? <p className="mt-2 text-sm text-red-200">{status.error}</p> : null}
            </div>
          </div>
          <div className="flex flex-col gap-2 sm:flex-row lg:flex-col xl:flex-row">
            <Button className="hc-hit" onClick={store.generate} disabled={!!store.busy} prefix={store.busy === "generate" ? <Spinner /> : <RotateCw className="h-4 w-4" />}>
              {de.autoresearch.fetchMore}
            </Button>
            <Button outlined className="hc-hit" onClick={store.applyAll} disabled={!!store.busy || store.openSkillProposals.length === 0} prefix={<GitPullRequestArrow className="h-4 w-4" />}>
              {de.autoresearch.applyAll} ({store.openSkillProposals.length})
            </Button>
          </div>
        </div>
      </section>

      {store.error ? <ToneCallout tone="red">{store.error}</ToneCallout> : null}

      <section className="space-y-3">
        <div className="flex items-center justify-between"><h2 className="text-lg font-semibold text-white">{de.autoresearch.proposals}</h2>{store.loading ? <Spinner /> : null}</div>
        {open.length === 0 && !store.loading ? <Empty icon={<FlaskConical className="h-5 w-5" />} text="Keine offenen Vorschl?ge." /> : null}
        <div className="grid gap-4">
          {open.map((proposal) => <ProposalCard key={proposal.id} proposal={proposal} density={density} busy={store.busy === proposal.id} onApply={store.apply} onSkip={store.skip} />)}
        </div>
      </section>

      {done.length > 0 ? (
        <section className="space-y-3"><h2 className="text-lg font-semibold text-white">{de.autoresearch.done}</h2><div className="grid gap-3">{done.map((proposal) => <ProposalCard key={proposal.id} proposal={proposal} density={density} onApply={store.apply} onSkip={store.skip} />)}</div></section>
      ) : null}

      <section className="hc-card p-4">
        <h2 className="mb-3 text-base font-semibold text-white">{de.autoresearch.activity}</h2>
        {store.activity.length === 0 ? <p className="text-sm hc-soft">Noch keine Aktion in dieser Ansicht.</p> : <div className="space-y-2">{store.activity.map((entry) => <div key={`${entry.at}-${entry.text}`} className={cn("flex gap-3 rounded-lg border px-3 py-2 text-sm", entry.tone === "red" ? "border-red-500/20 bg-red-500/10 text-red-100" : entry.tone === "amber" ? "border-amber-500/20 bg-amber-500/10 text-amber-100" : entry.tone === "emerald" ? "border-emerald-500/20 bg-emerald-500/10 text-emerald-100" : "border-[var(--hc-accent-border)] bg-[var(--hc-accent-wash)] text-[var(--hc-accent-text)]")}><span className="hc-mono hc-dim">{fmtClock(entry.at)}</span><span>{entry.text}</span></div>)}</div>}
      </section>
    </div>
  );
}

function Empty({ icon, text }: { icon: React.ReactNode; text: string }) {
  return <div className="hc-card flex items-center gap-3 p-4 text-sm hc-soft">{icon}<span>{text}</span></div>;
}
