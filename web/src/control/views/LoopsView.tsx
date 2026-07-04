import { useEffect, useMemo, useState } from "react";
import type { ReactNode } from "react";
import { motion, useReducedMotion } from "motion/react";
import {
  Anchor,
  AlertTriangle,
  CheckCircle2,
  PauseCircle,
  Play,
  RefreshCw,
  Square,
  Workflow,
  Wrench,
  XCircle,
} from "lucide-react";
import type { LucideIcon } from "lucide-react";
import { Button } from "@nous-research/ui/ui/components/button";
import { cn } from "@/lib/utils";
import {
  duplicateLoop,
  extractDetail,
  landLoop,
  saveLoopFile,
  startLoop,
  stopLoop,
  toggleLoopTimer,
  useLoopDetail,
  useLoopFiles,
  useLoopModels,
  useLoops,
} from "../hooks/useControlData";
import { de } from "../i18n/de";
import { ToneCallout } from "../components/atoms";
import { Disclosure } from "../components/primitives";
import {
  isLoopPackError,
  type LoopDetailResponse,
  type LoopFile,
  type LoopFilesResponse,
  type LoopHeartbeatHistoryEntry,
  type LoopModelsResponse,
  type LoopPack,
  type LoopPackError,
  type LoopPackSummary,
} from "../lib/types";
import { parseLedgerLine } from "../lib/loopLedger";
import type { LedgerVerdict } from "../lib/loopLedger";
import { deriveRingSegments, deriveRingTicks } from "../lib/loopRing";
import type { LoopRingSegment, LoopRingTicks } from "../lib/loopRing";
import { fmtAge, fmtDur, nowSec } from "../lib/derive";

const t = de.loops;

/**
 * "Nachtschicht" — bewusst nicht im Einheitslook des restlichen Dashboards
 * (siehe Design-Spec im Auftrag). Alle Farben/Fonts hängen an den `--ln-*`-
 * Custom-Properties unten, gesetzt auf dem View-Root (`NIGHT_VARS`). Da diese
 * Datei sowohl die hook-verdrahtete `LoopsView` als auch die reine
 * `LoopsGrid` (von den Tests direkt gerendert, ohne den Root-Wrapper) exportiert,
 * referenzieren alle `var(--ln-…)`-Aufrufe nur Farben/Fonts — layoutrelevante
 * Tailwind-Klassen bleiben unabhängig davon funktionsfähig.
 */
const NIGHT_VARS = {
  "--ln-void": "#060913",
  "--ln-surface": "#0D1322",
  "--ln-raised": "#141C31",
  "--ln-line": "#1E2A47",
  "--ln-ink": "#E9EEFA",
  "--ln-ink-soft": "#93A0C2",
  "--ln-ink-mute": "#5E6B8C",
  "--ln-sodium": "#FFB454",
  "--ln-sodium-ink": "#1A1205",
  "--ln-ok": "#34C383",
  "--ln-fail": "#E66767",
  "--ln-warn": "#C98500",
  "--ln-font-display": '"Bricolage Grotesque", Inter, system-ui, sans-serif',
  "--ln-font-mono": '"JetBrains Mono", ui-monospace, monospace',
} as React.CSSProperties;

const displayFont: React.CSSProperties = { fontFamily: "var(--ln-font-display)" };
const monoFont: React.CSSProperties = { fontFamily: "var(--ln-font-mono)" };

/** A11y-Boden ohne Ankündigung: 2px Amber-Fokusring auf jedem interaktiven
 *  Element (der Shared-`Button` bringt selbst nur den Einheitslook-Accent
 *  mit — hier explizit auf --ln-sodium überschrieben). */
const NIGHT_FOCUS =
  "focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-[var(--ln-sodium)] focus-visible:ring-offset-2 focus-visible:ring-offset-[var(--ln-void)]";

const NIGHT_FONT_ID = "loops-night-font";
const NIGHT_FONT_URL =
  "https://fonts.googleapis.com/css2?family=Bricolage+Grotesque:wght@500;600;700&display=swap";

/** Lädt Bricolage Grotesque einmalig (idempotent per id, `display=swap`,
 *  Fallback-Stack bleibt in NIGHT_VARS gesetzt — offline bricht nichts). */
function useNightFontInjection() {
  useEffect(() => {
    if (typeof document === "undefined") return;
    if (document.getElementById(NIGHT_FONT_ID)) return;
    const link = document.createElement("link");
    link.id = NIGHT_FONT_ID;
    link.rel = "stylesheet";
    link.href = NIGHT_FONT_URL;
    document.head.appendChild(link);
  }, []);
}

/** Validierte Dark-Palette je Engine — nur als Punkt neben dem Namen, nie
 *  farbe-allein (immer Text daneben). Unbekannte Engines: ink-mute. */
const ENGINE_COLOR: Record<string, string> = {
  claude: "#D95926",
  codex: "#3987E5",
  kimi: "#D55181",
  hermes: "#199E70",
};
const engineColor = (engine: string): string => ENGINE_COLOR[engine] ?? "var(--ln-ink-mute)";

function EngineDot({ engine }: { engine: string }) {
  return (
    <span
      aria-hidden
      className="inline-block h-2 w-2 shrink-0 rounded-full"
      style={{ background: engineColor(engine) }}
    />
  );
}

/** Reihenfolge der Queue-Stufenleiste; 90-bounced steht separat (roter Chip). */
const QUEUE_STAGE_KEYS = ["00-planned", "10-building", "20-verified", "30-landed"] as const;
const QUEUE_STAGE_LABEL: Record<(typeof QUEUE_STAGE_KEYS)[number], string> = {
  "00-planned": t.queuePlanned,
  "10-building": t.queueBuilding,
  "20-verified": t.queueVerified,
  "30-landed": t.queueLanded,
};

/** Kurzes Alter aus einer lokalen ISO-Zeit (heartbeat/ledger `at`-Feld) —
 *  wrap um `fmtAge` (das nur epoch-Sekunden kennt). Ungültige Zeiten → "—". */
function ageFromIso(iso: string, nowMs: number): string {
  const ms = Date.parse(iso);
  if (!Number.isFinite(ms)) return "—";
  return fmtAge(Math.floor(ms / 1000), Math.floor(nowMs / 1000));
}

/** Pro Phase gebaute overrides — nur Felder, die vom Manifest-Default abweichen. */
function buildPhaseOverrides(
  pack: LoopPackSummary,
  phaseValues: Record<string, { engine: string; model: string }>,
): Record<string, string> {
  const overrides: Record<string, string> = {};
  for (const [phase, original] of Object.entries(pack.phases)) {
    const current = phaseValues[phase];
    if (!current) continue;
    const upper = phase.toUpperCase();
    if (current.engine !== original.engine) overrides[`PHASE_${upper}_ENGINE`] = current.engine;
    if (current.model !== original.model) overrides[`PHASE_${upper}_MODEL`] = current.model;
  }
  return overrides;
}

// ── Der Loop-Ring — Signatur-Element ────────────────────────────────────────
// Pipeline: 3 Bogensegmente (PLAN→BUILD→VERIFY). Sweep: Runden-Ticks (≤24).
// Idle: dünner Ring. Error: rot gestrichelt. Running: weicher Amber-Atem-Glow
// (motion, reduced-motion-safe). Geometrie über SVG `pathLength=100` (Browser
// normiert stroke-dasharray/-offset auf Prozent, unabhängig vom Radius).

function RingSegment({
  index,
  total,
  r,
  cx,
  cy,
  stroke,
  seg,
}: {
  index: number;
  total: number;
  r: number;
  cx: number;
  cy: number;
  stroke: number;
  seg: LoopRingSegment;
}) {
  const gap = 2.2;
  const segLen = 100 / total - gap;
  const offset = index * (100 / total) + gap / 2;
  const baseColor = seg.state === "done" ? "var(--ln-ok)" : "var(--ln-line)";
  const progress = seg.state === "current" ? (seg.progress ?? 1) : 0;
  return (
    <>
      <circle
        cx={cx}
        cy={cy}
        r={r}
        fill="none"
        strokeLinecap="round"
        strokeWidth={stroke}
        stroke={baseColor}
        pathLength={100}
        strokeDasharray={`${segLen} ${100 - segLen}`}
        strokeDashoffset={-offset}
      />
      {progress > 0 ? (
        <circle
          cx={cx}
          cy={cy}
          r={r}
          fill="none"
          strokeLinecap="round"
          strokeWidth={stroke}
          stroke="var(--ln-sodium)"
          pathLength={100}
          strokeDasharray={`${segLen * progress} ${100 - segLen * progress}`}
          strokeDashoffset={-offset}
        />
      ) : null}
    </>
  );
}

