import type { AutoresearchRun, ToneName } from "./types";

export interface AutoresearchRunSummary {
  tone: ToneName;
  label: string;
  title: string;
  detail: string;
  next: string;
  facts: { label: string; value: string; tone: ToneName }[];
}

export interface AutoresearchRunCard {
  tone: ToneName;
  label: string;
  title: string;
  detail: string;
  next: string;
  facts: { label: string; value: string; tone: ToneName }[];
}

export interface AutoresearchLastRunBrief {
  tone: ToneName;
  label: string;
  title: string;
  detail: string;
  next: string;
  facts: { label: string; value: string; tone: ToneName }[];
  rawLine: string | null;
}

export function getAutoresearchLastRunBrief(input: {
  lastRun: unknown | null | undefined;
  latestRun: AutoresearchRun | null;
  receipt?: string | null;
  note?: string | null;
}): AutoresearchLastRunBrief {
  const objectRun = input.lastRun && typeof input.lastRun === "object" ? input.lastRun as Record<string, unknown> : null;
  const fallbackRun = objectRun ? null : input.latestRun;
  const proposed = numberFrom(objectRun?.proposed ?? fallbackRun?.proposed);
  const kept = numberFrom(objectRun?.kept);
  const reverted = numberFrom(objectRun?.reverted);
  const scanned = numberFrom(objectRun?.skills_researched ?? fallbackRun?.scanned);
  const errors = numberFrom(objectRun?.research_errors ?? fallbackRun?.errors);
  const tokens = numberFrom(objectRun?.research_tokens ?? fallbackRun?.tokens);
  const refused = stringFrom(objectRun?.refused);
  const stopped = objectRun?.stopped === true;
  const mode = stringFrom(objectRun?.mode);
  const finishedAt = stringFrom(objectRun?.finished_at);
  const rawLine = [mode, finishedAt ? new Date(finishedAt).toLocaleString("de-DE") : null, input.receipt ? `Receipt ${input.receipt}` : null]
    .filter(Boolean)
    .join(" · ") || stringFrom(input.lastRun) || input.note || null;
  const facts = [
    { label: "Vorschläge", value: proposed === null ? "-" : String(proposed), tone: proposed && proposed > 0 ? "emerald" : "zinc" },
    { label: "Geprüft", value: scanned === null ? "-" : String(scanned), tone: scanned && scanned > 0 ? "cyan" : "zinc" },
    { label: "Tokens", value: tokens === null || tokens <= 0 ? "-" : tokens.toLocaleString("de-DE"), tone: tokens && tokens > 150_000 ? "amber" : "zinc" },
    { label: "Fehler", value: errors === null ? "-" : String(errors), tone: errors && errors > 0 ? "red" : "emerald" },
  ] satisfies AutoresearchLastRunBrief["facts"];

  if (!objectRun && !input.latestRun && !input.receipt && !input.note) {
    return {
      tone: "zinc",
      label: "Noch kein Lauf",
      title: "Noch keine Lauf-Auswertung.",
      detail: "Starte einen kleinen Dry-Run, damit hier ein verwertbares Ergebnis erscheint.",
      next: "Mit einem kleinen Preset starten und danach die Queue prüfen.",
      facts,
      rawLine: null,
    };
  }

  if (errors && errors > 0) {
    return {
      tone: "red",
      label: "Fehler prüfen",
      title: `${errors} ${errors === 1 ? "Fehler" : "Fehler"} im letzten Lauf.`,
      detail: "Der Lauf ist kein gutes Signal für einen weiteren Start mit gleichem Setup.",
      next: "Aktivität und Receipt prüfen, bevor du erneut startest.",
      facts,
      rawLine,
    };
  }

  if (refused) {
    return {
      tone: "amber",
      label: "Abgelehnt",
      title: "Der letzte Lauf wurde vom Backend abgelehnt.",
      detail: refused,
      next: "Route, Scope oder laufende Prozesse prüfen; danach erneut klein starten.",
      facts,
      rawLine,
    };
  }

  if (stopped) {
    return {
      tone: "cyan",
      label: "Gestoppt",
      title: "Der letzte Lauf wurde bewusst gestoppt.",
      detail: "Das Stop-Signal wurde verarbeitet. Es ist kein automatischer Fehler.",
      next: "Nur neu starten, wenn der Scope jetzt klarer ist.",
      facts,
      rawLine,
    };
  }

  if (proposed && proposed > 0) {
    return {
      tone: "emerald",
      label: "Hat geliefert",
      title: `${proposed} neue ${proposed === 1 ? "Karte" : "Karten"} für die Queue.`,
      detail: kept !== null || reverted !== null
        ? `Backend-Autotest: ${kept ?? 0} übernommen, ${reverted ?? 0} zurückgerollt.`
        : "Der Lauf hat verwertbare Entscheidungen erzeugt.",
      next: "Jetzt erst Queue bearbeiten; weitere Läufe später gezielter starten.",
      facts,
      rawLine,
    };
  }

  if (tokens && tokens > 150_000) {
    return {
      tone: "amber",
      label: "Teuer ruhig",
      title: "Viel Aufwand ohne neue Karten.",
      detail: "Der Lauf hat spürbar Tokens verbraucht, aber keine neue Entscheidung erzeugt.",
      next: "Scope enger wählen oder einen anderen Lauf-Typ nutzen.",
      facts,
      rawLine,
    };
  }

  return {
    tone: "cyan",
    label: "Ruhig",
    title: "Keine neuen Karten.",
    detail: scanned && scanned > 0 ? `${scanned} Ziele geprüft, ohne neue Entscheidungen.` : "Der Lauf blieb ohne messbare Treffer.",
    next: "Nur neu starten, wenn du den Scope bewusst änderst.",
    facts,
    rawLine,
  };
}

