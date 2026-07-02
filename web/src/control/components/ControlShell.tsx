import { useEffect, useRef, useState } from "react";
import { Activity, BookOpen, ChartSpline, Clock, Columns3, Command, FlaskConical, Gauge, GitBranch, Radar, Hammer, KanbanSquare, LayoutDashboard, Lightbulb, MessageSquare, MoreHorizontal, PanelLeft, SearchCheck, Settings, Shield, Sparkles, TerminalSquare, Workflow } from "lucide-react";
import { Link } from "react-router-dom";
import { cn } from "@/lib/utils";
import { de } from "../i18n/de";
import type { Density } from "../hooks/useDensity";
import type { DecisionInboxData } from "../hooks/useControlData";
import type { HealthStatus, SystemHealthResponse, ToneName } from "../lib/types";
import { NotificationBridge } from "./NotificationBridge";
import { Overlay } from "./Overlay";
import { useClientNowSeconds } from "../lib/clock";

export type ControlTab = "overview" | "inbox" | "pulse" | "workstreams" | "agentTerminals" | "flow" | "ketten" | "statistik" | "autoresearch" | "backlog" | "orchestrator" | "crons" | "lanes" | "pressure" | "ops" | "research" | "bibliothek" | "schmiede" | "stratege";

// The daily spine — 4 tabs. Start (the Command cockpit: needs-me + fleet +
// health), Flow (the live work board, absorbs the fleet), Statistik (charts:
// throughput / burn / cycle-time / reliability), Bibliothek (the reading
// room — Piet-Entscheid 2026-06-11: der Alltags-Lesesaal verdrängt
// Autoresearch in den "Mehr"-Overflow). Everything else is a re-slice of
// these and lives in the "Mehr" overflow (moreTabs) below.
const tabs: Array<{ id: ControlTab; label: string; mobileLabel: string; path: string; icon: React.ComponentType<{ className?: string }> }> = [
  { id: "inbox", label: "Start", mobileLabel: "Start", path: "/control", icon: LayoutDashboard },
  // Terminals = Haupt-Arbeitszentrale (Operator-Entscheid 2026-07-01): der Tab,
  // in dem im tmux mit Hermes/Claude/Codex gearbeitet wird — daher primär.
  { id: "agentTerminals", label: "Terminals", mobileLabel: "Terminal", path: "/control/agent-terminals", icon: TerminalSquare },
  { id: "flow", label: de.tabs.flow, mobileLabel: "Flow", path: "/control/flow", icon: Columns3 },
  { id: "ketten", label: "Ketten", mobileLabel: "Ketten", path: "/control/ketten", icon: GitBranch },
  { id: "statistik", label: de.tabs.statistik, mobileLabel: "Statistik", path: "/control/statistik", icon: ChartSpline },
  // Kurzlabel "Regal" fürs 6-Slot-Grid der mobilen Bottom-Bar (Cockpit-Slice
  // "Bibliothek-Lesesaal + Shell-Upgrade") — Desktop-`label` bleibt "Bibliothek".
  { id: "bibliothek", label: "Bibliothek", mobileLabel: "Regal", path: "/control/bibliothek", icon: BookOpen },
  { id: "stratege", label: "Stratege", mobileLabel: "Stratege", path: "/control/stratege", icon: Lightbulb },
];

// Bibliothek zog in die primäre mobile Bottom-Bar (vorher nur per Mehr-Sheet/
// Direkt-URL erreichbar) — 5 Slots + "Mehr" = 6-Spalten-Grid (siehe unten).
const mobileTabs = tabs.filter((tab) => ["inbox", "agentTerminals", "flow", "ketten", "bibliothek"].includes(tab.id));