function RingTicks({
  r,
  cx,
  cy,
  stroke,
  ticks,
}: {
  r: number;
  cx: number;
  cy: number;
  stroke: number;
  ticks: LoopRingTicks;
}) {
  const items = Array.from({ length: ticks.total }, (_, i) => i);
  return (
    <>
      {items.map((i) => {
        const rad = ((360 / ticks.total) * i * Math.PI) / 180;
        const outerR = r + stroke * 0.7;
        const innerR = r - stroke * 0.7;
        const isCurrent = ticks.currentActive && i === ticks.done;
        const isDone = i < ticks.done;
        const color = isCurrent ? "var(--ln-sodium)" : isDone ? "var(--ln-ok)" : "var(--ln-line)";
        return (
          <line
            key={i}
            x1={cx + innerR * Math.cos(rad)}
            y1={cy + innerR * Math.sin(rad)}
            x2={cx + outerR * Math.cos(rad)}
            y2={cy + outerR * Math.sin(rad)}
            stroke={color}
            strokeWidth={Math.max(1.5, stroke / 2)}
            strokeLinecap="round"
          />
        );
      })}
    </>
  );
}

function LoopRing({
  size,
  state,
  segments,
  ticks,
}: {
  size: number;
  state: "running" | "idle" | "error";
  segments?: LoopRingSegment[];
  ticks?: LoopRingTicks;
}) {
  const reduceMotion = useReducedMotion();
  const stroke = size >= 64 ? 8 : 2.5;
  const r = size / 2 - stroke / 2 - 1;
  const cx = size / 2;
  const cy = size / 2;
  const glow = state === "running";
  return (
    <span
      className="relative inline-flex shrink-0"
      style={{ width: size, height: size }}
      data-testid="loop-ring"
      data-state={state}
    >
      {glow ? (
        <motion.span
          aria-hidden
          className="pointer-events-none absolute rounded-full blur-md"
          style={{ inset: "-30%", background: "radial-gradient(circle, var(--ln-sodium) 0%, transparent 70%)" }}
          animate={reduceMotion ? { opacity: 0.5 } : { opacity: [0.3, 0.75, 0.3] }}
          transition={reduceMotion ? undefined : { duration: 3, repeat: Infinity, ease: "easeInOut" }}
        />
      ) : null}
      <svg width={size} height={size} viewBox={`0 0 ${size} ${size}`} aria-hidden className="relative">
        <g transform={`rotate(-90 ${cx} ${cy})`}>
          {state === "idle" ? (
            <circle cx={cx} cy={cy} r={r} fill="none" stroke="var(--ln-line)" strokeWidth={stroke} />
          ) : null}
          {state === "error" ? (
            <circle
              cx={cx}
              cy={cy}
              r={r}
              fill="none"
              stroke="var(--ln-fail)"
              strokeWidth={stroke}
              strokeDasharray="6 5"
            />
          ) : null}
          {state === "running" && segments
            ? segments.map((seg, i) => (
                <RingSegment key={seg.key} index={i} total={segments.length} r={r} cx={cx} cy={cy} stroke={stroke} seg={seg} />
              ))
            : null}
          {state === "running" && ticks ? <RingTicks r={r} cx={cx} cy={cy} stroke={stroke} ticks={ticks} /> : null}
        </g>
      </svg>
    </span>
  );
}

// ── kleine Night-Atome (Badges, Chips) ──────────────────────────────────────

function NightPill({
  tone,
  icon: Icon,
  children,
}: {
  tone: "ok" | "warn" | "sodium" | "neutral";
  icon?: LucideIcon;
  children: ReactNode;
}) {
  const hex = { ok: "#34C383", warn: "#C98500", sodium: "#FFB454", neutral: "#5E6B8C" }[tone];
  return (
    <span
      className="inline-flex items-center gap-1.5 whitespace-nowrap rounded-full border px-2.5 py-1 text-xs font-medium"
      style={{ borderColor: `${hex}66`, backgroundColor: `${hex}1f`, color: hex }}
    >
      {Icon ? <Icon aria-hidden className="h-3.5 w-3.5" /> : null}
      {children}
    </span>
  );
}

function SourceBadge({ source }: { source?: "repo" | "custom" }) {
  if (!source) return null;
  return (
    <span
      className="inline-flex items-center rounded-full border px-2 py-0.5 text-[10px] uppercase tracking-[0.1em]"
      style={{ ...monoFont, borderColor: "var(--ln-line)", color: "var(--ln-ink-soft)" }}
    >
      {source === "custom" ? t.sourceCustom : t.sourceRepo}
    </span>
  );
}

function TypeBadge({ type }: { type: "pipeline" | "sweep" }) {
  const Icon = type === "pipeline" ? Workflow : RefreshCw;
  return (
    <span className="inline-flex items-center gap-1 text-xs" style={{ color: "var(--ln-ink-soft)" }}>
      <Icon aria-hidden className="h-3.5 w-3.5" />
      {type === "pipeline" ? t.typePipeline : t.typeSweep}
    </span>
  );
}

// ── Telemetrie-Zeile (Karte) ────────────────────────────────────────────────

function CardTelemetryLine({ pack, nowMs }: { pack: LoopPackSummary; nowMs: number }) {
  const reduceMotion = useReducedMotion();
  if (pack.running) {
    const current = pack.heartbeat?.current ?? null;
    if (!current) {
      return (
        <p className="mt-2 text-xs" style={{ ...monoFont, color: "var(--ln-ink-soft)" }}>
          {t.heartbeatBetweenPhases}
        </p>
      );
    }
    const startedMs = Date.parse(current.started_at);
    const elapsedSec = Number.isFinite(startedMs) ? Math.max(0, Math.floor((nowMs - startedMs) / 1000)) : 0;
    return (
      <p className="mt-2 inline-flex items-center gap-1.5 text-xs" style={{ ...monoFont, color: "var(--ln-ink)" }}>
        <span
          aria-hidden
          className={cn("inline-block h-1.5 w-1.5 rounded-full", !reduceMotion && "animate-pulse")}
          style={{ background: "var(--ln-sodium)" }}
        />
        {t.heartbeatCurrent(current.phase, current.model, fmtDur(elapsedSec))}
      </p>
    );
  }
  const last = pack.heartbeat?.last ?? [];
  const newest = last[last.length - 1];
  if (!newest) return null;
  return (
    <p className="mt-2 text-xs" style={{ ...monoFont, color: "var(--ln-ink-soft)" }}>
      {t.telemetryIdleLast(newest.phase, newest.rc === 0, newest.secs, ageFromIso(newest.at, nowMs))}
    </p>
  );
}

/** Phasen-Historie (≤5, jüngste zuerst) als kleine Balkenreihe — Breite ∝ Dauer
 *  relativ zum Max der Reihe, Farbe nach rc. Mono-Dauer-Label darunter (ab sm)
 *  trägt dieselbe Information nochmal als Text (Status nie farbe-allein). */
