// Local UI strings for the /lanes Modell-Plattform. Deliberately NOT in
// i18n/de.ts: this feature touches no shared file a parallel session may be
// editing (the established Lanes isolation pattern). Lives in the view folder.
import type { LaneProbeStatus } from "./api";

export const t = {
  title: "Lanes",
  emptyLanesTitle: "Keine Lanes",
  emptyLanesDesc: "Beim ersten Laden werden api-standard und max-abo angelegt.",

  // LaneBar
  aktiv: "Aktiv",
  activate: "Aktivieren",
  activateConfirm: "Als aktive Lane setzen?",
  confirmYes: "Bestätigen",
  confirmNo: "Abbrechen",
  neueLane: "Neue Lane",
  neueLanePlaceholder: "Name für neue Lane",
  create: "Anlegen",
  overrides: (n: number) => (n === 1 ? "1 Override" : `${n} Overrides`),
  profileCount: (n: number) => (n === 1 ? "1 Profil" : `${n} Profile`),
  builtin: "builtin",
  eigeneLane: "eigene Lane",
  zuletzt: (time: string) => `zuletzt ${time}`,

  // ProfileMatrix
  colRole: "Rolle",
  colModel: "Modell",
  colReasoning: "Reasoning",
  colFallback: "Fallback",
  colProbe: "Probe",
  colOverride: "Override",
  standard: "Standard",
  override: "Override",
  fallbacks: (n: number) => (n === 0 ? "—" : String(n)),
  fallbackEdit: "Fallbacks bearbeiten",
  probeUngeprüft: "ungeprüft",
  probeMessen: "Modell messen",
  probing: "misst …",
  save: "Speichern + aktivieren",
  saving: "speichert …",
  discard: "Verwerfen",
  saveHint: "wirkt ab dem nächsten Spawn",
  matrixEyebrow: "Profil-Matrix",
  currently: (value: string) => `aktuell: ${value}`,

  // Fallback drawer
  fallbackTitle: "Fallback-Kette",
  fallbackProvider: "Provider",
  fallbackModel: "Modell",
  fallbackAdd: "Fallback hinzufügen",
  fallbackRemove: "entfernen",
  fallbackEmpty: "Keine Fallbacks — primäres Modell ohne Ausweichkette.",
  apply: "Übernehmen",
  close: "Schließen",

  // Rauch (smoke) panel
  rauch: "Rauch",
  katalogMessen: (n: number) => `Katalog messen · ${n} sinnvolle Modelle`,
  measuring: "misst …",
  erreichbar: "erreichbar",
  blockiert: "blockiert",
  p50: "p50 Latenz",
  noProbeData: "—",
  smokeEmptyTitle: "Noch keine Messungen",
  smokeEmptyEval: "Erreichbarkeit und Latenz der Modelle sind ungesehen.",
  smokeEmptyAction: "Katalog messen startet sequenzielle Probes (moderate Kosten).",
  zuletztGemessen: (time: string) => `zuletzt gemessen ${time}`,
  truncated: (n: number) => `auf ${n} begrenzt`,
  probeFeed: "Ergebnis-Feed",

  // Kompass (compass) panel
  kompass: "Kompass",
  übernehmen: "Übernehmen",
  übernommen: "Übernehmen ✓",
  aktuellMarker: "● aktuell",
  fitTop: "Top-Modelle für diese Rolle",
  bench: "Bench",
  benchSelect: "2–4 Modelle wählen",
  benchRun: "Bench starten",
  benchRunning: "Bench läuft …",
  benchRepeat: "Bench mit Auswahl wiederholen",
  benchEmpty: "Keine Auswahl — mindestens zwei Modelle für einen Vergleich wählen.",
  compassHint: "Scoring aus Latenz, Preis, Reasoning-Support und Kontext gegen das Rollen-Profil.",

  // shared
  loading: "Lade Modelle …",
  retry: "Erneut versuchen",
  of: (a: number, b: number) => `${a}/${b}`,
};

export const PROBE_STATUS_LABEL: Record<LaneProbeStatus, string> = {
  ok: "OK",
  fallback: "Fallback",
  auth_error: "Auth-Fehler",
  quota_or_rate_limit: "Quota/Rate",
  timeout: "Timeout",
  config_error: "Config-Fehler",
  error: "Fehler",
  skipped: "Skipped",
};

/** Status trio mapping for a probe result (LED + label, never color alone). */
export function probeTone(status: LaneProbeStatus): "ok" | "warn" | "alert" | "neutral" {
  if (status === "ok" || status === "fallback") return "ok";
  if (status === "quota_or_rate_limit") return "warn";
  if (status === "auth_error" || status === "timeout" || status === "config_error" || status === "error") return "alert";
  return "neutral";
}