// Demoted control surfaces — still routed + reachable, just not in the primary
// rail/bottom-bar. The Command home already surfaces their headline signal.
const moreTabs = [
  { label: de.tabs.overview, path: "/control/overview", icon: Activity },
  { label: de.tabs.pulse, path: "/control/pulse", icon: Activity },
  { label: de.tabs.workstreams, path: "/control/workstreams", icon: GitBranch },
  { label: de.tabs.backlog, path: "/control/backlog", icon: KanbanSquare },
  { label: de.tabs.orchestrator, path: "/control/orchestrator", icon: Workflow },
  { label: de.tabs.crons, path: "/control/crons", icon: Clock },
  // Label literal (wie "Start"): die Lanes-Strings leben im View, nicht in
  // i18n/de.ts — kein Edit an Shared-Dateien paralleler Sessions.
  { label: "Lanes", path: "/control/lanes", icon: Shield },
  { label: "Pressure", path: "/control/pressure", icon: Gauge },
  { label: "Ops Radar", path: "/control/ops", icon: Radar },
  // Programm 3: Recherche (Wissen beauftragen); Bibliothek sitzt seit
  // 2026-06-11 in der Haupt-Nav, dafür wohnt Autoresearch jetzt hier.
  { label: "Recherche", path: "/control/research", icon: SearchCheck },
  { label: de.tabs.autoresearch, path: "/control/autoresearch", icon: FlaskConical },
  // Prompt-Schmiede: Copy-Paste-Generator für Agent-Steuerbefehle (kein Dispatch).
  { label: de.tabs.schmiede, path: "/control/schmiede", icon: Hammer },
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
  /** Decision-Inbox-Stand für die Glocke (Browser-Notifications) im Header. */
  inbox: DecisionInboxData;
  openProposals: number;
  /** Total deduped decision-inbox count — badged on the Postfach tab from anywhere. */
  inboxTotal: number;
  /** Worst tone present in the inbox — colours the Postfach badge. */
  inboxTone: ToneName;
  /** Neue Bibliothek-Einträge seit dem letzten Besuch — badged den Lesesaal-Tab. */
  libraryUnread?: number;
  /** Offene Strategen-Vorschläge — badged den Stratege-Tab. */
  strategistCount?: number;
  health: {
    data: SystemHealthResponse | null;
    error: string | null;
    isStale?: boolean;
    lastUpdated: number | null;
  };
  commandButtonRef?: React.RefObject<HTMLButtonElement | null>;
  onOpenCommand: () => void;
  children: React.ReactNode;
  onNavigate: (tab: ControlTab) => void;
  /** Hover/Fokus auf einem Tab → Lazy-Chunk der View schon laden (perceived nav speed). */
  onPrefetch?: (tab: ControlTab) => void;
}

interface BadgeInfo { count: number; cls: string }

// One badge model for every nav surface: the Postfach tab carries the live
// "needs me" total (tone-coloured), Autoresearch keeps its open-proposal count
// (relevant, falls es je in die Haupt-Nav zurückkehrt), die Bibliothek zählt
// ungelesene Lesesaal-Einträge.
function tabBadge(tab: ControlTab, openProposals: number, inboxTotal: number, inboxTone: ToneName, libraryUnread: number, strategistCount: number): BadgeInfo | null {
  if (tab === "inbox" && inboxTotal > 0) {
    const cls = inboxTone === "red" || inboxTone === "rose" ? "hc-badge-red" : inboxTone === "amber" ? "hc-badge-amber" : "hc-badge-accent";
    return { count: inboxTotal, cls };
  }
  if (tab === "autoresearch" && openProposals > 0) {
    return { count: openProposals, cls: "hc-badge-accent" };
  }
  if (tab === "bibliothek" && libraryUnread > 0) {
    return { count: libraryUnread, cls: "hc-badge-accent" };
  }
  if (tab === "stratege" && strategistCount > 0) {
    return { count: strategistCount, cls: "hc-badge-accent" };
  }
  return null;
}

export function ControlShell(props: Props) {
  return props.density === "compact" ? <ShellCompact {...props} /> : <ShellAiry {...props} />;
}

