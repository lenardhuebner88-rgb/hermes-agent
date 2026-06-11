import { useEffect, useMemo, useRef, useState } from "react";
import { Activity, ArrowRight, Bot, Clock, FlaskConical, GitBranch, Inbox, KanbanSquare, LayoutDashboard, Search, Workflow } from "lucide-react";
import { cn } from "@/lib/utils";
import { getSnapshot, subscribe } from "../hooks/pollingStore";
import { boardLoader, cronObservabilityLoader, epicsLoader } from "../hooks/useControlData";
import { KEYMAP } from "../lib/keymap";
import type { BacklogResponse, EpicsResponse, OrchestrationBacklogResponse } from "../lib/schemas";
import type { BoardResponse, CronObservabilityResponse, Worker } from "../lib/types";

export interface CommandItem {
  id: string;
  group: string;
  label: string;
  hint?: string;
  icon?: React.ReactNode;
  action: () => void;
}

interface Props {
  open: boolean;
  workers: Worker[];
  onClose: () => void;
  onNavigate: (path: string) => void;
  onGenerate: () => void;
  onApplyAll: () => void;
  triggerRef?: React.RefObject<HTMLElement | null>;
}

export interface CommandPaletteSnapshots {
  board: BoardResponse | null;
  crons: CronObservabilityResponse | null;
  backlog: BacklogResponse | null;
  orchestration: OrchestrationBacklogResponse | null;
  epics: EpicsResponse | null;
}

interface CommandPaletteItemInput {
  workers: Worker[];
  snapshots: CommandPaletteSnapshots;
  onNavigate: (path: string) => void;
  onGenerate: () => void;
  onApplyAll: () => void;
}

const secondary = [
  ["Sessions", "/sessions"],
  ["Kanban", "/plugins"],
  ["Modelle", "/models"],
  ["Logs", "/logs"],
  ["Cron", "/cron"],
  ["Skills", "/skills"],
  ["Konfiguration", "/config"],
] as const;

const GROUP_LIMIT = 8;

export function readCommandPaletteSnapshots(): CommandPaletteSnapshots {
  return {
    board: getSnapshot<BoardResponse>("kanban/board")?.data ?? null,
    crons: getSnapshot<CronObservabilityResponse>("cron/observability")?.data ?? null,
    backlog: getSnapshot<BacklogResponse>("family-organizer/backlog")?.data ?? null,
    orchestration: getSnapshot<OrchestrationBacklogResponse>("orchestration/backlog")?.data ?? null,
    epics: getSnapshot<EpicsResponse>("kanban/epics")?.data ?? null,
  };
}

