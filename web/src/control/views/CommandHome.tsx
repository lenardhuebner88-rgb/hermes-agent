/**
 * CommandHome (/control) — the operator's cockpit.
 *
 * Replaces the flat Postfach list with one composed screen that answers the
 * three questions a solo fleet-operator actually has, in priority order:
 *   1. "Was braucht mich JETZT?"  → the #1 decision, promoted to an actionable
 *      hero (the rest queue below).               [useDecisionInbox]
 *   2. "Was tut die Flotte gerade?" → a live worker strip + today's throughput.
 *                                                 [useHermesWorkers / board / digest]
 *   3. "Läuft die Maschine?"        → a compact health pulse.   [useSystemHealth]
 *
 * Everything is real, polled data (no mocks). One screen instead of the
 * inbox+overview+pulse triple that all re-rendered slices of the same sources.
 */
import { useMemo, useState } from "react";
import { ArrowRight, ChevronRight, Inbox as InboxIcon } from "lucide-react";
import { useNavigate, useSearchParams } from "react-router-dom";
import { cn } from "@/lib/utils";
import {
  useAccountUsage,
  useBoard,
  useDecisionInbox,
  useFixRedispatch,
  useRepairDeliverable,
  useHermesRunsDaily,
  useHermesTodayDigest,
  useHermesWorkers,
  useSystemHealth,
} from "../hooks/useControlData";
import type { Density } from "../hooks/useDensity";
import type { BoardTask, ToneName, Worker } from "../lib/types";
import type { InboxItem, InboxSurface } from "../lib/decisionInbox";
import { heroAccent, severitySpine } from "../lib/tones";
import { flowCounts, roleChip } from "../lib/fleet";
import { fmtAge, fmtTokens, nowSec } from "../lib/derive";
import { StaleBadge, StatusPill } from "../components/atoms";
import { RoleChip } from "../components/fleet/atoms";
import { Eyebrow, Text } from "../components/primitives";
import { DayBars, Sparkline } from "../components/charts/charts";
import { FlowCapture } from "../components/fleet/FlowCapture";
import { AccountUsageTile } from "../components/AccountUsageTile";

const SURFACE: Record<InboxSurface, { label: string; tone: ToneName }> = {
  autoresearch: { label: "Autoresearch", tone: "cyan" },
  family: { label: "Family", tone: "violet" },
  orchestrator: { label: "Orchestrator", tone: "sky" },
  kanban: { label: "Kanban", tone: "amber" },
};

function surfaceFromParam(value: string | null): InboxSurface | null {
  return value === "autoresearch" || value === "family" || value === "orchestrator" || value === "kanban" ? value : null;
}