function ShellAiry({ active, children, inbox, openProposals, inboxTotal, inboxTone, libraryUnread, strategistCount, health, onNavigate, onPrefetch, commandButtonRef, onOpenCommand }: Props) {
  // "Mehr" lebt auf Mobile als 5. Bottom-Tab + Bottom-Sheet (Audit 2026-06-11,
  // M3 Variante A) — das Header-Dropdown ist dort zu hoch und schloss sich beim
  // Scrollversuch. Aktiv markiert, wenn die aktuelle View keine der 4 Haupt-Tabs ist.
  const [moreOpen, setMoreOpen] = useState(false);
  const moreActive = !mobileTabs.some((tab) => tab.id === active);
  return (
    <div className="hc-page flex min-h-0 flex-col px-4 pb-[calc(5.5rem+env(safe-area-inset-bottom,0px))] pt-4 sm:px-6 lg:px-8">
      <header className="mb-4 flex flex-col gap-3 lg:flex-row lg:items-start lg:justify-between">
        <div><p className="hc-eyebrow">Operator Dashboard</p><h1 className="mt-1 text-2xl font-semibold tracking-normal text-white">Hermes Control</h1></div>
        <div className="flex flex-wrap items-center justify-end gap-2"><NotificationBridge inbox={inbox} /><CommandButton buttonRef={commandButtonRef} onOpen={onOpenCommand} /><MoreNav /><div className="flex flex-wrap items-center justify-end gap-2"><StatusDots health={health} /></div></div>
        <DesktopTabs active={active} openProposals={openProposals} inboxTotal={inboxTotal} inboxTone={inboxTone} libraryUnread={libraryUnread} strategistCount={strategistCount ?? 0} onNavigate={onNavigate} onPrefetch={onPrefetch} />
      </header>
      <main className="mx-auto w-full max-w-6xl flex-1">{children}</main>
      <nav className="fixed bottom-0 left-0 right-0 z-40 border-t lg:hidden border-white/10 bg-black/85 px-2 pb-[env(safe-area-inset-bottom,0px)] backdrop-blur-xl">
        <div className="grid grid-cols-6">
          {mobileTabs.map((tab) => <TabButton key={tab.id} tab={tab} active={active === tab.id} badge={tabBadge(tab.id, openProposals, inboxTotal, inboxTone, libraryUnread ?? 0, strategistCount ?? 0)} onClick={() => onNavigate(tab.id)} onPrefetch={() => onPrefetch?.(tab.id)} />)}
          <button type="button" onClick={() => setMoreOpen(true)} aria-label="Mehr" aria-expanded={moreOpen} className={cn("hc-tab relative flex flex-col items-center justify-center gap-1 text-xs hc-soft", moreActive && "text-[var(--hc-accent-text)]")}>
            <MoreHorizontal className="h-5 w-5" />
            <span className="max-w-full truncate px-0.5">Mehr</span>
          </button>
        </div>
      </nav>
      {moreOpen ? <MoreSheet onClose={() => setMoreOpen(false)} /> : null}
    </div>
  );
}

/** Mobiles "Mehr": Bottom-Sheet über den Overlay-Portal-Wrapper — scrollbar,
 *  Scroll-Lock, große Tap-Flächen; gruppiert wie das Desktop-Dropdown. */
function MoreSheet({ onClose }: { onClose: () => void }) {
  const renderItem = (item: { label: string; path: string; icon: React.ComponentType<{ className?: string }> }) => {
    const Icon = item.icon;
    return <Link key={item.path} to={item.path} onClick={onClose} className="flex min-h-11 items-center gap-2.5 rounded-md px-3 text-sm hc-soft hover:bg-white/5 hover:text-white"><Icon className="h-4 w-4" />{item.label}</Link>;
  };
  // Primäre Tabs außerhalb der 4-Slot-Bottom-Bar (Bibliothek, Stratege) sind auf
  // Mobile sonst nur per Direkt-URL erreichbar — hier mit ins "Mehr"-Sheet holen.
  const overflowPrimary = tabs.filter((tab) => !mobileTabs.some((m) => m.id === tab.id));
  return (
    <Overlay onClose={onClose} ariaLabel="Mehr">
      <p className="hc-eyebrow">Ansichten</p>
      <div className="mt-2 grid gap-0.5">{[...overflowPrimary, ...moreTabs].map(renderItem)}</div>
      <p className="hc-eyebrow mt-4">System</p>
      <div className="mt-2 grid gap-0.5">{secondaryNav.map(renderItem)}</div>
    </Overlay>
  );
}

