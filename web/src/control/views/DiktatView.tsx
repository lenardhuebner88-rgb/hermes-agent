import { useEffect, useState } from "react";
import { Download, ShieldCheck, Smartphone, TriangleAlert } from "lucide-react";
import { z } from "zod";
import { authedFetch, fetchJSON, openAuthedApiFile } from "@/lib/api";
import { DictateStatusTile } from "../components/DictateStatusTile";
import { FleetEmptyState, FleetPanel } from "../components/leitstand";
import { Eyebrow } from "../components/primitives";
import { useDictateStatus } from "../hooks/useControlData";
import { fmtRelativeTime, nowSec } from "../lib/derive";
import type { DictateStatusResponse } from "../lib/schemas";

// Artefakt-Listing (/api/artifacts) — bewusst lokal statt in lib/schemas.ts:
// die Seite ist der einzige Konsument der Versionshistorie.
const ArtifactsResponseSchema = z.object({
  artifacts: z.array(
    z.object({ name: z.string(), size: z.number(), mtime: z.number() }),
  ),
});

export interface DictateArtifact {
  name: string;
  size: number;
  mtime: number;
}

/** Dictate-APKs aus dem Artefakt-Listing, neueste zuerst. */
export function dictateApks(artifacts: DictateArtifact[]): DictateArtifact[] {
  return artifacts
    .filter(
      (artifact) =>
        artifact.name.toLowerCase().startsWith("hermes-dictate") &&
        artifact.name.toLowerCase().endsWith(".apk"),
    )
    .sort((a, b) => b.mtime - a.mtime);
}

export function fmtMegabytes(size: number): string {
  return `${(size / (1024 * 1024)).toFixed(1)} MB`;
}

/** Versionsnummer aus einem versionierten APK-Namen (`hermes-dictate-1.3-<sha>.apk`). */
export function apkVersion(name: string): string | null {
  return /^hermes-dictate-(\d+(?:\.\d+)+)-/.exec(name)?.[1] ?? null;
}

/**
 * Ältere Builds ohne Alias-Duplikate des neuesten APKs — `hermes-dictate-latest.apk`
 * wird als Kopie des versionierten Builds mit ausgeliefert (gleiche Größe, gleicher
 * Zeitstempel ±5 s) und wäre in der Historie nur Rauschen.
 */
export function olderBuilds(artifacts: DictateArtifact[]): DictateArtifact[] {
  const latest = artifacts[0];
  if (!latest) return [];
  return artifacts
    .slice(1)
    .filter((a) => !(a.size === latest.size && Math.abs(a.mtime - latest.mtime) <= 5))
    .slice(0, 5);
}

/**
 * „Update verfügbar“-Hinweis, wenn die verbundene App älter meldet als das neueste
 * versionierte APK. Die Versionsnummer kommt aus dem NEUESTEN Namen mit parsebarer
 * Version — das mtime-neueste Artefakt ist meist der versionslose `-latest`-Alias.
 */
export function updateHint(
  status: DictateStatusResponse | null,
  artifacts: DictateArtifact[] | null,
): string | null {
  const latest = artifacts?.map((a) => apkVersion(a.name)).find((v) => v !== null) ?? null;
  const reported = status?.connected ? status.app_version : null;
  if (!latest || !reported) return null;
  if (reported === latest || reported.startsWith(`${latest}-`)) return null;
  return `Update verfügbar: App meldet ${reported}, aktuell ist ${latest}`;
}

