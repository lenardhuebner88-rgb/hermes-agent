import { Navigate, Route, Routes, useLocation, useNavigate } from "react-router-dom";
import "./styles/control-tokens.css";
import { useDensity } from "./hooks/useDensity";
import { useProposals } from "./hooks/useControlData";
import { ControlShell, type ControlTab } from "./components/ControlShell";
import { OverviewView } from "./views/OverviewView";
import { HermesFleet } from "./views/HermesFleet";
import { OpenClawPlaceholder } from "./views/OpenClawPlaceholder";
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
  const active = activeFromPath(location.pathname);

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
      >
        <Routes>
          <Route index element={<OverviewView proposals={proposals.proposals} />} />
          <Route path="hermes" element={<HermesFleet density={density.density} />} />
          <Route path="openclaw" element={<OpenClawPlaceholder />} />
          <Route path="autoresearch" element={<AutoresearchView density={density.density} store={proposals} />} />
          <Route path="*" element={<Navigate to="/control" replace />} />
        </Routes>
      </ControlShell>
    </div>
  );
}
