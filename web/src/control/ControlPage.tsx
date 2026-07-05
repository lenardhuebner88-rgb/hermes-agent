import { lazy, Suspense, useEffect, useRef, useState } from "react";
import { Navigate, Route, Routes, useLocation, useNavigate } from "react-router-dom";
import { legacyControlRedirectTarget } from "./navigation";
import { Spinner } from "@nous-research/ui/ui/components/spinner";
import "./styles/control-tokens.css";
import { useDensity } from "./hooks/useDensity";
import { useDecisionInbox, useHermesWorkers, useLibraryUnread, useProposals, useStrategistCount, useSystemHealth } from "./hooks/useControlData";
import { useLiveEvents } from "./hooks/useLiveEvents";
import { ControlShell, type ControlTab } from "./components/ControlShell";
import { CommandPalette } from "./components/CommandPalette";
import { ErrorBoundary } from "./components/ErrorBoundary";
import { OfflineStaleBanner } from "./components/OfflineStaleBanner";
import { RouteTransition } from "./components/primitives";
import { CommandHome } from "./views/CommandHome";

// The Decision-Inbox is the /control landing → keep it eager. Every other tab is
// lazy-loaded (its own chunk, fetched on first visit) so opening /control no
// longer ships all 10 views up front — FlowView + AutoresearchView are the
// fattest, and most visits never open them.
const FleetView = lazy(() =>
  import("./views/FleetView").then((m) => ({ default: m.FleetView })),
);
// AgentOpsView (Ströme) bleibt vorerst — der Worker-Health-/Fan-out-Launch-
// Snapshot ist noch nicht 1:1 in OrchestratorBacklogView gedeckt (S4-Rescue-D1).
// Löschung + Redirect hängen an S6/Phase 3.
const AgentOpsView = lazy(() =>
  import("./views/AgentOpsView").then((m) => ({ default: m.AgentOpsView })),
);
const AgentTerminalsView = lazy(() =>
  import("./views/AgentTerminalsView").then((m) => ({ default: m.AgentTerminalsView })),
);
const StatistikView = lazy(() =>
  import("./views/StatistikView").then((m) => ({ default: m.StatistikView })),
);
const AutoresearchView = lazy(() =>
  import("./views/AutoresearchView").then((m) => ({ default: m.AutoresearchView })),
);
const BacklogView = lazy(() =>
  import("./views/BacklogView").then((m) => ({ default: m.BacklogView })),
);
const OrchestratorBacklogView = lazy(() =>
  import("./views/OrchestratorBacklogView").then((m) => ({
    default: m.OrchestratorBacklogView,
  })),
);
const CronView = lazy(() =>
  import("./views/CronView").then((m) => ({ default: m.CronView })),
);
const LoopsView = lazy(() =>
  import("./views/LoopsView").then((m) => ({ default: m.LoopsView })),
);
const LanesView = lazy(() =>
  import("./views/LanesView").then((m) => ({ default: m.LanesView })),
);
const RunTimelineView = lazy(() =>
  import("./views/RunTimelineView").then((m) => ({ default: m.RunTimelineView })),
);
const IssuesView = lazy(() =>
  import("./views/IssuesView").then((m) => ({ default: m.IssuesView })),
);
const ResearchView = lazy(() =>
  import("./views/ResearchView").then((m) => ({ default: m.ResearchView })),
);
const BibliothekView = lazy(() =>
  import("./views/BibliothekView").then((m) => ({ default: m.BibliothekView })),
);
const SchmiedeView = lazy(() =>
  import("./views/SchmiedeView").then((m) => ({ default: m.SchmiedeView })),
);
const StrategistView = lazy(() =>
  import("./views/StrategistView").then((m) => ({ default: m.StrategistView })),
);
const SystemView = lazy(() =>
  import("./views/system/SystemView").then((m) => ({ default: m.SystemView })),
);

function activeFromPath(pathname: string): ControlTab {
  if (pathname.includes("/control/fleet")) return "fleet";
  if (pathname.includes("/control/overview")) return "overview";
  if (pathname.includes("/control/pulse")) return "system";
  if (pathname.includes("/control/workstreams")) return "workstreams";
  if (pathname.includes("/control/agent-terminals")) return "agentTerminals";
  // Flow/Ketten/Hermes wurden ins Fleet-Cockpit absorbiert (Phase 2).
  if (pathname.includes("/control/flow")) return "fleet";
  if (pathname.includes("/control/ketten")) return "fleet";
  if (pathname.includes("/control/statistik")) return "statistik";
  if (pathname.includes("/control/autoresearch")) return "autoresearch";
  if (pathname.includes("/control/backlog")) return "backlog";
  if (pathname.includes("/control/orchestrator")) return "orchestrator";
  if (pathname.includes("/control/crons")) return "crons";
  if (pathname.includes("/control/loops")) return "loops";
  if (pathname.includes("/control/lanes")) return "lanes";
  if (pathname.includes("/control/system")) return "system";
  if (pathname.includes("/control/pressure")) return "system";
  if (pathname.includes("/control/ops")) return "system";
  if (pathname.includes("/control/research")) return "research";
  if (pathname.includes("/control/bibliothek")) return "bibliothek";
  if (pathname.includes("/control/schmiede")) return "schmiede";
  if (pathname.includes("/control/stratege")) return "stratege";
  // Run-Timeline (F3) ist eine Detail-Seite der Runs-Liste in Workstreams —
  // Rail-Highlight bleibt dort, eigener Tab existiert bewusst nicht.
  if (pathname.includes("/control/runs/")) return "workstreams";
  // Issues (F6) ist eine Detail-Seite der Statistik — gleiche Tab-Ökonomie.
  if (pathname.includes("/control/issues")) return "statistik";
  // Root /control (and the legacy /control/inbox) is the Decision-Inbox landing.
  return "inbox";
}

