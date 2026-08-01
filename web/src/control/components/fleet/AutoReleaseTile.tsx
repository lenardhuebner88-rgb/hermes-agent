import { useEffect, useState } from "react";

import { fetchJSON } from "../../../lib/api";
import { fmtRelativeTime } from "../../lib/derive";

const POLL_MS = 30_000;

interface ReleaseStatusEvent {
  task_id: string;
  task_title: string;
  created_at: number;
  payload: { outcome?: string; detail?: string; [key: string]: unknown };
}

interface ReleaseStatusResponse {
  autonomous: boolean;
  max_tier_autonomous: string;
  recent: ReleaseStatusEvent[];
  anchors: string[];
}

// Outcome → Ton (siehe hermes_cli/auto_release.py release_chain): deployed ist
// positiv, rolled_back negativ, held_* wartet auf Operator (amber). Alles
// andere (deploy_failed, aborted_pre_live_test, …) bleibt neutral statt
// geraten-farbig.
const OUTCOME_TONE: Record<string, string> = {
  deployed: "border-status-ok/30 bg-status-ok/10 text-status-ok",
  rolled_back: "border-status-alert/30 bg-status-alert/10 text-status-alert",
  held_critical: "border-status-warn/30 bg-status-warn/10 text-status-warn",
  held_live_test: "border-status-warn/30 bg-status-warn/10 text-status-warn",
};
const OUTCOME_TONE_DEFAULT = "border-line bg-surface-2 text-ink-3";

/** Read-only Auto-Release-Statuskachel — kein Toggle (Freischalten bleibt ein
 * Config-Datei-Akt), speist sich aus GET /api/plugins/kanban/release-status. */