function PhaseHistoryBars({ last }: { last: LoopHeartbeatHistoryEntry[] }) {
  if (last.length === 0) return null;
  const recent = last.slice(-5).reverse();
  const maxSecs = Math.max(1, ...recent.map((e) => e.secs));
  return (
    <div className="mt-2 flex flex-wrap items-end gap-2">
      {recent.map((entry, idx) => {
        const ok = entry.rc === 0;
        const widthPx = Math.max(16, Math.round((entry.secs / maxSecs) * 56));
        return (
          <div key={`${entry.at}-${entry.phase}-${idx}`} className="flex flex-col items-start gap-1">
            <span
              title={`${entry.phase} · ${entry.secs}s · ${entry.engine}`}
              className="block h-4 rounded-sm"
              style={{ width: widthPx, backgroundColor: ok ? "var(--ln-ok)" : "var(--ln-fail)" }}
            />
            <span className="hidden text-[10px] sm:inline" style={{ ...monoFont, color: "var(--ln-ink-mute)" }}>
              {entry.phase} {entry.secs}s {ok ? "✓" : "✗"}
            </span>
          </div>
        );
      })}
    </div>
  );
}

/** Queue-Stufenleiste als Stepper: Geplant → Gebaut → Verifiziert → Gelandet,
 *  90-bounced separat als roter Chip. Zahl je Stufe bleibt ein eigenständiger
 *  Textknoten (nicht mit dem Label verklebt). */
function LoopQueueStepper({ queue }: { queue: Record<string, number> }) {
  const bounced = queue["90-bounced"] ?? 0;
  return (
    <div className="mt-3 flex flex-wrap items-center gap-1">
      {QUEUE_STAGE_KEYS.map((key, idx) => {
        const n = queue[key] ?? 0;
        return (
          <span key={key} className="inline-flex items-center gap-1">
            {idx > 0 ? (
              <span aria-hidden className="text-xs" style={{ color: "var(--ln-ink-mute)" }}>→</span>
            ) : null}
            <span
              className="flex min-w-[3.75rem] flex-col items-center rounded-md border px-2 py-1"
              style={{
                borderColor: n > 0 ? "var(--ln-sodium)" : "var(--ln-line)",
                background: n > 0 ? "var(--ln-raised)" : "transparent",
              }}
            >
              <span className="block text-sm font-semibold" style={{ ...monoFont, color: "var(--ln-ink)" }}>{n}</span>
              <span className="block text-[10px] uppercase tracking-[0.06em]" style={{ color: "var(--ln-ink-mute)" }}>
                {QUEUE_STAGE_LABEL[key]}
              </span>
            </span>
          </span>
        );
      })}
      {bounced > 0 ? (
        <NightPill tone="warn"><AlertTriangle aria-hidden className="h-3 w-3" />{bounced} {t.queueBounced}</NightPill>
      ) : null}
    </div>
  );
}

// ── Logbuch (Ledger-Timeline) ───────────────────────────────────────────────

const VERDICT_META: Record<NonNullable<LedgerVerdict>, { Icon: LucideIcon; color: string }> = {
  ok: { Icon: CheckCircle2, color: "var(--ln-ok)" },
  fail: { Icon: XCircle, color: "var(--ln-fail)" },
  warn: { Icon: AlertTriangle, color: "var(--ln-warn)" },
  pause: { Icon: PauseCircle, color: "var(--ln-ink-soft)" },
  land: { Icon: Anchor, color: "var(--ln-sodium)" },
};

function LogChip({ children }: { children: ReactNode }) {
  return (
    <span
      className="rounded border px-1 text-[10px]"
      style={{ ...monoFont, borderColor: "var(--ln-line)", color: "var(--ln-ink)" }}
    >
      {children}
    </span>
  );
}

function LogbookFeed({ lines }: { lines: string[] }) {
  return (
    <div
      className="max-h-64 space-y-1 overflow-auto rounded-lg border p-2"
      style={{ borderColor: "var(--ln-line)", background: "var(--ln-void)" }}
    >
      {lines.map((line, idx) => {
        const parsed = parseLedgerLine(line);
        const meta = parsed.verdict ? VERDICT_META[parsed.verdict] : null;
        return (
          <div key={`${idx}-${line}`} className="flex items-start gap-2 rounded px-1 py-0.5 text-xs" style={monoFont}>
            {meta ? (
              <meta.Icon aria-hidden className="mt-0.5 h-3.5 w-3.5 shrink-0" style={{ color: meta.color }} />
            ) : (
              <span aria-hidden className="mt-0.5 inline-block h-3.5 w-3.5 shrink-0" />
            )}
            <span className="min-w-0 flex-1 break-words" style={{ color: "var(--ln-ink-soft)" }}>
              {parsed.round != null || parsed.phase || parsed.secs != null ? (
                <span className="mr-1.5 inline-flex gap-1 align-middle">
                  {parsed.round != null ? <LogChip>R{parsed.round}</LogChip> : null}
                  {parsed.phase ? <LogChip>{parsed.phase}</LogChip> : null}
                  {parsed.secs != null ? <LogChip>{parsed.secs}s</LogChip> : null}
                </span>
              ) : null}
              {parsed.raw}
            </span>
          </div>
        );
      })}
    </div>
  );
}

function LoopDetailPanel({ detail }: { detail: LoopDetailResponse }) {
  const caption: React.CSSProperties = { ...monoFont, color: "var(--ln-ink-mute)" };
  return (
    <div className="space-y-3 text-xs">
      <div>
        <p className="text-[11px] uppercase tracking-[0.14em]" style={caption}>{t.detailLedger}</p>
        {detail.ledger_tail.length > 0 ? (
          <div className="mt-1"><LogbookFeed lines={detail.ledger_tail} /></div>
        ) : (
          <p className="mt-1" style={{ color: "var(--ln-ink-mute)" }}>{t.detailNoLedger}</p>
        )}
      </div>
      {detail.queue_entries ? (
        <div>
          <p className="text-[11px] uppercase tracking-[0.14em]" style={caption}>{t.detailQueue}</p>
          <ul className="mt-1 space-y-1" style={monoFont}>
            {Object.entries(detail.queue_entries).map(([stage, files]) => (
              <li key={stage} style={{ color: "var(--ln-ink-soft)" }}>
                <span style={{ color: "var(--ln-ink)" }}>{stage}</span>: {files.length > 0 ? files.join(", ") : "—"}
              </li>
            ))}
          </ul>
        </div>
      ) : null}
      <div>
        <p className="text-[11px] uppercase tracking-[0.14em]" style={caption}>{t.detailCommits}</p>
        {detail.commits.length > 0 ? (
          <ul className="mt-1 space-y-0.5" style={{ ...monoFont, color: "var(--ln-ink-soft)" }}>
            {detail.commits.map((line) => <li key={line}>{line}</li>)}
          </ul>
        ) : (
          <p className="mt-1" style={{ color: "var(--ln-ink-mute)" }}>{t.detailNoCommits}</p>
        )}
      </div>
      <div>
        <p className="text-[11px] uppercase tracking-[0.14em]" style={caption}>{t.detailOverrides}</p>
        {Object.keys(detail.overrides).length > 0 ? (
          <ul className="mt-1 space-y-0.5" style={{ ...monoFont, color: "var(--ln-ink-soft)" }}>
            {Object.entries(detail.overrides).map(([key, value]) => <li key={key}>{key}={value}</li>)}
          </ul>
        ) : (
          <p className="mt-1" style={{ color: "var(--ln-ink-mute)" }}>{t.detailNoOverrides}</p>
        )}
      </div>
    </div>
  );
}

// ── Formulare / Felder ───────────────────────────────────────────────────────

const NIGHT_FIELD_CLASS = cn(
  "min-h-11 w-full rounded-md border px-2 py-1.5 text-base sm:min-h-9 sm:text-sm placeholder:text-[var(--ln-ink-mute)] disabled:opacity-60",
  NIGHT_FOCUS,
);
const nightFieldStyle: React.CSSProperties = {
  ...monoFont,
  backgroundColor: "var(--ln-void)",
  borderColor: "var(--ln-line)",
  color: "var(--ln-ink)",
};

