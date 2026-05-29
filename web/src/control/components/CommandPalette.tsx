import { useEffect, useMemo, useRef, useState } from "react";
import { ArrowRight, Bot, FlaskConical, LayoutDashboard, Search, Shield } from "lucide-react";
import { cn } from "@/lib/utils";
import { KEYMAP } from "../lib/keymap";
import type { AgentLive, Worker } from "../lib/types";

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
  agents: AgentLive[];
  onClose: () => void;
  onNavigate: (path: string) => void;
  onGenerate: () => void;
  onApplyAll: () => void;
  triggerRef?: React.RefObject<HTMLElement | null>;
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

export function CommandPalette({ open, workers, agents, onClose, onNavigate, onGenerate, onApplyAll, triggerRef }: Props) {
  const [query, setQuery] = useState("");
  const [active, setActive] = useState(0);
  const inputRef = useRef<HTMLInputElement>(null);
  const dialogRef = useRef<HTMLDivElement>(null);

  const items = useMemo<CommandItem[]>(() => {
    const nav: CommandItem[] = [
      { id: "nav-overview", group: "Navigation", label: "Übersicht", hint: "/control", icon: <LayoutDashboard className="h-4 w-4" />, action: () => onNavigate("/control") },
      { id: "nav-hermes", group: "Navigation", label: "Hermes-Worker", hint: "/control/hermes", icon: <Bot className="h-4 w-4" />, action: () => onNavigate("/control/hermes") },
      { id: "nav-openclaw", group: "Navigation", label: "OpenClaw-Worker", hint: "/control/openclaw", icon: <Shield className="h-4 w-4" />, action: () => onNavigate("/control/openclaw") },
      { id: "nav-autoresearch", group: "Navigation", label: "Autoresearch", hint: "/control/autoresearch", icon: <FlaskConical className="h-4 w-4" />, action: () => onNavigate("/control/autoresearch") },
    ];
    const more = secondary.map(([label, path]) => ({ id: `more-${path}`, group: "Mehr", label, hint: path, icon: <ArrowRight className="h-4 w-4" />, action: () => onNavigate(path) }));
    const actions: CommandItem[] = [
      { id: "act-generate", group: "Aktionen", label: "Verbesserungen holen", hint: "Autoresearch", icon: <Search className="h-4 w-4" />, action: onGenerate },
      { id: "act-apply-all", group: "Aktionen", label: "Alle übernehmen", hint: "offene Skill-Vorschläge", icon: <ArrowRight className="h-4 w-4" />, action: onApplyAll },
    ];
    const workerItems = workers.map((w) => ({ id: `worker-${w.run_id}`, group: "Worker", label: w.task_title || w.run_id, hint: w.profile, icon: <Bot className="h-4 w-4" />, action: () => onNavigate("/control/hermes") }));
    const agentItems = agents.map((a) => ({ id: `agent-${a.id}`, group: "Agenten", label: `${a.emoji} ${a.name}`, hint: a.roleLabel, icon: <Shield className="h-4 w-4" />, action: () => onNavigate("/control/openclaw") }));
    return [...nav, ...more, ...actions, ...workerItems, ...agentItems];
  }, [agents, onApplyAll, onGenerate, onNavigate, workers]);

  const filtered = useMemo(() => {
    const q = query.trim().toLowerCase();
    if (!q) return items;
    return items.filter((item) => `${item.group} ${item.label} ${item.hint ?? ""}`.toLowerCase().includes(q));
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
          <span>↑/↓ auswählen · Enter öffnen</span>
          <span>⌘K / Ctrl+K</span>
        </div>
      </div>
    </div>
  );
}