function ShellCompact({ active, children, inbox, openProposals, inboxTotal, inboxTone, libraryUnread, strategistCount, health, onNavigate, onPrefetch, commandButtonRef, onOpenCommand }: Props) {
  return (
    <div className="hc-page grid min-h-0 grid-cols-[72px_1fr] gap-0">
      <aside className="sticky top-0 flex h-[calc(100dvh-5rem)] flex-col items-center justify-between border-r border-[var(--hc-border)] bg-[var(--hc-rail)] px-2 py-4">
        <div className="flex flex-col gap-2">
          <div className="mb-2 grid h-11 w-11 place-items-center rounded-lg border border-[var(--hc-accent-border)] bg-[var(--hc-accent-wash)] text-[var(--hc-accent-text)]"><Sparkles className="h-5 w-5" /></div>
          {tabs.map((tab) => <RailButton key={tab.id} tab={tab} active={active === tab.id} badge={tabBadge(tab.id, openProposals, inboxTotal, inboxTone, libraryUnread ?? 0, strategistCount ?? 0)} onClick={() => onNavigate(tab.id)} onPrefetch={() => onPrefetch?.(tab.id)} />)}
          <button ref={commandButtonRef} type="button" title="Command Palette" aria-label="Command Palette" onClick={onOpenCommand} className="grid h-11 w-11 place-items-center rounded-lg border border-transparent hc-soft hover:border-[var(--hc-accent-border)] hover:bg-[var(--hc-accent-wash)]"><Command className="h-5 w-5" /></button>
          <RailMoreNav />
        </div>
        <PanelLeft className="h-4 w-4 hc-dim" />
      </aside>
      <div className="min-w-0 px-6 py-5">
        <header className="mb-5 flex items-center justify-between gap-3">
          <div><p className="hc-eyebrow">Hermes Control</p><h1 className="mt-1 text-xl font-semibold text-white">{tabs.find((t) => t.id === active)?.label}</h1></div>
          <div className="flex flex-wrap items-center justify-end gap-2"><NotificationBridge inbox={inbox} /><StatusDots health={health} /></div>
        </header>
        <main>{children}</main>
      </div>
    </div>
  );
}


function DesktopTabs({ active, openProposals, inboxTotal, inboxTone, libraryUnread, strategistCount, onNavigate, onPrefetch }: { active: ControlTab; openProposals: number; inboxTotal: number; inboxTone: ToneName; libraryUnread?: number; strategistCount?: number; onNavigate: (tab: ControlTab) => void; onPrefetch?: (tab: ControlTab) => void }) {
  return (
    <nav className="hidden w-full flex-wrap gap-2 lg:flex">
      {tabs.map((tab) => {
        const Icon = tab.icon;
        const badge = tabBadge(tab.id, openProposals, inboxTotal, inboxTone, libraryUnread ?? 0, strategistCount ?? 0);
        return (
          <button key={tab.id} type="button" onClick={() => onNavigate(tab.id)} onMouseEnter={() => onPrefetch?.(tab.id)} onFocus={() => onPrefetch?.(tab.id)} className={cn("relative inline-flex min-h-10 items-center gap-2 rounded-lg border border-white/10 px-3 text-sm hc-soft transition", active === tab.id && "hc-nav-active border-[var(--hc-accent-border)] bg-[var(--hc-accent-wash)] text-[var(--hc-accent-text)]")}>
            <Icon className="h-4 w-4" />{tab.label}
            {badge ? <span className={cn("hc-badge", badge.cls)}>{badge.count}</span> : null}
          </button>
        );
      })}
    </nav>
  );
}

function CommandButton({ buttonRef, onOpen }: { buttonRef?: React.RefObject<HTMLButtonElement | null>; onOpen: () => void }) {
  // Auf Touch-Phones ist ein "⌘K"-Hint bedeutungslos — Button erst ab sm zeigen.
  return <button ref={buttonRef} type="button" className="hc-hit hidden items-center gap-2 rounded-lg border border-white/10 px-3 text-sm hc-soft hover:bg-white/5 sm:inline-flex" onClick={onOpen}><Command className="h-4 w-4" />⌘K</button>;
}

