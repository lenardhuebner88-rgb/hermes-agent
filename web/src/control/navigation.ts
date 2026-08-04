import type { ComponentType } from "react";
import { Anchor, BadgeCheck, BookOpen, ChartSpline, Clock, FlaskConical, FolderGit2, Gauge, GitBranch, Hammer, KanbanSquare, LayoutDashboard, Lightbulb, Mic2, PenTool, RefreshCw, SearchCheck, Server, Shield, TerminalSquare, Workflow } from "lucide-react";
import { de } from "./i18n/de";

export function legacyControlRedirectTarget(targetPath: string, search: string): string {
  // Abriss S6: legacy deep-links must preserve drawer/filter intent
  // (/control/flow?task=... → /control/fleet?task=..., etc.).
  return `${targetPath}${search}`;
}

/**
 * DIE eine Route-Tabelle für /control (UI/UX-Iteration 3, 2026-07-30).
 *
 * Vorher war die Route-Konfiguration über sechs hand-synchronisierte Strukturen
 * verstreut (ControlPage lazy-imports, activeFromPath, viewImporters, tabPath,
 * ControlTab-Union, tabs/moreTabs in ControlShell) — inklusive Drift durch tote
 * Redirect-only-Schlüssel (flow/ketten/pressure/ops/pulse/overview). Jetzt
 * treibt diese Tabelle ALLE Consumer: ControlTab-Union (abgeleitet), Lazy-
 * Loading + Hover-Prefetch (denselben `load`-Importer — Vite deduped den
 * Chunk), aktive-Tab-Erkennung, Rail/Bottom-Bar/"Mehr"-Nav und Masthead-Label.
 *
 * Bewusst NICHT hier: Detail-Routen ohne eigenen Tab (runs/:runId, issues,
 * design-board/:cardId, projekte-klassisch) und die Legacy-Redirect-Routen
 * (overview/pulse/flow/ketten/hermes/pressure/ops) — die bleiben explizites
 * JSX in ControlPage.tsx bzw. Einträge in LEGACY_TAB_ALIASES unten.
 */
export interface ControlRoute {
  /** Stabiler Tab-Schlüssel (Badges, Prefetch, Tastatur-Shortcuts). */
  readonly id: string;
  /** Absolute Route, z. B. "/control/fleet" — Nav-Links, tabPath, activeFromPath. */
  readonly path: string;
  /** Relatives Segment für <Route path> innerhalb von /control/*. */
  readonly segment: string;
  /** Masthead-/Rail-Label (Desktop). */
  readonly label: string;
  /** Kurzlabel der mobilen Bottom-Bar (nur Primaries). */
  readonly mobileLabel?: string;
  readonly icon: ComponentType<{ className?: string }>;
  /** primary = Rail + Bottom-Bar (die 5 täglichen Wirbelsäulen-Tabs); more = "Mehr". */
  readonly nav: "primary" | "more";
  /** Export-Name der View im Lazy-Chunk. Fehlt bei eager Views (inbox). */
  readonly exportName?: string;
  /** Chunk-Importer — geteilt von lazy() und Hover/Touch-Prefetch. */
  readonly load?: () => Promise<unknown>;
}

