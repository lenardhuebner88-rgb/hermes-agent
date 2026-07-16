/**
 * Abo-Limits-Cockpit — die geteilte Kachel für /control (Start) und den Stats-Tab.
 *
 * Eine Zeile pro Abo mit genau zwei waagerechten Balken (5-Std-Fenster + Diese
 * Woche), eine Engpass-Zeile (knappstes echtes Fenster über alle Abos) und eine
 * Fußzeile für Provider ohne hartes Limit (Kimi = lokale Schätzung, OpenRouter =
 * $-Guthaben). Daten = `/api/account-usage` (echte OAuth-Fenster, deckt sich
 * zahlengenau mit den Provider-Apps); reine Darstellung/Klassifikation.
 */
import type {
  AccountUsageProvider,
  AccountUsageResponse,
  AccountUsageWindow,
} from "../lib/types";
import { TriangleAlert } from "lucide-react";
import {
  classifyWindow,
  formatReset,
  pickBottleneck,
  providerToLane,
  windowLabelDe,
  type SubscriptionLane,
} from "../lib/accountUsage";
import { fmtTokens, nowSec } from "../lib/derive";
import { DEFAULT_STATS_CONFIG, isProviderVisible, providerLabel as configuredProviderLabel, providerOrder, type StatsFieldConfig } from "../lib/statsFields";
import { SignalChip, SignalLabel, type SignalTone } from "./leitstand";
import { Eyebrow } from "./primitives";
import { RateBar } from "./charts/charts";

function fallbackProviderLabel(provider: string): string {
  if (provider === "openai-codex") return "ChatGPT / Codex";
  if (provider === "anthropic") return "Claude";
  if (provider === "kimi") return "Kimi";
  if (provider === "xai") return "Grok";
  if (provider === "openrouter") return "OpenRouter";
  return provider;
}

function limitColor(value: number | null): string {
  if (value == null) return "var(--color-line)";
  if (value >= 90) return "var(--color-status-alert)";
  if (value >= 75) return "var(--color-status-warn)";
  return "var(--color-status-ok)";
}

/** Eine Fenster-Zeile: deutsches Label · waagerechter Balken · % + Reset-Countdown. */
function AccountWindowRow({ window, nowMs, config }: { window: AccountUsageWindow; nowMs: number; config: StatsFieldConfig }) {
  const used =
    typeof window.used_percent === "number" && Number.isFinite(window.used_percent)
      ? Math.max(0, Math.min(100, window.used_percent))
      : null;
  const reset = formatReset(window.reset_at, nowMs);
  // Accessible-Name + role="meter": der RateBar ist aria-hidden (rein dekorativ),
  // also trägt die Zeile die Gauge-Semantik — Screenreader (und der control-smoke-
  // e2e) lesen "<Fenster>: <N> % genutzt" bzw. "<Fenster>: unbekannt".
  const label = windowLabelDe(window, config);
  const meterName = `${label}: ${used == null ? "unbekannt" : `${Math.round(used)} % genutzt`}`;
  return (
    <div
      className="grid grid-cols-[minmax(0,1fr)_auto] items-center gap-x-2 gap-y-1 text-xs sm:grid-cols-[7rem_minmax(0,1fr)_auto]"
      role="meter"
      aria-label={meterName}
      aria-valuemin={0}
      aria-valuemax={100}
      aria-valuenow={used == null ? undefined : Math.round(used)}
      aria-valuetext={used == null ? "unbekannt" : undefined}
    >
      <span className="order-1 min-w-0 truncate text-ink-2">{label}</span>
      <span className="order-2 whitespace-nowrap tabular-nums text-ink sm:order-3">
        {used == null ? "?" : `${Math.round(used)} %`}
        {reset ? <span className="text-ink-3"> · {reset}</span> : null}
      </span>
      <div className="order-3 col-span-2 sm:order-2 sm:col-span-1">
        <RateBar rate={used == null ? null : used / 100} color={limitColor(used)} />
      </div>
    </div>
  );
}

/**
 * Eine Abo-Karte: zwei waagerechte Balken (5-Std-Fenster + Diese Woche), darunter
 * optional der Worker-Run-Abgleich (nur im Stats-Tab durchgereicht), und ein
 * Details-Collapse für Nebenfenster (Opus/Sonnet) + Extra-Usage. Wird nur für
 * verfügbare Provider mit echtem session/weekly-Fenster gerendert.
 */
