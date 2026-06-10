import { lazy, Suspense, useEffect, useRef, useState } from "react";
import { Navigate, Route, Routes, useLocation, useNavigate } from "react-router-dom";
import { Spinner } from "@nous-research/ui/ui/components/spinner";
import "./styles/control-tokens.css";
import { useDensity } from "./hooks/useDensity";
import { useDecisionInbox, useHermesWorkers, useProposals } from "./hooks/useControlData";
import { ControlShell, type ControlTab } from "./components/ControlShell";
import { CommandPalette } from "./components/CommandPalette";
import { RouteTransition } from "./components/primitives";
import { InboxView } from "./views/InboxView";

// The Decision-Inbox is the /control landing → keep it eager. Every other tab is
// lazy-loaded (its own chunk, fetched on first visit) so opening /control no
// longer ships all 10 views up front — FlowView + AutoresearchView are the
// fattest, and most visits never open them.
const OverviewView = lazy(() =>
  import("./views/OverviewView").then((m) => ({ default: m.OverviewView })),
);
const PulseView = lazy(() =>
  import("./views/PulseView").then((m) => ({ default: m.PulseView })),
);
const AgentOpsView = lazy(() =>
  import("./views/AgentOpsView").then((m) => ({ default: m.AgentOpsView })),
);
const HermesFleet = lazy(() =>
  import("./views/HermesFleet").then((m) => ({ default: m.HermesFleet })),
);
const FlowView = lazy(() =>
  import("./views/FlowView").then((m) => ({ default: m.FlowView })),
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

function activeFromPath(pathname: string): ControlTab {
  if (pathname.includes("/control/overview")) return "overview";
  if (pathname.includes("/control/pulse")) return "pulse";
  if (pathname.includes("/control/workstreams")) return "workstreams";
  if (pathname.includes("/control/hermes")) return "hermes";
  if (pathname.includes("/control/flow")) return "flow";
  if (pathname.includes("/control/autoresearch")) return "autoresearch";
  if (pathname.includes("/control/backlog")) return "backlog";
  if (pathname.includes("/control/orchestrator")) return "orchestrator";
  if (pathname.includes("/control/crons")) return "crons";
  // Root /control (and the legacy /control/inbox) is the Decision-Inbox landing.
  return "inbox";
}

const tabPath: Record<ControlTab, string> = {
  inbox: "/control",
  overview: "/control/overview",
  pulse: "/control/pulse",
  workstreams: "/control/workstreams",
  hermes: "/control/hermes",
  flow: "/control/flow",
  autoresearch: "/control/autoresearch",
  backlog: "/control/backlog",
  orchestrator: "/control/orchestrator",
  crons: "/control/crons",
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

export default function ControlPage() {
  const density = useDensity();
  const navigate = useNavigate();
  const location = useLocation();
  const proposals = useProposals();
  const workers = useHermesWorkers();
  const inbox = useDecisionInbox();
  const [paletteOpen, setPaletteOpen] = useState(false);
  const commandButtonRef = useRef<HTMLButtonElement | null>(null);
  const gPendingRef = useRef<number>(0);
  const active = activeFromPath(location.pathname);

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
      // Zwei-Tasten-Navigation "g <x>" (g s / g h / g a / g u).
      const key = event.key.toLowerCase();
      const now = Date.now();
      if (gPendingRef.current && now - gPendingRef.current < 800) {
        const dest: Record<string, ControlTab> = { s: "workstreams", h: "hermes", a: "autoresearch", u: "overview", i: "inbox", p: "pulse" };
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
      <ControlShell
        active={active}
        density={density.density}
        openProposals={proposals.openSkillProposals.length}
        inboxTotal={inbox.summary.total}
        inboxTone={inbox.worstTone}
        onNavigate={(tab) => navigate(tabPath[tab])}
        commandButtonRef={commandButtonRef}
        onOpenCommand={() => setPaletteOpen(true)}
      >
        <RouteTransition pathname={active}>
          <Suspense fallback={<ControlViewFallback />}>
          <Routes location={location}>
            <Route index element={<InboxView density={density.density} />} />
            <Route path="inbox" element={<InboxView density={density.density} />} />
            <Route path="overview" element={<OverviewView proposals={proposals.proposals} proposalsLoading={proposals.loading} proposalsError={proposals.error} proposalsLastUpdated={proposals.lastUpdated} />} />
            <Route path="pulse" element={<PulseView proposals={proposals.proposals} proposalsLastUpdated={proposals.lastUpdated} />} />
            <Route path="workstreams" element={<AgentOpsView density={density.density} />} />
            <Route path="hermes" element={<HermesFleet density={density.density} />} />
            <Route path="flow" element={<FlowView />} />
            <Route path="autoresearch" element={<AutoresearchView density={density.density} store={proposals} />} />
            <Route path="backlog" element={<BacklogView density={density.density} />} />
            <Route path="orchestrator" element={<OrchestratorBacklogView density={density.density} />} />
            <Route path="crons" element={<CronView density={density.density} />} />
            <Route path="*" element={<Navigate to="/control" replace />} />
          </Routes>
          </Suspense>
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
