import { useEffect } from "react";
import { X } from "lucide-react";
import { cn } from "@/lib/utils";
import type { ToneName } from "../lib/types";
import { toneClasses } from "../lib/tones";
import { tokens } from "../lib/tokens";
import { StatusPill, ToneCallout } from "./atoms";

interface BacklogDetailDrawerProps {
  title: string;
  id: string;
  chips?: Array<{ label: string; tone?: string }>;
  fields?: Array<{ label: string; value: string }>;
  body: string;
  loading?: boolean;
  error?: string;
  onClose: () => void;
}

const supportedTones: ToneName[] = ["emerald", "cyan", "sky", "indigo", "amber", "rose", "red", "zinc", "violet"];

export function BacklogDetailDrawer({
  title,
  id,
  chips,
  fields,
  body,
  loading = false,
  error,
  onClose,
}: BacklogDetailDrawerProps) {
  useEffect(() => {
    const onKeyDown = (event: KeyboardEvent) => {
      if (event.key === "Escape") onClose();
    };

    window.addEventListener("keydown", onKeyDown);
    return () => window.removeEventListener("keydown", onKeyDown);
  }, [onClose]);

  const visibleFields = fields?.filter((field) => field.value.trim() !== "") ?? [];

  return (
    <div className="fixed inset-0 z-50">
      <button
        type="button"
        className="absolute inset-0 bg-black/60"
        aria-label="Close detail drawer"
        onClick={onClose}
      />
      <aside
        role="dialog"
        aria-modal="true"
        aria-labelledby="backlog-detail-title"
        className={cn(
          "hc-card absolute right-0 top-0 flex h-full w-full max-w-md flex-col rounded-none border-y-0 border-l shadow-2xl",
          "border-[var(--hc-border)]",
        )}
      >
        <header className="flex items-start justify-between gap-3 border-b border-[var(--hc-border)] p-5 pb-4">
          <div className="min-w-0">
            <h2 id="backlog-detail-title" className="truncate text-lg font-semibold text-white">
              {title}
            </h2>
            <p className="mt-1 truncate text-xs hc-mono hc-dim">{id}</p>
          </div>
          <button
            type="button"
            className="grid h-9 w-9 shrink-0 place-items-center rounded-lg border border-white/10 bg-white/[.03] text-zinc-200 hover:bg-white/[.07]"
            aria-label="Close detail drawer"
            onClick={onClose}
          >
            <X className="h-4 w-4" />
          </button>
        </header>

        <div className="min-h-0 flex-1 space-y-5 overflow-y-auto p-5">
          {chips?.length ? (
            <div className="flex flex-wrap gap-2">
              {chips.map((chip, index) => (
                <StatusPill
                  key={`${index}-${chip.label}`}
                  tone={normalizeTone(chip.tone)}
                  label={chip.label}
                  size="sm"
                />
              ))}
            </div>
          ) : null}

          {visibleFields.length ? (
            <dl className="space-y-3">
              {visibleFields.map((field, index) => (
                <div
                  key={`${index}-${field.label}`}
                  className="rounded-lg border border-white/10 bg-white/[.03] px-3 py-2"
                  style={{ borderRadius: tokens.radius.lg }}
                >
                  <dt className="text-xs font-semibold uppercase tracking-wide hc-dim">{field.label}</dt>
                  <dd className="mt-1 whitespace-pre-wrap break-words text-sm text-white">{field.value}</dd>
                </div>
              ))}
            </dl>
          ) : null}

          {error ? (
            <ToneCallout tone="red">
              <p className="whitespace-pre-wrap break-words hc-mono">{error}</p>
            </ToneCallout>
          ) : loading ? (
            <div className={cn("rounded-lg border px-3 py-2 text-sm", toneClasses("zinc"))}>
              <p className="whitespace-pre-wrap break-words hc-mono">Loading details...</p>
            </div>
          ) : (
            <div className="rounded-lg border border-white/10 bg-white/[.03] px-3 py-2 text-sm text-zinc-200">
              <p className="whitespace-pre-wrap break-words hc-mono">{body}</p>
            </div>
          )}
        </div>
      </aside>
    </div>
  );
}

function normalizeTone(tone?: string): ToneName {
  if (tone && supportedTones.includes(tone as ToneName)) return tone as ToneName;
  return "zinc";
}
