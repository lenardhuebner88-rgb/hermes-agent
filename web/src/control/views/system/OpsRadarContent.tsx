import { Bot, GitBranch, Radar, ShieldCheck, TriangleAlert, Wrench } from "lucide-react";
import { StaleBadge } from "../../components/atoms";
import { Card, Eyebrow, Panel, SkeletonCard } from "../../components/primitives";
import { KpiTile, SignalChip, SignalLabel, signalToneFromLegacy } from "../../components/leitstand";
import type { OperatorInventoryActor, OperatorInventoryResponse, OperatorInventoryWorktree, OperatorInventoryWorktreeState, ToneName } from "../../lib/types";

// OpsRadarContent lebt seit dem Abriss (S5) hier unter views/system/, weil die
// eigenständige OpsRadar-Route zum System-Redirect wurde. Die System-View
// bettet diesen Inhalt als "Worktrees · Akteure"-Sektion ein.

function fmtNumber(value: number | null | undefined): string {
  if (value == null || Number.isNaN(value)) return "-";
  return String(Math.round(value));
}

function fmtPercent(value: number | null | undefined): string {
  if (value == null || Number.isNaN(value)) return "-";
  return `${Math.round(value)}%`;
}

function fmtMb(value: number | null | undefined): string {
  if (value == null || Number.isNaN(value)) return "-";
  return value >= 1024 ? `${(value / 1024).toFixed(1)}GB` : `${Math.round(value)}MB`;
}

function fmtAge(seconds: number | null | undefined): string {
  if (seconds == null || Number.isNaN(seconds)) return "-";
  if (seconds < 60) return `${Math.max(0, Math.round(seconds))}s`;
  if (seconds < 3600) return `${Math.round(seconds / 60)}m`;
  if (seconds < 86400) return `${Math.round(seconds / 3600)}h`;
  return `${Math.round(seconds / 86400)}d`;
}

function stateTone(state: OperatorInventoryWorktreeState, orphaned: boolean): ToneName {
  if (orphaned) return "rose";
  if (state === "dirty" || state === "prunable") return "amber";
  if (state === "locked") return "cyan";
  if (state === "clean") return "emerald";
  return "zinc";
}

function leverDot(tone: ToneName) {
  if (tone === "red" || tone === "rose") return "error";
  if (tone === "amber") return "warn";
  if (tone === "emerald") return "ready";
  return "idle";
}

function worktreeRank(item: OperatorInventoryWorktree): number {
  if (item.orphaned) return 0;
  if (item.state === "dirty") return 1;
  if (item.state === "prunable") return 2;
  if (item.state === "locked") return 3;
  if (item.state === "unknown") return 4;
  return 5;
}

function WorktreeRow({ item }: { item: OperatorInventoryWorktree }) {
  const tone = stateTone(item.state, item.orphaned);
  return (
    <div className="grid min-w-0 gap-2 rounded-card border border-line bg-surface-2 px-3 py-3 lg:grid-cols-[minmax(0,1fr)_auto] lg:items-center">
      <div className="min-w-0">
        <div className="flex min-w-0 flex-wrap items-center gap-2">
          <SignalChip tone={signalToneFromLegacy(tone)} label={item.orphaned ? "orphan" : item.state} />
          <span className="truncate text-sec font-medium text-ink">{item.path_label}</span>
          <span className="text-micro text-ink-2">{item.relation}</span>
        </div>
        <p className="mt-1 truncate font-data text-sec text-ink-2">{item.branch}{item.task_hint ? ` - ${item.task_hint}` : ""}</p>
        {item.orphaned ? <p className="mt-1 text-sec text-status-alert">Kein aktiver Worker-Match fuer diesen Kanban-Worktree.</p> : null}
      </div>
      <div className="grid grid-cols-3 gap-2 lg:w-72">
        <KpiTile label="Dirty" value={item.status_checked ? fmtNumber(item.dirty_count) : "?"} dot={item.dirty_count ? "warn" : "idle"} />
        <KpiTile label="Untracked" value={item.status_checked ? fmtNumber(item.untracked_count) : "?"} />
        <KpiTile label="Lock" value={item.locked ? "ja" : "nein"} dot={item.locked ? "live" : "idle"} />
      </div>
    </div>
  );
}

