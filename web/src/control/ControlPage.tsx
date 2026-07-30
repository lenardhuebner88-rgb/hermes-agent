import { lazy, Suspense, useEffect, useRef, useState, type ComponentType, type ReactNode } from "react";
import { Navigate, Route, Routes, useLocation, useNavigate } from "react-router-dom";
import { LazyMotion, domAnimation } from "motion/react";
import { activeFromPath, legacyControlRedirectTarget, loadableRoutes, prefetchControlView, tabPath, type ControlTab } from "./navigation";
import { Spinner } from "@nous-research/ui/ui/components/spinner";
import "./styles/control-tokens.css";
import { useDensity } from "./hooks/useDensity";
import { useDecisionInbox } from "./hooks/decisionInbox";
import { useHermesRunsCosts } from "./hooks/costsUsage";
import { useHermesWorkers } from "./hooks/workersBoard";
import { useLibraryUnread } from "./hooks/libraryKnowledge";
import { useProposals } from "./hooks/proposalsDeepAudit";
import { useStrategistCount } from "./hooks/strategist";
import { HEALTH_POLL_INTERVAL_MS, useSystemHealth } from "./hooks/systemReleaseHealth";
import { useLiveEvents } from "./hooks/useLiveEvents";
import { costDisplayValue } from "./lib/fleetHub";
import { ControlShell } from "./components/ControlShell";
import { CommandPalette } from "./components/CommandPalette";
import { ErrorBoundary } from "./components/ErrorBoundary";
import { OfflineStaleBanner } from "./components/OfflineStaleBanner";
import { RouteTransition } from "./components/primitives";
import { CommandHome } from "./views/CommandHome";
import { StartMissionControl } from "./views/start/StartMissionControl";

// Start and the Decision-Inbox are eager. Every other tab is lazy-loaded (its
// own chunk, fetched on first visit) so opening /control no longer ships all
// views up front. Die lazy()-Wrapper kommen aus DER EINEN Route-Tabelle in
// ./navigation (UI/UX-Iteration 3) — `load` ist derselbe Importer, den auch
// der Hover/Touch-Prefetch nutzt, Vite dedupliziert den Chunk. Einzige
// Ausnahmen: Detail-/Fallback-Routen ohne eigenen Tab (Runs-Timeline, Issues,
// Design-Board-Card, Projekte-Klassisch) — kein Nav-Eintrag, daher bewusst
// außerhalb der Tabelle.
type AnyViewProps = Record<string, unknown>;
const lazyViews: Partial<Record<ControlTab, ComponentType<AnyViewProps>>> = Object.fromEntries(
  loadableRoutes.map((route) => [
    route.id,
    lazy(() =>
      route.load().then((module) => ({
        default: (module as Record<string, ComponentType<AnyViewProps>>)[route.exportName],
      })),
    ),
  ]),
);

