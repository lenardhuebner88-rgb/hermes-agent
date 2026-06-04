import type { AutoresearchRun, ToneName } from "./types";

export interface AutoresearchRunSummary {
  tone: ToneName;
  label: string;
  title: string;
  detail: string;
  next: string;
  facts: { label: string; value: string; tone: ToneName }[];
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