/** Fehlerklassen des Status-Reports → verständliche Ursache + nächster Schritt. */
export const DICTATE_ERROR_HELP: Record<string, { title: string; help: string }> = {
  no_speech: { title: "Keine Sprache erkannt", help: "Näher ans Mikrofon, kurz warten, erneut tippen." },
  language_unavailable: { title: "Sprachpaket fehlt", help: "Android-Einstellungen → Spracheingabe → Offline-Sprachpaket (Deutsch/Englisch) laden." },
  recognizer_unavailable: { title: "On-Device-Erkennung nicht verfügbar", help: "Gerät unterstützt keine Offline-Erkennung oder das Sprachpaket fehlt — Cloud-Modus nutzen oder Paket laden." },
  recognizer_busy: { title: "Erkenner belegt", help: "Eine andere Sprach-App (z. B. Hermes Voice) hält das Mikrofon — dort stoppen, dann erneut." },
  recognizer_other: { title: "Erkennungsfehler", help: "Einmalig? Erneut versuchen. Wiederholt? Bubble neu starten (App öffnen)." },
  mic_permission: { title: "Mikrofon-Berechtigung fehlt", help: "App-Info → Berechtigungen → Mikrofon erlauben." },
  recording_failed: { title: "Aufnahme fehlgeschlagen", help: "Mikrofon-Konflikt oder Speicher — andere Aufnahme-Apps schließen." },
  cloud_auth: { title: "Cloud nicht angemeldet", help: "Diktat-Einstellungen → Anmelden (Dashboard-Login im WebView)." },
  cloud_network: { title: "Server nicht erreichbar", help: "Tailscale/VPN auf dem Handy prüfen — das Dashboard muss erreichbar sein." },
  cloud_server: { title: "Serverfehler bei Transkription", help: "Homeserver-Whisper prüfen (Dashboard → System); Diktat bleibt lokal wiederholbar." },
  cloud_too_large: { title: "Aufnahme zu lang", help: "Cloud-Diktate sind auf 3 Minuten begrenzt — in Abschnitten diktieren." },
  cloud_empty: { title: "Leere Transkription", help: "Server hat nichts erkannt — lauter/deutlicher, oder On-Device nutzen." },
  insert_failed: { title: "Text konnte nicht eingefügt werden", help: "Zielfeld verlor den Fokus — Feld antippen und erneut diktieren." },
};

const SETUP_STEPS: Array<{ title: string; detail: string }> = [
  { title: "APK laden & installieren", detail: "Download-Button oben (im Handy-Browser dieser Seite). Play Protect: „Weitere Details“ → „Trotzdem installieren“. Updates installieren ohne Deinstallation." },
  { title: "App öffnen & Mikrofon erlauben", detail: "„Hermes Diktat“ öffnen, Mikrofon-Berechtigung bestätigen." },
  { title: "Tastatur aktivieren", detail: "„Aktivieren“ → Android-Tastaturliste → Hermes Diktat einschalten → „Auswählen“ im Picker." },
  { title: "Diktieren", detail: "In beliebigem Textfeld die Tastatur wechseln (Globus-Symbol). Tap = Start, Tap = Stopp. „Punkt“, „Komma“, „neue Zeile“ werden gesetzt; Formatierung läuft automatisch." },
  { title: "Bubble (systemweit, optional)", detail: "In den App-Einstellungen das Overlay aktivieren — diktieren ohne Tastaturwechsel, Bubble schwebt über jeder App." },
  { title: "Cloud-Qualität (optional)", detail: "Einstellungen → Cloud-Schalter → Anmelden (Dashboard-Login). Pro Diktat per Chip wählbar; danach fällt der Modus sichtbar auf On-Device zurück. Audio bleibt auf Piets Homeserver." },
];

function useDictateArtifacts(): { artifacts: DictateArtifact[] | null; error: string | null } {
  const [artifacts, setArtifacts] = useState<DictateArtifact[] | null>(null);
  const [error, setError] = useState<string | null>(null);
  useEffect(() => {
    let cancelled = false;
    fetchJSON<unknown>("/api/artifacts")
      .then((raw) => {
        if (cancelled) return;
        setArtifacts(dictateApks(ArtifactsResponseSchema.parse(raw).artifacts));
      })
      .catch((cause: unknown) => {
        if (cancelled) return;
        setError(cause instanceof Error ? cause.message : "Artefakte nicht erreichbar");
      });
    return () => {
      cancelled = true;
    };
  }, []);
  return { artifacts, error };
}