// AgentOpsView (Ströme) bleibt vorerst — der Worker-Health-/Fan-out-Launch-
// Snapshot ist noch nicht 1:1 in OrchestratorBacklogView gedeckt (S4-Rescue-D1).
// Löschung + Redirect hängen an S6/Phase 3.
const RunTimelineView = lazy(() =>
  import("./views/RunTimelineView").then((m) => ({ default: m.RunTimelineView })),
);
const IssuesView = lazy(() =>
  import("./views/IssuesView").then((m) => ({ default: m.IssuesView })),
);
const DesignBoardCardDetail = lazy(() =>
  import("./views/designboard/CardDetail").then((m) => ({ default: m.CardDetail })),
);
// Klassik-Fallback (Sprint 1 Karte e): die bisherige ProjekteView bleibt ohne
// Inhaltsänderung erreichbar, bis S2/S3 migrieren — /control/projekte selbst
// ist die Jarvis-Zone (Tab "projekte" in der Route-Tabelle).
const ProjekteView = lazy(() =>
  import("./views/ProjekteView").then((m) => ({ default: m.ProjekteView })),
);

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
  // Puls-Leiste (W2-b): poll-key "runs/costs" dedupes with FleetView — zero
  // new network requests, only a new subscriber to the shared polling store.
  const runsCosts = useHermesRunsCosts();
  const inbox = useDecisionInbox();
  const health = useSystemHealth();
  const libraryUnread = useLibraryUnread();
  const strat = useStrategistCount();
  const [paletteOpen, setPaletteOpen] = useState(false);
  const commandButtonRef = useRef<HTMLButtonElement | null>(null);
  const gPendingRef = useRef<number>(0);
  const active = activeFromPath(location.pathname);
  const isChromelessJarvis = /^\/control\/projekte\/?$/.test(location.pathname);
  useLiveEvents();

  // Puls-Leiste data contract (SHELL-SPEC.md W2-b): WORKER = running-filtered
  // count (fleetHub.deriveKpi's `aktiv` definition, NOT CommandHome's raw
  // liveWorkers.length); KOSTEN = actual_cost_usd, falls back to the marked
  // equivalent (fleetHub.costDisplayValue — same rule as Fleet's KPI panel).
  const pulseWorkers = workers.data ? workers.data.workers.filter((w) => w.run_status === "running").length : null;
  const pulseKosten = costDisplayValue(runsCosts.data?.today.actual_cost_usd, runsCosts.data?.today.cost_usd_equivalent);

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
        const dest: Record<string, ControlTab> = { s: "workstreams", f: "fleet", h: "fleet", k: "fleet", t: "statistik", a: "autoresearch", b: "bibliothek", u: "bibliothek", i: "inbox", p: "system", o: "system" };
        if (dest[key]) { event.preventDefault(); navigate(tabPath[dest[key]]); }
        gPendingRef.current = 0;
        return;
      }
      gPendingRef.current = key === "g" ? now : 0;
    };
    window.addEventListener("keydown", onKey);
    return () => window.removeEventListener("keydown", onKey);
  }, [navigate]);

  // Route-Elemente aus der einen Route-Tabelle: `lazyViews` liefert die
  // Lazy-Wrapper, hier kommen nur die Hook-Props der wenigen Views drauf,
  // die welche brauchen. inbox ist eager (StartMissionControl/CommandHome)
  // und steht explizit im <Routes>-Block, nicht in dieser Map.
  const viewElement = (id: ControlTab): ReactNode => {
    const View = lazyViews[id];
    if (!View) return null; // Defensiv: loadableRoutes deckt alle lazy Tabs ab.
    switch (id) {
      case "workstreams":
      case "backlog":
      case "orchestrator":
      case "crons":
      case "lanes":
      case "research":
      case "bibliothek":
      case "designBoard":
      case "schmiede":
      case "stratege":
        return <View density={density.density} />;
      case "autoresearch":
        return <View density={density.density} store={proposals} />;
      case "system":
        return <View proposals={proposals.proposals} proposalsLastUpdated={proposals.lastUpdated} />;
      default:
        return <View />;
    }
  };

  const routedContent = (
    <RouteTransition pathname={active}>
      <ErrorBoundary>
      <Suspense fallback={<ControlViewFallback />}>
      <Routes location={location}>
        <Route index element={<StartMissionControl density={density.density} />} />
        <Route path="inbox" element={<CommandHome density={density.density} />} />
        {/* Abriss S5: Übersicht → Bibliothek (Vault-Provenienz zog dorthin um). */}
        <Route path="overview" element={<QueryPreservingRedirect to="/control/bibliothek" />} />
        {/* Abriss S5: Puls → System (48h-Puls lebt in der fusionierten System-View). */}
        <Route path="pulse" element={<QueryPreservingRedirect to="/control/system" />} />
        {/* hermes wurde in Fleet absorbiert (Phase 2) */}
        <Route path="hermes" element={<QueryPreservingRedirect to="/control/fleet" />} />
        {/* Abriss S5: Flow → Fleet (Board/Task-Steuerung/Kette-starten zogen ins Fleet-Cockpit). */}
        <Route path="flow" element={<QueryPreservingRedirect to="/control/fleet" />} />
        {/* Abriss S5: Ketten → Fleet (Ketten-Subtab: Kosten, Cancel-Chain, Graph). */}
        <Route path="ketten" element={<QueryPreservingRedirect to="/control/fleet" />} />
        {/* Abriss S5: Pressure/Ops → System (Content in die fusionierte System-View evakuiert). */}
        <Route path="pressure" element={<QueryPreservingRedirect to="/control/system" />} />
        <Route path="ops" element={<QueryPreservingRedirect to="/control/system" />} />
        {/* Lebende Tabs: Pfad + Lazy-View aus der Route-Tabelle (./navigation). */}
        {loadableRoutes.map((route) => (
          <Route key={route.id} path={route.segment} element={viewElement(route.id)} />
        ))}
        {/* Klassik-Fallback (Sprint 1 Karte e): bisheriger Projekte-Tab
            bleibt ohne Inhaltsänderung erreichbar, bis S2/S3 migrieren. */}
        <Route path="projekte-klassisch" element={<ProjekteView />} />
        <Route path="runs/:runId" element={<RunTimelineView density={density.density} />} />
        <Route path="issues" element={<IssuesView density={density.density} />} />
        <Route path="design-board/:cardId" element={<DesignBoardCardDetail density={density.density} />} />
        <Route path="*" element={<Navigate to="/control" replace />} />
      </Routes>
      </Suspense>
      </ErrorBoundary>
    </RouteTransition>
  );

  return (
    <LazyMotion features={domAnimation} strict>
      <div data-control>
        <OfflineStaleBanner health={{ ...health, pollIntervalMs: HEALTH_POLL_INTERVAL_MS }} />
        {isChromelessJarvis ? (
          <div className="hc-chromeless -mx-3 -mt-2 sm:-mx-6 sm:-mt-4 lg:-mt-6">
            {routedContent}
          </div>
        ) : (
          <ControlShell
            active={active}
            density={density.density}
            inbox={inbox}
            openProposals={proposals.openSkillProposals.length}
            inboxTotal={inbox.summary.total}
            inboxTone={inbox.worstTone}
            libraryUnread={active === "bibliothek" ? 0 : libraryUnread}
            // Ehrlichkeits-Regel (Canon: unknown bleibt unknown, nie 0): der
            // Badge zählt erst nach erfolgreichem Load — ladend/fehlgeschlagen
            // rendern KEINEN Zähler, nie 0-as-"keine Vorschläge".
            strategistCount={strat.data?.count ?? null}
            health={health}
            pulse={{ workers: pulseWorkers, fragen: inbox.summary.total, fragenTone: inbox.worstTone, kostenUsd: pulseKosten.value, kostenIsEquivalent: pulseKosten.isEquivalent }}
            onNavigate={(tab) => navigate(tabPath[tab])}
            onPrefetch={prefetchControlView}
            commandButtonRef={commandButtonRef}
            onOpenCommand={() => setPaletteOpen(true)}
          >
            {routedContent}
          </ControlShell>
        )}
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
    </LazyMotion>
  );
}