function AccountProviderCard({
  provider,
  nowMs,
  align,
  config,
}: {
  provider: AccountUsageProvider;
  nowMs: number;
  align?: { tokens: number; runs: number } | null;
  config: StatsFieldConfig;
}) {
  const session = provider.windows.find((w) => classifyWindow(w, config) === "session");
  const weekly = provider.windows.find((w) => classifyWindow(w, config) === "weekly");
  const primary = [session, weekly].filter((w): w is AccountUsageWindow => Boolean(w));
  const others = provider.windows.filter((w) => classifyWindow(w, config) === "other");
  const unavailable = !provider.available;
  const tone: SignalTone = unavailable
    ? "neutral"
    : provider.windows.some((w) => (w.used_percent ?? 0) >= 90)
      ? "alert"
      : provider.windows.some((w) => (w.used_percent ?? 0) >= 75)
        ? "warn"
        : "ok";
  // Non-xai providers leave signal_at null; their fetched_at is always fresh
  // (≤ HTTP cache TTL), so the 60min guard keeps them on Cache/Live —
  // byte-identical chip behavior for non-xai.
  let chipLabel = unavailable ? "offline" : provider.cached ? "Cache" : "Live";
  let chipTone: SignalTone = tone;
  if (!unavailable) {
    const signalMs = Date.parse(provider.signal_at ?? provider.fetched_at ?? "");
    if (Number.isFinite(signalMs) && nowMs - signalMs > 60 * 60 * 1000) {
      const ageH = Math.floor((nowMs - signalMs) / (60 * 60 * 1000));
      const rel = ageH < 24 ? `${ageH}h` : `${Math.floor(ageH / 24)}d`;
      chipLabel = `Stand ${rel}`;
      chipTone = ageH >= 24 ? "warn" : "neutral";
    }
  }
  const hasExtras = others.length > 0 || provider.details.length > 0;
  return (
    <article className="rounded-card border border-line bg-surface-2 p-3">
      <div className="grid grid-cols-[minmax(0,1fr)_auto] items-start gap-2">
        <div className="min-w-0">
          <p className="truncate text-sm font-semibold text-ink">{configuredProviderLabel(config, provider.provider) || fallbackProviderLabel(provider.provider)}</p>
          {provider.plan ? <p className="text-xs text-ink-3">{provider.plan}</p> : null}
        </div>
        <div className="justify-self-end">
          <SignalChip
            tone={chipTone}
            label={chipLabel}
          />
        </div>
      </div>
      {primary.length ? (
        <div className="mt-3 space-y-1.5">
          {primary.map((w) => <AccountWindowRow key={w.window_key ?? w.label} window={w} nowMs={nowMs} config={config} />)}
        </div>
      ) : (
        // Gleichwertige Abo-Karte auch ohne Provider-Fenster (Kimi = lokale
        // Schätzung) bzw. wenn der Provider gerade offline ist: ehrlicher
        // Leerzustand statt zusammengeworfener Strichel-Fußzeile.
        <p className="mt-3 text-xs text-ink-2">
          {unavailable ? provider.unavailable_reason ?? "nicht verfügbar" : "keine Fensterdaten vom Provider"}
        </p>
      )}
      {align ? (
        <p className="mt-2 text-micro text-ink-3">
          ↳ Abgleich: {fmtTokens(align.tokens)} Tok diese Woche laut Worker-Runs ({align.runs} Runs)
        </p>
      ) : null}
      {hasExtras ? (
        <details className="mt-2 text-xs">
          <summary className="cursor-pointer text-ink-3 hover:text-ink">Details</summary>
          <div className="mt-1 space-y-1 text-ink-2">
            {others.map((w) => (
              <p key={w.window_key ?? w.label}>
                {windowLabelDe(w, config)}{" "}
                {typeof w.used_percent === "number" ? `${Math.round(w.used_percent)} %` : "—"}
              </p>
            ))}
            {provider.details.map((detail) => <p key={detail}>{detail}</p>)}
          </div>
        </details>
      ) : null}
    </article>
  );
}

/**
 * Abo-Limits-Cockpit: Engpass-Zeile (knappstes Fenster über alle echten Abos) +
 * eine Zwei-Balken-Karte pro Abo. Provider ohne Provider-Limit (Kimi = lokale
 * Schätzung, OpenRouter = $-Guthaben) landen in der „Ohne Fenster-Limit"-Fußzeile,
 * nie als gleichwertige Limit-Karte (§8). `laneUsage` ist optional — nur der
 * Stats-Tab reicht den Worker-Run-Abgleich durch.
 */
