/**
 * Statische Jarvis-Inhalte (Mock/Fallback). G2: Brain-/Filter-/Spark-Mocks
 * entfallen mit den Floats; KI-LAGE-Ticker nutzt JARVIS_NEWS_* als Fallback,
 * Graph-Tag und Engine-Switcher behalten ihre Konstanten.
 */

/** Graph-Fallback-Tag (JarvisGraph — sichtbar wenn nicht live). */
export const JARVIS_BRAIN_MOCKTAG = " · Graph: Vorschau (Mock)";

/** Cluster-Tone für Graph-Ableitungen (graphHubs) und künftige Chips. */
export interface JarvisHubRow {
  tone: "cyan" | "gruen" | "amber" | "blau" | "violett" | "pink" | "grau";
  name: string;
  count: string;
}

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

/** Statisches Modell-Badge — Fallback des EngineSwitchers, solange das
 *  Roster (/api/pa/engines) nicht geladen ist. */
export const JARVIS_EMBLEM_MODEL = "◆ GPT-5.6-SOL ▾";

export const JARVIS_ASK_HINT = "Frag · „/plan idee …“ · „erinnere mich …“";
