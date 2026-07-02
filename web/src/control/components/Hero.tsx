/**
 * Hero — the one Pflicht-Primitive at the top of every /control tab.
 *
 * Generalises the f5 decision-spine hero first built inline in InboxView: an
 * eyebrow kicker, an optional oversized aurora number, a Klartext-statement
 * title, supporting copy, and an optional status pill + primary action on the
 * right. The `.hc-hero` shell (control-tokens.css) carries the tone-driven
 * gradient + aurora top-edge; `tone` sets the mood — emerald = ruhig,
 * amber/red = Aufmerksamkeit, cyan = Info, violet = Marke/neutral.
 *
 * The headline number stays aurora-gradient regardless of tone — the one brand
 * flourish — while the shell + status dot carry the status colour.
 *
 * Static by design (no entrance motion): a hero that fades in can read as empty
 * on a paused/background tab or in a screenshot. The eye should land here first,
 * always painted.
 */
import type { ReactNode } from "react";
import { cn } from "@/lib/utils";
import type { ToneName } from "../lib/types";
import type { DotKind } from "../lib/tones";
import { heroAccent } from "../lib/tones";
import type { Density } from "../hooks/useDensity";
import { Eyebrow, Text } from "./primitives";
import { StatusPill } from "./atoms";

export interface HeroStatus {
  label: string;
  tone: ToneName;
  dot?: DotKind;
}

export interface HeroProps {
  eyebrow: ReactNode;
  title: ReactNode;
  subtitle?: ReactNode;
  /** Optional oversized headline number (rendered aurora-gradient, tabular). */
  count?: ReactNode;
  /** Line under the number — usually restates the count in words. */
  countHint?: ReactNode;
  /** Mood: drives the shell gradient + the default status dot colour. */
  tone?: ToneName;
  status?: HeroStatus;
  /** Primary action, rendered top-right next to the status pill. */
  action?: ReactNode;
  density?: Density;
  /** Mobile-only extra compaction (shell padding, head gap, number size) for
   *  tabs whose header ran too tall/loud on phones (Flow-Tab, Operator-
   *  Feedback 2026-07-02: "Schriften im Header zu groß"). At ≥sm this renders
   *  byte-identical to the default — opt-in per caller so every other Hero's
   *  mobile look stays exactly as today. */
  tight?: boolean;
  className?: string;
  /** Optional extra row under the hero head (KPI pods, filter chips …). */
  children?: ReactNode;
}

export function Hero({
  eyebrow,
  title,
  subtitle,
  count,
  countHint,
  tone = "violet",
  status,
  action,
  density,
  tight = false,
  className,
  children,
}: HeroProps) {
  const compact = density === "compact";
  const hasNumber = count != null || countHint != null;
  return (
    <section
      className={cn("hc-hero", tight ? "p-2.5 sm:p-6" : compact ? "p-4 sm:p-5" : "p-5 sm:p-6", className)}
      style={{ "--hc-hero-accent": heroAccent(tone) } as React.CSSProperties}
    >
      <div className={cn("flex flex-col sm:flex-row sm:items-start sm:justify-between", tight ? "gap-2 sm:gap-4" : "gap-4")}>
        {/* Clamp the statement column so the display number + copy never overflow
            a 390 px phone (display type is large; the title ellipsises). */}
        <div className="min-w-0 sm:max-w-[34rem]">
          <Eyebrow>{eyebrow}</Eyebrow>
          {count != null ? (
            // The signature: an oversized aurora number in the editorial serif.
            // `tight` swaps in a smaller mobile-only step of the same voice
            // (.hc-type-display-tight mirrors .hc-type-display from sm up).
            <div className={cn("hc-aurora-text mt-1.5 tabular-nums", tight ? "hc-type-display-tight" : "hc-type-display")}>{count}</div>
          ) : null}
          {countHint != null ? (
            <Text variant="label" className="mt-1 hc-soft">{countHint}</Text>
          ) : null}
          <Text
            as="h1"
            variant="title"
            className={cn("hc-hero-statement line-clamp-2 text-[var(--hc-text)]", hasNumber ? "mt-2" : "mt-1")}
          >
            {title}
          </Text>
          {subtitle != null ? (
            <Text variant="body" className="mt-1.5 line-clamp-2 hc-soft" >{subtitle}</Text>
          ) : null}
        </div>
        {status || action ? (
          <div className="flex shrink-0 flex-wrap items-center gap-2 sm:flex-col sm:items-end">
            {status ? <StatusPill tone={status.tone} label={status.label} dot={status.dot} size="md" /> : null}
            {action}
          </div>
        ) : null}
      </div>
      {children != null ? <div className={tight ? "mt-2 sm:mt-4" : compact ? "mt-3" : "mt-4"}>{children}</div> : null}
    </section>
  );
}
