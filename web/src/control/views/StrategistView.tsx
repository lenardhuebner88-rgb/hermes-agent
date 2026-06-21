import { useCallback, useEffect, useRef, useState } from "react";
import { Gauge, Lightbulb, Loader2, Play, Rocket, ScrollText, ShieldAlert, Target, TrendingUp, Trash2 } from "lucide-react";
import { fetchJSON } from "@/lib/api";
import { Hero } from "../components/Hero";
import { ToneCallout } from "../components/atoms";
import { FleetEmptyState, FleetPanel } from "../components/fleet/atoms";
import { fmtClock } from "../lib/derive";
import type { Density } from "../hooks/useDensity";
import {
  metricSnapshotRows,
  proposalSource,
  type StrategistProposal,
  type StrategistProposalsResponse,
} from "../lib/strategist";

// Dedizierte Strategen-Fläche (G1 der Vision-Flywheel-Pipeline). Distinct from
// the Demand-Funnel queue: this lists the strategist-cron's self-gated,
// ROI-annotated PlanSpecs that landed *held* (freigabe:operator). Freigeben
// (approve) releases the chain to build; Verwerfen (veto) archives it. The
// distilled Vision metric snapshot rides along as triage context. Strings local
// (F3/F6-Muster — kein Edit an i18n/de.ts paralleler Sessions).
const t = {
  eyebrow: "Stratege",
  title: "Vorschläge des Strategen",
  subtitle:
    "Self-gated, ROI-annotierte PlanSpecs vom Strategen-Cron — held, bis du freigibst. Freigeben baut, Verwerfen archiviert.",
  metricsEyebrow: "Metrik-Snapshot",
  metricsMeta: "destillierte Vision-Kennzahlen als Triage-Kontext",
  metricsEmpty: "Noch kein Metrik-Snapshot geschrieben.",
  metricsEmptyDesc: "Der Stratege/Heartbeat schreibt vision-metrics.json — bis dahin triagierst du ohne Kontext.",
  listEyebrow: "Held-Vorschläge",
  listMeta: "freigabe:operator · warten auf deine Triage",
  empty: "Keine Vorschläge warten auf Freigabe.",
  emptyDesc: "Der Stratege läuft 2×/Tag und reiht nur ROI-positive, self-gated Specs hier ein.",
  loadError: "Vorschläge konnten nicht geladen werden.",
  targetLabel: "Ziel",
  roiLabel: "ROI",
  counterLabel: "Gegen-Metrik",
  groundingLabel: "Grounding-Beleg",
  unannotated: "ohne Annotation",
  subtasks: (n: number) => `${n} ${n === 1 ? "Teilaufgabe" : "Teilaufgaben"}`,
  approve: "Freigeben → bauen",
  approveHint: "gibt die Kette frei (held → ready) — der Dispatcher übernimmt",
  veto: "Verwerfen",
  vetoHint: "archiviert den Vorschlag samt Teilaufgaben — es wird nichts gebaut",
  confirm: "Bestätigen",
  cancel: "Abbrechen",
  approved: (id: string) => `Freigegeben — Kette ${id} ist eingereiht.`,
  vetoed: (id: string) => `${id} verworfen und archiviert.`,
  // Manuelle Trigger (G1.5): Stratege/Bewerter on-demand starten.
  triggerEyebrow: "Manuell auslösen",
  triggerMeta: "Stratege + Bewerter on-demand starten",
  runStrategist: "Strategen jetzt laufen",
  runStrategistHint: "fährt den propose-Lauf (wie 06:00) — schlägt neue PlanSpecs vor",
  runGutachter: "Bewerter jetzt laufen",
  runGutachterHint: "bewertet die offenen Vorschläge — Kommentar + Discord, kein Dispatch",
  running: "läuft…",
  lastRun: (s: string) => `zuletzt: ${s}`,
  neverRun: "noch nicht gelaufen",
  triggerStarted: "Lauf gestartet — das Ergebnis erscheint, sobald er durch ist.",
};

type PendingAction = { id: string; kind: "approve" | "veto" } | null;
type JobStatus = { running: boolean; exit_code: number | null; last_modified: number | null; tail: string[] };
type RunStatus = { propose: JobStatus; gutachter: JobStatus };
type TriggerWhich = "propose" | "gutachter";