export function CommandHome({ density }: { density: Density }) {
  const navigate = useNavigate();
  const [searchParams, setSearchParams] = useSearchParams();
  const inbox = useDecisionInbox();
  const health = useSystemHealth();
  const accountUsage = useAccountUsage();
  const workers = useHermesWorkers();
  const digest = useHermesTodayDigest();
  const board = useBoard();
  // Der Surface-Filter lebt NUR im URL-Param (?surface=) — abgeleitet statt
  // als State-Spiegel (der frühere Sync-Effect verletzte react-hooks/
  // set-state-in-effect und konnte mit Taps um den Zustand konkurrieren).
  const surfaceFilter = surfaceFromParam(searchParams.get("surface"));
  const fix = useFixRedispatch();
  const repair = useRepairDeliverable();
  const now = board.data?.now ?? nowSec();

  const tasks: BoardTask[] = useMemo(
    () => board.data?.columns.flatMap((c) => c.tasks) ?? [],
    [board.data],
  );
  const counts = useMemo(() => flowCounts(tasks), [tasks]);
  const liveWorkers = workers.data?.workers ?? [];
  const shippedToday = digest.data?.count ?? 0;

  const settling = inbox.loading && inbox.items.length === 0;
  const calm = inbox.summary.total === 0;
  const heroTone: ToneName = settling ? "zinc" : calm ? "emerald" : inbox.worstTone;
  const top = inbox.items[0];
  const rest = (surfaceFilter ? inbox.items.filter((i) => i.surface === surfaceFilter) : inbox.items).slice(top && !surfaceFilter ? 1 : 0);

  const chooseSurfaceFilter = (id: InboxSurface | null) => {
    setSearchParams((current) => {
      const next = new URLSearchParams(current);
      if (id) next.set("surface", id);
      else next.delete("surface");
      return next;
    }, { replace: true });
  };

  const chips: Array<{ id: InboxSurface | null; label: string; count: number }> = [
    { id: null, label: "Alle", count: inbox.summary.total },
    { id: "autoresearch", label: SURFACE.autoresearch.label, count: inbox.summary.autoresearch },
    { id: "family", label: SURFACE.family.label, count: inbox.summary.family },
    { id: "orchestrator", label: SURFACE.orchestrator.label, count: inbox.summary.orchestrator },
    { id: "kanban", label: SURFACE.kanban.label, count: inbox.summary.kanban },
  ];

  return (
    <div className={cn("space-y-5", density === "compact" && "space-y-4")}>
      {/* ── COMMAND HERO ─────────────────────────────────────────────────────
          Left: the state + the #1 decision (actionable). Right: the pulse rail
          (fleet + today + health) — the whole situation in one surface. */}
      <section
        className="hc-hero grid gap-6 p-5 sm:p-7 lg:grid-cols-[1.55fr_1fr]"
        style={{ "--hc-hero-accent": heroAccent(heroTone) } as React.CSSProperties}
      >
        <div className="min-w-0">
          <div className="flex items-center gap-3">
            <Eyebrow>Kommandozentrale</Eyebrow>
            <StatusPill
              tone={calm ? "emerald" : heroTone === "red" || heroTone === "rose" ? "red" : "amber"}
              label={settling ? "lädt…" : calm ? "Alles ruhig" : "Aufmerksamkeit"}
              dot={settling ? "idle" : calm ? "live" : heroTone === "red" || heroTone === "rose" ? "error" : "warn"}
            />
          </div>
          <div className="hc-aurora-text hc-type-display mt-2 tabular-nums">{settling ? "—" : inbox.summary.total}</div>
          <Text as="h1" variant="title" className="hc-hero-statement mt-1 text-[var(--hc-text)]">
            {calm ? "Ruhig. Nichts wartet auf dich." : "Was braucht mich gerade?"}
          </Text>

          {top && !settling ? (
            <TopDecision item={top} onOpen={() => navigate(top.target)} fix={fix} repair={repair} />
          ) : calm ? (
            <p className="mt-4 max-w-md text-sm hc-soft">Die Flotte läuft, kein Vorschlag und kein Block wartet auf eine Entscheidung. Erfasse unten einen neuen Auftrag oder lehn dich zurück.</p>
          ) : null}
        </div>

        <PulseRail
          health={health}
          running={liveWorkers.length || counts.running}
          inReview={counts.review}
          blocked={counts.blocked}
          shippedToday={shippedToday}
          now={now}
          board={board}
          workers={workers}
          digest={digest}
        />
      </section>

      {/* ── LIVE FLEET ──────────────────────────────────────────────────────── */}
      <FleetStrip workers={liveWorkers} loading={workers.loading && !workers.data} now={now} onOpen={() => navigate("/control/flow")} freshness={workers} />

      {/* ── STATISTIK-PULS ──────────────────────────────────────────────────── */}
      <StatsPulse onOpen={() => navigate("/control/statistik")} />

      {/* ── ABO-LIMITS ──────────────────────────────────────────────────────── */}
      <AccountUsageTile usage={accountUsage.data} loading={accountUsage.loading && !accountUsage.data} error={accountUsage.error} />

      {/* ── THE QUEUE ───────────────────────────────────────────────────────── */}
      {!calm && !settling ? (
        <section className="space-y-3">
          <div className="flex flex-wrap items-center justify-between gap-2">
            <Eyebrow>Entscheidungen</Eyebrow>
            <div className="flex flex-wrap items-center gap-1.5">
              {chips.map((c) => (
                <button
                  key={c.id ?? "all"}
                  type="button"
                  aria-pressed={surfaceFilter === c.id}
                  onClick={() => chooseSurfaceFilter(c.id)}
                  className={cn(
                    "inline-flex items-center gap-1.5 rounded-full border px-2.5 py-1 text-xs font-medium transition",
                    surfaceFilter === c.id ? "border-[var(--hc-accent-border)] bg-[var(--hc-accent-wash)] text-[var(--hc-accent-text)]" : "border-white/10 hc-soft hover:border-white/20",
                  )}
                >
                  {c.label}<span className="hc-mono opacity-70">{c.count}</span>
                </button>
              ))}
            </div>
          </div>
          {rest.length === 0 ? (
            <p className="rounded-xl border border-dashed border-[var(--hc-border-strong)] px-4 py-5 text-center text-sm hc-soft">Nichts weiter in dieser Ansicht.</p>
          ) : (
            <div className="space-y-2">
              {rest.map((item) => (
                <DecisionRow key={item.key} item={item} onOpen={() => navigate(item.target)} fix={fix} repair={repair} />
              ))}
            </div>
          )}
        </section>
      ) : null}

      {inbox.sourceErrors.length ? (
        <p className="text-xs text-amber-300/80">{inbox.sourceErrors.join(" · ")}</p>
      ) : null}

      <FlowCapture onCreated={(id) => navigate(`/control/flow?task=${encodeURIComponent(id)}`)} />
    </div>
  );
}

