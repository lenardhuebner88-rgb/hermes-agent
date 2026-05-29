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
  const active = activeFromPath(location.pathname);

  useEffect(() => {
    const onKey = (event: KeyboardEvent) => {
      const combo = `${event.metaKey ? "Meta+" : event.ctrlKey ? "Control+" : ""}${event.key.toLowerCase()}`;
      if (combo === "Meta+k" || combo === "Control+k") {
        event.preventDefault();
        setPaletteOpen((open) => !open);
      }
    };
    window.addEventListener("keydown", onKey);
    return () => window.removeEventListener("keydown", onKey);
  }, []);

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
          <Route index element={<OverviewView proposals={proposals.proposals} proposalsLoading={proposals.loading} proposalsError={proposals.error} agents={openclaw.data?.agents ?? []} />} />
          <Route path="overview" element={<OverviewView proposals={proposals.proposals} proposalsLoading={proposals.loading} proposalsError={proposals.error} agents={openclaw.data?.agents ?? []} />} />
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
