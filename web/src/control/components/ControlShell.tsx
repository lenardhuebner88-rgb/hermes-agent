import { useEffect, useRef, useState } from "react";
import { BookOpen, ChartSpline, Clock, Command, FlaskConical, GitBranch, Hammer, KanbanSquare, LayoutDashboard, Lightbulb, MessageSquare, Mic2, MoreHorizontal, PanelLeft, PenTool, RefreshCw, SearchCheck, Server, Settings, Shield, Sparkles, TerminalSquare, Workflow, Anchor } from "lucide-react";
import { Link } from "react-router-dom";
import { cn } from "@/lib/utils";
import { de } from "../i18n/de";
import type { Density } from "../hooks/useDensity";
import type { DecisionInboxData } from "../hooks/useControlData";
import type { HealthStatus, SystemHealthResponse, ToneName } from "../lib/types";
import { healthLed, healthLabel } from "../lib/health";
import { NotificationBridge } from "./NotificationBridge";
import { Overlay } from "./Overlay";
import { PulsLeiste } from "./leitstand";
import { useClientNowSeconds } from "../lib/clock";

export type ControlTab = "fleet" | "overview" | "inbox" | "pulse" | "workstreams" | "agentTerminals" | "flow" | "ketten" | "statistik" | "autoresearch" | "backlog" | "orchestrator" | "crons" | "lanes" | "system" | "pressure" | "ops" | "research" | "bibliothek" | "schmiede" | "stratege" | "loops" | "designBoard" | "diktat";

// The daily spine: Fleet · Start · Terminal · Statistik · Regal. Flow/Ketten
// live in Fleet now, and System remains reachable through "Mehr" + deep-link.
const tabs: Array<{ id: ControlTab; label: string; mobileLabel: string; path: string; icon: React.ComponentType<{ className?: string }> }> = [
  // Fleet: neues erstes Tab — Operator-Lagezentrum (2026-07-03)
  { id: "fleet", label: "Fleet", mobileLabel: "Fleet", path: "/control/fleet", icon: Anchor },
  { id: "inbox", label: "Start", mobileLabel: "Start", path: "/control", icon: LayoutDashboard },
  // Terminals = Haupt-Arbeitszentrale (Operator-Entscheid 2026-07-01): der Tab,
  // in dem im tmux mit Hermes/Claude/Codex gearbeitet wird — daher primär.
  { id: "agentTerminals", label: "Terminals", mobileLabel: "Terminal", path: "/control/agent-terminals", icon: TerminalSquare },
  { id: "statistik", label: de.tabs.statistik, mobileLabel: "Statistik", path: "/control/statistik", icon: ChartSpline },
  // Kurzlabel "Regal" fürs 6-Slot-Grid der mobilen Bottom-Bar (Cockpit-Slice
  // "Bibliothek-Lesesaal + Shell-Upgrade") — Desktop-`label` bleibt "Bibliothek".
  { id: "bibliothek", label: "Bibliothek", mobileLabel: "Regal", path: "/control/bibliothek", icon: BookOpen },
];

// Fleet ist an erster Stelle der mobilen Bottom-Bar (2026-07-03); Statistik
// übernimmt den alten Flow-Slot (S6, 2026-07-05).
// 5 Slots + "Mehr" = 6-Spalten-Grid. Identisch mit `tabs` (die 5 Primaries
// SIND die mobilen Slots) — auch die Rail rendert daraus.
const mobileTabs = tabs.filter((tab) => ["fleet", "inbox", "agentTerminals", "statistik", "bibliothek"].includes(tab.id));