export const CONTROL_ROUTES = [
  // Die tägliche Wirbelsäule: Fleet · Start · Terminal · Statistik · Regal.
  // Fleet: erstes Tab — Operator-Lagezentrum (2026-07-03).
  { id: "fleet", path: "/control/fleet", segment: "fleet", label: "Fleet", mobileLabel: "Fleet", icon: Anchor, nav: "primary", exportName: "FleetView", load: () => import("./views/FleetView") },
  { id: "inbox", path: "/control", segment: "", label: "Start", mobileLabel: "Start", icon: LayoutDashboard, nav: "primary" },
  // Terminals = Haupt-Arbeitszentrale (Operator-Entscheid 2026-07-01): der Tab,
  // in dem im tmux mit Hermes/Claude/Codex gearbeitet wird — daher primär.
  { id: "agentTerminals", path: "/control/agent-terminals", segment: "agent-terminals", label: "Terminals", mobileLabel: "Terminal", icon: TerminalSquare, nav: "primary", exportName: "AgentTerminalsView", load: () => import("./views/AgentTerminalsView") },
  { id: "statistik", path: "/control/statistik", segment: "statistik", label: de.tabs.statistik, mobileLabel: "Statistik", icon: ChartSpline, nav: "primary", exportName: "StatistikView", load: () => import("./views/StatistikView") },
  // Kurzlabel "Regal" fürs 6-Slot-Grid der mobilen Bottom-Bar (Cockpit-Slice
  // "Bibliothek-Lesesaal + Shell-Upgrade") — Desktop-`label` bleibt "Bibliothek".
  { id: "bibliothek", path: "/control/bibliothek", segment: "bibliothek", label: "Bibliothek", mobileLabel: "Regal", icon: BookOpen, nav: "primary", exportName: "BibliothekView", load: () => import("./views/BibliothekView") },

  // Demotierte Kontrollflächen — weiterhin geroutet + erreichbar, nur nicht in
  // der primären Rail/Bottom-Bar. Die Command-Home zeigt ihr Headline-Signal.
  { id: "workstreams", path: "/control/workstreams", segment: "workstreams", label: de.tabs.workstreams, icon: GitBranch, nav: "more", exportName: "AgentOpsView", load: () => import("./views/AgentOpsView") },
  { id: "backlog", path: "/control/backlog", segment: "backlog", label: de.tabs.backlog, icon: KanbanSquare, nav: "more", exportName: "BacklogView", load: () => import("./views/BacklogView") },
  { id: "orchestrator", path: "/control/orchestrator", segment: "orchestrator", label: de.tabs.orchestrator, icon: Workflow, nav: "more", exportName: "OrchestratorBacklogView", load: () => import("./views/OrchestratorBacklogView") },
  { id: "crons", path: "/control/crons", segment: "crons", label: de.tabs.crons, icon: Clock, nav: "more", exportName: "CronView", load: () => import("./views/CronView") },
  { id: "loops", path: "/control/loops", segment: "loops", label: de.tabs.loops, icon: RefreshCw, nav: "more", exportName: "LoopsView", load: () => import("./views/LoopsView") },
  // Projekte: bewusst in "Mehr" statt in den 5 primären Tabs — die Primaries
  // sind die kuratierte tägliche Wirbelsäule (DESIGN.md UX-Kontrakt Punkt 1:
  // Phone-Erreichbarkeit über das mobile "Mehr"-Sheet in 2 Taps).
  { id: "projekte", path: "/control/projekte", segment: "projekte", label: de.tabs.projekte, icon: FolderGit2, nav: "more", exportName: "JarvisShellView", load: () => import("./jarvis/JarvisShellView") },
  // Label literal (wie "Start"): die Lanes-Strings leben im View, nicht in
  // i18n/de.ts — kein Edit an Shared-Dateien paralleler Sessions.
  { id: "lanes", path: "/control/lanes", segment: "lanes", label: "Lanes", icon: Shield, nav: "more", exportName: "LanesView", load: () => import("./views/LanesView") },
  // System (S1-Fusion): Druck + Ops Radar + Puls in einer Leitstand-Ansicht.
  { id: "system", path: "/control/system", segment: "system", label: "System", icon: Server, nav: "more", exportName: "SystemView", load: () => import("./views/system/SystemView") },
  { id: "scorecard", path: "/control/scorecard", segment: "scorecard", label: "Scorecard", icon: BadgeCheck, nav: "more", exportName: "ScorecardView", load: () => import("./views/ScorecardView") },
  // Hebel (S7): Kosten-SSOT-Tab auf dem usage-facts.v1 Read-Pfad.
  { id: "hebel", path: "/control/hebel", segment: "hebel", label: "Hebel", icon: Gauge, nav: "more", exportName: "HebelView", load: () => import("./views/HebelView") },
  // Verbrauch (usage-observability-Goal): PAYG-Äquivalent + Hebel auf
  // usage-consumption.v1. Label literal (wie "Lanes"): i18n/de.ts wird von
  // parallelen Sessions editiert.
  { id: "verbrauch", path: "/control/verbrauch", segment: "verbrauch", label: "Verbrauch", icon: ChartSpline, nav: "more", exportName: "VerbrauchView", load: () => import("./views/VerbrauchView") },
  // Programm 3: Recherche (Wissen beauftragen); Bibliothek sitzt in der
  // Haupt-Nav, dafür wohnt Autoresearch hier.
  { id: "research", path: "/control/research", segment: "research", label: "Recherche", icon: SearchCheck, nav: "more", exportName: "ResearchView", load: () => import("./views/ResearchView") },
  { id: "autoresearch", path: "/control/autoresearch", segment: "autoresearch", label: de.tabs.autoresearch, icon: FlaskConical, nav: "more", exportName: "AutoresearchView", load: () => import("./views/AutoresearchView") },
  // Prompt-Schmiede: Copy-Paste-Generator für Agent-Steuerbefehle (kein Dispatch).
  { id: "schmiede", path: "/control/schmiede", segment: "schmiede", label: de.tabs.schmiede, icon: Hammer, nav: "more", exportName: "SchmiedeView", load: () => import("./views/SchmiedeView") },
  { id: "stratege", path: "/control/stratege", segment: "stratege", label: "Stratege", icon: Lightbulb, nav: "more", exportName: "StrategistView", load: () => import("./views/StrategistView") },
  { id: "designBoard", path: "/control/design-board", segment: "design-board", label: "Design", icon: PenTool, nav: "more", exportName: "DesignBoardView", load: () => import("./views/DesignBoardView") },
  { id: "diktat", path: "/control/diktat", segment: "diktat", label: "Diktat", icon: Mic2, nav: "more", exportName: "DiktatView", load: () => import("./views/DiktatView") },
] as const satisfies readonly ControlRoute[];

