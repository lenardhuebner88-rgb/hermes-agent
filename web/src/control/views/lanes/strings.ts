// Local UI strings for the /lanes Modell-Plattform. Deliberately NOT in
// i18n/de.ts: this feature touches no shared file a parallel session may be
// editing (the established Lanes isolation pattern). Lives in the view folder.
import type { LaneProbeStatus } from "./api";

export const t = {
  title: "Lanes",
  emptyLanesTitle: "Keine Lanes",
  emptyLanesDesc: "Beim ersten Laden werden api-standard und max-abo angelegt.",
  reload: "Neu laden",

  // Frische / Stand (FleetSourceFreshness-Stil)
  stand: (time: string) => `Stand ${time}`,
  stale: "veraltet",

  // LaneBar
  aktiv: "Aktiv",
  ansicht: "Ansicht",
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

  // Lane-Verwaltung (Umbenennen / Duplizieren / Löschen)
  manage: "Verwalten",
  manageTitle: "Lane verwalten",
  rename: "Umbenennen",
  renameSave: "Namen speichern",
  duplicate: "Duplizieren",
  deleteLane: "Lane löschen",
  deleteConfirm: (name: string) => `Lane „${name}" endgültig löschen? Overrides gehen verloren.`,
  deleteYes: "Endgültig löschen",
  builtinNoDelete: "Builtin-Lanes können nicht gelöscht werden.",

  // Lane-Entwurf (Kompass-Presets, S5)
  presetLabel: "Entwurf",
  presetLeer: "Leer",
  presetGuenstig: "Coding-Team günstig",
  presetPremium: "Premium",
  presetHint: "Preset füllt alle Rollen mit dem Kompass-Spitzenmodell.",

  // Gesundheits-Strip (S5)
  healthEyebrow: "Gesundheit",
  unhealthy: (n: number) => (n === 1 ? "1 unhealthy" : `${n} unhealthy`),
  spawnOk: "alle spawn-bereit",
  spawnUnknown: "keine Spawn-Evidenz",
  jumpToRow: "zur Zeile",

  // ProfileMatrix
  colRole: "Rolle",
  colModel: "Modell",
  colReasoning: "Reasoning",
  colFast: "Fast",
  colFallback: "Fallback",
  colProbe: "Probe",
  colOverride: "Override",
  standard: "Standard",
  override: "Override",
  fallbacks: (n: number) => (n === 0 ? "Fallback —" : `Fallback ${n}`),
  fallbackEdit: "Fallbacks bearbeiten",
  probeUngeprüft: "ungeprüft",
  probeMessen: "Modell messen",
  probing: "misst …",
  probeFehler: "Probe fehlgeschlagen",
  save: "Speichern + aktivieren",
  saving: "speichert …",
  savedOk: "Gespeichert ✓",
  discard: "Verwerfen",
  saveHint: "wirkt ab dem nächsten Spawn",
  matrixEyebrow: "Profil-Matrix",
  currently: (value: string) => `aktuell: ${value}`,
  spawnHealth: (status: string) => `Spawn: ${status}`,

  // FastControl (service tier / fast mode)
  fastStd: "Std",
  fastFast: "Fast",
  fastStdFull: "Standard (Profil-Config)",
  fastFastFull: "Fast Mode (service_tier=priority / speed=fast)",
  fastUnsupported: "Modell unterstützt keinen Fast Mode (kein service_tier/speed-Transport)",

  // Fallback drawer
  fallbackTitle: "Fallback-Kette",
  fallbackProvider: "Provider",
  fallbackModel: "Modell",
  fallbackChooseProvider: "Provider wählen …",
  fallbackChooseModel: "Modell wählen …",
  fallbackIncomplete: "Provider und Modell wählen — unvollständige Zeilen werden verworfen.",
  fallbackAdd: "Fallback hinzufügen",
  fallbackRemove: "entfernen",
  fallbackEmpty: "Keine Fallbacks — primäres Modell ohne Ausweichkette.",
  apply: "Übernehmen",

  // Rauch (smoke) panel
  rauch: "Rauch",
  katalogMessen: (n: number) => `Katalog messen · ${n} sinnvolle Modelle`,
  katalogFehler: "Katalog-Messung fehlgeschlagen",
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
  übernommen: "Übernommen ✓",
  aktuellMarker: "● aktuell",
  fitTop: "Top-Modelle für diese Rolle",
  bench: "Bench",
  benchFehler: "Bench fehlgeschlagen",
  benchSelect: "2–4 Modelle wählen",
  benchRun: "Bench starten",
  benchRunning: "Bench läuft …",
  benchRepeat: "Bench mit Auswahl wiederholen",
  benchEmpty: "Keine Auswahl — mindestens zwei Modelle für einen Vergleich wählen.",
  benchColModel: "Modell",
  benchColStatus: "Status",
  benchColLatency: "Latenz",
  benchColPrice: "Preis",
  benchColReasoning: "Reasoning",
  compassHint: "Scoring aus Latenz, Preis, Reasoning-Support und Kontext gegen das Rollen-Profil.",

  // Vergleich (Lane-Diff, S5)
  vergleich: "Vergleich",
  diffLaneA: "Lane A",
  diffLaneB: "Lane B",
  diffOnlyChanges: "nur Abweichungen",
  diffAll: "alle Rollen",
  diffColRole: "Rolle",
  diffIdentical: "Keine Abweichungen — beide Lanes sind identisch verdrahtet.",
  diffNeedTwo: "Zwei Lanes für einen Vergleich wählen.",

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
