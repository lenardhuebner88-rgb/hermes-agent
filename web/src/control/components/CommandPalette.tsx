import { useEffect, useMemo, useRef, useState } from "react";
import { Search } from "lucide-react";
import { cn } from "@/lib/utils";
import { subscribe } from "../hooks/pollingStore";
import { boardLoader, cronObservabilityLoader, epicsLoader } from "../hooks/useControlData";
import { KEYMAP } from "../lib/keymap";
import type { Worker } from "../lib/types";
import { buildCommandPaletteItems, filterCommandPaletteItems, readCommandPaletteSnapshots, type CommandItem } from "./commandPaletteItems";
import { Eyebrow } from "./primitives";

interface Props {
  open: boolean;
  workers: Worker[];
  onClose: () => void;
  onNavigate: (path: string) => void;
  onGenerate: () => void;
  onApplyAll: () => void;
  triggerRef?: React.RefObject<HTMLElement | null>;
}

export function CommandPalette({ open, workers, onClose, onNavigate, onGenerate, onApplyAll, triggerRef }: Props) {
  const [query, setQuery] = useState("");
  const [active, setActive] = useState(0);
  const [snapshotTick, setSnapshotTick] = useState(0);
  const inputRef = useRef<HTMLInputElement>(null);
  const dialogRef = useRef<HTMLDivElement>(null);
  // Wer immer den Fokus trug, als die Palette aufging — nicht zwingend der
  // Rail-Button (commandButtonRef/triggerRef): auf Phones öffnet sie über den
  // Masthead-CommandButton, der keinen Ref trägt, `triggerRef` zeigte danach
  // auf einen `hidden`(display:none)-Rail-Button und der Fokus-Restore lief
  // ins Leere (Bug). Generisch statt Ref-spezifisch: einfach zurückgeben, wer
  // vorher fokussiert war — funktioniert für Rail-Button UND Masthead-Button
  // gleichermaßen, `triggerRef` bleibt als Prop nutzbar (Rail-Fall trägt sich
  // von selbst, da document.activeElement dort ohnehin der Rail-Button ist).
  const previouslyFocusedRef = useRef<HTMLElement | null>(null);

  // Sofort-Bump beim Öffnen als Render-Phase-Anpassung (React-Doku
  // "adjusting state when props change") — setState synchron im Effect-Body
  // verletzt react-hooks/set-state-in-effect. Der 1s-Intervall-Tick bleibt
  // im Effekt (Callback = asynchron, erlaubt).
  const [wasOpen, setWasOpen] = useState(open);
  if (open !== wasOpen) {
    setWasOpen(open);
    if (open) setSnapshotTick((tick) => tick + 1);
  }

  useEffect(() => {
    if (!open) return;
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

  // Fokus-Restore, generisch statt ref-spezifisch: wer immer den Fokus trug,
  // bevor die Palette aufging, bekommt ihn beim Schließen zurück — deckt
  // Escape, Backdrop-Klick und Item-Auswahl gleichermaßen ab (vorher restorte
  // nur der Escape-Pfad, und nur auf `triggerRef`, der auf Phones der
  // unsichtbare Rail-Button war). `triggerRef` bleibt als Fallback, falls beim
  // Öffnen kein activeElement einfangbar war.
  useEffect(() => {
    if (!open) return;
    previouslyFocusedRef.current = document.activeElement instanceof HTMLElement ? document.activeElement : null;
    const fallbackTrigger = triggerRef?.current ?? null;
    window.setTimeout(() => inputRef.current?.focus(), 0);
    return () => {
      const toRestore = previouslyFocusedRef.current ?? fallbackTrigger;
      if (toRestore && document.contains(toRestore)) toRestore.focus();
    };
  }, [open, triggerRef]);

  useEffect(() => {
    if (!open) return;
    const onKey = (event: KeyboardEvent) => {
      if (KEYMAP.palette.close.includes(event.key as "Escape")) {
        event.preventDefault();
        onClose();
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
  }, [active, filtered, onClose, open]);

  if (!open) return null;
  let lastGroup = "";

  return (
    <div className="fixed inset-0 z-50 grid place-items-start bg-surface-0/80 px-3 pt-[12vh] backdrop-blur-sm" role="presentation" onMouseDown={(event) => { if (event.target === event.currentTarget) onClose(); }}>
      <div ref={dialogRef} role="dialog" aria-modal="true" aria-label="Befehlspalette" className="mx-auto w-full max-w-2xl overflow-hidden rounded-panel border border-line bg-surface-1 shadow-2xl" onMouseDown={(event) => event.stopPropagation()}>
        <div className="flex min-h-14 items-center gap-3 border-b border-line px-4">
          <Search className="h-4 w-4 text-ink-3" />
          <input ref={inputRef} aria-label="Befehle durchsuchen" value={query} onChange={(e) => { setQuery(e.target.value); setActive(0); }} placeholder="Springe zu..." className="h-12 flex-1 bg-transparent text-body text-ink outline-none placeholder:text-ink-3" />
          <span className="rounded-card border border-line px-2 py-1 text-micro text-ink-3">Esc</span>
        </div>
        <div className="max-h-[60vh] overflow-auto p-2">
          {filtered.length === 0 ? <p className="px-3 py-6 text-sec text-ink-2">Kein Treffer.</p> : filtered.map((item, index) => {
            const showGroup = item.group !== lastGroup;
            lastGroup = item.group;
            return (
              <div key={item.id}>
                {showGroup ? <Eyebrow className="px-3 pb-1 pt-3">{item.group}</Eyebrow> : null}
                <button type="button" aria-selected={active === index} onMouseEnter={() => setActive(index)} onClick={() => { item.action(); onClose(); }} className={cn("flex min-h-12 w-full items-center justify-between gap-3 rounded-card px-3 py-2 text-left text-sec", active === index ? "bg-live/10 text-bronze-hi" : "text-ink-2 hover:bg-surface-3 hover:text-ink")}>
                  <span className="flex min-w-0 items-center gap-3">{item.icon}<span className="truncate">{item.label}</span></span>
                  {item.hint ? <span className="shrink-0 font-data text-micro text-ink-3">{item.hint}</span> : null}
                </button>
              </div>
            );
          })}
        </div>
        <div className="flex items-center justify-between border-t border-line px-4 py-2 text-micro text-ink-3">
          <span>↑/↓ auswählen · Enter öffnen · g i/f/t/a/u/p</span>
          <span>⌘K / Ctrl+K · / suchen</span>
        </div>
      </div>
    </div>
  );
}
