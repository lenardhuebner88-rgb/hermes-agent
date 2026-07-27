/**
 * WorkerCard — die V2-Worker-Karte des Fleet Worker-Tabs (Mockup 2026-07-27).
 * Pro aktivem Worker eine Karte: Liveness-Orb + Titel + Run-Chip im Kopf,
 * darunter der Elapsed-Ring (Füllung gegen das p90-Fenster, p50-Marke, über
 * p90 → warn-Färbung) neben Heartbeat-Alter, aktueller Activity-Note und der
 * ehrlichen Token-Zeile („— · Werte beim Closeout" ohne Live-Stichprobe),
 * dann die Heartbeat-Sparkline aus heartbeat_ticks und die p50/p90-Skala.
 *
 * Rein präsentational: Geometrie aus elapsedRingGeometry/heartbeatBars
 * (lib/fleetHub — dieselbe Fenster-Logik wie das alte WorkerBand), Farben
 * ausschließlich aus fleet.css-Klassen (DESIGN.md Regel 9). Bronze trägt nur
 * Live-Elemente (Ring-Füllung, letzter Sparkline-Balken), das Status-Trio nur
 * den Orb/die Warn-Färbung.
 */
import {
  claimCountdownSeconds,
  elapsedRingGeometry,
  fmtClockTime,
  fmtDurationClock,
  fmtSeconds,
  fmtTokens,
  heartbeatAge,
  heartbeatBars,
  laneTint,
  workerHasLiveTokenSample,
  workerLivenessTone,
} from "../../lib/fleetHub";
import { elapsedSeconds } from "../../lib/derive";
import type { Worker } from "../../lib/types";
import { de } from "../../i18n/de";
import { BoardBadge } from "../../components/fleet/BoardIdentity";

// ─── Liveness-Orb ────────────────────────────────────────────────────────────
// Auch im Drawer wiederverwendet: Punkt im Status-Trio + Tooltip mit Grund
// und Beobachtungs-Zeitpunkt (nie farb-only — der Text steht im title und im
// Drawer daneben).

const LIVENESS_LABEL: Record<"ok" | "warn" | "alert", string> = {
  ok: de.fleet.livenessRunning,
  warn: de.fleet.livenessSuspect,
  alert: de.fleet.livenessFailed,
};

export function livenessLabel(tone: "ok" | "warn" | "alert" | null): string {
  return tone ? LIVENESS_LABEL[tone] : de.fleet.livenessUnknown;
}

export function LivenessOrb({ worker, now }: { worker: Worker; now: number }) {
  const tone = workerLivenessTone(worker, now);
  if (tone == null) return null;
  const parts = [de.fleet.livenessOrbLabel(LIVENESS_LABEL[tone])];
  if (worker.liveness_reason) parts.push(worker.liveness_reason);
  if (worker.liveness_observed_at) {
    parts.push(de.fleet.livenessObserved(fmtClockTime(worker.liveness_observed_at)));
  }
  return (
    <span
      className={`fleet-orb fleet-orb-${tone}`}
      role="img"
      aria-label={parts.join(" · ")}
      title={parts.join(" · ")}
    />
  );
}

// ─── Elapsed-Ring ────────────────────────────────────────────────────────────

