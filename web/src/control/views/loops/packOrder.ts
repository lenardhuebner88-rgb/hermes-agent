/** Reihenfolge der Pack-Karten im Loops-Tab: was Aufmerksamkeit braucht, zuerst.
 *
 *  Bis 2026-07-28 war die Reihenfolge die Auflösungsreihenfolge der Packs
 *  (praktisch alphabetisch je Repo-Gruppe). Bei 19 Packs, von denen typisch
 *  keins oder eins läuft, konnte das laufende Pack ganz unten stehen — und die
 *  Karten, an denen etwas zu tun ist (verifizierte Commits zum Landen, ein
 *  hängengebliebener Build, ein totes Repo), lagen zwischen ruhigen Karten.
 *
 *  Eigenes Modul statt Helfer in LoopsView.tsx, damit die Ordnung testbar ist,
 *  ohne den ganzen Tab zu rendern.
 */
import type { LoopPackSummary } from "../../lib/types";

/** Rang 0 wird zuerst gezeigt. Kleinere Zahl = dringender. */
export const ATTENTION_RANK = {
  /** Läuft gerade — der einzige Zustand, der sich von selbst ändert. */
  running: 0,
  /** Verifizierte Arbeit wartet auf die Landung: eine Aktion ist möglich. */
  landable: 1,
  /** Commits ohne Verifikation bzw. ein Build, der mitten drin stehenblieb. */
  unsettled: 2,
  /** Der gebundene Repo-Pfad existiert nicht mehr — das Pack kann nicht starten. */
  broken: 3,
  /** Nichts zu tun. */
  quiet: 4,
} as const;

export type AttentionRank = (typeof ATTENTION_RANK)[keyof typeof ATTENTION_RANK];

export function attentionRank(pack: LoopPackSummary): AttentionRank {
  if (pack.running) return ATTENTION_RANK.running;
  // Reihenfolge der Prüfungen IST die Rangfolge: ein Pack mit landbarer Arbeit
  // und totem Repo bleibt zuerst ein Landefall — die Landung liest den
  // Worktree, nicht das Pack-Repo.
  // Landbarkeit exakt wie die Karte sie bestimmt (LoopsView `canLand`):
  // Sweep-Packs fuehren keine Plan-Queue, fuer sie genuegen offene Commits.
  // Ohne diesen Zweig landete JEDER Sweep faelschlich unter "unsettled" — im
  // ersten Bau sortierte das ein unverifiziertes Pipeline-Pack vor zwei
  // landbare Sweeps (visuell aufgefallen, 28.07.).
  const hasVerifiedPlan = pack.type !== "pipeline" || (pack.queue?.["20-verified"] ?? 0) > 0;
  if (pack.commits_ahead > 0 && hasVerifiedPlan) return ATTENTION_RANK.landable;
  const building = pack.queue?.["10-building"] ?? 0;
  if (pack.commits_ahead > 0 || building > 0) return ATTENTION_RANK.unsettled;
  if (!pack.repo_exists) return ATTENTION_RANK.broken;
  return ATTENTION_RANK.quiet;
}

/** Stabil sortierte Kopie: gleicher Rang → alphabetisch, damit die Position
 *  einer ruhigen Karte sich zwischen zwei Polls nicht grundlos bewegt. */
export function sortPacksByAttention(packs: readonly LoopPackSummary[]): LoopPackSummary[] {
  return [...packs].sort((a, b) => {
    const delta = attentionRank(a) - attentionRank(b);
    return delta !== 0 ? delta : a.name.localeCompare(b.name);
  });
}