function ActorRow({ actor }: { actor: OperatorInventoryActor }) {
  const tone: ToneName = actor.stale_count ? "red" : actor.source === "canonical" ? "emerald" : "cyan";
  return (
    <div className="grid min-w-0 gap-2 rounded-card border border-line bg-surface-2 px-3 py-3 lg:grid-cols-[minmax(0,1fr)_auto] lg:items-center">
      <div className="min-w-0">
        <div className="flex min-w-0 flex-wrap items-center gap-2">
          <SignalLabel tone={signalToneFromLegacy(tone)} label={actor.stale_count ? "stale" : actor.source === "canonical" ? "kanonisch" : "prozess"} />
          <span className="truncate text-sec font-medium text-ink">{actor.label}</span>
          <span className="text-micro text-ink-2">read-only</span>
        </div>
        <p className="mt-1 truncate text-sec text-ink-2">{actor.confidence} confidence{actor.stale_count ? ` - ${actor.stale_count} stale` : ""}</p>
      </div>
      <div className="grid grid-cols-4 gap-2 lg:w-80">
        <KpiTile label="Anzahl" value={actor.count} dot={tone === "red" ? "error" : tone === "emerald" ? "ready" : "idle"} />
        <KpiTile label="CPU" value={fmtPercent(actor.cpu_percent)} />
        <KpiTile label="RAM" value={fmtMb(actor.rss_mb)} />
        <KpiTile label="Aeltester" value={fmtAge(actor.oldest_age_seconds)} />
      </div>
    </div>
  );
}

