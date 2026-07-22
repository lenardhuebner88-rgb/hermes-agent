import { useMemo, useState, type CSSProperties, type ReactNode } from "react";
import { ArrowRight, CheckCircle2, ChevronRight, GitCommitHorizontal, WifiOff } from "lucide-react";
import { useNavigate } from "react-router-dom";
import { cn } from "@/lib/utils";
import { useAccountUsage, useHermesSubscriptionBurn, useHostUsage } from "../../hooks/costsUsage";
import { useDecisionInbox } from "../../hooks/decisionInbox";
import { useHermesRunsDaily, useHermesTodayDigest } from "../../hooks/runsDigestRollup";
import { useStatsConfig } from "../../hooks/stats";
import { useSystemHealth } from "../../hooks/systemReleaseHealth";
import { useBoard, useHermesWorkers } from "../../hooks/workersBoard";
import { useProjectCommits } from "../../hooks/projekte";
import type { Density } from "../../hooks/useDensity";
import type { StartCapacityCard, StartMatrixMode, StartProviderRow } from "../../lib/startMissionControl";
import {
  aggregateStartIssueCauses,
  buildStartCapacityCards,
  buildStartProviderRows,
  classifyCommitTheme,
  readableCommitTopic,
  startFlowFromToday,
  visibleAccountProviders,
} from "../../lib/startMissionControl";
import { DEFAULT_STATS_CONFIG } from "../../lib/statsFields";
import { fmtRelativeTime, fmtTokens, nowSec } from "../../lib/derive";
import { flowCounts } from "../../lib/fleet";
import { StaleBadge } from "../../components/atoms";
import { Eyebrow } from "../../components/primitives";
import { useStartIssues } from "./useStartIssues";
import "../start-mission-control.css";

type CssVars = CSSProperties & Record<`--${string}`, string | number>;

const DAY_FORMAT = new Intl.DateTimeFormat("de-DE", { weekday: "short", day: "2-digit" });
const DATE_FORMAT = new Intl.DateTimeFormat("de-DE", { weekday: "long", day: "numeric", month: "long" });

function Panel({ label, meta, children, className }: { label: ReactNode; meta?: ReactNode; children: ReactNode; className?: string }) {
  return (
    <section className={cn("smc-panel", className)}>
      <header className="smc-panel-head">
        <Eyebrow>{label}</Eyebrow>
        {meta ? <span className="smc-panel-meta">{meta}</span> : null}
      </header>
      {children}
    </section>
  );
}

function PollState({ loading, error, children }: { loading: boolean; error?: string | null; children: ReactNode }) {
  if (loading) return <div className="smc-state"><span className="smc-skeleton smc-skeleton-wide" /><span className="smc-skeleton" /></div>;
  if (error) return <div className="smc-state smc-state-error"><WifiOff aria-hidden /><div><strong>Daten nicht erreichbar</strong><span>{error}</span></div></div>;
  return <>{children}</>;
}

function ModeTabs({ mode, onChange }: { mode: StartMatrixMode; onChange: (mode: StartMatrixMode) => void }) {
  const modes: Array<{ id: StartMatrixMode; label: string }> = [
    { id: "tokens", label: "Tokens" },
    { id: "intensity", label: "Intensität" },
    { id: "sessions", label: "Sessions" },
  ];
  return (
    <div className="smc-tabs" role="group" aria-label="Matrixdarstellung">
      {modes.map((item) => (
        <button key={item.id} type="button" aria-pressed={mode === item.id} onClick={() => onChange(item.id)}>{item.label}</button>
      ))}
    </div>
  );
}

function matrixValue(row: StartProviderRow, index: number, mode: StartMatrixMode): string {
  const day = row.days[index];
  if (!row.tokenTelemetry) return "—";
  if (mode === "sessions") return String(day.sessions);
  return day.tokens > 0 ? fmtTokens(day.tokens) : "0";
}

