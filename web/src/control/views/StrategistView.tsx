import { useCallback, useEffect, useRef, useState } from "react";
import { Activity, Lightbulb, Loader2, Moon, Play, Rocket, ScrollText, ShieldAlert, Target, TrendingUp, Trash2, TriangleAlert } from "lucide-react";
import { fetchJSON } from "@/lib/api";
import { Hero } from "../components/Hero";
import { FleetEmptyState, FleetPanel, KpiTile, SignalChip, SignalLabel, signalToneFromLegacy } from "../components/leitstand";
import { fmtAge, fmtClock } from "../lib/derive";
import type { Density } from "../hooks/useDensity";
import { useStrategistLastRuns, useStrategistOutcomes } from "../hooks/useControlData";
import type { LeverOutcome } from "../lib/schemas";
import {
  formatSignedDelta,
  metricSnapshotRows,
  outcomeDeltaValue,
  outcomeStatusLabel,
  outcomeVerdictLabel,
  partitionProposals,
  runSummaryText,
  sourceLabel,
  sortProposalsByAge,
  isStaleHold,
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
  metricsEyebrow: "Kennzahlenbild",
  metricsMeta: "destillierte Vision-Kennzahlen als Triage-Kontext",
  metricsEmpty: "Noch kein Metrik-Snapshot geschrieben.",
  metricsEmptyDesc: "Der Stratege/Heartbeat schreibt vision-metrics.json — bis dahin triagierst du ohne Kontext.",
  listEyebrow: "Warten auf deine Entscheidung",
  listMeta: "Vorschläge des Strategen — du gibst frei oder verwirfst",
  empty: "Keine Vorschläge warten auf Freigabe.",
  emptyDesc: "Es ist keine Freigabe nötig; der Stratege prüft zweimal täglich auf neue Kandidaten.",
  // Zwei Untergruppen derselben freigabe:operator-Fläche, getrennt nach echter Herkunft.
  groupStrategistTitle: "Vom Strategen vorgeschlagen",
  groupStrategistDesc: "Self-gated, ROI-annotiert — das eigentliche Strategen-Ergebnis (propose-Lauf).",
  groupManualTitle: "Manuell ingestiert · wartet auf GO",
  groupManualDesc: "Von Hand via plan ingest eingereihte PlanSpecs (freigabe:operator) — kein Strategen-Vorschlag, teilt sich nur diese Freigabe-Fläche.",
  manualBadge: "Manuell ingestiert",
  loadError: "Vorschläge konnten nicht geladen werden.",
  targetLabel: "Ziel",
  roiLabel: "ROI",
  counterLabel: "Gegen-Metrik",
  groundingLabel: "Beleg anzeigen",
  unannotated: "ohne Annotation",
  subtasks: (n: number) => `${n} ${n === 1 ? "Teilaufgabe" : "Teilaufgaben"}`,
  heldFor: (age: string) => `seit ${age}`,
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
  // Wirkungs-Historie (Ziel-4): hat ein geshippter Lever gewirkt?
  outcomesEyebrow: "Wirkung",
  outcomesMeta: "geshippte Lever — Auftrag → Wirkung",
  outcomesEmpty: "Noch keine gemessenen Outcomes",
  outcomesEmptyDesc: "Sobald ein freigegebener Lever gebaut und die Reifezeit verstrichen ist, misst der Reflect-Schritt hier die Wirkung.",
  outcomesLoadError: "Wirkungs-Historie konnte nicht geladen werden.",
  noMetric: "kein Metrik-Key",
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
  const { strategist: strategistProposals, manual: manualProposals } = partitionProposals(proposals);
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

      {error ? <div className="flex items-start gap-2 rounded-card border border-status-alert/30 bg-status-alert/10 px-3 py-2 text-sec text-status-alert"><TriangleAlert aria-hidden className="mt-0.5 size-4 shrink-0" />{error}</div> : null}
      {notice ? <div className="flex items-center gap-2 rounded-card border border-line bg-surface-2 px-3 py-2 text-sec text-ink-2"><SignalLabel tone="ok" label="Erledigt" />{notice}</div> : null}

      <LastRunsStrip />

      <TriggerPanel onRan={() => void load()} />

      <FleetPanel eyebrow={t.metricsEyebrow} meta={t.metricsMeta}>
        {rows.length === 0 ? (
          <FleetEmptyState title={t.metricsEmpty} desc={t.metricsEmptyDesc} />
        ) : (
          <div className="grid grid-cols-2 gap-2 sm:grid-cols-3">
            {rows.map((row) => (
              <KpiTile key={row.key} label={row.label} value={row.value} />
            ))}
          </div>
        )}
      </FleetPanel>

      <FleetPanel eyebrow={t.listEyebrow} meta={t.listMeta}>
        {data === null ? (
          <p className="text-sm text-ink-3">…</p>
        ) : proposals.length === 0 ? (
          <FleetEmptyState title={t.empty} desc={t.emptyDesc} />
        ) : (
          <div className="space-y-4">
            {strategistProposals.length > 0 ? (
              <ProposalGroup
                title={t.groupStrategistTitle}
                desc={t.groupStrategistDesc}
                count={strategistProposals.length}
                accent="violet"
              >
                <ProposalList
                  proposals={strategistProposals}
                  variant="strategist"
                  pending={pending}
                  busy={busy}
                  onAct={(p, kind) => void act(p, kind)}
                  onPending={setPending}
                />
              </ProposalGroup>
            ) : null}
            {manualProposals.length > 0 ? (
              <ProposalGroup
                title={t.groupManualTitle}
                desc={t.groupManualDesc}
                count={manualProposals.length}
                accent="slate"
              >
                <ProposalList
                  proposals={manualProposals}
                  variant="manual"
                  pending={pending}
                  busy={busy}
                  onAct={(p, kind) => void act(p, kind)}
                  onPending={setPending}
                />
              </ProposalGroup>
            ) : null}
          </div>
        )}
      </FleetPanel>

      <OutcomesPanel />
    </div>
  );
}

