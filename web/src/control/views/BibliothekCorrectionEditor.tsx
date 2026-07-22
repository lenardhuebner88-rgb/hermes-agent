import { useId, useRef, useState } from "react";
import { fetchJSON } from "@/lib/api";
import { Overlay } from "../components/Overlay";
import {
  CHAIN_ROLE_LABEL,
  CHAIN_ROLE_ORDER,
  pathLabel,
  type LibraryProvenance,
  type LibraryProvenanceChain,
} from "./BibliothekView.helpers";

// ---------------------------------------------------------------------------
// P6b — Provenienz-Korrektur (Bibliothek). Eigenständige Komponente.
//
// Backend-Vertrag (verbindlich):
//   PUT  /api/library/correction          {item_id, fields, reason, confirm:true}
//        fields: flaches Teilobjekt der geänderten Keys
//        path|auftraggeber|delegation|autor|review|ablage — leerer Wert = null
//        (null hebt den Override auf, das Original kommt wieder durch).
//        Antwort: {correction, provenance} (ohne ok).
//   POST /api/library/correction/preview  {item_id, fields}
//        Antwort: {provenance, fields}; mutationsfrei, aber mit exakt derselben
//        Alias-/Unknown-/Merge-Normalisierung wie PUT.
//   POST /api/library/correction/revoke   {item_id, reason, confirm:true}
//        Antwort: {correction} (ohne ok).
//   Correction-Record: {item_id, fields, original, reason, actor, created_at,
//        updated_at, history:[{at, action, fields, reason, actor}]} —
//        history ist append-only, `fields` dort ein Objekt (kein Array).
// ---------------------------------------------------------------------------

/** Zulässige Werte für den Override des Wegs. */
export type CorrectionPath = "Cron" | "Task" | "Receipt" | "Manuell" | "Unbekannt";

const CORRECTION_PATHS: readonly CorrectionPath[] = ["Cron", "Task", "Receipt", "Manuell", "Unbekannt"];

/** Die sechs editierbaren Keys — Weg plus die fünf Rollen der Kette. */
const CORRECTION_FIELD_KEYS = ["path", "auftraggeber", "delegation", "autor", "review", "ablage"] as const;
export type CorrectionFieldKey = (typeof CORRECTION_FIELD_KEYS)[number];

/** Flache Override-Map. Der Server-Record enthält NUR aktive Keys (partiell);
 *  null hebt einen Override auf. Der PUT sendet nur tatsächlich geänderte
 *  Keys, damit parallele disjunkte Korrekturen einander nicht löschen. */
export type CorrectionFields = Partial<Record<CorrectionFieldKey, string | null>>;

/** Append-only-Audit-Zeile aus dem Correction-Record (fields partiell). */
export interface CorrectionAuditEntry {
  at: string | number;
  action: "set" | "revert";
  fields: Record<string, string | null>;
  reason: string;
  actor: string;
}

/** Ursprüngliche (automatisch hergeleitete) Provenanz aus dem Correction-Record. */
export interface CorrectionOriginal {
  producer: string;
  path: string;
  status: string;
  chain: LibraryProvenanceChain;
}

export interface LibraryCorrection {
  item_id: string;
  fields: CorrectionFields;
  original: CorrectionOriginal;
  /** Aktuelle automatische Ableitung; kann vom Erstsnapshot abweichen. */
  derived?: CorrectionOriginal;
  reason: string;
  actor: string;
  created_at: string | number;
  updated_at?: string | number | null;
  /** Item-Block-Kompatibilität: Kennzeichnet die aktuell wirksame Korrektur. */
  active?: boolean;
  history: CorrectionAuditEntry[];
}

export interface CorrectionPutResponse {
  correction: LibraryCorrection;
  /** Neu wirksame Provenanz (Original + Overrides). */
  provenance: LibraryProvenance;
}

export interface CorrectionRevokeResponse {
  /** Record der zurückgenommenen Korrektur (ihre History trägt den revoke-Eintrag). */
  correction: LibraryCorrection;
}

export interface CorrectionGetResponse {
  /** Voller Audit-Record; auch nach kompletter Rücknahme weiterhin vorhanden. */
  correction: LibraryCorrection | null;
}

export interface CorrectionPreviewResponse {
  provenance: LibraryProvenance;
  fields: CorrectionFields;
}