export function buildCommandPaletteItems({ workers, snapshots, onNavigate, onGenerate, onApplyAll }: CommandPaletteItemInput): CommandItem[] {
  const taskItems: CommandItem[] = snapshots.board?.columns.flatMap((column) =>
    column.tasks.map((task) => ({
      id: `task-${task.id}`,
      group: "Tasks",
      label: task.title || task.id,
      hint: `${task.id} · ${task.status}`,
      icon: <KanbanSquare className="h-4 w-4" />,
      action: () => onNavigate(`/control/flow?task=${encodeURIComponent(task.id)}`),
    })),
  ) ?? [];
  const cronItems: CommandItem[] = snapshots.crons?.jobs.map((job) => ({
    id: `cron-${job.id}`,
    group: "Crons",
    label: job.name || job.id,
    hint: [job.id, job.state, job.last_status].filter(Boolean).join(" · "),
    icon: <Clock className="h-4 w-4" />,
    action: () => onNavigate("/control/crons"),
  })) ?? [];
  const backlogItems: CommandItem[] = [
    ...(snapshots.backlog?.items.map((item) => ({
      id: `backlog-${item.id}`,
      group: "Backlog",
      label: item.title || item.id,
      hint: `${item.id} · ${item.status}`,
      icon: <Inbox className="h-4 w-4" />,
      action: () => onNavigate(`/control/backlog?focus=${encodeURIComponent(item.id)}`),
    })) ?? []),
    ...(snapshots.orchestration?.items.map((item) => ({
      id: `orchestration-${item.id}`,
      group: "Backlog",
      label: item.title || item.id,
      hint: `${item.id} · ${item.status} · Orchestrator`,
      icon: <Workflow className="h-4 w-4" />,
      action: () => onNavigate(`/control/orchestrator?focus=${encodeURIComponent(item.id)}`),
    })) ?? []),
  ];
  const epicItems: CommandItem[] = snapshots.epics?.epics.map((epic) => ({
    id: `epic-${epic.id}`,
    group: "Epics",
    label: epic.title || epic.id,
    hint: `${epic.id} · ${epic.status} · ${epic.open_tasks}/${epic.task_count} offen`,
    icon: <GitBranch className="h-4 w-4" />,
    action: () => onNavigate(`/control/flow?epic=${encodeURIComponent(epic.id)}`),
  })) ?? [];
  const nav: CommandItem[] = [
    { id: "nav-inbox", group: "Navigation", label: "Postfach", hint: "/control · g i", icon: <Inbox className="h-4 w-4" />, action: () => onNavigate("/control") },
    { id: "nav-overview", group: "Navigation", label: "Übersicht", hint: "/control/overview · g u", icon: <LayoutDashboard className="h-4 w-4" />, action: () => onNavigate("/control/overview") },
    { id: "nav-workstreams", group: "Navigation", label: "Arbeitsströme", hint: "/control/workstreams · g s", icon: <GitBranch className="h-4 w-4" />, action: () => onNavigate("/control/workstreams") },
    { id: "nav-flow-workers", group: "Navigation", label: "Worker (Flow)", hint: "/control/flow · g f", icon: <Bot className="h-4 w-4" />, action: () => onNavigate("/control/flow") },
    { id: "nav-statistik", group: "Navigation", label: "Statistik", hint: "/control/statistik · g t", icon: <Activity className="h-4 w-4" />, action: () => onNavigate("/control/statistik") },
    { id: "nav-autoresearch", group: "Navigation", label: "Autoresearch", hint: "/control/autoresearch · g a", icon: <FlaskConical className="h-4 w-4" />, action: () => onNavigate("/control/autoresearch") },
    { id: "nav-pulse", group: "Navigation", label: "Pulse", hint: "/control/pulse · g p", icon: <Activity className="h-4 w-4" />, action: () => onNavigate("/control/pulse") },
    ...secondary.map(([label, path]) => ({ id: `more-${path}`, group: "Navigation", label, hint: path, icon: <ArrowRight className="h-4 w-4" />, action: () => onNavigate(path) })),
  ];
  const actions: CommandItem[] = [
    { id: "act-generate", group: "Aktionen", label: "Verbesserungen holen", hint: "Autoresearch", icon: <Search className="h-4 w-4" />, action: onGenerate },
    { id: "act-apply-all", group: "Aktionen", label: "Alle übernehmen", hint: "offene Skill-Vorschläge", icon: <ArrowRight className="h-4 w-4" />, action: onApplyAll },
  ];
  const workerItems = workers.map((w) => ({ id: `worker-${w.run_id}`, group: "Worker", label: w.task_title || w.run_id, hint: `${w.task_id} · ${w.task_status} · ${w.profile}`, icon: <Bot className="h-4 w-4" />, action: () => onNavigate(`/control/flow?task=${encodeURIComponent(w.task_id)}`) }));
  return [...taskItems, ...cronItems, ...backlogItems, ...epicItems, ...nav, ...actions, ...workerItems];
}

export function filterCommandPaletteItems(items: CommandItem[], query: string): CommandItem[] {
  const q = query.trim().toLowerCase();
  const groupCounts = new Map<string, number>();
  const out: CommandItem[] = [];
  for (const item of items) {
    if (q && !`${item.group} ${item.label} ${item.hint ?? ""}`.toLowerCase().includes(q)) continue;
    const count = groupCounts.get(item.group) ?? 0;
    if (count >= GROUP_LIMIT) continue;
    groupCounts.set(item.group, count + 1);
    out.push(item);
  }
  return out;
}