function ProviderMatrixPanel({
  rows,
  mode,
  onMode,
  loading,
  error,
  stale,
  lastUpdated,
  scope,
}: {
  rows: StartProviderRow[];
  mode: StartMatrixMode;
  onMode: (mode: StartMatrixMode) => void;
  loading: boolean;
  error?: string | null;
  stale: boolean;
  lastUpdated: number | null;
  scope: ReturnType<typeof useHostUsage>["data"];
}) {
  const dates = rows[0]?.days.map((day) => day.date) ?? [];
  const maxSessions = Math.max(0, ...rows.flatMap((row) => row.days.map((day) => day.sessions)));
  const totalTokens = rows.reduce((sum, row) => sum + row.totalTokens, 0);
  const todayTokens = rows.reduce((sum, row) => sum + (row.days.at(-1)?.tokens ?? 0), 0);
  const yesterdayTokens = rows.reduce((sum, row) => sum + (row.days.at(-2)?.tokens ?? 0), 0);
  const totalSessions = rows.reduce((sum, row) => sum + row.totalSessions, 0);
  const leadProvider = [...rows].filter((row) => row.totalTokens > 0).sort((a, b) => b.totalTokens - a.totalTokens)[0];
  const trend = yesterdayTokens > 0
    ? `${new Intl.NumberFormat("de-DE", { signDisplay: "always", maximumFractionDigits: 0 }).format((todayTokens - yesterdayTokens) / yesterdayTokens * 100)} %`
    : todayTokens > 0 ? "neu" : "0 %";
  return (
    <Panel label="Provider-Matrix" meta={<ModeTabs mode={mode} onChange={onMode} />} className="smc-matrix-panel">
      <PollState loading={loading} error={error}>
        {rows.length === 0 ? <div className="smc-empty">Keine Provider-Telemetrie im gewählten Zeitraum.</div> : (
          <div className="smc-matrix-body">
            <div className="smc-matrix-summary">
              <div><strong>{fmtTokens(totalTokens)}</strong><span>aktive Tokens · {scope?.days ?? 7} Tage</span></div>
              <StaleBadge isStale={stale} lastUpdated={lastUpdated} error={error ?? ""} pollIntervalMs={60000} />
            </div>
            <div className="smc-matrix-scope" aria-label="Erfasste Host-Quellen">
              <span>Homeserver gesamt</span>
              <span>{totalSessions} Sessions</span>
              <span>{scope?.active_tmux_panes ?? 0} tmux aktiv</span>
              {(scope?.sources ?? []).map((source) => <span key={source.source}>{source.label} {source.sessions}</span>)}
            </div>
            <div className="smc-matrix smc-matrix-head" aria-hidden>
              <span>Provider</span>
              {dates.map((date, index) => <span key={date} className={index === dates.length - 1 ? "is-today" : undefined}>{index === dates.length - 2 ? "Gestern" : index === dates.length - 1 ? "Heute" : DAY_FORMAT.format(new Date(`${date}T12:00:00`))}</span>)}
              <span>7 Tage</span>
            </div>
            <div className="smc-matrix-rows">
              {rows.map((row) => (
                <div className="smc-matrix smc-matrix-row" key={row.key} style={{ "--provider-color": row.colorToken } as CssVars}>
                  <div className="smc-provider-name"><i aria-hidden /><span><strong>{row.label}</strong>{row.plan ? <small>{row.plan}</small> : null}</span></div>
                  <div className="smc-mobile-days" aria-hidden>{dates.map((date, index) => <span key={date} className={index === dates.length - 1 ? "is-today" : undefined}>{index === dates.length - 2 ? "Ge" : index === dates.length - 1 ? "He" : DAY_FORMAT.format(new Date(`${date}T12:00:00`)).slice(0, 2)}</span>)}</div>
                  {row.days.map((day, index) => {
                    const intensity = mode === "sessions" ? (maxSessions > 0 ? day.sessions / maxSessions : 0) : day.intensity;
                    return (
                      <div
                        key={day.date}
                        className={cn("smc-matrix-cell", index === row.days.length - 1 && "is-today", !row.tokenTelemetry && "is-unavailable")}
                        style={{ "--cell-alpha": Math.max(.04, intensity) } as CssVars}
                        title={`${row.label} · ${day.date}: ${row.tokenTelemetry ? `${fmtTokens(day.tokens)} Tokens · ${day.sessions} Sessions` : "keine Host-Telemetrie"}`}
                      >
                        {matrixValue(row, index, mode)}
                      </div>
                    );
                  })}
                  <div className="smc-matrix-total"><strong>{row.tokenTelemetry ? fmtTokens(row.totalTokens) : "—"}</strong><span>{row.tokenTelemetry && totalTokens > 0 ? `${Math.round(row.totalTokens / totalTokens * 100)} %` : "nicht erfasst"}</span></div>
                </div>
              ))}
            </div>
            <div className="smc-matrix-pulse" aria-label="Provider-Kurzsignale">
              <div><span>Heute</span><strong>{fmtTokens(todayTokens)}</strong></div>
              <div><span>vs. gestern</span><strong>{trend}</strong></div>
              <div><span>Sessions · 7 Tage</span><strong>{totalSessions}</strong></div>
              <div><span>Hauptlast</span><strong>{leadProvider ? `${leadProvider.label} · ${Math.round(leadProvider.totalTokens / totalTokens * 100)} %` : "—"}</strong></div>
            </div>
            <footer className="smc-matrix-foot"><span><i className="low" /><i className="mid" /><i className="high" /> wenig → viel</span><span>{scope?.accounting_note ?? "Aktive Ein-/Ausgabe ohne Cache"}</span></footer>
          </div>
        )}
      </PollState>
    </Panel>
  );
}

