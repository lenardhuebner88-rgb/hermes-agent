import { useMemo, useState } from "react";
import { ChevronDown, ChevronRight } from "lucide-react";
import { Badge } from "@nous-research/ui/ui/components/badge";
import { Button } from "@nous-research/ui/ui/components/button";
import { cn } from "@/lib/utils";
import { diffStats, withLineNumbers } from "../lib/diff";
import type { DiffLine, ToneName } from "../lib/types";
import { toneClasses } from "../lib/tones";
import type { DotKind } from "../lib/tones";

export function Led({ kind, size = 8 }: { kind: DotKind; size?: number }) {
  return <span aria-hidden className={cn("hc-led inline-block shrink-0 rounded-full", `hc-led-${kind}`)} style={{ width: size, height: size }} />;
}

export function StatusPill({ tone, label, dot, size = "sm" }: { tone: ToneName; label: string; dot?: DotKind; size?: "sm" | "md" }) {
  return (
    <span className={cn("inline-flex items-center gap-2 rounded-full border font-medium", toneClasses(tone), size === "sm" ? "px-2.5 py-1 text-xs" : "px-3 py-1.5 text-sm")}>
      {dot ? <Led kind={dot} /> : null}
      {label}
    </span>
  );
}

export function ToneCallout({ tone, children }: { tone: ToneName; children: React.ReactNode }) {
  return <div className={cn("rounded-lg border px-3 py-2 text-sm", toneClasses(tone))}>{children}</div>;
}

export function ModeBadge({ mode }: { mode: "skill" | "code" }) {
  return mode === "code" ? <Badge tone="warning">Code-Änderung</Badge> : <Badge className="border-[var(--hc-accent-border)] bg-[var(--hc-accent-wash)] text-[var(--hc-accent-text)]">Skill</Badge>;
}

export function MeterBar({ label, value, max, tone = "cyan" }: { label: string; value: number; max: number; tone?: "cyan" | "amber" | "red" }) {
  const pct = Math.max(0, Math.min(100, max > 0 ? (value / max) * 100 : 0));
  const color = tone === "red" ? "bg-red-400" : tone === "amber" ? "bg-amber-400" : "bg-cyan-300";
  return (
    <div className="space-y-1">
      <div className="flex items-center justify-between text-xs hc-soft"><span>{label}</span><span className="hc-mono">{Math.round(pct)}%</span></div>
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
        <div><p className="text-sm font-medium text-white">Vorher / Nachher</p><p className="text-xs hc-soft">+{stats.added} / -{stats.removed}</p></div>
        {collapsible ? (
          <Button ghost size="icon" className="md:hidden" aria-label={collapsed ? "Diff ausklappen" : "Diff einklappen"} onClick={() => setCollapsed((v) => !v)}>
            {collapsed ? <ChevronRight className="h-4 w-4" /> : <ChevronDown className="h-4 w-4" />}
          </Button>
        ) : null}
      </div>
      <pre className={cn("max-h-72 max-w-full overflow-auto p-0 text-xs leading-5 hc-mono", collapsed && "hidden md:block")}>
        {numbered.map((line, idx) => (
          <div key={`${idx}-${line.text}`} className={cn("grid min-w-0 grid-cols-[auto_minmax(0,1fr)] gap-3 px-3", line.type === "add" && "bg-emerald-500/10 text-emerald-100", line.type === "del" && "bg-red-500/10 text-red-100", line.type === "ctx" && "text-zinc-300")}>
            <span className="select-none text-right text-zinc-600">{showLineNumbers ? (line.ln ?? "-") : line.type === "add" ? "+" : line.type === "del" ? "-" : " "}</span>
            <code className="whitespace-pre-wrap break-words">{line.text || " "}</code>
          </div>
        ))}
      </pre>
    </div>
  );
}