/** The #1 decision, promoted: surface + title + why + the one next action.
 *  (Wrapper ist ein div, nicht ein button — der K3-Inline-Resolve braucht
 *  einen ECHTEN zweiten Button, und button-in-button ist invalides HTML.) */
function TopDecision({ item, onOpen, fix, repair }: { item: InboxItem; onOpen: () => void; fix: ReturnType<typeof useFixRedispatch>; repair: ReturnType<typeof useRepairDeliverable> }) {
  const surface = SURFACE[item.surface];
  return (
    <div
      className={cn(
        "hc-decision group mt-5 flex w-full flex-col gap-3 rounded-xl px-4 py-3.5 text-left transition sm:flex-row sm:items-start sm:gap-4",
        severitySpine[item.tone],
      )}
    >
      <button type="button" onClick={onOpen} className="min-w-0 flex-1 text-left">
        <div className="flex flex-wrap items-center gap-2">
          <StatusPill tone={surface.tone} label={surface.label} />
          <span className="text-[10px] font-semibold uppercase tracking-[.16em] hc-dim">Als Erstes</span>
        </div>
        <p className="mt-1.5 line-clamp-2 text-base font-semibold leading-snug text-white">{item.title}</p>
        <p className="mt-1 line-clamp-2 text-sm hc-soft">{item.why}</p>
      </button>
      <span className="flex shrink-0 flex-col items-stretch gap-2 sm:mt-0.5 sm:items-end">
        {item.fixTaskId ? <FixRedispatchButton taskId={item.fixTaskId} fix={fix} /> : null}
        {item.repairTaskId ? <RepairButton taskId={item.repairTaskId} repair={repair} /> : null}
        <button
          type="button"
          onClick={onOpen}
          className="inline-flex w-full items-center justify-center gap-1.5 rounded-lg border border-[var(--hc-accent-border)] bg-[var(--hc-accent-wash)] px-3 py-2.5 text-sm font-medium text-[var(--hc-accent-text)] transition group-hover:brightness-110 sm:w-auto sm:py-2"
        >
          {item.nextAction}<ArrowRight className="h-4 w-4" />
        </button>
      </span>
    </div>
  );
}

/** K3: confirm-gated Inline-Resolve für eine Verifier-Ablehnung — erster Klick
 *  scharfschalten, zweiter Klick löst PATCH ready + Dispatcher-Tick aus.
 *  Gleiches Zwei-Schritt-Muster wie die Fleet-Worker-Aktionen. */
function FixRedispatchButton({ taskId, fix }: { taskId: string; fix: ReturnType<typeof useFixRedispatch> }) {
  const [arming, setArming] = useState(false);
  const busy = fix.busyId === taskId;
  const done = !!fix.doneIds[taskId];
  const err = fix.errorById[taskId];
  if (done) {
    return (
      <span className="inline-flex items-center justify-center rounded-lg border border-emerald-500/25 bg-emerald-500/10 px-3 py-2 text-xs font-medium text-emerald-300">
        Fix-Lauf gestartet
      </span>
    );
  }
  return (
    <span className="flex flex-col items-stretch gap-1">
      <button
        type="button"
        disabled={busy}
        onClick={(e) => {
          e.stopPropagation();
          if (!arming) { setArming(true); return; }
          setArming(false);
          void fix.run(taskId);
        }}
        onBlur={() => setArming(false)}
        className={cn(
          "inline-flex items-center justify-center gap-1.5 rounded-lg border px-3 py-2 text-xs font-medium transition disabled:opacity-60",
          arming
            ? "border-amber-500/40 bg-amber-500/15 text-amber-200"
            : "border-[var(--hc-border-strong)] bg-[var(--hc-surface-2,rgba(255,255,255,0.04))] text-[var(--hc-text)] hover:border-[var(--hc-accent-border)]",
        )}
      >
        {busy ? "startet…" : arming ? "Sicher? Erneut klicken" : "Fix-Lauf starten"}
      </button>
      {err ? <span className="max-w-[14rem] text-[10px] leading-tight text-red-300">{err}</span> : null}
    </span>
  );
}

