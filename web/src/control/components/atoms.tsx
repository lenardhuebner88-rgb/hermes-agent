import { useMemo, useState } from "react";
import { AlertTriangle, ChevronDown, ChevronRight, Clock3 } from "lucide-react";
import { Badge } from "@nous-research/ui/ui/components/badge";
import { Button } from "@nous-research/ui/ui/components/button";
import { cn } from "@/lib/utils";
import { diffStats, withLineNumbers } from "../lib/diff";
import { fmtAge, nowSec } from "../lib/derive";
import type { DiffLine } from "../lib/types";
import type { DotKind } from "../lib/tones";
import type { StructuredError } from "../hooks/pollingStore";
import { de } from "../i18n/de";

export function Led({ kind, size = 8 }: { kind: DotKind; size?: number }) {
  return <span aria-hidden className={cn("hc-led inline-block shrink-0 rounded-full", `hc-led-${kind}`)} style={{ width: size, height: size }} />;
}

export function StaleBadge({ isStale, lastUpdated, errorObj, error, now = nowSec(), className }: {
  isStale?: boolean;
  lastUpdated?: number | null;
  errorObj?: StructuredError | null;
  error?: string | null;
  now?: number;
  className?: string;
}) {
  const hasError = Boolean(errorObj || error);
  if (!isStale && !hasError) return null;
  const ageLabel = lastUpdated != null ? fmtAge(lastUpdated, now) : null;
  const label = isStale ? (ageLabel ? de.staleBadge.stale(ageLabel) : de.staleBadge.staleUnknown) : de.staleBadge.error;
  const title = errorObj?.message ?? error ?? label;
  const Icon = hasError ? AlertTriangle : Clock3;
  return (
    <span
      title={title}
      className={cn(
        "inline-flex items-center gap-1.5 rounded-full border px-2.5 py-1 text-micro font-medium",
        hasError ? "border-status-warn/30 bg-status-warn/10 text-status-warn" : "border-line bg-surface-2 text-ink-2",
        className,
      )}
    >
      <Icon className="h-3.5 w-3.5" />
      {label}
    </span>
  );
}

export function ModeBadge({ mode }: { mode: "skill" | "code" }) {
  return mode === "code" ? <Badge tone="warning">Code-Änderung</Badge> : <Badge className="border-[var(--hc-accent-border)] bg-[var(--hc-accent-wash)] text-[var(--hc-accent-text)]">Skill</Badge>;
}

export function MeterBar({ label, value, max, tone = "cyan" }: { label: string; value: number; max: number; tone?: "cyan" | "amber" | "red" }) {
  const pct = Math.max(0, Math.min(100, max > 0 ? (value / max) * 100 : 0));
  const color = tone === "red" ? "bg-status-alert" : tone === "amber" ? "bg-status-warn" : "bg-live";
  return (
    <div className="space-y-1">
      <div className="flex items-center justify-between text-micro hc-soft"><span>{label}</span><span className="hc-mono">{Math.round(pct)}%</span></div>
      <div className="h-1.5 overflow-hidden rounded-full bg-white/10"><div className={cn("h-full rounded-full", color)} style={{ width: `${pct}%` }} /></div>
    </div>
  );
}

export function DiffView({ lines, showLineNumbers, collapsible = true, defaultCollapsed = false }: { lines: DiffLine[]; showLineNumbers?: boolean; collapsible?: boolean; defaultCollapsed?: boolean }) {
  const [collapsed, setCollapsed] = useState(defaultCollapsed);
  const numbered = useMemo(() => withLineNumbers(lines), [lines]);
  const stats = diffStats(lines);
  return (
    <div className="min-w-0 max-w-full overflow-hidden rounded-lg border border-[var(--hc-border)] bg-black/25">
      <div className="flex min-h-11 items-center justify-between gap-3 border-b border-[var(--hc-border)] px-3 py-2">
        <div><p className="text-sec font-medium text-white">Vorher / Nachher</p><p className="text-micro hc-soft">+{stats.added} / -{stats.removed}</p></div>
        {collapsible ? (
          <Button ghost size="icon" className="md:hidden" aria-label={collapsed ? "Diff ausklappen" : "Diff einklappen"} onClick={() => setCollapsed((v) => !v)}>
            {collapsed ? <ChevronRight className="h-4 w-4" /> : <ChevronDown className="h-4 w-4" />}
          </Button>
        ) : null}
      </div>
      <pre className={cn("max-h-72 max-w-full overflow-auto p-0 text-micro leading-5 hc-mono", collapsed && "hidden md:block")}>
        {numbered.map((line, idx) => (
          <div key={`${idx}-${line.text}`} className={cn("grid min-w-0 grid-cols-[auto_minmax(0,1fr)] gap-3 px-3", line.type === "add" && "bg-status-ok/10 text-ink", line.type === "del" && "bg-status-alert/10 text-ink", line.type === "ctx" && "text-ink-2")}>
            <span className="select-none text-right text-ink-3">{showLineNumbers ? (line.ln ?? "-") : line.type === "add" ? "+" : line.type === "del" ? "-" : " "}</span>
            <code className="whitespace-pre-wrap break-words">{line.text || " "}</code>
          </div>
        ))}
      </pre>
    </div>
  );
}
