/**
 * RisikoHero — Zone 1 "Auto-Mode Cockpit" (★ FINAL, Design-Board c_2103a234).
 *
 * Master-Kill-Switch (release.autonomous) + Reichweite-Segment
 * (max_tier_autonomous) + Stepper "Max. Worker gesamt" (kanban.max_in_progress,
 * der globale Deckel) + gekoppelter Stepper "Parallele Worker pro Profil"
 * (2026-07-08: schreibt kanban.max_in_progress_per_profile +
 * kanban.max_concurrent_per_repo mit demselben N) + read-only
 * Sicherheitsnetz-Zeile (pause_on_red_streak / red_streak). Kein
 * Per-Profil-Matrix (Piet 2026-07-07 verworfen) — ein skalarer Regler.
 *
 * Live-wired gegen GET/POST /api/plugins/kanban/release-mode (AD-S4 +
 * 2026-07-08 Follow-up: max_tier_autonomous-Write, red_streak,
 * max_in_progress, max_in_progress_per_profile, max_concurrent_per_repo,
 * serialize_by_repo) und POST /api/plugins/kanban/release-concurrency (setzt
 * alle drei Concurrency-Felder, einzeln oder gekoppelt). Kein Stub mehr —
 * alle POSTs schreiben wirklich; die Buttons sind nur WÄHREND eines
 * laufenden Writes disabled (Doppelklick-Schutz), nicht mehr grundsätzlich.
 *
 * ⚠️ Divergenz ggü. dem Mockup: die HTML-Vorlage zeigt Reichweite-Segmente
 * "review · high · critical" — der reale Backend-Tier-Enum (auto_release.py
 * `_TIER_ORDER`) kennt aber nur `standard · review · critical`; "high" existiert
 * serverseitig nicht. Gebaut gegen den echten Enum statt die Fantasie-Stufe zu
 * übernehmen.
 */
import { useReleaseConcurrencyWrite, useReleaseModeWrite } from "../../hooks/systemReleaseHealth";
import { de } from "../../i18n/de";
import type { ReleaseModeResponse, ReleaseTier } from "../../lib/schemas";

const TIERS: readonly ReleaseTier[] = ["standard", "review", "critical"];

export interface RisikoHeroProps {
  releaseMode: ReleaseModeResponse | null;
  /** Nach einem erfolgreichen Write aufgerufen — lädt release-mode neu, damit
   *  die Anzeige den persistierten Config-Stand zeigt (optimistic-then-refetch). */
  onReleaseModeChanged?: () => void | Promise<void>;
}

