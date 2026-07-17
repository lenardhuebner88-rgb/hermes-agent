import { useEffect, useId, useMemo, useRef, useState } from "react";
import type { ReactNode } from "react";
import { m, useReducedMotion } from "motion/react";
import {
  Anchor,
  AlertTriangle,
  CheckCircle2,
  ChevronDown,
  FolderGit2,
  PauseCircle,
  Play,
  RefreshCw,
  Search,
  Square,
  Workflow,
  Wrench,
  XCircle,
} from "lucide-react";
import type { LucideIcon } from "lucide-react";
import { Button } from "@nous-research/ui/ui/components/button";
import { cn } from "@/lib/utils";
import { extractDetail } from "../hooks/internal";
import { duplicateLoop, landLoop, saveLoopFile, setLoopTimerSchedule, startLoop, stopLoop, toggleLoopTimer, useLoopDetail, useLoopFiles, useLoopModels, useLoops } from "../hooks/loops";
import { de } from "../i18n/de";
import { SignalLabel, type SignalTone } from "../components/leitstand";
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
import { formatLoopTimestamp, loopElapsedSeconds, parseLoopTimestamp, useLoopNowMs } from "../lib/loopTime";
import type { LoopRingSegment, LoopRingTicks } from "../lib/loopRing";
import { fmtAge, fmtDur, nowSec } from "../lib/derive";

const t = de.loops;

/**
 * "Nachtschicht" — dunklere warm-graphitene Tiefenvariation aus den geteilten
 * Sheet-Tokens (One-Skin-Doktrin); der frühere Navy-Fork ist tot. Alle
 * Farben/Fonts hängen an den `--ln-*`-Custom-Properties unten, gesetzt auf dem
 * View-Root (`NIGHT_VARS`). Da diese
 * Datei sowohl die hook-verdrahtete `LoopsView` als auch die reine
 * `LoopsGrid` (von den Tests direkt gerendert, ohne den Root-Wrapper) exportiert,
 * referenzieren alle `var(--ln-…)`-Aufrufe nur Farben/Fonts — layoutrelevante
 * Tailwind-Klassen bleiben unabhängig davon funktionsfähig.
 *
 * Mono seit W3-5 (2026-07-10): der lokale View-eigene Mono-Font-Fork (seit
 * W1-A anderswo im Dashboard schon abgelöst) ist entfernt. Daten-Spans (IDs,
 * Timer, Zähler, Zeitstempel, Ledger-/Commit-/Override-Zeilen) tragen jetzt
 * die geteilte `font-data`-Klasse (IBM Plex Mono, `--font-data` aus
 * theme.css); Prosa/Labels wurden de-mono't. Display seit W3-5 ebenfalls auf
 * dem Sheet-Token (`--ln-font-display` → var(--font-display), Archivo): der
 * frühere Bricolage-Runtime-Loader holte Google Fonts zur Laufzeit und
 * verletzte die W1-A-Doktrin "zero network font requests".
 */
const NIGHT_VARS = {
  "--ln-void": "color-mix(in oklab, var(--color-surface-0) 72%, black)",
  "--ln-surface": "color-mix(in oklab, var(--color-surface-1) 82%, black)",
  "--ln-raised": "color-mix(in oklab, var(--color-surface-2) 85%, black)",
  "--ln-line": "color-mix(in oklab, var(--color-line) 80%, black)",
  "--ln-ink": "var(--color-ink)",
  "--ln-ink-soft": "var(--color-ink-2)",
  "--ln-ink-mute": "var(--color-ink-3)",
  "--ln-sodium": "var(--color-bronze-hi)",
  "--ln-sodium-ink": "var(--color-surface-0)",
  "--ln-ok": "var(--color-status-ok)",
  "--ln-fail": "var(--color-status-alert)",
  "--ln-warn": "var(--color-status-warn)",
  "--ln-header-glow": "color-mix(in oklab, var(--color-bronze) 10%, var(--color-surface-0))",
  // Display-Stimme = Sheet-Token (Archivo Variable, self-hosted). Der frühere
  // Bricolage-Runtime-Loader von Google Fonts verletzte die W1-A-Doktrin
  // "zero network font requests" und ist entfernt (W3-5 Director-Fix).
  "--ln-font-display": "var(--font-display)",
} as React.CSSProperties;

const displayFont: React.CSSProperties = { fontFamily: "var(--ln-font-display)" };

/** A11y-Boden ohne Ankündigung: 2px Amber-Fokusring auf jedem interaktiven
 *  Element (der Shared-`Button` bringt selbst nur den Einheitslook-Accent
 *  mit — hier explizit auf --ln-sodium überschrieben). */
const NIGHT_FOCUS =
  "focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-[var(--ln-sodium)] focus-visible:ring-offset-2 focus-visible:ring-offset-[var(--ln-void)]";

const NIGHT_CALLOUT_LABEL: Record<SignalTone, string> = {
  ok: "Erledigt",
  warn: "Hinweis",
  alert: "Fehler",
  neutral: "Status",
};

function NightCallout({ tone, children }: { tone: SignalTone; children: ReactNode }) {
  return (
    <div
      className="flex items-start gap-2 rounded-card border px-3 py-2 text-sec"
      style={{ borderColor: "var(--ln-line)", background: "var(--ln-raised)", color: "var(--ln-ink-soft)" }}
    >
      <SignalLabel tone={tone} label={NIGHT_CALLOUT_LABEL[tone]} className="mt-0.5 shrink-0" />
      <span className="min-w-0">{children}</span>
    </div>
  );
}
// @nous Button setzt mono immer intern. Leitstand-Aktionen sind Chrome, keine
// Daten: der wichtige Display-Override gewinnt gezielt über diesen Dependency-
// Default; Datenfelder und Datei-/Zeitwerte behalten font-data.
const NIGHT_ACTION_CLASS = cn(NIGHT_FOCUS, "!font-display");

/** DONE W6-4 — Engine-Identität nutzt die geteilte DATA-Palette, nie Skin,
 *  Status oder Interaktion. Nur als Punkt neben dem Namen, nie farbe-allein
 *  (immer Text daneben). Unbekannte Engines: ink-mute. */
const ENGINE_COLOR: Record<string, string> = {
  claude: "var(--color-data-1)",
  codex: "var(--color-data-4)",
  kimi: "var(--color-data-5)",
  hermes: "var(--color-data-2)",
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
  const ms = parseLoopTimestamp(iso);
  if (ms === null) return "—";
  return fmtAge(Math.floor(ms / 1000), Math.floor(nowMs / 1000));
}

