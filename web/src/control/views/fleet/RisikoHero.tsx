/**
 * RisikoHero — Zone 1 "Auto-Mode Cockpit" (★ FINAL, Design-Board c_2103a234).
 *
 * Master-Kill-Switch (release.autonomous) + Reichweite-Segment
 * (max_tier_autonomous) + EIN Stepper "Max. parallele Profile"
 * (kanban.max_in_progress) + read-only Sicherheitsnetz-Zeile
 * (pause_on_red_streak). Kein Per-Profil-Matrix (Piet 2026-07-07 verworfen).
 *
 * Alle drei Kontrollen sind READ-wired gegen echte Endpoints (release-status,
 * workers.cap); die WRITE-Seite (AD-S4 Toggle/Tier-Endpoint, ein neuer
 * kanban.max_in_progress-Setter) ist noch nicht gemerged — Controls sind daher
 * disabled mit "Backend folgt"-Hinweis statt einen nicht existierenden Endpoint
 * zu erfinden. Zwei stub-Funktionen markieren die Verdrahtungs-Naht.
 *
 * ⚠️ Divergenz ggü. dem Mockup: die HTML-Vorlage zeigt Reichweite-Segmente
 * "review · high · critical" — der reale Backend-Tier-Enum (auto_release.py
 * `_TIER_ORDER`) kennt aber nur `standard · review · critical`; "high" existiert
 * serverseitig nicht. Gebaut gegen den echten Enum statt die Fantasie-Stufe zu
 * übernehmen.
 */
import type { ReleaseStatusResponse } from "../../lib/schemas";

const TIERS = ["standard", "review", "critical"] as const;

// TODO(AD-S4): wire once POST /api/plugins/kanban/release-mode lands
// (t_3816e7fc, held). Body shape per handoff: {autonomous?, max_tier_autonomous?}.
// No-op today — the control stays read-only + disabled until this seam is filled.
async function writeReleaseMode(_next: { autonomous?: boolean; max_tier_autonomous?: string }): Promise<void> {
  return Promise.resolve();
}

// TODO(max_in_progress setter): wire once an atomic config-write endpoint for
// kanban.max_in_progress lands (not yet merged — anti-scope for this build, no
// backend touches). No-op today — the stepper stays read-only + disabled.
async function writeConcurrency(_next: number): Promise<void> {
  return Promise.resolve();
}

export interface RisikoHeroProps {
  releaseStatus: ReleaseStatusResponse | null;
  /** kanban.max_in_progress — GET /workers `cap` (F4), null = unconfigured. */
  cap: number | null;
}

export function RisikoHero({ releaseStatus, cap }: RisikoHeroProps) {
  const autonomous = releaseStatus?.autonomous ?? false;
  const tier = releaseStatus?.max_tier_autonomous ?? "review";
  const streak = releaseStatus?.pause_on_red_streak;
  const backendTodo = "Backend folgt (AD-S4)";

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
          disabled
          title={backendTodo}
          className="rk-switch"
          onClick={() => { void writeReleaseMode({ autonomous: !autonomous }); }}
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
          <div className="rk-seg" role="group" aria-label="Reichweite" title={backendTodo}>
            {TIERS.map((t) => (
              <button
                key={t}
                type="button"
                disabled
                aria-pressed={t === tier}
                className={`rk-seg-opt ${t === tier ? "rk-seg-opt-on" : ""}`}
                onClick={() => { void writeReleaseMode({ max_tier_autonomous: t }); }}
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
          <div className="rk-stepper" title={backendTodo}>
            <button
              type="button"
              disabled
              aria-label="weniger parallele Profile"
              className="rk-step-btn"
              onClick={() => { void writeConcurrency(Math.max(1, (cap ?? 1) - 1)); }}
            >
              −
            </button>
            <span className="rk-step-val">{cap ?? "—"}</span>
            <button
              type="button"
              disabled
              aria-label="mehr parallele Profile"
              className="rk-step-btn"
              onClick={() => { void writeConcurrency((cap ?? 0) + 1); }}
            >
              +
            </button>
          </div>
        </div>
      </div>

      <div className="rk-safety">
        <span className="rk-safety-ico" aria-hidden="true">🛡</span>
        {streak != null ? (
          <span className="rk-safety-txt">
            Auto-Stopp nach <b>{streak} roten</b> in Folge
          </span>
        ) : (
          <span className="rk-safety-txt">
            Guards aktiv — Streak-Stand noch nicht vom Backend exponiert
          </span>
        )}
      </div>
    </section>
  );
}