function useDismissibleMenu<T extends HTMLElement = HTMLDivElement>() {
  const [open, setOpen] = useState(false);
  const ref = useRef<T | null>(null);

  useEffect(() => {
    if (!open) return;
    const onPointerDown = (event: PointerEvent) => {
      if (!ref.current?.contains(event.target as Node)) setOpen(false);
    };
    const onKeyDown = (event: KeyboardEvent) => {
      if (event.key === "Escape") setOpen(false);
    };
    window.addEventListener("pointerdown", onPointerDown);
    window.addEventListener("keydown", onKeyDown);
    return () => {
      window.removeEventListener("pointerdown", onPointerDown);
      window.removeEventListener("keydown", onKeyDown);
    };
  }, [open]);

  return { open, setOpen, ref };
}

function MoreNav() {
  // Destrukturiert statt `menu.ref`/`menu.open`: react-hooks/refs erkennt nur
  // direkte Ref-Bezeichner als JSX-Durchreichung, keine Property-Zugriffe.
  const { open, setOpen, ref } = useDismissibleMenu<HTMLDetailsElement>();
  return (
    // hidden lg:block: unter lg übernimmt der "Mehr"-Bottom-Tab (MoreSheet);
    // max-h + overflow: auch kleine Desktop-Fenster erreichen alle Einträge.
    <details ref={ref} open={open} onToggle={(event) => setOpen(event.currentTarget.open)} className="group relative hidden lg:block">
      <summary className="flex min-h-11 cursor-pointer list-none items-center gap-2 rounded-lg border border-white/10 px-3 text-sm hc-soft hover:bg-white/5"><MoreHorizontal className="h-4 w-4" />Mehr</summary>
      <div className="absolute right-0 top-12 z-50 hidden max-h-[70dvh] w-56 overflow-y-auto overscroll-contain rounded-lg border border-[var(--hc-border)] bg-[var(--hc-panel)] p-2 shadow-xl group-open:block">
        {moreTabs.map((item) => { const Icon = item.icon; return <Link key={item.path} to={item.path} onClick={() => setOpen(false)} className="flex min-h-11 items-center gap-2 rounded-md px-3 text-sm hc-soft hover:bg-white/5 hover:text-white"><Icon className="h-4 w-4" />{item.label}</Link>; })}
        <div className="my-1.5 border-t border-[var(--hc-border)]" />
        {secondaryNav.map((item) => { const Icon = item.icon; return <Link key={item.path} to={item.path} onClick={() => setOpen(false)} className="flex min-h-11 items-center gap-2 rounded-md px-3 text-sm hc-soft hover:bg-white/5 hover:text-white"><Icon className="h-4 w-4" />{item.label}</Link>; })}
      </div>
    </details>
  );
}

function RailMoreNav() {
  const { open, setOpen, ref } = useDismissibleMenu();
  return (
    <div ref={ref} className="group relative">
      <button type="button" title="Mehr" aria-label="Mehr" aria-expanded={open} onClick={() => setOpen((current) => !current)} className="grid h-11 w-11 place-items-center rounded-lg border border-transparent hc-soft hover:border-[var(--hc-accent-border)] hover:bg-[var(--hc-accent-wash)]"><MoreHorizontal className="h-5 w-5" /></button>
      <div className={cn("absolute left-12 top-0 z-50 w-56 rounded-lg border border-[var(--hc-border)] bg-[var(--hc-panel)] p-2 shadow-xl transition group-hover:visible group-hover:opacity-100 group-focus-within:visible group-focus-within:opacity-100", open ? "visible opacity-100" : "invisible opacity-0")}>
        {moreTabs.map((item) => { const Icon = item.icon; return <Link key={item.path} to={item.path} onClick={() => setOpen(false)} className="flex min-h-11 items-center gap-2 rounded-md px-3 text-sm hc-soft hover:bg-white/5 hover:text-white"><Icon className="h-4 w-4" />{item.label}</Link>; })}
        <div className="my-1.5 border-t border-[var(--hc-border)]" />
        {secondaryNav.map((item) => { const Icon = item.icon; return <Link key={item.path} to={item.path} onClick={() => setOpen(false)} className="flex min-h-11 items-center gap-2 rounded-md px-3 text-sm hc-soft hover:bg-white/5 hover:text-white"><Icon className="h-4 w-4" />{item.label}</Link>; })}
      </div>
    </div>
  );
}