// Hover/Fokus-Prefetch: lädt den Lazy-Chunk einer View, bevor der Klick kommt.
// Vite dedupliziert dynamische Imports desselben Moduls — idempotent & billig.
// Muss dieselben import()-Ziele treffen wie die lazy()-Wrapper oben.
const viewImporters: Partial<Record<ControlTab, () => Promise<unknown>>> = {
  fleet: () => import("./views/FleetView"),
  workstreams: () => import("./views/AgentOpsView"),
  agentTerminals: () => import("./views/AgentTerminalsView"),
  statistik: () => import("./views/StatistikView"),
  autoresearch: () => import("./views/AutoresearchView"),
  backlog: () => import("./views/BacklogView"),
  orchestrator: () => import("./views/OrchestratorBacklogView"),
  crons: () => import("./views/CronView"),
  loops: () => import("./views/LoopsView"),
  lanes: () => import("./views/LanesView"),
  system: () => import("./views/system/SystemView"),
  research: () => import("./views/ResearchView"),
  bibliothek: () => import("./views/BibliothekView"),
  schmiede: () => import("./views/SchmiedeView"),
  stratege: () => import("./views/StrategistView"),
};

function prefetchControlView(tab: ControlTab): void {
  // Prefetch ist best-effort — ein Netzfehler hier darf nichts kaputt machen;
  // der echte Klick lädt den Chunk über Suspense erneut.
  void viewImporters[tab]?.().catch(() => {});
}

const tabPath: Record<ControlTab, string> = {
  fleet: "/control/fleet",
  inbox: "/control",
  overview: "/control/overview",
  pulse: "/control/system",
  workstreams: "/control/workstreams",
  agentTerminals: "/control/agent-terminals",
  flow: "/control/fleet",
  ketten: "/control/fleet",
  statistik: "/control/statistik",
  autoresearch: "/control/autoresearch",
  backlog: "/control/backlog",
  orchestrator: "/control/orchestrator",
  crons: "/control/crons",
  loops: "/control/loops",
  lanes: "/control/lanes",
  system: "/control/system",
  pressure: "/control/system",
  ops: "/control/system",
  research: "/control/research",
  bibliothek: "/control/bibliothek",
  schmiede: "/control/schmiede",
  stratege: "/control/stratege",
};

// Shown briefly while a lazy-loaded control view chunk downloads (first visit
// of that tab only; the browser caches it afterwards).
function ControlViewFallback() {
  return (
    <div
      className="flex items-center justify-center py-16"
      aria-busy="true"
      aria-live="polite"
    >
      <Spinner />
    </div>
  );
}

function QueryPreservingRedirect({ to }: { to: string }) {
  const location = useLocation();
  return <Navigate to={legacyControlRedirectTarget(to, location.search)} replace />;
}

