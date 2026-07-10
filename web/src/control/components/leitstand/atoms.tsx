/**
 * Leitstand atoms — the small building blocks that reproduce the Claude-design
 * "command" surfaces (KPI pod, titled panel with right-meta, quiet dashed
 * empty state, coloured run-role chip) on top of the Sheet-A tokens.
 * Layout is Tailwind; the few bespoke looks live in control-tokens.css
 * (.hc-fleet-*, .hc-role-*).
 *
 * Canonical home (S1): this file. `components/fleet/atoms.tsx` re-exports these
 * for existing fleet imports — one definition, one source of truth.
 */
import type { ReactNode } from "react";
import { cn } from "@/lib/utils";
import { Led } from "../atoms";
import { Eyebrow } from "../primitives";
import { TONE_HEX } from "../../lib/tones";
import type { DotKind } from "../../lib/tones";
import type { RoleChip as RoleChipMeta } from "../../lib/fleet";

/** A flat KPI pod: eyebrow label (with optional status dot) + a big mono value
 *  + an optional small suffix (e.g. "/ 6"). Mirrors the screenshot's pod row. */
export function FleetPod({
  label,
  value,
  suffix,
  dot,
}: {
  label: ReactNode;
  value: ReactNode;
  suffix?: ReactNode;
  dot?: DotKind;
}) {
  return (
    <div className="hc-fleet-pod">
      <div className="flex items-center gap-2">
        {dot ? <Led kind={dot} size={7} /> : null}
        <Eyebrow>{label}</Eyebrow>
      </div>
      <div className="hc-fleet-pod-value mt-2">
        {value}
        {suffix != null ? <small> {suffix}</small> : null}
      </div>
    </div>
  );
}

/** A titled panel: eyebrow on the left, optional meta text on the right, then a
 *  divider and the body. The Fleet's primary content container. */
export function FleetPanel({
  eyebrow,
  meta,
  children,
  className,
}: {
  eyebrow: ReactNode;
  meta?: ReactNode;
  children: ReactNode;
  className?: string;
}) {
  return (
    <section className={cn("hc-surface-card p-4", className)}>
      <div className="flex items-baseline justify-between gap-3">
        <Eyebrow>{eyebrow}</Eyebrow>
        {meta != null ? <span className="truncate text-right text-micro text-ink-3">{meta}</span> : null}
      </div>
      <div className="mt-3">{children}</div>
    </section>
  );
}

/** Quiet dashed empty state — calm title + one explaining line. `ok` greens the
 *  title (a deliberately reassuring "all clear", not an error). */
export function FleetEmptyState({ title, desc, ok }: { title: ReactNode; desc: ReactNode; ok?: boolean }) {
  return (
    <div className={cn("hc-fleet-empty", ok && "ok")}>
      <span className="hc-fleet-empty-title">{title}</span>
      <span className="hc-fleet-empty-desc">{desc}</span>
    </div>
  );
}

/** The coloured run-role chip: a square badge with the role initial + the role
 *  label, tinted by the role's tone (Verifier azure, Coder gold, Researcher
 *  emerald …). */
export function RoleChip({ role }: { role: RoleChipMeta }) {
  const color = TONE_HEX[role.tone];
  return (
    <span className="hc-role-chip" style={{ "--hc-role": color } as React.CSSProperties}>
      <span className="hc-role-badge">{role.short}</span>
      {role.label}
    </span>
  );
}