export function StrategistView({ density }: { density: Density }) {
  const [data, setData] = useState<StrategistProposalsResponse | null>(null);
  const [pending, setPending] = useState<PendingAction>(null);
  const [busy, setBusy] = useState(false);
  const [notice, setNotice] = useState<string | null>(null);
  const [error, setError] = useState<string | null>(null);
  const inFlightRef = useRef(false);
  const aliveRef = useRef(true);
  useEffect(() => () => { aliveRef.current = false; }, []);

  const load = useCallback(async () => {
    if (inFlightRef.current) return;
    inFlightRef.current = true;
    try {
      const next = await fetchJSON<StrategistProposalsResponse>("/api/plugins/kanban/strategist/proposals");
      if (aliveRef.current) {
        setData(next);
        setError(null);
      }
    } catch (e) {
      if (aliveRef.current) setError(e instanceof Error ? e.message : String(e));
    } finally {
      inFlightRef.current = false;
    }
  }, []);

  useEffect(() => {
    const initial = window.setTimeout(() => void load(), 0);
    const timer = window.setInterval(() => {
      if (!document.hidden) void load();
    }, 30000);
    return () => {
      window.clearTimeout(initial);
      window.clearInterval(timer);
    };
  }, [load]);

  const act = useCallback(async (proposal: StrategistProposal, kind: "approve" | "veto") => {
    setBusy(true);
    setError(null);
    try {
      await fetchJSON(
        `/api/plugins/kanban/strategist/proposals/${encodeURIComponent(proposal.id)}/${kind}`,
        { method: "POST" },
      );
      setNotice(kind === "approve" ? t.approved(proposal.id) : t.vetoed(proposal.id));
      void load();
    } catch (e) {
      setError(e instanceof Error ? e.message : String(e));
    } finally {
      setBusy(false);
      setPending(null);
    }
  }, [load]);

  const proposals = data?.proposals ?? [];
  const rows = metricSnapshotRows(data?.metrics);

  return (
    <div className="space-y-4">
      <Hero
        eyebrow={t.eyebrow}
        title={t.title}
        subtitle={t.subtitle}
        count={data ? proposals.length : undefined}
        tone="violet"
        density={density}
      />

      {error ? <ToneCallout tone="red">{error}</ToneCallout> : null}
      {notice ? <ToneCallout tone="emerald">{notice}</ToneCallout> : null}

      <TriggerPanel onRan={() => void load()} />

      <FleetPanel eyebrow={t.metricsEyebrow} meta={t.metricsMeta}>
        {rows.length === 0 ? (
          <FleetEmptyState title={t.metricsEmpty} desc={t.metricsEmptyDesc} />
        ) : (
          <dl className="grid grid-cols-2 gap-2 sm:grid-cols-3">
            {rows.map((row) => (
              <div key={row.key} className="rounded-md border border-white/10 bg-black/15 px-3 py-2">
                <dt className="hc-type-label hc-dim flex items-center gap-1 truncate">
                  <Gauge className="h-3 w-3 shrink-0" />{row.label}
                </dt>
                <dd className="hc-mono mt-0.5 truncate text-[0.95rem] text-white">{row.value}</dd>
              </div>
            ))}
          </dl>
        )}
      </FleetPanel>

      <FleetPanel eyebrow={t.listEyebrow} meta={t.listMeta}>
        {data === null ? (
          <p className="text-sm hc-dim">…</p>
        ) : proposals.length === 0 ? (
          <FleetEmptyState title={t.empty} desc={t.emptyDesc} ok />
        ) : (
          <ProposalList
            proposals={proposals}
            pending={pending}
            busy={busy}
            onAct={(p, kind) => void act(p, kind)}
            onPending={setPending}
          />
        )}
      </FleetPanel>
    </div>
  );
}

