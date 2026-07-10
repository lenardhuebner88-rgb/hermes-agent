/**
 * CommandHome (/control) — der Operator-Leitstand-Start.
 *
 * Beantwortet die drei Fragen eines Solo-Flotten-Operators in Prioritätsreihung:
 *   1. "Was braucht mich JETZT?"  → die #1-Entscheidung als aktionierbarer Hero
 *      (der Rest reiht sich darunter).            [useDecisionInbox]
 *   2. "Was tut die Flotte gerade?" → Live-Worker-Strip + heutiger Durchsatz.
 *                                                 [useHermesWorkers / board / digest]
 *   3. "Läuft die Maschine?"        → ein kompakter System-/Kosten-Puls. [useSystemHealth]
 *
 * Design: dunkler Leitstand-Canvas — von Fleet nicht unterscheidbar. Skin via
 * [data-command-home] + command-home.css (Full-Bleed-Regeln analog `.fleet-bleed`),
 * Flächen aus den Leitstand-Tokens (surface-0/1/2, ink/ink-2/ink-3, line, live,
 * status-trio). IA/Funktion unverändert gegenüber dem Broadsheet-Vorgänger (S2).
 * Alles ist echte, gepollte Live-Daten (keine Mocks).
 *
 * Masthead: seit W3-2 (2026-07-10) rendert Start KEIN eigenes Masthead mehr —
 * die Shell-Puls-Leiste (ControlShell) trägt Label "Start" + Instrumente + die
 * NotificationBridge-Glocke (schließt denselben P2 "Glocke unsichtbar", den
 * Fleet in W3-1a schon hatte). Die alte Brand-Zeile ("Hermes Start") war reine
 * Dopplung des Shell-Labels; ihr LIVE/SYNC-Punkt dopplte die Aufmerksamkeits-
 * Hero-Statussignal direkt darunter (settling/calm/Achtung) — beides ersatzlos
 * entfernt statt verschoben, es gab keinen eigenständigen Signalwert.
 */
import { useMemo, useState, type ReactNode } from "react";
import { ArrowRight, ChevronRight, HeartPulse, Inbox as InboxIcon } from "lucide-react";
import { useNavigate, useSearchParams } from "react-router-dom";
import { cn } from "@/lib/utils";
import {
  useAccountUsage,
  useBoard,
  useDecisionInbox,
  useFixRedispatch,
  useRepairDeliverable,
  useVetoEscalation,
  useHermesRunsDaily,
  useHermesTodayDigest,
  useHermesWorkers,
  useStrategistCount,
  useSystemHealth,
} from "../hooks/useControlData";
import type { Density } from "../hooks/useDensity";
import type { AccountUsageResponse, BoardTask, ToneName, Worker } from "../lib/types";
import type { InboxItem, InboxSurface } from "../lib/decisionInbox";
import { heroAccent, severitySpine } from "../lib/tones";
import { flowCounts, roleChip } from "../lib/fleet";
import { fmtAge, fmtDur, fmtTokens, nowSec } from "../lib/derive";
import { StaleBadge } from "../components/atoms";
import { KpiTile, RoleChip, SignalChip, type SignalTone } from "../components/leitstand";
import { Eyebrow, Text } from "../components/primitives";
import { DayBars, Sparkline } from "../components/charts/charts";
import { FlowCapture } from "../components/fleet/FlowCapture";
import "./command-home.css";

const SURFACE: Record<InboxSurface, { label: string; tone: SignalTone }> = {
  autoresearch: { label: "Autoresearch", tone: "neutral" },
  family: { label: "Family", tone: "neutral" },
  orchestrator: { label: "Orchestrator", tone: "neutral" },
  kanban: { label: "Kanban", tone: "warn" },
};

function surfaceFromParam(value: string | null): InboxSurface | null {
  return value === "autoresearch" || value === "family" || value === "orchestrator" || value === "kanban" ? value : null;
}

/** Sektionskopf: Eyebrow links, ruhiges Meta rechts (SectionHeader-Idiom, dunkel).
 *  Eyebrow ist seit W3-2 das geteilte Primitive (Archivo-Caps, DESIGN.md "Mono
 *  = data only") statt einer eigenen Mono-Variante — mono bleibt den echten
 *  Datenspannen (Zahlen/IDs/Zeitstempel) vorbehalten. */
