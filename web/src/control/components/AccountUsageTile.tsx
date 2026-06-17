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
import { StatusPill, ToneCallout } from "./atoms";
import { Eyebrow } from "./primitives";
import { RateBar } from "./charts/charts";

function providerLabel(provider: string): string {
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
function AccountWindowRow({ window, nowMs }: { window: AccountUsageWindow; nowMs: number }) {
  const used =
    typeof window.used_percent === "number" && Number.isFinite(window.used_percent)
      ? Math.max(0, Math.min(100, window.used_percent))
      : null;
  const reset = formatReset(window.reset_at, nowMs);
  return (
    <div className="grid grid-cols-[7rem_1fr_auto] items-center gap-2 text-xs">
      <span className="truncate hc-soft">{windowLabelDe(window)}</span>
      <RateBar rate={used == null ? null : used / 100} color={TONE_HEX[limitTone(used)]} />
      <span className="whitespace-nowrap tabular-nums text-white">
        {used == null ? "?" : `${Math.round(used)} %`}
        {reset ? <span className="hc-dim"> · {reset}</span> : null}
      </span>
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
}: {
  provider: AccountUsageProvider;
  nowMs: number;
  align?: { tokens: number; runs: number } | null;
}) {
  const session = provider.windows.find((w) => classifyWindow(w) === "session");
  const weekly = provider.windows.find((w) => classifyWindow(w) === "weekly");
  const primary = [session, weekly].filter((w): w is AccountUsageWindow => Boolean(w));
  const others = provider.windows.filter((w) => classifyWindow(w) === "other");
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
          <p className="truncate text-sm font-semibold text-white">{providerLabel(provider.provider)}</p>
          {provider.plan ? <p className="text-xs hc-dim">{provider.plan}</p> : null}
        </div>
        <StatusPill tone={tone} label={provider.cached ? "Cache" : "Live"} dot="live" />
      </div>
      <div className="mt-3 space-y-1.5">
        {primary.map((w) => <AccountWindowRow key={w.window_key ?? w.label} window={w} nowMs={nowMs} />)}
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
                {windowLabelDe(w)}{" "}
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
}: {
  usage: AccountUsageResponse | null;
  loading: boolean;
  error: string | null;
  laneUsage?: Partial<Record<SubscriptionLane, { tokens: number; runs: number }>>;
}) {
  const providers = usage?.providers ?? [];
  const available = providers.filter((p) => p.available).length;
  const nowMs = nowSec() * 1000;
  // Kimi = lokale Schätzung aus kanban.db, kein Provider-Limit → nie als Engpass
  // oder volle Karte (§8). Bleibt unten in der Fußzeile sichtbar.
  const realProviders = providers.filter((p) => p.provider !== "kimi");
  const bottleneck = pickBottleneck(realProviders);
  const cockpit = realProviders.filter(
    (p) => p.available && p.windows.some((w) => classifyWindow(w) !== "other"),
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
          {bnTone === "zinc" ? "Höchste Auslastung" : "⚠ Engpass"}: {providerLabel(bottleneck.providerId)}-
          {bottleneck.windowLabel} {Math.round(bottleneck.usedPercent)} %
          {bnReset ? ` — Reset ${bnReset}` : ""}
        </ToneCallout>
      ) : null}
      {loading ? (
        <div className="hc-skeleton h-28 w-full rounded-xl" />
      ) : cockpit.length ? (
        <div className="grid gap-3 lg:grid-cols-2">
          {cockpit.map((p) => {
            const lane = providerToLane(p.provider);
            const align = lane && laneUsage ? laneUsage[lane] ?? null : null;
            return <AccountProviderCard key={p.provider} provider={p} nowMs={nowMs} align={align} />;
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
              <span className="text-white">{providerLabel(p.provider)}</span>{" "}
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
