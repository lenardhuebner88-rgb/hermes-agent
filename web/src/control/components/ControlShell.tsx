import { Bot, Command, FlaskConical, KanbanSquare, LayoutDashboard, MessageSquare, MoreHorizontal, PanelLeft, Settings, Shield, Sparkles, Workflow } from "lucide-react";
import { Button } from "@nous-research/ui/ui/components/button";
import { Link } from "react-router-dom";
import { cn } from "@/lib/utils";
import { de } from "../i18n/de";
import type { Density } from "../hooks/useDensity";

export type ControlTab = "overview" | "hermes" | "autoresearch" | "backlog" | "orchestrator";

const tabs: Array<{ id: ControlTab; label: string; path: string; icon: React.ComponentType<{ className?: string }> }> = [
  { id: "overview", label: de.tabs.overview, path: "/control", icon: LayoutDashboard },
  { id: "hermes", label: de.tabs.hermes, path: "/control/hermes", icon: Bot },
  { id: "autoresearch", label: de.tabs.autoresearch, path: "/control/autoresearch", icon: FlaskConical },
  { id: "backlog", label: de.tabs.backlog, path: "/control/backlog", icon: KanbanSquare },
  { id: "orchestrator", label: de.tabs.orchestrator, path: "/control/orchestrator", icon: Workflow },
];

const secondaryNav = [
  { label: "Sessions", path: "/sessions", icon: MessageSquare },
  { label: "Kanban", path: "/plugins", icon: LayoutDashboard },
  { label: "Modelle", path: "/models", icon: Shield },
  { label: "Logs", path: "/logs", icon: PanelLeft },
  { label: "Cron", path: "/cron", icon: Sparkles },
  { label: "Skills", path: "/skills", icon: Bot },
  { label: "Konfig", path: "/config", icon: Settings },
];

