import { useState } from "react";
import { Download, Mic2 } from "lucide-react";
import { openAuthedApiFile } from "@/lib/api";
import type { DictateStatusResponse } from "../lib/schemas";
import { fmtRelativeTime, nowSec } from "../lib/derive";
import { KpiTile, SignalChip } from "./leitstand";
import { Eyebrow } from "./primitives";

const LANGUAGE_LABELS: Record<NonNullable<DictateStatusResponse["language"]>, string> = {
  system: "System",
  german: "Deutsch",
  english: "Englisch",
  auto: "Auto",
};

function label(value: string | null): string {
  if (!value) return "—";
  return value.replaceAll("_", " ");
}

/** Operational metadata only: this tile never receives audio or dictated text. */
export function DictateStatusTile({
  status,
  loading,
  error,
}: {
  status: DictateStatusResponse | null;
  loading: boolean;
  error: string | null;
}) {
  const [downloadError, setDownloadError] = useState<string | null>(null);
  const connected = Boolean(status?.connected && status.last_contact_at);
  const permissionOk = status?.microphone_permission === true && status.service_enabled === true;
  const tone = error || status?.last_error ? "alert" : connected ? (permissionOk ? "ok" : "warn") : "neutral";
  const contact = status?.last_contact_at ? fmtRelativeTime(status.last_contact_at, nowSec()) : "kein Bericht";

  return (
    <section aria-label="Hermes Diktat Status" className="ch-panel space-y-3 p-3">
      <div className="flex flex-wrap items-start justify-between gap-2">
        <div className="flex min-w-0 items-center gap-2">
          <Mic2 className="h-4 w-4 shrink-0 text-ink-2" />
          <div>
            <Eyebrow>Hermes Diktat</Eyebrow>
            <p className="mt-0.5 text-xs text-ink-2">Statusmetadaten · kein Audio, kein Transkript</p>
          </div>
        </div>
        <SignalChip
          tone={tone}
          label={loading && !status ? "lädt…" : connected ? "verbunden" : "ohne Kontakt"}
        />
      </div>

      <div className="grid grid-cols-2 gap-2 lg:grid-cols-4">
        <KpiTile label="Version" value={status?.app_version ?? "—"} delta={contact} dot={connected ? "live" : "idle"} />
        <KpiTile label="Engine" value={label(status?.engine ?? null)} delta={status?.surface ? `via ${status.surface}` : "—"} />
        <KpiTile
          label="Sprache / Stil"
          value={status?.language ? LANGUAGE_LABELS[status.language] : "—"}
          delta={label(status?.style ?? null)}
        />
        <KpiTile
          label="Berechtigungen"
          value={permissionOk ? "bereit" : connected ? "prüfen" : "—"}
          delta={`Mikro ${status?.microphone_permission === true ? "ok" : "—"} · Dienst ${status?.service_enabled === true ? "ok" : "—"}`}
          dot={permissionOk ? "live" : connected ? "warn" : "idle"}
        />
      </div>

      <div className="grid grid-cols-2 gap-2 sm:grid-cols-3 lg:grid-cols-6">
        <KpiTile label="Diktate" value={status?.dictations ?? 0} />
        <KpiTile label="Erfolg" value={status?.success_rate_percent ?? "—"} suffix={status?.success_rate_percent != null ? "%" : undefined} />
        <KpiTile label="Latenz p50" value={status?.latency_p50_ms ?? "—"} suffix={status?.latency_p50_ms != null ? "ms" : undefined} />
        <KpiTile label="Latenz p95" value={status?.latency_p95_ms ?? "—"} suffix={status?.latency_p95_ms != null ? "ms" : undefined} />
        <KpiTile label="Retry / BUSY" value={`${status?.retries ?? 0} / ${status?.busy ?? 0}`} />
        <KpiTile label="Fehler" value={status?.failures ?? 0} dot={(status?.failures ?? 0) > 0 ? "warn" : "idle"} delta={status?.latency_ms != null ? `zuletzt ${status.latency_ms} ms` : "keine Latenz"} />
      </div>

      <div className="flex flex-wrap items-center justify-between gap-2 border-t border-line pt-3">
        <p className="min-w-0 text-xs text-ink-2">
          {error ?? (status?.last_error ? `Letzter Fehler: ${label(status.last_error)}` : "Kein letzter Fehler gemeldet.")}
        </p>
        {status?.apk ? (
          <button
            type="button"
            className="ch-btn min-h-12 px-3 text-xs font-medium"
            onClick={() => {
              setDownloadError(null);
              void openAuthedApiFile(status.apk!.url, status.apk!.name).catch((cause: unknown) => {
                setDownloadError(cause instanceof Error ? cause.message : "APK-Download fehlgeschlagen");
              });
            }}
          >
            <Download className="h-4 w-4" /> APK laden
          </button>
        ) : <span className="text-xs text-ink-3">Kein Dictate-APK-Artefakt</span>}
      </div>
      {downloadError ? <p className="text-xs text-status-alert">{downloadError}</p> : null}
    </section>
  );
}