/** Wirkungs-Historie (Ziel-4): liest die lever-outcomes.json (Ziel-2) über den
 *  Reflect-Schritt gemessenen Levern — Auftrag → Wirkung rückverfolgbar, ohne
 *  JSON-Dateien zu lesen. Rein lesend, kein Approve/Veto. Polling/fetch lebt
 *  hier; die reine Liste ist als `OutcomeList` separat exportiert (statischer
 *  Render-Test, kein Netzwerk — ProposalList-Muster). */
function OutcomesPanel() {
  const { data, error } = useStrategistOutcomes();
  const outcomes = data?.outcomes ?? [];
  return (
    <FleetPanel eyebrow={t.outcomesEyebrow} meta={t.outcomesMeta}>
      {error ? <div className="flex items-start gap-2 rounded-card border border-status-alert/30 bg-status-alert/10 px-3 py-2 text-sec text-status-alert"><TriangleAlert aria-hidden className="mt-0.5 size-4 shrink-0" />{t.outcomesLoadError}</div> : null}
      <OutcomeList outcomes={outcomes} />
    </FleetPanel>
  );
}

// Pure list render — separately exported for the static render test (no
// polling), mirroring `ProposalList` above. Owns the empty state too since
// the Auftrag calls for a Leerzustand assertion in the component test.
export function OutcomeList({ outcomes }: { outcomes: LeverOutcome[] }) {
  if (outcomes.length === 0) {
    return <FleetEmptyState title={t.outcomesEmpty} desc={t.outcomesEmptyDesc} />;
  }
  return (
    <ul className="space-y-1.5">
      {outcomes.map((o, idx) => (
        <OutcomeRow key={`${o.root_task_id ?? o.lever_key ?? "outcome"}-${idx}`} outcome={o} />
      ))}
    </ul>
  );
}

