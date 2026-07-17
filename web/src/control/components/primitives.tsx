/**
 * Aurora Violet — primitive set for the /control SPA.
 *
 * The keystone every tab inherits: one type voice, one card, one panel
 * header, one stat, one skeleton, one disclosure, one route/list motion.
 * Everything here is additive, dark-only, scoped under [data-control], and
 * built on the Sheet-A tokens + the shared motion language (lib/motion.ts).
 *
 * Contracts kept throughout:
 *  · type-only imports for types (verbatimModuleSyntax),
 *  · every animated component calls useReducedMotion() and collapses to
 *    opacity-only / instant when the user prefers reduced motion,
 *  · no ad-hoc font sizes — text renders a named scale step via .hc-type-*.
 */
import { useEffect, useId, useState } from "react";
import type { ReactNode } from "react";
import { AnimatePresence, m, useReducedMotion } from "motion/react";
import { ChevronRight } from "lucide-react";
import { cn } from "@/lib/utils";
import type { SignalTone } from "./leitstand";
import {
  cardHover,
  cardTap,
  chevronTransition,
  disclosureVariants,
  disclosureVariantsReduced,
  routeVariants,
  routeVariantsReduced,
  staggerItemVariants,
  staggerItemVariantsReduced,
  staggerVariants,
  staggerVariantsReduced,
} from "../lib/motion";

/* ── Text ──────────────────────────────────────────────────────────────────
   Renders a named step of the type scale. One voice, no inline font-size. */
type TypeStep = "display" | "title" | "subtitle" | "body" | "label" | "eyebrow";
const TYPE_CLASS: Record<TypeStep, string> = {
  display: "hc-type-display",
  title: "hc-type-title",
  subtitle: "hc-type-subtitle",
  body: "hc-type-body",
  label: "text-micro font-medium",
  eyebrow: "hc-eyebrow",
};

export type TextProps = {
  as?: keyof React.JSX.IntrinsicElements;
  variant?: TypeStep;
  className?: string;
  children: ReactNode;
};

export function Text({ as = "p", variant = "body", className, children }: TextProps) {
  const Tag = as as React.ElementType;
  return <Tag className={cn(TYPE_CLASS[variant], className)}>{children}</Tag>;
}

/** The eyebrow kicker — Archivo expanded caps, tracked, dim. Sits above
 *  titles. Per DESIGN.md's type-scale + "mono = data only" rule: eyebrows are
 *  the display voice, never mono-wallpaper (theme.css `--font-display` /
 *  `--text-micro`). */
export function Eyebrow({ className, children }: { className?: string; children: ReactNode }) {
  return <p className={cn("font-display uppercase tracking-[0.08em] text-micro font-semibold text-ink-3", className)}>{children}</p>;
}

/* ── Card ──────────────────────────────────────────────────────────────────
   The composable surface. `surface` picks the depth tier;
   `interactive` adds the lift/press feel (reduced-motion-safe). */
type SurfaceTier = "panel" | "panel2" | "card" | "raised";
const SURFACE_CLASS: Record<SurfaceTier, string> = {
  panel: "hc-surface-panel",
  panel2: "hc-surface-panel2",
  card: "hc-surface-card",
  raised: "hc-surface-raised",
};

export type CardProps = {
  surface?: SurfaceTier;
  interactive?: boolean;
  className?: string;
  onClick?: () => void;
  /** A11y pass-through so an interactive Card can act as a real button:
   *  role="button" + tabIndex + onKeyDown keep keyboard activation intact. */
  role?: string;
  tabIndex?: number;
  onKeyDown?: (event: React.KeyboardEvent) => void;
  ariaLabel?: string;
  children: ReactNode;
};

export function Card({ surface = "card", interactive, className, onClick, role, tabIndex, onKeyDown, ariaLabel, children }: CardProps) {
  const reduce = useReducedMotion();
  const classes = cn(
    SURFACE_CLASS[surface],
    interactive && "cursor-pointer",
    className,
  );
  if (!interactive) {
    return (
      <div className={classes} onClick={onClick} role={role} tabIndex={tabIndex} onKeyDown={onKeyDown} aria-label={ariaLabel}>
        {children}
      </div>
    );
  }
  return (
    <m.div
      className={classes}
      onClick={onClick}
      role={role}
      tabIndex={tabIndex}
      onKeyDown={onKeyDown}
      aria-label={ariaLabel}
      whileHover={reduce ? undefined : cardHover}
      whileTap={reduce ? undefined : cardTap}
      transition={chevronTransition}
    >
      {children}
    </m.div>
  );
}

