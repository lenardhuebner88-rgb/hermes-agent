/**
 * AktivitaetPanel — „AKTIVITÄT" der Jarvis-Shell (Sprint 3, Karte S3.10):
 * Receipts + Commits der Klassik als HUD-Strip (neuestes Receipt + Zähler auf
 * einen Blick) mit Overlay-Drawer (Tabs RECEIPTS/COMMITS).
 *
 * Daten über EXAKT dieselben Hooks/Polling-Keys wie ProjekteView
 * (useProjectReceipts/useProjectCommits — der pollingStore dedupliziert über
 * Keys, kein zweiter Fetch) und dieselbe Ableitung (receiptEpoch/
 * commitAttributionLabel aus views/projekte/derive, Projekt-Namen über
 * useProjectNames). Zeilen-Inhalt wie die Klassik: Agent-Badge, Titel,
 * Projekt-Chip, relatives Alter bzw. Projekt-Tag, Message, Hash, Attribution.
 *
 * Receipt-Zeile (und der Strip-Teaser) öffnen das BESTEHENDE Lese-Sheet der
 * Klassik (ReceiptSheet, unverändert — es portalt wie dort ans body-Ende).
 * Commits bleiben read-only wie in der Klassik (kein Zeilenziel). Die
 * „Alle N anzeigen"-Disclosure ist das Expand-Idiom der Klassik. Fehler
 * inline (ReceiptsFeed-Idiom), nie still; Loading-/Empty-States ohne falsche
 * Leer-Zustände vor dem ersten Poll.
 */
import { useEffect, useState } from "react";

import { de } from "../i18n/de";
import { useProjectCommits, useProjectReceipts } from "../hooks/useControlData";
import { fmtRelativeTime, nowSec } from "../lib/derive";
import type { ProjectCommitFeedEntry, ProjectReceiptEntry } from "../lib/schemas";
import { commitAttributionLabel, receiptEpoch } from "../views/projekte/derive";
import { ReceiptSheet } from "../views/projekte/ReceiptSheet";
import { useProjectNames } from "./useProjectNames";

const t = de.jarvis;
const tp = de.projekte;

/** Anzeige-Cap je Tab im Drawer; der Rest sitzt hinter „Alle N anzeigen"
 *  (ReceiptsFeed-Idiom der Klassik, Backend-Cap 30 bleibt harte Obergrenze). */
const RECEIPTS_VISIBLE = 8;
const COMMITS_VISIBLE = 8;

type FeedTab = "receipts" | "commits";

export interface AktivitaetPanelProps {
  open: boolean;
  onToggle: () => void;
}

/** Eine Receipt-Zeile — Struktur der Klassik (ReceiptRow): Agent-Badge,
 *  Titel (truncate), Projekt-Chip (aufgelöster Name), relatives Alter. Die
 *  ganze Zeile ist EIN Button → öffnet das Lese-Sheet. */
function ReceiptJvRow({
  receipt,
  projectNames,
  now,
  onOpen,
}: {
  receipt: ProjectReceiptEntry;
  projectNames: Readonly<Record<string, string>>;
  now: number;
  onOpen: (receipt: ProjectReceiptEntry) => void;
}) {
  const title = receipt.title || receipt.filename;
  const projectName =
    receipt.project == null ? null : (projectNames[receipt.project] ?? receipt.project);

  return (
    <button
      type="button"
      className="jv-rrow"
      aria-label={tp.receiptOpenAria(title)}
      title={title}
      onClick={() => onOpen(receipt)}
    >
      <span className="jv-mini">{receipt.agent || "—"}</span>
      <span className="jv-row-title">{title}</span>
      {projectName ? <span className="jv-mini jv-proj">{projectName}</span> : null}
      <span className="jv-row-time">{fmtRelativeTime(receiptEpoch(receipt.mtime), now)}</span>
    </button>
  );
}

/** Eine Commit-Zeile — read-only wie die Klassik (CommitsFeed): Projekt-Tag,
 *  Message, Short-Hash, Attribution (Fallback Autor), relatives Alter. */
function CommitJvRow({ commit, now }: { commit: ProjectCommitFeedEntry; now: number }) {
  const attributionLabel = commitAttributionLabel(commit.attribution);
  const who = attributionLabel ?? (commit.author || null);

  return (
    <div className="jv-crow">
      <span className="jv-mini jv-proj">{commit.project_name || commit.project}</span>
      <span className="jv-row-title" title={commit.message}>
        {commit.message || tp.noCommitMessage}
      </span>
      <span className="jv-row-hash">{commit.hash}</span>
      {who ? (
        <span className="jv-mini" title={who}>
          {who}
        </span>
      ) : null}
      <span className="jv-row-time">{fmtRelativeTime(commit.committed_at, now)}</span>
    </div>
  );
}

