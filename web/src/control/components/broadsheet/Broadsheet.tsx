/**
 * Broadsheet shell — the fork-local Tab-Shell + presentational primitives for
 * /control/statistik (PlanSpec 2026-06-17, Richtung B · Broadsheet, ST3).
 *
 * Each primitive maps 1:1 to a structural element of the chosen mockup
 * (assets/stats-B2-broadsheet.html): a printed flotten-report carried by
 * hairline rules instead of cards. They are pure & presentational — no data
 * fetching, no business logic — so the data-bearing modules (ST4 masthead/
 * reliability/errors, ST5 budget-ledger/efficiency) compose them with real
 * live values. The look lives entirely in styles/stats-broadsheet.css, scoped
 * under [data-stats-broadsheet]; importing that CSS here keeps it lazy (loads
 * only when the stats view mounts) and leaves control-tokens.css untouched.
 */
import type { ReactNode } from "react";
import { cn } from "@/lib/utils";
import type { BroadsheetStatus } from "../../lib/broadsheetTokens";
import "../../styles/stats-broadsheet.css";

/** Status → figure ink class. `neutral` keeps the default ink (no class). */
const FIG_CLASS: Record<BroadsheetStatus, string> = {
  ok: "sb-ok",
  warn: "sb-warn",
  crit: "sb-crit",
  neutral: "",
};
/** Status → meter fill class. */
const METER_CLASS: Record<BroadsheetStatus, string> = {
  ok: "sb-me",
  warn: "sb-ma",
  crit: "sb-mr",
  neutral: "sb-me",
};

const clampPct = (pct: number) => Math.max(0, Math.min(100, pct));

/** The broadsheet column — carries the [data-stats-broadsheet] scope so every
 *  sb-* rule (and the font @import in the CSS) only applies inside the tab. */
export function BroadsheetShell({ children, className }: { children: ReactNode; className?: string }) {
  return (
    <div className={cn("sb-wrap", className)} data-stats-broadsheet>
      {children}
    </div>
  );
}

/** Mono uppercase kicker label; `accent` tints it navy (the single accent). */
export function Kicker({ children, accent, className }: { children: ReactNode; accent?: boolean; className?: string }) {
  return <div className={cn("sb-kick", accent && "sb-accent", className)}>{children}</div>;
}

/** Masthead — the nordstern block: two kicks, a navy label, the oversized
 *  display figure, and a footing line (note left, delta right). */
export function Masthead({
  kicker,
  meta,
  label,
  value,
  unit,
  note,
  delta,
  deltaStatus = "ok",
  className,
}: {
  kicker: ReactNode;
  meta?: ReactNode;
  label?: ReactNode;
  value: ReactNode;
  unit?: ReactNode;
  note?: ReactNode;
  delta?: ReactNode;
  deltaStatus?: BroadsheetStatus;
  className?: string;
}) {
  return (
    <div className={cn("sb-top", className)}>
      <div className="sb-between">
        <Kicker>{kicker}</Kicker>
        {meta != null ? <Kicker>{meta}</Kicker> : null}
      </div>
      {label != null ? (
        <Kicker accent className="sb-mast-label">
          {label}
        </Kicker>
      ) : null}
      <div className="sb-mast">
        {value}
        {unit != null ? <small>{unit}</small> : null}
      </div>
      {note != null || delta != null ? (
        <div className="sb-mline">
          <span className="sb-l">{note}</span>
          {delta != null ? <span className={cn("sb-d", FIG_CLASS[deltaStatus])}>{delta}</span> : null}
        </div>
      ) : null}
    </div>
  );
}

/** Three-up grid of supporting KPIs (top+bottom rule, vertical hairlines). */
export function SupportingStats({ children, className }: { children: ReactNode; className?: string }) {
  return <div className={cn("sb-threeup", className)}>{children}</div>;
}

export function SupportingStat({
  value,
  unit,
  label,
  accent,
}: {
  value: ReactNode;
  unit?: ReactNode;
  label: ReactNode;
  accent?: boolean;
}) {
  return (
    <div className="sb-tu">
      <div className={cn("sb-n", accent && "sb-accent")}>
        {value}
        {unit != null ? <small>{unit}</small> : null}
      </div>
      <div className="sb-l">{label}</div>
    </div>
  );
}

/** A section header: a display title, a hairline rule, and a mono right-meta. */
export function SectionRule({ title, meta, className }: { title: ReactNode; meta?: ReactNode; className?: string }) {
  return (
    <div className={cn("sb-skick", className)}>
      <h2>{title}</h2>
      <span className="sb-ln" />
      {meta != null ? <span className="sb-rt">{meta}</span> : null}
    </div>
  );
}