function ChSection({ label, meta }: { label: ReactNode; meta?: ReactNode }) {
  return (
    <div className="flex items-baseline justify-between gap-3">
      <Eyebrow>{label}</Eyebrow>
      {meta ? <span className="truncate text-right text-[11px] text-ink-3">{meta}</span> : null}
    </div>
  );
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
  const veto = useVetoEscalation();
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
    <div data-command-home className={cn("space-y-5", density === "compact" && "space-y-4")}>
      {/* ── ATTENTION FIRST ──────────────────────────────────────────────────
          Oben beantwortet CommandHome: ist alles ok und was braucht mich?
          Kein eigenes Masthead mehr (W3-2) — die Shell-Puls-Leiste trägt
          "Start" + Instrumente + Glocke, s. Datei-Kopfkommentar. */}
      <section
        aria-label="Aufmerksamkeit"
        className="ch-hero p-4 sm:p-6"
        style={{ "--ch-accent": heroAccent(heroTone) } as React.CSSProperties}
      >
        <div className="min-w-0">
          <div className="flex items-center gap-3">
            <Eyebrow>Attention Inbox</Eyebrow>
            <SignalChip
              tone={calm ? "ok" : heroTone === "red" || heroTone === "rose" ? "alert" : "warn"}
              label={settling ? "lädt…" : calm ? "Alles ruhig" : "Aufmerksamkeit"}
            />
          </div>
          <div className="mt-3 grid gap-3 sm:grid-cols-[minmax(0,1fr)_auto] sm:items-end">
            <div>
              <div className="ch-count">{settling ? "—" : inbox.summary.total}</div>
              <Text as="h1" variant="title" className="mt-1 text-ink">
                {calm ? "Ruhig. Nichts wartet auf dich." : "Was braucht mich gerade?"}
              </Text>
            </div>
            {/* 2×2 unter 480px: 4-up quetscht deutsche Labels ("FREIGABEN") bei 390 in Ellipsis. */}
            <div className="grid grid-cols-2 gap-1.5 min-[480px]:grid-cols-4 sm:min-w-72">
              <AttentionCount label="Freigaben" value={inbox.summary.autoresearch + inbox.summary.family} />
              <AttentionCount label="Held" value={inbox.summary.kanban} />
              <AttentionCount label="Fragen" value={inbox.summary.orchestrator} />
              <AttentionCount label="Top" value={top ? 1 : 0} />
            </div>
          </div>

          {top && !settling ? (
            <TopDecision item={top} onOpen={() => navigate(top.target)} fix={fix} repair={repair} veto={veto} />
          ) : calm ? (
            <p className="mt-4 max-w-md text-sm text-ink-2">Die Flotte läuft, kein Vorschlag und kein Block wartet auf eine Entscheidung. Erfasse unten einen neuen Auftrag oder lehn dich zurück.</p>
          ) : null}
        </div>
      </section>

      {/* ── SYSTEM-/KOSTEN-PULS ─────────────────────────────────────────────── */}
      <section aria-label="System- und Kosten-Puls" className="space-y-3">
        <ChSection label="System-/Kosten-Puls" meta="kompakt" />
        <div className="grid gap-3 lg:grid-cols-[minmax(0,1.35fr)_minmax(280px,.65fr)]">
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
          <AccountUsagePulse usage={accountUsage.data} loading={accountUsage.loading && !accountUsage.data} error={accountUsage.error} />
        </div>
      </section>

      {/* ── LIVE FLEET bleibt als reduzierte Präsenz unter dem Puls; die primäre
          Navigation sitzt unten in QuickJumps, damit keine Fleet-Funktion still
          verschwindet. */}
      <FleetStrip workers={liveWorkers} loading={workers.loading && !workers.data} now={now} onOpen={() => navigate("/control/fleet")} freshness={workers} />

      {/* ── STATISTIK-PULS ──────────────────────────────────────────────────── */}
      <StatsPulse onOpen={() => navigate("/control/statistik")} />

      {/* ── STRATEGEN-VORSCHLÄGE ────────────────────────────────────────────── */}
      <StrategistSignalTile onOpen={() => navigate("/control/stratege")} />

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
                  className={cn("ch-chip", surfaceFilter === c.id && "ch-chip-on")}
                >
                  {c.label}<span className="hc-mono opacity-70">{c.count}</span>
                </button>
              ))}
            </div>
          </div>
          {rest.length === 0 ? (
            <p className="ch-dashed px-4 py-5 text-center text-sm text-ink-2">Nichts weiter in dieser Ansicht.</p>
          ) : (
            <div className="space-y-2">
              {rest.map((item) => (
                <DecisionRow key={item.key} item={item} onOpen={() => navigate(item.target)} fix={fix} repair={repair} veto={veto} />
              ))}
            </div>
          )}
        </section>
      ) : null}

      {inbox.sourceErrors.length ? (
        <p className="text-xs text-status-warn">{inbox.sourceErrors.join(" · ")}</p>
      ) : null}

      <QuickJumps
        fleetCount={liveWorkers.length || counts.running}
        blocked={counts.blocked}
        shippedToday={shippedToday}
        accountProviders={accountUsage.data?.providers.length ?? 0}
        onNavigate={(path) => navigate(path)}
      />

      <FlowCapture onCreated={(id) => navigate(`/control/fleet?task=${encodeURIComponent(id)}`)} />
    </div>
  );
}