export function getAutoresearchRunCard(run: AutoresearchRun): AutoresearchRunCard {
  const tokens = safeNumber(run.tokens);
  const proposed = safeNumber(run.proposed);
  const errors = safeNumber(run.errors);
  const scanned = safeNumber(run.scanned);
  const vetoed = safeNumber(run.vetoed ?? 0);
  const facts = [
    { label: "Vorschläge", value: String(proposed), tone: proposed > 0 ? "emerald" : "zinc" },
    { label: "Geprüft", value: String(scanned), tone: scanned > 0 ? "cyan" : "zinc" },
    { label: "Tokens", value: tokens > 0 ? tokens.toLocaleString("de-DE") : "-", tone: tokens > 150_000 ? "amber" : "zinc" },
    { label: "Fehler", value: String(errors), tone: errors > 0 ? "red" : "emerald" },
    { label: "Veto", value: String(vetoed), tone: vetoed > 0 ? "amber" : "zinc" },
  ] satisfies AutoresearchRunCard["facts"];

  if (errors > 0) {
    return {
      tone: "red",
      label: "Fehler",
      title: `${errors} ${errors === 1 ? "Fehler" : "Fehler"} im Lauf.`,
      detail: "Dieser Lauf ist kein gutes Signal für weitere Starts mit gleichem Setup.",
      next: "Receipt und Aktivität prüfen, bevor du erneut startest.",
      facts,
    };
  }

  if (proposed > 0) {
    return {
      tone: "emerald",
      label: "Geliefert",
      title: `${proposed} neue ${proposed === 1 ? "Karte" : "Karten"} für die Queue.`,
      detail: "Der Lauf hat verwertbare Entscheidungen erzeugt.",
      next: "Jetzt erst Queue bearbeiten; weitere Läufe später gezielter starten.",
      facts,
    };
  }

  if (tokens > 150_000) {
    return {
      tone: "amber",
      label: "Teuer ruhig",
      title: "Viel Aufwand ohne neue Karten.",
      detail: "Das kann bedeuten, dass der Scope leer ist oder zu breit gesucht wurde.",
      next: "Scope enger wählen oder einen anderen Lauf-Typ nutzen.",
      facts,
    };
  }

  return {
    tone: "cyan",
    label: "Ruhig",
    title: "Keine neuen Karten.",
    detail: scanned > 0 ? `${scanned} Ziele geprüft, ohne neue Entscheidungen.` : "Der Lauf blieb ohne messbare Treffer.",
    next: "Nur neu starten, wenn du den Scope bewusst änderst.",
    facts,
  };
}

