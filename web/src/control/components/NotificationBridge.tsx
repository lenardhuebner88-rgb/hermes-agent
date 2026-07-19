import { useEffect, useMemo, useRef, useState } from "react";
import { Bell, BellOff } from "lucide-react";
import { fetchJSON } from "@/lib/api";
import { cn } from "@/lib/utils";
import { de } from "../i18n/de";
import type { DecisionInboxData } from "../hooks/decisionInbox";

const STORAGE_KEY = "hermes.control.notifications.enabled";
const DEFAULT_TITLE = "Hermes Control";

type PushStatus = "unsupported" | "off" | "on" | "busy" | "blocked" | "unconfigured" | "error";

function canNotify(): boolean {
  return typeof window !== "undefined" && "Notification" in window;
}

function canPush(): boolean {
  return canNotify()
    && typeof navigator !== "undefined"
    && "serviceWorker" in navigator
    && "PushManager" in window;
}

function readEnabled(): boolean {
  if (typeof window === "undefined") return false;
  return window.localStorage.getItem(STORAGE_KEY) === "true";
}

function urlBase64ToUint8Array(value: string): Uint8Array<ArrayBuffer> {
  const padding = "=".repeat((4 - (value.length % 4)) % 4);
  const base64 = (value + padding).replace(/-/g, "+").replace(/_/g, "/");
  const raw = window.atob(base64);
  const output = new Uint8Array(raw.length);
  for (let i = 0; i < raw.length; i += 1) output[i] = raw.charCodeAt(i);
  return output;
}

function subscriptionPayload(subscription: PushSubscription): {
  endpoint: string;
  keys: { p256dh: string; auth: string };
} {
  const json = subscription.toJSON() as {
    endpoint?: string;
    keys?: { p256dh?: string; auth?: string };
  };
  if (!json.endpoint || !json.keys?.p256dh || !json.keys.auth) {
    throw new Error("push subscription is missing endpoint or keys");
  }
  return {
    endpoint: json.endpoint,
    keys: {
      p256dh: json.keys.p256dh,
      auth: json.keys.auth,
    },
  };
}

async function postSubscription(subscription: PushSubscription): Promise<void> {
  await fetchJSON("/api/plugins/kanban/push/subscribe", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(subscriptionPayload(subscription)),
  }, { timeoutMs: 0 });
}

async function postUnsubscribe(subscription: PushSubscription): Promise<void> {
  await fetchJSON("/api/plugins/kanban/push/unsubscribe", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ endpoint: subscription.endpoint }),
  }, { timeoutMs: 0 });
}