interface Props {
  active: ControlTab;
  density: Density;
  pinned: boolean;
  openProposals: number;
  commandButtonRef?: React.RefObject<HTMLButtonElement | null>;
  onOpenCommand: () => void;
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

function ShellAiry({ active, children, density, pinned, openProposals, onNavigate, setDensity, resetToAuto, commandButtonRef, onOpenCommand }: Props) {
  return (
    <div className="hc-page flex min-h-0 flex-col px-4 pb-[calc(5.5rem+env(safe-area-inset-bottom,0px))] pt-4 sm:px-6 lg:px-8">
      <header className="mb-4 flex flex-col gap-3 lg:flex-row lg:items-start lg:justify-between">
        <div><p className="hc-eyebrow">Operator Dashboard</p><h1 className="mt-1 text-2xl font-semibold tracking-normal text-white">Hermes Control</h1></div>
        <div className="flex flex-wrap justify-end gap-2"><CommandButton buttonRef={commandButtonRef} onOpen={onOpenCommand} /><MoreNav /><div className="flex flex-wrap items-center justify-end gap-2"><StatusDots /><DensityControls density={density} pinned={pinned} setDensity={setDensity} resetToAuto={resetToAuto} /></div></div>
        <DesktopTabs active={active} openProposals={openProposals} onNavigate={onNavigate} />
      </header>
      <main className="mx-auto w-full max-w-6xl flex-1">{children}</main>
      <nav className="fixed bottom-0 left-0 right-0 z-40 border-t lg:hidden border-white/10 bg-black/85 px-2 pb-[env(safe-area-inset-bottom,0px)] backdrop-blur-xl lg:left-64">
        <div className="grid grid-cols-5">
          {tabs.map((tab) => <TabButton key={tab.id} tab={tab} active={active === tab.id} openProposals={openProposals} onClick={() => onNavigate(tab.id)} />)}
        </div>
      </nav>
    </div>
  );
}

function ShellCompact({ active, children, density, pinned, openProposals, onNavigate, setDensity, resetToAuto, commandButtonRef, onOpenCommand }: Props) {
  return (
    <div className="hc-page grid min-h-0 grid-cols-[72px_1fr] gap-0">
      <aside className="sticky top-0 flex h-[calc(100dvh-5rem)] flex-col items-center justify-between border-r border-[var(--hc-border)] bg-[var(--hc-rail)] px-2 py-4">
        <div className="flex flex-col gap-2">
          <div className="mb-2 grid h-11 w-11 place-items-center rounded-lg border border-[var(--hc-accent-border)] bg-[var(--hc-accent-wash)] text-[var(--hc-accent-text)]"><Sparkles className="h-5 w-5" /></div>
          {tabs.map((tab) => <RailButton key={tab.id} tab={tab} active={active === tab.id} openProposals={openProposals} onClick={() => onNavigate(tab.id)} />)}
          <button ref={commandButtonRef} type="button" title="Command Palette" aria-label="Command Palette" onClick={onOpenCommand} className="grid h-11 w-11 place-items-center rounded-lg border border-transparent hc-soft hover:border-[var(--hc-accent-border)] hover:bg-[var(--hc-accent-wash)]"><Command className="h-5 w-5" /></button>
          <RailMoreNav />
        </div>
        <PanelLeft className="h-4 w-4 hc-dim" />
      </aside>
      <div className="min-w-0 px-6 py-5">
        <header className="mb-5 flex items-center justify-between gap-3">
          <div><p className="hc-eyebrow">Hermes Control</p><h1 className="mt-1 text-xl font-semibold text-white">{tabs.find((t) => t.id === active)?.label}</h1></div>
          <div className="flex flex-wrap items-center justify-end gap-2"><StatusDots /><DensityControls density={density} pinned={pinned} setDensity={setDensity} resetToAuto={resetToAuto} /></div>
        </header>
        <main>{children}</main>
      </div>
    </div>
  );
}


function DesktopTabs({ active, openProposals, onNavigate }: { active: ControlTab; openProposals: number; onNavigate: (tab: ControlTab) => void }) {
  return (
    <nav className="hidden w-full flex-wrap gap-2 lg:flex">
      {tabs.map((tab) => {
        const Icon = tab.icon;
        return (
          <button key={tab.id} type="button" onClick={() => onNavigate(tab.id)} className={cn("relative inline-flex min-h-10 items-center gap-2 rounded-lg border border-white/10 px-3 text-sm hc-soft", active === tab.id && "border-[var(--hc-accent-border)] bg-[var(--hc-accent-wash)] text-[var(--hc-accent-text)]")}>
            <Icon className="h-4 w-4" />{tab.label}
            {tab.id === "autoresearch" && openProposals > 0 ? <span className="rounded-full bg-[var(--hc-accent)] px-1.5 text-[10px] text-white">{openProposals}</span> : null}
          </button>
        );
      })}
    </nav>
  );
}

function CommandButton({ buttonRef, onOpen }: { buttonRef?: React.RefObject<HTMLButtonElement | null>; onOpen: () => void }) {
  return <button ref={buttonRef} type="button" className="hc-hit inline-flex items-center gap-2 rounded-lg border border-white/10 px-3 text-sm hc-soft hover:bg-white/5" onClick={onOpen}><Command className="h-4 w-4" />⌘K</button>;
}

function MoreNav() {
  return (
    <details className="relative">
      <summary className="flex min-h-11 cursor-pointer list-none items-center gap-2 rounded-lg border border-white/10 px-3 text-sm hc-soft hover:bg-white/5"><MoreHorizontal className="h-4 w-4" />Mehr</summary>
      <div className="absolute right-0 top-12 z-50 w-52 rounded-lg border border-[var(--hc-border)] bg-[var(--hc-panel)] p-2 shadow-xl">
        {secondaryNav.map((item) => { const Icon = item.icon; return <Link key={item.path} to={item.path} className="flex min-h-11 items-center gap-2 rounded-md px-3 text-sm hc-soft hover:bg-white/5 hover:text-white"><Icon className="h-4 w-4" />{item.label}</Link>; })}
      </div>
    </details>
  );
}

function RailMoreNav() {
  return (
    <div className="group relative">
      <button type="button" title="Mehr" aria-label="Mehr" className="grid h-11 w-11 place-items-center rounded-lg border border-transparent hc-soft hover:border-[var(--hc-accent-border)] hover:bg-[var(--hc-accent-wash)]"><MoreHorizontal className="h-5 w-5" /></button>
      <div className="invisible absolute left-12 top-0 z-50 w-52 rounded-lg border border-[var(--hc-border)] bg-[var(--hc-panel)] p-2 opacity-0 shadow-xl transition group-hover:visible group-hover:opacity-100 group-focus-within:visible group-focus-within:opacity-100">
        {secondaryNav.map((item) => { const Icon = item.icon; return <Link key={item.path} to={item.path} className="flex min-h-11 items-center gap-2 rounded-md px-3 text-sm hc-soft hover:bg-white/5 hover:text-white"><Icon className="h-4 w-4" />{item.label}</Link>; })}
      </div>
    </div>
  );
}

function StatusDots() {
  return <div className="hidden items-center gap-2 rounded-full border border-white/10 bg-black/20 px-3 py-2 text-xs hc-soft md:flex"><span className="hc-led hc-led-live h-2 w-2 rounded-full" />Live<span className="hc-mono">9119</span><span className="hc-led hc-led-ready h-2 w-2 rounded-full" />MC</div>;
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
