/**
 * WartetPanel — „Wartet · dezent" der Jarvis-Shell an der echten
 * Entscheidungs-Inbox (S2.4: GET /api/pa/inbox — offene Fragen +
 * pa_action-Cards + held chains + freigabe-Gates, serverseitig nach
 * Blockradius sortiert).
 *
 * Bleibt die dezente 3-Zeilen-Variante (A4: Entscheidungen NUR dezent).
 * Der Expand-Toggle öffnet die volle Inbox-Ansicht (InboxPanel, gleiche
 * Daten per Props): dort sitzen die Approval-Cards für pa_action
 * (Ausführen/Ablehnen über den bestehenden answer-Endpoint). Dezente Zeilen
 * je Typ: pa_action → PRÜFEN öffnet den Drawer, question → ANTWORTEN-Link in
 * die Klassik (S1-Muster), held/freigabe → Board-Link. Fehler werden inline
 * gezeigt (ReceiptsFeed-Idiom), Teilquellen-Ausfälle (errors[]) dezent —
 * nie still, nie ein Crash.
 *
 * S7.6: die Zeilen rendern entscheidungs-first — Decision-Headline statt
 * Roh-Titel (summary > clientseitige Destillation) plus 🔑/Alter/Blockradius-
 * Badges; der Roh-Titel bleibt im title-Attribut der Zeile. S8-WHY bleibt
 * bewusst im gemeinsamen Drawer-Expand (InboxPanel), nicht in der dezenten Zeile.
 *
 * `?inbox=open` öffnet den Drawer initial (Deep-Link/Screenshot-Naht).
 */
import { useEffect, useState } from "react";
import { Link } from "react-router-dom";

import type { PaInboxItem } from "@/lib/api";
import { de } from "../i18n/de";
import { decisionAge, decisionHeadline, needsOperatorKey } from "./decisionTitle";
import { InboxPanel } from "./InboxPanel";
import { usePaInbox } from "./usePaInbox";

const t = de.jarvis;

/** Wie viele Items die dezente Leiste maximal zeigt (A4-LIVE: 3 Zeilen). */
const WARTET_MAX_ROWS = 3;
/** Hinweiszeilen (Evidenz/Stale nach Approval) verfallen nach 20 s. */
const HINT_TTL_MS = 20_000;

/** Initial-Expand per Deep-Link (?inbox=open) — Screenshot-/Verlinkungs-Naht. */
function initialOpen(): boolean {
  if (typeof window === "undefined") return false;
  return new URLSearchParams(window.location.search).get("inbox") === "open";
}

/** Dezente Zeile je Item-Typ (Dot-Ton + Aktions-Affordanz rechts). */
function QuietRow({ item, onReview }: { item: PaInboxItem; onReview: () => void }) {
  // S7.6: entscheidungs-first — Zeilentext ist die Decision-Headline
  // (Server-`summary`, Fallback: Destillation), der Roh-Titel bleibt im
  // title-Attribut; Badges (🔑 Operator-Freigabe, Alter, Blockradius)
  // sitzen kompakt zwischen Text und Aktion.
  const headline = decisionHeadline(item);
  const age = decisionAge(item.ts);
  const badges = (
    <>
      {needsOperatorKey(item) ? (
        <span className="jv-qbadge jv-qbadge-key" title={t.inboxKeyTitle}>
          🔑
        </span>
      ) : null}
      {age ? <span className="jv-qbadge">{age}</span> : null}
      {item.block_radius > 0 ? (
        <span className="jv-qbadge jv-qbadge-blocked">{t.inboxBlocked(item.block_radius)}</span>
      ) : null}
    </>
  );
  if (item.type === "pa_action") {
    return (
      <div className="jv-qrow" data-testid={`jv-wartet-row-${item.id}`}>
        <span className="jv-rd jv-rd-amber" aria-hidden="true" />
        <span className="jv-tx" title={item.title}>
          {headline}
        </span>
        {badges}
        <button
          type="button"
          className="jv-ok jv-review"
          onClick={onReview}
          aria-label={t.inboxReviewAria(item.title)}
        >
          {t.inboxReview}
        </button>
      </div>
    );
  }
  if (item.type === "question") {
    return (
      <div className="jv-qrow" data-testid={`jv-wartet-row-${item.id}`}>
        <span className="jv-rd" aria-hidden="true" />
        <span className="jv-tx" title={item.title}>
          {headline}
        </span>
        {badges}
        <Link
          className="jv-ok"
          to="/control/projekte-klassisch"
          aria-label={t.wartetAnswer(item.title)}
        >
          ANTWORTEN
        </Link>
      </div>
    );
  }
  const isGate = item.type === "freigabe_gate";
  return (
    <div className="jv-qrow" data-testid={`jv-wartet-row-${item.id}`}>
      <span className={isGate ? "jv-rd jv-rd-violett" : "jv-rd jv-rd-blau"} aria-hidden="true" />
      <span className="jv-tx" title={item.title}>
        {headline}
      </span>
      {badges}
      <Link className="jv-ok" to={`/control/fleet?task=${encodeURIComponent(item.card_id)}`} aria-label={t.inboxBoardAria(item.title)}>
        BOARD
      </Link>
    </div>
  );
}

