/**
 * usePlanspecDraft — S3.3-FE PlanSpec-Draft-Flow der Jarvis-Zone.
 *
 * Kontrakt (hermes_cli/pa_planspec.py, LIVE):
 *  - POST /api/pa/planspec/draft {idea, engine?, model?} → {draft_id,
 *    planspec_text, validation:{status: CLEAN|WARN|BLOCK, findings[]},
 *    slices[]}. 422 = Engine-Ausgabe ohne PlanSpec-Frontmatter
 *    (detail = {error, engine_output}), 400 = unbekannte Engine/Modell.
 *  - POST /api/pa/planspec/propose {draft_id} → {question_id}; 400 = BLOCK/
 *    stale, 404 = Draft weg, Duplikat = idempotent dieselbe question_id.
 *
 * Die Draft-Card ist eine CLIENT-intere Bubble-Art (kein Server-Verlauf):
 * der Hook hält sie als lokalen State, der Chat rendert sie im Thread.
 * engine/model folgen der S2.2-Switcher-Wahl (keine Wahl → Felder weg-
 * lassen, Backend-Default sol) — derselbe Kontrakt wie sendPaMessage.
 * Nach erfolgreichem Propose wird die S2.4-Inbox sofort neu geladen
 * (geteilter pollingStore-Key), damit die Approval-Card ohne Wartezeit
 * im Wartet-Panel/InboxPanel auftaucht.
 */
import { useCallback, useRef, useState } from "react";

import { api, type PaPlanspecDraft } from "@/lib/api";
import { refresh } from "../hooks/pollingStore";
import { de } from "../i18n/de";
import {
  findEngineSpec,
  getEngineChoice,
  getPaEnginesSnapshot,
} from "./engineSelection";
import { PA_INBOX_KEY } from "./usePaInbox";

const t = de.jarvis;

/** Kommando-Präfix der Frag-Leiste: „/plan <idee>" startet den Draft-Flow
 *  statt eines normalen Chat-Turns (A4-dezent: kein zusätzlicher Composer-
 *  Chrome). Gruppe 1 = die Idee (fehlt → Usage-Hinweis). Case-insensitive,
 *  damit Mobile-Autocaps („/Plan …") denselben Weg nimmt. */
export const PLAN_PREFIX_RE = /^\/plan(?:\s+([\s\S]+))?$/i;

export interface PlanspecDraftCard {
  /** Lokaler monotoner Key — die Card hat keine Server-ID im Verlauf. */
  key: number;
  idea: string;
  /** Angefragte Engine/Model (Switcher-Wahl oder Roster-Default) für das
   *  Provenienz-Badge — der Draft-Response trägt diese Felder nicht. */
  engine: string;
  model: string;
  phase: "drafting" | "ready" | "error";
  draft: PaPlanspecDraft | null;
  error: string | null;
  proposePhase: "idle" | "pending" | "done" | "error";
  questionId: number | null;
  proposeError: string | null;
}

/** fetchJSON wirft „<status>: <body>"; der Draft-422 trägt ein detail-OBJEKT
 *  ({error, engine_output}), 400/404 einen detail-String — beides lesbar
 *  machen, statt rohes JSON an die Bubble zu werfen. */
export function extractDraftError(err: unknown): string {
  const msg = err instanceof Error ? err.message : String(err);
  const body = msg.match(/^\d+:\s*([\s\S]*)$/)?.[1] ?? msg;
  try {
    const parsed = JSON.parse(body);
    if (parsed && parsed.detail && typeof parsed.detail.error === "string") {
      return parsed.detail.error;
    }
    if (parsed && typeof parsed.detail === "string") return parsed.detail;
  } catch {
    /* kein JSON — Rohtext verwenden */
  }
  return body || msg;
}

export function usePlanspecDraft() {
  const [cards, setCards] = useState<PlanspecDraftCard[]>([]);
  /** Usage-Hinweis („/plan" ohne Idee) — erscheint in der Composer-Fehler-
   *  zeile, gleiches Idiom wie die Chat-Composer-Fehler. */
  const [usageError, setUsageError] = useState<string | null>(null);
  const keyRef = useRef(0);
  /** In-flight Proposes je Card-Key — der Closure-Snapshot der Card ist im
   *  Doppelklick-Fenster stale, das Ref ist es nie. */
  const proposeInFlightRef = useRef(new Set<number>());

  const submitIdea = useCallback(async (idea: string) => {
    const trimmed = idea.trim();
    if (!trimmed) {
      setUsageError(t.planUsage);
      return;
    }
    setUsageError(null);
    // S2.2-Kontrakt: nur eine explizite Switcher-Wahl reist als engine/model
    // mit; ohne Wahl entscheidet das Backend (Default sol). Badge zeigt die
    // effektive Wahl (Roster-Default, solange das Roster nicht da ist: sol).
    const choice = getEngineChoice();
    const roster = getPaEnginesSnapshot();
    const engine = choice?.engine ?? roster?.default_engine ?? "sol";
    const model =
      choice?.model ?? findEngineSpec(roster, engine)?.default_model ?? "";
    keyRef.current += 1;
    const key = keyRef.current;
    const pending: PlanspecDraftCard = {
      key,
      idea: trimmed,
      engine,
      model,
      phase: "drafting",
      draft: null,
      error: null,
      proposePhase: "idle",
      questionId: null,
      proposeError: null,
    };
    setCards((current) => [...current, pending]);
    try {
      const draft = await api.draftPlanspec(
        trimmed,
        choice ? { engine: choice.engine, model: choice.model } : undefined,
      );
      setCards((current) =>
        current.map((c) => (c.key === key ? { ...c, phase: "ready", draft } : c)),
      );
    } catch (err) {
      // Fehler landen an der Card (Error-Bubble-Äquivalent), nie still.
      setCards((current) =>
        current.map((c) =>
          c.key === key
            ? { ...c, phase: "error", error: `${t.planDraftFailed} ${extractDraftError(err)}` }
            : c,
        ),
      );
    }
  }, []);

  const propose = useCallback(async (card: PlanspecDraftCard) => {
    if (card.phase !== "ready" || card.draft === null) return;
    if (card.proposePhase === "pending" || card.proposePhase === "done") return;
    const key = card.key;
    if (proposeInFlightRef.current.has(key)) return;
    proposeInFlightRef.current.add(key);
    setCards((current) =>
      current.map((c) =>
        c.key === key ? { ...c, proposePhase: "pending", proposeError: null } : c,
      ),
    );
    try {
      const result = await api.proposePlanspec(card.draft.draft_id);
      setCards((current) =>
        current.map((c) =>
          c.key === key
            ? { ...c, proposePhase: "done", questionId: result.question_id }
            : c,
        ),
      );
      // Die neue Approval-Card sofort in der S2.4-Inbox sichtbar machen;
      // ohne Subscriber (Tests) ist der Refresh ein No-op.
      void refresh(PA_INBOX_KEY).catch(() => {});
    } catch (err) {
      setCards((current) =>
        current.map((c) =>
          c.key === key
            ? {
                ...c,
                proposePhase: "error",
                proposeError: `${t.planProposeFailed} ${extractDraftError(err)}`,
              }
            : c,
        ),
      );
    } finally {
      proposeInFlightRef.current.delete(key);
    }
  }, []);

  return { cards, usageError, clearUsageError: () => setUsageError(null), submitIdea, propose };
}