export function getAutoresearchRunSummary(input: {
  runs: readonly AutoresearchRun[];
  acceptanceRate: number | null;
  tokensPerApplied: number | null;
}): AutoresearchRunSummary {
  const latest = input.runs[0] ?? null;
  if (!latest) {
    return {
      tone: "zinc",
      label: "Noch kein Lauf",
      title: "Noch keine Lauf-Auswertung.",
      detail: "Starte einen kleinen Dry-Run oder einen gezielten Scan, damit Autoresearch eine messbare Spur bekommt.",
      next: "Mit 1-2 Iterationen starten und danach die Queue prüfen.",
      facts: [
        { label: "Läufe", value: "0", tone: "zinc" },
        { label: "Vorschläge", value: "0", tone: "zinc" },
        { label: "Fehler", value: "0", tone: "zinc" },
      ],
    };
  }

  const totalTokens = input.runs.reduce((sum, run) => sum + safeNumber(run.tokens), 0);
  const totalProposed = input.runs.reduce((sum, run) => sum + safeNumber(run.proposed), 0);
  const totalErrors = input.runs.reduce((sum, run) => sum + safeNumber(run.errors), 0);
  const latestTokens = safeNumber(latest.tokens);
  const latestProposed = safeNumber(latest.proposed);
  const latestErrors = safeNumber(latest.errors);

  if (latestErrors > 0) {
    return {
      tone: "red",
      label: "Fehler im letzten Lauf",
      title: "Erst Fehler prüfen, dann weiterlaufen lassen.",
      detail: `Der letzte Lauf meldete ${latestErrors} Fehler. Neue Läufe können sonst dieselbe Ursache wiederholen.`,
      next: "Aktivität und Receipt prüfen; danach erst neu starten.",
      facts: runFacts(totalTokens, totalProposed, totalErrors, input.acceptanceRate, input.tokensPerApplied),
    };
  }

  if (latestProposed > 0) {
    return {
      tone: "emerald",
      label: "Hat geliefert",
      title: `${latestProposed} neue ${latestProposed === 1 ? "Entscheidung" : "Entscheidungen"} im letzten Lauf.`,
      detail: "Der letzte Lauf hat verwertbare Kandidaten erzeugt. Jetzt ist Review sinnvoller als direkt noch ein Scan.",
      next: "Queue zuerst leeren, dann den nächsten Lauf kleiner oder gezielter starten.",
      facts: runFacts(totalTokens, totalProposed, totalErrors, input.acceptanceRate, input.tokensPerApplied),
    };
  }

  if (latestTokens > 150_000) {
    return {
      tone: "amber",
      label: "Teuer ohne Treffer",
      title: "Der letzte Lauf fand nichts, kostete aber spürbar Tokens.",
      detail: "Das ist nicht zwingend schlecht, aber ein weiterer gleicher Lauf wird wahrscheinlich wenig bringen.",
      next: "Scope enger wählen oder Code-/Deep-Audit statt breitem Skill-Scan nutzen.",
      facts: runFacts(totalTokens, totalProposed, totalErrors, input.acceptanceRate, input.tokensPerApplied),
    };
  }

  return {
    tone: "cyan",
    label: "Kein Treffer",
    title: "Der letzte Lauf blieb ruhig.",
    detail: "Es wurden keine neuen Entscheidungen erzeugt. Das kann ein gutes Signal sein, wenn der Scope klein war.",
    next: "Bei Bedarf Scope ändern oder erst bestehende Queue prüfen.",
    facts: runFacts(totalTokens, totalProposed, totalErrors, input.acceptanceRate, input.tokensPerApplied),
  };
}

function runFacts(totalTokens: number, totalProposed: number, totalErrors: number, acceptanceRate: number | null, tokensPerApplied: number | null): AutoresearchRunSummary["facts"] {
  return [
    { label: "Tokens", value: totalTokens > 0 ? totalTokens.toLocaleString("de-DE") : "-", tone: totalTokens > 500_000 ? "amber" : "zinc" },
    { label: "Vorschläge", value: String(totalProposed), tone: totalProposed > 0 ? "emerald" : "zinc" },
    { label: "Fehler", value: String(totalErrors), tone: totalErrors > 0 ? "red" : "emerald" },
    { label: "Annahme", value: acceptanceRate === null ? "-" : `${Math.round(acceptanceRate * 100)}%`, tone: acceptanceRate === null ? "zinc" : acceptanceRate >= 0.5 ? "emerald" : "amber" },
    { label: "Token/OK", value: tokensPerApplied === null ? "-" : Math.round(tokensPerApplied).toLocaleString("de-DE"), tone: tokensPerApplied === null ? "zinc" : tokensPerApplied > 250_000 ? "amber" : "cyan" },
  ];
}

function safeNumber(value: number): number {
  return Number.isFinite(value) && value > 0 ? value : 0;
}

function numberFrom(value: unknown): number | null {
  return typeof value === "number" && Number.isFinite(value) ? Math.max(0, value) : null;
}

function stringFrom(value: unknown): string | null {
  if (typeof value === "string") return value.trim() || null;
  if (typeof value === "number") return String(value);
  return null;
}
