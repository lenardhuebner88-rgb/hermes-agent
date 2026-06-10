import { Activity, ChartSpline, Clock, Columns3, Command, FlaskConical, GitBranch, KanbanSquare, LayoutDashboard, MessageSquare, MoreHorizontal, PanelLeft, Settings, Shield, Sparkles, Workflow } from "lucide-react";
import { Link } from "react-router-dom";
import { cn } from "@/lib/utils";
import { de } from "../i18n/de";
import type { Density } from "../hooks/useDensity";
import type { ToneName } from "../lib/types";

export type ControlTab = "overview" | "inbox" | "pulse" | "workstreams" | "flow" | "statistik" | "autoresearch" | "backlog" | "orchestrator" | "crons";

// The daily spine — 4 tabs. Start (the Command cockpit: needs-me + fleet +
// health), Flow (the live work board, absorbs the fleet), Statistik (charts:
// throughput / burn / cycle-time / reliability), Autoresearch (the
// self-improvement console). Everything else is a re-slice of these and
// lives in the "Mehr" overflow (moreTabs) below.
const tabs: Array<{ id: ControlTab; label: string; mobileLabel: string; path: string; icon: React.ComponentType<{ className?: string }> }> = [
  { id: "inbox", label: "Start", mobileLabel: "Start", path: "/control", icon: LayoutDashboard },
  { id: "flow", label: de.tabs.flow, mobileLabel: "Flow", path: "/control/flow", icon: Columns3 },
  { id: "statistik", label: de.tabs.statistik, mobileLabel: "Stats", path: "/control/statistik", icon: ChartSpline },
  { id: "autoresearch", label: de.tabs.autoresearch, mobileLabel: "Auto", path: "/control/autoresearch", icon: FlaskConical },
];

// Demoted control surfaces — still routed + reachable, just not in the primary
// rail/bottom-bar. The Command home already surfaces their headline signal.
const moreTabs = [
  { label: de.tabs.overview, path: "/control/overview", icon: Activity },
  { label: de.tabs.pulse, path: "/control/pulse", icon: Activity },
  { label: de.tabs.workstreams, path: "/control/workstreams", icon: GitBranch },
  { label: de.tabs.backlog, path: "/control/backlog", icon: KanbanSquare },
  { label: de.tabs.orchestrator, path: "/control/orchestrator", icon: Workflow },
  { label: de.tabs.crons, path: "/control/crons", icon: Clock },
];

const secondaryNav = [
  { label: "Sessions", path: "/sessions", icon: MessageSquare },
  { label: "Kanban", path: "/plugins", icon: KanbanSquare },
  { label: "Modelle", path: "/models", icon: Shield },
  { label: "Logs", path: "/logs", icon: PanelLeft },
  { label: "Skills", path: "/skills", icon: Sparkles },
  { label: "Konfig", path: "/config", icon: Settings },
];

interface Props {
  active: ControlTab;
  density: Density;
  openProposals: number;
  /** Total deduped decision-inbox count — badged on the Postfach tab from anywhere. */
  inboxTotal: number;
  /** Worst tone present in the inbox — colours the Postfach badge. */
  inboxTone: ToneName;
  commandButtonRef?: React.RefObject<HTMLButtonElement | null>;
  onOpenCommand: () => void;
  children: React.ReactNode;
  onNavigate: (tab: ControlTab) => void;
}

interface BadgeInfo { count: number; cls: string }

// One badge model for every nav surface: the Postfach tab carries the live
// "needs me" total (tone-coloured), Autoresearch keeps its open-proposal count.
function tabBadge(tab: ControlTab, openProposals: number, inboxTotal: number, inboxTone: ToneName): BadgeInfo | null {
  if (tab === "inbox" && inboxTotal > 0) {
    const cls = inboxTone === "red" || inboxTone === "rose" ? "hc-badge-red" : inboxTone === "amber" ? "hc-badge-amber" : "hc-badge-accent";
    return { count: inboxTotal, cls };
  }
  if (tab === "autoresearch" && openProposals > 0) {
    return { count: openProposals, cls: "hc-badge-accent" };
  }
  return null;
}

export function ControlShell(props: Props) {
  return props.density === "compact" ? <ShellCompact {...props} /> : <ShellAiry {...props} />;
}

