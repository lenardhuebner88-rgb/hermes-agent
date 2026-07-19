/**
 * A4-Statik-Inhalte der Jarvis-Shell (Sprint 1): Brain-Panel-Stats, Top-Hubs,
 * Filter-Zähler, KI-Lage und Sparklines sind der freigegebene Mock aus
 * jarvis-variante-a4-brain-feinschliff.html (Szene LIVE). KEINE Endpoints —
 * echte Daten folgen in S2/S3 (Graph S2.7). Wartet-dezent ist NICHT hier:
 * das Panel hängt an echten offenen Fragen (WartetPanel.tsx).
 */

export const JARVIS_BRAIN_STATS = "13 911 Notizen indiziert · 4 738 Kanban-Tasks · qmd live ✓";
export const JARVIS_BRAIN_MOCKTAG = " · Graph: Vorschau (Mock)";
export const JARVIS_SEARCH_HINT = "Das Gehirn durchsuchen …";

export interface JarvisHubRow {
  /** Cluster-Farbe aus dem Graph-Mock (CSS-var-Name ohne Prefix). */
  tone: "cyan" | "gruen" | "amber" | "blau" | "violett" | "pink" | "grau";
  name: string;
  count: string;
}

export const JARVIS_TOP_HUBS: JarvisHubRow[] = [
  { tone: "cyan", name: "00-Canon", count: "13" },
  { tone: "gruen", name: "Kanban-Tasks", count: "4 738" },
  { tone: "amber", name: "Skills", count: "84" },
  { tone: "pink", name: "Receipts", count: "1 887" },
  { tone: "violett", name: "Memories", count: "7" },
  { tone: "blau", name: "Design-Board", count: "19" },
];

export const JARVIS_FILTER_ROWS: JarvisHubRow[] = [
  { tone: "cyan", name: "Canon", count: "13" },
  { tone: "gruen", name: "Projekte", count: "4" },
  { tone: "amber", name: "Agenten", count: "8" },
  { tone: "blau", name: "Skills", count: "84" },
  { tone: "violett", name: "Memories", count: "7" },
  { tone: "pink", name: "Receipts", count: "1 887" },
  { tone: "grau", name: "Archiv", count: "4 322" },
];

export interface JarvisNewsItem {
  text: string;
  source: string;
  lead?: boolean;
}

export const JARVIS_NEWS_CRON = "cron 06:00 ✓";

export const JARVIS_NEWS_ITEMS: JarvisNewsItem[] = [
  {
    lead: true,
    text: "OpenAI senkt Realtime-API-Preise um ~40 % — relevant für unseren Duplex-Spike",
    source: "LLM-WIKI · MODELS",
  },
  {
    text: "Kimi K3.1 Coding-Update rollt aus — Changelog gelesen, Lane unverändert",
    source: "X-LENS · GROK",
  },
  {
    text: "DeepSeek R4-Gewichte offen — Kandidat fürs lokale Fallback-Brett",
    source: "LLM-WIKI · LANDSCAPE",
  },
  {
    text: "EU-AI-Act Stufe 2 ab 01.08. — keine Pflichten für Piet-Estate",
    source: "LAGEBERICHT · RECHT",
  },
];

export const JARVIS_EMBLEM_NAME = "J.A.R.V.I.S.";
export const JARVIS_EMBLEM_STATUS = "● ONLINE · WACHE AKTIV";
/** Statisches Modell-Badge — S1-Platzhalter, seit S2.2 nur noch Fallback des
 *  EngineSwitchers, solange das Roster (/api/pa/engines) nicht geladen ist. */
export const JARVIS_EMBLEM_MODEL = "◆ GPT-5.6-SOL ▾";

export const JARVIS_ASK_HINT =
  "Frag · „/plan idee …“ · „erinnere mich …“ · „guten Morgen“";

export interface JarvisSpark {
  label: string;
  value: string;
  tone: "cyan" | "amber" | "grau";
  /** A4-Pfade: Fläche (fill) + Linie (stroke), viewBox 0 0 100 22. */
  areaPath: string;
  linePath: string;
}

export const JARVIS_SPARKS: JarvisSpark[] = [
  {
    label: "CPU",
    value: "14 %",
    tone: "cyan",
    areaPath:
      "M0 18 L8 16 L16 17 L24 13 L32 15 L40 12 L48 14 L56 9 L64 13 L72 11 L80 14 L88 12 L100 13 L100 22 L0 22 Z",
    linePath:
      "M0 18 L8 16 L16 17 L24 13 L32 15 L40 12 L48 14 L56 9 L64 13 L72 11 L80 14 L88 12 L100 13",
  },
  {
    label: "RAM",
    value: "61 %",
    tone: "amber",
    areaPath:
      "M0 12 L10 11 L20 13 L30 10 L40 11 L50 9 L60 10 L70 8 L80 10 L90 9 L100 10 L100 22 L0 22 Z",
    linePath:
      "M0 12 L10 11 L20 13 L30 10 L40 11 L50 9 L60 10 L70 8 L80 10 L90 9 L100 10",
  },
  {
    label: "DISK",
    value: "53 %",
    tone: "grau",
    areaPath: "M0 14 L100 12 L100 22 L0 22 Z",
    linePath: "M0 14 L100 12",
  },
];