/** sha256-Begleitdatei des neuesten APKs (falls vorhanden) als Hex-String. */
function useSha256(artifacts: DictateArtifact[] | null): string | null {
  const [sha, setSha] = useState<string | null>(null);
  const latest = artifacts?.[0]?.name ?? null;
  useEffect(() => {
    if (!latest) return;
    let cancelled = false;
    authedFetch(`/api/artifacts/${encodeURIComponent(`${latest}.sha256`)}`)
      .then(async (response) => {
        if (!response.ok) return;
        const text = await response.text();
        const hex = text.trim().split(/\s+/)[0] ?? "";
        if (!cancelled && /^[0-9a-f]{64}$/i.test(hex)) setSha(hex);
      })
      .catch(() => undefined);
    return () => {
      cancelled = true;
    };
  }, [latest]);
  return sha;
}

export function DiktatView() {
  const dictate = useDictateStatus();
  const { artifacts, error: artifactsError } = useDictateArtifacts();
  const sha256 = useSha256(artifacts);
  return (
    <DiktatBody
      status={dictate.data}
      statusLoading={dictate.loading}
      statusError={dictate.error}
      artifacts={artifacts}
      artifactsError={artifactsError}
      sha256={sha256}
    />
  );
}

/** Reine Darstellung — von DiktatView mit Live-Daten befüllt, im Test mit Fixtures. */
export function DiktatBody({
  status,
  statusLoading,
  statusError,
  artifacts,
  artifactsError,
  sha256,
}: {
  status: DictateStatusResponse | null;
  statusLoading: boolean;
  statusError: string | null;
  artifacts: DictateArtifact[] | null;
  artifactsError: string | null;
  sha256: string | null;
}) {
  const [downloadError, setDownloadError] = useState<string | null>(null);
  const latest = artifacts?.[0] ?? null;
  const older = artifacts ? olderBuilds(artifacts) : [];
  const lastError = status?.last_error ?? null;
  const update = updateHint(status, artifacts);

  const download = (artifact: DictateArtifact) => {
    setDownloadError(null);
    void openAuthedApiFile(`/api/artifacts/${encodeURIComponent(artifact.name)}`, artifact.name).catch(
      (cause: unknown) => {
        setDownloadError(cause instanceof Error ? cause.message : "APK-Download fehlgeschlagen");
      },
    );
  };

  return (
    <div className="grid grid-cols-1 gap-4">
      <header>
        <Eyebrow>Hermes Diktat</Eyebrow>
        <h2 className="mt-1 font-display text-h2 font-semibold text-ink">Systemweites Diktat — der Wispr-Flow-Ersatz</h2>
        <p className="mt-1 text-body text-ink-2">
          Push-to-Talk-Tastatur + Bubble für jedes Android-Textfeld. On-Device by default, Cloud-Whisper als Opt-in —
          Audio und Text bleiben auf dem Homeserver. Diese Seite bündelt Download, Einrichtung und Live-Status.
        </p>
      </header>

      <DictateStatusTile status={status} loading={statusLoading} error={statusError} />

      <div className="grid min-w-0 grid-cols-1 gap-4 lg:grid-cols-2">
        <FleetPanel
          eyebrow="Download & Versionen"
          meta={<span className="inline-flex items-center gap-1 text-xs text-ink-2"><Smartphone className="h-3.5 w-3.5" aria-hidden /> im Handy-Browser öffnen → laden</span>}
        >
          {artifactsError ? (
            <div className="flex items-start gap-2 rounded-card border border-status-alert/30 bg-status-alert/10 px-3 py-2 text-sec text-status-alert">
              <TriangleAlert aria-hidden className="mt-0.5 size-4 shrink-0" />
              <span><strong>Artefakte nicht erreichbar:</strong> {artifactsError}</span>
            </div>
          ) : !artifacts ? (
            <p className="text-sm text-ink-2">Versionen werden geladen …</p>
          ) : !latest ? (
            <FleetEmptyState title="Kein APK im Artefakt-Store" desc="Es liegt kein hermes-dictate-*.apk unter ~/Android/artifacts — Build/Delivery prüfen." />
          ) : (
            <div className="space-y-3">
              <div className="flex flex-wrap items-center justify-between gap-3 rounded-card border border-line bg-surface-2 px-3 py-2.5">
                <div className="min-w-0">
                  <p className="truncate font-medium text-ink">{latest.name}</p>
                  <p className="text-xs text-ink-2">
                    {fmtMegabytes(latest.size)} · Stand {fmtRelativeTime(latest.mtime, nowSec())}
                  </p>
                </div>
                <button type="button" className="ch-btn min-h-12 px-3 text-xs font-medium" onClick={() => download(latest)}>
                  <Download className="h-4 w-4" /> APK laden
                </button>
              </div>
              {update ? (
                <p className="rounded-card border border-status-warn/40 bg-status-warn/10 px-3 py-2 text-xs text-status-warn">
                  {update}
                </p>
              ) : null}
              {sha256 ? (
                <p className="break-all text-xs text-ink-3">
                  sha256 <code className="text-ink-2">{sha256}</code>
                </p>
              ) : null}
              {downloadError ? <p className="text-xs text-status-alert">{downloadError}</p> : null}
              {older.length > 0 ? (
                <div>
                  <Eyebrow>Ältere Builds</Eyebrow>
                  <ul className="mt-1 space-y-1">
                    {older.map((artifact) => (
                      <li key={artifact.name} className="flex items-center justify-between gap-2 text-xs text-ink-2">
                        <span className="min-w-0 truncate">{artifact.name} · {fmtMegabytes(artifact.size)} · {fmtRelativeTime(artifact.mtime, nowSec())}</span>
                        <button type="button" className="ch-btn min-h-9 px-2 text-xs" onClick={() => download(artifact)}>
                          laden
                        </button>
                      </li>
                    ))}
                  </ul>
                </div>
              ) : null}
            </div>
          )}
        </FleetPanel>

        <FleetPanel eyebrow="Einrichtung in 6 Schritten">
          <ol className="space-y-2.5">
            {SETUP_STEPS.map((step, index) => (
              <li key={step.title} className="flex gap-3">
                <span aria-hidden className="mt-0.5 inline-flex h-5 w-5 shrink-0 items-center justify-center rounded-full border border-line bg-surface-2 text-xs text-ink-2">
                  {index + 1}
                </span>
                <div className="min-w-0">
                  <p className="text-sm font-medium text-ink">{step.title}</p>
                  <p className="text-xs text-ink-2">{step.detail}</p>
                </div>
              </li>
            ))}
          </ol>
        </FleetPanel>
      </div>

      <div className="grid min-w-0 grid-cols-1 gap-4 lg:grid-cols-2">
        <FleetPanel eyebrow="Wenn etwas hakt" meta={lastError ? <span className="text-xs text-status-alert">zuletzt gemeldet: {DICTATE_ERROR_HELP[lastError]?.title ?? lastError}</span> : undefined}>
          <ul className="space-y-2">
            {Object.entries(DICTATE_ERROR_HELP).map(([code, entry]) => (
              <li
                key={code}
                className={
                  code === lastError
                    ? "rounded-card border border-status-alert/40 bg-status-alert/10 px-3 py-2"
                    : "rounded-card border border-line bg-surface-2 px-3 py-2"
                }
              >
                <p className="text-sm font-medium text-ink">{entry.title}</p>
                <p className="text-xs text-ink-2">{entry.help}</p>
              </li>
            ))}
          </ul>
        </FleetPanel>

        <FleetPanel eyebrow="Datenschutz">
          <div className="flex items-start gap-3">
            <ShieldCheck aria-hidden className="mt-0.5 size-5 shrink-0 text-ink-2" />
            <div className="space-y-1.5 text-sm text-ink-2">
              <p><strong className="text-ink">On-Device by default:</strong> ohne Cloud-Opt-in verlässt kein Audio das Handy — es wird ausschließlich der Offline-Recognizer gebunden, ein Netz-Fallback existiert nicht.</p>
              <p><strong className="text-ink">Cloud = Homeserver:</strong> das Opt-in schickt Audio an den eigenen Whisper auf dem Homeserver, an keinen Drittanbieter. Nach jedem Cloud-Diktat springt der Modus sichtbar zurück.</p>
              <p><strong className="text-ink">Diese Seite sieht nur Metadaten:</strong> Version, Zähler, Latenz, Fehlerklassen — nie Audio, nie Diktattext.</p>
            </div>
          </div>
        </FleetPanel>
      </div>
    </div>
  );
}
