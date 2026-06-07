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
        "inline-flex min-h-10 items-center justify-center gap-2 rounded-md border px-3 text-sm font-medium transition",
        copied
          ? "border-emerald-500/40 bg-emerald-500/10 text-emerald-200"
          : "border-cyan-500/30 bg-cyan-500/10 text-cyan-200 hover:bg-cyan-500/15 disabled:cursor-not-allowed disabled:border-white/10 disabled:text-zinc-500",
      )}
    >
      {copied ? <Check className="h-4 w-4" /> : <ClipboardCopy className="h-4 w-4" />}
      {copied ? copiedLabel : label}
    </button>
  );
}