export interface BibliothekCorrectionEditorProps {
  itemId: string;
  /** Aktuell wirksame Provenanz (Original + aktive Overrides). */
  provenance: LibraryProvenance;
  /** Aktive Korrektur, falls vorhanden. */
  correction?: LibraryCorrection | null;
  /** Nach erfolgreichem Speichern/Zurücknehmen — darf asynchron sein (Refetch). */
  onChanged?: (correction: LibraryCorrection | null) => void | Promise<void>;
}

const EMPTY_FIELDS: CorrectionFields = {
  path: null,
  auftraggeber: null,
  delegation: null,
  autor: null,
  review: null,
  ablage: null,
};

const FIELD_LABEL: Record<CorrectionFieldKey, string> = {
  path: "Weg",
  auftraggeber: CHAIN_ROLE_LABEL.auftraggeber ?? "Auftraggeber",
  delegation: CHAIN_ROLE_LABEL.delegation ?? "Delegation",
  autor: CHAIN_ROLE_LABEL.autor ?? "Autor",
  review: CHAIN_ROLE_LABEL.review ?? "Review",
  ablage: CHAIN_ROLE_LABEL.ablage ?? "Ablage",
};

function fieldsFromCorrection(correction: LibraryCorrection | null | undefined): CorrectionFields {
  return { ...EMPTY_FIELDS, ...(correction?.fields ?? {}) };
}

function hasOverrides(correction: LibraryCorrection | null | undefined): boolean {
  return CORRECTION_FIELD_KEYS.some((key) => (correction?.fields?.[key] ?? null) !== null);
}

/** Weg-Wert in die kanonische Schreibweise normalisieren (unbekannt → "Unbekannt"). */
function normalizePath(value: string | undefined | null): CorrectionPath {
  const hit = CORRECTION_PATHS.find((p) => p.toLowerCase() === (value ?? "").toLowerCase());
  return hit ?? "Unbekannt";
}

function formatTs(ts: string | number | null | undefined): string {
  if (ts === null || ts === undefined || ts === "") return "–";
  const date = typeof ts === "number" ? new Date(ts < 1e12 ? ts * 1000 : ts) : new Date(ts);
  return Number.isNaN(date.getTime())
    ? String(ts)
    : date.toLocaleString("de-DE", { dateStyle: "short", timeStyle: "short" });
}

function errorText(e: unknown): string {
  return e instanceof Error ? e.message : String(e);
}