export function RisikoHero({ releaseMode, onReleaseModeChanged }: RisikoHeroProps) {
  const modeWrite = useReleaseModeWrite();
  const concurrencyWrite = useReleaseConcurrencyWrite();

  const autonomous = releaseMode?.autonomous ?? false;
  const tier: ReleaseTier = releaseMode?.max_tier_autonomous ?? "review";
  const pauseOnRedStreak = releaseMode?.pause_on_red_streak;
  const redStreak = releaseMode?.red_streak ?? 0;
  const maxInProgress = releaseMode?.max_in_progress ?? null;
  const maxInProgressCeiling = maxInProgress ?? 1; // clamp math only, never displayed
  // max_in_progress_per_profile's real config default is unlimited (null) —
  // fall back to max_concurrent_per_repo (default 1) as the effective
  // displayed floor rather than faking "1".
  const maxConcurrentPerRepo = releaseMode?.max_concurrent_per_repo ?? 1;
  const parallelPerProfile = releaseMode?.max_in_progress_per_profile ?? maxConcurrentPerRepo;

  async function handleToggle() {
    const res = await modeWrite.run({ autonomous: !autonomous });
    if (res.ok) void onReleaseModeChanged?.();
  }

  async function handleTier(next: ReleaseTier) {
    if (next === tier || modeWrite.busy) return;
    const res = await modeWrite.run({ max_tier_autonomous: next });
    if (res.ok) void onReleaseModeChanged?.();
  }

  async function handleStep(delta: number) {
    const next = Math.max(1, maxInProgressCeiling + delta);
    if (next === maxInProgress || concurrencyWrite.busy) return;
    const res = await concurrencyWrite.run({ max_in_progress: next });
    if (res.ok) void onReleaseModeChanged?.();
  }

  async function handleParallelStep(delta: number) {
    const next = Math.max(1, Math.min(maxInProgressCeiling, parallelPerProfile + delta));
    if (next === parallelPerProfile || concurrencyWrite.busy) return;
    const res = await concurrencyWrite.run({
      max_in_progress_per_profile: next,
      max_concurrent_per_repo: next,
    });
    if (res.ok) void onReleaseModeChanged?.();
  }

  return (
    <section
      className={`risiko-v4 rk-console ${autonomous ? "rk-console-on" : "rk-console-off"}`}
      aria-label="Auto-Mode Cockpit"
    >
      <div className="rk-console-top">
        <div className="rk-console-state">
          <span className="rk-console-state-dot" aria-hidden="true" />
          <div>
            <div className="rk-console-state-txt">{autonomous ? "AUTONOM" : "Kill-Switch AUS"}</div>
            <div className="rk-console-sub">
              {autonomous ? "deployt grüne Arbeit selbst · Guards aktiv" : "nichts wird autonom deployt"}
            </div>
          </div>
        </div>
        <button
          type="button"
          role="switch"
          aria-checked={autonomous}
          aria-label="Autonomie-Kill-Switch"
          disabled={modeWrite.busy}
          className="rk-switch"
          onClick={() => { void handleToggle(); }}
        >
          <span className="rk-switch-knob" aria-hidden="true" />
        </button>
      </div>

      <div className="rk-console-controls">
        <div className="rk-ctl-row">
          <div className="rk-ctl-label">
            <span className="rk-ctl-cap">Reichweite</span>
            <span className="rk-ctl-hint">wie weit Autonomie reicht, bevor sie eskaliert</span>
          </div>
          <div className="rk-seg" role="group" aria-label="Reichweite">
            {TIERS.map((t) => (
              <button
                key={t}
                type="button"
                disabled={modeWrite.busy}
                aria-pressed={t === tier}
                className={`rk-seg-opt ${t === tier ? "rk-seg-opt-on" : ""}`}
                onClick={() => { void handleTier(t); }}
              >
                {t}
              </button>
            ))}
          </div>
        </div>
        <div className="rk-ctl-row">
          <div className="rk-ctl-label">
            <span className="rk-ctl-cap">{de.fleet.risikoMaxWorkerGesamtLabel}</span>
            <span className="rk-ctl-hint">wie viele Profile gleichzeitig arbeiten dürfen</span>
          </div>
          <div className="rk-stepper">
            <button
              type="button"
              disabled={concurrencyWrite.busy || maxInProgressCeiling <= 1}
              aria-label="weniger Worker gesamt"
              className="rk-step-btn"
              onClick={() => { void handleStep(-1); }}
            >
              −
            </button>
            <span className="rk-step-val">{maxInProgress ?? "—"}</span>
            <button
              type="button"
              disabled={concurrencyWrite.busy}
              aria-label="mehr Worker gesamt"
              className="rk-step-btn"
              onClick={() => { void handleStep(1); }}
            >
              +
            </button>
          </div>
        </div>
        <div className="rk-ctl-row">
          <div className="rk-ctl-label">
            <span className="rk-ctl-cap">{de.fleet.risikoParallelWorkerLabel}</span>
            <span className="rk-ctl-hint">{de.fleet.risikoParallelWorkerHint}</span>
          </div>
          <div className="rk-stepper">
            <button
              type="button"
              disabled={concurrencyWrite.busy || parallelPerProfile <= 1}
              aria-label="weniger parallele Worker pro Profil"
              className="rk-step-btn"
              onClick={() => { void handleParallelStep(-1); }}
            >
              −
            </button>
            <span className="rk-step-val">{parallelPerProfile}</span>
            <button
              type="button"
              disabled={concurrencyWrite.busy || parallelPerProfile >= maxInProgressCeiling}
              aria-label="mehr parallele Worker pro Profil"
              className="rk-step-btn"
              onClick={() => { void handleParallelStep(1); }}
            >
              +
            </button>
          </div>
        </div>
        <p className="rk-ctl-subhint">
          {parallelPerProfile > 1
            ? de.fleet.risikoParallelWorkerStaleMainHint
            : de.fleet.risikoParallelWorkerStrictHint}
        </p>
      </div>

      {modeWrite.error ? <p className="rk-write-error" role="alert">{modeWrite.error}</p> : null}
      {concurrencyWrite.error ? <p className="rk-write-error" role="alert">{concurrencyWrite.error}</p> : null}

      <div className="rk-safety">
        <span className="rk-safety-ico" aria-hidden="true">🛡</span>
        {pauseOnRedStreak != null && pauseOnRedStreak > 0 ? (
          <span className="rk-safety-txt">
            Auto-Stopp nach <b>{pauseOnRedStreak} roten</b> in Folge · Streak <b>{redStreak}/{pauseOnRedStreak}</b>
          </span>
        ) : (
          <span className="rk-safety-txt">
            Guards aktiv — kein Auto-Stopp-Schwellenwert konfiguriert
          </span>
        )}
      </div>
    </section>
  );
}
