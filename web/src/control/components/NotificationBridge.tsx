import { useEffect, useMemo, useRef, useState } from "react";
import { Bell, BellOff } from "lucide-react";
import { cn } from "@/lib/utils";
import type { DecisionInboxData } from "../hooks/useControlData";

const STORAGE_KEY = "hermes.control.notifications.enabled";
const DEFAULT_TITLE = "Hermes Control";

function canNotify(): boolean {
  return typeof window !== "undefined" && "Notification" in window;
}

function readEnabled(): boolean {
  if (typeof window === "undefined") return false;
  return window.localStorage.getItem(STORAGE_KEY) === "true";
}

export function NotificationBridge({ inbox }: { inbox: DecisionInboxData }) {
  const [enabled, setEnabled] = useState(readEnabled);
  const [permission, setPermission] = useState<NotificationPermission>(() => canNotify() ? Notification.permission : "denied");
  const notifiedRef = useRef<Set<string>>(new Set());
  const redItems = useMemo(() => inbox.items.filter((item) => item.tone === "red"), [inbox.items]);

  useEffect(() => {
    document.title = inbox.summary.total > 0 ? `(${inbox.summary.total}) ${DEFAULT_TITLE}` : DEFAULT_TITLE;
    return () => {
      document.title = DEFAULT_TITLE;
    };
  }, [inbox.summary.total]);

  useEffect(() => {
    window.localStorage.setItem(STORAGE_KEY, String(enabled));
  }, [enabled]);

  useEffect(() => {
    if (!enabled || permission !== "granted" || !canNotify()) return;
    for (const item of redItems) {
      if (notifiedRef.current.has(item.key)) continue;
      notifiedRef.current.add(item.key);
      new Notification("Rote Entscheidung wartet", {
        body: item.title,
        tag: `hermes-control-${item.key}`,
      });
    }
  }, [enabled, permission, redItems]);

  const toggle = async () => {
    if (!canNotify()) {
      setEnabled(false);
      setPermission("denied");
      return;
    }
    if (enabled) {
      setEnabled(false);
      return;
    }
    const nextPermission = Notification.permission === "default"
      ? await Notification.requestPermission()
      : Notification.permission;
    setPermission(nextPermission);
    setEnabled(nextPermission === "granted");
  };

  return (
    <button
      type="button"
      onClick={() => void toggle()}
      title={enabled ? "Browser-Benachrichtigungen aus" : "Browser-Benachrichtigungen an"}
      aria-pressed={enabled}
      className={cn(
        "fixed bottom-[calc(5.5rem+env(safe-area-inset-bottom,0px))] right-3 z-40 grid h-10 w-10 place-items-center rounded-full border text-xs shadow-xl backdrop-blur lg:bottom-4",
        enabled ? "border-[var(--hc-accent-border)] bg-[var(--hc-accent-wash)] text-[var(--hc-accent-text)]" : "border-white/10 bg-black/30 hc-soft hover:bg-white/5",
      )}
    >
      {enabled ? <Bell className="h-4 w-4" /> : <BellOff className="h-4 w-4" />}
    </button>
  );
}
