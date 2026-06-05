import type { ToneName } from "./types";

export type AutoresearchActionKey = "generate" | "scan" | "applySkills" | "prune";

export interface AutoresearchActionHint {
  tone: ToneName;
  label: string;
  reason: string;
  after: string;
}

export type AutoresearchActionPlan = Record<AutoresearchActionKey, AutoresearchActionHint>;

const WAIT: AutoresearchActionHint = {
  tone: "violet",
  label: "Warten",
  reason: "Eine Review-Aktion läuft bereits. Der aktuelle Stand wird gerade aktualisiert.",
  after: "Erst die Rückmeldung abwarten.",
};

export function getAutoresearchActionPlan(input: {
  routeOk: boolean;
  loopRunning: boolean;
  openCount: number;
  highPriorityCount: number;
  openSkillCount: number;
  openSkillManualReviewCount: number;
  revertedCount: number;
  storeBusy: boolean;
  pruneBusy: boolean;
}): AutoresearchActionPlan {
  if (input.storeBusy) {
    return {
      generate: WAIT,
      scan: WAIT,
      applySkills: WAIT,
      prune: WAIT,
    };
  }

  const queueHasWork = input.openCount > 0;
  const hasHighPriority = input.highPriorityCount > 0;
  const generate = actionForStart({
    routeOk: input.routeOk,
    loopRunning: input.loopRunning,
    queueHasWork,
    hasHighPriority,
    kind: "generate",
  });
  const scan = actionForStart({
    routeOk: input.routeOk,
    loopRunning: input.loopRunning,
    queueHasWork,
    hasHighPriority,
    kind: "scan",
  });

  return {
    generate,
    scan,
    applySkills: actionForSkillApply(input.openSkillCount, input.openSkillManualReviewCount, queueHasWork),
    prune: input.pruneBusy ? WAIT : actionForPrune(input.revertedCount, queueHasWork),
  };
}

function actionForStart(input: {
  routeOk: boolean;
  loopRunning: boolean;
  queueHasWork: boolean;
  hasHighPriority: boolean;
  kind: "generate" | "scan";
}): AutoresearchActionHint {
  const noun = input.kind === "generate" ? "neue Skill-Kandidaten" : "neue Code-Kandidaten";

  if (!input.routeOk) {
    return {
      tone: "amber",
      label: "Route prüfen",
      reason: "Die Modellroute ist nicht bestätigt. Neue Läufe liefern sonst kein verlässliches Ergebnis.",
      after: "Erst Route stabilisieren, dann klein starten.",
    };
  }
  if (input.loopRunning) {
    return {
      tone: "cyan",
      label: "Nicht parallel",
      reason: "Der Research-Loop arbeitet schon. Ein zweiter Start macht die offenen Entscheidungen unübersichtlich.",
      after: "Loop beobachten und danach neue Starts wählen.",
    };
  }
  if (input.queueHasWork) {
    return {
      tone: input.hasHighPriority ? "amber" : "cyan",
      label: input.hasHighPriority ? "Erst Hoch+" : "Erst entscheiden",
      reason: `Es liegen noch Entscheidungen an. Mehr ${noun} erhöhen nur den Review-Stau.`,
      after: "Offene Karten entscheiden, dann gezielt nachlegen.",
    };
  }
  return {
    tone: input.kind === "generate" ? "emerald" : "cyan",
    label: input.kind === "generate" ? "Empfohlen" : "Optional",
    reason: input.kind === "generate"
      ? "Entscheidungsbereich und Loop sind ruhig. Ein kleiner Skill-Lauf ist der leichteste nächste Schritt."
      : "Code-Scan ist sinnvoll, wenn du gezielt technische Risiken suchen willst.",
    after: input.kind === "generate"
      ? "Neue Karten erscheinen zur Prüfung."
      : "Code-Karten erscheinen einzeln gegatet zur Prüfung.",
  };
}

function actionForSkillApply(openSkillCount: number, manualReviewCount: number, queueHasWork: boolean): AutoresearchActionHint {
  if (openSkillCount === 0) {
    return {
      tone: "zinc",
      label: "Nichts offen",
      reason: "Es gibt gerade keine offenen Skill-Karten für Sammelübernahme.",
      after: queueHasWork ? "Code- oder Einzelreview-Karten direkt entscheiden." : "Bei Bedarf erst neue Kandidaten holen.",
    };
  }
  if (manualReviewCount > 0) {
    return {
      tone: "amber",
      label: "Einzelreview",
      reason: `${manualReviewCount} Skill-${manualReviewCount === 1 ? "Karte braucht" : "Karten brauchen"} bewusstes Lesen.`,
      after: "Entscheidungen öffnen und riskante Karten einzeln prüfen.",
    };
  }
  return {
    tone: "emerald",
    label: "Sicher möglich",
    reason: `${openSkillCount} Skill-${openSkillCount === 1 ? "Karte ist" : "Karten sind"} batch-sicher.`,
    after: "Sammelübernahme schreibt nur diese Skill-Karten.",
  };
}

function actionForPrune(revertedCount: number, queueHasWork: boolean): AutoresearchActionHint {
  if (revertedCount > 0) {
    return {
      tone: "emerald",
      label: "Aufräumen",
      reason: `${revertedCount} zurückgerollte ${revertedCount === 1 ? "Karte kann" : "Karten können"} aus dem Weg.`,
      after: "Die Liste wird kürzer, offene Entscheidungen bleiben erhalten.",
    };
  }
  return {
    tone: queueHasWork ? "zinc" : "cyan",
    label: "Optional",
    reason: queueHasWork ? "Aufräumen ersetzt kein Review der offenen Karten." : "Nur Pflege; es gibt keinen sichtbaren Rückstand.",
    after: queueHasWork ? "Erst entscheiden, wenn du Klarheit willst." : "Kann warten.",
  };
}