function OutcomeRow({ outcome }: { outcome: LeverOutcome }) {
  const delta = outcomeDeltaValue(outcome);
  return (
    <li className="flex flex-wrap items-center gap-2 rounded-card border border-line px-3 py-2">
      <Activity className="h-3.5 w-3.5 shrink-0 text-ink-3" />
      <span className="min-w-0 flex-1 basis-56 truncate text-sec font-medium text-ink">
        {outcome.lever_key ?? "—"}
      </span>
      <SignalChip tone={signalToneFromLegacy(outcome.status === "done" ? "emerald" : outcome.status === "failed" ? "red" : "zinc")} label={outcomeStatusLabel(outcome.status)} />
      <SignalChip tone={signalToneFromLegacy(outcome.verdict === "improved" ? "emerald" : outcome.verdict === "worsened" ? "red" : outcome.verdict === "unmeasurable" ? "amber" : "zinc")} label={outcomeVerdictLabel(outcome.verdict)} />
      <span className="font-data tabular-nums shrink-0 text-micro text-ink-3">{outcome.metric_key ?? t.noMetric}</span>
      {delta !== null ? (
        <span className="font-data tabular-nums shrink-0 text-sec text-ink">{formatSignedDelta(delta)}</span>
      ) : null}
      {outcome.proposed_at != null ? (
        <span className="font-data tabular-nums shrink-0 text-micro text-ink-3">{fmtAge(outcome.proposed_at)}</span>
      ) : null}
    </li>
  );
}

// Eine betitelte Untergruppe innerhalb der Freigabe-Fläche. Trennt die echten
// Strategen-Vorschläge sichtbar von den manuell ingesteten Hold-PlanSpecs, die
// sich nur dieselbe freigabe:operator-Fläche teilen.
function ProposalGroup({
  title,
  desc,
  count,
  accent,
  children,
}: {
  title: string;
  desc: string;
  count: number;
  accent: "violet" | "slate";
  children: React.ReactNode;
}) {
  const titleClass = accent === "violet" ? "font-display text-micro font-semibold uppercase tracking-[0.08em] text-brand" : "font-display text-micro font-semibold uppercase tracking-[0.08em] text-ink-2";
  return (
    <section className="space-y-1.5">
      <div>
        <p className={titleClass}>
          {title} <span className="font-data tabular-nums text-ink-3">· {count}</span>
        </p>
        <p className="mt-0.5 text-micro text-ink-3">{desc}</p>
      </div>
      {children}
    </section>
  );
}