function ShellAiry({ active, children, openProposals, inboxTotal, inboxTone, onNavigate, commandButtonRef, onOpenCommand }: Props) {
  return (
    <div className="hc-page flex min-h-0 flex-col px-4 pb-[calc(5.5rem+env(safe-area-inset-bottom,0px))] pt-4 sm:px-6 lg:px-8">
      <header className="mb-4 flex flex-col gap-3 lg:flex-row lg:items-start lg:justify-between">
        <div><p className="hc-eyebrow">Operator Dashboard</p><h1 className="mt-1 text-2xl font-semibold tracking-normal text-white">Hermes Control</h1></div>
        <div className="flex flex-wrap justify-end gap-2"><CommandButton buttonRef={commandButtonRef} onOpen={onOpenCommand} /><MoreNav /><div className="flex flex-wrap items-center justify-end gap-2"><StatusDots /></div></div>
        <DesktopTabs active={active} openProposals={openProposals} inboxTotal={inboxTotal} inboxTone={inboxTone} onNavigate={onNavigate} />
      </header>
      <main className="mx-auto w-full max-w-6xl flex-1">{children}</main>
      <nav className="fixed bottom-0 left-0 right-0 z-40 border-t lg:hidden border-white/10 bg-black/85 px-2 pb-[env(safe-area-inset-bottom,0px)] backdrop-blur-xl">
        <div className="grid" style={{ gridTemplateColumns: `repeat(${tabs.length}, minmax(0, 1fr))` }}>
          {tabs.map((tab) => <TabButton key={tab.id} tab={tab} active={active === tab.id} badge={tabBadge(tab.id, openProposals, inboxTotal, inboxTone)} onClick={() => onNavigate(tab.id)} />)}
        </div>
      </nav>
    </div>
  );
}

function ShellCompact({ active, children, openProposals, inboxTotal, inboxTone, onNavigate, commandButtonRef, onOpenCommand }: Props) {
  return (
    <div className="hc-page grid min-h-0 grid-cols-[72px_1fr] gap-0">
      <aside className="sticky top-0 flex h-[calc(100dvh-5rem)] flex-col items-center justify-between border-r border-[var(--hc-border)] bg-[var(--hc-rail)] px-2 py-4">
        <div className="flex flex-col gap-2">
          <div className="mb-2 grid h-11 w-11 place-items-center rounded-lg border border-[var(--hc-accent-border)] bg-[var(--hc-accent-wash)] text-[var(--hc-accent-text)]"><Sparkles className="h-5 w-5" /></div>
          {tabs.map((tab) => <RailButton key={tab.id} tab={tab} active={active === tab.id} badge={tabBadge(tab.id, openProposals, inboxTotal, inboxTone)} onClick={() => onNavigate(tab.id)} />)}
          <button ref={commandButtonRef} type="button" title="Command Palette" aria-label="Command Palette" onClick={onOpenCommand} className="grid h-11 w-11 place-items-center rounded-lg border border-transparent hc-soft hover:border-[var(--hc-accent-border)] hover:bg-[var(--hc-accent-wash)]"><Command className="h-5 w-5" /></button>
          <RailMoreNav />
        </div>
        <PanelLeft className="h-4 w-4 hc-dim" />
      </aside>
      <div className="min-w-0 px-6 py-5">
        <header className="mb-5 flex items-center justify-between gap-3">
          <div><p className="hc-eyebrow">Hermes Control</p><h1 className="mt-1 text-xl font-semibold text-white">{tabs.find((t) => t.id === active)?.label}</h1></div>
          <div className="flex flex-wrap items-center justify-end gap-2"><StatusDots /></div>
        </header>
        <main>{children}</main>
      </div>
    </div>
  );
}