export default function ControlPage() {
  const density = useDensity();
  const navigate = useNavigate();
  const location = useLocation();
  const proposals = useProposals();
  const workers = useHermesWorkers();
  const inbox = useDecisionInbox();
  const health = useSystemHealth();
  const libraryUnread = useLibraryUnread();
  const strat = useStrategistCount();
  const [paletteOpen, setPaletteOpen] = useState(false);
  const commandButtonRef = useRef<HTMLButtonElement | null>(null);
  const gPendingRef = useRef<number>(0);
  const active = activeFromPath(location.pathname);
  useLiveEvents();

  useEffect(() => {
    const onKey = (event: KeyboardEvent) => {
      const target = event.target as HTMLElement | null;
      if (target?.closest("input,textarea,[contenteditable='true'],[role='dialog']")) return;
      const combo = `${event.metaKey ? "Meta+" : event.ctrlKey ? "Control+" : ""}${event.key.toLowerCase()}`;
      if (combo === "Meta+k" || combo === "Control+k") {
        event.preventDefault();
        setPaletteOpen((open) => !open);
        return;
      }
      if (event.metaKey || event.ctrlKey || event.altKey) return;
      // "/" springt in die Command-Palette (Schnell-Fokus/Suche).
      if (event.key === "/") {
        event.preventDefault();
        setPaletteOpen(true);
        return;
      }
      // Zwei-Tasten-Navigation "g <x>" (g f / g h / g p -> Fleet/System).
      const key = event.key.toLowerCase();
      const now = Date.now();
      if (gPendingRef.current && now - gPendingRef.current < 800) {
        const dest: Record<string, ControlTab> = { s: "workstreams", f: "fleet", h: "fleet", k: "fleet", t: "statistik", a: "autoresearch", b: "bibliothek", u: "overview", i: "inbox", p: "system", o: "system" };
        if (dest[key]) { event.preventDefault(); navigate(tabPath[dest[key]]); }
        gPendingRef.current = 0;
        return;
      }
      gPendingRef.current = key === "g" ? now : 0;
    };
    window.addEventListener("keydown", onKey);
    return () => window.removeEventListener("keydown", onKey);
  }, [navigate]);

  return (
    <div data-control>
      <OfflineStaleBanner health={health} />
      <ControlShell
        active={active}
        density={density.density}
        inbox={inbox}
        openProposals={proposals.openSkillProposals.length}
        inboxTotal={inbox.summary.total}
        inboxTone={inbox.worstTone}
        libraryUnread={active === "bibliothek" ? 0 : libraryUnread}
        strategistCount={strat.data?.count ?? 0}
        health={health}
        onNavigate={(tab) => navigate(tabPath[tab])}
        onPrefetch={prefetchControlView}
        commandButtonRef={commandButtonRef}
        onOpenCommand={() => setPaletteOpen(true)}
      >
        <RouteTransition pathname={active}>
          <ErrorBoundary>
          <Suspense fallback={<ControlViewFallback />}>
          <Routes location={location}>
            <Route index element={<CommandHome density={density.density} />} />
            <Route path="fleet" element={<FleetView />} />
            <Route path="inbox" element={<CommandHome density={density.density} />} />
            {/* Abriss S5: Übersicht → Bibliothek (Vault-Provenienz zog dorthin um). */}
            <Route path="overview" element={<QueryPreservingRedirect to="/control/bibliothek" />} />
            {/* Abriss S5: Puls → System (48h-Puls lebt in der fusionierten System-View). */}
            <Route path="pulse" element={<QueryPreservingRedirect to="/control/system" />} />
            <Route path="workstreams" element={<AgentOpsView density={density.density} />} />
            <Route path="agent-terminals" element={<AgentTerminalsView />} />
            {/* hermes wurde in Fleet absorbiert (Phase 2) */}
            <Route path="hermes" element={<QueryPreservingRedirect to="/control/fleet" />} />
            <Route path="statistik" element={<StatistikView />} />
            {/* Abriss S5: Flow → Fleet (Board/Task-Steuerung/Kette-starten zogen ins Fleet-Cockpit). */}
            <Route path="flow" element={<QueryPreservingRedirect to="/control/fleet" />} />
            {/* Abriss S5: Ketten → Fleet (Ketten-Subtab: Kosten, Cancel-Chain, Graph). */}
            <Route path="ketten" element={<QueryPreservingRedirect to="/control/fleet" />} />
            <Route path="autoresearch" element={<AutoresearchView density={density.density} store={proposals} />} />
            <Route path="backlog" element={<BacklogView density={density.density} />} />
            <Route path="orchestrator" element={<OrchestratorBacklogView density={density.density} />} />
            <Route path="crons" element={<CronView density={density.density} />} />
            <Route path="loops" element={<LoopsView />} />
            <Route path="lanes" element={<LanesView density={density.density} />} />
            <Route path="system" element={<SystemView proposals={proposals.proposals} proposalsLastUpdated={proposals.lastUpdated} />} />
            {/* Abriss S5: Pressure/Ops → System (Content in die fusionierte System-View evakuiert). */}
            <Route path="pressure" element={<QueryPreservingRedirect to="/control/system" />} />
            <Route path="ops" element={<QueryPreservingRedirect to="/control/system" />} />
            <Route path="runs/:runId" element={<RunTimelineView density={density.density} />} />
            <Route path="issues" element={<IssuesView density={density.density} />} />
            <Route path="research" element={<ResearchView density={density.density} />} />
            <Route path="bibliothek" element={<BibliothekView density={density.density} />} />
            <Route path="schmiede" element={<SchmiedeView density={density.density} />} />
            <Route path="stratege" element={<StrategistView density={density.density} />} />
            <Route path="*" element={<Navigate to="/control" replace />} />
          </Routes>
          </Suspense>
          </ErrorBoundary>
        </RouteTransition>
      </ControlShell>
      <CommandPalette
        open={paletteOpen}
        workers={workers.data?.workers ?? []}
        onClose={() => setPaletteOpen(false)}
        onNavigate={(path) => navigate(path)}
        onGenerate={proposals.generate}
        onApplyAll={proposals.applyAll}
        triggerRef={commandButtonRef}
      />
    </div>
  );
}