function quotaTone(value: number | null): "normal" | "warn" | "alert" {
  if (value == null || value < 75) return "normal";
  return value >= 90 ? "alert" : "warn";
}

function CapacityPanel({ cards, loading, error }: { cards: StartCapacityCard[]; loading: boolean; error?: string | null }) {
  const navigate = useNavigate();
  const bottleneck = [...cards].filter((card) => card.percent != null).sort((a, b) => (b.percent ?? 0) - (a.percent ?? 0))[0];
  const liveCount = cards.filter((card) => card.state === "live").length;
  return (
    <Panel
      label="Abos"
      meta={<span className="smc-capacity-status"><i className={liveCount === cards.length ? "is-ok" : "is-warn"} />{liveCount}/{cards.length || 4} live</span>}
      className="smc-capacity-panel"
    >
      <PollState loading={loading} error={error}>
        <div className="smc-capacity-body">
          <div className="smc-capacity-grid">
            {cards.map((card) => {
              const tone = quotaTone(card.percent);
              const stateLabel = card.state === "live" ? "live" : card.state === "fallback" ? "letzter Stand" : "Signal fehlt";
              return (
                <article
                  className={cn("smc-capacity-card", `is-${tone}`, bottleneck?.providerId === card.providerId && "is-bottleneck")}
                  key={card.providerId}
                  style={{ "--provider-color": card.colorToken, "--quota": `${card.percent ?? 0}%` } as CssVars}
                >
                  <header>
                    <div className="smc-provider-name"><i aria-hidden /><span><strong>{card.label}</strong>{card.plan ? <small>{card.plan}</small> : null}</span></div>
                    <span className={cn("smc-capacity-signal", `is-${card.state}`)}>{stateLabel}</span>
                  </header>
                  <div className="smc-capacity-reading">
                    <strong>{card.percent == null ? "—" : Math.round(card.percent)}{card.percent == null ? null : <small>%</small>}</strong>
                    <span><b>{card.windowLabel || "Kein Limitfenster"}</b>{card.reset ? <small>{card.reset}</small> : null}</span>
                  </div>
                  <div
                    className="smc-capacity-track"
                    role="progressbar"
                    aria-label={`${card.label}: ${card.percent == null ? "kein Prozentwert" : `${Math.round(card.percent)} Prozent verbraucht`}`}
                    aria-valuemin={0}
                    aria-valuemax={100}
                    aria-valuenow={card.percent ?? undefined}
                  ><i /><b aria-hidden /></div>
                  {card.secondary.length > 0 ? (
                    <footer>{card.secondary.map((window) => <span key={`${window.label}-${window.percent}`}>{window.label}<b>{Math.round(window.percent)}%</b></span>)}</footer>
                  ) : null}
                </article>
              );
            })}
          </div>
          <footer className="smc-capacity-foot"><span>Verbraucht · Marke bei 80 %</span><button type="button" onClick={() => navigate("/control/statistik")}>Details →</button></footer>
        </div>
      </PollState>
    </Panel>
  );
}

function DayFlowPanel({ flow, verified, loading, error }: { flow: ReturnType<typeof startFlowFromToday>; verified: number; loading: boolean; error?: string | null }) {
  const stages = [
    { label: "beendet", value: flow.ended, tone: "neutral" },
    { label: "erfolgreich", value: flow.successful, tone: "ok" },
    { label: "mit Reibung", value: flow.friction, tone: "alert" },
    { label: "geliefert", value: flow.delivered, tone: "ok" },
    { label: "geprüft", value: verified, tone: "ok" },
  ];
  const max = Math.max(1, ...stages.map((stage) => stage.value));
  return (
    <Panel label="Tagesfluss" meta="lokaler Tag">
      <PollState loading={loading} error={error}>
        <div className="smc-flow">
          {stages.map((stage) => <div key={stage.label} className={cn("smc-flow-stage", `is-${stage.tone}`)} style={{ "--flow-height": `${28 + stage.value / max * 44}px` } as CssVars}><strong>{stage.value}</strong><span>{stage.label}</span></div>)}
        </div>
        <footer className="smc-mini-foot"><span>{flow.successful} von {flow.ended} Läufen erfolgreich</span><span>{flow.deliveredTasks} Aufgaben abgeschlossen</span></footer>
      </PollState>
    </Panel>
  );
}

