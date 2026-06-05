import type { AutoresearchState, ToneName } from "./types";

export interface AutoresearchReadinessFact {
  label: string;
  value: string;
  tone: ToneName;
}

export interface AutoresearchReadinessSummary {
  tone: ToneName;
  label: string;
  title: string;
  detail: string;
  next: string;
  facts: AutoresearchReadinessFact[];
}

export function getAutoresearchReadiness(input: {
  state?: AutoresearchState | null;
  routeStatus?: string | null;
  heartbeatFresh?: boolean | null;
  loopRunning: boolean;
  openCount: number;
  highPriorityCount: number;
  busy: boolean;
}): AutoresearchReadinessSummary {
  const routeOk = input.routeStatus === "configured";
  const statusUnknown = !input.state && !input.routeStatus;
  const facts = readinessFacts(input);

  if (statusUnknown) {
    return {
      tone: "amber",
      label: "Status laden",
      title: "Noch nicht starten.",
      detail: "Der Autoresearch-Status ist noch nicht vollständig geladen. Das Cockpit kann den sicheren nächsten Schritt erst danach bewerten.",
      next: "Kurz warten oder Status neu laden; danach Entscheidungen oder Probelauf wählen.",
      facts,
    };
  }

  if (input.state === "crashed") {
    return {
      tone: "red",
      label: "Fehler prüfen",
      title: "Der Betrieb ist nicht sauber.",
      detail: "Der Loop meldet einen Crash. Neue Läufe würden den Fehler nur überdecken.",
      next: "Status, Activity und letzten Lauf prüfen; erst nach Klärung neu starten.",
      facts,
    };
  }

  if (!routeOk) {
    return {
      tone: "amber",
      label: "Route prüfen",
      title: "Noch kein sinnvoller Probelauf.",
      detail: "Die Modellroute ist nicht bestätigt. Ohne passende Route kann Autoresearch keine verlässlichen Kandidaten erzeugen.",
      next: "Modellroute prüfen, dann erst einen kleinen Dry-Run starten.",
      facts,
    };
  }

  if (input.loopRunning) {
    return {
      tone: "cyan",
      label: "Lauf aktiv",
      title: "Beobachten statt neu starten.",
      detail: "Autoresearch arbeitet gerade. Ein zweiter Start wäre für einen Operator schwer zu verfolgen.",
      next: "Loop beobachten; neue Karten danach bewusst entscheiden.",
      facts,
    };
  }

  if (input.busy) {
    return {
      tone: "violet",
      label: "Aktion läuft",
      title: "Kurz warten.",
      detail: "Eine Cockpit-Aktion aktualisiert gerade Entscheidungen, Status oder Laufdaten.",
      next: "Warten, bis die Rückmeldung sichtbar ist; danach den nächsten Schritt wählen.",
      facts,
    };
  }

  if (input.openCount > 0) {
    const hasHighPriority = input.highPriorityCount > 0;
    return {
      tone: hasHighPriority ? "amber" : "emerald",
      label: "Review bereit",
      title: hasHighPriority ? "Erst die wichtigen Karten entscheiden." : "Entscheidungen sind bereit.",
      detail: hasHighPriority
        ? "Es liegen Hoch+-Karten offen. Sie brauchen zuerst bewusstes Einzelreview."
        : "Es liegen geprüfte Vorschläge vor. Das ist der normale, sichere Arbeitsmodus.",
      next: "Offene Karten bearbeiten; danach erst neue Kandidaten holen oder den nächsten Probelauf starten.",
      facts,
    };
  }

  return {
    tone: "emerald",
    label: "Betriebsbereit",
    title: "Bereit für einen kleinen Probelauf.",
    detail: "Route und Loop sehen ruhig aus, und es warten keine offenen Entscheidungen.",
    next: "Preset wählen und Dry-Run starten; die Ergebnisse erscheinen als neue Karten.",
    facts,
  };
}

function readinessFacts(input: {
  routeStatus?: string | null;
  heartbeatFresh?: boolean | null;
  loopRunning: boolean;
  openCount: number;
  highPriorityCount: number;
  busy: boolean;
}): AutoresearchReadinessFact[] {
  return [
    {
      label: "Route",
      value: input.routeStatus === "configured" ? "bereit" : input.routeStatus || "unbekannt",
      tone: input.routeStatus === "configured" ? "emerald" : "amber",
    },
    {
      label: "Loop",
      value: input.loopRunning ? "läuft" : input.busy ? "aktualisiert" : "ruhig",
      tone: input.loopRunning ? "cyan" : input.busy ? "violet" : "emerald",
    },
    {
      label: "Offen",
      value: input.openCount === 0 ? "leer" : `${input.openCount} offen`,
      tone: input.openCount === 0 ? "emerald" : "cyan",
    },
    {
      label: "Hoch+",
      value: String(input.highPriorityCount),
      tone: input.highPriorityCount > 0 ? "amber" : "zinc",
    },
    {
      label: "Heartbeat",
      value: input.heartbeatFresh == null ? "unbekannt" : input.heartbeatFresh ? "frisch" : "stale",
      tone: input.heartbeatFresh == null ? "zinc" : input.heartbeatFresh ? "emerald" : "amber",
    },
  ];
}