function healthLed(status: HealthStatus | "unknown", stale: boolean): string {
  if (stale) return "hc-led-warn";
  if (status === "healthy") return "hc-led-live";
  if (status === "degraded") return "hc-led-warn";
  if (status === "offline") return "hc-led-error";
  return "hc-led-idle";
}

function healthLabel(status: HealthStatus | "unknown", stale: boolean): string {
  if (stale) return "stale";
  if (status === "healthy") return "gesund";
  if (status === "degraded") return "degraded";
  if (status === "offline") return "offline";
  return "unbekannt";
}

function StatusDots({ health }: { health: Props["health"] }) {
  const gateway = health.data?.subsystems.gateway.status ?? (health.error ? "offline" : "unknown");
  const dashboard = health.data?.overall ?? (health.error ? "offline" : "unknown");
  const stale = Boolean(health.isStale);
  const clientNow = useClientNowSeconds();
  const checked = health.lastUpdated ? `Zuletzt aktuell vor ${Math.max(0, clientNow - health.lastUpdated)}s` : "Noch kein Health-Signal";
  const title = [health.error, checked].filter(Boolean).join(" · ");
  return (
    <div title={title} className="hidden items-center gap-2 rounded-full border border-white/10 bg-black/20 px-3 py-2 text-xs hc-soft md:flex">
      <span className={cn("hc-led h-2 w-2 rounded-full", healthLed(gateway, stale))} />Hermes<span className="hc-mono">:9119</span><span className="hc-mono hc-dim">{healthLabel(gateway, stale)}</span>
      <span className={cn("hc-led h-2 w-2 rounded-full", healthLed(dashboard, stale))} />Dashboard<span className="hc-mono hc-dim">{healthLabel(dashboard, stale)}</span>
    </div>
  );
}

function TabButton({ tab, active, badge, onClick, onPrefetch }: { tab: (typeof tabs)[number]; active: boolean; badge: BadgeInfo | null; onClick: () => void; onPrefetch?: () => void }) {
  const Icon = tab.icon;
  return (
    <button type="button" onClick={onClick} onTouchStart={onPrefetch} onFocus={onPrefetch} aria-label={tab.label} className={cn("hc-tab relative flex flex-col items-center justify-center gap-1 text-xs hc-soft", active && "text-[var(--hc-accent-text)]")}>
      <Icon className="h-5 w-5" />
      <span className="max-w-full truncate px-0.5">{tab.mobileLabel}</span>
      {badge ? <span className={cn("hc-badge absolute right-4 top-2", badge.cls)}>{badge.count}</span> : null}
    </button>
  );
}

function RailButton({ tab, active, badge, onClick, onPrefetch }: { tab: (typeof tabs)[number]; active: boolean; badge: BadgeInfo | null; onClick: () => void; onPrefetch?: () => void }) {
  const Icon = tab.icon;
  return (
    <button type="button" title={tab.label} aria-label={tab.label} onClick={onClick} onMouseEnter={onPrefetch} onFocus={onPrefetch} className={cn("relative grid h-11 w-11 place-items-center rounded-lg border border-transparent hc-soft transition", active && "hc-nav-active border-[var(--hc-accent-border)] bg-[var(--hc-accent-wash)] text-[var(--hc-accent-text)]")}>
      <Icon className="h-5 w-5" />
      {badge ? <span className={cn("hc-badge absolute -right-1 -top-1", badge.cls)}>{badge.count}</span> : null}
    </button>
  );
}