export function WartetPanel() {
  const inbox = usePaInbox();
  const [open, setOpen] = useState(initialOpen);
  const [hint, setHint] = useState<string | null>(null);
  const items = inbox.data?.items ?? [];
  const sourceErrors = inbox.data?.errors ?? [];
  const shown = items.slice(0, WARTET_MAX_ROWS);
  const extra = items.length - shown.length;
  // Der Expand braucht geladene Daten und mindestens ein Item; bei Fehler/
  // Erstladen bleibt nur die dezente Zeile bzw. der Fehler-/Ladehinweis.
  const canExpand = !inbox.error && inbox.data !== null && items.length > 0;

  // Hinweiszeilen nach Approval-Aktionen verfallen von selbst (dezent).
  useEffect(() => {
    if (!hint) return;
    const id = window.setTimeout(() => setHint(null), HINT_TTL_MS);
    return () => window.clearTimeout(id);
  }, [hint]);

  return (
    <>
      <div className="jv-ptitle">
        WARTET · DEZENT{" "}
        <span style={{ float: "right", color: "var(--faint)", letterSpacing: ".05em" }}>
          {items.length}
        </span>
      </div>

      {inbox.error ? (
        <p className="jv-qerror" role="alert">
          {t.wartetError}
        </p>
      ) : null}

      {!inbox.error && inbox.data === null ? (
        <p className="jv-qloading">{t.wartetLoading}</p>
      ) : null}

      {!inbox.error && inbox.data !== null && items.length === 0 ? (
        <div className="jv-quietempty">
          ✓ Nichts wartet — Estate ruhig.
          <span>Neue Entscheidungen erscheinen hier dezent, nie als Popup.</span>
        </div>
      ) : null}

      {/* Teilquellen-Ausfall (errors[]): dezent, der Rest der Inbox gilt
          weiter — kein Crash, kein Verstecken (Brief). */}
      {sourceErrors.map((err) => (
        <p className="jv-srcerr" key={err.source} title={err.error}>
          {t.inboxSourceError(err.source)}
        </p>
      ))}

      {hint ? (
        <p className="jv-hint" data-testid="jv-wartet-hint">
          {hint}
        </p>
      ) : null}

      {shown.map((item) => (
        <QuietRow key={item.id} item={item} onReview={() => setOpen(true)} />
      ))}

      {canExpand ? (
        <button
          type="button"
          className="jv-expand"
          aria-expanded={open}
          aria-controls="jv-inbox-panel"
          onClick={() => setOpen((value) => !value)}
        >
          {open ? t.inboxCollapse : extra > 0 ? t.inboxExpand(extra) : t.inboxExpandAll}
        </button>
      ) : null}

      {open && canExpand ? (
        <InboxPanel
          items={items}
          onClose={() => setOpen(false)}
          onRefresh={inbox.reload}
          onHint={setHint}
        />
      ) : null}
    </>
  );
}