export function NotificationBridge({ inbox }: { inbox: DecisionInboxData }) {
  const [enabled, setEnabled] = useState(readEnabled);
  const [permission, setPermission] = useState<NotificationPermission>(() => canNotify() ? Notification.permission : "denied");
  const [pushStatus, setPushStatus] = useState<PushStatus>(() => canPush() ? "off" : "unsupported");
  const notifiedRef = useRef<Set<string>>(new Set());
  const seededRef = useRef(false);
  const redItems = useMemo(() => inbox.items.filter((item) => item.tone === "red"), [inbox.items]);
  const busy = pushStatus === "busy";

  // Beim ersten vollständig geladenen Inbox-Stand die schon vorhandenen roten
  // Items als "gesehen" seeden — sonst löst das Aktivieren der Glocke (oder ein
  // Reload mit persistiertem Opt-in) eine Notification-Flut für alte
  // Entscheidungen aus. Benachrichtigt wird nur, was NACH dem Seed neu auftaucht.
  useEffect(() => {
    if (seededRef.current || inbox.loading) return;
    seededRef.current = true;
    for (const item of redItems) notifiedRef.current.add(item.key);
  }, [inbox.loading, redItems]);

  useEffect(() => {
    document.title = inbox.summary.total > 0 ? `(${inbox.summary.total}) ${DEFAULT_TITLE}` : DEFAULT_TITLE;
    return () => {
      document.title = DEFAULT_TITLE;
    };
  }, [inbox.summary.total]);

  useEffect(() => {
    window.localStorage.setItem(STORAGE_KEY, String(enabled));
  }, [enabled]);

  // I3 visibility heartbeat: tell the agent-questions store that a /control
  // tab is visible so open-question web-push is suppressed. Shared across all
  // control surfaces (this component mounts in ControlShell).
  useEffect(() => {
    if (typeof document === "undefined") return;
    let cancelled = false;
    const postVisibility = () => {
      if (cancelled || document.hidden) return;
      // 5s timeout: a hanging server must not queue up fire-and-forget
      // heartbeats behind each other (Kimi review m8).
      void fetchJSON("/api/agent-questions/visibility", { method: "POST" }, { timeoutMs: 5000 }).catch(
        () => undefined,
      );
    };
    postVisibility();
    const intervalId = window.setInterval(postVisibility, 15_000);
    const onVisibility = () => {
      if (!document.hidden) postVisibility();
    };
    document.addEventListener("visibilitychange", onVisibility);
    return () => {
      cancelled = true;
      window.clearInterval(intervalId);
      document.removeEventListener("visibilitychange", onVisibility);
    };
  }, []);

  useEffect(() => {
    if (!seededRef.current || !enabled || permission !== "granted" || !canNotify()) return;
    if (canPush() && pushStatus === "on") return;
    for (const item of redItems) {
      if (notifiedRef.current.has(item.key)) continue;
      notifiedRef.current.add(item.key);
      new Notification(de.notifications.localTitle, {
        body: item.title,
        tag: `hermes-control-${item.key}`,
      });
    }
  }, [enabled, permission, pushStatus, redItems]);

  useEffect(() => {
    let cancelled = false;
    if (!canPush()) {
      // eslint-disable-next-line react-hooks/set-state-in-effect -- einmalige Capability-Detection beim Mount (kein externes Subscribe möglich, wenn Push fehlt)
      setPushStatus("unsupported");
      return;
    }
    navigator.serviceWorker.ready
      .then((registration) => registration.pushManager.getSubscription())
      .then((subscription) => {
        if (cancelled) return;
        if (subscription) {
          setPushStatus("on");
          setEnabled(true);
        } else {
          setPushStatus("off");
        }
      })
      .catch(() => {
        if (!cancelled) setPushStatus("error");
      });
    return () => {
      cancelled = true;
    };
  }, []);

  const enableNotifications = async () => {
    if (!canNotify()) {
      setEnabled(false);
      setPermission("denied");
      setPushStatus("unsupported");
      return;
    }
    const nextPermission = Notification.permission === "default"
      ? await Notification.requestPermission()
      : Notification.permission;
    setPermission(nextPermission);
    if (nextPermission !== "granted") {
      setEnabled(false);
      setPushStatus("blocked");
      return;
    }
    if (!canPush()) {
      setEnabled(true);
      setPushStatus("unsupported");
      return;
    }
    setPushStatus("busy");
    try {
      const vapid = await fetchJSON<{ enabled?: boolean; public_key?: string | null }>("/api/plugins/kanban/push/vapid-public-key");
      if (!vapid.enabled || !vapid.public_key) {
        setEnabled(false);
        setPushStatus("unconfigured");
        return;
      }
      const registration = await navigator.serviceWorker.ready;
      let subscription = await registration.pushManager.getSubscription();
      if (!subscription) {
        subscription = await registration.pushManager.subscribe({
          userVisibleOnly: true,
          applicationServerKey: urlBase64ToUint8Array(vapid.public_key),
        });
      }
      await postSubscription(subscription);
      setEnabled(true);
      setPushStatus("on");
    } catch {
      setEnabled(false);
      setPushStatus("error");
    }
  };

  const disableNotifications = async () => {
    if (canPush()) {
      setPushStatus("busy");
      try {
        const registration = await navigator.serviceWorker.ready;
        const subscription = await registration.pushManager.getSubscription();
        if (subscription) {
          await postUnsubscribe(subscription).catch(() => undefined);
          await subscription.unsubscribe().catch(() => false);
        }
        setPushStatus("off");
      } catch {
        setPushStatus("error");
      }
    }
    setEnabled(false);
  };

  const toggle = async () => {
    if (busy) return;
    if (enabled) {
      await disableNotifications();
      return;
    }
    await enableNotifications();
  };

  // Sichtbares Feedback statt stillem "aus": denied/dismissed Permission
  // (z. B. versehentlich weggeklickte Browser-Abfrage) muss der Nutzer in
  // den Site-Einstellungen lösen — ein erneuter Tap allein kann das nicht.
  const statusLabel = enabled
    ? de.notifications.statusOn
    : pushStatus === "blocked"
      ? de.notifications.statusBlocked
      : pushStatus === "error"
        ? de.notifications.statusError
        : de.notifications.statusOff;
  const title = enabled
    ? de.notifications.disable
    : pushStatus === "blocked"
      ? de.notifications.blockedHint
      : pushStatus === "error"
        ? de.notifications.errorHint
        : pushStatus === "unconfigured"
          ? de.notifications.unconfigured
          : de.notifications.enable;

  return (
    // Inline-Button im Shell-Header (nicht mehr schwebend): der frühere
    // `fixed`-FAB überlagerte auf Mobil Karten/Sheets und kollidierte mit dem
    // Capture-FAB (der 9.5rem-Stack-Hack). Im Header rechts ist auf Mobil ohnehin
    // Platz (CommandButton/MoreNav/StatusDots sind dort versteckt).
    <button
      type="button"
      onClick={() => void toggle()}
      title={title}
      aria-label={title}
      aria-pressed={enabled}
      disabled={busy}
      className={cn(
        "inline-flex h-11 min-w-[4.5rem] shrink-0 items-center justify-center gap-1.5 rounded-lg border px-2 text-xs transition disabled:opacity-60",
        enabled
          ? "border-[var(--hc-accent-border)] bg-[var(--hc-accent-wash)] text-[var(--hc-accent-text)]"
          : pushStatus === "blocked" || pushStatus === "error"
            ? "border-amber-500/40 bg-amber-500/10 text-amber-100"
            : "border-white/10 hc-soft hover:bg-white/5",
      )}
    >
      {enabled ? <Bell className="h-4 w-4" /> : <BellOff className="h-4 w-4" />}
      <span className="font-medium">{busy ? de.notifications.busy : statusLabel}</span>
    </button>
  );
}