function RangeTabs({ days, onChange }: { days: 1 | 3 | 7; onChange: (days: 1 | 3 | 7) => void }) {
  return (
    <div className="smc-tabs smc-range-tabs" role="group" aria-label="Zeitraum der Problemursachen">
      {([1, 3, 7] as const).map((value) => (
        <button key={value} type="button" aria-pressed={days === value} onClick={() => onChange(value)}>{value} T</button>
      ))}
    </div>
  );
}

function IssuesPanel({ data, causes, days, onDays, loading, error }: {
  data: ReturnType<typeof useStartIssues>["data"];
  causes: ReturnType<typeof aggregateStartIssueCauses>;
  days: 1 | 3 | 7;
  onDays: (days: 1 | 3 | 7) => void;
  loading: boolean;
  error?: string | null;
}) {
  const navigate = useNavigate();
  const max = Math.max(1, ...causes.map((cause) => cause.count));
  const clusters = [...(data?.issues ?? [])].sort((a, b) => b.count - a.count || (b.last_seen ?? 0) - (a.last_seen ?? 0)).slice(0, 3);
  return (
    <Panel label="Problemursachen" meta={<RangeTabs days={days} onChange={onDays} />} className="smc-issues-panel">
      <PollState loading={loading} error={error}>
        {causes.length === 0 ? <div className="smc-empty">Keine problematischen Run-Ausgänge erfasst.</div> : (
          <div className="smc-issues-body">
            <div className="smc-cause-list">{causes.slice(0, 4).map((cause) => <div className="smc-cause-row" key={cause.key}><span>{cause.label}</span><i><b style={{ width: `${cause.count / max * 100}%` }} /></i><strong>{cause.count}</strong></div>)}</div>
            <div className="smc-cluster-list" aria-label="Größte Fehlercluster">
              {clusters.map((cluster, index) => {
                const causeKey = cluster.cause_key ?? "other";
                const reason = cluster.example_text.split("\n").find((line) => line.trim())?.trim() || cluster.cause_hint || cluster.signature;
                return (
                  <button
                    type="button"
                    key={`${cluster.profile}:${cluster.signature}:${causeKey}`}
                    className={cn("smc-cluster-row", index === 0 && "is-largest")}
                    onClick={() => cluster.example_task_id && navigate(`/control/fleet?task=${encodeURIComponent(cluster.example_task_id)}`)}
                  >
                    <span><em>{cluster.cause_label || "Sonstige"}</em><b>{cluster.count}×</b></span>
                    <strong>{cluster.example_task_title || cluster.signature}</strong>
                    <small title={reason}>{reason}</small>
                  </button>
                );
              })}
            </div>
          </div>
        )}
        <footer className="smc-mini-foot"><span>{data?.total_failed_runs ?? 0} Fälle · Ø {((data?.total_failed_runs ?? 0) / days).toFixed(1).replace(".", ",")} / Tag</span><span>{data?.truncated ? "Top-Gruppen" : "vollständig"}</span></footer>
      </PollState>
    </Panel>
  );
}

function CommitsPanel({ commits, loading, error, now }: {
  commits: ReturnType<typeof useProjectCommits>["data"];
  loading: boolean;
  error?: string | null;
  now: number;
}) {
  const navigate = useNavigate();
  const items = commits?.commits.slice(0, 4) ?? [];
  return (
    <Panel label="Änderungen" meta="neueste Commits">
      <PollState loading={loading} error={error}>
        {items.length === 0 ? <div className="smc-empty">Noch keine Änderungen im Projektfeed.</div> : (
          <div className="smc-commit-list">{items.map((commit) => (
            <button type="button" className="smc-commit-row" key={`${commit.project}:${commit.hash}`} onClick={() => navigate("/control/projekte")}>
              <GitCommitHorizontal aria-hidden />
              <span><em>{classifyCommitTheme(commit)}</em><strong>{readableCommitTopic(commit)}</strong><small>{commit.author || "Unbekannt"} · {commit.project_name || commit.project} · {fmtRelativeTime(commit.committed_at, now)}</small></span>
              <code>{commit.hash}</code>
            </button>
          ))}</div>
        )}
      </PollState>
      <footer className="smc-panel-link"><span>Hash · Autor · verständliches Thema</span><button type="button" onClick={() => navigate("/control/projekte")}>Alle Commits →</button></footer>
    </Panel>
  );
}