export function AktivitaetPanel({ open, onToggle }: AktivitaetPanelProps) {
  const receipts = useProjectReceipts();
  const commits = useProjectCommits();
  const projectNames = useProjectNames();
  const now = nowSec();
  const [tab, setTab] = useState<FeedTab>("receipts");
  const [showAll, setShowAll] = useState(false);
  const [selected, setSelected] = useState<ProjectReceiptEntry | null>(null);

  const receiptList = receipts.data?.receipts ?? [];
  const commitList = commits.data?.commits ?? [];
  const latest = receiptList[0] ?? null;

  const visibleReceipts = showAll ? receiptList : receiptList.slice(0, RECEIPTS_VISIBLE);
  const visibleCommits = showAll ? commitList : commitList.slice(0, COMMITS_VISIBLE);

  const switchTab = (next: FeedTab) => {
    setTab(next);
    setShowAll(false);
  };

  // ESC schließt den Drawer (InboxPanel-Idiom der Shell).
  useEffect(() => {
    if (!open) return;
    const onKey = (event: KeyboardEvent) => {
      if (event.key === "Escape") onToggle();
    };
    window.addEventListener("keydown", onKey);
    return () => window.removeEventListener("keydown", onKey);
  }, [open, onToggle]);

  return (
    <>
      <div className="jv-float jv-strip">
        <span className="jv-strip-title">{t.aktivitaetTitle}</span>
        {receipts.error ? (
          <span className="jv-strip-tease jv-warn" title={tp.receiptsError}>
            !
          </span>
        ) : receipts.data === null ? (
          <span className="jv-strip-tease jv-dim">…</span>
        ) : latest ? (
          <button
            type="button"
            className="jv-strip-tease"
            aria-label={tp.receiptOpenAria(latest.title || latest.filename)}
            title={latest.title || latest.filename}
            onClick={() => setSelected(latest)}
          >
            {latest.title || latest.filename}
          </button>
        ) : (
          <span className="jv-strip-tease jv-dim">{t.aktivitaetEmpty}</span>
        )}
        <span className="jv-strip-count">
          {receipts.data !== null || commits.data !== null
            ? `${receiptList.length} · ${commitList.length}`
            : "—"}
        </span>
        <button
          type="button"
          className="jv-strip-toggle"
          aria-expanded={open}
          aria-controls="jv-aktiv-sheet"
          aria-label={t.aktivitaetExpandAria}
          onClick={onToggle}
        >
          {open ? "▾" : "▸"}
        </button>
      </div>

      {open ? (
        <div
          className="jv-float jv-sheet"
          id="jv-aktiv-sheet"
          role="region"
          aria-label={t.aktivitaetTitle}
        >
          <div className="jv-ptitle jv-fragen-head">
            {t.aktivitaetTitle}
            <button
              type="button"
              className="jv-fclose"
              onClick={onToggle}
              aria-label={t.aktivitaetClose}
            >
              ×
            </button>
          </div>

          <div className="jv-chips" role="tablist" aria-label={t.aktivitaetTitle}>
            <button
              type="button"
              role="tab"
              aria-selected={tab === "receipts"}
              className="jv-chip"
              onClick={() => switchTab("receipts")}
            >
              {t.receiptsTab} <span className="jv-n">{receiptList.length}</span>
            </button>
            <button
              type="button"
              role="tab"
              aria-selected={tab === "commits"}
              className="jv-chip"
              onClick={() => switchTab("commits")}
            >
              {t.commitsTab} <span className="jv-n">{commitList.length}</span>
            </button>
          </div>

          <div className="jv-fbody">
            {tab === "receipts" ? (
              <>
                {receipts.error ? (
                  <p className="jv-qerror" role="alert">
                    {tp.receiptsError}
                  </p>
                ) : null}
                {!receipts.error && receipts.data === null ? (
                  <p className="jv-qloading">{t.aktivitaetLoading}</p>
                ) : null}
                {!receipts.error && receipts.data !== null && receiptList.length === 0 ? (
                  <p className="jv-qloading">{tp.receiptsEmpty}</p>
                ) : null}
                {visibleReceipts.map((receipt) => (
                  <ReceiptJvRow
                    key={`${receipt.agent}:${receipt.filename}`}
                    receipt={receipt}
                    projectNames={projectNames}
                    now={now}
                    onOpen={setSelected}
                  />
                ))}
                {receiptList.length > RECEIPTS_VISIBLE ? (
                  <button
                    type="button"
                    className="jv-expand"
                    aria-expanded={showAll}
                    onClick={() => setShowAll((value) => !value)}
                  >
                    {showAll ? tp.receiptsShowLess : tp.receiptsShowAll(receiptList.length)}
                  </button>
                ) : null}
              </>
            ) : (
              <>
                {commits.error ? (
                  <p className="jv-qerror" role="alert">
                    {t.aktivitaetCommitsError}
                  </p>
                ) : null}
                {!commits.error && commits.data === null ? (
                  <p className="jv-qloading">{t.aktivitaetLoading}</p>
                ) : null}
                {!commits.error && commits.data !== null && commitList.length === 0 ? (
                  <p className="jv-qloading">{tp.commitsEmpty}</p>
                ) : null}
                {visibleCommits.map((commit) => (
                  <CommitJvRow
                    key={`${commit.project}:${commit.hash}:${commit.committed_at}`}
                    commit={commit}
                    now={now}
                  />
                ))}
                {commitList.length > COMMITS_VISIBLE ? (
                  <button
                    type="button"
                    className="jv-expand"
                    aria-expanded={showAll}
                    onClick={() => setShowAll((value) => !value)}
                  >
                    {showAll ? tp.receiptsShowLess : tp.receiptsShowAll(commitList.length)}
                  </button>
                ) : null}
              </>
            )}
          </div>
        </div>
      ) : null}

      {/* Lese-Sheet der Klassik, unverändert wiederverwendet (portalt ans
          body-Ende — gleiche Mechanik wie im Klassik-Tab). */}
      {selected ? <ReceiptSheet receipt={selected} onClose={() => setSelected(null)} /> : null}
    </>
  );
}
