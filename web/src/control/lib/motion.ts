/**
 * Aurora Violet — shared motion language for the /control SPA.
 *
 * One vocabulary so every tab moves the same way: a calm rise, a soft
 * disclosure, a route cross-fade. Durations + easings live here, never
 * inline, so the cockpit reads as one instrument and not nine.
 *
 * Reduced-motion is a first-class consumer, not an afterthought: every
 * component that animates calls `useReducedMotion()` and swaps these
 * variants for `*Reduced` (opacity-only / instant). Keep that contract.
 */
import type { Transition, Variants } from "motion/react";

/** Durations in seconds (Motion's unit). Mirror of tokens.dur (ms /1000). */
export const DUR = { fast: 0.15, med: 0.2, slow: 0.4 } as const;

/** Standard decelerate — UI settling into place. */
export const EASE_OUT = [0.2, 0.8, 0.2, 1] as const;
/** The signature "rise" curve, shared with the CSS .hc-rise keyframe. */
export const EASE_RISE = [0.2, 0.7, 0.2, 1] as const;

/** Route cross-fade: gentle upward settle, keyed by pathname in RouteTransition. */
export const routeVariants: Variants = {
  initial: { opacity: 0, y: 8 },
  animate: { opacity: 1, y: 0, transition: { duration: 0.16, ease: EASE_OUT } },
  exit: { opacity: 0, y: -6, transition: { duration: 0.12, ease: EASE_OUT } },
};
export const routeVariantsReduced: Variants = {
  initial: { opacity: 0 },
  animate: { opacity: 1, transition: { duration: 0.12 } },
  exit: { opacity: 0, transition: { duration: 0.08 } },
};

/** Disclosure: height auto↔0 with opacity, ~180ms. */
export const disclosureVariants: Variants = {
  collapsed: { height: 0, opacity: 0, transition: { duration: 0.18, ease: EASE_OUT } },
  open: { height: "auto", opacity: 1, transition: { duration: 0.18, ease: EASE_OUT } },
};
export const disclosureVariantsReduced: Variants = {
  collapsed: { height: 0, opacity: 0, transition: { duration: 0 } },
  open: { height: "auto", opacity: 1, transition: { duration: 0 } },
};

/** Stagger container — reveals children in sequence. */
export const staggerVariants: Variants = {
  hidden: {},
  show: { transition: { staggerChildren: 0.04, delayChildren: 0.02 } },
};
export const staggerVariantsReduced: Variants = {
  hidden: {},
  show: { transition: { staggerChildren: 0, delayChildren: 0 } },
};

/** Stagger item — the per-row rise. */
export const staggerItemVariants: Variants = {
  hidden: { opacity: 0, y: 7 },
  show: { opacity: 1, y: 0, transition: { duration: DUR.slow, ease: EASE_RISE } },
};
export const staggerItemVariantsReduced: Variants = {
  hidden: { opacity: 0 },
  show: { opacity: 1, transition: { duration: 0.12 } },
};

/** Hover/tap feel for interactive cards. */
export const cardHover = { y: -2 } as const;
export const cardTap = { scale: 0.985 } as const;

/** Chevron rotation between collapsed (0°) and open (90°). */
export const chevronTransition: Transition = { duration: DUR.fast, ease: EASE_OUT };