function AttentionCount({ label, value }: { label: string; value: number }) {
  return (
    <div className="ch-card px-2 py-2 text-center">
      <div className="hc-mono text-base font-semibold tabular-nums text-ink">{value}</div>
      <div className="mt-0.5 truncate text-[10px] font-semibold uppercase tracking-[.12em] text-ink-3">{label}</div>
    </div>
  );
}

function AccountUsagePulse({ usage, loading, error }: { usage?: AccountUsageResponse | null; loading: boolean; error?: string | null }) {
  const providers = usage?.providers ?? [];
  const available = providers.filter((provider) => provider.available).length;
  const percents = providers.flatMap((provider) => provider.windows.map((window) => window.used_percent).filter((value): value is number => value != null));
  const maxPercent = percents.length ? Math.max(...percents) : null;
  const limited = providers.find((provider) => provider.windows.some((window) => (window.used_percent ?? 0) >= 80));
  const status = error ? "Fehler" : loading ? "lädt" : limited ? limited.title : `${available}/${providers.length || 0}`;
  return (
    <div className="ch-panel space-y-3 p-3">
      <ChSection label="Kosten" meta={status} />
      <div className="grid grid-cols-2 gap-2">
        <KpiTile label="Accounts" value={loading ? "…" : available} suffix={providers.length ? `/ ${providers.length}` : undefined} dot={error ? "error" : limited ? "warn" : "live"} />
        <KpiTile label="Max Limit" value={maxPercent == null ? "—" : Math.round(maxPercent)} suffix={maxPercent == null ? undefined : "%"} dot={maxPercent != null && maxPercent >= 80 ? "warn" : "idle"} />
      </div>
      <p className="line-clamp-2 text-xs text-ink-2">{error ? error : limited ? `${limited.title}: Limit im Blick behalten.` : "Abo-/Provider-Puls ohne den alten Detail-Tiefgang; Details bleiben im Statistik-Tab."}</p>
    </div>
  );
}

function QuickJumps({ fleetCount, blocked, shippedToday, accountProviders, onNavigate }: {
  fleetCount: number;
  blocked: number;
  shippedToday: number;
  accountProviders: number;
  onNavigate: (path: string) => void;
}) {
  const rows = [
    { label: "Fleet", detail: `${fleetCount} aktiv`, path: "/control/fleet" },
    { label: "System", detail: blocked ? `${blocked} blockiert` : "Health" , path: "/control/system" },
    { label: "Statistik", detail: `${shippedToday} heute`, path: "/control/statistik" },
    { label: "Regal", detail: `${accountProviders} Provider`, path: "/control/bibliothek" },
  ];
  return (
    <section aria-label="Quick-Jumps" className="space-y-3">
      <ChSection label="Quick-Jumps" meta="Bottom-Bar Einstieg" />
      <div className="grid gap-2 sm:grid-cols-2 lg:grid-cols-4">
        {rows.map((row) => (
          <button key={row.path} type="button" onClick={() => onNavigate(row.path)} className="ch-jump">
            <span className="min-w-0 flex-1">
              <span className="block text-sm font-semibold text-ink">{row.label}</span>
              <span className="mt-0.5 block hc-mono text-[0.7rem] text-ink-3">{row.detail}</span>
            </span>
            <ChevronRight className="h-4 w-4 shrink-0 text-ink-3" />
          </button>
        ))}
      </div>
    </section>
  );
}

