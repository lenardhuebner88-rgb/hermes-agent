import { useState } from "react";
import { Check, ClipboardCopy } from "lucide-react";

import { cn } from "@/lib/utils";

export function CopyButton({ text, label, copiedLabel }: { text: string | undefined; label: string; copiedLabel: string }) {
  const [copied, setCopied] = useState(false);
  const copy = async () => {
    if (!text) return;
    try {
      await navigator.clipboard.writeText(text);
      setCopied(true);
      window.setTimeout(() => setCopied(false), 1600);
    } catch {
      /* clipboard blocked */
    }
  };
  return (
    <button
      type="button"
      onClick={copy}
      disabled={!text}
      className={cn(
        "inline-flex min-h-12 items-center justify-center gap-2 rounded-card border px-3 text-sec font-medium transition",
        copied
          ? "border-live/50 bg-live/15 text-bronze-hi"
          : "border-live/40 bg-live/10 text-bronze-hi hover:bg-live/15 disabled:cursor-not-allowed disabled:border-line disabled:bg-transparent disabled:text-ink-3",
      )}
    >
      {copied ? <Check className="h-4 w-4" /> : <ClipboardCopy className="h-4 w-4" />}
      {copied ? copiedLabel : label}
    </button>
  );
}
