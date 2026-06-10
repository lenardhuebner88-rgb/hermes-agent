/**
 * Chart-Primitives für /control — handgebaute SVGs, null Dependencies.
 * Operator-Vertrag 2026-06-10 (Phase 3): bewusst KEINE Charting-Lib —
 * exakt im House-Style (Ink auf Papier, Ultramarin-Signal), merge-kalt.
 *
 * Alles ist statisch renderbar (kein JS-Layout, kein Resize-Observer):
 * die SVGs skalieren über viewBox + width:100%. Hover-Details laufen über
 * native <title>-Tooltips — ausreichend für ein Ein-Operator-Cockpit.
 */
import { useId } from "react";

/** Ein Datenpunkt einer Tagesreihe. */
export interface SeriesPoint {
  /** ISO-Datum (Achsen-/Tooltip-Label). */
  label: string;
  value: number;
}

const W = 240;
const H = 56;
const PAD = 2;

function scaleY(v: number, max: number): number {
  if (max <= 0) return H - PAD;
  return H - PAD - (v / max) * (H - PAD * 2);
}

/** Linien-Sparkline mit weicher Fläche darunter. Werte ≥ 0. */
export function Sparkline({ points, stroke = "var(--hc-accent)", fillOpacity = 0.12, valueFmt }: {
  points: SeriesPoint[];
  stroke?: string;
  fillOpacity?: number;
  valueFmt?: (v: number) => string;
}) {
  const gid = useId();
  if (!points.length) return null;
  const max = Math.max(...points.map((p) => p.value), 1);
  const step = points.length > 1 ? (W - PAD * 2) / (points.length - 1) : 0;
  const coords = points.map((p, i) => [PAD + i * step, scaleY(p.value, max)] as const);
  const line = coords.map(([x, y], i) => `${i === 0 ? "M" : "L"}${x.toFixed(1)},${y.toFixed(1)}`).join(" ");
  const area = `${line} L${coords[coords.length - 1][0].toFixed(1)},${H - PAD} L${PAD},${H - PAD} Z`;
  const last = points[points.length - 1];
  return (
    <svg viewBox={`0 0 ${W} ${H}`} className="block h-14 w-full" role="img" aria-label={`${last.label}: ${valueFmt ? valueFmt(last.value) : last.value}`}>
      <defs>
        <linearGradient id={gid} x1="0" y1="0" x2="0" y2="1">
          <stop offset="0%" stopColor={stroke} stopOpacity={fillOpacity * 2} />
          <stop offset="100%" stopColor={stroke} stopOpacity={0} />
        </linearGradient>
      </defs>
      <path d={area} fill={`url(#${gid})`} />
      <path d={line} fill="none" stroke={stroke} strokeWidth="1.5" strokeLinejoin="round" strokeLinecap="round" />
      <circle cx={coords[coords.length - 1][0]} cy={coords[coords.length - 1][1]} r="2.2" fill={stroke} />
      <title>{points.map((p) => `${p.label}: ${valueFmt ? valueFmt(p.value) : p.value}`).join("\n")}</title>
    </svg>
  );
}

/** Tages-Balken (Durchsatz/Kosten). Jeder Balken trägt seinen Tooltip. */
export function DayBars({ points, color = "var(--hc-accent)", valueFmt }: {
  points: SeriesPoint[];
  color?: string;
  valueFmt?: (v: number) => string;
}) {
  if (!points.length) return null;
  const max = Math.max(...points.map((p) => p.value), 1);
  const gap = 1.5;
  const bw = (W - PAD * 2 - gap * (points.length - 1)) / points.length;
  return (
    <svg viewBox={`0 0 ${W} ${H}`} className="block h-14 w-full" role="img">
      {points.map((p, i) => {
        const x = PAD + i * (bw + gap);
        const y = scaleY(p.value, max);
        return (
          <rect
            key={p.label}
            x={x.toFixed(1)}
            y={p.value > 0 ? y.toFixed(1) : H - PAD - 1}
            width={Math.max(0.5, bw).toFixed(1)}
            height={p.value > 0 ? (H - PAD - y).toFixed(1) : 1}
            rx="1"
            fill={p.value > 0 ? color : "var(--hc-border)"}
            opacity={p.value > 0 ? 0.9 : 0.6}
          >
            <title>{`${p.label}: ${valueFmt ? valueFmt(p.value) : p.value}`}</title>
          </rect>
        );
      })}
    </svg>
  );
}

/** Horizontale Rate (0..1) als gefüllte Leiste — für Reliability-Zeilen. */
export function RateBar({ rate, color = "var(--hc-emerald)" }: { rate: number | null; color?: string }) {
  const pct = rate == null ? 0 : Math.max(0, Math.min(1, rate)) * 100;
  return (
    <div className="hc-stage-rail w-full" aria-hidden>
      <i style={{ width: `${pct}%`, background: rate == null ? "var(--hc-border)" : color }} />
    </div>
  );
}