function DesktopTabs({ active, openProposals, inboxTotal, inboxTone, onNavigate }: { active: ControlTab; openProposals: number; inboxTotal: number; inboxTone: ToneName; onNavigate: (tab: ControlTab) => void }) {
  return (
    <nav className="hidden w-full flex-wrap gap-2 lg:flex">
      {tabs.map((tab) => {
        const Icon = tab.icon;
        const badge = tabBadge(tab.id, openProposals, inboxTotal, inboxTone);
        return (
          <button key={tab.id} type="button" onClick={() => onNavigate(tab.id)} className={cn("relative inline-flex min-h-10 items-center gap-2 rounded-lg border border-white/10 px-3 text-sm hc-soft transition", active === tab.id && "hc-nav-active border-[var(--hc-accent-border)] bg-[var(--hc-accent-wash)] text-[var(--hc-accent-text)]")}>
            <Icon className="h-4 w-4" />{tab.label}
            {badge ? <span className={cn("hc-badge", badge.cls)}>{badge.count}</span> : null}
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
    <details className="group relative">
      <summary className="flex min-h-11 cursor-pointer list-none items-center gap-2 rounded-lg border border-white/10 px-3 text-sm hc-soft hover:bg-white/5"><MoreHorizontal className="h-4 w-4" />Mehr</summary>
      <div className="absolute right-0 top-12 z-50 hidden w-56 rounded-lg border border-[var(--hc-border)] bg-[var(--hc-panel)] p-2 shadow-xl group-open:block">
        {moreTabs.map((item) => { const Icon = item.icon; return <Link key={item.path} to={item.path} className="flex min-h-11 items-center gap-2 rounded-md px-3 text-sm hc-soft hover:bg-white/5 hover:text-white"><Icon className="h-4 w-4" />{item.label}</Link>; })}
        <div className="my-1.5 border-t border-[var(--hc-border)]" />
        {secondaryNav.map((item) => { const Icon = item.icon; return <Link key={item.path} to={item.path} className="flex min-h-11 items-center gap-2 rounded-md px-3 text-sm hc-soft hover:bg-white/5 hover:text-white"><Icon className="h-4 w-4" />{item.label}</Link>; })}
      </div>
    </details>
  );
}

function RailMoreNav() {
  return (
    <div className="group relative">
      <button type="button" title="Mehr" aria-label="Mehr" className="grid h-11 w-11 place-items-center rounded-lg border border-transparent hc-soft hover:border-[var(--hc-accent-border)] hover:bg-[var(--hc-accent-wash)]"><MoreHorizontal className="h-5 w-5" /></button>
      <div className="invisible absolute left-12 top-0 z-50 w-56 rounded-lg border border-[var(--hc-border)] bg-[var(--hc-panel)] p-2 opacity-0 shadow-xl transition group-hover:visible group-hover:opacity-100 group-focus-within:visible group-focus-within:opacity-100">
        {moreTabs.map((item) => { const Icon = item.icon; return <Link key={item.path} to={item.path} className="flex min-h-11 items-center gap-2 rounded-md px-3 text-sm hc-soft hover:bg-white/5 hover:text-white"><Icon className="h-4 w-4" />{item.label}</Link>; })}
        <div className="my-1.5 border-t border-[var(--hc-border)]" />
        {secondaryNav.map((item) => { const Icon = item.icon; return <Link key={item.path} to={item.path} className="flex min-h-11 items-center gap-2 rounded-md px-3 text-sm hc-soft hover:bg-white/5 hover:text-white"><Icon className="h-4 w-4" />{item.label}</Link>; })}
      </div>
    </div>
  );
}

function StatusDots() {
  return <div className="hidden items-center gap-2 rounded-full border border-white/10 bg-black/20 px-3 py-2 text-xs hc-soft md:flex"><span className="hc-led hc-led-live h-2 w-2 rounded-full" />Hermes<span className="hc-mono">:9119</span><span className="hc-led hc-led-ready h-2 w-2 rounded-full" />Dashboard</div>;
}

function TabButton({ tab, active, badge, onClick }: { tab: (typeof tabs)[number]; active: boolean; badge: BadgeInfo | null; onClick: () => void }) {
  const Icon = tab.icon;
  return (
    <button type="button" onClick={onClick} aria-label={tab.label} className={cn("hc-tab relative flex flex-col items-center justify-center gap-1 text-xs hc-soft", active && "text-[var(--hc-accent-text)]")}>
      <Icon className="h-5 w-5" />
      <span className="max-w-full truncate px-0.5">{tab.mobileLabel}</span>
      {badge ? <span className={cn("hc-badge absolute right-4 top-2", badge.cls)}>{badge.count}</span> : null}
    </button>
  );
}

function RailButton({ tab, active, badge, onClick }: { tab: (typeof tabs)[number]; active: boolean; badge: BadgeInfo | null; onClick: () => void }) {
  const Icon = tab.icon;
  return (
    <button type="button" title={tab.label} aria-label={tab.label} onClick={onClick} className={cn("relative grid h-11 w-11 place-items-center rounded-lg border border-transparent hc-soft transition", active && "hc-nav-active border-[var(--hc-accent-border)] bg-[var(--hc-accent-wash)] text-[var(--hc-accent-text)]")}>
      <Icon className="h-5 w-5" />
      {badge ? <span className={cn("hc-badge absolute -right-1 -top-1", badge.cls)}>{badge.count}</span> : null}
    </button>
  );
}
