import { Bot, FlaskConical, LayoutDashboard, PanelLeft, Shield, Sparkles } from "lucide-react";
import { Button } from "@nous-research/ui/ui/components/button";
import { cn } from "@/lib/utils";
import { de } from "../i18n/de";
import type { Density } from "../hooks/useDensity";

export type ControlTab = "overview" | "hermes" | "openclaw" | "autoresearch";

const tabs: Array<{ id: ControlTab; label: string; path: string; icon: React.ComponentType<{ className?: string }> }> = [
  { id: "overview", label: de.tabs.overview, path: "/control", icon: LayoutDashboard },
  { id: "hermes", label: de.tabs.hermes, path: "/control/hermes", icon: Bot },
  { id: "openclaw", label: de.tabs.openclaw, path: "/control/openclaw", icon: Shield },
  { id: "autoresearch", label: de.tabs.autoresearch, path: "/control/autoresearch", icon: FlaskConical },
];

interface Props {
  active: ControlTab;
  density: Density;
  pinned: boolean;
  openProposals: number;
  children: React.ReactNode;
  onNavigate: (tab: ControlTab) => void;
  setDensity: (density: Density) => void;
  resetToAuto: () => void;
}

export function ControlShell(props: Props) {
  return props.density === "compact" ? <ShellCompact {...props} /> : <ShellAiry {...props} />;
}

function DensityControls({ density, pinned, setDensity, resetToAuto }: Pick<Props, "density" | "pinned" | "setDensity" | "resetToAuto">) {
  return (
    <div className="flex items-center gap-1 rounded-full border border-white/10 bg-black/20 p-1 text-xs">
      <Button size="xs" ghost={!pinned} onClick={resetToAuto}>{de.shell.auto}</Button>
      <Button size="xs" ghost={density !== "airy"} onClick={() => setDensity("airy")}>{de.shell.airy}</Button>
      <Button size="xs" ghost={density !== "compact"} onClick={() => setDensity("compact")}>{de.shell.compact}</Button>
    </div>
  );
}

function ShellAiry({ active, children, density, pinned, openProposals, onNavigate, setDensity, resetToAuto }: Props) {
  return (
    <div className="hc-page flex min-h-0 flex-col px-4 pb-[calc(5.5rem+env(safe-area-inset-bottom,0px))] pt-4 sm:px-6 lg:px-8">
      <header className="mb-4 flex items-start justify-between gap-3">
        <div><p className="hc-eyebrow">Operator Dashboard</p><h1 className="mt-1 text-2xl font-semibold tracking-normal text-white">Hermes Control</h1></div>
        <DensityControls density={density} pinned={pinned} setDensity={setDensity} resetToAuto={resetToAuto} />
      </header>
      <main className="mx-auto w-full max-w-6xl flex-1">{children}</main>
      <nav className="fixed bottom-0 left-0 right-0 z-40 border-t border-white/10 bg-black/85 px-2 pb-[env(safe-area-inset-bottom,0px)] backdrop-blur-xl lg:left-64">
        <div className="grid grid-cols-4">
          {tabs.map((tab) => <TabButton key={tab.id} tab={tab} active={active === tab.id} openProposals={openProposals} onClick={() => onNavigate(tab.id)} />)}
        </div>
      </nav>
    </div>
  );
}

function ShellCompact({ active, children, density, pinned, openProposals, onNavigate, setDensity, resetToAuto }: Props) {
  return (
    <div className="hc-page grid min-h-0 grid-cols-[72px_1fr] gap-0">
      <aside className="sticky top-0 flex h-[calc(100dvh-5rem)] flex-col items-center justify-between border-r border-[var(--hc-border)] bg-[var(--hc-rail)] px-2 py-4">
        <div className="flex flex-col gap-2">
          <div className="mb-2 grid h-11 w-11 place-items-center rounded-lg border border-[var(--hc-accent-border)] bg-[var(--hc-accent-wash)] text-[var(--hc-accent-text)]"><Sparkles className="h-5 w-5" /></div>
          {tabs.map((tab) => <RailButton key={tab.id} tab={tab} active={active === tab.id} openProposals={openProposals} onClick={() => onNavigate(tab.id)} />)}
        </div>
        <PanelLeft className="h-4 w-4 hc-dim" />
      </aside>
      <div className="min-w-0 px-6 py-5">
        <header className="mb-5 flex items-center justify-between gap-3">
          <div><p className="hc-eyebrow">Hermes Control</p><h1 className="mt-1 text-xl font-semibold text-white">{tabs.find((t) => t.id === active)?.label}</h1></div>
          <DensityControls density={density} pinned={pinned} setDensity={setDensity} resetToAuto={resetToAuto} />
        </header>
        <main>{children}</main>
      </div>
    </div>
  );
}

function TabButton({ tab, active, openProposals, onClick }: { tab: (typeof tabs)[number]; active: boolean; openProposals: number; onClick: () => void }) {
  const Icon = tab.icon;
  return (
    <button type="button" onClick={onClick} className={cn("hc-tab relative flex flex-col items-center justify-center gap-1 text-xs hc-soft", active && "text-[var(--hc-accent-text)]")}>
      <Icon className="h-5 w-5" />
      <span>{tab.label}</span>
      {tab.id === "autoresearch" && openProposals > 0 ? <span className="absolute right-5 top-2 rounded-full bg-[var(--hc-accent)] px-1.5 text-[10px] text-white">{openProposals}</span> : null}
    </button>
  );
}

function RailButton({ tab, active, openProposals, onClick }: { tab: (typeof tabs)[number]; active: boolean; openProposals: number; onClick: () => void }) {
  const Icon = tab.icon;
  return (
    <button type="button" title={tab.label} aria-label={tab.label} onClick={onClick} className={cn("relative grid h-11 w-11 place-items-center rounded-lg border border-transparent hc-soft", active && "border-[var(--hc-accent-border)] bg-[var(--hc-accent-wash)] text-[var(--hc-accent-text)]")}>
      <Icon className="h-5 w-5" />
      {tab.id === "autoresearch" && openProposals > 0 ? <span className="absolute -right-1 -top-1 rounded-full bg-[var(--hc-accent)] px-1.5 text-[10px] text-white">{openProposals}</span> : null}
    </button>
  );
}