export function AccountUsageTile({
  usage,
  loading,
  error,
  laneUsage,
  config = DEFAULT_STATS_CONFIG,
}: {
  usage: AccountUsageResponse | null;
  loading: boolean;
  error: string | null;
  laneUsage?: Partial<Record<SubscriptionLane, { tokens: number; runs: number }>>;
  config?: StatsFieldConfig;
}) {
  const providers = (usage?.providers ?? [])
    .filter((p) => isProviderVisible(config, p.provider))
    .sort((a, b) => providerOrder(config, a.provider) - providerOrder(config, b.provider));
  const available = providers.filter((p) => p.available).length;
  const nowMs = nowSec() * 1000;
  // Abo-Karte = Provider mit Subscription-Lane ODER echtem Provider-Fenster
  // (Grok: Fenster ohne Lane — xai hat lane:null by design, kein Worker-Run-
  // Reconciliation). Lane-Abos (Claude/Codex/Kimi) behalten die Karte auch offline
  // (ehrlicher Leerzustand). Fußzeile = nur fensterlose Nicht-Abos (OpenRouter-
  // $-Guthaben, offline Provider ohne Lane und ohne Fenster). Tradeoff: unavailable
  // xai ohne Fenster fällt bewusst in die Fußzeile (im Gegensatz zu Lane-Abos).
  const isAbo = (p: AccountUsageProvider) =>
    providerToLane(p.provider, config) != null || p.windows.length > 0;
  // Kimi = lokale Schätzung, kein hartes Provider-Limit → zählt nie als Engpass (§8).
  const bottleneck = pickBottleneck(
    providers.filter((p) => providerToLane(p.provider, config) !== "kimi"),
  );
  // Cockpit: Lane-Abos (auch offline) + Provider mit echtem Fenster (Grok).
  // Fußzeile: fensterlose Nicht-Abos (OpenRouter, offline window-less xai, …).
  const cockpit = providers.filter((p) => isAbo(p));
  const footer = providers.filter((p) => !cockpit.includes(p));
  // Engpass-Ton: rot ab 90 %, gelb ab 75 %, sonst neutral (kein ⚠) — §3.
  const bnTone: SignalTone =
    bottleneck == null
      ? "neutral"
      : bottleneck.usedPercent >= 90
        ? "alert"
        : bottleneck.usedPercent >= 75
          ? "warn"
          : "neutral";
  const bnReset = bottleneck ? formatReset(bottleneck.resetAt, nowMs) : "";
  return (
    <section className="space-y-2">
      <div className="flex flex-wrap items-center justify-between gap-2">
        <div className="flex flex-wrap items-center gap-2">
          <Eyebrow>Abo-Limits</Eyebrow>
          <SignalChip
            tone={error ? "warn" : providers.length === 0 ? "neutral" : available === providers.length ? "ok" : "warn"}
            label={loading ? "lädt…" : error ? "teilweise unbekannt" : providers.length === 0 ? "Limit unbekannt" : `${available}/${providers.length} live`}
          />
        </div>
        {usage ? <span className="text-xs text-ink-3">TTL {usage.cache_ttl_seconds}s</span> : null}
      </div>
      {bottleneck ? (
        <div role={bnTone === "neutral" ? undefined : "alert"} className={`flex items-start gap-2 rounded-card border px-3 py-2 text-sec ${bnTone === "alert" ? "border-status-alert/30 bg-status-alert/10 text-status-alert" : bnTone === "warn" ? "border-status-warn/30 bg-status-warn/10 text-status-warn" : "border-line bg-surface-2 text-ink-2"}`}>
          {bnTone === "neutral" ? <SignalLabel tone="neutral" label="Höchste Auslastung" className="mt-0.5 shrink-0" /> : <TriangleAlert aria-hidden className="mt-0.5 size-4 shrink-0" />}
          <span>{bnTone === "neutral" ? "" : "Engpass: "}{configuredProviderLabel(config, bottleneck.providerId) || fallbackProviderLabel(bottleneck.providerId)}-{bottleneck.windowLabel} {Math.round(bottleneck.usedPercent)} %{bnReset ? ` — Reset ${bnReset}` : ""}</span>
        </div>
      ) : null}
      {loading ? (
        <div className="hc-skeleton h-28 w-full rounded-xl" />
      ) : cockpit.length ? (
        <div className="grid gap-3 lg:grid-cols-2">
          {cockpit.map((p) => {
            const lane = providerToLane(p.provider, config);
            const align = lane && laneUsage ? laneUsage[lane] ?? null : null;
            return <AccountProviderCard key={p.provider} provider={p} nowMs={nowMs} align={align} config={config} />;
          })}
        </div>
      ) : providers.length === 0 ? (
        <div className="rounded-panel border border-dashed border-line px-4 py-4 text-sm text-ink-2">Limit unbekannt — noch keine Abo-Daten geladen.</div>
      ) : null}
      {footer.length ? (
        <div className="rounded-card border border-dashed border-line px-3 py-2 text-xs text-ink-2">
          <span className="text-ink-3">Ohne Fenster-Limit: </span>
          {footer.map((p, i) => (
            <span key={p.provider}>
              {i > 0 ? " · " : ""}
              <span className="text-ink">{configuredProviderLabel(config, p.provider) || fallbackProviderLabel(p.provider)}</span>{" "}
              {p.available
                ? p.details.length
                  ? p.details.join(", ")
                  : "keine Limit-Daten"
                : p.unavailable_reason ?? "nicht verfügbar"}
            </span>
          ))}
        </div>
      ) : null}
      {error ? <SignalLabel tone="warn" label={error} /> : null}
    </section>
  );
}