/** The one urgent lead line, spined by a status colour (default crit). The
 *  caller supplies the emphasised phrase via a <b> inside `children`. */
export function EngpassLead({
  children,
  tone = "crit",
  className,
}: {
  children: ReactNode;
  tone?: "crit" | "warn" | "calm";
  className?: string;
}) {
  return (
    <div className={cn("sb-lead", tone === "warn" && "sb-lead-warn", tone === "calm" && "sb-lead-calm", className)}>
      <span>{children}</span>
    </div>
  );
}

/** A budget-ledger row: name (+ optional tag), leader-dots, status figure, a
 *  thin meter, and an optional two-up footing. */
export function LedgerRow({
  name,
  tag,
  figure,
  status,
  pct,
  footLeft,
  footRight,
}: {
  name: ReactNode;
  tag?: ReactNode;
  figure: ReactNode;
  status: BroadsheetStatus;
  pct: number;
  footLeft?: ReactNode;
  footRight?: ReactNode;
}) {
  return (
    <div className="sb-led-row">
      <div className="sb-led-top">
        <span className="sb-led-name">
          {name}
          {tag != null ? <span className="sb-tagm">{tag}</span> : null}
        </span>
        <span className="sb-led-dots" />
        <span className={cn("sb-led-fig", FIG_CLASS[status])}>{figure}</span>
      </div>
      <div className="sb-led-meter">
        <i className={METER_CLASS[status]} style={{ width: `${clampPct(pct)}%` }} />
      </div>
      {footLeft != null || footRight != null ? (
        <div className="sb-led-foot">
          <span>{footLeft}</span>
          <span>{footRight}</span>
        </div>
      ) : null}
    </div>
  );
}

/** Two big display figures split by a hairline — e.g. latency p50 / p90. */
export function TwinStats({ children, className }: { children: ReactNode; className?: string }) {
  return <div className={cn("sb-twin", className)}>{children}</div>;
}

export function TwinStat({ label, value, unit }: { label: ReactNode; value: ReactNode; unit?: ReactNode }) {
  return (
    <div>
      <div className="sb-kick">{label}</div>
      <div className="sb-tn">
        {value}
        {unit != null ? <small>{unit}</small> : null}
      </div>
    </div>
  );
}

/** A leaderboard row: rank · name · status score · latency, rule per row. */
export function LeaderRow({
  rank,
  name,
  score,
  status,
  latency,
}: {
  rank: ReactNode;
  name: ReactNode;
  score: ReactNode;
  status: BroadsheetStatus;
  latency?: ReactNode;
}) {
  return (
    <div className="sb-lr">
      <span className="sb-rk">{rank}</span>
      <span className="sb-nm">{name}</span>
      <span className={cn("sb-sc", FIG_CLASS[status])}>{score}</span>
      <span className="sb-lt">{latency}</span>
    </div>
  );
}

export type ErrorSegment = { pct: number; color: string; key?: string };

/** A single stacked bar of error buckets, widths in %, fills per segment. */
export function ErrorBar({ segments }: { segments: ErrorSegment[] }) {
  return (
    <div className="sb-estack">
      {segments.map((s, i) => (
        <i key={s.key ?? i} style={{ width: `${clampPct(s.pct)}%`, background: s.color }} />
      ))}
    </div>
  );
}

export function ErrorLegend({ children, className }: { children: ReactNode; className?: string }) {
  return <div className={cn("sb-leg", className)}>{children}</div>;
}

export function ErrorLegendItem({ color, label, count }: { color: string; label: ReactNode; count: ReactNode }) {
  return (
    <div className="sb-li">
      <span className="sb-sw" style={{ background: color }} />
      {label}
      <b>{count}</b>
    </div>
  );
}

/** The findings verdict — a spined paragraph; caller bolds the lead via <b>. */
export function Verdict({
  children,
  tone = "calm",
  className,
}: {
  children: ReactNode;
  tone?: "calm" | "warn" | "crit";
  className?: string;
}) {
  return (
    <div className={cn("sb-vd", tone === "warn" && "sb-vd-warn", tone === "crit" && "sb-vd-crit", className)}>
      {children}
    </div>
  );
}

/** The bottom rule + masthead-style colophon. */
export function BroadsheetFooter({ left, right }: { left: ReactNode; right: ReactNode }) {
  return (
    <div className="sb-foot">
      <span>{left}</span>
      <span>{right}</span>
    </div>
  );
}