function LoopStartForm({
  pack,
  models,
  busy,
  onSubmit,
  onCancel,
}: {
  pack: LoopPackSummary;
  models: LoopModelsResponse | null;
  busy: boolean;
  onSubmit: (overrides: Record<string, string>) => void;
  onCancel: () => void;
}) {
  const phaseNames = useMemo(() => Object.keys(pack.phases), [pack]);
  const [phaseValues, setPhaseValues] = useState<Record<string, { engine: string; model: string }>>(() =>
    Object.fromEntries(phaseNames.map((name) => [name, { ...pack.phases[name] }])),
  );
  const defaultMaxRounds = String(pack.stop.max_rounds ?? "");
  const defaultMaxHours = String(pack.stop.max_hours ?? "");
  const [maxRounds, setMaxRounds] = useState(defaultMaxRounds);
  const [maxHours, setMaxHours] = useState(defaultMaxHours);
  const [skipPlan, setSkipPlan] = useState(false);
  // Pack-Params dynamisch (focus/fokus/services/…): ein Feld pro Manifest-Param.
  const paramNames = useMemo(() => Object.keys(pack.params), [pack]);
  const [paramValues, setParamValues] = useState<Record<string, string>>(() => ({ ...pack.params }));

  const engines = models?.engines ?? {};
  const engineNames = Object.keys(engines);
  const captionStyle: React.CSSProperties = { color: "var(--ln-ink-mute)" };

  const handleSubmit = () => {
    const overrides = buildPhaseOverrides(pack, phaseValues);
    if (maxRounds.trim() && maxRounds !== defaultMaxRounds) overrides.MAX_ROUNDS = maxRounds.trim();
    if (maxHours.trim() && maxHours !== defaultMaxHours) overrides.MAX_HOURS = maxHours.trim();
    if (skipPlan) overrides.SKIP_PLAN = "1";
    for (const name of paramNames) {
      const value = (paramValues[name] ?? "").trim();
      if (value && value !== pack.params[name]) overrides[name.toUpperCase()] = value;
    }
    onSubmit(overrides);
  };

  const failStreak = pack.stop.fail_streak;
  const dryRounds = pack.stop.dry_rounds;

  return (
    <div className="space-y-3">
      <p className="text-[11px] uppercase tracking-[0.14em]" style={{ ...monoFont, ...captionStyle }}>{t.startPanelTitle}</p>
      {phaseNames.map((phase) => {
        const value = phaseValues[phase];
        const engineModels = engines[value.engine]?.models ?? [];
        return (
          <div key={phase} className="grid gap-2 sm:grid-cols-[minmax(0,90px)_minmax(0,1fr)_minmax(0,1fr)] sm:items-end">
            <span className="text-xs font-semibold uppercase tracking-[0.1em]" style={{ ...monoFont, color: "var(--ln-ink)" }}>
              {phase}
            </span>
            <label className="min-w-0">
              <span className="text-[11px] uppercase tracking-[0.08em]" style={captionStyle}>{t.phaseEngine}</span>
              <span className="mt-1 flex items-center gap-2">
                <EngineDot engine={value.engine} />
                <select
                  value={value.engine}
                  disabled={busy}
                  aria-label={`${t.phaseEngine} ${phase}`}
                  onChange={(e) => {
                    const engine = e.target.value;
                    const firstModel = engines[engine]?.models[0] ?? "";
                    setPhaseValues((prev) => ({ ...prev, [phase]: { engine, model: firstModel } }));
                  }}
                  className={NIGHT_FIELD_CLASS}
                  style={nightFieldStyle}
                >
                  {engineNames.map((name) => {
                    const disabled = (engines[name]?.models.length ?? 0) === 0;
                    return (
                      <option key={name} value={name} disabled={disabled}>
                        {engines[name]?.label ?? name}
                        {disabled ? ` — ${t.neuralwattDisabled}` : ""}
                      </option>
                    );
                  })}
                </select>
              </span>
            </label>
            <label className="min-w-0">
              <span className="text-[11px] uppercase tracking-[0.08em]" style={captionStyle}>{t.phaseModel}</span>
              <select
                value={value.model}
                disabled={busy || engineModels.length === 0}
                aria-label={`${t.phaseModel} ${phase}`}
                onChange={(e) => {
                  const model = e.target.value;
                  setPhaseValues((prev) => ({ ...prev, [phase]: { ...prev[phase], model } }));
                }}
                className={cn(NIGHT_FIELD_CLASS, "mt-1")}
                style={nightFieldStyle}
              >
                {engineModels.map((model) => (
                  <option key={model} value={model}>{model}</option>
                ))}
              </select>
            </label>
          </div>
        );
      })}
      <div className="grid gap-2 sm:grid-cols-2">
        <label className="min-w-0">
          <span className="text-[11px] uppercase tracking-[0.08em]" style={captionStyle}>{t.maxRoundsLabel}</span>
          <input
            type="number"
            min={1}
            value={maxRounds}
            disabled={busy}
            onChange={(e) => setMaxRounds(e.target.value)}
            className={cn(NIGHT_FIELD_CLASS, "mt-1")}
            style={nightFieldStyle}
          />
        </label>
        <label className="min-w-0">
          <span className="text-[11px] uppercase tracking-[0.08em]" style={captionStyle}>{t.maxHoursLabel}</span>
          <input
            type="number"
            min={1}
            value={maxHours}
            disabled={busy}
            onChange={(e) => setMaxHours(e.target.value)}
            className={cn(NIGHT_FIELD_CLASS, "mt-1")}
            style={nightFieldStyle}
          />
        </label>
      </div>
      {pack.type === "pipeline" ? (
        <label className="inline-flex min-h-9 items-center gap-2 text-xs" style={{ color: "var(--ln-ink-soft)" }}>
          <input
            type="checkbox"
            checked={skipPlan}
            disabled={busy}
            aria-label={t.skipPlanLabel}
            onChange={(e) => setSkipPlan(e.target.checked)}
            className={NIGHT_FOCUS}
          />
          {t.skipPlanLabel}
        </label>
      ) : null}
      {failStreak != null && dryRounds != null ? (
        <p className="text-xs" style={{ ...monoFont, color: "var(--ln-ink-mute)" }}>
          {t.stopCriteria(failStreak, dryRounds)}
        </p>
      ) : null}
      {paramNames.map((name) => (
        <label key={name} className="block min-w-0">
          <span className="text-[11px] uppercase tracking-[0.08em]" style={captionStyle}>{t.paramLabel} · {name}</span>
          <textarea
            value={paramValues[name] ?? ""}
            disabled={busy}
            rows={2}
            aria-label={`${t.paramLabel} ${name}`}
            onChange={(e) => setParamValues((prev) => ({ ...prev, [name]: e.target.value }))}
            className={cn(NIGHT_FIELD_CLASS, "mt-1 min-h-16 resize-y")}
            style={nightFieldStyle}
          />
        </label>
      ))}
      <div className="flex flex-wrap items-center gap-2">
        <Button
          size="sm"
          disabled={busy}
          onClick={handleSubmit}
          className={cn("border-0", NIGHT_FOCUS)}
          style={{ background: "var(--ln-sodium)", color: "var(--ln-sodium-ink)" }}
        >
          {busy ? "…" : t.submitStart}
        </Button>
        <Button size="sm" ghost disabled={busy} onClick={onCancel} className={NIGHT_FOCUS} style={{ color: "var(--ln-ink-soft)" }}>
          {t.cancelStart}
        </Button>
      </div>
    </div>
  );
}

/** Editor für genau eine Datei. Gemountet mit `key={file.name}` vom Elternteil —
 *  ein Datei-/Reload-Wechsel remountet die Komponente statt den Entwurf per
 *  Effect zurückzusetzen, damit kein Merge über Dateien hinweg entsteht. */
