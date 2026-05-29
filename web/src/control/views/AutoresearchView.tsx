import { useEffect, useState } from "react";
import { FlaskConical, GitPullRequestArrow, Play, RotateCw, Square } from "lucide-react";
import { Button } from "@nous-research/ui/ui/components/button";
import { Spinner } from "@nous-research/ui/ui/components/spinner";
import { cn } from "@/lib/utils";
import { fetchJSON } from "@/lib/api";
import { useAutoresearchStatus, type useProposals } from "../hooks/useControlData";
import { fmtClock } from "../lib/derive";
import { clampLoopIterations, describeLoopStatus } from "../lib/autoresearch";
import { KEYMAP } from "../lib/keymap";
import { de } from "../i18n/de";
import type { Density } from "../hooks/useDensity";
import { StatusPill, ToneCallout } from "../components/atoms";
import { ProposalCard } from "../components/ProposalCard";

type ProposalStore = ReturnType<typeof useProposals>;

export function AutoresearchView({ density, store }: { density: Density; store: ProposalStore }) {
  const status = useAutoresearchStatus();
  const open = store.proposals.filter((p) => p.status === "proposed");
  const testing = store.proposals.filter((p) => p.status === "testing");
  const active = [...open, ...testing];
  const done = store.proposals.filter((p) => p.status === "applied" || p.status === "skipped");
  const statusTone = status.data?.state === "crashed" ? "red" : status.data?.heartbeat_fresh ? "cyan" : "amber";
  const loop = describeLoopStatus(status.data);
  const [maxIterations, setMaxIterations] = useState(2);
  const [loopBusy, setLoopBusy] = useState<"start" | "stop" | null>(null);
  const [loopMessage, setLoopMessage] = useState<string | null>(null);

  const startLoop = async () => {
    setLoopBusy("start");
    setLoopMessage(null);
    try {
      const body = { area: "all", focus: "recommended_sections", mode: "dry-run", confirm: false, max_iterations: clampLoopIterations(maxIterations) };
      const result = await fetchJSON<{ request_id?: string; pid?: number }>("/autoresearch/trigger", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify(body),
      });
      setLoopMessage(`Research-Loop gestartet${result.request_id ? ` · ${result.request_id}` : ""}`);
      await status.reload();
    } catch (e) {
      setLoopMessage(`Loop-Start fehlgeschlagen: ${e instanceof Error ? e.message : String(e)}`);
    } finally {
      setLoopBusy(null);
    }
  };

  const stopLoop = async () => {
    setLoopBusy("stop");
    setLoopMessage(null);
    try {
      const result = await fetchJSON<{ ok?: boolean; detail?: string }>("/autoresearch/stop", { method: "POST" });
      setLoopMessage(result.detail || "Stop-Signal gesendet");
      await status.reload();
    } catch (e) {
      setLoopMessage(`Stop fehlgeschlagen: ${e instanceof Error ? e.message : String(e)}`);
    } finally {
      setLoopBusy(null);
    }
  };

  useEffect(() => {
    const onKey = (event: KeyboardEvent) => {
      const target = event.target as HTMLElement | null;
      if (target?.closest("input,textarea,[contenteditable='true'],[role='dialog']")) return;
      const top = open[0];
      if (!top) return;
      const key = event.key.toLowerCase();
      if (KEYMAP.autoresearch.apply.includes(key as "a")) {
        event.preventDefault();
        void store.apply(top);
      }
      if (KEYMAP.autoresearch.skip.includes(key as "s")) {
        event.preventDefault();
        void store.skip(top);
      }
    };
    window.addEventListener("keydown", onKey);
    return () => window.removeEventListener("keydown", onKey);
  }, [open, store]);

  return (
    <div className="space-y-5">
      <section className="hc-card p-4 sm:p-5">
        <div className="flex flex-col gap-4 lg:flex-row lg:items-center lg:justify-between">
          <div className="space-y-3">
            <div className="flex flex-wrap items-center gap-2">
              {status.loading ? <Spinner /> : <StatusPill tone={statusTone} label={status.data?.state ?? "unbekannt"} dot={loop.running ? "live" : status.data?.state === "crashed" ? "error" : "idle"} />}
              <StatusPill tone={loop.routeTone} label={`Route ${status.data?.route_status ?? "unbekannt"}`} dot={loop.routeTone === "emerald" ? "ready" : "warn"} />
              <span className="rounded-full border border-white/10 px-2.5 py-1 text-xs hc-soft">{loop.iterationLabel}</span>
            </div>
            <div>
              <p className="hc-eyebrow">{de.autoresearch.nextStep}</p>
              <p className="mt-1 max-w-2xl text-base leading-7 text-white">{open.length > 0 ? de.autoresearch.nextStepOpen(open.length) : de.autoresearch.nextStepEmpty}</p>
              {status.error ? <p className="mt-2 text-sm text-red-200">{status.error}</p> : null}
            </div>
          </div>
          <div className="flex flex-col gap-2 sm:flex-row lg:flex-col xl:flex-row">
            <Button className="hc-hit" onClick={store.generate} disabled={!!store.busy} prefix={store.busy === "generate" ? <Spinner /> : <RotateCw className="h-4 w-4" />}>
              Vorschläge erzeugen (sofort)
            </Button>
            <Button outlined className="hc-hit" onClick={store.applyAll} disabled={!!store.busy || store.openSkillProposals.length === 0} prefix={<GitPullRequestArrow className="h-4 w-4" />}>
              {de.autoresearch.applyAll} ({store.openSkillProposals.length})
            </Button>
          </div>
        </div>
      </section>

      <section className="hc-card p-4 sm:p-5">
        <div className="flex flex-col gap-4 lg:flex-row lg:items-start lg:justify-between">
          <div className="min-w-0 flex-1 space-y-3">
            <div className="flex items-center justify-between gap-3">
              <div><p className="hc-eyebrow">Iterativer Research-Loop</p><h2 className="mt-1 text-lg font-semibold text-white">{loop.running ? `Iteration ${loop.iterationLabel}` : "kein Lauf aktiv"}</h2></div>
              <span className="hc-mono text-xs hc-soft">Heartbeat {loop.heartbeatLabel}</span>
            </div>
            <div className="h-2 overflow-hidden rounded-full bg-white/10"><div className="h-full rounded-full bg-[var(--hc-accent)]" style={{ width: `${loop.progressPercent}%` }} /></div>
            <div className="grid gap-3 text-sm sm:grid-cols-3">
              <Metric label="Letzter Schritt" value={loop.stepLabel} />
              <Metric label="Letzte Bewertung" value={loop.evalLabel} />
              <Metric label="Request" value={status.data?.request_id || "-"} />
            </div>
            {loop.routeHint ? <ToneCallout tone="amber">{loop.routeHint}: {status.data?.route_status ?? "unbekannt"}</ToneCallout> : null}
            <LastRun status={status.data} />
            {loopMessage ? <ToneCallout tone={loopMessage.includes("fehlgeschlagen") ? "red" : "emerald"}>{loopMessage}</ToneCallout> : null}
          </div>
          <div className="flex min-w-56 flex-col gap-2 rounded-lg border border-white/10 bg-white/[.03] p-3">
            <label className="text-xs hc-soft" htmlFor="loop-iterations">Max. Iterationen</label>
            <input id="loop-iterations" type="number" min={1} max={5} value={maxIterations} onChange={(event) => setMaxIterations(clampLoopIterations(Number(event.target.value)))} className="hc-hit rounded-lg border border-white/10 bg-black/30 px-3 text-sm text-white outline-none focus:border-[var(--hc-accent-border)]" />
            <Button className="hc-hit" onClick={startLoop} disabled={loop.running || !!loopBusy} prefix={loopBusy === "start" ? <Spinner /> : <Play className="h-4 w-4" />}>Research-Loop starten</Button>
            <Button outlined className="hc-hit" onClick={stopLoop} disabled={!loop.running || !!loopBusy} prefix={loopBusy === "stop" ? <Spinner /> : <Square className="h-4 w-4" />}>Stop</Button>
          </div>
        </div>
      </section>

      {store.error ? <ToneCallout tone="red">{store.error}</ToneCallout> : null}

      <section className="space-y-3">
        <div className="flex items-center justify-between"><h2 className="text-lg font-semibold text-white">{de.autoresearch.proposals}</h2>{store.loading ? <Spinner /> : null}</div>
        {active.length === 0 && !store.loading ? <Empty icon={<FlaskConical className="h-5 w-5" />} text="Keine offenen Vorschläge." /> : null}
        <div className="grid gap-4">
          {active.map((proposal) => <ProposalCard key={proposal.id} proposal={proposal} density={density} busy={store.busy === proposal.id} onApply={store.apply} onSkip={store.skip} />)}
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

function Metric({ label, value }: { label: string; value: string }) {
  return <div className="rounded-lg border border-white/10 bg-white/[.03] px-3 py-2"><p className="text-xs hc-dim">{label}</p><p className="hc-mono truncate text-sm font-semibold text-white">{value}</p></div>;
}

function LastRun({ status }: { status: ReturnType<typeof useAutoresearchStatus>["data"] }) {
  const receipt = status?.last_receipt;
  const note = status?.note;
  const lastRun = status?.last_run;
  const lastRunText = typeof lastRun === "string" || typeof lastRun === "number" ? String(lastRun) : null;
  const objectRun = lastRun && typeof lastRun === "object" ? lastRun as Record<string, unknown> : null;
  const finishedAt = typeof objectRun?.finished_at === "string" ? objectRun.finished_at : null;
  const mode = typeof objectRun?.mode === "string" ? objectRun.mode : null;
  const summary = objectRun ? [mode, finishedAt ? new Date(finishedAt).toLocaleString("de-DE") : null].filter(Boolean).join(" · ") : lastRunText;

  if (!summary && !receipt && !note) return <p className="text-sm hc-soft">Letzter Lauf: noch keine verwertbaren Laufdaten.</p>;
  return (
    <div className="rounded-lg border border-white/10 bg-black/20 px-3 py-2 text-sm hc-soft">
      <p><span className="text-white">Letzter Lauf:</span> {summary || "Backend liefert nur Statusnotiz"}</p>
      {receipt ? <p className="mt-1 truncate">Receipt: <span className="hc-mono">{receipt}</span></p> : null}
      {note ? <p className="mt-1">{note}</p> : null}
    </div>
  );
}

function Empty({ icon, text }: { icon: React.ReactNode; text: string }) {
  return <div className="hc-card flex items-center gap-3 p-4 text-sm hc-soft">{icon}<span>{text}</span></div>;
}