// Demoted control surfaces — still routed + reachable, just not in the primary
// rail/bottom-bar. The Command home already surfaces their headline signal.
// `id` matcht den ControlTab-Wert (Lookup für Masthead-Label + Rail-Pin).
const moreTabs: Array<{ id: ControlTab; label: string; path: string; icon: React.ComponentType<{ className?: string }> }> = [
  { id: "workstreams", label: de.tabs.workstreams, path: "/control/workstreams", icon: GitBranch },
  { id: "backlog", label: de.tabs.backlog, path: "/control/backlog", icon: KanbanSquare },
  { id: "orchestrator", label: de.tabs.orchestrator, path: "/control/orchestrator", icon: Workflow },
  { id: "crons", label: de.tabs.crons, path: "/control/crons", icon: Clock },
  { id: "loops", label: de.tabs.loops, path: "/control/loops", icon: RefreshCw },
  // Label literal (wie "Start"): die Lanes-Strings leben im View, nicht in
  // i18n/de.ts — kein Edit an Shared-Dateien paralleler Sessions.
  { id: "lanes", label: "Lanes", path: "/control/lanes", icon: Shield },
  // System (S1-Fusion): Druck + Ops Radar + Puls in einer Leitstand-Ansicht.
  { id: "system", label: "System", path: "/control/system", icon: Server },

  // Programm 3: Recherche (Wissen beauftragen); Bibliothek sitzt seit
  // 2026-06-11 in der Haupt-Nav, dafür wohnt Autoresearch jetzt hier.
  { id: "research", label: "Recherche", path: "/control/research", icon: SearchCheck },
  { id: "autoresearch", label: de.tabs.autoresearch, path: "/control/autoresearch", icon: FlaskConical },
  // Prompt-Schmiede: Copy-Paste-Generator für Agent-Steuerbefehle (kein Dispatch).
  { id: "schmiede", label: de.tabs.schmiede, path: "/control/schmiede", icon: Hammer },
  { id: "stratege", label: "Stratege", path: "/control/stratege", icon: Lightbulb },
  { id: "designBoard", label: "Design", path: "/control/design-board", icon: PenTool },
  // Label literal (wie "Lanes"): kein Edit an i18n/de.ts paralleler Sessions.
  { id: "diktat", label: "Diktat", path: "/control/diktat", icon: Mic2 },
];

const secondaryNav = [
  { label: "Sessions", path: "/sessions", icon: MessageSquare },
  { label: "Kanban", path: "/plugins", icon: KanbanSquare },
  { label: "Modelle", path: "/models", icon: Shield },
  { label: "Logs", path: "/logs", icon: PanelLeft },
  { label: "Skills", path: "/skills", icon: Sparkles },
  { label: "Konfig", path: "/config", icon: Settings },
];

// Lookup für Masthead-Route-Label + Rail-Pin: primäre + sekundäre Tabs in
// einer Struktur (moreTabs deckt alle ControlTab-Werte außerhalb der 5
// Primaries ab; "overview"/"pulse"/… sind Redirect-Zwischenzustände ohne
// eigenen Eintrag — navLabel fällt dafür sicher auf "Control" zurück).
type NavEntry = { id: ControlTab; label: string; path: string; icon: React.ComponentType<{ className?: string }> };

const navLookup: Partial<Record<ControlTab, NavEntry>> = Object.fromEntries(
  [...tabs, ...moreTabs].map((item): [ControlTab, NavEntry] => [item.id, { id: item.id, label: item.label, path: item.path, icon: item.icon }]),
);