export function BibliothekCorrectionEditor({
  itemId,
  provenance,
  correction,
  onChanged,
}: BibliothekCorrectionEditorProps) {
  const uid = useId();
  const [open, setOpen] = useState(false);
  const [active, setActive] = useState<LibraryCorrection | null>(correction ?? null);
  const [fields, setFields] = useState<CorrectionFields>(() => fieldsFromCorrection(correction));
  const [reason, setReason] = useState("");
  const [revokeReason, setRevokeReason] = useState("");
  const [confirmAction, setConfirmAction] = useState<"save" | "revoke" | null>(null);
  const [busy, setBusy] = useState<"save" | "revoke" | null>(null);
  const [previewBusy, setPreviewBusy] = useState(false);
  const [serverPreview, setServerPreview] = useState<LibraryProvenance | null>(null);
  const [previewPayload, setPreviewPayload] = useState<CorrectionFields | null>(null);
  const [auditLoading, setAuditLoading] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [notice, setNotice] = useState<string | null>(null);
  const editorCloseRef = useRef<HTMLButtonElement>(null);
  const saveTriggerRef = useRef<HTMLButtonElement>(null);
  const revokeTriggerRef = useRef<HTMLButtonElement>(null);
  const confirmationReturnRef = useRef<HTMLElement | null>(null);

  const [activeForItem, setActiveForItem] = useState(itemId);
  const [prevCorrection, setPrevCorrection] = useState(correction);
  if (activeForItem !== itemId) {
    setActiveForItem(itemId);
    setPrevCorrection(correction);
    setActive(correction ?? null);
    setFields(fieldsFromCorrection(correction));
    setReason("");
    setRevokeReason("");
    setConfirmAction(null);
    setServerPreview(null);
    setPreviewPayload(null);
  } else if (prevCorrection !== correction) {
    // Eltern-Refetch nach onChanged soll den Editor sauber nachführen:
    // Prop-Diff beim Rendern statt Effect (react.dev: state-derivation-Muster).
    setPrevCorrection(correction);
    if (correction) {
      setActive(correction);
      setFields(fieldsFromCorrection(correction));
    } else if (hasOverrides(active)) {
      // Externe Rücknahme: keine wirksamen Overrides stehen lassen. Der volle
      // inaktive Audit-Record wird beim Öffnen über den GET-Pfad nachgeladen.
      setActive(null);
      setFields({ ...EMPTY_FIELDS });
    }
  }

  // Ursprünglich = correction.original (mit Fallback auf die wirksame Provenanz,
  // solange keine Korrektur existiert); wirksam = die provenance-Prop.
  const original: CorrectionOriginal = active?.original ?? {
    producer: provenance.producer,
    path: provenance.path,
    status: provenance.status,
    chain: provenance.chain,
  };
  const derived: CorrectionOriginal = active?.derived ?? (
    active && hasOverrides(active)
      ? active.original
      : {
          producer: provenance.producer,
          path: provenance.path,
          status: provenance.status,
          chain: provenance.chain,
        }
  );

  async function refreshAudit() {
    setAuditLoading(true);
    try {
      const res = await fetchJSON<CorrectionGetResponse>(
        `/api/library/correction?id=${encodeURIComponent(itemId)}`,
      );
      setActive(res.correction);
      setFields(fieldsFromCorrection(res.correction));
    } catch (e) {
      setError(errorText(e));
    } finally {
      setAuditLoading(false);
    }
  }

  // Öffnen ist mutationsfrei. Ein session-gated GET lädt den vollen Record,
  // damit auch eine vollständig zurückgenommene Historie im Produkt sichtbar ist.
  function toggleOpen() {
    if (previewBusy || busy !== null) return;
    if (open) {
      setOpen(false);
      setConfirmAction(null);
      setServerPreview(null);
      setPreviewPayload(null);
      return;
    }
    setOpen(true);
    setFields(fieldsFromCorrection(active));
    setError(null);
    setNotice(null);
    if (active === null) void refreshAudit();
  }

  function setField(key: CorrectionFieldKey, value: string) {
    setServerPreview(null);
    setPreviewPayload(null);
    setFields((prev) => ({ ...prev, [key]: value.trim() === "" ? null : value }));
  }

  /** Aktuell gespeicherte Override-Map des Records (leer ohne aktive Korrektur). */
  const activeFields = fieldsFromCorrection(active);

  /** Tatsächliche Änderung = die Override-Map weicht vom gespeicherten Record ab
   *  (null = kein Override; „kein Override“ gegen einen wirksamen Wert ist KEINE Änderung). */
  const dirtyKeys = CORRECTION_FIELD_KEYS.filter((key) => {
    const draft = fields[key] === "" ? null : fields[key];
    return (draft ?? null) !== (activeFields[key] ?? null);
  });
  const hasDirty = dirtyKeys.length > 0;

  /** Mindestens ein aktiver Override im Record (Voraussetzung für die Rücknahme). */
  const hasActiveOverrides = hasOverrides(active);

  const previewChain: LibraryProvenanceChain = { ...derived.chain };
  for (const role of CHAIN_ROLE_ORDER) {
    const draft = fields[role];
    previewChain[role] = draft == null || draft.trim() === ""
      ? derived.chain?.[role] ?? ""
      : draft.trim();
  }
  const previewPath = fields.path == null || fields.path.trim() === ""
    ? derived.path
    : normalizePath(fields.path);

  const canReviewSave = hasDirty && reason.trim().length > 0 && busy === null && !auditLoading && !previewBusy;
  const canReviewRevoke =
    active !== null &&
    hasActiveOverrides &&
    revokeReason.trim().length > 0 &&
    busy === null &&
    !auditLoading;

  /** PUT-Payload: nur geänderte Keys; leer → null (Original kommt wieder).
   *  Der Store merged unter Lock, sodass parallele disjunkte Änderungen leben. */
  function buildSaveFields(): CorrectionFields {
    const out: CorrectionFields = {};
    for (const key of dirtyKeys) {
      const raw = fields[key];
      out[key] = raw == null || raw.trim() === "" ? null : raw.trim();
    }
    return out;
  }

  async function openSaveConfirmation() {
    if (!canReviewSave) return;
    confirmationReturnRef.current = saveTriggerRef.current;
    setPreviewBusy(true);
    setPreviewPayload(null);
    setError(null);
    setNotice(null);
    try {
      const requestedFields = buildSaveFields();
      const res = await fetchJSON<CorrectionPreviewResponse>(
        "/api/library/correction/preview",
        {
          method: "POST",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify({ item_id: itemId, fields: requestedFields }),
        },
      );
      if (!res.provenance) throw new Error("Server hat keine Vorschau geliefert.");
      setServerPreview(res.provenance);
      setPreviewPayload(res.fields);
      setConfirmAction("save");
    } catch (e) {
      setError(errorText(e));
    } finally {
      setPreviewBusy(false);
    }
  }

  function closeConfirmation() {
    setConfirmAction(null);
    setServerPreview(null);
    setPreviewPayload(null);
  }

  async function handleSave() {
    if (confirmAction !== "save" || !serverPreview || !previewPayload || !canReviewSave) return;
    setBusy("save");
    setError(null);
    setNotice(null);
    let completed = false;
    try {
      const res = await fetchJSON<CorrectionPutResponse>("/api/library/correction", {
        method: "PUT",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({
          item_id: itemId,
          fields: previewPayload,
          reason: reason.trim(),
          confirm: true,
        }),
      });
      if (!res.correction) throw new Error("Server hat die Korrektur nicht bestätigt.");
      const next = res.correction;
      setActive(next);
      // Editorzustand sauber nachführen: Overrides spiegeln den Server-Record,
      // Grund und der bereits geschlossene Dialog werden zurückgesetzt.
      setFields(fieldsFromCorrection(next));
      setReason("");
      setServerPreview(null);
      setPreviewPayload(null);
      setNotice("Korrektur gespeichert — die Overrides sind jetzt wirksam.");
      completed = true;
      await onChanged?.(next);
    } catch (e) {
      setError(errorText(e));
    } finally {
      if (completed) confirmationReturnRef.current = editorCloseRef.current;
      setBusy(null);
      setConfirmAction(null);
    }
  }

  async function handleRevoke() {
    if (confirmAction !== "revoke" || !canReviewRevoke) return;
    setBusy("revoke");
    setError(null);
    setNotice(null);
    let completed = false;
    try {
      const res = await fetchJSON<CorrectionRevokeResponse>("/api/library/correction/revoke", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ item_id: itemId, reason: revokeReason.trim(), confirm: true }),
      });
      // Der Record bleibt absichtlich erhalten: Felder sind leer, die
      // append-only-Historie inklusive Revert bleibt weiterhin sichtbar.
      setActive(res.correction);
      setFields({ ...EMPTY_FIELDS });
      setRevokeReason("");
      setNotice("Korrektur vollständig zurückgenommen — das Original gilt wieder.");
      completed = true;
      await onChanged?.(null);
    } catch (e) {
      setError(errorText(e));
    } finally {
      if (completed) confirmationReturnRef.current = editorCloseRef.current;
      setBusy(null);
      setConfirmAction(null);
    }
  }

  function openRevokeConfirmation() {
    if (!canReviewRevoke) return;
    confirmationReturnRef.current = revokeTriggerRef.current;
    setConfirmAction("revoke");
  }

  const history = active?.history ?? [];

  return (
    <section aria-label="Provenienz-Korrektur" className="text-sec">
      {!open && (
        <button
          type="button"
          onClick={toggleOpen}
          aria-expanded="false"
          aria-controls={`${uid}-panel`}
          className="inline-flex min-h-12 items-center rounded-card border border-line px-3 text-micro text-ink-2 transition hover:border-live/40 hover:bg-surface-3"
        >
          {hasActiveOverrides ? "Korrektur aktiv · bearbeiten" : "Herkunft korrigieren"}
        </button>
      )}
      {open && (
        <div
          id={`${uid}-panel`}
          role="region"
          aria-label="Herkunft korrigieren"
          className="rounded-card border border-line bg-surface-2 p-3"
        >
          <div className="mb-3 flex flex-wrap items-center justify-between gap-2">
            <h4 className="font-display text-micro font-semibold uppercase tracking-[0.08em] text-ink-3">
              Provenienz-Korrektur
            </h4>
            <button
              ref={editorCloseRef}
              type="button"
              onClick={toggleOpen}
              disabled={previewBusy || busy !== null}
              aria-expanded="true"
              aria-controls={`${uid}-panel`}
              className="inline-flex min-h-12 items-center rounded-card border border-line px-3 text-micro text-ink-2 transition hover:border-live/40 hover:bg-surface-3"
            >
              Schließen
            </button>
          </div>

          {error && (
            <div role="alert" className="mb-3 break-words rounded-card border border-status-alert/30 bg-status-alert/10 px-3 py-2 text-sec text-status-alert">
              <span className="font-semibold">Fehler:</span> {error}
            </div>
          )}
          {notice && (
            <div role="status" className="mb-3 break-words rounded-card border border-status-ok/30 bg-status-ok/10 px-3 py-2 text-sec text-status-ok">
              {notice}
            </div>
          )}
          {auditLoading && (
            <p role="status" className="mb-3 text-sec text-ink-3">Audit-Historie wird geladen…</p>
          )}

          <div className="grid gap-3 sm:grid-cols-2">
            <div className="min-w-0 rounded-card border border-line bg-surface p-3">
              <h5 className="mb-2 text-micro font-semibold text-ink-3">Ursprünglich (automatisch hergeleitet)</h5>
              <dl className="grid gap-1.5">
                <div className="flex flex-wrap items-baseline justify-between gap-x-3">
                  <dt className="text-micro text-ink-3">Weg</dt>
                  <dd className="min-w-0 break-words font-data text-sec text-ink-2">{pathLabel(original.path)}</dd>
                </div>
                {CHAIN_ROLE_ORDER.map((role) => (
                  <div key={role} className="flex flex-wrap items-baseline justify-between gap-x-3">
                    <dt className="text-micro text-ink-3">{CHAIN_ROLE_LABEL[role] ?? role}</dt>
                    <dd className="min-w-0 break-words font-data text-sec text-ink-2">
                      {original.chain?.[role] || "–"}
                    </dd>
                  </div>
                ))}
              </dl>
            </div>

            <div className="min-w-0 rounded-card border border-line bg-surface p-3">
              <h5 className="mb-2 text-micro font-semibold text-ink-3">Overrides (wirksam nach Speichern)</h5>
              <div className="grid gap-2">
                {CORRECTION_FIELD_KEYS.map((key) => {
                  const currentValue = key === "path" ? derived.path || "" : derived.chain?.[key] ?? "";
                  const draft = fields[key] ?? null;
                  const overridden = draft !== null;
                  const dirty = dirtyKeys.includes(key);
                  const fieldId = key === "path" ? `${uid}-weg` : `${uid}-rolle-${key}`;
                  return (
                    <div key={key} className="grid gap-0.5">
                      <label htmlFor={fieldId} className="text-micro text-ink-3">
                        {FIELD_LABEL[key]}
                        {overridden && <span className="ml-1 text-live">(Override)</span>}
                        {dirty && <span className="ml-1 text-status-warn">(ändert)</span>}
                      </label>
                      {key === "path" ? (
                        <select
                          id={fieldId}
                          value={draft === null ? "" : normalizePath(draft)}
                          onChange={(e) => setField(key, e.target.value)}
                          disabled={busy !== null || auditLoading || previewBusy}
                          className="min-h-12 rounded-card border border-line bg-surface-2 px-3 text-sec text-ink disabled:opacity-50"
                        >
                          {/* Leer-Option entfernt den Weg-Override wie bei einer Rolle */}
                          <option value="">{`Automatisch aktuell: ${derived.path || "Unbekannt"}`}</option>
                          {CORRECTION_PATHS.map((p) => (
                            <option key={p} value={p}>
                              {p}
                            </option>
                          ))}
                        </select>
                      ) : (
                        <input
                          id={fieldId}
                          type="text"
                          value={draft ?? ""}
                          onChange={(e) => setField(key, e.target.value)}
                          disabled={busy !== null || auditLoading || previewBusy}
                          placeholder={currentValue === "" ? "Automatisch aktuell: unbekannt" : `Automatisch aktuell: ${currentValue}`}
                          className="min-h-12 w-full rounded-card border border-line bg-surface-2 px-3 text-sec text-ink placeholder:text-ink-3 disabled:opacity-50"
                        />
                      )}
                      {overridden && key !== "path" && (
                        <button
                          type="button"
                          onClick={() => setField(key, "")}
                          disabled={busy !== null || auditLoading || previewBusy}
                          className="justify-self-start rounded-card px-1 text-micro text-ink-3 underline-offset-2 transition hover:text-ink-2 hover:underline disabled:opacity-50"
                        >
                          Override entfernen
                        </button>
                      )}
                    </div>
                  );
                })}
              </div>
            </div>
          </div>

          <div className="mt-3 min-w-0 rounded-card border border-live/30 bg-live/5 p-3">
            <h5 className="mb-2 text-micro font-semibold text-bronze-hi">Entwurf (Server prüft vor Speichern)</h5>
            <dl className="grid gap-1.5 sm:grid-cols-2">
              <div className="flex flex-wrap items-baseline justify-between gap-x-3">
                <dt className="text-micro text-ink-3">Weg</dt>
                <dd className="min-w-0 break-words font-data text-sec text-ink-2">{pathLabel(previewPath)}</dd>
              </div>
              {CHAIN_ROLE_ORDER.map((role) => (
                <div key={role} className="flex flex-wrap items-baseline justify-between gap-x-3">
                  <dt className="text-micro text-ink-3">{CHAIN_ROLE_LABEL[role] ?? role}</dt>
                  <dd className="min-w-0 break-words font-data text-sec text-ink-2">
                    {previewChain[role] || "–"}
                  </dd>
                </div>
              ))}
            </dl>
          </div>

          <div className="mt-3 grid gap-0.5">
            <label htmlFor={`${uid}-grund`} className="text-micro text-ink-3">
              Begründung (Pflicht)
            </label>
            <textarea
              id={`${uid}-grund`}
              value={reason}
              onChange={(e) => {
                setReason(e.target.value);
                setServerPreview(null);
                setPreviewPayload(null);
              }}
              disabled={busy !== null || auditLoading || previewBusy}
              rows={2}
              placeholder="Warum weicht die tatsächliche Herkunft von der Herleitung ab?"
              className="w-full rounded-card border border-line bg-surface-2 px-3 py-2 text-sec text-ink placeholder:text-ink-3 disabled:opacity-50"
            />
            <button
              ref={saveTriggerRef}
              type="button"
              onClick={() => void openSaveConfirmation()}
              disabled={!canReviewSave}
              className="inline-flex min-h-12 items-center justify-center rounded-card border border-live/40 bg-live/10 px-4 text-sec font-medium text-bronze-hi transition hover:bg-live/15 disabled:opacity-50"
            >
              {previewBusy ? "Server prüft…" : busy === "save" ? "Speichern…" : "Korrektur prüfen"}
            </button>
          </div>

          {active && hasActiveOverrides && (
            <div className="mt-3 rounded-card border border-line bg-surface p-3">
              <h5 className="mb-2 text-micro font-semibold text-ink-3">Aktive Korrektur</h5>
              <p className="break-words text-sec text-ink-2">
                Korrigiert von <span className="font-data text-ink">{active.actor || "unbekannt"}</span>
                {" · angelegt "}
                {formatTs(active.created_at)}
                {active.updated_at != null && active.updated_at !== "" && (
                  <>{" · zuletzt geändert "}{formatTs(active.updated_at)}</>
                )}
              </p>
              <p className="mt-1 break-words text-sec text-ink-2">
                <span className="text-micro text-ink-3">Grund:</span> {active.reason}
              </p>

              <div className="mt-3 grid gap-0.5 border-t border-line pt-3">
                <label htmlFor={`${uid}-revoke-grund`} className="text-micro text-ink-3">
                  Rücknahme — Begründung (Pflicht)
                </label>
                <textarea
                  id={`${uid}-revoke-grund`}
                  value={revokeReason}
                  onChange={(e) => setRevokeReason(e.target.value)}
                  disabled={busy !== null || auditLoading || previewBusy}
                  rows={2}
                  placeholder="Warum soll die Korrektur weg?"
                  className="w-full rounded-card border border-line bg-surface-2 px-3 py-2 text-sec text-ink placeholder:text-ink-3 disabled:opacity-50"
                />
                <button
                  ref={revokeTriggerRef}
                  type="button"
                  onClick={openRevokeConfirmation}
                  disabled={!canReviewRevoke}
                  className="inline-flex min-h-12 items-center justify-center rounded-card border border-line px-4 text-sec text-ink-2 transition hover:border-live/40 hover:bg-surface-3 disabled:opacity-50"
                >
                  Rücknahme prüfen
                </button>
              </div>
            </div>
          )}

          <div className="mt-3 rounded-card border border-line bg-surface p-3">
            <h5 className="mb-2 text-micro font-semibold text-ink-3">Historie (append-only)</h5>
            {history.length === 0 ? (
              <p className="text-sec text-ink-3">Noch keine Korrektur-Ereignisse.</p>
            ) : (
              <ol className="grid gap-1.5">
                {history.map((entry, i) => (
                  <li key={`${entry.at}-${i}`} className="break-words border-l-2 border-line pl-2 text-sec text-ink-2">
                    <span className="font-data text-micro text-ink-3">{formatTs(entry.at)}</span>
                    {" · "}
                    <span className="text-ink">{entry.actor}</span>
                    {" · "}
                    {entry.action}
                    <span className="ml-1 font-data text-micro text-ink-3">
                      {Object.entries(entry.fields ?? {})
                        .map(([k, v]) => `${k}: ${v ?? "—"}`)
                        .join(" · ")}
                    </span>
                    <span className="block text-ink-2">{entry.reason}</span>
                  </li>
                ))}
              </ol>
            )}
          </div>

          {confirmAction && (
            <Overlay
              onClose={closeConfirmation}
              closeDisabled={busy !== null}
              restoreFocusRef={confirmationReturnRef}
              ariaLabel={confirmAction === "save" ? "Korrektur verbindlich speichern" : "Korrektur vollständig zurücknehmen"}
              maxWidthClassName="max-w-lg"
            >
                <h5 className="text-body font-semibold text-ink">
                  {confirmAction === "save" ? "Korrektur verbindlich speichern?" : "Korrektur vollständig zurücknehmen?"}
                </h5>
                <p className="mt-2 text-sec text-ink-2">
                  {confirmAction === "save"
                    ? "Erst diese Bestätigung sendet die Änderung. Originaldokumente bleiben unverändert."
                    : "Danach gilt wieder die automatische Herleitung; der Audit-Eintrag bleibt erhalten."}
                </p>
                <div className="mt-3 rounded-card border border-line bg-surface-2 p-3">
                  <p className="text-micro text-ink-3">Begründung</p>
                  <p className="mt-1 break-words text-sec text-ink">
                    {confirmAction === "save" ? reason.trim() : revokeReason.trim()}
                  </p>
                  {confirmAction === "save" && serverPreview && (
                    <dl className="mt-3 grid gap-1.5 border-t border-line pt-3">
                      <div className="flex flex-wrap items-baseline justify-between gap-x-3">
                        <dt className="text-micro text-ink-3">Weg (Vorschau)</dt>
                        <dd className="font-data text-sec text-ink-2">{pathLabel(serverPreview.path)}</dd>
                      </div>
                      {CHAIN_ROLE_ORDER.map((role) => (
                        <div key={role} className="flex flex-wrap items-baseline justify-between gap-x-3">
                          <dt className="text-micro text-ink-3">{CHAIN_ROLE_LABEL[role] ?? role}</dt>
                          <dd className="min-w-0 break-words font-data text-sec text-ink-2">{serverPreview.chain?.[role] || "–"}</dd>
                        </div>
                      ))}
                    </dl>
                  )}
                </div>
                <div className="mt-4 grid gap-2 sm:grid-cols-2">
                  <button
                    type="button"
                    onClick={closeConfirmation}
                    disabled={busy !== null}
                    className="inline-flex min-h-12 items-center justify-center rounded-card border border-line px-4 text-sec text-ink-2 transition hover:bg-surface-3 disabled:opacity-50"
                  >
                    Abbrechen
                  </button>
                  <button
                    type="button"
                    onClick={confirmAction === "save" ? handleSave : handleRevoke}
                    aria-disabled={busy !== null}
                    aria-busy={busy !== null}
                    className="inline-flex min-h-12 items-center justify-center rounded-card border border-live/40 bg-live/10 px-4 text-sec font-medium text-bronze-hi transition hover:bg-live/15 aria-disabled:pointer-events-none aria-disabled:cursor-wait aria-disabled:opacity-50"
                  >
                    {busy === "save"
                      ? "Wird gespeichert…"
                      : busy === "revoke"
                        ? "Wird zurückgenommen…"
                        : confirmAction === "save"
                          ? "Jetzt verbindlich speichern"
                          : "Jetzt vollständig zurücknehmen"}
                  </button>
                </div>
            </Overlay>
          )}
        </div>
      )}
    </section>
  );
}