function LoopWorkstationFileEditor({
  file,
  saveBusy,
  saveError,
  onSave,
}: {
  file: LoopFile;
  saveBusy: boolean;
  saveError: string | null;
  onSave: (filename: string, content: string) => void;
}) {
  const [draft, setDraft] = useState(file.content);
  return (
    <div className="space-y-2">
      {!file.editable ? <ToneCallout tone="amber">{t.workshopReadOnly}</ToneCallout> : null}
      <textarea
        value={draft}
        disabled={!file.editable || saveBusy}
        onChange={(e) => setDraft(e.target.value)}
        rows={14}
        spellCheck={false}
        aria-label={`${t.workshopTitle} ${file.name}`}
        className={cn(NIGHT_FIELD_CLASS, "min-h-64 resize-y text-xs leading-5")}
        style={nightFieldStyle}
      />
      {file.editable ? (
        <div className="flex flex-wrap items-center gap-2">
          <Button
            size="xs"
            disabled={saveBusy || draft === file.content}
            onClick={() => onSave(file.name, draft)}
            className={cn("border-0", NIGHT_FOCUS)}
            style={{ background: "var(--ln-sodium)", color: "var(--ln-sodium-ink)" }}
          >
            {saveBusy ? "…" : t.workshopSave}
          </Button>
          {saveError ? <ToneCallout tone="red">{t.workshopSaveFailed}: {saveError}</ToneCallout> : null}
        </div>
      ) : null}
    </div>
  );
}

/** Werkstatt-Panel: Datei-Tabs (Mono-Chips) + Textarea + Speichern (nur editable
 *  Packs) + Duplizieren. Rein präsentational — Netzwerk-Aufrufe laufen über
 *  onSave/onDuplicate, wie LoopStartForm. */
function LoopWorkstationPanel({
  files,
  loading,
  error,
  saveBusy,
  saveError,
  onSave,
  duplicateBusy,
  duplicateError,
  onDuplicate,
}: {
  files: LoopFilesResponse | null;
  loading: boolean;
  error: string | null;
  saveBusy: boolean;
  saveError: string | null;
  onSave: (filename: string, content: string) => void;
  duplicateBusy: boolean;
  duplicateError: string | null;
  onDuplicate: (name: string) => void;
}) {
  const fileList = files?.files ?? [];
  const [activeName, setActiveName] = useState<string | null>(null);
  const active = fileList.find((f) => f.name === activeName) ?? fileList[0] ?? null;
  const [dupName, setDupName] = useState("");

  if (loading) return <p className="text-xs" style={{ color: "var(--ln-ink-mute)" }}>{t.loading}</p>;
  if (error) return <ToneCallout tone="red">{t.workshopError}: {error}</ToneCallout>;
  if (!files || fileList.length === 0) return <p className="text-xs" style={{ color: "var(--ln-ink-mute)" }}>{t.workshopEmpty}</p>;

  return (
    <div className="space-y-3 text-xs">
      <p className="text-[11px] uppercase tracking-[0.14em]" style={{ ...monoFont, color: "var(--ln-ink-mute)" }}>{t.workshopTitle}</p>
      <div className="flex flex-wrap gap-1.5">
        {fileList.map((f) => {
          const isActive = f.name === active?.name;
          return (
            <button
              key={f.name}
              type="button"
              onClick={() => setActiveName(f.name)}
              className={cn("rounded-full border px-2.5 py-1 text-[11px]", NIGHT_FOCUS)}
              style={{
                ...monoFont,
                borderColor: isActive ? "var(--ln-sodium)" : "var(--ln-line)",
                color: isActive ? "var(--ln-sodium)" : "var(--ln-ink-soft)",
                background: isActive ? "var(--ln-raised)" : "transparent",
              }}
            >
              {f.name}
            </button>
          );
        })}
      </div>
      {active ? (
        <LoopWorkstationFileEditor key={active.name} file={active} saveBusy={saveBusy} saveError={saveError} onSave={onSave} />
      ) : null}
      <div className="border-t pt-3" style={{ borderColor: "var(--ln-line)" }}>
        <p className="text-[11px] uppercase tracking-[0.14em]" style={{ ...monoFont, color: "var(--ln-ink-mute)" }}>{t.workshopDuplicateTitle}</p>
        <div className="mt-1 flex flex-wrap items-center gap-2">
          <input
            type="text"
            value={dupName}
            disabled={duplicateBusy}
            placeholder={t.workshopDuplicatePlaceholder}
            aria-label={t.workshopDuplicateTitle}
            onChange={(e) => setDupName(e.target.value)}
            className={cn(NIGHT_FIELD_CLASS, "min-h-9 w-auto")}
            style={nightFieldStyle}
          />
          <Button
            size="xs"
            disabled={duplicateBusy || !dupName.trim()}
            onClick={() => onDuplicate(dupName.trim())}
            className={cn("border-0", NIGHT_FOCUS)}
            style={{ background: "var(--ln-sodium)", color: "var(--ln-sodium-ink)" }}
          >
            {duplicateBusy ? "…" : t.workshopDuplicateSubmit}
          </Button>
        </div>
        {duplicateError ? <div className="mt-2"><ToneCallout tone="red">{t.workshopDuplicateFailed}: {duplicateError}</ToneCallout></div> : null}
      </div>
    </div>
  );
}

function LoopErrorCard({ pack }: { pack: LoopPackError }) {
  return (
    <section
      className="rounded-2xl border p-4"
      style={{ borderColor: "var(--ln-fail)", borderStyle: "dashed", background: "var(--ln-surface)" }}
    >
      <div className="flex items-center gap-3">
        <LoopRing size={28} state="error" />
        <span className="truncate text-[17px] font-semibold" style={{ ...displayFont, color: "var(--ln-ink)" }}>
          {pack.name}
        </span>
        <NightPill tone="warn"><XCircle aria-hidden className="h-3.5 w-3.5" />{t.manifestError}</NightPill>
      </div>
      <div className="mt-3"><ToneCallout tone="red">{pack.error}</ToneCallout></div>
    </section>
  );
}

interface LoopCardProps {
  pack: LoopPackSummary;
  models: LoopModelsResponse | null;
  selected: boolean;
  detail: LoopDetailResponse | null;
  detailLoading: boolean;
  detailError: string | null;
  busy: boolean;
  actionError?: string;
  landNote?: string;
  startOpen: boolean;
  pendingStop: boolean;
  pendingLand: boolean;
  workshopOpen: boolean;
  files: LoopFilesResponse | null;
  filesLoading: boolean;
  filesError: string | null;
  fileSaveBusy: boolean;
  fileSaveError: string | null;
  duplicateBusy: boolean;
  duplicateError: string | null;
  nowMs: number;
  onSetPendingStop: (name: string | null) => void;
  onSetPendingLand: (name: string | null) => void;
  onToggleDetail: (name: string) => void;
  onToggleWorkshop: (name: string) => void;
  onOpenStart: (name: string) => void;
  onCloseStart: () => void;
  onSubmitStart: (name: string, overrides: Record<string, string>) => void;
  onStop: (name: string) => void;
  onLand: (name: string) => void;
  onToggleTimer: (name: string, enabled: boolean) => void;
  onSaveFile: (pack: string, filename: string, content: string) => void;
  onDuplicate: (source: string, name: string) => void;
}