function ActionsPanel({ inbox, loading }: { inbox: ReturnType<typeof useDecisionInbox>; loading: boolean }) {
  const navigate = useNavigate();
  const actions = inbox.items.slice(0, 3);
  return (
    <Panel label="Nächster sinnvoller Klick" meta="nach Wirkung">
      {loading ? <div className="smc-state"><span className="smc-skeleton smc-skeleton-wide" /></div> : actions.length === 0 ? <div className="smc-empty smc-empty-calm"><CheckCircle2 aria-hidden />Keine Entscheidung wartet. Die Flotte kann weiterarbeiten.</div> : <div className="smc-action-list">{actions.map((item, index) => (
        <button type="button" key={item.key} className={cn("smc-action-row", index === 0 && "is-first")} onClick={() => navigate(item.target)}>
          <span className="smc-action-rank">{index + 1}</span><span><strong>{item.title}</strong><small>{item.why}</small></span><span className="smc-action-link">{item.nextAction}<ArrowRight aria-hidden /></span>
        </button>
      ))}</div>}
      <footer className="smc-panel-link"><span>{inbox.summary.total > actions.length ? `+ ${inbox.summary.total - actions.length} weitere` : "Priorität aus der Entscheidungs-Inbox"}</span><button type="button" onClick={() => navigate("/control/fleet")}>Alles öffnen →</button></footer>
    </Panel>
  );
}

function DeliveriesPanel({ digest, loading, error }: { digest: ReturnType<typeof useHermesTodayDigest>["data"]; loading: boolean; error?: string | null }) {
  const navigate = useNavigate();
  const items = digest?.items.slice(0, 4) ?? [];
  return (
    <Panel label="Geliefert" meta="heute · nach Evidenz">
      <PollState loading={loading} error={error}>
        {items.length === 0 ? <div className="smc-empty">Heute ist noch kein Ergebnis geliefert.</div> : <div className="smc-delivery-list">{items.map((item) => (
          <button type="button" className="smc-delivery-row" key={item.run_id} onClick={() => navigate(`/control/fleet?task=${encodeURIComponent(item.task_id)}`)}>
            <i aria-hidden className={item.verification_state === "approved" ? "is-approved" : undefined} />
            <span><strong>{item.task_title}</strong><small>{item.deliverable_excerpt || item.task_summary || "Ergebnis liegt vor"}</small></span>
            <em className={item.verification_state === "approved" ? "is-approved" : undefined}>{item.verification_state === "approved" ? "geprüft" : item.verdict_label}</em>
          </button>
        ))}</div>}
      </PollState>
      <footer className="smc-panel-link"><span>{digest?.count && digest.count > items.length ? `+ ${digest.count - items.length} weitere` : `${digest?.count ?? 0} heute`}</span><button type="button" onClick={() => navigate("/control/statistik")}>Alle Ergebnisse →</button></footer>
    </Panel>
  );
}

function QuickJumps({ blocked, delivered, providers }: { blocked: number; delivered: number; providers: number }) {
  const navigate = useNavigate();
  const jumps = [
    { label: "Neuer Auftrag", detail: "+ erfassen", path: "/control/fleet" },
    { label: "Offene Eingriffe", detail: blocked ? `${blocked} blockiert` : "nichts akut", path: "/control/fleet" },
    { label: "Provider & Limits", detail: `${providers} Signale`, path: "/control/statistik" },
    { label: "Lieferungen", detail: `${delivered} heute`, path: "/control/statistik" },
  ];
  return <nav className="smc-jumps" aria-label="Schnellzugriffe">{jumps.map((jump) => <button type="button" key={`${jump.label}-${jump.path}`} onClick={() => navigate(jump.path)}><span>{jump.label}<small>{jump.detail}</small></span><ChevronRight aria-hidden /></button>)}</nav>;
}