/** R1: confirm-gated Inline-Repair für ein hängendes Deliverable — erster Klick
 *  scharfschalten, zweiter Klick ruft POST /tasks/<id>/repair (blocked→done).
 *  Gleiches Zwei-Schritt-Muster wie der K3-Fix-Lauf und die Worker-Aktionen. */
function RepairButton({ taskId, repair }: { taskId: string; repair: ReturnType<typeof useRepairDeliverable> }) {
  const [arming, setArming] = useState(false);
  const busy = repair.busyId === taskId;
  const done = !!repair.doneIds[taskId];
  const err = repair.errorById[taskId];
  if (done) {
    return (
      <span className="inline-flex items-center justify-center rounded-lg border border-emerald-500/25 bg-emerald-500/10 px-3 py-2 text-xs font-medium text-emerald-300">
        Repariert
      </span>
    );
  }
  return (
    <span className="flex flex-col items-stretch gap-1">
      <button
        type="button"
        disabled={busy}
        onClick={(e) => {
          e.stopPropagation();
          if (!arming) { setArming(true); return; }
          setArming(false);
          void repair.run(taskId);
        }}
        onBlur={() => setArming(false)}
        className={cn(
          "inline-flex items-center justify-center gap-1.5 rounded-lg border px-3 py-2 text-xs font-medium transition disabled:opacity-60",
          arming
            ? "border-amber-500/40 bg-amber-500/15 text-amber-200"
            : "border-[var(--hc-border-strong)] bg-[var(--hc-surface-2,rgba(255,255,255,0.04))] text-[var(--hc-text)] hover:border-[var(--hc-accent-border)]",
        )}
      >
        {busy ? "repariert…" : arming ? "Sicher? Erneut klicken" : "Repair starten"}
      </button>
      {err ? <span className="max-w-[14rem] text-[10px] leading-tight text-red-300">{err}</span> : null}
    </span>
  );
}