export function AutoReleaseTile({ onOpenTask, compact = false }: { onOpenTask?: (taskId: string) => void; compact?: boolean }) {
  const [data, setData] = useState<ReleaseStatusResponse | null>(null);
  const [error, setError] = useState<string | null>(null);

  useEffect(() => {
    let alive = true;
    async function load() {
      try {
        const result = await fetchJSON<ReleaseStatusResponse>("/api/plugins/kanban/release-status");
        if (!alive) return;
        setData(result);
        setError(null);
      } catch (e: unknown) {
        if (!alive) return;
        setError(e instanceof Error ? e.message : String(e));
      }
    }
    void load();
    const id = window.setInterval(() => void load(), POLL_MS);
    return () => {
      alive = false;
      window.clearInterval(id);
    };
  }, []);

  const recent = (data?.recent ?? []).slice(0, 5);
  const anchors = data?.anchors ?? [];
  const lastAnchor = anchors.length > 0 ? anchors[anchors.length - 1] : null;

  // Neuestes gehaltenes Release-Ereignis (generisch ueber held_*), gesucht in
  // der ungekuerzten Liste — ein juengeres deployed-Ereignis verdrängt den
  // geparkten Gate-Hinweis nicht. Endpoint liefert neueste zuerst.
  const held = (data?.recent ?? []).find(
    (ev) => typeof ev.payload?.outcome === "string" && ev.payload.outcome.startsWith("held_"),
  );

  if (compact) {
    if (held) {
      const heldOutcome = typeof held.payload?.outcome === "string" ? held.payload.outcome : "unbekannt";
      const heldTitle = held.task_title?.trim() ? held.task_title : held.task_id;
      const heldDetail = typeof held.payload?.detail === "string" && held.payload.detail.trim()
        ? held.payload.detail
        : heldOutcome;
      const heldLine = `${held.task_id} · ${fmtRelativeTime(held.created_at)} · ${heldDetail}`;
      return (
        <div
          className="fleet-auto-release-compact flex-wrap py-1.5"
          role="status"
          aria-label={`Auto-Release-Status, Kette gehalten: ${heldTitle}`}
        >
          <span>Auto-Release</span>
          {/* div statt span: die Mobile-Regel in fleet.css blendet span/small im
              Compact-Tile aus; der Gehalten-Hinweis muss bei 390px sichtbar bleiben. */}
          <div className="grid min-w-0 flex-1 basis-40 gap-0.5 border-l border-status-warn/40 pl-2">
            <div className="flex min-w-0 items-center gap-1.5">
              <div className="shrink-0 rounded border border-status-warn/30 bg-status-warn/10 px-1.5 text-status-warn">
                Gehalten
              </div>
              <div className="min-w-0 truncate text-ink-2" title={heldTitle}>{heldTitle}</div>
            </div>
            <div className="flex min-w-0 items-baseline gap-1.5" title={heldLine}>
              <div className="shrink-0 font-mono text-ink-3">{`${held.task_id} · ${fmtRelativeTime(held.created_at)} ·`}</div>
              <div className="min-w-0 truncate text-ink-3">{heldDetail}</div>
            </div>
          </div>
        </div>
      );
    }
    return (
      <div className="fleet-auto-release-compact" role="status" aria-label="Auto-Release-Status">
        <span>Auto-Release</span>
        <strong>
          {data
            ? data.autonomous ? `AUTONOM ≤ ${data.max_tier_autonomous}` : "Kill-Switch AUS"
            : error ? "nicht erreichbar" : "lädt …"}
        </strong>
        {recent.length > 0 ? <small>{recent.length} letzte</small> : null}
      </div>
    );
  }

  return (
    <section className="mb-3 grid min-w-0 gap-3 rounded-lg border border-line bg-surface-1 p-3 text-ink">
      <div className="flex min-w-0 items-center justify-between gap-3">
        <span className="text-xs font-semibold text-ink-2">Auto-Release</span>
        {data ? (
          data.autonomous ? (
            <span className="inline-flex max-w-full items-center gap-1 truncate rounded-lg border border-status-ok/30 bg-status-ok/10 px-2 py-1 text-[11px] text-status-ok">
              {`AUTONOM (≤ ${data.max_tier_autonomous})`}
            </span>
          ) : (
            <span className="inline-flex max-w-full items-center gap-1 truncate rounded-lg border border-line bg-surface-2 px-2 py-1 text-[11px] text-ink-3">
              Kill-Switch AUS
            </span>
          )
        ) : null}
      </div>

      {data === null && error ? (
        <p className="text-xs text-ink-3">Status nicht erreichbar</p>
      ) : recent.length === 0 ? (
        <p className="text-xs text-ink-3">Noch keine autonomen Releases</p>
      ) : (
        <div className="grid min-w-0 gap-1.5">
          {recent.map((ev, index) => {
            const outcome = typeof ev.payload?.outcome === "string" ? ev.payload.outcome : "unbekannt";
            const toneClass = OUTCOME_TONE[outcome] ?? OUTCOME_TONE_DEFAULT;
            return (
              <button
                key={`${ev.task_id}-${index}`}
                type="button"
                onClick={() => onOpenTask?.(ev.task_id)}
                className="flex min-w-0 items-center gap-2 rounded-md text-left text-[11px] hover:bg-surface-2 focus-visible:outline focus-visible:outline-2 focus-visible:outline-accent"
                aria-label={`${ev.task_title} (${ev.task_id}) öffnen`}
              >
                <span className={`shrink-0 rounded-lg border px-2 py-0.5 ${toneClass}`}>{outcome}</span>
                <span className="min-w-0 flex-1 truncate text-ink-2" title={ev.task_title}>{ev.task_title}</span>
                <span className="shrink-0 font-mono text-ink-3">{ev.task_id}</span>
                <span className="shrink-0 font-mono text-ink-3">{fmtRelativeTime(ev.created_at)}</span>
              </button>
            );
          })}
        </div>
      )}

      {lastAnchor ? (
        <p className="min-w-0 truncate font-mono text-[11px] text-ink-3" title={`Anker: ${lastAnchor}`}>{`Anker: ${lastAnchor}`}</p>
      ) : null}
    </section>
  );
}