function LoopCard({
  pack,
  models,
  selected,
  detail,
  detailLoading,
  detailError,
  busy,
  actionError,
  landNote,
  startOpen,
  pendingStop,
  pendingLand,
  workshopOpen,
  files,
  filesLoading,
  filesError,
  fileSaveBusy,
  fileSaveError,
  duplicateBusy,
  duplicateError,
  nowMs,
  onSetPendingStop,
  onSetPendingLand,
  onToggleDetail,
  onToggleWorkshop,
  onOpenStart,
  onCloseStart,
  onSubmitStart,
  onStop,
  onLand,
  onToggleTimer,
  onSaveFile,
  onDuplicate,
}: LoopCardProps) {
  const isStable = pack.stability === "stable";
  const statusLabel = pack.stop_requested ? t.stopRequested : pack.running ? t.statusRunning : t.statusIdle;
  const canLand = !pack.running && pack.commits_ahead > 0;
  const ringState: "running" | "idle" = pack.running ? "running" : "idle";
  const ringSegments = pack.type === "pipeline" && pack.running ? deriveRingSegments(pack, nowMs) : undefined;
  const ringTicks = pack.type === "sweep" && pack.running ? deriveRingTicks(pack) : undefined;

  return (
    <section
      className="rounded-2xl border p-4 transition-[transform,border-color] duration-150 hover:-translate-y-0.5 motion-reduce:hover:translate-y-0 sm:p-5"
      style={{ borderColor: "var(--ln-line)", background: "var(--ln-surface)" }}
    >
      <div className="flex min-w-0 flex-wrap items-center gap-2.5">
        <LoopRing size={28} state={ringState} segments={ringSegments} ticks={ringTicks} />
        <span className="truncate text-[17px] font-semibold" style={{ ...displayFont, color: "var(--ln-ink)" }}>
          {pack.name}
        </span>
        <SourceBadge source={pack.source} />
        <NightPill tone={isStable ? "ok" : "warn"}>{isStable ? t.stabilityStable : t.stabilityExperimental}</NightPill>
        <TypeBadge type={pack.type} />
      </div>

      <p className="mt-1 text-[11px] uppercase tracking-[0.08em]" style={{ color: "var(--ln-ink-mute)" }}>{statusLabel}</p>
      <p className="mt-1.5 line-clamp-2 text-[13px]" style={{ color: "var(--ln-ink-soft)" }}>{pack.description}</p>

      <CardTelemetryLine pack={pack} nowMs={nowMs} />
      {pack.heartbeat?.last.length ? <PhaseHistoryBars last={pack.heartbeat.last} /> : null}
      {pack.queue ? <LoopQueueStepper queue={pack.queue} /> : null}
      {pack.commits_ahead > 0 ? (
        <div className="mt-2" title={t.commitsAheadHint}>
          <NightPill tone="sodium"><Anchor aria-hidden className="h-3.5 w-3.5" />{t.commitsAhead(pack.commits_ahead)}</NightPill>
        </div>
      ) : null}

      <div className="mt-3 flex flex-wrap items-center justify-between gap-2 border-t pt-3" style={{ borderColor: "var(--ln-line)" }}>
        <label className="inline-flex min-h-9 items-center gap-2 text-xs" style={{ color: "var(--ln-ink-soft)" }}>
          <input
            type="checkbox"
            checked={pack.timer_enabled}
            disabled={busy}
            aria-label={`${t.timerLabel} ${pack.name}`}
            onChange={(e) => onToggleTimer(pack.name, e.target.checked)}
            className={NIGHT_FOCUS}
          />
          {t.timerLabel}: {pack.timer_enabled ? t.timerOn : t.timerOff}
        </label>
        <div className="flex flex-wrap items-center gap-2">
          {pack.running ? (
            pendingStop ? (
              <span className="inline-flex flex-wrap items-center gap-2">
                <span className="text-xs" style={{ color: "var(--ln-ink-soft)" }}>{t.confirmStop}</span>
                <Button size="xs" disabled={busy} onClick={() => onStop(pack.name)} className={NIGHT_FOCUS} style={{ background: "var(--ln-fail)", color: "var(--ln-ink)" }}>
                  {busy ? "…" : t.confirmYes}
                </Button>
                <Button size="xs" ghost disabled={busy} onClick={() => onSetPendingStop(null)} className={NIGHT_FOCUS} style={{ color: "var(--ln-ink-soft)" }}>{t.confirmNo}</Button>
              </span>
            ) : (
              <Button size="xs" ghost disabled={busy} onClick={() => onSetPendingStop(pack.name)} className={NIGHT_FOCUS} style={{ color: "var(--ln-fail)" }}>
                <Square className="h-3.5 w-3.5" />{t.actions.stop}
              </Button>
            )
          ) : (
            <>
              <Button
                size="xs"
                disabled={busy}
                onClick={() => onOpenStart(pack.name)}
                className={cn("border-0", NIGHT_FOCUS)}
                style={{ background: "var(--ln-sodium)", color: "var(--ln-sodium-ink)" }}
              >
                <Play className="h-3.5 w-3.5" />{t.actions.start}
              </Button>
              {canLand ? (
                pendingLand ? (
                  <span className="inline-flex flex-wrap items-center gap-2">
                    <span className="text-xs" style={{ color: "var(--ln-ink-soft)" }}>{t.confirmLand}</span>
                    <Button size="xs" disabled={busy} onClick={() => onLand(pack.name)} className={NIGHT_FOCUS} style={{ background: "var(--ln-ok)", color: "var(--ln-sodium-ink)" }}>
                      {busy ? "…" : t.confirmYes}
                    </Button>
                    <Button size="xs" ghost disabled={busy} onClick={() => onSetPendingLand(null)} className={NIGHT_FOCUS} style={{ color: "var(--ln-ink-soft)" }}>{t.confirmNo}</Button>
                  </span>
                ) : (
                  <Button size="xs" ghost disabled={busy} onClick={() => onSetPendingLand(pack.name)} className={NIGHT_FOCUS} style={{ color: "var(--ln-ok)", borderColor: "var(--ln-ok)" }}>
                    <Anchor className="h-3.5 w-3.5" />{t.actions.land}
                  </Button>
                )
              ) : null}
            </>
          )}
          <Button size="xs" ghost disabled={busy} onClick={() => onToggleWorkshop(pack.name)} className={NIGHT_FOCUS} style={{ color: "var(--ln-ink-soft)" }}>
            <Wrench className="h-3.5 w-3.5" />{t.actions.workshop}
          </Button>
        </div>
      </div>

      {actionError ? <div className="mt-2"><ToneCallout tone="red">{actionError}</ToneCallout></div> : null}
      {landNote ? <div className="mt-2"><ToneCallout tone="emerald">{landNote}</ToneCallout></div> : null}

      {startOpen ? (
        <div className="mt-3 border-t pt-3" style={{ borderColor: "var(--ln-line)" }}>
          <LoopStartForm
            pack={pack}
            models={models}
            busy={busy}
            onSubmit={(overrides) => onSubmitStart(pack.name, overrides)}
            onCancel={onCloseStart}
          />
        </div>
      ) : null}

      {workshopOpen ? (
        <div className="mt-3 border-t pt-3" style={{ borderColor: "var(--ln-line)" }}>
          <LoopWorkstationPanel
            files={files}
            loading={filesLoading}
            error={filesError}
            saveBusy={fileSaveBusy}
            saveError={fileSaveError}
            onSave={(filename, content) => onSaveFile(pack.name, filename, content)}
            duplicateBusy={duplicateBusy}
            duplicateError={duplicateError}
            onDuplicate={(name) => onDuplicate(pack.name, name)}
          />
        </div>
      ) : null}

      <div className="mt-3 border-t pt-3" style={{ borderColor: "var(--ln-line)" }}>
        <Disclosure
          open={selected}
          onToggle={() => onToggleDetail(pack.name)}
          summary={<span className="text-xs" style={{ ...monoFont, color: "var(--ln-ink-soft)" }}>{t.actions.detail}</span>}
        >
          {detailLoading ? <p className="text-xs" style={{ color: "var(--ln-ink-mute)" }}>{t.loading}</p> : null}
          {detailError ? <ToneCallout tone="red">{t.detailError}</ToneCallout> : null}
          {detail ? <LoopDetailPanel detail={detail} /> : null}
        </Disclosure>
      </div>
    </section>
  );
}

// ── Hero "Lagebild" ──────────────────────────────────────────────────────────

function newestGlobalEvent(packs: LoopPackSummary[]): LoopHeartbeatHistoryEntry | null {
  let best: LoopHeartbeatHistoryEntry | null = null;
  let bestMs = -Infinity;
  for (const p of packs) {
    const last = p.heartbeat?.last ?? [];
    const newest = last[last.length - 1];
    if (!newest) continue;
    const ms = Date.parse(newest.at);
    if (Number.isFinite(ms) && ms > bestMs) {
      bestMs = ms;
      best = newest;
    }
  }
  return best;
}

