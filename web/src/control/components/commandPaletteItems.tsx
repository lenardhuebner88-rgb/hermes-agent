// Ausgelagert aus CommandPalette.tsx (react-refresh/only-export-components):
// Snapshot-Lese-, Item-Bau- und Filter-Logik der Palette, exportiert für
// Tests und getrennt von der Komponente (Fast-Refresh-Boundary).
import { Activity, ArrowRight, Bot, Clock, FlaskConical, GitBranch, Inbox, KanbanSquare, LayoutDashboard, Search, Workflow } from "lucide-react";
import { getSnapshot } from "../hooks/pollingStore";
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