// Pure list render — separately exported for the static render test (no polling).
export function ProposalList({
  proposals,
  pending,
  busy,
  onAct,
  onPending,
}: {
  proposals: StrategistProposal[];
  pending: PendingAction;
  busy: boolean;
  onAct: (p: StrategistProposal, kind: "approve" | "veto") => void;
  onPending: (p: PendingAction) => void;
}) {
  return (
    <ul className="space-y-1.5">
      {proposals.map((p) => {
        const isPending = pending?.id === p.id ? pending : null;
        const annotated = p.target_metric || p.roi || p.counter_metric || p.grounding;
        return (
          <li key={p.id} className="rounded-md border border-[var(--hc-accent-border)] px-3 py-2.5">
            <div className="flex flex-wrap items-center gap-2">
              <Lightbulb className="h-3.5 w-3.5 shrink-0 text-violet-200" />
              <span className="min-w-0 flex-1 basis-56 truncate text-[0.85rem] font-medium text-white">{p.title}</span>
              <span className="hc-mono shrink-0 rounded-full border border-white/15 px-2 py-0.5 text-[0.68rem] hc-soft">
                {proposalSource(p.created_by)}
              </span>
              <span className="hc-mono shrink-0 rounded-full border border-white/15 px-2 py-0.5 text-[0.68rem] hc-soft">
                {t.subtasks(p.subtask_count)}
              </span>
              <span className="hc-mono shrink-0 text-[0.72rem] hc-dim">{fmtClock(p.created_at)}</span>
            </div>

            <div className="mt-2 grid gap-1.5 sm:grid-cols-3">
              <AnnotationCell icon={<Target className="h-3 w-3" />} label={t.targetLabel} value={p.target_metric} />
              <AnnotationCell icon={<TrendingUp className="h-3 w-3" />} label={t.roiLabel} value={p.roi} />
              <AnnotationCell icon={<ShieldAlert className="h-3 w-3" />} label={t.counterLabel} value={p.counter_metric} />
            </div>
            {p.grounding ? (
              <p className="mt-1.5 flex items-start gap-1.5 rounded-md border border-emerald-400/20 bg-emerald-500/[.06] px-2.5 py-1.5 text-[0.74rem] hc-soft">
                <ScrollText className="mt-0.5 h-3.5 w-3.5 shrink-0 text-emerald-300" />
                <span><span className="hc-eyebrow mr-1.5">{t.groundingLabel}</span>{p.grounding}</span>
              </p>
            ) : null}
            {!annotated ? <p className="mt-1 text-[0.72rem] hc-dim">{t.unannotated}</p> : null}

            <div className="mt-2 flex flex-wrap items-center gap-2">
              {isPending ? (
                <>
                  <button
                    type="button"
                    disabled={busy}
                    onClick={() => onAct(p, isPending.kind)}
                    className="inline-flex min-h-9 items-center gap-1.5 rounded-md border border-[var(--hc-accent-border)] bg-[var(--hc-accent-wash)] px-3 py-1 text-[0.78rem] font-medium text-[var(--hc-accent-text)] disabled:opacity-50"
                  >
                    {isPending.kind === "approve" ? <Rocket className="h-3.5 w-3.5" /> : <Trash2 className="h-3.5 w-3.5" />}
                    {isPending.kind === "approve" ? t.approve : t.veto} · {t.confirm}
                  </button>
                  <button type="button" disabled={busy} onClick={() => onPending(null)} className="inline-flex min-h-9 items-center rounded-md border border-white/10 px-3 py-1 text-[0.78rem] hc-soft">{t.cancel}</button>
                  <span className="text-[0.72rem] hc-dim">{isPending.kind === "approve" ? t.approveHint : t.vetoHint}</span>
                </>
              ) : (
                <>
                  <button type="button" disabled={busy} onClick={() => onPending({ id: p.id, kind: "approve" })} className="inline-flex min-h-9 items-center gap-1.5 rounded-md border border-emerald-500/30 px-3 py-1 text-[0.78rem] text-emerald-200 hover:bg-emerald-500/10">
                    <Rocket className="h-3.5 w-3.5" />{t.approve}
                  </button>
                  <button type="button" disabled={busy} onClick={() => onPending({ id: p.id, kind: "veto" })} className="inline-flex min-h-9 items-center gap-1.5 rounded-md border border-red-500/25 px-3 py-1 text-[0.78rem] text-red-200 hover:bg-red-500/10">
                    <Trash2 className="h-3.5 w-3.5" />{t.veto}
                  </button>
                </>
              )}
            </div>
          </li>
        );
      })}
    </ul>
  );
}

function AnnotationCell({ icon, label, value }: { icon: React.ReactNode; label: string; value: string | null }) {
  return (
    <div className="rounded-md border border-white/10 bg-black/15 px-2.5 py-1.5">
      <div className="hc-type-label hc-dim flex items-center gap-1">{icon}{label}</div>
      <div className={value ? "mt-0.5 text-[0.8rem] text-white" : "mt-0.5 text-[0.8rem] hc-dim"}>{value ?? "—"}</div>
    </div>
  );
}