function LoopsHero({
  packs,
  nowMs,
  busyPack,
  pendingStopPack,
  onSetPendingStop,
  onStop,
}: {
  packs: LoopPackSummary[];
  nowMs: number;
  busyPack: string | null;
  pendingStopPack: string | null;
  onSetPendingStop: (name: string | null) => void;
  onStop: (name: string) => void;
}) {
  const running = packs.filter((p) => p.running);
  const hero = running[0] ?? null;

  return (
    <section
      data-testid="loops-hero"
      data-state={hero ? "running" : "sleeping"}
      className="rounded-2xl border p-5"
      style={{ borderColor: "var(--ln-line)", background: "var(--ln-surface)" }}
    >
      {hero ? (
        <div className="flex flex-wrap items-center gap-4">
          <LoopRing
            size={112}
            state="running"
            segments={hero.type === "pipeline" ? deriveRingSegments(hero, nowMs) : undefined}
            ticks={hero.type === "sweep" ? deriveRingTicks(hero) : undefined}
          />
          <div className="min-w-0 flex-1">
            <div className="flex flex-wrap items-center gap-2">
              <span className="truncate text-xl font-semibold" style={{ ...displayFont, color: "var(--ln-ink)" }}>
                {hero.name}
              </span>
              {running.length > 1 ? (
                <NightPill tone="sodium">{t.heroMoreRunning(running.length - 1)}</NightPill>
              ) : null}
            </div>
            <p className="mt-1 text-sm" style={{ ...monoFont, color: "var(--ln-ink-soft)" }}>
              {hero.heartbeat?.current ? (
                <>
                  {t.heroPhase(
                    hero.heartbeat.current.phase,
                    hero.heartbeat.current.engine,
                    hero.heartbeat.current.model,
                    fmtDur(
                      Number.isFinite(Date.parse(hero.heartbeat.current.started_at))
                        ? Math.max(0, Math.floor((nowMs - Date.parse(hero.heartbeat.current.started_at)) / 1000))
                        : 0,
                    ),
                  )}
                </>
              ) : (
                t.heartbeatBetweenPhases
              )}
            </p>
            <div className="mt-3">
              {pendingStopPack === hero.name ? (
                <span className="inline-flex flex-wrap items-center gap-2">
                  <span className="text-xs" style={{ color: "var(--ln-ink-soft)" }}>{t.confirmStop}</span>
                  <Button
                    size="xs"
                    disabled={busyPack === hero.name}
                    onClick={() => onStop(hero.name)}
                    className={NIGHT_FOCUS}
                    style={{ background: "var(--ln-fail)", color: "var(--ln-ink)" }}
                  >
                    {busyPack === hero.name ? "…" : t.confirmYes}
                  </Button>
                  <Button size="xs" ghost onClick={() => onSetPendingStop(null)} className={NIGHT_FOCUS} style={{ color: "var(--ln-ink-soft)" }}>{t.confirmNo}</Button>
                </span>
              ) : (
                <Button size="xs" ghost onClick={() => onSetPendingStop(hero.name)} className={NIGHT_FOCUS} style={{ color: "var(--ln-fail)" }}>
                  <Square className="h-3.5 w-3.5" />{t.actions.stop}
                </Button>
              )}
            </div>
          </div>
        </div>
      ) : (
        <div className="flex flex-wrap items-center gap-4">
          <LoopRing size={112} state="idle" />
          <div className="min-w-0 flex-1">
            <p className="text-xl font-semibold" style={{ ...displayFont, color: "var(--ln-ink)" }}>{t.heroSleeping}</p>
            <p className="mt-1 text-sm" style={{ ...monoFont, color: "var(--ln-ink-soft)" }}>
              {(() => {
                const timerCount = packs.filter((p) => p.timer_enabled).length;
                return timerCount > 0 ? t.heroTimerActive(timerCount) : t.heroTimerNone;
              })()}
            </p>
            {(() => {
              const last = newestGlobalEvent(packs);
              if (!last) return null;
              return (
                <p className="mt-0.5 text-sm" style={{ ...monoFont, color: "var(--ln-ink-mute)" }}>
                  {t.heroLastEvent(last.phase, last.rc === 0, ageFromIso(last.at, nowMs))}
                </p>
              );
            })()}
          </div>
        </div>
      )}
    </section>
  );
}

// ── Grid (exported — von den Tests direkt gerendert) ────────────────────────

export interface LoopsGridProps {
  packs: LoopPack[];
  models: LoopModelsResponse | null;
  selectedPack: string | null;
  detail: LoopDetailResponse | null;
  detailLoading: boolean;
  detailError: string | null;
  busyPack: string | null;
  actionErrorByPack: Record<string, string>;
  landNoteByPack: Record<string, string>;
  startOpenPack: string | null;
  pendingStopPack: string | null;
  pendingLandPack: string | null;
  workshopOpenPack: string | null;
  files: LoopFilesResponse | null;
  filesLoading: boolean;
  filesError: string | null;
  fileSaveBusy: boolean;
  fileSaveError: string | null;
  duplicateBusy: boolean;
  duplicateError: string | null;
  /** Referenz-„jetzt" für Heartbeat-Dauer/Ring-Fortschritt — Default Date.now(), im Test injizierbar. */
  nowMs?: number;
  onSetPendingStop: (name: string | null) => void;
  onSetPendingLand: (name: string | null) => void;
  onToggleDetail: (name: string) => void;
  onToggleWorkshop: (name: string) => void;
  onOpenStart: (name: string) => void;
  onCloseStart: () => void;
  onSubmitStart: (name: string, overrides: Record<string, string>) => void;
  onStop: (name: string) => void;
  onLand: (name: string) => void;
  onToggleTimer: (name: string, enabled: boolean) => void;
  onSaveFile: (pack: string, filename: string, content: string) => void;
  onDuplicate: (source: string, name: string) => void;
}

/** Pure presentation — Lagebild-Hero + Karten-Flotte. Exportiert für Tests
 *  (mit Fixtures gerendert); `LoopsView` unten verdrahtet die Hooks. */
export function LoopsGrid({
  packs,
  models,
  selectedPack,
  detail,
  detailLoading,
  detailError,
  busyPack,
  actionErrorByPack,
  landNoteByPack,
  startOpenPack,
  pendingStopPack,
  pendingLandPack,
  workshopOpenPack,
  files,
  filesLoading,
  filesError,
  fileSaveBusy,
  fileSaveError,
  duplicateBusy,
  duplicateError,
  nowMs = nowSec() * 1000,
  onSetPendingStop,
  onSetPendingLand,
  onToggleDetail,
  onToggleWorkshop,
  onOpenStart,
  onCloseStart,
  onSubmitStart,
  onStop,
  onLand,
  onToggleTimer,
  onSaveFile,
  onDuplicate,
}: LoopsGridProps) {
  if (packs.length === 0) {
    return (
      <div className="rounded-2xl border border-dashed p-8 text-center" style={{ borderColor: "var(--ln-line)" }}>
        <p className="text-base font-semibold" style={{ ...displayFont, color: "var(--ln-ink)" }}>{t.empty}</p>
        <p className="mt-1 text-sm" style={{ color: "var(--ln-ink-soft)" }}>{t.emptyHint}</p>
      </div>
    );
  }

  const runnable = packs.filter((p): p is LoopPackSummary => !isLoopPackError(p));

  return (
    <div className="space-y-4">
      <LoopsHero
        packs={runnable}
        nowMs={nowMs}
        busyPack={busyPack}
        pendingStopPack={pendingStopPack}
        onSetPendingStop={onSetPendingStop}
        onStop={onStop}
      />
      <div className="grid gap-3 sm:grid-cols-2 xl:grid-cols-3 sm:gap-4">
        {packs.map((pack) =>
          isLoopPackError(pack) ? (
            <LoopErrorCard key={pack.name} pack={pack} />
          ) : (
            <LoopCard
              key={pack.name}
              pack={pack}
              models={models}
              selected={selectedPack === pack.name}
              detail={selectedPack === pack.name ? detail : null}
              detailLoading={selectedPack === pack.name ? detailLoading : false}
              detailError={selectedPack === pack.name ? detailError : null}
              busy={busyPack === pack.name}
              actionError={actionErrorByPack[pack.name]}
              landNote={landNoteByPack[pack.name]}
              startOpen={startOpenPack === pack.name}
              pendingStop={pendingStopPack === pack.name}
              pendingLand={pendingLandPack === pack.name}
              workshopOpen={workshopOpenPack === pack.name}
              files={workshopOpenPack === pack.name ? files : null}
              filesLoading={workshopOpenPack === pack.name ? filesLoading : false}
              filesError={workshopOpenPack === pack.name ? filesError : null}
              fileSaveBusy={fileSaveBusy}
              fileSaveError={fileSaveError}
              duplicateBusy={duplicateBusy}
              duplicateError={duplicateError}
              nowMs={nowMs}
              onSetPendingStop={onSetPendingStop}
              onSetPendingLand={onSetPendingLand}
              onToggleDetail={onToggleDetail}
              onToggleWorkshop={onToggleWorkshop}
              onOpenStart={onOpenStart}
              onCloseStart={onCloseStart}
              onSubmitStart={onSubmitStart}
              onStop={onStop}
              onLand={onLand}
              onToggleTimer={onToggleTimer}
              onSaveFile={onSaveFile}
              onDuplicate={onDuplicate}
            />
          ),
        )}
      </div>
    </div>
  );
}

