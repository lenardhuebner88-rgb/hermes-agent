import { RefreshCw, X } from "lucide-react";

import type { AccountUsageProvider } from "../../lib/types";
import { useAccountUsage } from "../../hooks/useControlData";
import { Eyebrow } from "../../components/primitives";

import { providerUsageSummary, sortTerminalUsageProviders, TERMINAL_USAGE_PROVIDER_ORDER } from "./usageModel";

const PROVIDER_TITLES: Record<(typeof TERMINAL_USAGE_PROVIDER_ORDER)[number], string> = {
  "openai-codex": "ChatGPT",
  anthropic: "Claude",
  kimi: "Kimi",
};

function UsageMeter({ label, percent }: { label: string; percent: number | null }) {
  return (
    <div className="space-y-1">
      <div className="flex items-center justify-between text-[10px] uppercase tracking-[0.12em] text-ink-3">
        <span>{label}</span>
        <span className="font-mono text-ink">{percent == null ? "—" : `${Math.round(percent)}%`}</span>
      </div>
      <div className="h-1.5 overflow-hidden rounded-full bg-surface-2">
        {percent != null ? (
          <div
            className={`h-full rounded-full transition-[width] ${percent >= 90 ? "bg-status-alert" : percent >= 75 ? "bg-status-warn" : "bg-status-ok"}`}
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
      className="rounded-card border border-line bg-surface-1 p-3"
    >
      <div className="mb-3 flex items-start justify-between gap-2">
        <div className="min-w-0">
          <h3 className="text-sm font-semibold text-ink">{title}</h3>
          <p className="truncate text-[11px] text-ink-3">{provider.plan || "Plan nicht gemeldet"}</p>
        </div>
        <span className="mt-1 inline-flex shrink-0 items-center gap-1 text-[10px] text-ink-2">
          <span aria-hidden className={`hc-led size-2 rounded-full ${provider.available ? "hc-led-live" : "hc-led-idle"}`} />
          {provider.available ? "aktiv" : "aus"}
        </span>
      </div>
      <div className="space-y-2.5">
        <UsageMeter label="5h" percent={summary.sessionPercent} />
        <UsageMeter label="Woche" percent={summary.weeklyPercent} />
      </div>
      {!provider.available ? (
        <p className="mt-2 text-[10px] text-ink-2">Aktuell keine verlässlichen Limitdaten.</p>
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
      className={`absolute inset-y-0 right-0 z-30 flex w-[min(310px,calc(100%-1rem))] flex-col overflow-hidden rounded-panel border border-line bg-surface-1/95 shadow-[-24px_0_55px_rgba(0,0,0,.38)] backdrop-blur-xl transition-transform duration-200 xl:relative xl:z-0 xl:w-[300px] xl:shrink-0 xl:shadow-none ${open ? "translate-x-0" : "translate-x-full xl:hidden"}`}
    >
      <div className="flex items-center justify-between border-b border-line-soft px-4 py-3">
        <div>
          <Eyebrow>Usage</Eyebrow>
          <h2 className="text-sm font-semibold text-ink">Abo-Limits</h2>
        </div>
        <div className="flex items-center gap-1">
          <span className="rounded-card p-2 text-ink-3" aria-label={usage.loading ? "Usage wird aktualisiert" : "Usage wird jede Minute aktualisiert"}>
            <RefreshCw className={`size-3.5 ${usage.loading ? "animate-spin" : ""}`} />
          </span>
          <button type="button" onClick={onClose} className="grid h-12 w-12 place-items-center rounded-card border border-line bg-surface-2 text-ink-3 hover:border-live/40 hover:bg-surface-3 hover:text-ink" aria-label="Usage schließen">
            <X className="size-3.5" />
          </button>
        </div>
      </div>
      <div className="min-h-0 flex-1 space-y-3 overflow-y-auto p-3">
        {usage.error ? <p className="rounded-card border border-status-alert/20 bg-status-alert/10 p-3 text-xs text-status-alert">Usage konnte nicht geladen werden.</p> : null}
        {usage.loading && providers.length === 0 ? (
          <div className="space-y-3" aria-label="Usage wird geladen">
            {[0, 1, 2].map((index) => <div key={index} className="hc-skeleton h-28 rounded-card" />)}
          </div>
        ) : null}
        {!usage.loading && !usage.error && providers.length === 0 ? (
          <p className="rounded-card border border-line p-3 text-xs text-ink-2">Keine Abo-Daten verfügbar.</p>
        ) : null}
        {providers.map((provider) => <ProviderCard key={provider.provider} provider={provider} />)}
      </div>
    </aside>
  );
}
