import { FleetEmptyState, FleetPanel, SignalLabel, type SignalTone } from "../../components/leitstand";
import { useHermesFleetAgentHistory } from "../../hooks/runsDigestRollup";
import { fmtUsd } from "../../lib/fleetHub";

function errorTone(rate: number): SignalTone {
  if (rate === 0) return "ok";
  if (rate <= 0.1) return "warn";
  return "alert";
}

function formatPercent(rate: number): string {
  return `${(rate * 100).toLocaleString("de-DE", { maximumFractionDigits: 1 })} %`;
}

function formatDay(date: string): string {
  const [, month, day] = date.split("-");
  return `${day}.${month}.`;
}

export function AgentHistoryPanel({ profile }: { profile: string }) {
  const history = useHermesFleetAgentHistory(profile);
  const points = history.data?.series ?? [];
  const maxRuns = Math.max(1, ...points.map((point) => point.runs));

  return (
    <div role="region" aria-label="Agent-Verlauf (30 Tage)">
      <FleetPanel eyebrow="Agent-Verlauf (30 Tage)" meta={profile}>
        {history.loading && history.data == null ? (
          <div role="status" className="rounded-card border border-line bg-surface-2 px-3 py-2 text-sec text-ink-2">
            Agent-Verlauf wird geladen …
          </div>
        ) : history.error && history.data == null ? (
          <div role="alert" className="rounded-card border border-status-alert/30 bg-status-alert/10 px-3 py-2 text-sec text-status-alert">
            Agent-Verlauf konnte nicht geladen werden.
          </div>
        ) : points.length === 0 ? (
          <FleetEmptyState
            title="Kein Verlauf in den letzten 30 Tagen"
            desc="Für dieses Profil liegen in diesem Zeitraum keine Läufe vor."
          />
        ) : (
          <div className="space-y-3">
            <div aria-label="Summen Agent-Verlauf" className="grid grid-cols-3 gap-2 rounded-card border border-line bg-surface-2 p-2.5">
              <div className="min-w-0">
                <div className="font-display text-micro uppercase tracking-[0.08em] text-ink-3">Läufe gesamt</div>
                <div className="mt-1 font-data text-sec font-semibold tabular-nums text-ink">{history.data?.totals.runs}</div>
              </div>
              <div className="min-w-0">
                <div className="font-display text-micro uppercase tracking-[0.08em] text-ink-3">Fehlerquote gesamt</div>
                <SignalLabel
                  className="mt-1 font-data tabular-nums"
                  tone={errorTone(history.data?.totals.error_rate ?? 0)}
                  label={formatPercent(history.data?.totals.error_rate ?? 0)}
                />
              </div>
              <div className="min-w-0 text-right">
                <div className="font-display text-micro uppercase tracking-[0.08em] text-ink-3">Kosten gesamt</div>
                <div className="mt-1 font-data text-sec font-semibold tabular-nums text-ink">{fmtUsd(history.data?.totals.cost_usd)}</div>
              </div>
            </div>

            <div className="space-y-1.5">
              {points.map((point) => (
                <div
                  key={point.date}
                  aria-label={`Verlauf ${point.date}`}
                  className="grid min-w-0 grid-cols-[minmax(4.5rem,1fr)_auto_auto_auto] items-center gap-2 rounded-card border border-line-soft bg-surface-2 px-2.5 py-2"
                >
                  <div className="min-w-0">
                    <div className="font-data text-micro tabular-nums text-ink-2">{formatDay(point.date)}</div>
                    <div className="mt-1 h-1.5 overflow-hidden rounded-full bg-surface-1" aria-hidden>
                      <div className="h-full rounded-full bg-data-2" style={{ width: `${Math.max(5, (point.runs / maxRuns) * 100)}%` }} />
                    </div>
                  </div>
                  <span className="whitespace-nowrap font-data text-micro tabular-nums text-ink">
                    {point.runs} {point.runs === 1 ? "Lauf" : "Läufe"}
                  </span>
                  <SignalLabel
                    className="whitespace-nowrap font-data tabular-nums"
                    tone={errorTone(point.error_rate)}
                    label={`Fehler ${formatPercent(point.error_rate)}`}
                  />
                  <span className="whitespace-nowrap text-right font-data text-micro tabular-nums text-ink-2">
                    {fmtUsd(point.cost_usd)}
                  </span>
                </div>
              ))}
            </div>
          </div>
        )}
      </FleetPanel>
    </div>
  );
}