export function CommandPalette({ open, workers, onClose, onNavigate, onGenerate, onApplyAll, triggerRef }: Props) {
  const [query, setQuery] = useState("");
  const [active, setActive] = useState(0);
  const [snapshotTick, setSnapshotTick] = useState(0);
  const inputRef = useRef<HTMLInputElement>(null);
  const dialogRef = useRef<HTMLDivElement>(null);

  useEffect(() => {
    if (!open) return;
    setSnapshotTick((tick) => tick + 1);
    const timer = window.setInterval(() => setSnapshotTick((tick) => tick + 1), 1000);
    return () => window.clearInterval(timer);
  }, [open]);

  // Board/Crons/Epics werden sonst nur von ihren Views gemountet — auf einem
  // frischen /control-Load wäre die globale Suche dafür leer. Solange die
  // Palette offen ist, abonnieren wir die Quellen selbst (ref-counted, teilt
  // sich Timer/Requests mit ggf. offenen Views); der snapshotTick oben pickt
  // die eintreffenden Snapshots auf.
  useEffect(() => {
    if (!open) return;
    const unsubs = [
      subscribe("kanban/board", boardLoader, 8000, () => {}),
      subscribe("cron/observability", cronObservabilityLoader, 30000, () => {}),
      subscribe("kanban/epics", epicsLoader, 15000, () => {}),
    ];
    return () => {
      for (const unsub of unsubs) unsub();
    };
  }, [open]);

  const items = useMemo<CommandItem[]>(() => {
    void snapshotTick;
    return buildCommandPaletteItems({ workers, snapshots: readCommandPaletteSnapshots(), onNavigate, onGenerate, onApplyAll });
  }, [onApplyAll, onGenerate, onNavigate, snapshotTick, workers]);

  const filtered = useMemo(() => {
    return filterCommandPaletteItems(items, query);
  }, [items, query]);

  useEffect(() => {
    if (!open) return;
    window.setTimeout(() => inputRef.current?.focus(), 0);
  }, [open]);

  useEffect(() => {
    if (!open) return;
    const onKey = (event: KeyboardEvent) => {
      if (KEYMAP.palette.close.includes(event.key as "Escape")) {
        event.preventDefault();
        onClose();
        triggerRef?.current?.focus();
        return;
      }
      if (KEYMAP.palette.next.includes(event.key as "ArrowDown")) {
        event.preventDefault();
        setActive((idx) => Math.min(filtered.length - 1, idx + 1));
      }
      if (KEYMAP.palette.prev.includes(event.key as "ArrowUp")) {
        event.preventDefault();
        setActive((idx) => Math.max(0, idx - 1));
      }
      if (KEYMAP.palette.confirm.includes(event.key as "Enter") && filtered[active]) {
        event.preventDefault();
        filtered[active].action();
        onClose();
      }
      if (event.key === "Tab" && dialogRef.current) {
        const focusable = Array.from(dialogRef.current.querySelectorAll<HTMLElement>("button,[href],input,[tabindex]:not([tabindex='-1'])")).filter((el) => !el.hasAttribute("disabled"));
        if (focusable.length === 0) return;
        const first = focusable[0];
        const last = focusable[focusable.length - 1];
        if (event.shiftKey && document.activeElement === first) { event.preventDefault(); last.focus(); }
        else if (!event.shiftKey && document.activeElement === last) { event.preventDefault(); first.focus(); }
      }
    };
    window.addEventListener("keydown", onKey);
    return () => window.removeEventListener("keydown", onKey);
  }, [active, filtered, onClose, open, triggerRef]);

  if (!open) return null;
  let lastGroup = "";

  return (
    <div className="fixed inset-0 z-50 grid place-items-start bg-black/70 px-3 pt-[12vh] backdrop-blur-sm" role="presentation" onMouseDown={(event) => { if (event.target === event.currentTarget) onClose(); }}>
      <div ref={dialogRef} role="dialog" aria-modal="true" aria-label="Command Palette" className="mx-auto w-full max-w-2xl overflow-hidden rounded-xl border border-[var(--hc-border-strong)] bg-[var(--hc-panel)] shadow-2xl" onMouseDown={(event) => event.stopPropagation()}>
        <div className="flex min-h-14 items-center gap-3 border-b border-[var(--hc-border)] px-4">
          <Search className="h-4 w-4 hc-dim" />
          <input ref={inputRef} value={query} onChange={(e) => { setQuery(e.target.value); setActive(0); }} placeholder="Springe zu..." className="h-12 flex-1 bg-transparent text-sm text-white outline-none placeholder:text-zinc-600" />
          <span className="rounded border border-white/10 px-2 py-1 text-xs hc-dim">Esc</span>
        </div>
        <div className="max-h-[60vh] overflow-auto p-2">
          {filtered.length === 0 ? <p className="px-3 py-6 text-sm hc-soft">Kein Treffer.</p> : filtered.map((item, index) => {
            const showGroup = item.group !== lastGroup;
            lastGroup = item.group;
            return (
              <div key={item.id}>
                {showGroup ? <p className="px-3 pb-1 pt-3 text-[10px] font-semibold uppercase tracking-[.18em] hc-dim">{item.group}</p> : null}
                <button type="button" aria-selected={active === index} onMouseEnter={() => setActive(index)} onClick={() => { item.action(); onClose(); }} className={cn("flex min-h-11 w-full items-center justify-between gap-3 rounded-lg px-3 py-2 text-left text-sm", active === index ? "bg-[var(--hc-accent-wash)] text-[var(--hc-accent-text)]" : "text-zinc-200 hover:bg-white/5")}>
                  <span className="flex min-w-0 items-center gap-3">{item.icon}<span className="truncate">{item.label}</span></span>
                  {item.hint ? <span className="hc-mono shrink-0 text-xs hc-dim">{item.hint}</span> : null}
                </button>
              </div>
            );
          })}
        </div>
        <div className="flex items-center justify-between border-t border-[var(--hc-border)] px-4 py-2 text-xs hc-dim">
          <span>↑/↓ auswählen · Enter öffnen · g i/f/t/a/u/p</span>
          <span>⌘K / Ctrl+K · / suchen</span>
        </div>
      </div>
    </div>
  );
}