/** The pulse rail: fleet · today · health, stacked — the situation at a glance. */
function PulseRail({ health, running, inReview, blocked, shippedToday, now, board, workers, digest }: {
  health: ReturnType<typeof useSystemHealth>;
  board: ReturnType<typeof useBoard>;
  workers: ReturnType<typeof useHermesWorkers>;
  digest: ReturnType<typeof useHermesTodayDigest>;
  running: number; inReview: number; blocked: number; shippedToday: number; now: number;
}) {
  const overall = health.data?.overall ?? "offline";
  const healthTone = !health.data ? "zinc" : overall === "healthy" ? "emerald" : overall === "degraded" ? "amber" : "red";
  const subs = health.data?.subsystems;
  const sysLabel: Array<[string, "healthy" | "degraded" | "offline" | undefined]> = [
    ["Gateway", subs?.gateway.status], ["Research", subs?.autoresearch.status], ["Kanban", subs?.kanban_db.status],
  ];
  return (
    <div className="flex flex-col gap-3 rounded-xl border border-[var(--hc-border)] bg-black/20 p-4 backdrop-blur-sm">
      <div className="grid grid-cols-3 gap-3">
        <RailStat label="Laufen" value={running} tone="cyan" dot={running > 0 ? "live" : "idle"} />
        <RailStat label="In Prüfung" value={inReview} tone={inReview > 0 ? "amber" : "zinc"} dot={inReview > 0 ? "warn" : "idle"} />
        <RailStat label="Blockiert" value={blocked} tone={blocked > 0 ? "red" : "zinc"} dot={blocked > 0 ? "error" : "idle"} />
      </div>
      <div className="flex items-center justify-between rounded-lg border border-emerald-500/20 bg-emerald-500/[.07] px-3 py-2">
        <span className="text-xs font-medium text-emerald-100/90">Heute geliefert</span>
        <span className="hc-mono text-lg font-semibold tabular-nums text-emerald-200">{shippedToday}</span>
      </div>
      <div className="mt-1 border-t border-white/8 pt-3">
        <div className="mb-2 flex items-center justify-between">
          <span className="text-[10px] font-semibold uppercase tracking-[.16em] hc-dim">System</span>
          <StatusPill tone={healthTone} label={!health.data ? "unbekannt" : overall === "healthy" ? "gesund" : overall} dot={!health.data ? "idle" : overall === "healthy" ? "live" : overall === "degraded" ? "warn" : "error"} />
        </div>
        <div className="mb-2 flex flex-wrap gap-1.5">
          <StaleBadge isStale={health.isStale} lastUpdated={health.lastUpdated} errorObj={health.errorObj} error={health.error} now={now} />
          <StaleBadge isStale={board.isStale} lastUpdated={board.lastUpdated} errorObj={board.errorObj} error={board.error} now={now} />
          <StaleBadge isStale={workers.isStale} lastUpdated={workers.lastUpdated} errorObj={workers.errorObj} error={workers.error} now={now} />
          <StaleBadge isStale={digest.isStale} lastUpdated={digest.lastUpdated} errorObj={digest.errorObj} error={digest.error} now={now} />
        </div>
        <div className="flex flex-col gap-1.5">
          {sysLabel.map(([label, st]) => (
            <div key={label} className="flex items-center justify-between text-xs">
              <span className="hc-soft">{label}</span>
              <span className={cn("inline-flex items-center gap-1.5 hc-mono", st === "healthy" ? "text-emerald-300" : st === "degraded" ? "text-amber-300" : st === "offline" ? "text-red-300" : "hc-dim")}>
                <span className={cn("h-1.5 w-1.5 rounded-full", st === "healthy" ? "bg-emerald-400" : st === "degraded" ? "bg-amber-400" : st === "offline" ? "bg-red-400" : "bg-zinc-500")} />
                {st ?? "—"}
              </span>
            </div>
          ))}
        </div>
      </div>
    </div>
  );
}

function RailStat({ label, value, tone, dot }: { label: string; value: number; tone: ToneName; dot: "live" | "warn" | "error" | "idle" }) {
  void tone;
  return (
    <div className="rounded-lg border border-white/10 bg-white/[.02] px-2.5 py-2">
      <div className="flex items-center gap-1.5">
        <span className={cn("h-1.5 w-1.5 rounded-full", dot === "live" ? "bg-cyan-400" : dot === "warn" ? "bg-amber-400" : dot === "error" ? "bg-red-400" : "bg-zinc-600")} />
        <span className="text-[10px] font-semibold uppercase tracking-wider hc-dim">{label}</span>
      </div>
      <div className="mt-1 hc-mono text-xl font-semibold tabular-nums text-white">{value}</div>
    </div>
  );
}

/** Live worker chips — what each agent is on right now. */
function FleetStrip({ workers, loading, now, onOpen, freshness }: { workers: Worker[]; loading: boolean; now: number; onOpen: () => void; freshness: ReturnType<typeof useHermesWorkers> }) {
  return (
    <section className="space-y-2">
      <div className="flex items-center justify-between">
        <div className="flex flex-wrap items-center gap-2">
          <Eyebrow>Die Flotte arbeitet</Eyebrow>
          <StaleBadge isStale={freshness.isStale} lastUpdated={freshness.lastUpdated} errorObj={freshness.errorObj} error={freshness.error} now={now} />
        </div>
        <button type="button" onClick={onOpen} className="inline-flex items-center gap-1 text-xs text-[var(--hc-accent-text)] hover:brightness-110">Flow öffnen<ChevronRight className="h-3.5 w-3.5" /></button>
      </div>
      {loading ? (
        <div className="hc-skeleton h-16 w-full rounded-xl" />
      ) : workers.length === 0 ? (
        <div className="flex items-center gap-3 rounded-xl border border-dashed border-[var(--hc-border-strong)] px-4 py-4">
          <InboxIcon className="h-4 w-4 hc-dim" />
          <p className="text-sm hc-soft">Kein Worker läuft gerade. Erfasse einen Auftrag, und die Flotte nimmt ihn auf.</p>
        </div>
      ) : (
        <div className="flex gap-3 overflow-x-auto pb-1">
          {workers.map((w) => {
            const role = roleChip(w.profile, null);
            return (
              <div key={w.run_id} className="hc-surface-card min-w-[15rem] max-w-[18rem] shrink-0 p-3">
                <div className="flex items-center gap-2">
                  <RoleChip role={role} />
                  <span className="ml-auto inline-flex items-center gap-1.5 text-[0.68rem] hc-dim">
                    <span className="h-1.5 w-1.5 animate-pulse rounded-full bg-emerald-400" />♥ {fmtAge(w.last_heartbeat_at, now)}
                  </span>
                </div>
                <p className="mt-2 line-clamp-2 text-sm font-medium leading-snug text-white">{w.task_title}</p>
                <p className="mt-1 hc-mono text-[0.66rem] hc-dim">{w.task_id} · seit {fmtAge(w.started_at, now)}</p>
              </div>
            );
          })}
        </div>
      )}
    </section>
  );
}

