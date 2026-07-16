/**
 * RisikoPulse — Zone 4 "System-Puls" (★ FINAL, Design-Board c_2103a234).
 *
 * Kompakte CPU/RAM-Meter aus PressureHost (instantan, keine Historie) — tippen
 * blendet den echten Source-Breakdown (PressureSource[]) ein. Die Sparkline ist
 * client-seitig aus den Polls akkumuliert (kein Backend-Verlauf verfügbar).
 * Darunter eine schlanke Lane-Health-Zeile → Statistik-Tab statt der
 * ausgebauten Zuverlässigkeits-Disclosure-Tabelle (Cut lt. Handoff).
 */
import { useEffect, useState } from "react";
import { Link } from "react-router-dom";
import type { Freshness } from "../../lib/derive";
import type { PressureStatusResponse, SystemHealthResponse } from "../../lib/types";
import type { ReliabilityRiskModel } from "../../lib/fleetRisk";

const SPARK_MAX_POINTS = 20;

type MeterKey = "cpu" | "ram";

function healthDot(status: string | null | undefined): "ok" | "warn" | "alert" {
  if (status === "healthy") return "ok";
  if (status === "degraded") return "warn";
  return "alert";
}

function tailnetDot(state: PressureStatusResponse["access"]["tailnet"] | undefined): "ok" | "warn" | "alert" {
  if (state === "direct") return "ok";
  if (state === "relay") return "warn";
  return "alert";
}

function toneDot(tone: ReliabilityRiskModel["rows"][number]["tone"]): "ok" | "warn" | "alert" {
  if (tone === "red") return "alert";
  if (tone === "amber") return "warn";
  return "ok";
}

export interface RisikoPulseProps {
  pressureStatus: PressureStatusResponse | null;
  systemHealth: SystemHealthResponse | null;
  reliabilityModel: ReliabilityRiskModel;
  fresh: Freshness;
}

export function RisikoPulse({ pressureStatus, systemHealth, reliabilityModel, fresh }: RisikoPulseProps) {
  const [open, setOpen] = useState<MeterKey | null>(null);
  const [spark, setSpark] = useState<{ cpu: number[]; ram: number[] }>({ cpu: [], ram: [] });

  const cpu = pressureStatus?.host.cpu_percent ?? null;
  const ram = pressureStatus?.host.memory_percent ?? null;

  // Akkumuliert die Sparkline-Historie aus jedem neuen Poll-Tick (externe
  // Quelle, kein Backend-Verlauf) — es gibt keinen abgeleiteten Wert, den man
  // stattdessen während des Renders berechnen könnte.
  useEffect(() => {
    if (pressureStatus == null) return;
    // eslint-disable-next-line react-hooks/set-state-in-effect
    setSpark((prev) => ({
      cpu: cpu != null ? [...prev.cpu, cpu].slice(-SPARK_MAX_POINTS) : prev.cpu,
      ram: ram != null ? [...prev.ram, ram].slice(-SPARK_MAX_POINTS) : prev.ram,
    }));
  }, [pressureStatus?.checked_at]);

  const sources = pressureStatus?.pressure_sources ?? [];

  return (
    <section className="risiko-v4" aria-label="System-Puls">
      <div className="rk-eyebrow-row"><span className="rk-eyebrow">System-Puls</span></div>
      <div className="rk-puls">
        <MeterTile
          label="CPU"
          value={cpu}
          spark={spark.cpu}
          expanded={open === "cpu"}
          onToggle={() => setOpen((k) => (k === "cpu" ? null : "cpu"))}
        />
        <MeterTile
          label="RAM"
          value={ram}
          spark={spark.ram}
          expanded={open === "ram"}
          onToggle={() => setOpen((k) => (k === "ram" ? null : "ram"))}
        />
      </div>

      {open != null ? (
        <div className="rk-breakdown" aria-label={`${open === "cpu" ? "CPU" : "RAM"}-Source-Breakdown`}>
          {sources.length === 0 ? (
            <p className="rk-breakdown-empty">Keine Source-Daten.</p>
          ) : (
            sources.map((s) => (
              <div key={`${s.kind}-${s.label}`} className="rk-breakdown-row">
                <span className="rk-breakdown-label">{s.label}</span>
                <span className="rk-breakdown-val">{`${s.cpu_percent.toFixed(0)}% CPU · ${s.rss_mb.toFixed(0)} MB${s.throttled ? " · throttled" : ""}`}</span>
              </div>
            ))
          )}
        </div>
      ) : null}

      <div className="rk-footer-line">
        <span className={`rk-sdot rk-sdot-${fresh.stale ? "warn" : "ok"}`}>Puls {fresh.stale ? `veraltet (${fresh.label})` : fresh.label}</span>
        <span className={`rk-sdot rk-sdot-${healthDot(systemHealth?.subsystems.gateway.status)}`}>Gateway</span>
        <span className={`rk-sdot rk-sdot-${healthDot(systemHealth?.subsystems.kanban_dispatcher.status)}`}>Dispatcher</span>
        <span className={`rk-sdot rk-sdot-${tailnetDot(pressureStatus?.access.tailnet)}`}>Tailnet</span>
        <Link to="/control/statistik" className="rk-lanes-inline" aria-label="Lane-Health im Statistik-Tab öffnen">
          <span className="rk-lanes-dots" aria-hidden="true">
            {reliabilityModel.rows.slice(0, 7).map((row) => (
              <span key={row.profile} className={`rk-ld rk-ld-${toneDot(row.tone)}`} />
            ))}
          </span>
          {reliabilityModel.rows.length > 0 ? reliabilityModel.summary : "Lanes ruhig"} ›
        </Link>
      </div>
    </section>
  );
}

function MeterTile({ label, value, spark, expanded, onToggle }: {
  label: string;
  value: number | null;
  spark: number[];
  expanded: boolean;
  onToggle: () => void;
}) {
  const fillClass = label === "CPU" ? "rk-fill-cpu" : "rk-fill-ram";
  return (
    <button
      type="button"
      className={`rk-meter${expanded ? " rk-meter-open" : ""}`}
      onClick={onToggle}
      aria-expanded={expanded}
      aria-label={`${label}-Breakdown ${expanded ? "schließen" : "öffnen"}`}
    >
      <span className="rk-meter-tap" aria-hidden="true">tippen ▸</span>
      <div className="rk-meter-cap">{label}</div>
      <div className="rk-meter-val">{value == null ? "—" : `${Math.round(value)}%`}</div>
      <div className="rk-meter-track">
        <span className={`rk-meter-fill ${fillClass}`} style={{ width: `${Math.max(0, Math.min(100, value ?? 0))}%` }} />
      </div>
      {spark.length > 1 ? (
        <div className="rk-meter-spark" aria-hidden="true">
          {spark.map((v, i) => (
            <i key={i} style={{ height: `${Math.max(6, Math.min(100, v))}%` }} />
          ))}
        </div>
      ) : null}
    </button>
  );
}
