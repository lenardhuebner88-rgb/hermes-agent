/**
 * watcherDigest — S5-Design („JARVIS OS"): der Wächter tritt aus dem Gespräch
 * in die Peripherie. Watcher-Nachrichten (engine === "pa-watcher") werden nicht
 * mehr als Chat-Bubbles gerendert, sondern hier rein clientseitig zu einem
 * deduplizierten Digest verdichtet (Tages-Zähler + letzter Stand) — Backend und
 * Rohdaten (gateway/pa_watcher.py) bleiben unberührt (Presentation-Layer only).
 *
 * Bundle-Textformat des Watchers:
 *   Jarvis-Wächter: N signifikante Ereignisse gebündelt.
 *   - <title> — <state> (Beleg: …)
 * mit state ∈ completed | blocked:<grund> | review_wait_attention | gave_up,
 * dazu freie Zeilen „Neues Receipt: …" und „Agenten-Session beendet: …".
 *
 * Dedupe-Regel: gleiche taskId (sonst gleicher Titel) → nur das NEUESTE Event
 * überlebt (gave_up/review_wait_attention verschwinden, sobald completed da
 * ist). Die Zähler laufen über die deduplizierte Menge: attentionOpen =
 * letzter Stand review_wait_attention ohne späteres completed/blocked.
 */
import type { PaChatMessage } from "@/lib/api";

export type WatcherState =
  | "completed"
  | "attention"
  | "blocked"
  | "gave_up"
  | "receipt"
  | "session"
  | "info";

export interface WatcherEvent {
  /** Task-Referenz aus /t_[0-9a-f]{8}/, null wenn die Zeile keine trägt. */
  taskId: string | null;
  state: WatcherState;
  /** Zeile ohne „- ", ohne „(Beleg: …)", ohne „— <state>"-Suffix und ohne
   *  die Klammer-Task-ID (die bleibt im taskId-Feld). */
  title: string;
  /** ts der Ursprungs-Message (Unix-Sekunden). */
  ts: number;
}

export interface WatcherDigest {
  /** Latest-State pro taskId (sonst pro Titel), max N, neueste zuerst. */
  latest: WatcherEvent[];
  completedToday: number;
  attentionOpen: number;
  blockedOpen: number;
  lastEvent: WatcherEvent | null;
}

/** Standard-Tiefe der Peripherie-Liste. */
const DEFAULT_MAX = 5;

const TASK_ID_RE = /t_[0-9a-f]{8}/;
const BELEG_RE = /\s*\(Beleg:[^)]*\)/g;
/** Zustands-Suffix am Zeilenende (nach Entfernung des Belegs). */
const STATE_SUFFIX_RE = /\s*—\s*(completed|blocked:\S+|review_wait_attention|gave_up)\s*$/;
const RECEIPT_RE = /^Neues Receipt:/;
const SESSION_RE = /^Agenten-Session beendet:/;

/** Eine Bundle-Zeile („- …") in ein Event übersetzen; null = keine Event-Zeile. */
function parseLine(rawLine: string, ts: number): WatcherEvent | null {
  let line = rawLine.trim().replace(/^-\s+/, "").trim();
  if (!line) return null;
  line = line.replace(BELEG_RE, "").trim();
  const taskId = TASK_ID_RE.exec(line)?.[0] ?? null;
  let state: WatcherState = "info";
  const suffix = STATE_SUFFIX_RE.exec(line);
  if (suffix) {
    const raw = suffix[1];
    state =
      raw === "completed"
        ? "completed"
        : raw.startsWith("blocked:")
          ? "blocked"
          : raw === "review_wait_attention"
            ? "attention"
            : "gave_up";
    line = line.slice(0, suffix.index).trim();
    // Klammer-Task-ID („(t_492864de)") ist Maschinen-Referenz, kein Titeltext —
    // sie bleibt im taskId-Feld und fliegt aus der Anzeigeform raus.
    line = line.replace(/\(t_[0-9a-f]{8}\)/g, "").replace(/\s{2,}/g, " ").trim();
  } else if (RECEIPT_RE.test(line)) {
    state = "receipt";
  } else if (SESSION_RE.test(line)) {
    state = "session";
  }
  return { taskId, state, title: line, ts };
}

/** Alle Watcher-Events der History in Eingabereihenfolge (älteste zuerst).
 *  Nur engine === "pa-watcher"; die Headerzeile („Jarvis-Wächter: …") und
 *  jede Nicht-„- "-Zeile wird übersprungen. */
export function parseWatcherEvents(messages: PaChatMessage[]): WatcherEvent[] {
  const events: WatcherEvent[] = [];
  for (const message of messages) {
    if (message.engine !== "pa-watcher") continue;
    for (const rawLine of message.content.split("\n")) {
      if (!rawLine.trim().startsWith("-")) continue; // Header + Leerzeilen
      const event = parseLine(rawLine, message.ts);
      if (event) events.push(event);
    }
  }
  return events;
}

/** Lokaler Tagesbeginn in Unix-Sekunden (für den Tages-Zähler). */
function startOfToday(): number {
  const day = new Date();
  day.setHours(0, 0, 0, 0);
  return day.getTime() / 1000;
}

/** Watcher-History → deduplizierter Digest für die Periphery-Zeile.
 *  Dedupe: gleiche taskId (sonst gleicher Titel) — das neueste Event gewinnt
 *  (bei Gleichstand das spätere in Eingabereihenfolge, die History ist
 *  chronologisch). */
export function digestWatcherEvents(
  messages: PaChatMessage[],
  opts?: { max?: number },
): WatcherDigest {
  const max = opts?.max ?? DEFAULT_MAX;
  const events = parseWatcherEvents(messages);
  const byKey = new Map<string, WatcherEvent>();
  for (const event of events) {
    const key = event.taskId ?? `title:${event.title}`;
    const previous = byKey.get(key);
    if (!previous || event.ts >= previous.ts) {
      byKey.set(key, event);
    }
  }
  const deduped = [...byKey.values()].sort((a, b) => b.ts - a.ts);
  const todayStart = startOfToday();
  return {
    latest: deduped.slice(0, max),
    completedToday: deduped.filter((e) => e.state === "completed" && e.ts >= todayStart).length,
    attentionOpen: deduped.filter((e) => e.state === "attention").length,
    blockedOpen: deduped.filter((e) => e.state === "blocked").length,
    lastEvent: deduped[0] ?? null,
  };
}