/* ── Panel / Section ───────────────────────────────────────────────────────
   A titled region: eyebrow + title + optional actions + divider, then body.
   Section is the lighter sibling (no surface chrome, smaller title). */
type HeaderProps = {
  eyebrow?: ReactNode;
  title: ReactNode;
  actions?: ReactNode;
  titleVariant: TypeStep;
};

function RegionHeader({ eyebrow, title, actions, titleVariant }: HeaderProps) {
  return (
    // Stack title over actions on phones — at ≥sm it is the original
    // row (shrink-0 actions would otherwise squeeze the title into a
    // word-per-line column on a 390 px screen).
    <div className="flex flex-col gap-3 sm:flex-row sm:items-start sm:justify-between">
      <div className="min-w-0">
        {eyebrow ? <Eyebrow>{eyebrow}</Eyebrow> : null}
        <Text as="h2" variant={titleVariant} className="text-ink">
          {title}
        </Text>
      </div>
      {actions ? <div className="flex shrink-0 flex-wrap items-center gap-2">{actions}</div> : null}
    </div>
  );
}

export type PanelProps = {
  eyebrow?: ReactNode;
  title: ReactNode;
  actions?: ReactNode;
  surface?: SurfaceTier;
  className?: string;
  children: ReactNode;
};

export function Panel({ eyebrow, title, actions, surface = "card", className, children }: PanelProps) {
  return (
    <section className={cn(SURFACE_CLASS[surface], "p-4", className)}>
      <RegionHeader eyebrow={eyebrow} title={title} actions={actions} titleVariant="subtitle" />
      <div className="mt-3 border-t border-line pt-3">{children}</div>
    </section>
  );
}

export type SectionProps = {
  eyebrow?: ReactNode;
  title: ReactNode;
  actions?: ReactNode;
  className?: string;
  children: ReactNode;
};

export function Section({ eyebrow, title, actions, className, children }: SectionProps) {
  return (
    <section className={cn("space-y-3", className)}>
      <RegionHeader eyebrow={eyebrow} title={title} actions={actions} titleVariant="label" />
      <div>{children}</div>
    </section>
  );
}

/* ── Stat ──────────────────────────────────────────────────────────────────
   Promotes the inline Metric pattern (label + value). `accent` renders an
   oversized hero number in aurora gradient text. A new primitive — the two
   existing `Metric` components stay where they are until per-tab migration. */
export type StatProps = {
  label: ReactNode;
  value: ReactNode;
  hint?: ReactNode;
  accent?: boolean;
  tone?: SignalTone;
  className?: string;
};

export function Stat({ label, value, hint, accent, tone, className }: StatProps) {
  if (accent) {
    return (
      <div className={cn("space-y-1", className)}>
        <Eyebrow>{label}</Eyebrow>
        <div className="hc-aurora-text hc-type-display font-data tabular-nums">{value}</div>
        {hint ? <Text variant="label" className="text-ink-2">{hint}</Text> : null}
      </div>
    );
  }
  const toneClass = tone === "ok"
    ? "border-status-ok/30 bg-status-ok/10"
    : tone === "warn"
      ? "border-status-warn/30 bg-status-warn/10"
      : tone === "alert"
        ? "border-status-alert/30 bg-status-alert/10"
        : "border-line bg-surface-2";
  return (
    <div className={cn("rounded-card border px-3 py-2", toneClass, className)}>
      <Eyebrow>{label}</Eyebrow>
      <p className="font-data tabular-nums truncate text-sec font-semibold text-ink">{value}</p>
      {hint ? <p className="mt-0.5 text-sec text-ink-2">{hint}</p> : null}
    </div>
  );
}

/* ── Skeleton ──────────────────────────────────────────────────────────────
   Pure-CSS shimmer (gated on prefers-reduced-motion: no-preference in the
   stylesheet). Works under renderToStaticMarkup — no JS needed. */
export function Skeleton({ className }: { className?: string }) {
  return <div aria-hidden className={cn("hc-skeleton", className)} />;
}

export function SkeletonRow({ className }: { className?: string }) {
  return <Skeleton className={cn("h-4 w-full", className)} />;
}

export function SkeletonCard({ rows = 3, className }: { rows?: number; className?: string }) {
  return (
    <div className={cn("hc-surface-card space-y-3 p-4", className)} aria-hidden aria-busy="true">
      <Skeleton className="h-5 w-2/5" />
      <div className="space-y-2">
        {Array.from({ length: rows }).map((_, i) => (
          <SkeletonRow key={i} className={i === rows - 1 ? "w-3/5" : undefined} />
        ))}
      </div>
    </div>
  );
}