/** The #1 decision, promoted: surface + title + why + the one next action.
 *  (Wrapper ist ein div, nicht ein button — der K3-Inline-Resolve braucht
 *  einen ECHTEN zweiten Button, und button-in-button ist invalides HTML.) */
export function TopDecision({ item, onOpen, fix, repair, veto }: { item: InboxItem; onOpen: () => void; fix: ReturnType<typeof useFixRedispatch>; repair: ReturnType<typeof useRepairDeliverable>; veto: ReturnType<typeof useVetoEscalation> }) {
  const surface = SURFACE[item.surface];
  return (
    <div
      className={cn(
        "ch-decision group mt-5 flex w-full flex-col gap-3 px-4 py-3.5 text-left sm:flex-row sm:items-start sm:gap-4",
        severitySpine[item.tone],
      )}
    >
      <button type="button" onClick={onOpen} className="min-w-0 flex-1 text-left">
        <div className="flex flex-wrap items-center gap-2">
          <SignalChip tone={surface.tone} label={surface.label} />
          <span className="text-[10px] font-semibold uppercase tracking-[.16em] text-ink-3">Als Erstes</span>
          {item.ageSeconds != null ? (
            <span className="hc-mono text-[10px] tabular-nums text-ink-3">vor {fmtDur(item.ageSeconds)}</span>
          ) : null}
        </div>
        <p title={item.title} className="mt-1.5 line-clamp-3 text-base font-semibold leading-snug text-ink sm:line-clamp-2">{item.title}</p>
        <p title={item.why} className="mt-1 line-clamp-2 text-sm text-ink-2">{item.why}</p>
      </button>
      <span className="flex shrink-0 flex-col items-stretch gap-2 sm:mt-0.5 sm:items-end">
        {item.fixTaskId ? <FixRedispatchButton taskId={item.fixTaskId} fix={fix} /> : null}
        {item.repairTaskId ? <RepairButton taskId={item.repairTaskId} repair={repair} /> : null}
        {item.vetoEscalationTaskId ? <VetoSignalButton taskId={item.vetoEscalationTaskId} veto={veto} /> : null}
        <button
          type="button"
          onClick={onOpen}
          className="ch-btn ch-btn-primary w-full px-3 py-2.5 text-sm font-medium sm:w-auto sm:py-2"
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
      <span className="ch-btn ch-btn-done px-3 py-2 text-xs font-medium">Fix-Lauf gestartet</span>
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
        className={cn("ch-btn px-3 py-2 text-xs font-medium", arming && "ch-btn-arming")}
      >
        {busy ? "startet…" : arming ? "Sicher? Erneut klicken" : "Fix-Lauf starten"}
      </button>
      {err ? <span className="max-w-[14rem] text-[10px] leading-tight text-status-alert">{err}</span> : null}
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
      <span className="ch-btn ch-btn-done px-3 py-2 text-xs font-medium">Repariert</span>
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
        className={cn("ch-btn px-3 py-2 text-xs font-medium", arming && "ch-btn-arming")}
      >
        {busy ? "repariert…" : arming ? "Sicher? Erneut klicken" : "Repair starten"}
      </button>
      {err ? <span className="max-w-[14rem] text-[10px] leading-tight text-status-alert">{err}</span> : null}
    </span>
  );
}

/** Naht 3: veto an Autoresearch escalation → archive it AND teach the strategist
 *  (reflect) to suppress the signal. Two-click arming mirrors RepairButton. */