function navLabel(active: ControlTab): string {
  return navLookup[active]?.label ?? "Control";
}

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
  /** Puls-Leiste-Werte für die generische Masthead (W2-b). Optional — additiv,
   *  fehlt sie, rendert die Masthead nur den Routen-Label (kein Instrument-Fake). */
  pulse?: {
    workers: number | null;
    fragen: number | null;
    fragenTone?: ToneName;
    kostenUsd: number | null;
    kostenIsEquivalent?: boolean;
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

interface NavBadgeArgs {
  openProposals: number;
  inboxTotal: number;
  inboxTone: ToneName;
  libraryUnread: number;
  strategistCount: number;
}

export function ControlShell(props: Props) {
  const { active, density, children, inbox, openProposals, inboxTotal, inboxTone, libraryUnread, strategistCount, health, pulse, onNavigate, onPrefetch, commandButtonRef, onOpenCommand } = props;
  const [moreOpen, setMoreOpen] = useState(false);
  // One-masthead contract (W3-3, 2026-07-10): every route renders the shared
  // Puls-Leiste below — label + instruments + the NotificationBridge bell.
  // Fleet (W3-1a), Start/Inbox (W3-2) and Statistik (W3-3) were the last
  // per-view mastheads; that legacy mechanism (route-keyed `hasOwnMasthead`,
  // the hidden side-effect-only NotificationBridge mount, the padding fork)
  // is retired along with them — no route branching left here at all.
  const badgeArgs: NavBadgeArgs = { openProposals, inboxTotal, inboxTone, libraryUnread: libraryUnread ?? 0, strategistCount: strategistCount ?? 0 };

  return (
    <div data-density={density} className="hc-page flex min-h-0">
      <Rail
        active={active}
        {...badgeArgs}
        onNavigate={onNavigate}
        onPrefetch={onPrefetch}
        commandButtonRef={commandButtonRef}
        onOpenCommand={onOpenCommand}
        health={health}
      />
      <div className="flex min-w-0 flex-1 flex-col">
        <Masthead active={active} inbox={inbox} health={health} pulse={pulse} onOpenCommand={onOpenCommand} />
        <main className="mx-auto w-full flex-1 px-4 pt-4 pb-[calc(5.5rem+env(safe-area-inset-bottom,0px))] sm:px-6 tab:pb-6 lg:px-8 min-[1920px]:max-w-[1520px]">
          {children}
        </main>
      </div>
      <BottomBar
        active={active}
        {...badgeArgs}
        onNavigate={onNavigate}
        onPrefetch={onPrefetch}
        moreOpen={moreOpen}
        onToggleMore={() => setMoreOpen(true)}
      />
      {moreOpen ? <MoreSheet onClose={() => setMoreOpen(false)} /> : null}
    </div>
  );
}

/** Die eine geteilte Puls-Leiste für jede Route (W3-3: keine Ausnahmen mehr) —
 *  DESIGN.md "Puls-Leiste contract" / SHELL-SPEC.md W2-b. Rechts die geteilten
 *  Utilities — ⌘K nur unterhalb von `tab:` (die Rail trägt ihr eigenes ab
 *  `tab:`). */
function Masthead({ active, inbox, health, pulse, onOpenCommand }: { active: ControlTab; inbox: DecisionInboxData; health: Props["health"]; pulse: Props["pulse"]; onOpenCommand: () => void }) {
  const { gateway, stale, title } = useGatewayHealth(health);
  return (
    <div data-testid="control-masthead">
      <PulsLeiste
        label={navLabel(active)}
        workers={pulse?.workers ?? null}
        fragen={pulse?.fragen ?? null}
        fragenTone={pulse?.fragenTone}
        kostenUsd={pulse?.kostenUsd ?? null}
        kostenIsEquivalent={pulse?.kostenIsEquivalent}
        gateway={{ status: gateway, stale, title }}
      >
        <NotificationBridge inbox={inbox} />
        <StatusDots health={health} demoted={Boolean(pulse)} />
        <CommandButton onOpen={onOpenCommand} />
      </PulsLeiste>
    </div>
  );
}

interface RailProps extends NavBadgeArgs {
  active: ControlTab;
  onNavigate: (tab: ControlTab) => void;
  onPrefetch?: (tab: ControlTab) => void;
  commandButtonRef?: React.RefObject<HTMLButtonElement | null>;
  onOpenCommand: () => void;
  health: Props["health"];
}

/** ≥`tab` (600px): 88px persistente Rail — ersetzt den alten Density-Fork
 *  für alle Breiten von Medium bis Expanded/1920. */
function Rail({ active, openProposals, inboxTotal, inboxTone, libraryUnread, strategistCount, onNavigate, onPrefetch, commandButtonRef, onOpenCommand, health }: RailProps) {
  // Aktiver Tab außerhalb der 5 Primaries → als 6. Slot pinnen, damit der
  // aktuelle Standort auf der Rail immer sichtbar bleibt.
  const pinned = tabs.some((tab) => tab.id === active) ? undefined : navLookup[active];
  return (
    <nav
      aria-label="Hauptnavigation"
      // -mt-*: die App-Hülle trägt oberhalb von .hc-page ein responsives
      // pt-2/pt-4/pt-6, .hc-page selbst kompensiert nur pauschal -0.5rem
      // (margin-top) — ab `sm` bleibt ein Rest von 0.5rem, ab `lg` von 1rem,
      // der die volle `h-dvh`-Rail vorm ersten Scroll unten kappt (ihr
      // py-4-Boden mit Gateway-LED/„Mehr" lag 0.5–1rem unterm Fold). Die
      // Rail hier exakt um den Rest hochziehen bringt sie flush an den
      // echten Viewport-Rand, ohne die verifizierte volle Sticky-Höhe
      // anzutasten — Masthead/Main bleiben unverändert (kein Redesign).
      className="hidden tab:flex sticky top-0 z-40 h-dvh w-[5.5rem] shrink-0 flex-col items-center justify-between border-r border-line bg-surface-1 px-2 py-4 sm:-mt-2 lg:-mt-4"
    >
      <div className="flex w-full flex-col items-center gap-1">
        <div className="mb-2 grid h-11 w-11 place-items-center rounded-card border border-live bg-live/10 text-live">
          <Sparkles className="h-5 w-5" />
        </div>
        {tabs.map((tab) => (
          <RailItem
            key={tab.id}
            icon={tab.icon}
            label={tab.label}
            active={active === tab.id}
            badge={tabBadge(tab.id, openProposals, inboxTotal, inboxTone, libraryUnread, strategistCount)}
            onClick={() => onNavigate(tab.id)}
            onPrefetch={() => onPrefetch?.(tab.id)}
          />
        ))}
        {pinned ? (
          <RailItem
            key={pinned.id}
            icon={pinned.icon}
            label={pinned.label}
            active
            pinned
            to={pinned.path}
            badge={tabBadge(pinned.id, openProposals, inboxTotal, inboxTone, libraryUnread, strategistCount)}
            onPrefetch={() => onPrefetch?.(pinned.id)}
          />
        ) : null}
        <button
          ref={commandButtonRef}
          type="button"
          title="Command Palette"
          aria-label="Command Palette"
          onClick={onOpenCommand}
          className="mt-1 grid h-11 w-11 place-items-center rounded-card border border-transparent text-ink-2 hover:border-live hover:bg-live/10 hover:text-live"
        >
          <Command className="h-5 w-5" />
        </button>
        <RailMoreFlyout />
      </div>
      <GatewayLed health={health} />
    </nav>
  );
}

function RailItem({ icon: Icon, label, active, badge, to, onClick, onPrefetch, pinned }: {
  icon: React.ComponentType<{ className?: string }>;
  label: string;
  active: boolean;
  badge?: BadgeInfo | null;
  to?: string;
  onClick?: () => void;
  onPrefetch?: () => void;
  pinned?: boolean;
}) {
  const className = cn(
    "relative flex min-h-12 w-full flex-col items-center justify-center gap-1 rounded-card px-1 transition",
    active ? "bg-surface-2 text-ink" : "text-ink-3 hover:bg-surface-2 hover:text-ink-2",
  );
  const content = (
    <>
      {active ? <span className="absolute inset-y-1 left-0 w-[3px] rounded-full bg-live" /> : null}
      <Icon className="h-[22px] w-[22px]" />
      <span className={cn("text-[11px] leading-none", pinned && "w-full truncate text-center")}>{label}</span>
      {badge ? <span className={cn("hc-badge absolute -right-1 -top-1", badge.cls)}>{badge.count}</span> : null}
    </>
  );
  if (to) {
    return (
      <Link to={to} title={label} aria-current={active ? "page" : undefined} onMouseEnter={onPrefetch} onFocus={onPrefetch} className={className}>
        {content}
      </Link>
    );
  }
  return (
    <button type="button" title={label} aria-current={active ? "page" : undefined} onClick={onClick} onMouseEnter={onPrefetch} onFocus={onPrefetch} className={className}>
      {content}
    </button>
  );
}

/** Rail-"Mehr": leichtes anchored Flyout (kein Portal/Scroll-Lock — das
 *  bleibt der mobilen MoreSheet vorbehalten). Click togglet, Escape/Outside
 *  schließt (useDismissibleMenu), Fokus im Panel hält es offen. */
function RailMoreFlyout() {
  const { open, setOpen, ref } = useDismissibleMenu<HTMLDivElement>();
  const renderItem = (item: { label: string; path: string; icon: React.ComponentType<{ className?: string }> }) => {
    const Icon = item.icon;
    return (
      <Link key={item.path} to={item.path} onClick={() => setOpen(false)} className="flex min-h-11 items-center gap-2 rounded-card px-3 text-sm text-ink-2 hover:bg-surface-2 hover:text-ink">
        <Icon className="h-4 w-4" />{item.label}
      </Link>
    );
  };
  return (
    <div ref={ref} className="group relative mt-1">
      <button type="button" title="Mehr" aria-label="Mehr" aria-expanded={open} onClick={() => setOpen((current) => !current)} className="grid h-11 w-11 place-items-center rounded-card border border-transparent text-ink-2 hover:border-live hover:bg-live/10 hover:text-live">
        <MoreHorizontal className="h-5 w-5" />
      </button>
      <div
        data-testid="rail-more-flyout"
        className={cn(
          "fixed bottom-4 left-[6rem] z-50 w-56 max-h-[calc(100dvh-2rem)] overflow-y-auto overscroll-contain rounded-card border border-line bg-surface-1 p-2 shadow-xl transition-opacity duration-150 ease-out motion-reduce:transition-none",
          open ? "visible opacity-100" : "invisible opacity-0",
        )}
      >
        <p className="px-3 pb-1 pt-1 text-[10px] font-display uppercase tracking-[0.08em] text-ink-3">Ansichten</p>
        {moreTabs.map(renderItem)}
        <div className="my-1.5 border-t border-line" />
        {secondaryNav.map(renderItem)}
      </div>
    </div>
  );
}

/** Mobiles "Mehr": Bottom-Sheet über den Overlay-Portal-Wrapper — scrollbar,
 *  Scroll-Lock, große Tap-Flächen; gruppiert wie das Rail-Flyout. */
function MoreSheet({ onClose }: { onClose: () => void }) {
  const renderItem = (item: { label: string; path: string; icon: React.ComponentType<{ className?: string }> }) => {
    const Icon = item.icon;
    return (
      <Link key={item.path} to={item.path} onClick={onClose} className="flex min-h-11 items-center gap-2.5 rounded-card px-3 text-sm text-ink-2 hover:bg-surface-2 hover:text-ink">
        <Icon className="h-4 w-4" />{item.label}
      </Link>
    );
  };
  // Primäre Tabs außerhalb der 5-Slot-Bottom-Bar sind auf Mobile sonst nur per
  // Direkt-URL erreichbar — hier mit ins "Mehr"-Sheet holen (aktuell stets
  // leer, da `tabs` === `mobileTabs`; bleibt resilient für künftige Primaries).
  const overflowPrimary = tabs.filter((tab) => !mobileTabs.some((m) => m.id === tab.id));
  return (
    <Overlay onClose={onClose} ariaLabel="Mehr">
      <p className="text-[10px] font-display uppercase tracking-[0.08em] text-ink-3">Ansichten</p>
      <div className="mt-2 grid gap-0.5">{[...overflowPrimary, ...moreTabs].map(renderItem)}</div>
      <p className="mt-4 text-[10px] font-display uppercase tracking-[0.08em] text-ink-3">System</p>
      <div className="mt-2 grid gap-0.5">{secondaryNav.map(renderItem)}</div>
    </Overlay>
  );
}

interface BottomBarProps extends NavBadgeArgs {
  active: ControlTab;
  onNavigate: (tab: ControlTab) => void;
  onPrefetch?: (tab: ControlTab) => void;
  moreOpen: boolean;
  onToggleMore: () => void;
}

/** <`tab` (600px): fixed Bottom-Bar — funktional wie bisher (5 Primaries +
 *  Mehr → MoreSheet), Bronze-Restyle (surface-1/95 Blur, 2px Top-Indikator). */
function BottomBar({ active, openProposals, inboxTotal, inboxTone, libraryUnread, strategistCount, onNavigate, onPrefetch, moreOpen, onToggleMore }: BottomBarProps) {
  const moreActive = !tabs.some((tab) => tab.id === active);
  return (
    <nav
      aria-label="Navigation"
      className="tab:hidden fixed inset-x-0 bottom-0 z-40 border-t border-line bg-surface-1/95 px-2 pb-[env(safe-area-inset-bottom,0px)] backdrop-blur-xl"
    >
      <div className="grid grid-cols-6">
        {mobileTabs.map((tab) => (
          <TabButton
            key={tab.id}
            tab={tab}
            active={active === tab.id}
            badge={tabBadge(tab.id, openProposals, inboxTotal, inboxTone, libraryUnread, strategistCount)}
            onClick={() => onNavigate(tab.id)}
            onPrefetch={() => onPrefetch?.(tab.id)}
          />
        ))}
        <button
          type="button"
          onClick={onToggleMore}
          aria-label="Mehr"
          aria-expanded={moreOpen}
          className={cn("relative flex min-h-12 flex-col items-center justify-center gap-1 text-[11px] text-ink-3", moreActive && "text-live")}
        >
          <MoreHorizontal className="h-5 w-5" />
          <span className="max-w-full truncate px-0.5">Mehr</span>
        </button>
      </div>
    </nav>
  );
}

function CommandButton({ buttonRef, onOpen }: { buttonRef?: React.RefObject<HTMLButtonElement | null>; onOpen: () => void }) {
  // Der ⌘K-Hint lebt jetzt fest auf der Rail (≥tab) — hier also nur unterhalb
  // von `tab:` sichtbar. Die alte "erst ab sm zeigen"-Schranke (Touch-Phone-
  // Hint irrelevant) entfällt: `tab` (600px) liegt unter `sm` (640px), beide
  // Schranken zusammen hätten nie eine sichtbare Breite ergeben.
  return (
    <button ref={buttonRef} type="button" aria-label="Command Palette (⌘K)" className="hc-hit inline-flex items-center gap-2 rounded-card border border-line px-3 text-sm text-ink-2 hover:bg-surface-2 hover:text-ink tab:hidden" onClick={onOpen}>
      <Command className="h-4 w-4" />⌘K
    </button>
  );
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

/** Geteilte Health-Ableitung für StatusDots (Masthead) + GatewayLed (Rail) —
 *  eine Quelle für Gateway-Status/Stale/Zuletzt-aktuell-Titel. */
function useGatewayHealth(health: Props["health"]): { gateway: HealthStatus | "unknown"; stale: boolean; title: string } {
  const gateway: HealthStatus | "unknown" = health.data?.subsystems.gateway.status ?? (health.error ? "offline" : "unknown");
  const stale = Boolean(health.isStale);
  const clientNow = useClientNowSeconds();
  const checked = health.lastUpdated ? `Zuletzt aktuell vor ${Math.max(0, clientNow - health.lastUpdated)}s` : "Noch kein Health-Signal";
  const title = [health.error, checked].filter(Boolean).join(" · ");
  return { gateway, stale, title };
}

/** `demoted`: die Puls-Leiste trägt ihr eigenes Gateway-Instrument (ab `tab`) —
 *  wenn `pulse` (also echte Instrument-Werte) vorliegt, ist diese Legacy-Pille
 *  darunter redundant und zeigt erst ab `lg`, statt schon ab `md` zu doppeln. */
function StatusDots({ health, demoted }: { health: Props["health"]; demoted?: boolean }) {
  const { gateway, stale, title } = useGatewayHealth(health);
  const dashboard = health.data?.overall ?? (health.error ? "offline" : "unknown");
  return (
    <div data-testid="status-dots" title={title} className={cn("hidden items-center gap-2 rounded-full border border-line bg-surface-2 px-3 py-2 text-xs text-ink-2", demoted ? "lg:flex" : "md:flex")}>
      <span className={cn("hc-led h-2 w-2 rounded-full", healthLed(gateway, stale))} />Hermes<span className="font-data">:9119</span><span className="font-data text-ink-3">{healthLabel(gateway, stale)}</span>
      <span className={cn("hc-led h-2 w-2 rounded-full", healthLed(dashboard, stale))} />Dashboard<span className="font-data text-ink-3">{healthLabel(dashboard, stale)}</span>
    </div>
  );
}

/** Rail-Bottom-Cluster: Gateway-LED + Label (aus der StatusDots-Ableitung). */
function GatewayLed({ health }: { health: Props["health"] }) {
  const { gateway, stale, title } = useGatewayHealth(health);
  return (
    <div title={title} className="flex flex-col items-center gap-1 py-1">
      <span className={cn("hc-led h-2 w-2 rounded-full", healthLed(gateway, stale))} />
      <span className="text-[10px] text-ink-3">Gateway</span>
    </div>
  );
}

function TabButton({ tab, active, badge, onClick, onPrefetch }: { tab: (typeof tabs)[number]; active: boolean; badge: BadgeInfo | null; onClick: () => void; onPrefetch?: () => void }) {
  const Icon = tab.icon;
  return (
    <button
      type="button"
      onClick={onClick}
      onTouchStart={onPrefetch}
      onFocus={onPrefetch}
      aria-label={tab.label.includes(tab.mobileLabel) ? tab.label : `${tab.label} (${tab.mobileLabel})`}
      aria-current={active ? "page" : undefined}
      className={cn("relative flex min-h-12 flex-col items-center justify-center gap-1 text-[11px] text-ink-3", active && "text-live")}
    >
      {active ? <span className="absolute inset-x-2 top-0 h-0.5 rounded-full bg-live" /> : null}
      <Icon className="h-5 w-5" />
      <span className="max-w-full truncate px-0.5">{tab.mobileLabel}</span>
      {badge ? <span className={cn("hc-badge absolute right-4 top-2", badge.cls)}>{badge.count}</span> : null}
    </button>
  );
}