function ElapsedRing({ worker, now }: { worker: Worker; now: number }) {
  const geo = elapsedRingGeometry(worker, now);
  const elapsed = elapsedSeconds(worker.started_at, now) ?? Number.NaN;
  const R = 39;
  const C = 2 * Math.PI * R;
  // F4: ohne geerdetes Fenster (source "none") ist die Füllung eine Konstante
  // (elapsed/elapsed·1.3 ≈ 77 %) — dann nur Track + Zahl, kein Fake-Bogen.
  // F3: p50/p90-Marken nur, wenn das Fenster aus echten Perzentilen stammt.
  const showFill = geo.grounded;
  const p50Angle = showFill && geo.p50Fraction != null ? geo.p50Fraction * 360 - 90 : null;
  const showP90Tick = geo.source === "p90";
  const pol = (angleDeg: number, r: number): [number, number] => {
    const rad = (angleDeg * Math.PI) / 180;
    return [44 + r * Math.cos(rad), 44 + r * Math.sin(rad)];
  };
  return (
    <span className="fleet-ring-box">
      <svg
        width="88"
        height="88"
        viewBox="0 0 88 88"
        role="img"
        aria-label={`${de.fleet.drawerLaufzeit} ${fmtDurationClock(elapsed)}`}
      >
        <circle cx="44" cy="44" r={R} className="fleet-ring-track" />
        {showFill ? (
          <circle
            cx="44"
            cy="44"
            r={R}
            className={geo.overP90 ? "fleet-ring-fill fleet-ring-fill-over" : "fleet-ring-fill"}
            strokeDasharray={`${(C * geo.fillFraction).toFixed(1)} ${C.toFixed(1)}`}
            transform="rotate(-90 44 44)"
          />
        ) : null}
        {p50Angle != null ? (
          <line
            x1={pol(p50Angle, 43)[0]}
            y1={pol(p50Angle, 43)[1]}
            x2={pol(p50Angle, 33)[0]}
            y2={pol(p50Angle, 33)[1]}
            className="fleet-ring-p50"
          />
        ) : null}
        {showP90Tick ? (
          <line
            x1={pol(-90, 43)[0]}
            y1={pol(-90, 43)[1]}
            x2={pol(-90, 33)[0]}
            y2={pol(-90, 33)[1]}
            className="fleet-ring-p90"
          />
        ) : null}
      </svg>
      <span className="fleet-ring-num">
        <span className="fleet-ring-el">{fmtDurationClock(elapsed)}</span>
        <span className="fleet-ring-cap">{de.fleet.chainStateRunning}</span>
      </span>
    </span>
  );
}

// ─── Heartbeat-Sparkline ─────────────────────────────────────────────────────

function HeartbeatSparkline({ ticks }: { ticks: number[] | undefined }) {
  const bars = heartbeatBars(ticks);
  if (bars.length < 2) return null;
  return (
    <span className="fleet-hb-bars" aria-hidden="true">
      {bars.map((h, i) => (
        <span
          key={`${i}-${ticks?.[i] ?? 0}`}
          style={{ height: `${Math.round(h * 100)}%` }}
          className={i === bars.length - 1 ? "fleet-hb-bar fleet-hb-bar-last" : "fleet-hb-bar"}
        />
      ))}
    </span>
  );
}

// ─── Fenster-Skala ───────────────────────────────────────────────────────────
// F3: die rechte Kante heißt nur „p90", wenn das Fenster ein echtes eta_p90
// ist — bei Runtime-Cap „Cap", bei p50×1.6-Hochrechnung „≈". Ohne geerdetes
// Fenster (source "none") blendet sich die Skala komplett aus.

function windowSourceLabel(source: "p90" | "cap" | "p50x1.6" | "none"): string {
  if (source === "p90") return de.fleet.windowP90Label;
  if (source === "cap") return de.fleet.windowCapLabel;
  return de.fleet.windowApproxLabel;
}

