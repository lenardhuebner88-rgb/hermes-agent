/**
 * Abo-Limits-Cockpit — die geteilte Kachel für /control (Start) und den Stats-Tab.
 *
 * Eine Zeile pro Abo mit den Primärbalken (alle session/weekly-Fenster: 5-Std-
 * Fenster, Diese Woche und ggf. das modell-spezifische Wochenlimit, z. B. Fable),
 * eine Engpass-Zeile (knappstes echtes Fenster über alle Abos) und eine Fußzeile
 * für reine Ausgaben-Provider (OpenRouter = $-Guthaben). Daten =
 * `/api/account-usage` (echte Provider-Fenster, deckt sich
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
  sortUsageProviders,
  sortedUsageWindows,
  staleUsageSignalLabel,
  usageProviderLabel,
  windowLabelDe,
  type SubscriptionLane,
} from "../lib/accountUsage";
import { fmtTokens, nowSec } from "../lib/derive";
import { DEFAULT_STATS_CONFIG, usageRoleForProvider, type StatsFieldConfig } from "../lib/statsFields";
import { SignalChip, SignalLabel, type SignalTone } from "./leitstand";
import { Eyebrow } from "./primitives";
import { RateBar } from "./charts/charts";

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
  const baseLabel = windowLabelDe(window, config);
  // Ein Modellname wie "Fable" qualifiziert das scoped weekly limit und gehört
  // sichtbar ins Balkenlabel. Quota-Details wie "24/100 verbleibend" kommen auf
  // eine eigene volle-Breite-Zeile UNTER dem Balken; inline an der 7rem-Spalte
  // würden sie auf Desktop abschneiden (Operator-Bug 2026-07-21: "24/100 nicht weg").
  const label = window.detail ? `${baseLabel} · ${window.detail}` : baseLabel;
  const detailInLabel = window.window_key === "scoped_week";
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
      <span className="order-1 min-w-0 truncate text-ink-2">{detailInLabel ? label : baseLabel}</span>
      <span className="order-2 whitespace-nowrap tabular-nums text-ink sm:order-3">
        {used == null ? "?" : `${Math.round(used)} %`}
        {reset ? <span className="text-ink-3"> · {reset}</span> : null}
      </span>
      <div className="order-3 col-span-2 sm:order-2 sm:col-span-1">
        <RateBar rate={used == null ? null : used / 100} color={limitColor(used)} />
      </div>
      {window.detail && !detailInLabel ? (
        <span className="order-4 col-span-2 text-ink-3 js-window-detail sm:col-span-3">
          {window.detail}
        </span>
      ) : null}
    </div>
  );
}

/**
 * Eine Abo-Karte: die Primärbalken (alle session/weekly-Fenster — 5-Std-Fenster,
 * Diese Woche und ggf. das modell-spezifische Wochenlimit, z. B. Fable), darunter
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
  // ALLE session/weekly-Fenster werden Primärbalken (Operator-Spec 2026-07-21:
  // drei Balken — 5h-Fenster, Woche UND das modell-spezifische Wochenlimit).
  // `find` würde nur das erste Weekly-Fenster erwischen; weitere Weekly-Fenster
  // fielen weder primär noch ins Details-Collapse → stiller Datenverlust.
  const primary = sortedUsageWindows(provider, config).filter((w) => {
    const kind = classifyWindow(w, config);
    return kind === "session" || kind === "weekly";
  });
  const others = provider.windows.filter((w) => classifyWindow(w, config) === "other");
  const unavailable = !provider.available;
  // Ausgaben-Karte (OpenRouter = Pay-as-you-go, kein Fenster-Limit): keine
  // session/weekly-Balken, aber echte $-Details → als Karten-Körper statt als
  // muter „keine Fensterdaten"-Leerzustand rendern.
  const spendCard = !unavailable && primary.length === 0 && others.length === 0 && provider.details.length > 0;
  const tone: SignalTone = unavailable
    ? "neutral"
    : provider.windows.some((w) => (w.used_percent ?? 0) >= 90)
      ? "alert"
      : provider.windows.some((w) => (w.used_percent ?? 0) >= 75)
        ? "warn"
        : "ok";
  let chipLabel = unavailable ? "offline" : provider.cached ? "Cache" : "Live";
  let chipTone: SignalTone = tone;
  if (!unavailable) {
    const staleLabel = staleUsageSignalLabel(provider, nowMs);
    if (staleLabel) {
      chipLabel = staleLabel;
      chipTone = staleLabel.endsWith("d") ? "warn" : "neutral";
    }
  }
  // Details-Collapse nur für Nebenfenster + Details, die NICHT schon als
  // Ausgaben-Körper gerendert werden (sonst doppelt).
  const hasExtras = others.length > 0 || (provider.details.length > 0 && !spendCard);
  return (
    <article className="rounded-card border border-line bg-surface-2 p-3">
      <div className="grid grid-cols-[minmax(0,1fr)_auto] items-start gap-2">
        <div className="min-w-0">
          <p className="truncate text-sm font-semibold text-ink">{usageProviderLabel(provider, config)}</p>
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
          {primary.map((w, i) => <AccountWindowRow key={`${w.window_key ?? w.label}-${w.detail ?? ""}-${i}`} window={w} nowMs={nowMs} config={config} />)}
        </div>
      ) : spendCard ? (
        // Ausgaben-Karte (OpenRouter): $-Zeilen als Karten-Körper.
        <div className="mt-3 space-y-1 text-xs text-ink-2">
          {provider.details.map((detail) => <p key={detail}>{detail}</p>)}
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
            {others.map((w, i) => (
              <p key={`${w.window_key ?? w.label}-${w.detail ?? ""}-${i}`}>
                {windowLabelDe(w, config)}
                {w.detail ? ` · ${w.detail}` : ""}{" "}
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
 * eine Primärbalken-Karte pro Abo (alle session/weekly-Fenster). Provider ohne Provider-Limit (Kimi = lokale
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
  const providers = sortUsageProviders(usage?.providers ?? [], config);
  const nowMs = nowSec() * 1000;
  const isAbo = (p: AccountUsageProvider) => usageRoleForProvider(config, p.provider) === "subscription";
  const subscriptions = providers.filter(isAbo);
  const availableSubscriptions = subscriptions.filter((p) => p.available).length;
  // Ausgaben-Provider (OpenRouter = Pay-as-you-go): kein Fenster, keine Lane,
  // aber echte $-Details → eigene Karte im Cockpit statt Fußzeile.
  const isSpend = (p: AccountUsageProvider) =>
    p.available && usageRoleForProvider(config, p.provider) === "spend" && p.details.length > 0;
  const bottleneck = pickBottleneck(subscriptions, config);
  // Cockpit: Lane-Abos (auch offline) + Provider mit echtem Fenster (Grok) +
  // Ausgaben-Karten (OpenRouter). Fußzeile: nur noch offline window-lose Nicht-
  // Abos ohne Lane und ohne Details.
  const cockpit = providers.filter((p) => isAbo(p) || isSpend(p));
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
  const bottleneckProviderLabel = bottleneck
    ? usageProviderLabel(subscriptions.find((p) => p.provider === bottleneck.providerId)!, config)
    : "";
  return (
    <section className="space-y-2">
      <div className="flex flex-wrap items-center justify-between gap-2">
        <div className="flex flex-wrap items-center gap-2">
          <Eyebrow>Abo-Limits</Eyebrow>
          <SignalChip
            tone={error ? "warn" : subscriptions.length === 0 ? "neutral" : availableSubscriptions === subscriptions.length ? "ok" : "warn"}
            label={loading ? "lädt…" : error ? "teilweise unbekannt" : subscriptions.length === 0 ? "Limit unbekannt" : `${availableSubscriptions}/${subscriptions.length} Abos live`}
          />
        </div>
        {usage ? <span className="text-xs text-ink-3">TTL {usage.cache_ttl_seconds}s</span> : null}
      </div>
      {bottleneck ? (
        <div role={bnTone === "neutral" ? undefined : "alert"} className={`flex items-start gap-2 rounded-card border px-3 py-2 text-sec ${bnTone === "alert" ? "border-status-alert/30 bg-status-alert/10 text-status-alert" : bnTone === "warn" ? "border-status-warn/30 bg-status-warn/10 text-status-warn" : "border-line bg-surface-2 text-ink-2"}`}>
          {bnTone === "neutral" ? <SignalLabel tone="neutral" label="Höchste Auslastung" className="mt-0.5 shrink-0" /> : <TriangleAlert aria-hidden className="mt-0.5 size-4 shrink-0" />}
          <span>{bnTone === "neutral" ? "" : "Engpass: "}{bottleneckProviderLabel}-{bottleneck.windowLabel} {Math.round(bottleneck.usedPercent)} %{bnReset ? ` — Reset ${bnReset}` : ""}</span>
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
              <span className="text-ink">{usageProviderLabel(p, config)}</span>{" "}
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