/* ── Disclosure ────────────────────────────────────────────────────────────
   Animated height auto↔0 + opacity + chevron rotate. Replaces native
   <details>, so it mirrors <details open=…> semantics precisely:
   · open + onToggle  → fully controlled (parent owns the state).
   · open alone       → sync-hint: seeds + re-syncs internal state on change,
                        but the user can still toggle in between (like native).
   · neither          → uncontrolled from defaultOpen.
   aria-expanded + aria-controls keep it accessible. */
export type DisclosureProps = {
  summary: ReactNode;
  children: ReactNode;
  defaultOpen?: boolean;
  open?: boolean;
  onToggle?: (open: boolean) => void;
  id?: string;
  className?: string;
};

export function Disclosure({ summary, children, defaultOpen = false, open, onToggle, id, className }: DisclosureProps) {
  const reduce = useReducedMotion();
  const reactId = useId();
  const panelId = id ? `${id}-panel` : `${reactId}-panel`;
  // Controlled only when a setter (onToggle) is also provided — React's
  // value+onChange convention. With `open` alone we mirror native <details
  // open=…>: it seeds and re-syncs the internal state, but stays toggleable.
  const isControlled = open !== undefined && onToggle !== undefined;
  const [internalOpen, setInternalOpen] = useState(open ?? defaultOpen);
  useEffect(() => {
    // Deliberate prop→state sync mirroring native <details open=…>: when the
    // uncontrolled hint flips, re-seed internal state. Safe (guarded, runs only
    // on the hint change), so opt out of the cascading-render heuristic.
    // eslint-disable-next-line react-hooks/set-state-in-effect
    if (!isControlled && open !== undefined) setInternalOpen(open);
  }, [open, isControlled]);
  const isOpen = isControlled ? (open as boolean) : internalOpen;

  const toggle = () => {
    const next = !isOpen;
    if (!isControlled) setInternalOpen(next);
    onToggle?.(next);
  };

  const variants = reduce ? disclosureVariantsReduced : disclosureVariants;

  return (
    <div id={id} className={cn("min-w-0", className)}>
      <button
        type="button"
        onClick={toggle}
        aria-expanded={isOpen}
        aria-controls={panelId}
        className="flex w-full items-center gap-2 text-left"
      >
        <m.span
          aria-hidden
          className="shrink-0 text-ink-3"
          animate={{ rotate: isOpen ? 90 : 0 }}
          transition={reduce ? { duration: 0 } : chevronTransition}
        >
          <ChevronRight className="h-4 w-4" />
        </m.span>
        <span className="min-w-0 flex-1">{summary}</span>
      </button>
      <AnimatePresence initial={false}>
        {isOpen ? (
          <m.div
            key="panel"
            id={panelId}
            className="overflow-hidden"
            initial="collapsed"
            animate="open"
            exit="collapsed"
            variants={variants}
          >
            <div className="pt-2">{children}</div>
          </m.div>
        ) : null}
      </AnimatePresence>
    </div>
  );
}

/* ── RouteTransition ───────────────────────────────────────────────────────
   Wraps <Routes>. AnimatePresence keyed by pathname → opacity + y:8→0.
   Reduced motion collapses to a quick fade. */
export function RouteTransition({ pathname, children }: { pathname: string; children: ReactNode }) {
  const reduce = useReducedMotion();
  const variants = reduce ? routeVariantsReduced : routeVariants;
  return (
    <AnimatePresence mode="wait" initial={false}>
      <m.div key={pathname} variants={variants} initial="initial" animate="animate" exit="exit">
        {children}
      </m.div>
    </AnimatePresence>
  );
}

/* ── Stagger / StaggerItem ─────────────────────────────────────────────────
   Optional list reveal. Wrap a list in <Stagger>, each row in <StaggerItem>. */
export function Stagger({ className, children }: { className?: string; children: ReactNode }) {
  const reduce = useReducedMotion();
  return (
    <m.div
      className={className}
      variants={reduce ? staggerVariantsReduced : staggerVariants}
      initial="hidden"
      animate="show"
    >
      {children}
    </m.div>
  );
}

export function StaggerItem({ className, children }: { className?: string; children: ReactNode }) {
  const reduce = useReducedMotion();
  return (
    <m.div className={className} variants={reduce ? staggerItemVariantsReduced : staggerItemVariants}>
      {children}
    </m.div>
  );
}