export function LoopsView() {
  useNightFontInjection();
  const loops = useLoops();
  const models = useLoopModels();
  const [selectedPack, setSelectedPack] = useState<string | null>(null);
  const [startOpenPack, setStartOpenPack] = useState<string | null>(null);
  const [pendingStopPack, setPendingStopPack] = useState<string | null>(null);
  const [pendingLandPack, setPendingLandPack] = useState<string | null>(null);
  const [workshopOpenPack, setWorkshopOpenPack] = useState<string | null>(null);
  const [busyPack, setBusyPack] = useState<string | null>(null);
  const [actionErrorByPack, setActionErrorByPack] = useState<Record<string, string>>({});
  const [landNoteByPack, setLandNoteByPack] = useState<Record<string, string>>({});
  const [fileSaveBusy, setFileSaveBusy] = useState(false);
  const [fileSaveError, setFileSaveError] = useState<string | null>(null);
  const [duplicateBusy, setDuplicateBusy] = useState(false);
  const [duplicateError, setDuplicateError] = useState<string | null>(null);
  const detail = useLoopDetail(selectedPack);
  const files = useLoopFiles(workshopOpenPack);

  const packs = loops.data?.packs ?? [];

  const clearActionError = (name: string) =>
    setActionErrorByPack((prev) => {
      if (!(name in prev)) return prev;
      const next = { ...prev };
      delete next[name];
      return next;
    });

  const handleToggleDetail = (name: string) => setSelectedPack((prev) => (prev === name ? null : name));
  const handleOpenStart = (name: string) => {
    clearActionError(name);
    setStartOpenPack(name);
  };
  const handleCloseStart = () => setStartOpenPack(null);

  const handleSubmitStart = async (name: string, overrides: Record<string, string>) => {
    setBusyPack(name);
    clearActionError(name);
    try {
      await startLoop(name, overrides);
      setStartOpenPack(null);
      await loops.reload();
    } catch (e) {
      setActionErrorByPack((prev) => ({ ...prev, [name]: `${t.startFailed}: ${extractDetail(e)}` }));
    } finally {
      setBusyPack(null);
    }
  };

  const handleStop = async (name: string) => {
    setBusyPack(name);
    clearActionError(name);
    try {
      await stopLoop(name);
      await loops.reload();
    } catch (e) {
      setActionErrorByPack((prev) => ({ ...prev, [name]: `${t.stopFailed}: ${extractDetail(e)}` }));
    } finally {
      setBusyPack(null);
      setPendingStopPack(null);
    }
  };

  const handleToggleTimer = async (name: string, enabled: boolean) => {
    setBusyPack(name);
    clearActionError(name);
    try {
      await toggleLoopTimer(name, enabled);
      await loops.reload();
    } catch (e) {
      setActionErrorByPack((prev) => ({ ...prev, [name]: `${t.timerFailed}: ${extractDetail(e)}` }));
    } finally {
      setBusyPack(null);
    }
  };

  const handleLand = async (name: string) => {
    setBusyPack(name);
    clearActionError(name);
    try {
      const result = await landLoop(name);
      setLandNoteByPack((prev) => ({ ...prev, [name]: `${t.landStarted} (${result.log}): ${result.note}` }));
      setSelectedPack(name); // Detail-Logbuch nach Auslösen sichtbar machen
      await loops.reload();
    } catch (e) {
      setActionErrorByPack((prev) => ({ ...prev, [name]: `${t.landFailed}: ${extractDetail(e)}` }));
    } finally {
      setBusyPack(null);
      setPendingLandPack(null);
    }
  };

  const handleToggleWorkshop = (name: string) => {
    setWorkshopOpenPack((prev) => (prev === name ? null : name));
    setFileSaveError(null);
    setDuplicateError(null);
  };

  const handleSaveFile = async (pack: string, filename: string, content: string) => {
    setFileSaveBusy(true);
    setFileSaveError(null);
    try {
      await saveLoopFile(pack, filename, content);
      await files.reload();
    } catch (e) {
      setFileSaveError(extractDetail(e));
    } finally {
      setFileSaveBusy(false);
    }
  };

  const handleDuplicate = async (source: string, name: string) => {
    setDuplicateBusy(true);
    setDuplicateError(null);
    try {
      await duplicateLoop(source, name);
      await loops.reload();
    } catch (e) {
      setDuplicateError(extractDetail(e));
    } finally {
      setDuplicateBusy(false);
    }
  };

  return (
    <div className="loops-night relative isolate space-y-5" style={NIGHT_VARS}>
      <div
        aria-hidden
        className="pointer-events-none absolute inset-x-0 top-0 -z-10 h-[420px]"
        style={{
          background:
            "radial-gradient(ellipse 900px 420px at 50% -10%, rgba(255,180,84,0.07), transparent 60%), " +
            "linear-gradient(180deg, #1A2344 0%, transparent 70%)",
        }}
      />
      <header>
        <p className="text-[11px] uppercase tracking-[0.2em]" style={{ ...monoFont, color: "var(--ln-ink-soft)" }}>{t.eyebrow}</p>
        <div className="mt-1 flex flex-wrap items-baseline gap-x-3 gap-y-1">
          <h2 className="text-[28px] font-semibold md:text-[36px]" style={{ ...displayFont, color: "var(--ln-ink)" }}>{t.title}</h2>
          <span className="text-sm" style={{ ...monoFont, color: "var(--ln-ink-mute)" }}>{t.subtitle}</span>
        </div>
      </header>

      {loops.error ? <ToneCallout tone="amber">{t.error}</ToneCallout> : null}

      <div className="flex items-center gap-2 text-xs" style={{ ...monoFont, color: "var(--ln-ink-mute)" }}>{t.packCount(packs.length)}</div>

      <LoopsGrid
        packs={packs}
        models={models.data}
        selectedPack={selectedPack}
        detail={detail.data}
        detailLoading={detail.loading}
        detailError={detail.error}
        busyPack={busyPack}
        actionErrorByPack={actionErrorByPack}
        landNoteByPack={landNoteByPack}
        startOpenPack={startOpenPack}
        pendingStopPack={pendingStopPack}
        pendingLandPack={pendingLandPack}
        workshopOpenPack={workshopOpenPack}
        files={files.data}
        filesLoading={files.loading}
        filesError={files.error}
        fileSaveBusy={fileSaveBusy}
        fileSaveError={fileSaveError}
        duplicateBusy={duplicateBusy}
        duplicateError={duplicateError}
        onSetPendingStop={setPendingStopPack}
        onSetPendingLand={setPendingLandPack}
        onToggleDetail={handleToggleDetail}
        onToggleWorkshop={handleToggleWorkshop}
        onOpenStart={handleOpenStart}
        onCloseStart={handleCloseStart}
        onSubmitStart={(name, overrides) => void handleSubmitStart(name, overrides)}
        onStop={(name) => void handleStop(name)}
        onLand={(name) => void handleLand(name)}
        onToggleTimer={(name, enabled) => void handleToggleTimer(name, enabled)}
        onSaveFile={(pack, filename, content) => void handleSaveFile(pack, filename, content)}
        onDuplicate={(source, name) => void handleDuplicate(source, name)}
      />
    </div>
  );
}
