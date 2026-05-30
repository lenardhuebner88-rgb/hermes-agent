import { useEffect, useRef, useState } from "react";
import { Navigate, Route, Routes, useLocation, useNavigate } from "react-router-dom";
import "./styles/control-tokens.css";
import { useDensity } from "./hooks/useDensity";
import { useHermesWorkers, useOpenClawAgents, useProposals } from "./hooks/useControlData";
import { ControlShell, type ControlTab } from "./components/ControlShell";
import { CommandPalette } from "./components/CommandPalette";
import { OverviewView } from "./views/OverviewView";
import { HermesFleet } from "./views/HermesFleet";
import { OpenClawFleet } from "./views/OpenClawFleet";
import { AutoresearchView } from "./views/AutoresearchView";

function activeFromPath(pathname: string): ControlTab {
  if (pathname.includes("/control/hermes")) return "hermes";
  if (pathname.includes("/control/openclaw")) return "openclaw";
  if (pathname.includes("/control/autoresearch")) return "autoresearch";
  return "overview";
}

const tabPath: Record<ControlTab, string> = {
  overview: "/control",
  hermes: "/control/hermes",
  openclaw: "/control/openclaw",
  autoresearch: "/control/autoresearch",
};

export default function ControlPage() {
  const density = useDensity();
  const navigate = useNavigate();
  const location = useLocation();
  const proposals = useProposals();
  const openclaw = useOpenClawAgents();
  const workers = useHermesWorkers();
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
      // Zwei-Tasten-Navigation "g <x>" (g h / g a / g o / g u).
      const key = event.key.toLowerCase();
      const now = Date.now();
      if (gPendingRef.current && now - gPendingRef.current < 800) {
        const dest: Record<string, ControlTab> = { h: "hermes", a: "autoresearch", o: "openclaw", u: "overview" };
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
        pinned={density.pinned}
        openProposals={proposals.openSkillProposals.length}
        onNavigate={(tab) => navigate(tabPath[tab])}
        setDensity={density.setDensity}
        resetToAuto={density.resetToAuto}
        commandButtonRef={commandButtonRef}
        onOpenCommand={() => setPaletteOpen(true)}
      >
        <Routes>
          <Route index element={<OverviewView proposals={proposals.proposals} proposalsLoading={proposals.loading} proposalsError={proposals.error} proposalsLastUpdated={proposals.lastUpdated} agents={openclaw.data?.agents ?? []} agentsLastUpdated={openclaw.lastUpdated} agentsError={openclaw.data?.error ?? openclaw.error} />} />
          <Route path="overview" element={<OverviewView proposals={proposals.proposals} proposalsLoading={proposals.loading} proposalsError={proposals.error} proposalsLastUpdated={proposals.lastUpdated} agents={openclaw.data?.agents ?? []} agentsLastUpdated={openclaw.lastUpdated} agentsError={openclaw.data?.error ?? openclaw.error} />} />
          <Route path="hermes" element={<HermesFleet density={density.density} />} />
          <Route path="openclaw" element={<OpenClawFleet density={density.density} />} />
          <Route path="autoresearch" element={<AutoresearchView density={density.density} store={proposals} />} />
          <Route path="*" element={<Navigate to="/control" replace />} />
        </Routes>
      </ControlShell>
      <CommandPalette
        open={paletteOpen}
        workers={workers.data?.workers ?? []}
        agents={openclaw.data?.agents ?? []}
        onClose={() => setPaletteOpen(false)}
        onNavigate={(path) => navigate(path)}
        onGenerate={proposals.generate}
        onApplyAll={proposals.applyAll}
        triggerRef={commandButtonRef}
      />
    </div>
  );
}