function VetoSignalButton({ taskId, veto }: { taskId: string; veto: ReturnType<typeof useVetoEscalation> }) {
  const [arming, setArming] = useState(false);
  const busy = veto.busyId === taskId;
  const done = !!veto.doneIds[taskId];
  const err = veto.errorById[taskId];
  if (done) {
    return (
      <span className="ch-btn ch-btn-done px-3 py-2 text-xs font-medium">Signal unterdrückt</span>
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
          void veto.run(taskId);
        }}
        onBlur={() => setArming(false)}
        className={cn("ch-btn px-3 py-2 text-xs font-medium", arming && "ch-btn-arming")}
      >
        {busy ? "unterdrücke…" : arming ? "Sicher? Erneut klicken" : "Signal künftig unterdrücken"}
      </button>
      {err ? <span className="max-w-[14rem] text-[10px] leading-tight text-status-alert">{err}</span> : null}
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
  const healthTone: SignalTone = !health.data ? "neutral" : overall === "healthy" ? "ok" : overall === "degraded" ? "warn" : "alert";
  const subs = health.data?.subsystems;
  const sysLabel: Array<[string, "healthy" | "degraded" | "offline" | undefined]> = [
    ["Gateway", subs?.gateway.status], ["Research", subs?.autoresearch.status], ["Kanban", subs?.kanban_db.status],
  ];
  return (
    <div className="ch-panel flex flex-col gap-3 p-3">
      <div className="grid grid-cols-2 gap-2 sm:grid-cols-4">
        <KpiTile label="Laufen" value={running} dot={running > 0 ? "live" : "idle"} />
        <KpiTile label="Prüfung" value={inReview} dot={inReview > 0 ? "warn" : "idle"} />
        <KpiTile label="Blockiert" value={blocked} dot={blocked > 0 ? "error" : "idle"} />
        <KpiTile label="Geliefert" value={shippedToday} suffix="heute" dot="live" />
      </div>
      <div className="border-t border-line pt-3">
        <div className="mb-2 flex items-center justify-between">
          <span className="text-[10px] font-semibold uppercase tracking-[.16em] text-ink-3">System</span>
          <SignalChip tone={healthTone} label={!health.data ? "unbekannt" : overall === "healthy" ? "gesund" : overall} />
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
              <span className="text-ink-2">{label}</span>
              <span className={cn("inline-flex items-center gap-1.5 hc-mono", st === "healthy" ? "text-status-ok" : st === "degraded" ? "text-status-warn" : st === "offline" ? "text-status-alert" : "text-ink-3")}>
                <span className={cn("h-1.5 w-1.5 rounded-full", st === "healthy" ? "bg-status-ok" : st === "degraded" ? "bg-status-warn" : st === "offline" ? "bg-status-alert" : "bg-ink-3")} />
                {st ?? "—"}
              </span>
            </div>
          ))}
        </div>
      </div>
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
        <button type="button" onClick={onOpen} className="inline-flex min-h-12 items-center gap-1 text-xs text-live hover:brightness-110">Flow öffnen<ChevronRight className="h-3.5 w-3.5" /></button>
      </div>
      {loading ? (
        <div className="ch-skeleton h-16 w-full" />
      ) : workers.length === 0 ? (
        <div className="ch-dashed flex items-center gap-3 px-4 py-4">
          <InboxIcon className="h-4 w-4 text-ink-3" />
          <p className="text-sm text-ink-2">Kein Worker läuft gerade. Erfasse einen Auftrag, und die Flotte nimmt ihn auf.</p>
        </div>
      ) : (
        <div className="flex gap-3 overflow-x-auto pb-1">
          {workers.map((w) => {
            const role = roleChip(w.profile, null);
            return (
              <div key={w.run_id} className="ch-card min-w-[15rem] max-w-[18rem] shrink-0 p-3">
                <div className="flex items-center gap-2">
                  <RoleChip role={role} />
                  <span className="ml-auto inline-flex shrink-0 items-center gap-1 text-[0.68rem] text-live">
                    <HeartPulse className="h-3 w-3 motion-safe:animate-pulse" aria-hidden />
                    {fmtAge(w.last_heartbeat_at, now)}
                  </span>
                </div>
                <p className="mt-2 line-clamp-2 text-sm font-medium leading-snug text-ink">{w.task_title}</p>
                <p className="mt-1 hc-mono text-[0.66rem] text-ink-3">{w.task_id} · seit {fmtAge(w.started_at, now)}</p>
              </div>
            );
          })}
        </div>
      )}
    </section>
  );
}