/** Pro Phase gebaute overrides — nur Felder, die vom Manifest-Default abweichen. */
function buildPhaseOverrides(
  pack: LoopPackSummary,
  phaseValues: Record<string, { engine: string; model: string }>,
): Record<string, string> {
  const overrides: Record<string, string> = {};
  for (const phase of Object.keys(pack.phases)) {
    const current = phaseValues[phase];
    if (!current) continue;
    const upper = phase.toUpperCase();
    overrides[`PHASE_${upper}_ENGINE`] = current.engine;
    overrides[`PHASE_${upper}_MODEL`] = current.model;
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
        <m.span
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
  // Referenziert dieselben Werte wie NIGHT_VARS statt sie zu duplizieren
  // (vorher vier literale Hex-Codes hier — W3-5 raw-hex-Aufräumung).
  const hex = { ok: "var(--ln-ok)", warn: "var(--ln-warn)", sodium: "var(--ln-sodium)", neutral: "var(--ln-ink-soft)" }[tone];
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
      style={{ borderColor: "var(--ln-line)", color: "var(--ln-ink-soft)" }}
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

// ── Landing-Zeile (Karte) ────────────────────────────────────────────────────

/** Wohin/ob eine Landung pusht — sicherheitsrelevant für den Operator (z.B.
 *  land_push=false = gewollter Dreifach-Lock, kein Alarmzustand). Pro Pack,
 *  nicht pro Repo-Gruppe: land_remote/land_gates können sich zwischen Packs
 *  im selben Repo unterscheiden. */
function LoopLandingLine({ pack }: { pack: LoopPackSummary }) {
  return (
    <p className="mt-1.5 flex flex-wrap items-center gap-x-1.5 gap-y-0.5 text-xs" style={{ color: "var(--ln-ink-soft)" }}>
      {pack.land_push ? (
        <span>
          {t.landRemoteArrow}{" "}
          <span className="font-data" style={{ color: "var(--ln-ink)" }}>{pack.land_remote}</span>
        </span>
      ) : (
        <span style={{ color: "var(--ln-ink-mute)" }}>{t.landNoAutoPush}</span>
      )}
      {pack.land_gates ? (
        <span title={pack.land_gates.join(", ")}>{t.landGatesCount(pack.land_gates.length)}</span>
      ) : null}
    </p>
  );
}

// ── Telemetrie-Zeile (Karte) ────────────────────────────────────────────────

function CardTelemetryLine({ pack, nowMs }: { pack: LoopPackSummary; nowMs: number }) {
  const reduceMotion = useReducedMotion();
  if (pack.running) {
    const current = pack.heartbeat?.current ?? null;
    if (!current) {
      return (
        <p className="mt-2 text-xs" style={{ color: "var(--ln-ink-soft)" }}>
          {t.heartbeatBetweenPhases}
        </p>
      );
    }
    const elapsedSec = loopElapsedSeconds(current.started_at, nowMs);
    if (elapsedSec === null) {
      return (
        <p className="mt-2 text-xs" role="status" style={{ color: "var(--ln-warn)" }}>
          {current.phase} · {current.model} · {t.heartbeatInvalid}
        </p>
      );
    }
    return (
      <>
        <p className="mt-2 inline-flex items-center gap-1.5 text-xs" style={{ color: "var(--ln-ink)" }}>
          <span
            aria-hidden
            className={cn("inline-block h-1.5 w-1.5 rounded-full", !reduceMotion && elapsedSec <= 30 && "animate-pulse")}
            style={{ background: elapsedSec > 30 ? "var(--ln-warn)" : "var(--ln-sodium)" }}
          />
          {t.heartbeatCurrent(current.phase, current.model, fmtDur(elapsedSec))}
        </p>
        {elapsedSec > 30 ? (
          <p className="mt-1 text-xs" role="status" style={{ color: "var(--ln-warn)" }}>
            {t.heartbeatStale(fmtDur(elapsedSec))}
          </p>
        ) : null}
      </>
    );
  }
  const last = pack.heartbeat?.last ?? [];
  const newest = last[last.length - 1];
  if (!newest) return null;
  return (
    <p className="mt-2 text-xs" style={{ color: "var(--ln-ink-soft)" }}>
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
            <span className="hidden font-data text-[10px] sm:inline" style={{ color: "var(--ln-ink-soft)" }}>
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
              <span className="block font-data text-sm font-semibold" style={{ color: "var(--ln-ink)" }}>{n}</span>
              <span className="block text-[10px] uppercase tracking-[0.06em]" style={{ color: "var(--ln-ink-soft)" }}>
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
      className="rounded border px-1 font-data text-[10px]"
      style={{ borderColor: "var(--ln-line)", color: "var(--ln-ink)" }}
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
          <div key={`${idx}-${line}`} className="flex items-start gap-2 rounded px-1 py-0.5 font-data text-xs">
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
  const caption: React.CSSProperties = { color: "var(--ln-ink-soft)" };
  return (
    <div className="space-y-3 text-xs">
      <div>
        <p className="text-[11px] uppercase tracking-[0.14em]" style={caption}>{t.detailLedger}</p>
        {detail.ledger_tail.length > 0 ? (
          <div className="mt-1"><LogbookFeed lines={detail.ledger_tail} /></div>
        ) : (
          <p className="mt-1" style={{ color: "var(--ln-ink-soft)" }}>{t.detailNoLedger}</p>
        )}
      </div>
      {detail.queue_entries ? (
        <div>
          <p className="text-[11px] uppercase tracking-[0.14em]" style={caption}>{t.detailQueue}</p>
          <ul className="mt-1 space-y-1 font-data">
            {Object.entries(detail.queue_entries).map(([stage, files]) => (
              <li key={stage} style={{ color: "var(--ln-ink-soft)" }}>
                <span style={{ color: "var(--ln-ink)" }}>{stage}</span>: {files.length > 0 ? files.join(", ") : "—"}
              </li>
            ))}
          </ul>
        </div>
      ) : null}
      <div>
        <p className="text-[11px] uppercase tracking-[0.14em]" style={caption}>{t.detailTokens}</p>
        {detail.phase_usage.length > 0 ? (
          <ul className="mt-1 space-y-1 font-data" style={{ color: "var(--ln-ink-soft)" }}>
            {detail.phase_usage.map((usage, index) => (
              <li key={`${usage.ts}-${usage.phase}-${index}`}>
                {usage.round ? `R${usage.round} · ` : ""}{usage.phase} · {usage.model} · {usage.total_tokens != null ? t.tokenUsage(usage.total_tokens) : "Tokens —"}
                {usage.billing === "subscription" && usage.metered_cost_eur === 0 ? ` · ${t.subscriptionZeroMetered}` : ""}
              </li>
            ))}
          </ul>
        ) : (
          <p className="mt-1" style={{ color: "var(--ln-ink-soft)" }}>{t.detailNoTokens}</p>
        )}
      </div>
      <div>
        <p className="text-[11px] uppercase tracking-[0.14em]" style={caption}>{t.detailCommits}</p>
        {detail.commits.length > 0 ? (
          <ul className="mt-1 space-y-0.5 font-data" style={{ color: "var(--ln-ink-soft)" }}>
            {detail.commits.map((line) => <li key={line}>{line}</li>)}
          </ul>
        ) : (
          <p className="mt-1" style={{ color: "var(--ln-ink-soft)" }}>{t.detailNoCommits}</p>
        )}
      </div>
      <div>
        <p className="text-[11px] uppercase tracking-[0.14em]" style={caption}>{t.detailOverrides}</p>
        {Object.keys(detail.overrides).length > 0 ? (
          <ul className="mt-1 space-y-0.5 font-data" style={{ color: "var(--ln-ink-soft)" }}>
            {Object.entries(detail.overrides).map(([key, value]) => <li key={key}>{key}={value}</li>)}
          </ul>
        ) : (
          <p className="mt-1" style={{ color: "var(--ln-ink-soft)" }}>{t.detailNoOverrides}</p>
        )}
      </div>
    </div>
  );
}

// ── Formulare / Felder ───────────────────────────────────────────────────────

// font-data: Feldwerte (Engine/Modell-Namen, Zahlen, Param-/Datei-Inhalte)
// sind Daten, kein Fließtext — geteilte Klasse statt lokalem Mono-Fork.
const NIGHT_FIELD_CLASS = cn(
  "min-h-12 w-full rounded-md border px-2 py-1.5 text-base sm:text-sm placeholder:text-[var(--ln-ink-mute)] disabled:opacity-60 font-data",
  NIGHT_FOCUS,
);
const nightFieldStyle: React.CSSProperties = {
  backgroundColor: "var(--ln-void)",
  borderColor: "var(--ln-line)",
  color: "var(--ln-ink)",
};

interface LoopModelChoice {
  engine: string;
  engineLabel: string;
  model: string;
}

function LoopModelPicker({
  phase,
  value,
  models,
  busy,
  onChange,
}: {
  phase: string;
  value: { engine: string; model: string };
  models: LoopModelsResponse | null;
  busy: boolean;
  onChange: (next: { engine: string; model: string }) => void;
}) {
  const [open, setOpen] = useState(false);
  const [query, setQuery] = useState("");
  const panelId = useId();
  const triggerId = useId();
  const rootRef = useRef<HTMLDivElement>(null);
  const choices = useMemo<LoopModelChoice[]>(() =>
    Object.entries(models?.engines ?? {}).flatMap(([engine, entry]) =>
      entry.models.map((model) => ({ engine, engineLabel: entry.label || engine, model })),
    ), [models]);
  const normalized = query.trim().toLocaleLowerCase("de");
  const filtered = normalized
    ? choices.filter((choice) =>
        `${choice.engine} ${choice.engineLabel} ${choice.model}`.toLocaleLowerCase("de").includes(normalized),
      )
    : choices;

  useEffect(() => {
    if (!open) return;
    const closeOnOutsidePointer = (event: PointerEvent) => {
      if (event.target instanceof Node && !rootRef.current?.contains(event.target)) {
        setOpen(false);
        setQuery("");
      }
    };
    document.addEventListener("pointerdown", closeOnOutsidePointer);
    return () => document.removeEventListener("pointerdown", closeOnOutsidePointer);
  }, [open]);

  const closePicker = () => {
    setOpen(false);
    setQuery("");
  };
  const restoreTriggerFocus = () => {
    queueMicrotask(() => document.getElementById(triggerId)?.focus());
  };

  return (
    <div ref={rootRef} className="relative min-w-0">
      <button
        id={triggerId}
        type="button"
        disabled={busy || choices.length === 0}
        aria-expanded={open}
        aria-controls={open ? panelId : undefined}
        aria-label={t.chooseModel(phase)}
        onClick={() => setOpen((current) => !current)}
        className={cn(
          "flex min-h-12 w-full items-center gap-2 rounded-md border px-3 py-2 text-left disabled:opacity-60",
          NIGHT_FOCUS,
        )}
        style={nightFieldStyle}
      >
        <EngineDot engine={value.engine} />
        <span className="min-w-0 flex-1">
          <span className="block truncate font-data text-sm" style={{ color: "var(--ln-ink)" }}>{value.model}</span>
          <span className="block truncate text-[11px]" style={{ color: "var(--ln-ink-soft)" }}>
            {models?.engines[value.engine]?.label ?? value.engine}
          </span>
        </span>
        <ChevronDown aria-hidden className={cn("h-4 w-4 shrink-0 transition-transform", open && "rotate-180")} />
      </button>
      {open ? (
        <div
          id={panelId}
          role="region"
          aria-label={t.modelCatalogTitle(phase)}
          className="mt-2 rounded-xl border p-2 sm:p-3"
          style={{ borderColor: "var(--ln-sodium)", background: "var(--ln-void)" }}
        >
          <label className="relative block">
            <Search aria-hidden className="absolute left-3 top-1/2 h-4 w-4 -translate-y-1/2" style={{ color: "var(--ln-ink-mute)" }} />
            <input
              type="search"
              value={query}
              autoFocus
              aria-label={t.modelSearch}
              placeholder={t.modelSearch}
              onChange={(event) => setQuery(event.target.value)}
              onKeyDown={(event) => {
                if (event.key === "Escape") {
                  event.preventDefault();
                  closePicker();
                  restoreTriggerFocus();
                }
              }}
              className={cn(NIGHT_FIELD_CLASS, "pl-9")}
              style={nightFieldStyle}
            />
          </label>
          <div className="mt-2 max-h-72 space-y-1 overflow-y-auto overscroll-contain">
            {filtered.length > 0 ? filtered.map((choice) => {
              const selected = choice.engine === value.engine && choice.model === value.model;
              return (
                <button
                  key={`${choice.engine}:${choice.model}`}
                  type="button"
                  aria-pressed={selected}
                  onClick={() => {
                    onChange({ engine: choice.engine, model: choice.model });
                    closePicker();
                    restoreTriggerFocus();
                  }}
                  className={cn("flex min-h-12 w-full items-center gap-2 rounded-lg border px-3 py-2 text-left", NIGHT_FOCUS)}
                  style={{
                    borderColor: selected ? "var(--ln-sodium)" : "var(--ln-line)",
                    background: selected ? "var(--ln-raised)" : "transparent",
                    color: "var(--ln-ink)",
                  }}
                >
                  <EngineDot engine={choice.engine} />
                  <span className="min-w-0 flex-1">
                    <span className="block break-all font-data text-sm">{choice.model}</span>
                    <span className="block text-[11px]" style={{ color: "var(--ln-ink-soft)" }}>{choice.engineLabel}</span>
                  </span>
                  {selected ? <CheckCircle2 aria-hidden className="h-4 w-4 shrink-0" style={{ color: "var(--ln-ok)" }} /> : null}
                </button>
              );
            }) : (
              <p className="px-2 py-4 text-center text-xs" style={{ color: "var(--ln-ink-soft)" }}>{t.modelCatalogEmpty}</p>
            )}
          </div>
        </div>
      ) : null}
    </div>
  );
}

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

  const captionStyle: React.CSSProperties = { color: "var(--ln-ink-soft)" };

  const handleSubmit = () => {
    const overrides = buildPhaseOverrides(pack, phaseValues);
    if (maxRounds.trim() && maxRounds !== defaultMaxRounds) overrides.MAX_ROUNDS = maxRounds.trim();
    if (maxHours.trim() && maxHours !== defaultMaxHours) overrides.MAX_HOURS = maxHours.trim();
    if (!pack.autoland && skipPlan) overrides.SKIP_PLAN = "1";
    if (!pack.autoland) {
      for (const name of paramNames) {
        const value = (paramValues[name] ?? "").trim();
        if (value && value !== pack.params[name]) overrides[name.toUpperCase()] = value;
      }
    }
    onSubmit(overrides);
  };

  const failStreak = pack.stop.fail_streak;
  const dryRounds = pack.stop.dry_rounds;

  return (
    <div className="space-y-3">
      <p className="text-[11px] uppercase tracking-[0.14em]" style={captionStyle}>{t.startPanelTitle}</p>
      {pack.autoland ? (
        <p className="rounded-md border px-3 py-2 text-xs leading-relaxed" style={{ borderColor: "var(--ln-sodium)", background: "var(--ln-raised)", color: "var(--ln-ink-soft)" }}>
          {t.autolandContractHint}
        </p>
      ) : null}
      {phaseNames.map((phase) => {
        const value = phaseValues[phase];
        return (
          <div key={phase} className="grid gap-2 rounded-xl border p-3 sm:grid-cols-[minmax(0,90px)_minmax(0,1fr)] sm:items-center" style={{ borderColor: "var(--ln-line)" }}>
            <span className="font-data text-xs font-semibold uppercase tracking-[0.1em]" style={{ color: "var(--ln-ink)" }}>{phase}</span>
            <LoopModelPicker
              phase={phase}
              value={value}
              models={models}
              busy={busy}
              onChange={(next) => setPhaseValues((prev) => ({ ...prev, [phase]: next }))}
            />
          </div>
        );
      })}
      <div className="grid gap-2 sm:grid-cols-2">
        <label className="min-w-0">
          <span className="text-[11px] uppercase tracking-[0.08em]" style={captionStyle}>{t.maxRoundsLabel}</span>
          <input
            type="number"
            min={1}
            max={50}
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
            max={24}
            value={maxHours}
            disabled={busy}
            onChange={(e) => setMaxHours(e.target.value)}
            className={cn(NIGHT_FIELD_CLASS, "mt-1")}
            style={nightFieldStyle}
          />
        </label>
      </div>
      {!pack.autoland && pack.type === "pipeline" ? (
        <label className="inline-flex min-h-9 items-center gap-2 text-xs" style={{ color: "var(--ln-ink-soft)" }}>
          <input
            type="checkbox"
            checked={skipPlan}
            disabled={busy}
            aria-label={t.skipPlanLabel}
            onChange={(e) => setSkipPlan(e.target.checked)}
            // size-12 (vorher unbemaßt = Browser-Default ~13px, undersized;
            // s. Kommentar an der Nachttimer-Checkbox oben — box-content+
            // padding wird von Chromium für Checkboxen ignoriert).
            className={cn("size-12 shrink-0 accent-[var(--ln-sodium)]", NIGHT_FOCUS)}
          />
          {t.skipPlanLabel}
        </label>
      ) : null}
      {failStreak != null && dryRounds != null ? (
        <p className="text-xs" style={{ color: "var(--ln-ink-soft)" }}>
          {t.stopCriteria(failStreak, dryRounds)}
        </p>
      ) : null}
      {!pack.autoland ? paramNames.map((name) => (
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
      )) : null}
      <div className="flex flex-wrap items-center gap-2">
        <Button
          size="sm"
          disabled={busy}
          onClick={handleSubmit}
          className={cn("border-0", NIGHT_ACTION_CLASS)}
          style={{ background: "var(--ln-sodium)", color: "var(--ln-sodium-ink)" }}
        >
          {busy ? "…" : t.submitStart}
        </Button>
        <Button size="sm" ghost disabled={busy} onClick={onCancel} className={NIGHT_ACTION_CLASS} style={{ color: "var(--ln-ink-soft)" }}>
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
      {!file.editable ? <NightCallout tone="warn">{t.workshopReadOnly}</NightCallout> : null}
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
            size="sm"
            disabled={saveBusy || draft === file.content}
            onClick={() => onSave(file.name, draft)}
            className={cn("border-0", NIGHT_ACTION_CLASS)}
            style={{ background: "var(--ln-sodium)", color: "var(--ln-sodium-ink)" }}
          >
            {saveBusy ? "…" : t.workshopSave}
          </Button>
          {saveError ? <NightCallout tone="alert">{t.workshopSaveFailed}: {saveError}</NightCallout> : null}
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

  if (loading) return <p className="text-xs" style={{ color: "var(--ln-ink-soft)" }}>{t.loading}</p>;
  if (error) return <NightCallout tone="alert">{t.workshopError}: {error}</NightCallout>;
  if (!files || fileList.length === 0) return <p className="text-xs" style={{ color: "var(--ln-ink-soft)" }}>{t.workshopEmpty}</p>;

  return (
    <div className="space-y-3 text-xs">
      <p className="text-[11px] uppercase tracking-[0.14em]" style={{ color: "var(--ln-ink-mute)" }}>{t.workshopTitle}</p>
      <div className="flex flex-wrap gap-1.5">
        {fileList.map((f) => {
          const isActive = f.name === active?.name;
          return (
            <button
              key={f.name}
              type="button"
              onClick={() => setActiveName(f.name)}
              // min-h-12 (45px @15px-Root): Datei-Tab ist ein <button> und muss
              // das 44px-Touch-AC erfüllen (Codex-P1 W3-5; py-2 ergab nur ~30px).
              className={cn("inline-flex min-h-12 items-center rounded-full border px-2.5 font-data text-[11px]", NIGHT_FOCUS)}
              style={{
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
        <p className="text-[11px] uppercase tracking-[0.14em]" style={{ color: "var(--ln-ink-mute)" }}>{t.workshopDuplicateTitle}</p>
        <div className="mt-1 flex flex-wrap items-center gap-2">
          <input
            type="text"
            value={dupName}
            disabled={duplicateBusy}
            placeholder={t.workshopDuplicatePlaceholder}
            aria-label={t.workshopDuplicateTitle}
            onChange={(e) => setDupName(e.target.value)}
            className={cn(NIGHT_FIELD_CLASS, "w-auto")}
            style={nightFieldStyle}
          />
          <Button
            size="sm"
            disabled={duplicateBusy || !dupName.trim()}
            onClick={() => onDuplicate(dupName.trim())}
            className={cn("border-0", NIGHT_ACTION_CLASS)}
            style={{ background: "var(--ln-sodium)", color: "var(--ln-sodium-ink)" }}
          >
            {duplicateBusy ? "…" : t.workshopDuplicateSubmit}
          </Button>
        </div>
        {duplicateError ? <div className="mt-2"><NightCallout tone="alert">{t.workshopDuplicateFailed}: {duplicateError}</NightCallout></div> : null}
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
      <div className="mt-3"><NightCallout tone="alert">{pack.error}</NightCallout></div>
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
  onSaveTimerSchedule: (name: string, time: string) => void;
  onSaveFile: (pack: string, filename: string, content: string) => void;
  onDuplicate: (source: string, name: string) => void;
}

function LoopCardAction({
  children,
  disabled,
  onClick,
  tone = "neutral",
  filled = false,
}: {
  children: ReactNode;
  disabled?: boolean;
  onClick: () => void;
  tone?: "neutral" | "bronze" | "alert";
  filled?: boolean;
}) {
  const color = tone === "alert" ? "var(--ln-fail)" : tone === "bronze" ? "var(--ln-sodium)" : "var(--ln-ink-soft)";
  return (
    <button
      type="button"
      disabled={disabled}
      onClick={onClick}
      className={cn(
        "inline-flex min-h-12 items-center justify-center gap-2 rounded-lg border px-3 text-xs font-semibold disabled:opacity-50",
        NIGHT_FOCUS,
        "!font-display",
      )}
      style={{
        borderColor: color,
        background: filled ? color : "transparent",
        color: filled ? (tone === "alert" ? "var(--ln-ink)" : "var(--ln-sodium-ink)") : color,
      }}
    >
      {children}
    </button>
  );
}

const TIMER_TIME_RE = /^(?:[01]\d|2[0-3]):[0-5]\d$/;

function TimerScheduleControl({
  pack,
  busy,
  onToggleTimer,
  onSaveTimerSchedule,
}: {
  pack: LoopPackSummary;
  busy: boolean;
  onToggleTimer: (name: string, enabled: boolean) => void;
  onSaveTimerSchedule: (name: string, time: string) => void;
}) {
  const [time, setTime] = useState(pack.timer_schedule);

  const valid = TIMER_TIME_RE.test(time);
  const dirty = time !== pack.timer_schedule;
  const inputId = `loop-timer-time-${pack.name}`;

  return (
    <div className="min-w-0 flex-1 rounded-xl border px-3 py-2.5" style={{ borderColor: "var(--ln-line)", background: "var(--ln-raised)" }}>
      <div className="flex flex-wrap items-center gap-x-3 gap-y-2">
        <label className="inline-flex min-h-[44px] cursor-pointer items-center gap-2 text-xs" style={{ color: "var(--ln-ink-soft)" }}>
          <input
            type="checkbox"
            checked={pack.timer_enabled}
            disabled={busy}
            aria-label={`${t.timerLabel} ${pack.name}`}
            onChange={(e) => onToggleTimer(pack.name, e.target.checked)}
            // Direkte size-12 statt box-content+padding: Chromium ignoriert
            // Padding/box-sizing für den nativen Checkbox-Rendering-Bereich
            // (gemessen, verifiziert — box-content-Variante blieb bei
            // 18.8px), respektiert aber explizite h/w. Vorher h-5 w-5
            // (18.8px, undersized — visual-verify.sh maß den <input> direkt,
            // nicht das umschließende Label).
            className={cn("size-12 shrink-0 accent-[var(--ln-sodium)]", NIGHT_FOCUS)}
          />
          <span>{t.timerLabel}: {pack.timer_enabled ? t.timerOn : t.timerOff}</span>
        </label>
        <div className="ml-auto flex min-w-0 flex-wrap items-center gap-2">
          <label htmlFor={inputId} className="text-[11px]" style={{ color: "var(--ln-ink-soft)" }}>
            {t.timerLocalTime}
          </label>
          <input
            id={inputId}
            type="time"
            step={60}
            value={time}
            disabled={busy}
            aria-label={`${t.timerTimeLabel} ${pack.name}`}
            onChange={(event) => setTime(event.target.value)}
            className={cn("h-[44px] min-w-28 rounded-lg border px-3 font-data text-sm", NIGHT_FOCUS)}
            style={{ borderColor: "var(--ln-line)", background: "var(--ln-void)", color: "var(--ln-ink)" }}
          />
          <Button
            size="sm"
            disabled={busy || !valid || !dirty}
            onClick={() => onSaveTimerSchedule(pack.name, time)}
            className={cn("min-h-[44px] border-0 px-3 disabled:opacity-40", NIGHT_ACTION_CLASS)}
            style={{ background: "var(--ln-sodium)", color: "var(--ln-sodium-ink)" }}
          >
            {busy ? "…" : t.timerSave}
          </Button>
        </div>
      </div>
      <p className="mt-1 text-[11px]" style={{ color: "var(--ln-ink-soft)" }}>
        {pack.timer_enabled
          ? pack.timer_next_run
            ? t.timerNextRun(formatLoopTimestamp(pack.timer_next_run))
            : t.timerNextRunPending
          : t.timerDisabledHint(valid ? time : pack.timer_schedule)}
      </p>
    </div>
  );
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
  onSaveTimerSchedule,
  onSaveFile,
  onDuplicate,
}: LoopCardProps) {
  const isStable = pack.stability === "stable";
  const hasStrandedBuild = !pack.running && (pack.queue?.["10-building"] ?? 0) > 0;
  const statusLabel = pack.stop_requested
    ? t.stopRequested
    : pack.running
      ? t.statusRunning
      : hasStrandedBuild
        ? t.statusInterrupted
        : t.statusIdle;
  const hasVerifiedPipelinePlan = pack.type !== "pipeline" || (pack.queue?.["20-verified"] ?? 0) > 0;
  const canLand = !pack.running && pack.commits_ahead > 0 && hasVerifiedPipelinePlan;
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

      <p className="mt-1 text-[11px] uppercase tracking-[0.08em]" style={{ color: "var(--ln-ink-soft)" }}>{statusLabel}</p>
      <p className="mt-1.5 line-clamp-2 text-[13px]" style={{ color: "var(--ln-ink-soft)" }}>{pack.description}</p>
      <LoopLandingLine pack={pack} />

      <CardTelemetryLine pack={pack} nowMs={nowMs} />
      {pack.heartbeat?.last.length ? <PhaseHistoryBars last={pack.heartbeat.last} /> : null}
      {pack.queue ? <LoopQueueStepper queue={pack.queue} /> : null}
      {pack.token_usage?.total_tokens != null ? (
        <div className="mt-2 flex flex-wrap gap-1.5">
          <NightPill tone="sodium">{t.tokenUsage(pack.token_usage.total_tokens)}</NightPill>
          {pack.token_usage.billing === "subscription" && pack.token_usage.metered_cost_eur === 0 ? (
            <NightPill tone="ok">{t.subscriptionZeroMetered}</NightPill>
          ) : null}
        </div>
      ) : null}
      {pack.commits_ahead > 0 ? (
        <div className="mt-2" title={hasVerifiedPipelinePlan ? t.commitsAheadHint : t.commitsUnverifiedHint}>
          <NightPill tone={hasVerifiedPipelinePlan ? "sodium" : "warn"}>
            <Anchor aria-hidden className="h-3.5 w-3.5" />
            {hasVerifiedPipelinePlan ? t.commitsAhead(pack.commits_ahead) : t.commitsUnverified(pack.commits_ahead)}
          </NightPill>
        </div>
      ) : null}

      <div className="mt-3 space-y-3 border-t pt-3" style={{ borderColor: "var(--ln-line)" }}>
        <TimerScheduleControl
          key={`${pack.name}:${pack.timer_schedule}`}
          pack={pack}
          busy={busy}
          onToggleTimer={onToggleTimer}
          onSaveTimerSchedule={onSaveTimerSchedule}
        />
        <div className="flex flex-wrap items-center justify-end gap-2">
          {pack.running ? (
            pendingStop ? (
              <span className="inline-flex flex-wrap items-center gap-2">
                <span className="text-xs" style={{ color: "var(--ln-ink-soft)" }}>{t.confirmStop}</span>
                <LoopCardAction disabled={busy} onClick={() => onStop(pack.name)} tone="alert" filled>
                  {busy ? "…" : t.confirmYes}
                </LoopCardAction>
                <LoopCardAction disabled={busy} onClick={() => onSetPendingStop(null)}>{t.confirmNo}</LoopCardAction>
              </span>
            ) : (
              <LoopCardAction disabled={busy} onClick={() => onSetPendingStop(pack.name)} tone="alert">
                <Square className="h-3.5 w-3.5" />{t.actions.stop}
              </LoopCardAction>
            )
          ) : (
            <>
              <LoopCardAction
                disabled={busy}
                onClick={() => onOpenStart(pack.name)}
                tone="bronze"
                filled
              >
                <Play className="h-3.5 w-3.5" />{t.actions.start}
              </LoopCardAction>
              {canLand ? (
                pendingLand ? (
                  <span className="inline-flex flex-wrap items-center gap-2">
                    <span className="text-xs" style={{ color: "var(--ln-ink-soft)" }}>{t.confirmLand}</span>
                    <LoopCardAction disabled={busy} onClick={() => onLand(pack.name)} tone="bronze" filled>
                      {busy ? "…" : t.confirmYes}
                    </LoopCardAction>
                    <LoopCardAction disabled={busy} onClick={() => onSetPendingLand(null)}>{t.confirmNo}</LoopCardAction>
                  </span>
                ) : (
                  <LoopCardAction disabled={busy} onClick={() => onSetPendingLand(pack.name)} tone="bronze">
                    <Anchor className="h-3.5 w-3.5" />{t.actions.land}
                  </LoopCardAction>
                )
              ) : null}
            </>
          )}
          <LoopCardAction disabled={busy} onClick={() => onToggleWorkshop(pack.name)}>
            <Wrench className="h-3.5 w-3.5" />{t.actions.workshop}
          </LoopCardAction>
        </div>
      </div>

      {actionError ? <div className="mt-2"><NightCallout tone="alert">{actionError}</NightCallout></div> : null}
      {landNote ? <div className="mt-2"><NightCallout tone="ok">{landNote}</NightCallout></div> : null}

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
          // inline-flex + min-h-12: die Disclosure-Trigger-<button> hat keine
          // eigene Höhe (sizet sich am Inhalt) — vorher 23.3px (undersized),
          // der Summary-Inhalt zieht die Zeile jetzt auf ≥44px hoch.
          summary={<span className="inline-flex min-h-12 items-center text-xs" style={{ color: "var(--ln-ink-soft)" }}>{t.actions.detail}</span>}
        >
          {detailLoading ? <p className="text-xs" style={{ color: "var(--ln-ink-soft)" }}>{t.loading}</p> : null}
          {detailError ? <NightCallout tone="alert">{t.detailError}</NightCallout> : null}
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
    const ms = parseLoopTimestamp(newest.at);
    if (ms !== null && ms > bestMs) {
      bestMs = ms;
      best = newest;
    }
  }
  return best;
}

const PIPELINE_PROGRESS_LABEL: Record<string, string> = {
  plan: "Plan",
  build: "Build",
  verify: "Verify",
};

function latestRound(pack: LoopPackSummary): number | null {
  const current = pack.heartbeat?.current?.round;
  if (current != null) return current;
  const history = pack.heartbeat?.last ?? [];
  for (let index = history.length - 1; index >= 0; index--) {
    if (history[index].round != null) return history[index].round!;
  }
  return null;
}

function LoopPipelineProgress({ pack, nowMs }: { pack: LoopPackSummary; nowMs: number }) {
  const segments = deriveRingSegments(pack, nowMs);
  const current = pack.heartbeat?.current ?? null;
  const maxRounds = pack.stop.max_rounds ?? 1;

  return (
    <div data-testid="loop-mobile-progress" className="mt-4 border-t pt-4" style={{ borderColor: "var(--ln-line)" }}>
      <div className="flex flex-wrap items-end justify-between gap-1.5">
        <p className="font-data text-sm font-semibold tabular-nums" style={{ color: "var(--ln-ink)" }}>
          {t.progressRound(latestRound(pack), maxRounds)}
        </p>
        <p className="text-[11px]" style={{ color: "var(--ln-ink-mute)" }}>{t.progressEvidence}</p>
      </div>
      <ol className="mt-3 grid grid-cols-1 gap-2 sm:grid-cols-3">
        {segments.map((segment, index) => {
          const active = segment.state === "current";
          const done = segment.state === "done";
          const elapsed = active && current ? loopElapsedSeconds(current.started_at, nowMs) : null;
          const status = done ? t.phaseStateDone : active ? (elapsed === null ? t.heartbeatInvalid : t.phaseStateActive(fmtDur(elapsed))) : t.phaseStateWaiting;
          return (
            <li
              key={segment.key}
              className="flex min-h-14 items-center gap-3 rounded-lg border px-3 py-2"
              style={{
                borderColor: active ? "var(--ln-sodium)" : done ? "var(--ln-ok)" : "var(--ln-line)",
                background: active ? "var(--ln-raised)" : "transparent",
              }}
            >
              <span
                aria-hidden
                className="grid size-7 shrink-0 place-items-center rounded-full border font-data text-xs"
                style={{
                  borderColor: active ? "var(--ln-sodium)" : done ? "var(--ln-ok)" : "var(--ln-line)",
                  color: active ? "var(--ln-sodium)" : done ? "var(--ln-ok)" : "var(--ln-ink-mute)",
                }}
              >
                {done ? "✓" : active ? "•" : index + 1}
              </span>
              <span className="min-w-0">
                <span className="block text-xs font-semibold uppercase tracking-[0.06em]" style={{ color: "var(--ln-ink)" }}>
                  {PIPELINE_PROGRESS_LABEL[segment.key] ?? segment.key}
                </span>
                <span className="block font-data text-[11px]" style={{ color: "var(--ln-ink-soft)" }}>{status}</span>
              </span>
            </li>
          );
        })}
      </ol>
    </div>
  );
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
  const heroElapsedSec = hero?.heartbeat?.current
    ? loopElapsedSeconds(hero.heartbeat.current.started_at, nowMs)
    : null;

  return (
    <section
      data-testid="loops-hero"
      data-state={hero ? "running" : "sleeping"}
      className="rounded-2xl border p-5"
      style={{ borderColor: "var(--ln-line)", background: "var(--ln-surface)" }}
    >
      {hero ? (
        <>
          <div className="flex flex-wrap items-center gap-4">
            <span className="hidden sm:inline-flex">
              <LoopRing
                size={112}
                state="running"
                segments={hero.type === "pipeline" ? deriveRingSegments(hero, nowMs) : undefined}
                ticks={hero.type === "sweep" ? deriveRingTicks(hero) : undefined}
              />
            </span>
            <div className="min-w-0 flex-1">
            <div className="flex flex-wrap items-center gap-2">
              <span className="truncate text-xl font-semibold" style={{ ...displayFont, color: "var(--ln-ink)" }}>
                {hero.name}
              </span>
              {running.length > 1 ? (
                <NightPill tone="sodium">{t.heroMoreRunning(running.length - 1)}</NightPill>
              ) : null}
            </div>
            <p className="mt-1 text-sm" style={{ color: "var(--ln-ink-soft)" }}>
              {hero.heartbeat?.current ? (
                <>
                  {t.heroPhase(
                    hero.heartbeat.current.phase,
                    hero.heartbeat.current.engine,
                    hero.heartbeat.current.model,
                    heroElapsedSec === null ? t.heartbeatInvalid : fmtDur(heroElapsedSec),
                  )}
                </>
              ) : (
                t.heartbeatBetweenPhases
              )}
            </p>
            {hero.heartbeat?.current && heroElapsedSec !== null && heroElapsedSec > 30 ? (
              <p className="mt-1 text-xs" role="status" style={{ color: "var(--ln-warn)" }}>
                {t.heartbeatStale(fmtDur(heroElapsedSec))}
              </p>
            ) : null}
            <div className="mt-3 hidden sm:block">
              {pendingStopPack === hero.name ? (
                <span className="inline-flex flex-wrap items-center gap-2">
                  <span className="text-xs" style={{ color: "var(--ln-ink-soft)" }}>{t.confirmStop}</span>
                  <Button
                    size="sm"
                    disabled={busyPack === hero.name}
                    onClick={() => onStop(hero.name)}
                    className={NIGHT_ACTION_CLASS}
                    style={{ background: "var(--ln-fail)", color: "var(--ln-ink)" }}
                  >
                    {busyPack === hero.name ? "…" : t.confirmYes}
                  </Button>
                  <Button size="sm" ghost onClick={() => onSetPendingStop(null)} className={NIGHT_ACTION_CLASS} style={{ color: "var(--ln-ink-soft)" }}>{t.confirmNo}</Button>
                </span>
              ) : (
                <Button size="sm" ghost onClick={() => onSetPendingStop(hero.name)} className={NIGHT_ACTION_CLASS} style={{ color: "var(--ln-fail)" }}>
                  <Square className="h-3.5 w-3.5" />{t.actions.stop}
                </Button>
              )}
            </div>
            </div>
          </div>
          {hero.type === "pipeline" ? <LoopPipelineProgress pack={hero} nowMs={nowMs} /> : null}
          <div className="mt-3 sm:hidden">
            {pendingStopPack === hero.name ? (
              <div className="grid grid-cols-2 gap-2">
                <button
                  type="button"
                  disabled={busyPack === hero.name}
                  onClick={() => onStop(hero.name)}
                  className={cn("inline-flex min-h-12 items-center justify-center rounded-lg border px-4 text-sm font-semibold", NIGHT_FOCUS, "!font-display")}
                  style={{ borderColor: "var(--ln-fail)", background: "var(--ln-fail)", color: "var(--ln-ink)" }}
                >
                  {busyPack === hero.name ? "…" : t.confirmYes}
                </button>
                <button
                  type="button"
                  onClick={() => onSetPendingStop(null)}
                  className={cn("inline-flex min-h-12 items-center justify-center rounded-lg border px-4 text-sm font-semibold", NIGHT_FOCUS, "!font-display")}
                  style={{ borderColor: "var(--ln-line)", color: "var(--ln-ink-soft)" }}
                >
                  {t.confirmNo}
                </button>
              </div>
            ) : (
              <button
                type="button"
                onClick={() => onSetPendingStop(hero.name)}
                className={cn("inline-flex min-h-12 w-full items-center justify-center gap-2 rounded-lg border px-4 text-sm font-semibold", NIGHT_FOCUS, "!font-display")}
                style={{ borderColor: "var(--ln-line)", color: "var(--ln-fail)" }}
              >
                <Square aria-hidden className="h-4 w-4" />{t.actions.stop}
              </button>
            )}
          </div>
        </>
      ) : (
        <div className="flex flex-wrap items-center gap-4">
          <LoopRing size={112} state="idle" />
          <div className="min-w-0 flex-1">
            <p className="text-xl font-semibold" style={{ ...displayFont, color: "var(--ln-ink)" }}>{t.heroSleeping}</p>
            <p className="mt-1 text-sm" style={{ color: "var(--ln-ink-soft)" }}>
              {(() => {
                const timerCount = packs.filter((p) => p.timer_enabled).length;
                return timerCount > 0 ? t.heroTimerActive(timerCount) : t.heroTimerNone;
              })()}
            </p>
            {(() => {
              const last = newestGlobalEvent(packs);
              if (!last) return null;
              return (
                <p className="mt-0.5 text-sm" style={{ color: "var(--ln-ink-soft)" }}>
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
  onSaveTimerSchedule: (name: string, time: string) => void;
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
  onSaveTimerSchedule,
  onSaveFile,
  onDuplicate,
}: LoopsGridProps) {
  const [repoFilter, setRepoFilter] = useState<string | null>(null);
  if (packs.length === 0) {
    return (
      <div className="rounded-card border border-dashed p-4 text-left" style={{ borderColor: "var(--ln-line)" }}>
        <p className="text-base font-semibold" style={{ ...displayFont, color: "var(--ln-ink)" }}>{t.empty}</p>
        <p className="mt-1 text-sm" style={{ color: "var(--ln-ink-soft)" }}>{t.emptyHint}</p>
      </div>
    );
  }

  const runnable = packs.filter((p): p is LoopPackSummary => !isLoopPackError(p));
  const repoPaths = Array.from(new Set(runnable.map((pack) => pack.repo || t.sourceRepo))).sort();
  const effectiveRepoFilter = repoFilter && repoPaths.includes(repoFilter) ? repoFilter : null;
  const visible = effectiveRepoFilter
    ? packs.filter((pack) => !isLoopPackError(pack) && (pack.repo || t.sourceRepo) === effectiveRepoFilter)
    : packs;
  const repoGroups = Array.from(new Set(
    visible.filter((pack): pack is LoopPackSummary => !isLoopPackError(pack)).map((pack) => pack.repo || t.sourceRepo),
  )).map((repo) => ({
    repo,
    packs: visible.filter((pack): pack is LoopPackSummary => !isLoopPackError(pack) && (pack.repo || t.sourceRepo) === repo),
  }));
  const broken = visible.filter(isLoopPackError);
  const repoName = (repo: string) => repo.replace(/\/+$/, "").split("/").pop() || repo;

  const renderPack = (pack: LoopPackSummary) => (
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
      onSaveTimerSchedule={onSaveTimerSchedule}
      onSaveFile={onSaveFile}
      onDuplicate={onDuplicate}
    />
  );

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
      {repoPaths.length > 1 ? (
        <div role="group" className="flex gap-2 overflow-x-auto pb-1" aria-label={t.repoFilterLabel}>
          {[null, ...repoPaths].map((repo) => {
            const active = effectiveRepoFilter === repo;
            return (
              <button
                key={repo ?? "all"}
                type="button"
                aria-pressed={active}
                onClick={() => setRepoFilter(repo)}
                className={cn("min-h-12 shrink-0 rounded-full border px-4 text-xs", NIGHT_FOCUS)}
                style={{
                  borderColor: active ? "var(--ln-sodium)" : "var(--ln-line)",
                  color: active ? "var(--ln-sodium)" : "var(--ln-ink-soft)",
                  background: active ? "var(--ln-raised)" : "transparent",
                }}
              >
                {repo ? repoName(repo) : t.allRepos}
              </button>
            );
          })}
        </div>
      ) : null}
      {repoGroups.map((group) => (
        <section key={group.repo} className="space-y-2.5" aria-label={`${t.boundRepo}: ${repoName(group.repo)}`}>
          <header className="flex min-w-0 flex-wrap items-center gap-x-2 gap-y-1 px-1">
            <FolderGit2 aria-hidden className="h-4 w-4 shrink-0" style={{ color: "var(--ln-sodium)" }} />
            <h2 className="font-display text-sm font-semibold" style={{ color: "var(--ln-ink)" }}>{repoName(group.repo)}</h2>
            <span className="min-w-0 truncate font-data text-[11px]" title={group.repo} style={{ color: "var(--ln-ink-mute)" }}>{group.repo}</span>
            <span className="font-data text-[11px]" style={{ color: "var(--ln-ink-soft)" }}>
              {t.boundBranch(Array.from(new Set(group.packs.map((pack) => pack.base_branch))).join(", ") || "main")}
            </span>
          </header>
          <div className="grid gap-3 sm:grid-cols-2 xl:grid-cols-3 sm:gap-4">
            {group.packs.map(renderPack)}
          </div>
        </section>
      ))}
      {broken.length > 0 ? <div className="grid gap-3 sm:grid-cols-2 xl:grid-cols-3">{broken.map((pack) => <LoopErrorCard key={pack.name} pack={pack} />)}</div> : null}
    </div>
  );
}

export function LoopsView() {
  const loops = useLoops();
  const models = useLoopModels();
  const nowMs = useLoopNowMs();
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

  const handleSaveTimerSchedule = async (name: string, time: string) => {
    setBusyPack(name);
    clearActionError(name);
    try {
      await setLoopTimerSchedule(name, time);
      await loops.reload();
    } catch (e) {
      setActionErrorByPack((prev) => ({ ...prev, [name]: `${t.timerScheduleFailed}: ${extractDetail(e)}` }));
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
            "radial-gradient(ellipse 900px 420px at 50% -10%, color-mix(in oklab, var(--color-bronze) 7%, transparent), transparent 60%), " +
            "linear-gradient(180deg, var(--ln-header-glow) 0%, transparent 70%)",
        }}
      />
      <header>
        <p className="text-[11px] uppercase tracking-[0.2em]" style={{ color: "var(--ln-ink-soft)" }}>{t.eyebrow}</p>
        <div className="mt-1 flex flex-wrap items-baseline gap-x-3 gap-y-1">
          <span className="text-sm" style={{ color: "var(--ln-ink-soft)" }}>{t.subtitle}</span>
        </div>
      </header>

      {loops.error ? <NightCallout tone="warn">{t.error}</NightCallout> : null}

      <div className="flex items-center gap-2 text-xs" style={{ color: "var(--ln-ink-soft)" }}>{t.packCount(packs.length)}</div>

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
        nowMs={nowMs}
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
        onSaveTimerSchedule={(name, time) => void handleSaveTimerSchedule(name, time)}
        onSaveFile={(pack, filename, content) => void handleSaveFile(pack, filename, content)}
        onDuplicate={(source, name) => void handleDuplicate(source, name)}
      />
    </div>
  );
}