// Manuelle Trigger für Stratege (propose) + Bewerter (stratege-gutachter). Pollt
// /strategist/run-status alle 3s; nach einem abgeschlossenen Lauf refetcht es die
// Vorschlagsliste (onRan). Zwei-Schritt-Confirm wie approve/veto.
export function TriggerPanel({ onRan }: { onRan: () => void }) {
  const [status, setStatus] = useState<RunStatus | null>(null);
  const [pending, setPending] = useState<TriggerWhich | null>(null);
  const [busy, setBusy] = useState(false);
  const [err, setErr] = useState<string | null>(null);
  const [notice, setNotice] = useState<string | null>(null);
  const wasRunning = useRef(false);
  const aliveRef = useRef(true);
  useEffect(() => () => { aliveRef.current = false; }, []);

  const poll = useCallback(async () => {
    try {
      const s = await fetchJSON<RunStatus>("/api/plugins/kanban/strategist/run-status");
      if (!aliveRef.current) return;
      setStatus(s);
      const running = s.propose.running || s.gutachter.running;
      if (wasRunning.current && !running) onRan(); // ein Lauf wurde gerade fertig → Liste neu laden
      wasRunning.current = running;
    } catch {
      /* Status ist best-effort */
    }
  }, [onRan]);

  useEffect(() => {
    const initial = window.setTimeout(() => void poll(), 0);  // deferred: kein setState synchron im Effect
    const timer = window.setInterval(() => { if (!document.hidden) void poll(); }, 3000);
    return () => { window.clearTimeout(initial); window.clearInterval(timer); };
  }, [poll]);

  const fire = useCallback(async (which: TriggerWhich) => {
    setBusy(true); setErr(null); setNotice(null);
    const path = which === "propose" ? "run-propose" : "run-gutachter";
    try {
      const res = await fetchJSON<{ ok: boolean; detail?: string }>(
        `/api/plugins/kanban/strategist/${path}`, { method: "POST" });
      if (res.ok) setNotice(t.triggerStarted);
      else if (res.detail) setErr(res.detail);
      void poll();
    } catch (e) {
      setErr(e instanceof Error ? e.message : String(e));
    } finally {
      setBusy(false); setPending(null);
    }
  }, [poll]);

  return (
    <FleetPanel eyebrow={t.triggerEyebrow} meta={t.triggerMeta}>
      {err ? <ToneCallout tone="red">{err}</ToneCallout> : null}
      {notice ? <ToneCallout tone="cyan">{notice}</ToneCallout> : null}
      <div className="grid gap-2 sm:grid-cols-2">
        <TriggerRow
          label={t.runStrategist} hint={t.runStrategistHint}
          job={status?.propose} which="propose"
          pending={pending} busy={busy} onPending={setPending} onFire={(w) => void fire(w)}
        />
        <TriggerRow
          label={t.runGutachter} hint={t.runGutachterHint}
          job={status?.gutachter} which="gutachter"
          pending={pending} busy={busy} onPending={setPending} onFire={(w) => void fire(w)}
        />
      </div>
    </FleetPanel>
  );
}

function TriggerRow({ label, hint, job, which, pending, busy, onPending, onFire }: {
  label: string; hint: string; job: JobStatus | undefined; which: TriggerWhich;
  pending: TriggerWhich | null; busy: boolean;
  onPending: (p: TriggerWhich | null) => void; onFire: (w: TriggerWhich) => void;
}) {
  const running = job?.running ?? false;
  const isPending = pending === which;
  return (
    <div className="rounded-md border border-white/10 bg-black/15 px-3 py-2.5">
      <div className="flex items-center gap-2">
        <span className="min-w-0 flex-1 truncate text-[0.85rem] font-medium text-white">{label}</span>
        {running ? (
          <span className="hc-mono inline-flex shrink-0 items-center gap-1 rounded-full border border-cyan-400/30 px-2 py-0.5 text-[0.68rem] text-cyan-200">
            <Loader2 className="h-3 w-3 animate-spin" />{t.running}
          </span>
        ) : (
          <span className="hc-mono shrink-0 text-[0.7rem] hc-dim">
            {job?.last_modified ? t.lastRun(fmtClock(job.last_modified)) : t.neverRun}
          </span>
        )}
      </div>
      <div className="mt-2 flex flex-wrap items-center gap-2">
        {isPending ? (
          <>
            <button
              type="button" disabled={busy || running} onClick={() => onFire(which)}
              className="inline-flex min-h-9 items-center gap-1.5 rounded-md border border-[var(--hc-accent-border)] bg-[var(--hc-accent-wash)] px-3 py-1 text-[0.78rem] font-medium text-[var(--hc-accent-text)] disabled:opacity-50"
            >
              <Play className="h-3.5 w-3.5" />{t.confirm}
            </button>
            <button
              type="button" disabled={busy} onClick={() => onPending(null)}
              className="inline-flex min-h-9 items-center rounded-md border border-white/10 px-3 py-1 text-[0.78rem] hc-soft"
            >{t.cancel}</button>
          </>
        ) : (
          <button
            type="button" disabled={busy || running} onClick={() => onPending(which)}
            className="inline-flex min-h-9 items-center gap-1.5 rounded-md border border-cyan-500/30 px-3 py-1 text-[0.78rem] text-cyan-100 hover:bg-cyan-500/10 disabled:opacity-50"
          >
            <Play className="h-3.5 w-3.5" />{label}
          </button>
        )}
        <span className="text-[0.72rem] hc-dim">{hint}</span>
      </div>
    </div>
  );
}