/** A compact decision row — severity spine, surface, why, next action. */
function DecisionRow({ item, onOpen, fix, repair, veto }: { item: InboxItem; onOpen: () => void; fix: ReturnType<typeof useFixRedispatch>; repair: ReturnType<typeof useRepairDeliverable>; veto: ReturnType<typeof useVetoEscalation> }) {
  const surface = SURFACE[item.surface];
  return (
    <div className={cn("ch-decision flex w-full items-center gap-3 px-3.5 py-3 text-left", severitySpine[item.tone])}>
      <button type="button" onClick={onOpen} className="min-w-0 flex-1 text-left">
        <div className="flex flex-wrap items-center gap-2">
          <SignalChip tone={surface.tone} label={surface.label} />
          <span className="truncate text-sm font-semibold text-ink">{item.title}</span>
        </div>
        <p className="mt-1 line-clamp-1 text-xs text-ink-2">{item.ageSeconds != null ? <span className="hc-mono tabular-nums text-ink-3">vor {fmtDur(item.ageSeconds)} · </span> : null}{item.why} · <span className="text-ink">{item.nextAction}</span></p>
      </button>
      {item.fixTaskId ? <FixRedispatchButton taskId={item.fixTaskId} fix={fix} /> : null}
      {item.repairTaskId ? <RepairButton taskId={item.repairTaskId} repair={repair} /> : null}
      {item.vetoEscalationTaskId ? <VetoSignalButton taskId={item.vetoEscalationTaskId} veto={veto} /> : null}
      <button type="button" onClick={onOpen} aria-label={`Öffnen: ${item.title}`} className="grid h-12 w-12 shrink-0 place-items-center rounded-card hover:bg-surface-3">
        <ChevronRight className="h-4 w-4 text-ink-3" />
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
        <button type="button" onClick={onOpen} className="inline-flex min-h-12 items-center gap-1 text-xs text-live hover:brightness-110">Statistik öffnen<ChevronRight className="h-3.5 w-3.5" /></button>
      </div>
      {hasSeries ? <div className="grid gap-3 sm:grid-cols-2">
        <div className="ch-card p-3">
          <Text variant="label" className="text-ink-3">Geliefert (Roots/Tag)</Text>
          <DayBars points={series.map((p) => ({ label: p.date.slice(5), value: p.done_roots }))} />
        </div>
        <div className="ch-card p-3">
          <Text variant="label" className="text-ink-3">Token-Burn (out/Tag)</Text>
          <Sparkline points={series.map((p) => ({ label: p.date.slice(5), value: p.output_tokens ?? 0 }))} stroke="var(--color-live)" valueFmt={fmtTokens} />
        </div>
      </div> : null}
    </section>
  );
}

/** Signal-Kachel: Strategen-Vorschläge warten auf Entscheidung.
 *  Nur sichtbar wenn count > 0. Muster: StatsPulse. */
function StrategistSignalTile({ onOpen }: { onOpen: () => void }) {
  const strat = useStrategistCount();
  const count = strat.data?.count ?? 0;
  if (count === 0) return null;
  return (
    <section className="space-y-2">
      <div className="flex items-center justify-between">
        <div className="flex flex-wrap items-center gap-2">
          <Eyebrow>Strategen-Vorschläge</Eyebrow>
        </div>
        <button type="button" onClick={onOpen} className="inline-flex min-h-12 items-center gap-1 text-xs text-live hover:brightness-110">Stratege öffnen<ChevronRight className="h-3.5 w-3.5" /></button>
      </div>
      <div className="ch-card p-3">
        <p className="text-sm text-ink-2">{count} {count === 1 ? "wartet" : "warten"} auf deine Entscheidung</p>
      </div>
    </section>
  );
}