/** Tab-Ids — aus der Tabelle abgeleitet, kein zweiter hand-gepflegter Union. */
export type ControlTab = (typeof CONTROL_ROUTES)[number]["id"];

/** Consumer-Typ eines Tabellen-Eintrags: wie ControlRoute, aber mit tab-
 *  getyptem `id`. CONTROL_ROUTES bleibt `as const` (für die ControlTab-
 *  Ableitung); Consumer lesen über die getypten Views unten, nicht über die
 *  rohe Literal-Union (deren Varianten optionale Felder nicht teilen). */
export interface ControlRouteEntry extends Omit<ControlRoute, "id"> {
  readonly id: ControlTab;
}

const routes: readonly ControlRouteEntry[] = CONTROL_ROUTES;

/** Alle lazy Tabs (alles außer dem eager inbox) — treibt die lazy()-Wrapper
 *  UND die <Route>-Map in ControlPage aus einer Struktur. */
export type LoadableControlRoute = ControlRouteEntry & { readonly load: () => Promise<unknown>; readonly exportName: string };
export const loadableRoutes: readonly LoadableControlRoute[] = routes.filter(
  (route): route is LoadableControlRoute => Boolean(route.load && route.exportName),
);

export const primaryRoutes: readonly ControlRouteEntry[] = routes.filter((route) => route.nav === "primary");
export const moreRoutes: readonly ControlRouteEntry[] = routes.filter((route) => route.nav === "more");

const routeById: Record<ControlTab, ControlRouteEntry> = Object.fromEntries(
  routes.map((route) => [route.id, route]),
) as Record<ControlTab, ControlRouteEntry>;

/** Navigation aus der Shell heraus: Tab → absolute Route. */
export const tabPath: Record<ControlTab, string> = Object.fromEntries(
  routes.map((route) => [route.id, route.path]),
) as Record<ControlTab, string>;

/** Hover/Fokus-Prefetch: lädt den Lazy-Chunk einer View, bevor der Klick kommt.
 *  Derselbe `load`-Importer wie beim lazy()-Wrapper — Vite dedupliziert den
 *  dynamischen Import, also idempotent & billig. Best-effort: ein Netzfehler
 *  hier darf nichts kaputt machen, der echte Klick lädt über Suspense erneut. */
export function prefetchControlView(tab: ControlTab): void {
  const load = routeById[tab]?.load;
  if (load) void load().catch(() => {});
}

/** Legacy-/Abriss-Routen (S5/S6): Redirect-only-Pfade, die auf einen lebenden
 *  Tab zeigen. Treibt activeFromPath — die <Navigate>-Routen selbst bleiben
 *  explizites JSX in ControlPage (QueryPreservingRedirect erhält Drawer-/
 *  Filter-Intent im Query-String, s. legacyControlRedirectTarget). */
const LEGACY_TAB_ALIASES: ReadonlyArray<{ match: string; tab: ControlTab }> = [
  // Flow/Ketten/Hermes wurden ins Fleet-Cockpit absorbiert (Phase 2 / S5).
  { match: "/control/flow", tab: "fleet" },
  { match: "/control/ketten", tab: "fleet" },
  { match: "/control/hermes", tab: "fleet" },
  // Puls/Pressure/Ops leben in der fusionierten System-View (S1/S5).
  { match: "/control/pulse", tab: "system" },
  { match: "/control/pressure", tab: "system" },
  { match: "/control/ops", tab: "system" },
  // Übersicht → Bibliothek (Vault-Provenienz zog dorthin um, S5).
  { match: "/control/overview", tab: "bibliothek" },
];

/** Aktiver Tab aus dem Pfad. Lebende Routen schlagen Aliase; Detail-Seiten
 *  ohne eigenen Tab (runs/:runId, issues) pinnen ihren Eltern-Tab; alles
 *  andere (inkl. /control und /control/inbox) ist die Start-Landing. */
export function activeFromPath(pathname: string): ControlTab {
  for (const route of routes) {
    if (route.segment && pathname.includes(route.path)) return route.id;
  }
  for (const alias of LEGACY_TAB_ALIASES) {
    if (pathname.includes(alias.match)) return alias.tab;
  }
  // Run-Timeline (F3) ist eine Detail-Seite der Runs-Liste in Workstreams —
  // Rail-Highlight bleibt dort, eigener Tab existiert bewusst nicht.
  if (pathname.includes("/control/runs/")) return "workstreams";
  // Issues (F6) ist eine Detail-Seite der Statistik — gleiche Tab-Ökonomie.
  if (pathname.includes("/control/issues")) return "statistik";
  return "inbox";
}
