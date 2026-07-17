import {
  lazy,
  Suspense,
  forwardRef,
  type ComponentPropsWithoutRef,
  type ComponentType,
  type RefAttributes,
} from "react";

import { cn } from "@/lib/utils";

export type BadgeTone =
  | "default"
  | "destructive"
  | "outline"
  | "secondary"
  | "success"
  | "warning";

export interface BadgeProps extends Omit<ComponentPropsWithoutRef<"span">, "color"> {
  tone?: BadgeTone;
}

const BASE_CLASS =
  "inline-flex items-center font-compressed text-display px-2 py-1 leading-none tracking-[0.2em]";

const TONE_CLASSES: Record<Exclude<BadgeTone, "default">, string> = {
  destructive: "border border-destructive/30 bg-destructive/15 text-destructive",
  outline: "border border-midground/30 bg-transparent text-midground/80",
  secondary: "border border-midground/15 bg-midground/8 text-midground",
  success: "border border-success/30 bg-success/15 text-success",
  warning: "border border-warning/30 bg-warning/15 text-warning",
};

type LazyDefaultProps = BadgeProps & RefAttributes<HTMLSpanElement>;

// Only the Lens-aware default tone needs Nous UI's BlendMode implementation.
// Keep that exact variant available without putting leva/gsap in the initial graph.
const LazyDefaultBadge = lazy(async () => {
  const module = await import("@nous-research/ui/ui/components/badge");
  return { default: module.Badge as ComponentType<LazyDefaultProps> };
});

function DefaultBadgeFallback({ className, style, ...props }: LazyDefaultProps) {
  return (
    <span
      className={cn(BASE_CLASS, "bg-midground/8 text-midground", className)}
      style={{ opacity: "var(--midground-alpha)", ...style }}
      {...props}
    />
  );
}

export const Badge = forwardRef<HTMLSpanElement, BadgeProps>(function Badge(
  { className, style, tone = "default", ...props },
  ref,
) {
  if (tone === "default") {
    return (
      <Suspense
        fallback={
          <DefaultBadgeFallback
            {...props}
            className={className}
            ref={ref}
            style={style}
          />
        }
      >
        <LazyDefaultBadge {...props} className={className} ref={ref} style={style} />
      </Suspense>
    );
  }

  return (
    <span
      className={cn(BASE_CLASS, TONE_CLASSES[tone], className)}
      ref={ref}
      style={style}
      {...props}
    />
  );
});