/** A compact decision row — severity spine, surface, why, next action. */
function DecisionRow({ item, onOpen, fix, repair }: { item: InboxItem; onOpen: () => void; fix: ReturnType<typeof useFixRedispatch>; repair: ReturnType<typeof useRepairDeliverable> }) {
  const surface = SURFACE[item.surface];
  return (
    <div className={cn("hc-decision flex w-full items-center gap-3 rounded-lg px-3.5 py-3 text-left", severitySpine[item.tone])}>
      <button type="button" onClick={onOpen} className="min-w-0 flex-1 text-left">
        <div className="flex flex-wrap items-center gap-2">
          <StatusPill tone={surface.tone} label={surface.label} />
          <span className="truncate text-sm font-semibold text-white">{item.title}</span>
        </div>
        <p className="mt-1 line-clamp-1 text-xs hc-soft">{item.why} · <span className="text-zinc-300">{item.nextAction}</span></p>
      </button>
      {item.fixTaskId ? <FixRedispatchButton taskId={item.fixTaskId} fix={fix} /> : null}
      {item.repairTaskId ? <RepairButton taskId={item.repairTaskId} repair={repair} /> : null}
      <button type="button" onClick={onOpen} aria-label={`Öffnen: ${item.title}`} className="shrink-0">
        <ChevronRight className="h-4 w-4 hc-dim" />
      </button>
    </div>
  );
}

/** Zwei Mini-Sparklines (14 Tage Durchsatz + Token-Burn) als Brücke in den
 *  Statistik-Tab. Teilt den /runs/daily-Poll mit der StatistikView über den
 *  dedupli­zierenden pollingStore — kein zusätzlicher Request. */
function StatsPulse({ onOpen }: { onOpen: () => void }) {
  const daily = useHermesRunsDaily();
  const series = (daily.data?.series ?? []).slice(-14);
  const hasSeries = series.length > 0 && series.some((p) => p.done_tasks > 0 || (p.output_tokens ?? 0) > 0);
  const hasSourceProblem = Boolean(daily.isStale || daily.errorObj || daily.error);
  if (!hasSeries && !hasSourceProblem) return null;
  return (
    <section className="space-y-2">
      <div className="flex items-center justify-between">
        <div className="flex flex-wrap items-center gap-2">
          <Eyebrow>Statistik-Puls · 14 Tage</Eyebrow>
          <StaleBadge isStale={daily.isStale} lastUpdated={daily.lastUpdated} errorObj={daily.errorObj} error={daily.error} />
        </div>
        <button type="button" onClick={onOpen} className="inline-flex items-center gap-1 text-xs text-[var(--hc-accent-text)] hover:brightness-110">Statistik öffnen<ChevronRight className="h-3.5 w-3.5" /></button>
      </div>
      {hasSeries ? <div className="grid gap-3 sm:grid-cols-2">
        <div className="hc-surface-card p-3">
          <Text variant="label" className="hc-dim">Geliefert (Roots/Tag)</Text>
          <DayBars points={series.map((p) => ({ label: p.date.slice(5), value: p.done_roots }))} />
        </div>
        <div className="hc-surface-card p-3">
          <Text variant="label" className="hc-dim">Token-Burn (out/Tag)</Text>
          <Sparkline points={series.map((p) => ({ label: p.date.slice(5), value: p.output_tokens ?? 0 }))} stroke="var(--hc-accent-2)" valueFmt={fmtTokens} />
        </div>
      </div> : null}
    </section>
  );
}
