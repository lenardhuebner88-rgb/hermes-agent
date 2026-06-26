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
  ToneName,
} from "../lib/types";
import { TONE_HEX } from "../lib/tones";
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
import { StatusPill, ToneCallout } from "./atoms";
import { Eyebrow } from "./primitives";
import { RateBar } from "./charts/charts";

function fallbackProviderLabel(provider: string): string {
  if (provider === "openai-codex") return "ChatGPT / Codex";
  if (provider === "anthropic") return "Claude";
  if (provider === "kimi") return "Kimi";
  if (provider === "openrouter") return "OpenRouter";
  return provider;
}

function limitTone(value: number | null): ToneName {
  if (value == null) return "zinc";
  if (value >= 90) return "red";
  if (value >= 75) return "amber";
  return "emerald";
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
      <span className="order-1 min-w-0 truncate hc-soft">{label}</span>
      <span className="order-2 whitespace-nowrap tabular-nums text-white sm:order-3">
        {used == null ? "?" : `${Math.round(used)} %`}
        {reset ? <span className="hc-dim"> · {reset}</span> : null}
      </span>
      <div className="order-3 col-span-2 sm:order-2 sm:col-span-1">
        <RateBar rate={used == null ? null : used / 100} color={TONE_HEX[limitTone(used)]} />
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
  const tone: ToneName = provider.windows.some((w) => (w.used_percent ?? 0) >= 90)
    ? "red"
    : provider.windows.some((w) => (w.used_percent ?? 0) >= 75)
      ? "amber"
      : "emerald";
  const hasExtras = others.length > 0 || provider.details.length > 0;
  return (
    <article className="rounded-xl border border-[var(--hc-border)] bg-black/20 p-3">
      <div className="flex flex-wrap items-center justify-between gap-2">
        <div className="min-w-0">
          <p className="truncate text-sm font-semibold text-white">{configuredProviderLabel(config, provider.provider) || fallbackProviderLabel(provider.provider)}</p>
          {provider.plan ? <p className="text-xs hc-dim">{provider.plan}</p> : null}
        </div>
        <StatusPill tone={tone} label={provider.cached ? "Cache" : "Live"} dot="live" />
      </div>
      <div className="mt-3 space-y-1.5">
        {primary.map((w) => <AccountWindowRow key={w.window_key ?? w.label} window={w} nowMs={nowMs} config={config} />)}
      </div>
      {align ? (
        <p className="mt-2 text-[0.7rem] hc-dim">
          ↳ Abgleich: {fmtTokens(align.tokens)} Tok diese Woche laut Worker-Runs ({align.runs} Runs)
        </p>
      ) : null}
      {hasExtras ? (
        <details className="mt-2 text-xs">
          <summary className="cursor-pointer hc-dim hover:text-white">Details</summary>
          <div className="mt-1 space-y-1 hc-soft">
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
  // Kimi = lokale Schätzung aus kanban.db, kein Provider-Limit → nie als Engpass
  // oder volle Karte (§8). Bleibt unten in der Fußzeile sichtbar.
  const realProviders = providers.filter((p) => providerToLane(p.provider, config) !== "kimi");
  const bottleneck = pickBottleneck(realProviders);
  const cockpit = realProviders.filter(
    (p) => p.available && p.windows.some((w) => classifyWindow(w, config) !== "other"),
  );
  const footer = providers.filter((p) => !cockpit.includes(p));
  // Engpass-Ton: rot ab 90 %, gelb ab 75 %, sonst neutral (kein ⚠) — §3.
  const bnTone: ToneName =
    bottleneck == null
      ? "zinc"
      : bottleneck.usedPercent >= 90
        ? "red"
        : bottleneck.usedPercent >= 75
          ? "amber"
          : "zinc";
  const bnReset = bottleneck ? formatReset(bottleneck.resetAt, nowMs) : "";
  return (
    <section className="space-y-2">
      <div className="flex flex-wrap items-center justify-between gap-2">
        <div className="flex flex-wrap items-center gap-2">
          <Eyebrow>Abo-Limits</Eyebrow>
          <StatusPill
            tone={error ? "amber" : providers.length === 0 ? "zinc" : available === providers.length ? "emerald" : "amber"}
            label={loading ? "lädt…" : error ? "teilweise unbekannt" : providers.length === 0 ? "Limit unbekannt" : `${available}/${providers.length} live`}
            dot={error ? "warn" : providers.length === 0 ? "idle" : "live"}
          />
        </div>
        {usage ? <span className="text-xs hc-dim">TTL {usage.cache_ttl_seconds}s</span> : null}
      </div>
      {bottleneck ? (
        <ToneCallout tone={bnTone}>
          {bnTone === "zinc" ? "Höchste Auslastung" : "⚠ Engpass"}: {configuredProviderLabel(config, bottleneck.providerId) || fallbackProviderLabel(bottleneck.providerId)}-
          {bottleneck.windowLabel} {Math.round(bottleneck.usedPercent)} %
          {bnReset ? ` — Reset ${bnReset}` : ""}
        </ToneCallout>
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
        <div className="rounded-xl border border-dashed border-[var(--hc-border-strong)] px-4 py-4 text-sm hc-soft">Limit unbekannt — noch keine Abo-Daten geladen.</div>
      ) : null}
      {footer.length ? (
        <div className="rounded-lg border border-dashed border-[var(--hc-border)] px-3 py-2 text-xs hc-soft">
          <span className="hc-dim">Ohne Fenster-Limit: </span>
          {footer.map((p, i) => (
            <span key={p.provider}>
              {i > 0 ? " · " : ""}
              <span className="text-white">{configuredProviderLabel(config, p.provider) || fallbackProviderLabel(p.provider)}</span>{" "}
              {p.available
                ? p.details.length
                  ? p.details.join(", ")
                  : "keine Limit-Daten"
                : p.unavailable_reason ?? "nicht verfügbar"}
            </span>
          ))}
        </div>
      ) : null}
      {error ? <p className="text-xs text-amber-300/80">{error}</p> : null}
    </section>
  );
}