// Pure list render — separately exported for the static render test (no polling).
// `variant` controls provenance display: "strategist" shows the ROI annotation
// scaffold (Ziel/ROI/Gegen-Metrik + grounding); "manual" is a hand-ingested
// Hold-PlanSpec that never carries those, so we show an honest origin badge and
// drop the empty annotation grid instead of mislabelling it "Aus Kennzahl".
export function ProposalList({
  proposals,
  pending,
  busy,
  onAct,
  onPending,
  variant = "strategist",
}: {
  proposals: StrategistProposal[];
  pending: PendingAction;
  busy: boolean;
  onAct: (p: StrategistProposal, kind: "approve" | "veto") => void;
  onPending: (p: PendingAction) => void;
  variant?: "strategist" | "manual";
}) {
  const isManual = variant === "manual";
  const sorted = sortProposalsByAge(proposals);
  return (
    <ul className="space-y-1.5">
      {sorted.map((p) => {
        const isPending = pending?.id === p.id ? pending : null;
        const annotated = p.target_metric || p.roi || p.counter_metric || p.grounding;
        return (
          <li key={p.id} className="rounded-card border border-line bg-surface-1 px-3 py-2.5">
            <div className="flex flex-wrap items-center gap-2">
              <Lightbulb className="h-3.5 w-3.5 shrink-0 text-brand" />
              <span className="min-w-0 flex-1 basis-56 truncate text-sec font-medium text-ink">{p.display_title || p.title}</span>
              <span className="shrink-0 text-micro text-ink-2">
                {isManual ? t.manualBadge : sourceLabel(p.source)}
              </span>
              <span className="shrink-0 text-micro text-ink-2">
                {t.subtasks(p.subtask_count)}
              </span>
              <span className="font-data tabular-nums shrink-0 text-micro text-ink-3">{fmtClock(p.created_at)}</span>
              {isStaleHold(p.age_seconds) ? <SignalChip tone="warn" label={t.heldFor(fmtAge(p.held_since))} /> : <span className="shrink-0 text-sec text-ink-3">{t.heldFor(fmtAge(p.held_since))}</span>}
            </div>

            {isManual ? null : (
              <>
                <div className="mt-2 grid gap-1.5 sm:grid-cols-3">
                  <AnnotationCell icon={<Target className="h-3 w-3" />} label={t.targetLabel} value={p.target_metric} />
                  <AnnotationCell icon={<TrendingUp className="h-3 w-3" />} label={t.roiLabel} value={p.roi} />
                  <AnnotationCell icon={<ShieldAlert className="h-3 w-3" />} label={t.counterLabel} value={p.counter_metric} />
                </div>
                {p.origin ? (
                  <p className="mt-1 text-micro text-ink-3">Entstanden aus: {p.origin}</p>
                ) : null}
                {p.grounding ? (
                  <details className="mt-1.5 rounded-card border border-line bg-surface-2 px-2.5 py-1.5 text-sec text-ink-2">
                    <summary className="flex cursor-pointer items-center gap-1.5 list-none">
                      <ScrollText className="h-3.5 w-3.5 shrink-0 text-brand" />
                      <span className="text-ink-3">{t.groundingLabel}</span>
                    </summary>
                    <p className="mt-1.5">{p.grounding}</p>
                  </details>
                ) : null}
                {!annotated ? <p className="mt-1 text-micro text-ink-3">{t.unannotated}</p> : null}
              </>
            )}

            <div className="mt-2 flex flex-wrap items-center gap-2">
              {isPending ? (
                <>
                  <button
                    type="button"
                    disabled={busy}
                    onClick={() => onAct(p, isPending.kind)}
                    className="inline-flex min-h-12 items-center gap-1.5 rounded-card border border-live bg-live/10 px-3 py-1 text-sec font-medium text-live disabled:opacity-50"
                  >
                    {isPending.kind === "approve" ? <Rocket className="h-3.5 w-3.5" /> : <Trash2 className="h-3.5 w-3.5" />}
                    {isPending.kind === "approve" ? t.approve : t.veto} · {t.confirm}
                  </button>
                  <button type="button" disabled={busy} onClick={() => onPending(null)} className="inline-flex min-h-12 items-center rounded-card border border-line px-3 py-1 text-sec text-ink-2">{t.cancel}</button>
                  <span className="text-micro text-ink-3">{isPending.kind === "approve" ? t.approveHint : t.vetoHint}</span>
                </>
              ) : (
                <>
                  <button type="button" disabled={busy} onClick={() => onPending({ id: p.id, kind: "approve" })} className="inline-flex min-h-12 items-center gap-1.5 rounded-card border border-live px-3 py-1 text-sec text-live hover:bg-live/10">
                    <Rocket className="h-3.5 w-3.5" />{t.approve}
                  </button>
                  <button type="button" disabled={busy} onClick={() => onPending({ id: p.id, kind: "veto" })} className="inline-flex min-h-12 items-center gap-1.5 rounded-card border border-line px-3 py-1 text-sec text-ink-2 hover:border-live hover:bg-live/10 hover:text-live">
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

/** Letzte-Läufe-Leiste: zeigt den jüngsten Harvest- und Propose-Lauf. */
function LastRunsStrip() {
  const { data } = useStrategistLastRuns();
  const harvest = data?.harvest ?? null;
  const propose = data?.propose ?? null;
  return (
    <FleetPanel eyebrow="Letzte Läufe" meta="Harvest + Stratege-Vorschlag on-demand oder via Cron">
      <div className="grid gap-2 sm:grid-cols-2">
        <div className="rounded-card border border-line bg-surface-2 px-3 py-2">
          <div className="flex items-center gap-2">
            <Moon className="h-3.5 w-3.5 shrink-0 text-ink-3" />
            <span className="min-w-0 flex-1 truncate text-sec font-medium text-ink">Harvest</span>
            {harvest ? <span className="font-data tabular-nums shrink-0 text-micro text-ink-3">{fmtClock(harvest.ts)}</span> : null}
          </div>
          <p className="mt-1 text-sec text-ink-2">{runSummaryText("harvest", harvest)}</p>
        </div>
        <div className="rounded-card border border-line bg-surface-2 px-3 py-2">
          <div className="flex items-center gap-2">
            <Lightbulb className="h-3.5 w-3.5 shrink-0 text-brand" />
            <span className="min-w-0 flex-1 truncate text-sec font-medium text-ink">Stratege</span>
            {propose ? <span className="font-data tabular-nums shrink-0 text-micro text-ink-3">{fmtClock(propose.ts)}</span> : null}
          </div>
          <p className="mt-1 text-sec text-ink-2">{runSummaryText("propose", propose)}</p>
        </div>
      </div>
    </FleetPanel>
  );
}

function AnnotationCell({ icon, label, value }: { icon: React.ReactNode; label: string; value: string | null }) {
  return (
    <div className="rounded-card border border-line bg-surface-2 px-2.5 py-1.5">
      <div className="font-display text-micro font-semibold uppercase tracking-[0.08em] text-ink-3 flex items-center gap-1">{icon}{label}</div>
      <div className={value ? "mt-0.5 text-sec text-ink" : "mt-0.5 text-sec text-ink-3"}>{value ?? "—"}</div>
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
      {err ? <div className="flex items-start gap-2 rounded-card border border-status-alert/30 bg-status-alert/10 px-3 py-2 text-sec text-status-alert"><TriangleAlert aria-hidden className="mt-0.5 size-4 shrink-0" />{err}</div> : null}
      {notice ? <div className="flex items-center gap-2 rounded-card border border-line bg-surface-2 px-3 py-2 text-sec text-ink-2"><SignalLabel tone="ok" label="Gestartet" />{notice}</div> : null}
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
    <div className="rounded-card border border-line bg-surface-2 px-3 py-2.5">
      <div className="flex items-center gap-2">
        <span className="min-w-0 flex-1 truncate text-sec font-medium text-ink">{label}</span>
        {running ? (
          <span className="inline-flex shrink-0 items-center gap-1.5"><Loader2 aria-hidden className="h-3 w-3 animate-spin text-status-ok" /><SignalLabel tone="ok" label={t.running} /></span>
        ) : (
          <span className="font-data tabular-nums shrink-0 text-micro text-ink-3">
            {job?.last_modified ? t.lastRun(fmtClock(job.last_modified)) : t.neverRun}
          </span>
        )}
      </div>
      <div className="mt-2 flex flex-wrap items-center gap-2">
        {isPending ? (
          <>
            <button
              type="button" disabled={busy || running} onClick={() => onFire(which)}
              className="inline-flex min-h-12 items-center gap-1.5 rounded-card border border-live bg-live/10 px-3 py-1 text-sec font-medium text-live disabled:opacity-50"
            >
              <Play className="h-3.5 w-3.5" />{t.confirm}
            </button>
            <button
              type="button" disabled={busy} onClick={() => onPending(null)}
              className="inline-flex min-h-12 items-center rounded-card border border-line px-3 py-1 text-sec text-ink-2"
            >{t.cancel}</button>
          </>
        ) : (
          <button
            type="button" disabled={busy || running} onClick={() => onPending(which)}
            className="inline-flex min-h-12 items-center gap-1.5 rounded-card border border-live px-3 py-1 text-sec text-live hover:bg-live/10 disabled:opacity-50"
          >
            <Play className="h-3.5 w-3.5" />{label}
          </button>
        )}
        <span className="text-micro text-ink-3">{hint}</span>
      </div>
    </div>
  );
}