export function StartMissionControl({ density }: { density: Density }) {
  const accountUsage = useAccountUsage();
  const hostUsage = useHostUsage(7);
  const burn = useHermesSubscriptionBurn();
  const daily = useHermesRunsDaily();
  const [issueDays, setIssueDays] = useState<1 | 3 | 7>(7);
  const issues = useStartIssues(issueDays);
  const digest = useHermesTodayDigest();
  const commits = useProjectCommits();
  const inbox = useDecisionInbox();
  const health = useSystemHealth();
  const workers = useHermesWorkers();
  const board = useBoard();
  const statsConfig = useStatsConfig();
  const [mode, setMode] = useState<StartMatrixMode>("intensity");
  const now = hostUsage.data?.generated_at ?? burn.data?.now ?? daily.data?.now ?? board.data?.now ?? nowSec();
  const nowMs = now * 1000;
  const config = statsConfig.data ?? DEFAULT_STATS_CONFIG;
  const providers = useMemo(
    () => visibleAccountProviders(accountUsage.data?.providers ?? [], config),
    [accountUsage.data?.providers, config],
  );
  const capacityCards = useMemo(
    () => buildStartCapacityCards(providers, config, nowMs),
    [providers, config, nowMs],
  );
  const rows = useMemo(() => buildStartProviderRows({ providers, burn: burn.data, hostUsage: hostUsage.data, config, nowMs }), [providers, burn.data, hostUsage.data, config, nowMs]);
  const today = daily.data?.series.at(-1);
  const flow = startFlowFromToday(today);
  const verified = digest.data?.items.filter((item) => item.verification_state === "approved").length ?? 0;
  const causes = aggregateStartIssueCauses(issues.data);
  const overall = health.data?.overall;
  const liveWorkers = workers.data?.workers.length ?? 0;
  const boardCounts = useMemo(
    () => flowCounts(board.data?.columns.flatMap((column) => column.tasks) ?? []),
    [board.data],
  );

  return (
    <div data-start-mission-control className={cn("smc-root", density === "compact" && "is-compact")}>
      <div className="smc-context">
        <span><Eyebrow>{DATE_FORMAT.format(new Date(nowMs))}</Eyebrow><small>bis {new Date(nowMs).toLocaleTimeString("de-DE", { hour: "2-digit", minute: "2-digit" })}</small></span>
        <span className="smc-context-status"><i className={overall === "healthy" ? "is-ok" : overall === "degraded" ? "is-warn" : "is-alert"} />{overall === "healthy" ? "System gesund" : overall === "degraded" ? "System beeinträchtigt" : overall ? "System offline" : "System wird geprüft"}</span>
      </div>

      <div className="smc-instruments" aria-label="Tagesstatus">
        <div><span>Sessions</span><strong>{hostUsage.data?.active_tmux_panes ?? 0} tmux · {liveWorkers} Worker</strong></div>
        <div><span>Blockaden</span><strong className={boardCounts.blocked ? "is-warn" : undefined}>{boardCounts.blocked} offen</strong></div>
        <div><span>Probleme</span><strong className={flow.failed ? "is-warn" : undefined}>{flow.failed} heute</strong></div>
        <div><span>Geliefert</span><strong>{flow.delivered} Vorhaben</strong></div>
      </div>

      <div className="smc-hero-grid">
        <ProviderMatrixPanel rows={rows} mode={mode} onMode={setMode} loading={hostUsage.loading && !hostUsage.data} error={hostUsage.error} stale={Boolean(hostUsage.isStale)} lastUpdated={hostUsage.lastUpdated} scope={hostUsage.data} />
        <CapacityPanel cards={capacityCards} loading={accountUsage.loading && !accountUsage.data} error={accountUsage.error} />
      </div>

      <div className="smc-analysis-grid">
        <DayFlowPanel flow={flow} verified={verified} loading={daily.loading && !daily.data} error={daily.error} />
        <IssuesPanel data={issues.data} causes={causes} days={issueDays} onDays={setIssueDays} loading={issues.loading && !issues.data} error={issues.error} />
        <CommitsPanel commits={commits.data} loading={commits.loading && !commits.data} error={commits.error} now={now} />
      </div>

      <div className="smc-bottom-grid">
        <ActionsPanel inbox={inbox} loading={inbox.loading && inbox.items.length === 0} />
        <DeliveriesPanel digest={digest.data} loading={digest.loading && !digest.data} error={digest.error} />
      </div>

      <QuickJumps blocked={boardCounts.blocked} delivered={flow.delivered} providers={capacityCards.filter((card) => card.percent != null).length} />
    </div>
  );
}
