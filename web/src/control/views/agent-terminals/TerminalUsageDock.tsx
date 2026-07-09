import { RefreshCw, X } from "lucide-react";

import type { AccountUsageProvider } from "../../lib/types";
import { useAccountUsage } from "../../hooks/useControlData";

import { providerUsageSummary, sortTerminalUsageProviders, TERMINAL_USAGE_PROVIDER_ORDER } from "./usageModel";

const PROVIDER_TITLES: Record<(typeof TERMINAL_USAGE_PROVIDER_ORDER)[number], string> = {
  "openai-codex": "ChatGPT",
  anthropic: "Claude",
  kimi: "Kimi",
};

function UsageMeter({ label, percent }: { label: string; percent: number | null }) {
  return (
    <div className="space-y-1">
      <div className="flex items-center justify-between text-[10px] uppercase tracking-[0.12em] text-ink-dim">
        <span>{label}</span>
        <span className="font-mono text-ink">{percent == null ? "—" : `${Math.round(percent)}%`}</span>
      </div>
      <div className="h-1.5 overflow-hidden rounded-full bg-white/[0.06]">
        {percent != null ? (
          <div
            className={`h-full rounded-full transition-[width] ${percent >= 90 ? "bg-red-400" : percent >= 75 ? "bg-amber-300" : "bg-cyan-300"}`}
            style={{ width: `${percent}%` }}
          />
        ) : null}
      </div>
    </div>
  );
}

function ProviderCard({ provider }: { provider: AccountUsageProvider }) {
  const summary = providerUsageSummary(provider);
  const knownId = TERMINAL_USAGE_PROVIDER_ORDER.includes(provider.provider as (typeof TERMINAL_USAGE_PROVIDER_ORDER)[number]);
  const title = knownId ? PROVIDER_TITLES[provider.provider as (typeof TERMINAL_USAGE_PROVIDER_ORDER)[number]] : provider.title;
  return (
    <article
      data-provider={provider.provider}
      className="rounded-[16px] border border-white/[0.08] bg-[linear-gradient(145deg,rgba(13,22,32,.96),rgba(7,12,19,.92))] p-3 shadow-[inset_0_1px_0_rgba(255,255,255,.035),0_14px_30px_rgba(0,0,0,.18)]"
    >
      <div className="mb-3 flex items-start justify-between gap-2">
        <div className="min-w-0">
          <h3 className="text-sm font-semibold text-ink">{title}</h3>
          <p className="truncate text-[11px] text-ink-dim">{provider.plan || "Plan nicht gemeldet"}</p>
        </div>
        <span
          className={`mt-1 size-2 shrink-0 rounded-full ${provider.available ? "bg-emerald-300 shadow-[0_0_10px_rgba(110,231,183,.55)]" : "bg-white/20"}`}
          aria-label={provider.available ? "verfügbar" : "nicht verfügbar"}
        />
      </div>
      <div className="space-y-2.5">
        <UsageMeter label="5h" percent={summary.sessionPercent} />
        <UsageMeter label="Woche" percent={summary.weeklyPercent} />
      </div>
      {!provider.available ? (
        <p className="mt-2 text-[10px] text-ink-dim">Aktuell keine verlässlichen Limitdaten.</p>
      ) : null}
    </article>
  );
}

export function TerminalUsageDock({ open, onClose }: { open: boolean; onClose: () => void }) {
  const usage = useAccountUsage();
  const providers = sortTerminalUsageProviders(usage.data?.providers ?? []);
  return (
    <aside
      data-testid="terminal-usage-dock"
      aria-hidden={!open}
      className={`absolute inset-y-0 right-0 z-30 flex w-[min(310px,calc(100%-1rem))] flex-col border-l border-white/[0.08] bg-surface-0/95 shadow-[-24px_0_55px_rgba(0,0,0,.38)] backdrop-blur-xl transition-transform duration-200 xl:relative xl:z-0 xl:w-[300px] xl:shrink-0 xl:shadow-none ${open ? "translate-x-0" : "translate-x-full xl:hidden"}`}
    >
      <div className="flex items-center justify-between border-b border-white/[0.07] px-4 py-3">
        <div>
          <p className="text-[10px] uppercase tracking-[0.18em] text-cyan-200/70">Usage</p>
          <h2 className="text-sm font-semibold text-ink">Abo-Limits</h2>
        </div>
        <div className="flex items-center gap-1">
          <span className="rounded-lg p-2 text-ink-dim" aria-label={usage.loading ? "Usage wird aktualisiert" : "Usage wird jede Minute aktualisiert"}>
            <RefreshCw className={`size-3.5 ${usage.loading ? "animate-spin" : ""}`} />
          </span>
          <button type="button" onClick={onClose} className="rounded-lg p-2 text-ink-dim hover:bg-white/[0.06] hover:text-ink" aria-label="Usage schließen">
            <X className="size-3.5" />
          </button>
        </div>
      </div>
      <div className="min-h-0 flex-1 space-y-3 overflow-y-auto p-3">
        {usage.error ? <p className="rounded-xl border border-red-400/20 bg-red-400/[0.06] p-3 text-xs text-red-200">Usage konnte nicht geladen werden.</p> : null}
        {usage.loading && providers.length === 0 ? (
          <div className="space-y-3" aria-label="Usage wird geladen">
            {[0, 1, 2].map((index) => <div key={index} className="h-28 animate-pulse rounded-[16px] border border-white/[0.06] bg-white/[0.025]" />)}
          </div>
        ) : null}
        {!usage.loading && !usage.error && providers.length === 0 ? (
          <p className="rounded-xl border border-white/[0.07] bg-white/[0.025] p-3 text-xs text-ink-dim">Keine Abo-Daten verfügbar.</p>
        ) : null}
        {providers.map((provider) => <ProviderCard key={provider.provider} provider={provider} />)}
      </div>
    </aside>
  );
}
