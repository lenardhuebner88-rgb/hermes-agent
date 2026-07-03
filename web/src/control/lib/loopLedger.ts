/**
 * Ledger-Zeilen-Parser für den Loops-Tab ("Nachtschicht"-Logbuch).
 *
 * Format-Quelle: `loops/runner.py::LoopRunner.ledger()` — jede Zeile ist
 * `"- {YYYY-MM-DD HH:MM} {msg}\n"`; das Backend liefert sie roh als
 * `ledger_tail` (hermes_cli/control_loops.py, `splitlines()[-50:]`). Dieser
 * Parser zerlegt `{msg}` so weit wie möglich in Verdict/Runde/Phase/Dauer für
 * die Logbuch-Timeline — was nicht passt, bleibt unverändert als rohe Zeile
 * erhalten (kein Crash, kein Raten): defensiv by design, weil fremde/ältere
 * Ledger-Formate (siehe Tests) niemals die UI sprengen dürfen.
 */

export type LedgerVerdict = "ok" | "fail" | "warn" | "pause" | "land" | null;

export interface ParsedLedgerLine {
  verdict: LedgerVerdict;
  round?: number;
  phase?: string;
  secs?: number;
  raw: string;
}

const TIMESTAMP_PREFIX_RE = /^-\s+\d{4}-\d{2}-\d{2}\s+\d{2}:\d{2}\s+/;

/** Summe aller "{phase} {n}s"-Treffer in einer "[…]"-Dauerklammer, z.B.
 *  "build 812s · verify 340s" → 1152. undefined, wenn keine Klammer da war. */
function sumSecs(bracket: string | undefined): number | undefined {
  if (!bracket) return undefined;
  const matches = [...bracket.matchAll(/(\d+)s/g)];
  if (matches.length === 0) return undefined;
  return matches.reduce((sum, m) => sum + Number(m[1]), 0);
}

export function parseLedgerLine(line: string): ParsedLedgerLine {
  const raw = line;
  const body = line.replace(TIMESTAMP_PREFIX_RE, "");

  // R{n} ✅ {plan}.md verified ({sha}) [build Xs · verify Ys]
  let m = body.match(/^R(\d+)\s+✅\s+.+?\s+verified\s+\([0-9a-f]+\)(?:\s+\[([^\]]+)\])?\s*$/);
  if (m) return { verdict: "ok", round: Number(m[1]), phase: "verify", secs: sumSecs(m[2]), raw };

  // R{n} ❌ {plan}.md build-fail: …
  if ((m = body.match(/^R(\d+)\s+❌\s+\S+\s+build-fail:/)))
    return { verdict: "fail", round: Number(m[1]), phase: "build", raw };

  // R{n} ❌ {plan}.md verify-fail: … (reverted)
  if ((m = body.match(/^R(\d+)\s+❌\s+\S+\s+verify-fail:/)))
    return { verdict: "fail", round: Number(m[1]), phase: "verify", raw };

  // R{n} ⚠️ {plan}.md Commit vorhanden aber UNVERIFIED (usage-limit im Build)
  if ((m = body.match(/^R(\d+)\s+⚠️\s+\S+\s+.*UNVERIFIED/)))
    return { verdict: "warn", round: Number(m[1]), phase: "build", raw };

  // R{n} ⏸ {plan}.md zurück in die Queue (usage-limit, kein Commit)
  if ((m = body.match(/^R(\d+)\s+⏸\s+/)))
    return { verdict: "pause", round: Number(m[1]), phase: "build", raw };

  // R{n} sweep status={STATUS} [round Xs]
  m = body.match(/^R(\d+)\s+sweep status=(\S+)(?:\s+\[([^\]]+)\])?\s*$/);
  if (m) {
    const status = m[2];
    const verdict: LedgerVerdict =
      status.startsWith("DRY") ? "pause" : status.startsWith("BLOCKED") || status === "TIMEOUT" ? "fail" : "ok";
    return { verdict, round: Number(m[1]), phase: "round", secs: sumSecs(m[3]), raw };
  }

  // PLAN: {n} Pläne (status={status})
  if (/^PLAN:\s+\d+\s+Pläne\b/.test(body)) return { verdict: null, phase: "plan", raw };

  // bounced: {plan}.md ({reason}) — reason trägt oft "build: …"/"verify: …" voran.
  m = body.match(/^bounced:\s+\S+\s+\((.*)\)\s*$/);
  if (m) {
    const reason = m[1];
    const phase = reason.startsWith("build:") ? "build" : reason.startsWith("verify:") ? "verify" : undefined;
    return { verdict: "fail", phase, raw };
  }

  // LAND ✅ {n} Commits → main {sha} (Anker …, … Pläne archiviert)[ · piet-fork gepusht]
  if (/^LAND\s+✅\s+/.test(body)) return { verdict: "land", raw };

  // LAND abgebrochen: … / LAND rollback (Anker …): …
  if (/^LAND\s+(abgebrochen|rollback)\b/.test(body)) return { verdict: "fail", raw };

  // ⚠️ REVERT FEHLGESCHLAGEN ({range}): {msg}
  if (/^⚠️\s+REVERT FEHLGESCHLAGEN\b/.test(body)) return { verdict: "warn", raw };

  return { verdict: null, raw };
}
