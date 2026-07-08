/**
 * RisikoHero — Zone 1 "Auto-Mode Cockpit" (★ FINAL, Design-Board c_2103a234).
 *
 * Master-Kill-Switch (release.autonomous) + Reichweite-Segment
 * (max_tier_autonomous) + EIN Stepper "Max. parallele Profile"
 * (kanban.max_in_progress) + read-only Sicherheitsnetz-Zeile
 * (pause_on_red_streak / red_streak). Kein Per-Profil-Matrix (Piet
 * 2026-07-07 verworfen).
 *
 * Live-wired gegen GET/POST /api/plugins/kanban/release-mode (AD-S4 +
 * 2026-07-08 Follow-up: max_tier_autonomous-Write, red_streak,
 * max_in_progress) und POST /api/plugins/kanban/release-concurrency (neuer
 * kanban.max_in_progress-Setter). Kein Stub mehr — beide POSTs schreiben
 * wirklich; die Buttons sind nur WÄHREND eines laufenden Writes disabled
 * (Doppelklick-Schutz), nicht mehr grundsätzlich.
 *
 * ⚠️ Divergenz ggü. dem Mockup: die HTML-Vorlage zeigt Reichweite-Segmente
 * "review · high · critical" — der reale Backend-Tier-Enum (auto_release.py
 * `_TIER_ORDER`) kennt aber nur `standard · review · critical`; "high" existiert
 * serverseitig nicht. Gebaut gegen den echten Enum statt die Fantasie-Stufe zu
 * übernehmen.
 */
import { useReleaseConcurrencyWrite, useReleaseModeWrite } from "../../hooks/useControlData";
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
    const next = Math.max(1, (maxInProgress ?? 1) + delta);
    if (next === maxInProgress || concurrencyWrite.busy) return;
    const res = await concurrencyWrite.run(next);
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
            <span className="rk-ctl-cap">Max. parallele Profile</span>
            <span className="rk-ctl-hint">wie viele Profile gleichzeitig arbeiten dürfen</span>
          </div>
          <div className="rk-stepper">
            <button
              type="button"
              disabled={concurrencyWrite.busy || (maxInProgress ?? 1) <= 1}
              aria-label="weniger parallele Profile"
              className="rk-step-btn"
              onClick={() => { void handleStep(-1); }}
            >
              −
            </button>
            <span className="rk-step-val">{maxInProgress ?? "—"}</span>
            <button
              type="button"
              disabled={concurrencyWrite.busy}
              aria-label="mehr parallele Profile"
              className="rk-step-btn"
              onClick={() => { void handleStep(1); }}
            >
              +
            </button>
          </div>
        </div>
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
