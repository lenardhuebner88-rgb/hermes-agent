import { useEffect, useState } from "react";

import { fetchJSON } from "../../../lib/api";
import { fmtAge } from "../../lib/derive";

const POLL_MS = 30_000;

interface ReleaseStatusEvent {
  task_id: string;
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
export function AutoReleaseTile() {
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
              <div key={`${ev.task_id}-${index}`} className="flex min-w-0 items-center gap-2 text-[11px]">
                <span className={`shrink-0 rounded-lg border px-2 py-0.5 ${toneClass}`}>{outcome}</span>
                <span className="min-w-0 flex-1 truncate font-mono text-ink-3" title={ev.task_id}>{ev.task_id}</span>
                <span className="shrink-0 font-mono text-ink-3">{`vor ${fmtAge(ev.created_at)}`}</span>
              </div>
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