export function OpsRadarContent({ data, lastUpdated, isStale, error, embedded }: {
  data: OperatorInventoryResponse | null;
  lastUpdated: number | null;
  isStale?: boolean;
  error?: string | null;
  /** In der System-Fusion (S1) trägt der geteilte Kopf die Status-Zeile + Hebel —
   *  eingebettet bleiben nur die Sektionen Worktrees + Akteure. */
  embedded?: boolean;
}) {
  if (!data) {
    return (
      <div className="space-y-4">
        {error ? <div className="flex items-start gap-2 rounded-card border border-status-warn/30 bg-status-warn/10 px-3 py-2 text-sec text-status-warn"><TriangleAlert aria-hidden className="mt-0.5 size-4 shrink-0" />Ops Radar konnte nicht geladen werden: {error}</div> : null}
        {!error ? <SkeletonCard rows={6} /> : null}
      </div>
    );
  }

  const summary = data.summary;
  const mismatchCount = summary.worktrees_dirty + summary.worktrees_orphaned + summary.worktrees_status_unknown;
  const worktrees = [...data.worktrees].sort((a, b) => worktreeRank(a) - worktreeRank(b) || a.path_label.localeCompare(b.path_label)).slice(0, 24);
  const actors = [...data.actors].sort((a, b) => (b.source === "canonical" ? 1 : 0) - (a.source === "canonical" ? 1 : 0) || b.count - a.count || a.label.localeCompare(b.label));
  const next = data.next_lever;

  return (
    <div className="space-y-4">
      {embedded ? null : (
      <Card surface="raised" className="overflow-hidden border border-line p-0" ariaLabel="Ops Radar">
        <div className="flex flex-col gap-3 border-b border-line px-4 py-3 sm:flex-row sm:items-center sm:justify-between">
          <div className="min-w-0">
            <Eyebrow>Ops Radar</Eyebrow>
            <div className="mt-1 flex min-w-0 flex-wrap items-center gap-2">
              <SignalChip tone={signalToneFromLegacy(next.tone)} label={next.label} />
              <h2 className="truncate font-display text-emph font-semibold text-ink">Worktrees und Akteure</h2>
            </div>
          </div>
          <StaleBadge isStale={isStale} lastUpdated={lastUpdated} />
        </div>

        <div className="grid grid-cols-2 gap-2 p-3 lg:grid-cols-4">
          <KpiTile icon={GitBranch} label="Worktrees" value={`${summary.worktrees_total} total`} delta={`${summary.worktrees_locked} locked - ${summary.worktrees_dirty} dirty`} dot={summary.worktrees_dirty || summary.worktrees_orphaned ? "warn" : "idle"} />
          <KpiTile icon={Bot} label="Akteure" value={String(summary.actors_total)} delta={`${summary.actors_canonical} kanonisch`} dot={summary.actors_total ? "live" : "idle"} />
          <KpiTile icon={Radar} label="Mismatch" value={String(mismatchCount)} delta={`${summary.worktrees_orphaned} orphan - ${summary.worktrees_status_unknown} unklar`} dot={mismatchCount ? "error" : "ready"} />
          <KpiTile icon={Wrench} label="Top-Hebel" value={next.label} delta={next.detail} dot={leverDot(next.tone)} />
        </div>
      </Card>
      )}

      <Panel title="Echte Hebel" eyebrow="read-only">
        <div className="grid gap-2 md:grid-cols-2 xl:grid-cols-4">
          {data.levers.map((lever) => (
            <a key={lever.action} href={lever.target} className="min-h-12 rounded-card border border-line bg-surface-2 px-3 py-3 hover:border-live hover:bg-surface-3">
              <div className="flex items-center justify-between gap-3">
                <SignalChip tone={signalToneFromLegacy(lever.tone)} label={lever.label} />
                <span className="font-data text-sec tabular-nums text-ink">{lever.count}</span>
              </div>
              <p className="mt-2 line-clamp-2 text-sec text-ink-2">{lever.detail}</p>
              <p className="mt-2 text-micro text-ink-3">read-only - keine Runtime-Mutation</p>
            </a>
          ))}
        </div>
      </Panel>

      <div className="grid gap-4 xl:grid-cols-[minmax(0,1.15fr)_minmax(0,.85fr)]">
        <Panel title="Worktree-Ledger" eyebrow="Git Inventar">
          {worktrees.length === 0 ? (
            <div className="flex min-h-20 items-center gap-3 rounded-card border border-line bg-surface-2 px-3 py-3">
              <ShieldCheck className="h-5 w-5 text-ink-3" />
              <div>
                <p className="text-sec font-medium text-ink">Keine Worktrees gemeldet</p>
                <p className="text-sec text-ink-2">Die Inventarquelle ist leer; ein Worktree-Zustand ist nicht bewertbar.</p>
              </div>
            </div>
          ) : (
            <div className="grid gap-2">{worktrees.map((item, idx) => <WorktreeRow key={`${item.id}:${item.branch}:${idx}`} item={item} />)}</div>
          )}
        </Panel>

        <Panel title="Actor Map" eyebrow="Worker, Agents, Daemons">
          {actors.length === 0 ? (
            <div className="flex min-h-20 items-center gap-3 rounded-card border border-line bg-surface-2 px-3 py-3">
              <ShieldCheck className="h-5 w-5 text-ink-3" />
              <div>
                <p className="text-sec font-medium text-ink">Keine Akteure aktiv</p>
                <p className="text-sec text-ink-2">Die Actor Map ist leer; es gibt derzeit nichts zuzuordnen.</p>
              </div>
            </div>
          ) : (
            <div className="grid gap-2">{actors.map((actor) => <ActorRow key={`${actor.role}-${actor.source}`} actor={actor} />)}</div>
          )}
        </Panel>
      </div>

      {data.errors.length > 0 ? (
        <div className="flex items-start gap-2 rounded-card border border-status-warn/30 bg-status-warn/10 px-3 py-2 text-sec text-status-warn"><TriangleAlert aria-hidden className="mt-0.5 size-4 shrink-0" />Einige Inventarwerte konnten nicht gelesen werden. Die Anzeige bleibt read-only und zeigt keine Rohpfade oder Cmdlines.</div>
      ) : null}
    </div>
  );
}