function PercentileScale({ worker, now }: { worker: Worker; now: number }) {
  const geo = elapsedRingGeometry(worker, now);
  if (!geo.grounded) return null;
  const pct = Math.round(geo.fillFraction * 1000) / 10;
  return (
    <span className="fleet-pscale" aria-hidden="true">
      <span className="fleet-pscale-track" />
      <span
        className={geo.overP90 ? "fleet-pscale-fill fleet-pscale-fill-over" : "fleet-pscale-fill"}
        style={{ width: `${pct}%` }}
      />
      {geo.p50Fraction != null ? (
        <>
          <span className="fleet-pscale-tick" style={{ left: `${(geo.p50Fraction * 100).toFixed(1)}%` }} />
          <span className="fleet-pscale-tlab" style={{ left: `${(geo.p50Fraction * 100).toFixed(1)}%` }}>p50</span>
        </>
      ) : null}
      <span className="fleet-pscale-tick fleet-pscale-tick-p90" style={{ left: "100%" }} />
      <span className="fleet-pscale-tlab" style={{ left: "100%" }}>{windowSourceLabel(geo.source)}</span>
      <span
        className={geo.overP90 ? "fleet-pscale-pos fleet-pscale-pos-over" : "fleet-pscale-pos"}
        style={{ left: `${pct}%` }}
      />
    </span>
  );
}

// ─── Worker-Karte ────────────────────────────────────────────────────────────

export function WorkerCard({
  worker: w,
  now,
  branchName = null,
  onOpen,
}: {
  worker: Worker;
  now: number;
  /** Branch aus dem Board-Payload (nur aktuelles Board) — null wenn unbekannt. */
  branchName?: string | null;
  onOpen: () => void;
}) {
  const hbAge = heartbeatAge(w.last_heartbeat_at, now);
  const tint = laneTint(w.profile);
  const model = w.active_model ?? w.effective_model ?? w.requested_model ?? null;
  const claimIn = claimCountdownSeconds(w.claim_expires, now);
  const hasTokens = workerHasLiveTokenSample(w);
  const note = (w.last_heartbeat_note ?? "").trim();

  return (
    <button
      type="button"
      className="fleet-wcard"
      onClick={onOpen}
      aria-label={de.fleet.workerOpenAria(w.profile)}
    >
      <span className="fleet-wcard-head">
        <LivenessOrb worker={w} now={now} />
        <span className="fleet-wcard-title" title={w.task_title}>{w.task_title}</span>
        <span className="fleet-wchip fleet-wchip-run">#{w.run_id}</span>
      </span>
      <span className="fleet-wchips">
        <span className={`fleet-wchip fleet-wchip-tint-${tint}`}>{w.profile}</span>
        {model ? (
          <span className="fleet-wchip">
            {model}{w.model_state ? ` · ${w.model_state}` : ""}
          </span>
        ) : null}
        <BoardBadge slug={w.board_slug} />
      </span>
      <span className="fleet-wcard-body">
        <ElapsedRing worker={w} now={now} />
        <span className="fleet-wstats">
          <span className="fleet-wstat">
            <span className="fleet-wstat-k">{de.fleet.drawerHeartbeat}</span>
            <span className="fleet-wstat-v">{hbAge != null ? fmtSeconds(hbAge) : "—"}</span>
          </span>
          {note ? (
            <span className="fleet-wstat">
              <span className="fleet-wstat-note" title={note}>{note}</span>
            </span>
          ) : null}
          <span className="fleet-wstat">
            <span className="fleet-wstat-k">{de.fleet.drawerTokens}</span>
            {hasTokens ? (
              <span className="fleet-wstat-v">{fmtTokens(w.input_tokens)} → {fmtTokens(w.output_tokens)}</span>
            ) : (
              <span className="fleet-wstat-v fleet-wstat-dim">— · {de.fleet.pulseTokensCloseout}</span>
            )}
          </span>
        </span>
      </span>
      <HeartbeatSparkline ticks={w.heartbeat_ticks} />
      <PercentileScale worker={w} now={now} />
      {branchName || claimIn != null ? (
        <span className="fleet-wchips">
          {branchName ? <span className="fleet-wchip fleet-wchip-mono">{branchName}</span> : null}
          {claimIn != null ? (
            <span className="fleet-wchip">
              {de.fleet.claimChipPrefix} {claimIn > 0 ? fmtDurationClock(claimIn) : de.fleet.claimExpired}
            </span>
          ) : null}
        </span>
      ) : null}
    </button>
  );
}
